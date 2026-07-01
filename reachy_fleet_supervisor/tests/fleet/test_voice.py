"""Tests for the voice renderer — voice renders FleetState (U15, decision #17).

U15 is a *hardware* gate: real speech is confirmed by a human on the robot. What
is software-testable is everything up to the audio — the pure state-change →
spoken-phrase mapping, the gate-policy-driven escalation, the edge-triggered
FleetState subscriber, and the resume-on-answer path — which is what these tests
pin down:

- ``speakable_kind`` classifies failed / escalating-gate / completed and stays
  silent on running and on *autonomous* gates (reusing U14's gate policy).
- ``voice_phrase_for`` front-loads the manager name and folds in a reason.
- ``FleetVoiceRenderer`` primes silently on the first snapshot, speaks on a real
  transition (completion + failure + gate), is edge-triggered (no repeats),
  survives a bad speak callback, and drives off a real ``FleetState``.
- the ``answer_gate`` voice tool routes the spoken answer through
  ``FleetController.send`` (``claude --resume -p``) to resume the gated manager.
"""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path

import pytest

from reachy_fleet_supervisor.fleet import (
    AgentInfo,
    FleetState,
    FleetSnapshot,
    FleetRuntime,
    FleetPoller,
    ManagerSnapshot,
    ManagerStatus,
    GatePolicy,
    GATE_TRIGGER_KEY,
    SENTINEL_FAILED,
    SENTINEL_COMPLETE,
    SENTINEL_HUMAN_GATE,
    SENTINEL_RUNNING,
    VOICE_FAILED,
    VOICE_GATE,
    VOICE_COMPLETED,
    FleetVoiceRenderer,
    speakable_kind,
    voice_phrase_for,
    set_fleet_runtime,
)


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


def _bg(
    short_id: str,
    name: str,
    state: str = "working",
    *,
    waiting_for: str | None = None,
) -> AgentInfo:
    return AgentInfo.from_dict(
        {
            "id": short_id,
            "sessionId": f"{short_id}-0000-0000-0000-000000000000",
            "kind": "background",
            "name": name,
            "cwd": "/proj",
            "state": state,
            "status": state,
            "waitingFor": waiting_for,
        }
    )


def _snap(m: ManagerSnapshot) -> ManagerSnapshot:
    return m


def _ms(
    name: str,
    *,
    state: str = "working",
    report_state: str | None = None,
    gate: str | None = None,
    summary: str | None = None,
    gate_trigger: str | None = None,
    waiting_for: str | None = None,
) -> ManagerSnapshot:
    report = None
    if report_state is not None:
        extra = {GATE_TRIGGER_KEY: gate_trigger} if gate_trigger else {}
        report = ManagerStatus(
            state=report_state, name=name, gate=gate, summary=summary, extra=extra
        )
    return ManagerSnapshot.from_agent_info(
        _bg(name[:8], name, state, waiting_for=waiting_for), report=report
    )


def _fleet(*managers: ManagerSnapshot) -> FleetSnapshot:
    return FleetSnapshot(managers=tuple(managers), updated_at=1.0)


# ---------------------------------------------------------------------------
# speakable_kind — the pure classification (reuses U14 gate policy)
# ---------------------------------------------------------------------------


def test_speakable_kind_failed_from_report_and_roster() -> None:
    assert speakable_kind(_ms("a", report_state=SENTINEL_FAILED)) == VOICE_FAILED
    # roster-only fallback (no report)
    assert speakable_kind(_ms("a", state="failed")) == VOICE_FAILED


def test_speakable_kind_completed_from_report_and_roster() -> None:
    assert speakable_kind(_ms("a", report_state=SENTINEL_COMPLETE)) == VOICE_COMPLETED
    assert speakable_kind(_ms("a", state="done")) == VOICE_COMPLETED


def test_speakable_kind_running_is_silent() -> None:
    assert speakable_kind(_ms("a", report_state=SENTINEL_RUNNING)) is None
    assert speakable_kind(_ms("a", state="working")) is None


def test_bare_gate_speaks_by_default() -> None:
    # A bare HUMAN_GATE (no trigger) always escalates → spoken.
    assert speakable_kind(_ms("a", report_state=SENTINEL_HUMAN_GATE)) == VOICE_GATE


def test_autonomous_labelled_gate_is_silent() -> None:
    # An autonomous policy with an unrelated escalate_on list: a *labelled* gate
    # whose trigger isn't escalated stays silent (auto-approvable).
    policy = GatePolicy(mode="autonomous", escalate_on=["publish"])
    m = _ms("a", report_state=SENTINEL_HUMAN_GATE, gate_trigger="format")
    assert speakable_kind(m, policy=policy) is None


def test_escalating_labelled_gate_speaks() -> None:
    policy = GatePolicy(mode="autonomous", escalate_on=["push"])
    m = _ms("a", report_state=SENTINEL_HUMAN_GATE, gate_trigger="push")
    assert speakable_kind(m, policy=policy) == VOICE_GATE


def test_gated_mode_speaks_every_gate() -> None:
    policy = GatePolicy(mode="gated")
    m = _ms("a", report_state=SENTINEL_HUMAN_GATE, gate_trigger="anything")
    assert speakable_kind(m, policy=policy) == VOICE_GATE


# --- the missed-escalation fix: a manager waiting on the human via the roster ---
# (blocked / waitingFor) with NO emitted HUMAN_GATE sentinel must still speak.


def test_speakable_kind_roster_blocked_speaks_as_gate() -> None:
    # No report at all — just a roster row that went `blocked` (e.g. Claude asked
    # a clarifying question and parked). Previously silent; now a spoken gate.
    assert speakable_kind(_ms("a", state="blocked")) == VOICE_GATE


def test_speakable_kind_roster_waiting_for_speaks_as_gate() -> None:
    # `state` may stay working-ish but `waitingFor` is set → still waiting on us.
    m = _ms("a", state="working", waiting_for="permission prompt")
    assert speakable_kind(m) == VOICE_GATE


def test_roster_gate_ignores_autonomous_policy() -> None:
    # A bare block/wait has no gate *trigger* to classify, so even a restrictive
    # autonomous policy speaks it — the agent can't progress without a human.
    policy = GatePolicy(mode="autonomous", escalate_on=["publish"])
    assert speakable_kind(_ms("a", state="blocked"), policy=policy) == VOICE_GATE


def test_roster_gate_phrase_uses_waiting_for_detail() -> None:
    m = _ms("builder", state="blocked", waiting_for="which port should it bind?")
    phrase = voice_phrase_for(VOICE_GATE, m)
    assert "builder" in phrase
    assert "which port should it bind?" in phrase


def test_completed_report_wins_over_blocked_roster() -> None:
    # A terminal COMPLETE report is spoken as completion even if the exited bg
    # roster row still reads oddly — completion is checked before the roster gate.
    m = _ms("a", state="blocked", report_state=SENTINEL_COMPLETE)
    assert speakable_kind(m) == VOICE_COMPLETED


def test_renderer_speaks_on_transition_to_blocked_roster_gate() -> None:
    # End-to-end through the edge-triggered renderer: a manager that goes from
    # working → blocked (waiting on the human) is spoken exactly once.
    spoken: list[str] = []
    r = FleetVoiceRenderer(speak=spoken.append)
    r.on_snapshot(_fleet(_ms("builder", state="working")))  # prime, silent
    r.on_snapshot(_fleet(_ms("builder", state="blocked", waiting_for="need input")))
    assert len(spoken) == 1
    assert "builder" in spoken[0]


# ---------------------------------------------------------------------------
# voice_phrase_for — terse, name-first, reason-folding
# ---------------------------------------------------------------------------


def test_phrase_failed_mentions_name_and_needs_you() -> None:
    phrase = voice_phrase_for(VOICE_FAILED, _ms("tester", report_state=SENTINEL_FAILED))
    assert "tester" in phrase
    assert "needs you" in phrase.lower()


def test_phrase_completed_mentions_finished() -> None:
    phrase = voice_phrase_for(VOICE_COMPLETED, _ms("tester", report_state=SENTINEL_COMPLETE))
    assert "tester" in phrase
    assert "finished" in phrase.lower()


def test_phrase_gate_folds_in_gate_text_first_line() -> None:
    m = _ms(
        "tester",
        report_state=SENTINEL_HUMAN_GATE,
        gate="Which port should it bind?\nmore detail here",
    )
    phrase = voice_phrase_for(VOICE_GATE, m)
    assert "tester" in phrase
    assert "Which port should it bind?" in phrase
    assert "more detail here" not in phrase  # only the first line


# ---------------------------------------------------------------------------
# FleetVoiceRenderer — edge-triggered subscriber
# ---------------------------------------------------------------------------


def test_renderer_primes_silently_on_first_snapshot() -> None:
    spoken: list[str] = []
    r = FleetVoiceRenderer(speak=spoken.append)
    # First snapshot already has a completed manager — must NOT blurt it.
    r.on_snapshot(_fleet(_ms("a", report_state=SENTINEL_COMPLETE)))
    assert spoken == []


def test_renderer_speaks_on_transition_to_completed() -> None:
    spoken: list[str] = []
    r = FleetVoiceRenderer(speak=spoken.append)
    r.on_snapshot(_fleet(_ms("a", report_state=SENTINEL_RUNNING)))  # prime
    r.on_snapshot(_fleet(_ms("a", report_state=SENTINEL_COMPLETE)))
    assert len(spoken) == 1
    assert "finished" in spoken[0].lower()


def test_renderer_speaks_failure_and_gate() -> None:
    spoken: list[str] = []
    r = FleetVoiceRenderer(speak=spoken.append)
    r.on_snapshot(_fleet(_ms("a", report_state=SENTINEL_RUNNING), _ms("b", report_state=SENTINEL_RUNNING)))
    r.on_snapshot(
        _fleet(
            _ms("a", report_state=SENTINEL_FAILED),
            _ms("b", report_state=SENTINEL_HUMAN_GATE, gate="need a decision"),
        )
    )
    joined = " | ".join(spoken).lower()
    assert "failed" in joined
    assert "need a decision" in " | ".join(spoken)
    assert len(spoken) == 2


def test_renderer_is_edge_triggered_no_repeat() -> None:
    spoken: list[str] = []
    r = FleetVoiceRenderer(speak=spoken.append)
    r.on_snapshot(_fleet(_ms("a", report_state=SENTINEL_RUNNING)))  # prime
    r.on_snapshot(_fleet(_ms("a", report_state=SENTINEL_FAILED)))
    r.on_snapshot(_fleet(_ms("a", report_state=SENTINEL_FAILED)))  # still failed
    assert len(spoken) == 1  # spoken once, not on every poll


def test_renderer_announce_initial_speaks_baseline() -> None:
    spoken: list[str] = []
    r = FleetVoiceRenderer(speak=spoken.append, announce_initial=True)
    r.on_snapshot(_fleet(_ms("a", report_state=SENTINEL_COMPLETE)))
    assert len(spoken) == 1


def test_renderer_survives_bad_speak_callback() -> None:
    def boom(_phrase: str) -> None:
        raise RuntimeError("speaker offline")

    r = FleetVoiceRenderer(speak=boom)
    r.on_snapshot(_fleet(_ms("a", report_state=SENTINEL_RUNNING)))
    # Must not raise even though speak() throws.
    anns = r.on_snapshot(_fleet(_ms("a", report_state=SENTINEL_FAILED)))
    assert len(anns) == 1  # it tried to say one thing


def test_renderer_drives_off_real_fleetstate() -> None:
    spoken: list[str] = []
    state = FleetState()
    r = FleetVoiceRenderer(speak=spoken.append)
    unsub = r.attach(state, fire_immediately=True)
    try:
        # prime with a running manager (no speech)
        state.apply([_bg("a1", "alpha", state="working")])
        assert spoken == []
        # now it completes → spoken once
        done = _bg("a1", "alpha", state="done")
        state.apply([done])
        assert len(spoken) == 1
        assert "alpha" in spoken[0]
    finally:
        unsub()


def test_renderer_escalating_gate_via_policy_off_fleetstate() -> None:
    spoken: list[str] = []
    state = FleetState()
    # autonomous policy: only 'push' escalates; a 'format' gate stays silent.
    policy = GatePolicy(mode="autonomous", escalate_on=["push"])
    r = FleetVoiceRenderer(speak=spoken.append, policy=policy)
    r.attach(state, fire_immediately=True)
    state.apply([_bg("a1", "alpha", state="working")])  # prime

    fmt = ManagerStatus(state=SENTINEL_HUMAN_GATE, name="alpha", extra={GATE_TRIGGER_KEY: "format"})
    state.apply([_bg("a1", "alpha", state="working")], statuses={"a1": fmt})
    assert spoken == []  # autonomous gate: silent

    push = ManagerStatus(state=SENTINEL_HUMAN_GATE, name="alpha", extra={GATE_TRIGGER_KEY: "push"})
    state.apply([_bg("a1", "alpha", state="working")], statuses={"a1": push})
    assert len(spoken) == 1  # escalating gate: spoken


# ---------------------------------------------------------------------------
# answer_gate voice tool — the spoken answer resumes the loop (resume seam)
# ---------------------------------------------------------------------------


def _load_answer_gate_cls():
    """Load the AnswerGate voice tool straight from the profile file."""
    here = Path(__file__).resolve()
    src = here.parents[2] / "src"
    tool_file = (
        src
        / "reachy_fleet_supervisor"
        / "profiles"
        / "_reachy_fleet_supervisor_locked_profile"
        / "answer_gate.py"
    )
    spec = importlib.util.spec_from_file_location("rfs_answer_gate_undertest", tool_file)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.AnswerGate


def _capturing_runtime() -> tuple[FleetRuntime, dict]:
    """A runtime whose managers record the steering message send() forwards."""
    captured: dict = {}

    class _FakeProc:
        def __init__(self, stdout: str) -> None:
            self.stdout = stdout

    class _FakeManager:
        def __init__(self, name: str) -> None:
            self.name = name
            self.id = name[:8]

        def send_message(self, message: str, **kwargs: object):
            captured["message"] = message
            captured["name"] = self.name
            return _FakeProc("ok, continuing")

    class _FakeSessionManager:
        def get(self, key: str):
            captured["key"] = key
            return _FakeManager(key)

    state = FleetState()
    rt = FleetRuntime(
        state=state,
        session_manager=_FakeSessionManager(),  # type: ignore[arg-type]
        poller=FleetPoller(state, agents_source=lambda: []),
    )
    return rt, captured


@pytest.fixture(autouse=True)
def _clean_runtime():
    set_fleet_runtime(None)
    yield
    set_fleet_runtime(None)


def test_answer_gate_resumes_manager_with_spoken_answer() -> None:
    rt, captured = _capturing_runtime()
    set_fleet_runtime(rt)
    AnswerGate = _load_answer_gate_cls()
    out = asyncio.run(AnswerGate()(None, name="tester", answer="use port 8080"))
    assert out["status"] == "resumed"
    assert captured["key"] == "tester"
    assert captured["message"] == "use port 8080"
    assert out["reply"] == "ok, continuing"


def test_answer_gate_requires_name_and_answer() -> None:
    AnswerGate = _load_answer_gate_cls()
    assert "error" in asyncio.run(AnswerGate()(None, name="", answer="x"))
    assert "error" in asyncio.run(AnswerGate()(None, name="tester", answer=""))
