"""Phase 2 integration — the whole fleet core wired together (U10).

U10 is the Phase-2 capstone. The fleet's design is *one source of truth, many
renderers* (plan.md §3): :class:`FleetState` is fed by polling the durable
``claude --bg`` roster via :class:`SessionManager` / :class:`FleetPoller`, and the
**headless CLI**, the **web dashboard** and the **robot body** are all just
subscribers / serializers of the same :class:`FleetSnapshot`. These tests pin down
that the pieces compose:

- a single synthetic snapshot, rendered by all three renderers, makes them *agree*
  on who needs attention and what colour that manager is (no ``claude`` needed);
- the body renderer's idle-breathing policy (U10 decision) at the software level:
  an attention/failure pose fires the optional *hold* hook (linger), a steady
  working pose does not (accept the momentary breathing glance);
- one real end-to-end pass: spawn a REAL trivial ``claude --bg`` manager, poll it
  into one FleetState, and confirm it shows up through the CLI, the dashboard AND
  the body renderer at once — then always tear the session down (no stray agents).

The on-robot end-to-end (talk → spawn → observe on body + dashboard) is a HUMAN
gate (see ``docs/roadmap/STATE.yaml`` → ``status.pending_gate``); it cannot be
self-verified here.
"""

from __future__ import annotations
import uuid
import shutil

import pytest


pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from reachy_fleet_supervisor.fleet import (  # noqa: E402
    SIGNAL_WORKING,
    SENTINEL_RUNNING,
    SIGNAL_ATTENTION,
    SENTINEL_HUMAN_GATE,
    BodyPose,
    AgentInfo,
    FleetState,
    FleetPoller,
    ManagerStatus,
    SessionManager,
    FleetBodyRenderer,
    FleetVoiceRenderer,
    VOICE_GATE,
    VOICE_COMPLETED,
    body_signal_for,
    snapshot_payload,
    create_dashboard_app,
)
from reachy_fleet_supervisor.fleet import cli as cli_mod  # noqa: E402
from reachy_fleet_supervisor.fleet import state as state_mod  # noqa: E402
from reachy_fleet_supervisor.fleet.cli import _snapshot_dict  # noqa: E402


_HAS_CLAUDE = shutil.which("claude") is not None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _row(short_id: str, name: str, state: str) -> AgentInfo:
    return AgentInfo.from_dict(
        {
            "id": short_id,
            "sessionId": f"{short_id}-0000-0000-0000-000000000000",
            "kind": "background",
            "name": name,
            "cwd": "/proj",
            "state": state,
            "status": state,
        }
    )


class _Recorder:
    """Records applied poses (the fake body applier)."""

    def __init__(self) -> None:
        self.poses: list[BodyPose] = []

    def __call__(self, pose: BodyPose) -> None:
        self.poses.append(pose)


# ---------------------------------------------------------------------------
# All three renderers agree on one snapshot (the integration invariant)
# ---------------------------------------------------------------------------


def test_fleet_core_renderers_agree_on_one_snapshot() -> None:
    """CLI, dashboard and body all read the same FleetSnapshot consistently.

    Three managers, keys sorted build < docs < tests → slots [-span, 0, +span].
    'tests' (right slot) hits a HUMAN_GATE; build + docs keep working. Every
    renderer must agree that 'tests' is the one needing attention.
    """
    state = FleetState()
    applier = _Recorder()
    renderer = FleetBodyRenderer(applier)
    renderer.attach(state, fire_immediately=False)

    snapshot = state.apply(
        [
            _row("build", "build", "working"),
            _row("docs", "docs", "working"),
            _row("tests", "tests", "blocked"),
        ],
        statuses={
            "build": ManagerStatus(state=SENTINEL_RUNNING, name="build"),
            "docs": ManagerStatus(state=SENTINEL_RUNNING, name="docs"),
            "tests": ManagerStatus(
                state=SENTINEL_HUMAN_GATE, name="tests", summary="approve deploy?"
            ),
        },
        now=1.0,
    )

    # --- BODY renderer: faces the waiting 'tests' manager (right slot), attention.
    assert renderer.last_pose is not None
    body_pose = renderer.last_pose
    assert body_pose.target_key == "tests"
    assert body_pose.signal == SIGNAL_ATTENTION
    assert body_pose.body_yaw_deg > 0  # right slot

    # The pure mapping over the same snapshot matches the subscribed renderer.
    assert body_signal_for(snapshot) == body_pose

    # --- DASHBOARD: same snapshot → 'tests' flagged needs_attention; HTML shows all.
    payload = snapshot_payload(snapshot)
    dash_attn = [m for m in payload["managers"] if m["needs_attention"]]
    assert [m["name"] for m in dash_attn] == ["tests"]
    html = create_dashboard_app(state)  # serves the same state
    page = TestClient(html).get("/").text
    for who in ("build", "docs", "tests"):
        assert who in page

    # --- CLI serializer: identical view of the same snapshot (dashboard wraps it).
    cli_dict = _snapshot_dict(snapshot, include_transcript=True)
    assert cli_dict == payload, "dashboard JSON must not drift from `fleet status --json`"
    cli_attn = [m["name"] for m in cli_dict["managers"] if m["needs_attention"]]
    assert cli_attn == ["tests"]

    # --- All three agree on WHO + colours are the single source of truth (U9).
    colors = {m.key: (m.color.hex if m.color else None) for m in snapshot.managers}
    for m in cli_dict["managers"]:
        assert m["color"]["hex"] == colors[m["id"]]
    assert len(set(colors.values())) == len(colors), "distinct colour per manager"


# ---------------------------------------------------------------------------
# Idle-breathing policy (U10 decision) — hold only on attention/failure
# ---------------------------------------------------------------------------


def test_body_holds_attention_poses_but_lets_working_relax() -> None:
    """The hold hook fires for an attention signal, not for a steady working one.

    Decision: don't suppress breathing continuously (a frozen robot reads as
    "dead"); accept a momentary glance for steady states, but linger on an
    attention/failure signal via the hold hook. Tested at the software seam.
    """
    held: list[BodyPose] = []
    renderer = FleetBodyRenderer(_Recorder(), hold=held.append)
    state = FleetState()
    renderer.attach(state, fire_immediately=False)

    # Everyone working → body relaxes (no hold; the breathing glance is accepted).
    state.apply([_row("a1", "alpha", "working")], now=1.0)
    assert renderer.last_pose is not None and renderer.last_pose.signal == SIGNAL_WORKING
    assert held == [], "a steady working pose must not be held"

    # alpha needs a human → attention pose is held (lingered on).
    state.apply([_row("a1", "alpha", "blocked")], now=2.0)
    assert held and held[-1].signal == SIGNAL_ATTENTION
    assert held[-1].wants_attention

    # Back to working → relaxes again; no new hold.
    before = len(held)
    state.apply([_row("a1", "alpha", "working")], now=3.0)
    assert len(held) == before


# ---------------------------------------------------------------------------
# Missed-gate reproduction: the FULL poller → state → voice signal path fires a
# spoken escalation when a manager goes blocked/waiting on the human (PROBLEM 1),
# deterministically (injected roster source; no subprocess).
# ---------------------------------------------------------------------------


def test_voice_speaks_when_manager_blocks_waiting_on_human_via_poller() -> None:
    """A manager that goes `working` → `blocked`/`waitingFor` (with NO emitted
    HUMAN_GATE sentinel — e.g. Claude asked a clarifying question and parked) must
    drive a spoken escalation through the real FleetPoller → FleetState →
    FleetVoiceRenderer chain. This is the exact path that was silent before."""
    spoken: list[str] = []
    state = FleetState()
    voice = FleetVoiceRenderer(speak=spoken.append)
    voice.attach(state, fire_immediately=False)

    # Roster source we can flip: first working, then blocked+waitingFor. No status.
    rows = {"cur": [_row("builder", "builder", "working")]}
    poller = FleetPoller(state, agents_source=lambda: rows["cur"])

    poller.poll_once()  # prime — working, nothing spoken
    assert spoken == []

    rows["cur"] = [
        AgentInfo.from_dict(
            {
                "id": "builder",
                "sessionId": "builder-0000-0000-0000-000000000000",
                "kind": "background",
                "name": "builder",
                "cwd": "/proj",
                "state": "blocked",
                "status": "idle",
                "waitingFor": "which port should it bind?",
            }
        )
    ]
    poller.poll_once()  # now waiting on the human → must speak an escalation
    assert len(spoken) == 1, spoken
    assert "builder" in spoken[0]
    assert "which port should it bind?" in spoken[0]


# ---------------------------------------------------------------------------
# End-to-end with a REAL bg session through all three renderers (cleaned up)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _HAS_CLAUDE, reason="claude CLI not installed")
def test_fleet_core_end_to_end_real_bg_session() -> None:
    """Spawn a REAL bg manager; one poll lands it in CLI + dashboard + body at once.

    Always stops + removes the session (no stray background agents), even if an
    assertion fails first.
    """
    name = f"u10-int-{uuid.uuid4().hex[:8]}"
    sm = SessionManager()
    mgr = None
    spawned_id = None
    try:
        mgr = sm.spawn("Reply with the single word DONE and do nothing else.", name=name)
        spawned_id = mgr.id

        # One real poll into a single FleetState, scoped to just our session.
        state = FleetState()
        poller = FleetPoller(
            state,
            predicate=lambda a: a.session_id == mgr.session_id,
            include_all=True,
        )
        snapshot = poller.poll_once()
        assert any(m.session_id == mgr.session_id for m in snapshot.managers), snapshot

        # Body renderer attached to the SAME state reacted to the poll.
        applier = _Recorder()
        renderer = FleetBodyRenderer(applier)
        renderer.attach(state, fire_immediately=True)
        assert renderer.last_pose is not None
        assert applier.poses, "body renderer should have applied at least one pose"

        # Dashboard over the same state shows the manager (JSON + HTML).
        client = TestClient(create_dashboard_app(state))
        data = client.get("/api/fleet").json()
        assert any(m["name"] == name for m in data["managers"])
        assert name in client.get("/").text

        # CLI path (independent reconnect to the live roster) also sees it.
        out: list[str] = []

        class _Buf:
            def write(self, s: str) -> int:
                out.append(s)
                return len(s)

        code = cli_mod.main(["status", name, "--all", "--json"], out=_Buf(), err=_Buf())
        assert code == 0
        assert name in "".join(out)
    finally:
        if mgr is not None:
            mgr.stop_and_remove()
        elif spawned_id is not None:  # pragma: no cover - spawn-time failure path
            from reachy_fleet_supervisor.fleet import FleetManager

            FleetManager(session_id="x", id=spawned_id, name=name).stop_and_remove()
        remaining = [
            a for a in state_mod.list_agents(include_all=False) if a.name == name
        ]
        assert remaining == [], f"manager {name} still active after teardown"
