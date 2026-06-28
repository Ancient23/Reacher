"""Tests for the body renderer — body renders FleetState (U8).

U8 is a *hardware* gate: real motion is confirmed by a human on the robot. What
is software-testable is everything up to the motors — the pure status→pose
mapping and the FleetState→applier wiring — which is what these tests pin down:

- ``slot_yaw`` spreads 1–4 managers symmetrically and stably.
- ``antenna_pose`` gives a distinct pose per signal.
- ``signal_state`` / ``choose_target`` pick the right manager (failed > waiting).
- ``body_signal_for`` faces the manager needing attention, front otherwise.
- ``FleetBodyRenderer`` applies on change, skips no-ops, survives a bad applier,
  and drives off a real :class:`FleetState` subscription.
- ``make_goto_applier`` queues exactly one move with the pose converted to radians
  (against fakes — no real motors).
"""

from __future__ import annotations

import math

from reachy_fleet_supervisor.fleet import (
    BODY_YAW_SPAN_DEG,
    SIGNAL_IDLE,
    SIGNAL_WORKING,
    SIGNAL_ATTENTION,
    SIGNAL_DONE,
    SIGNAL_FAILED,
    AgentInfo,
    BodyPose,
    FleetState,
    FleetSnapshot,
    ManagerSnapshot,
    ManagerStatus,
    FleetBodyRenderer,
    antenna_pose,
    body_signal_for,
    choose_target,
    make_goto_applier,
    signal_state,
    slot_yaw,
)


# ---------------------------------------------------------------------------
# fixtures
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


def _snap(*managers: ManagerSnapshot) -> FleetSnapshot:
    return FleetSnapshot(managers=tuple(managers), updated_at=1.0)


def _m(short_id: str, name: str, *, report: ManagerStatus | None = None, **extra: object) -> ManagerSnapshot:
    return ManagerSnapshot.from_agent_info(_bg(short_id, name, **extra), report=report)


# ---------------------------------------------------------------------------
# slot_yaw — where each manager sits
# ---------------------------------------------------------------------------


def test_slot_yaw_single_manager_faces_front() -> None:
    assert slot_yaw(0, 1) == 0.0


def test_slot_yaw_is_symmetric_and_spans_full_range() -> None:
    # Two managers sit at the extremes, symmetric about front.
    assert slot_yaw(0, 2) == -BODY_YAW_SPAN_DEG
    assert slot_yaw(1, 2) == BODY_YAW_SPAN_DEG
    # Three: left / center / right.
    assert slot_yaw(0, 3) == -BODY_YAW_SPAN_DEG
    assert slot_yaw(1, 3) == 0.0
    assert slot_yaw(2, 3) == BODY_YAW_SPAN_DEG
    # Four: evenly spaced, symmetric (middle pair mirror each other).
    yaws = [slot_yaw(i, 4) for i in range(4)]
    assert yaws[0] == -BODY_YAW_SPAN_DEG and yaws[3] == BODY_YAW_SPAN_DEG
    assert math.isclose(yaws[1], -yaws[2])


def test_slot_yaw_respects_custom_span() -> None:
    assert slot_yaw(0, 2, span=10.0) == -10.0
    assert slot_yaw(1, 2, span=10.0) == 10.0


# ---------------------------------------------------------------------------
# antenna_pose — what state the antennas show
# ---------------------------------------------------------------------------


def test_antenna_pose_distinct_per_signal() -> None:
    poses = {
        s: antenna_pose(s)
        for s in (SIGNAL_IDLE, SIGNAL_WORKING, SIGNAL_ATTENTION, SIGNAL_DONE, SIGNAL_FAILED)
    }
    assert len(set(poses.values())) == len(poses), "each signal needs a distinct antenna pose"
    assert poses[SIGNAL_IDLE] == (0.0, 0.0)
    assert poses[SIGNAL_ATTENTION][0] > poses[SIGNAL_WORKING][0], "attention perks higher than working"


def test_failed_is_the_only_downward_droop() -> None:
    # The human-perceptual fix: ``failed`` must be the one state whose antennas go
    # clearly DOWN (below neutral) while every other state sits at/above neutral —
    # so a failure is the single unmistakable droop.
    failed = antenna_pose(SIGNAL_FAILED)
    assert failed[0] < 0 and failed[1] < 0, "failed antennas droop downward"
    assert failed[0] == failed[1], "both antennas droop together (same sign/magnitude)"
    for sig in (SIGNAL_IDLE, SIGNAL_WORKING, SIGNAL_ATTENTION, SIGNAL_DONE):
        left, right = antenna_pose(sig)
        assert left >= 0 and right >= 0, f"{sig} should not droop below neutral"
    # And the droop is emphatic, not a token dip — at least as deep as 'done' is high.
    assert failed[0] <= -antenna_pose(SIGNAL_DONE)[0], "failed droop should be clearly visible"


def test_antenna_pose_unknown_signal_falls_back_to_idle() -> None:
    assert antenna_pose("???") == (0.0, 0.0)


# ---------------------------------------------------------------------------
# signal_state — per-manager state classification
# ---------------------------------------------------------------------------


def test_signal_state_from_roster_row() -> None:
    assert signal_state(_m("a1", "alpha", state="working")) == SIGNAL_WORKING
    assert signal_state(_m("a2", "beta", state="failed")) == SIGNAL_FAILED
    assert signal_state(_m("a3", "gamma", state="blocked")) == SIGNAL_ATTENTION
    assert signal_state(_m("a4", "delta", state="working", waitingFor="permission")) == SIGNAL_ATTENTION
    assert signal_state(_m("a5", "idle", state="idle")) == SIGNAL_IDLE


def test_signal_state_prefers_emitted_report() -> None:
    # An emitted HUMAN_GATE outranks a stale "working" roster row.
    m = _m("b1", "alpha", state="working", report=ManagerStatus(state="HUMAN_GATE"))
    assert signal_state(m) == SIGNAL_ATTENTION
    # COMPLETE report → done.
    m2 = _m("b2", "beta", state="working", report=ManagerStatus(state="COMPLETE"))
    assert signal_state(m2) == SIGNAL_DONE
    # FAILED report → failed (even if roster says working).
    m3 = _m("b3", "gamma", state="working", report=ManagerStatus(state="FAILED"))
    assert signal_state(m3) == SIGNAL_FAILED


# ---------------------------------------------------------------------------
# choose_target — who the body faces
# ---------------------------------------------------------------------------


def test_choose_target_none_when_all_working() -> None:
    snap = _snap(_m("a1", "alpha", state="working"), _m("b2", "beta", state="working"))
    assert choose_target(snap) is None


def test_choose_target_picks_the_waiting_manager() -> None:
    snap = _snap(_m("a1", "alpha", state="working"), _m("b2", "beta", state="blocked"))
    target = choose_target(snap)
    assert target is not None and target.id == "b2"


def test_choose_target_failed_outranks_waiting() -> None:
    snap = _snap(
        _m("a1", "alpha", state="blocked"),
        _m("b2", "beta", state="failed"),
    )
    target = choose_target(snap)
    assert target is not None and target.id == "b2", "failed should win over merely waiting"


# ---------------------------------------------------------------------------
# body_signal_for — the full mapping
# ---------------------------------------------------------------------------


def test_body_signal_faces_front_when_idle() -> None:
    snap = _snap(_m("a1", "alpha", state="working"), _m("b2", "beta", state="working"))
    pose = body_signal_for(snap)
    assert pose.body_yaw_deg == 0.0
    assert pose.target_key is None
    assert pose.signal == SIGNAL_WORKING  # aggregate: someone's working
    assert pose.antenna_left_deg == antenna_pose(SIGNAL_WORKING)[0]


def test_body_signal_faces_the_attention_manager_slot() -> None:
    # Keys sort as a1 < b2 < c3 → slots [-span, 0, +span]. b2 is waiting.
    snap = _snap(
        _m("a1", "alpha", state="working"),
        _m("b2", "beta", state="blocked"),
        _m("c3", "gamma", state="working"),
    )
    pose = body_signal_for(snap)
    assert pose.target_key == "b2"
    assert pose.body_yaw_deg == 0.0  # b2 is the middle slot of three
    assert pose.signal == SIGNAL_ATTENTION
    assert pose.antenna_left_deg == antenna_pose(SIGNAL_ATTENTION)[0]


def test_body_signal_yaw_matches_target_position() -> None:
    # First key (a1) waiting → leftmost slot; last key (c3) waiting → rightmost.
    left = body_signal_for(
        _snap(_m("a1", "alpha", state="blocked"), _m("b2", "b", state="working"), _m("c3", "c", state="working"))
    )
    assert left.target_key == "a1" and left.body_yaw_deg == -BODY_YAW_SPAN_DEG
    right = body_signal_for(
        _snap(_m("a1", "a", state="working"), _m("b2", "b", state="working"), _m("c3", "gamma", state="blocked"))
    )
    assert right.target_key == "c3" and right.body_yaw_deg == BODY_YAW_SPAN_DEG


def test_body_signal_empty_fleet_is_idle_front() -> None:
    pose = body_signal_for(_snap())
    assert pose == BodyPose(
        body_yaw_deg=0.0,
        antenna_left_deg=0.0,
        antenna_right_deg=0.0,
        target_key=None,
        signal=SIGNAL_IDLE,
        reason=pose.reason,
    )
    assert pose.signal == SIGNAL_IDLE


def test_body_pose_describe_is_one_line() -> None:
    pose = body_signal_for(_snap(_m("a1", "alpha", state="blocked")))
    text = pose.describe()
    assert "\n" not in text
    assert "a1" in text and "attention" in text


# ---------------------------------------------------------------------------
# FleetBodyRenderer — change detection + applier fan-out
# ---------------------------------------------------------------------------


class _RecordingApplier:
    def __init__(self) -> None:
        self.poses: list[BodyPose] = []

    def __call__(self, pose: BodyPose) -> None:
        self.poses.append(pose)


def test_renderer_applies_on_change_and_skips_noop() -> None:
    applier = _RecordingApplier()
    renderer = FleetBodyRenderer(applier)

    snap1 = _snap(_m("a1", "alpha", state="working"))
    applied = renderer.on_snapshot(snap1)
    assert applied is not None
    assert len(applier.poses) == 1

    # Same picture → no new move.
    assert renderer.on_snapshot(snap1) is None
    assert len(applier.poses) == 1

    # Now a1 needs attention → pose changes → applied.
    snap2 = _snap(_m("a1", "alpha", state="blocked"))
    applied2 = renderer.on_snapshot(snap2)
    assert applied2 is not None and applied2.signal == SIGNAL_ATTENTION
    assert len(applier.poses) == 2
    assert renderer.last_pose == applied2


def test_renderer_survives_a_bad_applier() -> None:
    def boom(_pose: BodyPose) -> None:
        raise RuntimeError("motor offline")

    renderer = FleetBodyRenderer(boom)
    # Must not raise; returns None and does not record the pose as applied.
    assert renderer.on_snapshot(_snap(_m("a1", "alpha", state="blocked"))) is None
    assert renderer.last_pose is None


def test_renderer_drives_off_fleetstate_subscription() -> None:
    applier = _RecordingApplier()
    renderer = FleetBodyRenderer(applier)
    state = FleetState()

    unsub = renderer.attach(state, fire_immediately=True)
    try:
        # fire_immediately paints the empty/idle state once.
        assert len(applier.poses) == 1
        assert applier.poses[-1].target_key is None

        state.apply([_bg("a1", "alpha", state="blocked")], now=2.0)
        assert applier.poses[-1].target_key == "a1"
        assert applier.poses[-1].signal == SIGNAL_ATTENTION
    finally:
        unsub()

    # After unsubscribe, further updates don't move the body.
    before = len(applier.poses)
    state.apply([_bg("a1", "alpha", state="working")], now=3.0)
    assert len(applier.poses) == before


# ---------------------------------------------------------------------------
# make_goto_applier — the motor seam (against fakes, no real robot)
# ---------------------------------------------------------------------------


class _FakeMovementManager:
    def __init__(self) -> None:
        self.moves: list[object] = []
        self.moving_durations: list[float] = []

    def queue_move(self, move: object) -> None:
        self.moves.append(move)

    def set_moving_state(self, duration: float) -> None:
        self.moving_durations.append(duration)


class _FakeReachy:
    """Minimal stand-in exposing the two reads the applier attempts."""

    def get_current_head_pose(self):  # noqa: ANN201 - test fake
        import numpy as np

        return np.eye(4, dtype=np.float64)

    def get_current_joint_positions(self):  # noqa: ANN201 - test fake
        import numpy as np

        # (head joints, [body_yaw, antenna_l, antenna_r])
        return np.zeros(0), np.array([0.0, 0.0, 0.0])


def test_make_goto_applier_queues_one_move_in_radians() -> None:
    mm = _FakeMovementManager()
    apply = make_goto_applier(_FakeReachy(), mm, duration=0.5)

    pose = BodyPose(body_yaw_deg=BODY_YAW_SPAN_DEG, antenna_left_deg=45.0, antenna_right_deg=45.0, signal=SIGNAL_ATTENTION)
    move = apply(pose)

    assert len(mm.moves) == 1 and mm.moves[0] is move
    assert mm.moving_durations == [0.5]
    # Degrees were converted to radians for the kinematics layer.
    assert math.isclose(move.target_body_yaw, math.radians(BODY_YAW_SPAN_DEG))
    assert math.isclose(move.target_antennas[0], math.radians(45.0))
    assert math.isclose(move.target_antennas[1], math.radians(45.0))
    assert move.duration == 0.5


def test_make_goto_applier_interpolates_from_last_commanded_pose() -> None:
    # Smoothness / anti-abruptness: a second move must start from where the first
    # one ended (body_yaw can't be read back from the robot, so the applier tracks
    # it) — otherwise a +40°→-40° turn would jump through 0° between managers.
    mm = _FakeMovementManager()
    apply = make_goto_applier(_FakeReachy(), mm, duration=1.0)

    m1 = apply(BodyPose(body_yaw_deg=40.0, antenna_left_deg=45.0, antenna_right_deg=45.0))
    # First move starts from neutral (nothing commanded yet).
    assert math.isclose(m1.start_body_yaw, 0.0)
    assert m1.start_antennas == (0.0, 0.0)

    m2 = apply(BodyPose(body_yaw_deg=-40.0, antenna_left_deg=-45.0, antenna_right_deg=-45.0))
    # Second move starts where the first ended — no snap back to 0.
    assert math.isclose(m2.start_body_yaw, math.radians(40.0))
    assert math.isclose(m2.start_antennas[0], math.radians(45.0))
    assert math.isclose(m2.start_antennas[1], math.radians(45.0))
    assert math.isclose(m2.target_body_yaw, math.radians(-40.0))
    assert math.isclose(m2.target_antennas[0], math.radians(-45.0))


def test_make_goto_applier_tolerates_unreadable_current_pose() -> None:
    class _Broken:
        def get_current_head_pose(self):  # noqa: ANN201 - test fake
            raise RuntimeError("no motors")

        def get_current_joint_positions(self):  # noqa: ANN201 - test fake
            raise RuntimeError("no motors")

    mm = _FakeMovementManager()
    apply = make_goto_applier(_Broken(), mm, duration=1.0)
    move = apply(BodyPose(body_yaw_deg=0.0))
    # Still queued a move despite the failed current-state reads.
    assert len(mm.moves) == 1 and mm.moves[0] is move


# ---------------------------------------------------------------------------
# body_demo — the scripted hardware-gate scenario (pure parts)
# ---------------------------------------------------------------------------


def test_demo_scenario_signals_the_expected_sequence() -> None:
    from reachy_fleet_supervisor.fleet.body_demo import demo_steps

    applier = _RecordingApplier()
    renderer = FleetBodyRenderer(applier)
    state = FleetState()
    renderer.attach(state, fire_immediately=False)

    for i, step in enumerate(demo_steps(), start=1):
        state.apply(step.agents, statuses=step.statuses, now=float(i))

    signals = [p.signal for p in applier.poses]
    assert signals == [SIGNAL_WORKING, SIGNAL_ATTENTION, SIGNAL_FAILED, SIGNAL_DONE]

    # The attention beat faces the waiting 'tests' manager (right slot, +span);
    # the failure beat faces 'build' (left slot, -span). Distinct directions.
    attention_pose = applier.poses[1]
    failed_pose = applier.poses[2]
    assert attention_pose.target_key == "tests" and attention_pose.body_yaw_deg == BODY_YAW_SPAN_DEG
    assert failed_pose.target_key == "build" and failed_pose.body_yaw_deg == -BODY_YAW_SPAN_DEG

    # The failure beat must droop the antennas DOWN, and be the only step that does
    # — this is the behaviour the human reported missing.
    assert failed_pose.antenna_left_deg < 0 and failed_pose.antenna_right_deg < 0
    for i, pose in enumerate(applier.poses):
        if i != 2:
            assert pose.antenna_left_deg >= 0, f"only the failed step should droop (step {i})"


# ---------------------------------------------------------------------------
# hold_pose — keep the signalled pose on the robot (no idle-breathing revert)
# ---------------------------------------------------------------------------


class _ClockMM:
    """Fake movement manager + injectable clock to test the hold loop without real time."""

    def __init__(self) -> None:
        self.t = 0.0
        self.marks: list[float] = []

    def set_moving_state(self, duration: float) -> None:
        self.marks.append(duration)

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds


def test_hold_pose_marks_activity_below_idle_delay() -> None:
    from reachy_fleet_supervisor.fleet.body_demo import hold_pose

    mm = _ClockMM()
    hold_pose(mm, 1.0, tick=0.2, sleep=mm.sleep, monotonic=mm.now)

    # It held for ~the requested duration...
    assert math.isclose(mm.t, 1.0, abs_tol=0.21)
    # ...re-asserting activity several times (so idle breathing never starts),
    # each tick safely below the MovementManager's 0.3s idle-breathing delay.
    assert len(mm.marks) >= 4
    assert all(d < 0.3 for d in mm.marks), "ticks must stay under the idle-breathing delay"


def test_hold_pose_zero_seconds_still_marks_once() -> None:
    from reachy_fleet_supervisor.fleet.body_demo import hold_pose

    mm = _ClockMM()
    hold_pose(mm, 0.0, tick=0.2, sleep=mm.sleep, monotonic=mm.now)
    assert mm.marks == [0.2], "a zero hold should still refresh the pose once"
    assert mm.t == 0.0
