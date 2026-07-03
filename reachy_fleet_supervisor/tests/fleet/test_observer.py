"""Tests for the proactive supervisor observer (U21, decision #9).

The other half of U20's vision tool: instead of a *worker* asking to look,
the *supervisor* (Reachy) proactively glances at a manager's work the moment
it completes and comments on it aloud. Real screen capture, real ``claude -p``
assessment and real speech can only be confirmed on/around the physical robot
(see ``docs/roadmap/STATE.yaml`` ``status.deferred_gates``); what is fully
software-testable — and pinned down here — is everything up to that:

- ``should_glance`` / ``build_glance_question`` — the pure decision + prompt.
- ``build_glance_comment`` — trims a raw assessment to a short spoken line.
- ``FleetObserver`` — edge-triggered exactly like ``FleetVoiceRenderer``: primes
  silently, glances once per fresh completion, never repeats, survives a bad
  vision/speak callback (incl. the hardware-gated ``RobotCameraUnavailable``
  seam — a missing camera must never fake a comment), and drives off a real
  ``FleetState``.
"""

from __future__ import annotations

from reachy_fleet_supervisor.fleet import (
    AgentInfo,
    FleetState,
    FleetSnapshot,
    ManagerSnapshot,
    ManagerStatus,
    GatePolicy,
    SENTINEL_FAILED,
    SENTINEL_COMPLETE,
    SENTINEL_HUMAN_GATE,
    SENTINEL_RUNNING,
    LookResult,
    VisionError,
    RobotCameraUnavailable,
    SOURCE_SCREEN,
    SOURCE_CAMERA,
    DEFAULT_GLANCE_KINDS,
    FleetObserver,
    should_glance,
    build_glance_question,
    build_glance_comment,
)
from reachy_fleet_supervisor.fleet.voice import VOICE_COMPLETED, VOICE_FAILED, VOICE_GATE


# ---------------------------------------------------------------------------
# fixtures (mirrors tests/fleet/test_voice.py)
# ---------------------------------------------------------------------------


def _bg(short_id: str, name: str, state: str = "working", **kw) -> AgentInfo:
    return AgentInfo.from_dict(
        {
            "id": short_id,
            "sessionId": f"{short_id}-0000-0000-0000-000000000000",
            "kind": "background",
            "name": name,
            "cwd": "/proj",
            "state": state,
            "status": state,
            **kw,
        }
    )


def _ms(name: str, *, state: str = "working", report_state: str | None = None) -> ManagerSnapshot:
    report = None
    if report_state is not None:
        report = ManagerStatus(state=report_state, name=name)
    return ManagerSnapshot.from_agent_info(_bg(name[:8], name, state), report=report)


def _fleet(*managers: ManagerSnapshot) -> FleetSnapshot:
    return FleetSnapshot(managers=tuple(managers), updated_at=1.0)


# ---------------------------------------------------------------------------
# should_glance / build_glance_question — pure
# ---------------------------------------------------------------------------


def test_should_glance_true_on_completion() -> None:
    assert should_glance(_ms("a", report_state=SENTINEL_COMPLETE)) is True


def test_should_glance_false_on_running() -> None:
    assert should_glance(_ms("a", report_state=SENTINEL_RUNNING)) is False


def test_should_glance_false_on_failed_and_gate_by_default() -> None:
    # Failure/gate are already spoken by FleetVoiceRenderer without needing a
    # visual assessment; the default glance policy is completion-only.
    assert should_glance(_ms("a", report_state=SENTINEL_FAILED)) is False
    assert should_glance(_ms("a", report_state=SENTINEL_HUMAN_GATE)) is False


def test_should_glance_respects_custom_glance_kinds() -> None:
    assert should_glance(
        _ms("a", report_state=SENTINEL_FAILED), glance_kinds=frozenset({VOICE_FAILED})
    ) is True


def test_default_glance_kinds_is_completion_only() -> None:
    assert DEFAULT_GLANCE_KINDS == frozenset({VOICE_COMPLETED})


def test_build_glance_question_mentions_name_and_cwd() -> None:
    q = build_glance_question(_ms("builder"))
    assert "builder" in q
    assert "/proj" in q


# ---------------------------------------------------------------------------
# build_glance_comment — pure, trims to first line
# ---------------------------------------------------------------------------


def test_build_glance_comment_uses_first_line_only() -> None:
    m = _ms("builder")
    comment = build_glance_comment(m, "Looks solid, buttons align.\nMore detail here.")
    assert "builder" in comment
    assert "Looks solid, buttons align." in comment
    assert "More detail here." not in comment


def test_build_glance_comment_handles_empty_assessment() -> None:
    comment = build_glance_comment(_ms("builder"), "")
    assert "builder" in comment


# ---------------------------------------------------------------------------
# FleetObserver — edge-triggered subscriber (fake look_fn / speak)
# ---------------------------------------------------------------------------


def _fake_look(*, assessment: str = "it works.", calls: list | None = None):
    def _look(question: str, *, source: str = SOURCE_SCREEN):
        if calls is not None:
            calls.append((question, source))
        return LookResult(source=source, image_path=None, assessment=assessment)

    return _look


def test_observer_primes_silently_on_first_snapshot() -> None:
    spoken: list[str] = []
    o = FleetObserver(speak=spoken.append, look_fn=_fake_look())
    o.on_snapshot(_fleet(_ms("a", report_state=SENTINEL_COMPLETE)))
    assert spoken == []


def test_observer_glances_and_speaks_on_completion() -> None:
    spoken: list[str] = []
    calls: list = []
    o = FleetObserver(speak=spoken.append, look_fn=_fake_look(assessment="looks great.", calls=calls))
    o.on_snapshot(_fleet(_ms("a", report_state=SENTINEL_RUNNING)))  # prime
    reports = o.on_snapshot(_fleet(_ms("a", report_state=SENTINEL_COMPLETE)))
    assert len(reports) == 1
    assert len(spoken) == 1
    assert "a" in spoken[0]
    assert "looks great." in spoken[0]
    assert len(calls) == 1
    assert calls[0][1] == SOURCE_SCREEN  # default source


def test_observer_ignores_failure_and_gate_by_default() -> None:
    spoken: list[str] = []
    o = FleetObserver(speak=spoken.append, look_fn=_fake_look())
    o.on_snapshot(_fleet(_ms("a", report_state=SENTINEL_RUNNING)))  # prime
    o.on_snapshot(_fleet(_ms("a", report_state=SENTINEL_FAILED)))
    o.on_snapshot(_fleet(_ms("a", report_state=SENTINEL_HUMAN_GATE)))
    assert spoken == []


def test_observer_is_edge_triggered_no_repeat() -> None:
    spoken: list[str] = []
    o = FleetObserver(speak=spoken.append, look_fn=_fake_look())
    o.on_snapshot(_fleet(_ms("a", report_state=SENTINEL_RUNNING)))  # prime
    o.on_snapshot(_fleet(_ms("a", report_state=SENTINEL_COMPLETE)))
    o.on_snapshot(_fleet(_ms("a", report_state=SENTINEL_COMPLETE)))  # still complete
    assert len(spoken) == 1


def test_observer_survives_vision_error() -> None:
    def boom(question: str, *, source: str = SOURCE_SCREEN):
        raise VisionError("screen capture failed")

    spoken: list[str] = []
    o = FleetObserver(speak=spoken.append, look_fn=boom)
    o.on_snapshot(_fleet(_ms("a", report_state=SENTINEL_RUNNING)))  # prime
    reports = o.on_snapshot(_fleet(_ms("a", report_state=SENTINEL_COMPLETE)))
    assert reports == []
    assert spoken == []  # a bad glance must never fake a comment


def test_observer_survives_robot_camera_unavailable() -> None:
    # The hardware-gated seam: without a wired camera, a "nothing to glance at
    # this time" — never a faked comment. Exercised with source="camera".
    def boom(question: str, *, source: str = SOURCE_CAMERA):
        raise RobotCameraUnavailable("no frame provider wired")

    spoken: list[str] = []
    o = FleetObserver(speak=spoken.append, look_fn=boom, source=SOURCE_CAMERA)
    o.on_snapshot(_fleet(_ms("a", report_state=SENTINEL_RUNNING)))  # prime
    reports = o.on_snapshot(_fleet(_ms("a", report_state=SENTINEL_COMPLETE)))
    assert reports == []
    assert spoken == []


def test_observer_survives_bad_speak_callback() -> None:
    def boom_speak(_comment: str) -> None:
        raise RuntimeError("speaker offline")

    o = FleetObserver(speak=boom_speak, look_fn=_fake_look())
    o.on_snapshot(_fleet(_ms("a", report_state=SENTINEL_RUNNING)))  # prime
    # Must not raise even though speak() throws; it still tried once.
    reports = o.on_snapshot(_fleet(_ms("a", report_state=SENTINEL_COMPLETE)))
    assert len(reports) == 1


def test_observer_announce_initial_glances_baseline() -> None:
    spoken: list[str] = []
    o = FleetObserver(speak=spoken.append, look_fn=_fake_look(), announce_initial=True)
    o.on_snapshot(_fleet(_ms("a", report_state=SENTINEL_COMPLETE)))
    assert len(spoken) == 1


def test_observer_respects_gate_policy_for_default_kinds() -> None:
    # Sanity: passing a policy still flows into speakable_kind (shared with
    # the voice renderer) even though the default glance_kinds don't touch gates.
    policy = GatePolicy(mode="gated")
    spoken: list[str] = []
    o = FleetObserver(speak=spoken.append, look_fn=_fake_look(), policy=policy)
    o.on_snapshot(_fleet(_ms("a", report_state=SENTINEL_RUNNING)))
    o.on_snapshot(_fleet(_ms("a", report_state=SENTINEL_COMPLETE)))
    assert len(spoken) == 1


def test_observer_drives_off_real_fleetstate() -> None:
    spoken: list[str] = []
    calls: list = []
    state = FleetState()
    o = FleetObserver(speak=spoken.append, look_fn=_fake_look(calls=calls))
    unsub = o.attach(state, fire_immediately=True)
    try:
        state.apply([_bg("a1", "alpha", state="working")])  # prime
        assert spoken == []
        state.apply([_bg("a1", "alpha", state="done")])
        assert len(spoken) == 1
        assert "alpha" in spoken[0]
        assert len(calls) == 1
    finally:
        unsub()


# ---------------------------------------------------------------------------
# diagnostic logging (U21 problem-fix 2026-07-03) — a live test reported no
# spoken comment with no way to tell whether a glance was even attempted; the
# observer now logs an attempt/speak line so the next live test is
# diagnosable via the app's normal (INFO-level) logs.
# ---------------------------------------------------------------------------


def test_observer_logs_glance_attempt(caplog) -> None:
    import logging

    o = FleetObserver(speak=lambda _c: None, look_fn=_fake_look())
    with caplog.at_level(logging.INFO, logger="reachy_fleet_supervisor.fleet.observer"):
        o.on_snapshot(_fleet(_ms("a", report_state=SENTINEL_RUNNING)))  # prime
        o.on_snapshot(_fleet(_ms("a", report_state=SENTINEL_COMPLETE)))
    assert any("glance attempted" in r.message for r in caplog.records)
    assert any("speaking glance" in r.message for r in caplog.records)


def test_observer_logs_glance_attempt_even_when_vision_fails(caplog) -> None:
    import logging

    def boom(question: str, *, source: str = SOURCE_SCREEN):
        raise VisionError("screen capture failed")

    o = FleetObserver(speak=lambda _c: None, look_fn=boom)
    with caplog.at_level(logging.INFO, logger="reachy_fleet_supervisor.fleet.observer"):
        o.on_snapshot(_fleet(_ms("a", report_state=SENTINEL_RUNNING)))  # prime
        o.on_snapshot(_fleet(_ms("a", report_state=SENTINEL_COMPLETE)))
    assert any("glance attempted" in r.message for r in caplog.records)
    assert any("glance unavailable" in r.message for r in caplog.records)
    # Never reaches the speak step since the look failed.
    assert not any("speaking glance" in r.message for r in caplog.records)


def test_observer_multiple_managers_independent() -> None:
    spoken: list[str] = []
    o = FleetObserver(speak=spoken.append, look_fn=_fake_look())
    o.on_snapshot(
        _fleet(
            _ms("a", report_state=SENTINEL_RUNNING),
            _ms("b", report_state=SENTINEL_RUNNING),
        )
    )  # prime
    o.on_snapshot(
        _fleet(
            _ms("a", report_state=SENTINEL_COMPLETE),
            _ms("b", report_state=SENTINEL_RUNNING),
        )
    )
    assert len(spoken) == 1
    assert "a" in spoken[0]
