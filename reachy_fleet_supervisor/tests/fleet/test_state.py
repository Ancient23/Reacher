"""Tests for FleetState + FleetPoller — single source of truth, pub/sub (U4).

Logic tests (snapshot building, transcript ring-buffer merge, pub/sub
change-notification, and the injectable-source poll loop) run everywhere. The
single integration test spawns a REAL trivial background session, drives one
real poll through a FleetPoller scoped to that session by name, asserts it lands
in FleetState (and that subscribers fire), then ALWAYS cleans it up. It is
skipped when the ``claude`` CLI is not installed.
"""

from __future__ import annotations
import uuid
import shutil
import threading

import pytest

from reachy_fleet_supervisor.fleet import (
    AgentInfo,
    FleetPoller,
    FleetState,
    FleetSnapshot,
    ManagerSnapshot,
)
from reachy_fleet_supervisor.fleet import state as state_mod
from reachy_fleet_supervisor.fleet.state import _merge_tail


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _bg(short_id: str, name: str, **extra: object) -> AgentInfo:
    row = {
        "id": short_id,
        "sessionId": f"{short_id}-1111-2222-3333-444444444444",
        "kind": "background",
        "name": name,
        "cwd": "/proj",
        "state": "working",
        "status": "busy",
    }
    row.update(extra)
    return AgentInfo.from_dict(row)


def _interactive(session_id: str) -> AgentInfo:
    return AgentInfo.from_dict({"sessionId": session_id, "kind": "interactive", "status": "idle"})


# ---------------------------------------------------------------------------
# ManagerSnapshot
# ---------------------------------------------------------------------------


def test_manager_snapshot_from_agent_info() -> None:
    snap = ManagerSnapshot.from_agent_info(_bg("aaaa1111", "alpha"), transcript=["one", "two"])
    assert snap.id == "aaaa1111"
    assert snap.name == "alpha"
    assert snap.is_background
    assert snap.key == "aaaa1111"
    assert snap.transcript == ("one", "two")
    assert snap.last_line == "two"


def test_manager_snapshot_key_falls_back_to_session_id() -> None:
    snap = ManagerSnapshot.from_agent_info(_interactive("eeee5555-aaaa"))
    assert snap.id is None
    assert snap.key == "eeee5555-aaaa"
    assert snap.last_line is None
    assert not snap.is_background


def test_needs_attention_on_state_or_waiting() -> None:
    assert ManagerSnapshot.from_agent_info(_bg("a", "n", state="blocked")).needs_attention
    assert ManagerSnapshot.from_agent_info(_bg("b", "n", state="failed")).needs_attention
    assert ManagerSnapshot.from_agent_info(
        _bg("c", "n", state="working", waitingFor="permission prompt")
    ).needs_attention
    assert not ManagerSnapshot.from_agent_info(_bg("d", "n", state="working")).needs_attention


def test_manager_snapshot_value_equality() -> None:
    """Frozen snapshots compare by value (the basis for change detection)."""
    a = ManagerSnapshot.from_agent_info(_bg("aaaa1111", "alpha"), transcript=["x"])
    b = ManagerSnapshot.from_agent_info(_bg("aaaa1111", "alpha"), transcript=["x"])
    c = ManagerSnapshot.from_agent_info(_bg("aaaa1111", "alpha"), transcript=["y"])
    assert a == b
    assert a != c


def test_apply_assigns_stable_distinct_colors() -> None:
    """apply() fills each manager's identity color, distinct across the fleet (U9)."""
    fs = FleetState()
    snap = fs.apply([_bg("aaaa1111", "alpha"), _bg("bbbb2222", "bravo")], now=1.0)
    colors = [m.color for m in snap.managers]
    assert all(c is not None for c in colors)
    assert colors[0] != colors[1], "concurrent managers must be distinct"

    # Stable across a re-poll of the same fleet.
    snap2 = fs.apply([_bg("aaaa1111", "alpha"), _bg("bbbb2222", "bravo")], now=2.0)
    assert [m.color for m in snap2.managers] == colors


# ---------------------------------------------------------------------------
# FleetSnapshot
# ---------------------------------------------------------------------------


def test_fleet_snapshot_lookup_and_attention() -> None:
    snap = FleetSnapshot(
        managers=(
            ManagerSnapshot.from_agent_info(_bg("aaaa1111", "alpha")),
            ManagerSnapshot.from_agent_info(_bg("bbbb2222", "bravo", state="blocked")),
        ),
        updated_at=123.0,
    )
    assert len(snap) == 2
    assert snap.ids() == ["aaaa1111", "bbbb2222"]
    assert snap.names() == ["alpha", "bravo"]
    assert snap.get("aaaa1111").name == "alpha"           # by id
    assert snap.get("bravo").id == "bbbb2222"             # by name
    assert snap.get("aaaa1111-1111-2222-3333-444444444444").name == "alpha"  # by session id
    assert snap.get("nope") is None
    assert [m.id for m in snap.attention] == ["bbbb2222"]


# ---------------------------------------------------------------------------
# Transcript ring-buffer merge
# ---------------------------------------------------------------------------


def test_merge_tail_appends_new_lines_with_overlap() -> None:
    """Overlapping tails de-dup; only genuinely new lines are appended."""
    first = _merge_tail([], ["a", "b", "c"], maxlen=100)
    assert first == ("a", "b", "c")
    # next poll overlaps on b,c and adds d.
    second = _merge_tail(first, ["b", "c", "d"], maxlen=100)
    assert second == ("a", "b", "c", "d")


def test_merge_tail_no_overlap_appends_all() -> None:
    merged = _merge_tail(["a", "b"], ["c", "d"], maxlen=100)
    assert merged == ("a", "b", "c", "d")


def test_merge_tail_empty_incoming_keeps_existing() -> None:
    assert _merge_tail(["a", "b"], [], maxlen=100) == ("a", "b")


def test_merge_tail_caps_at_maxlen() -> None:
    merged = _merge_tail(["a", "b", "c"], ["d", "e"], maxlen=3)
    assert merged == ("c", "d", "e")


# ---------------------------------------------------------------------------
# FleetState — apply + pub/sub
# ---------------------------------------------------------------------------


def test_apply_builds_snapshot_with_transcripts() -> None:
    fs = FleetState()
    snap = fs.apply([_bg("aaaa1111", "alpha")], logs={"aaaa1111": ["line 1"]}, now=1.0)
    assert snap is fs.snapshot
    assert snap.updated_at == 1.0
    m = snap.get("alpha")
    assert m.transcript == ("line 1",)
    assert m.last_line == "line 1"


def test_apply_accumulates_transcript_across_polls() -> None:
    fs = FleetState()
    fs.apply([_bg("aaaa1111", "alpha")], logs={"aaaa1111": ["a", "b"]})
    fs.apply([_bg("aaaa1111", "alpha")], logs={"aaaa1111": ["b", "c"]})
    assert fs.get("alpha").transcript == ("a", "b", "c")


def test_apply_drops_transcripts_for_gone_managers() -> None:
    fs = FleetState()
    fs.apply([_bg("aaaa1111", "alpha")], logs={"aaaa1111": ["x"]})
    # alpha is gone; only bravo remains.
    fs.apply([_bg("bbbb2222", "bravo")], logs={"bbbb2222": ["y"]})
    assert fs.get("alpha") is None
    assert "aaaa1111" not in fs._transcripts
    assert fs.get("bravo").transcript == ("y",)


def test_subscribe_notifies_on_change_only() -> None:
    fs = FleetState()
    seen: list[FleetSnapshot] = []
    fs.subscribe(seen.append)

    fs.apply([_bg("aaaa1111", "alpha")], now=1.0)            # change → notify
    fs.apply([_bg("aaaa1111", "alpha")], now=2.0)            # identical managers → no notify
    fs.apply([_bg("aaaa1111", "alpha", state="blocked")], now=3.0)  # change → notify

    assert len(seen) == 2
    assert seen[0].get("alpha").state == "working"
    assert seen[1].get("alpha").state == "blocked"


def test_subscribe_fire_immediately() -> None:
    fs = FleetState()
    fs.apply([_bg("aaaa1111", "alpha")])
    seen: list[FleetSnapshot] = []
    fs.subscribe(seen.append, fire_immediately=True)
    assert len(seen) == 1
    assert seen[0].get("alpha") is not None


def test_unsubscribe_stops_updates() -> None:
    fs = FleetState()
    seen: list[FleetSnapshot] = []
    unsub = fs.subscribe(seen.append)
    fs.apply([_bg("aaaa1111", "alpha")], now=1.0)
    unsub()
    fs.apply([_bg("bbbb2222", "bravo")], now=2.0)
    assert len(seen) == 1


def test_one_bad_subscriber_does_not_break_others() -> None:
    fs = FleetState()
    good: list[FleetSnapshot] = []

    def boom(_snap: FleetSnapshot) -> None:
        raise RuntimeError("subscriber error")

    fs.subscribe(boom)
    fs.subscribe(good.append)
    fs.apply([_bg("aaaa1111", "alpha")])  # must not raise
    assert len(good) == 1


def test_transcript_respects_log_lines_cap() -> None:
    fs = FleetState(log_lines=2)
    fs.apply([_bg("aaaa1111", "alpha")], logs={"aaaa1111": ["a", "b", "c"]})
    assert fs.get("alpha").transcript == ("b", "c")


# ---------------------------------------------------------------------------
# FleetPoller — injectable sources (no subprocess)
# ---------------------------------------------------------------------------


def test_poll_once_feeds_state_from_sources() -> None:
    fs = FleetState()
    poller = FleetPoller(
        fs,
        fetch_logs=True,
        agents_source=lambda: [_bg("aaaa1111", "alpha")],
        logs_source=lambda key: [f"log for {key}"],
    )
    snap = poller.poll_once()
    assert snap.get("alpha").last_line == "log for aaaa1111"


def test_poll_once_predicate_scopes_rows() -> None:
    fs = FleetState()
    poller = FleetPoller(
        fs,
        agents_source=lambda: [
            _bg("aaaa1111", "mine", cwd="/proj/a"),
            _bg("bbbb2222", "theirs", cwd="/other"),
        ],
        predicate=lambda a: a.cwd == "/proj/a",
    )
    snap = poller.poll_once()
    assert snap.ids() == ["aaaa1111"]


def test_poll_once_skips_logs_when_disabled() -> None:
    fs = FleetState()
    calls = {"logs": 0}

    def logs_source(_key: str) -> list[str]:
        calls["logs"] += 1
        return ["x"]

    poller = FleetPoller(
        fs,
        fetch_logs=False,
        agents_source=lambda: [_bg("aaaa1111", "alpha")],
        logs_source=logs_source,
    )
    snap = poller.poll_once()
    assert calls["logs"] == 0
    assert snap.get("alpha").transcript == ()


def test_poller_start_stop_runs_loop() -> None:
    """The background thread polls at least once, then stops cleanly."""
    fs = FleetState()
    fired = threading.Event()

    def agents_source() -> list[AgentInfo]:
        fired.set()
        return [_bg("aaaa1111", "alpha")]

    poller = FleetPoller(fs, interval=0.01, agents_source=agents_source)
    with poller:
        assert fired.wait(timeout=2.0), "poller thread never polled"
        # Give the snapshot a beat to land.
        assert _wait_until(lambda: fs.get("alpha") is not None, 2.0)
    assert not poller.running
    assert fs.get("alpha").name == "alpha"


def test_poller_loop_survives_source_errors() -> None:
    """A throwing source must not kill the loop; later polls still land."""
    fs = FleetState()
    state = {"n": 0}

    def flaky_source() -> list[AgentInfo]:
        state["n"] += 1
        if state["n"] == 1:
            raise RuntimeError("transient CLI failure")
        return [_bg("aaaa1111", "alpha")]

    poller = FleetPoller(fs, interval=0.01, agents_source=flaky_source)
    with poller:
        assert _wait_until(lambda: fs.get("alpha") is not None, 2.0), "loop did not recover"


def _wait_until(predicate, timeout: float) -> bool:
    import time

    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


# ---------------------------------------------------------------------------
# tail_logs — best-effort (mocked subprocess)
# ---------------------------------------------------------------------------


def test_tail_logs_tails_output(monkeypatch) -> None:
    class _Proc:
        stdout = "l1\nl2\nl3\nl4\n"
        stderr = ""
        returncode = 0

    monkeypatch.setattr(state_mod, "_run_claude", lambda *a, **k: _Proc())
    assert state_mod.tail_logs("aaaa1111", lines=2) == ["l3", "l4"]


def test_tail_logs_returns_empty_on_failure(monkeypatch) -> None:
    def boom(*a, **k):
        raise state_mod.FleetManagerError("no such agent")

    monkeypatch.setattr(state_mod, "_run_claude", boom)
    assert state_mod.tail_logs("ghost") == []


# ---------------------------------------------------------------------------
# Integration — REAL bg session driven through a real poll, always cleaned up
# ---------------------------------------------------------------------------

_HAS_CLAUDE = shutil.which("claude") is not None


@pytest.mark.skipif(not _HAS_CLAUDE, reason="claude CLI not installed")
def test_real_poll_populates_fleet_state(tmp_path) -> None:
    """Spawn a REAL bg manager; one real poll lands it in FleetState + fires subs."""
    from reachy_fleet_supervisor.fleet import SessionManager

    name = f"u4-test-{uuid.uuid4().hex[:8]}"
    sm = SessionManager()
    mgr = None
    try:
        mgr = sm.spawn(
            "Reply with the single word DONE and do nothing else.",
            name=name,
            cwd=tmp_path,
        )

        fs = FleetState()
        seen: list[FleetSnapshot] = []
        fs.subscribe(seen.append)

        # Real poll, scoped to our unique session so we don't pick up other
        # background agents on the machine. fetch_logs on to exercise the real
        # `claude logs` path (best-effort; may be empty this early).
        poller = FleetPoller(
            fs,
            include_all=True,
            fetch_logs=True,
            predicate=lambda a: a.session_id == mgr.session_id,
        )
        snap = poller.poll_once()

        assert snap.get(mgr.id) is not None, "spawned manager not in FleetState after poll"
        assert snap.get(mgr.id).session_id == mgr.session_id
        assert snap.get(mgr.id).is_background
        assert len(seen) == 1  # the change fired the subscriber
    finally:
        if mgr is not None:
            mgr.stop_and_remove()
            from reachy_fleet_supervisor.fleet import list_agents

            remaining = [
                a for a in list_agents(include_all=False) if a.session_id == mgr.session_id
            ]
            assert remaining == [], f"manager {mgr.session_id} still active after teardown"
