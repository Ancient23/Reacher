"""Body renders FleetState — the robot is a third renderer (U8, hardware gate).

The fleet's design is *one source of truth, many renderers* (plan.md §3): the
headless CLI, the web dashboard **and the robot body** all subscribe to a single
:class:`~reachy_fleet_supervisor.fleet.state.FleetState`. This module is the body
renderer. It maps a :class:`~reachy_fleet_supervisor.fleet.state.FleetSnapshot`
to two physical signals:

- **Body yaw → *who* needs attention.** Each manager owns a fixed slot angle
  (managers sorted by key, spread symmetrically across ±:data:`BODY_YAW_SPAN_DEG`
  — the same key ordering U9 uses for colors, so "the one it's facing" is stable).
  The torso turns toward the manager currently needing a human (a HUMAN_GATE /
  failed / waiting manager); when nobody needs attention it faces front (0°).
- **Antennas → *what* state.** The antenna pose encodes the signalled state
  (idle / working / attention / done / failed) — see :data:`ANTENNA_POSES`.

The mapping itself is **pure and unit-tested in degrees** (:func:`body_signal_for`
returns a frozen :class:`BodyPose`); nothing here imports the motor stack at
module load. The hardware seam is :func:`make_goto_applier`, which converts a
``BodyPose`` to a queued ``GotoQueueMove`` on the movement manager (lazy imports
so tests and the dashboard never need ``reachy_mini``). :class:`FleetBodyRenderer`
wires the two together as a FleetState subscriber and de-dupes unchanged poses so
the body only moves when the picture actually changes (decision #21: read status,
never interrupt the agent).

Because real motion can only be confirmed on the physical robot, U8 is a
HARDWARE gate: this module is software-tested end-to-end against a fake applier,
but "the body signals the right manager" is verified by a human (see
``docs/roadmap/STATE.yaml`` → ``status.pending_gate``).
"""

from __future__ import annotations
import math
import logging
from typing import Any, Callable, Optional, Sequence
from dataclasses import dataclass

from .state import FleetState, FleetSnapshot, ManagerSnapshot
from .status import (
    SENTINEL_FAILED,
    SENTINEL_RUNNING,
    SENTINEL_COMPLETE,
    SENTINEL_CONTINUE,
)


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Signal vocabulary + physical mapping (degrees; pure)
# ---------------------------------------------------------------------------

# Normalized body-signal states. "attention" = a manager wants a human now.
SIGNAL_IDLE = "idle"
SIGNAL_WORKING = "working"
SIGNAL_ATTENTION = "attention"
SIGNAL_DONE = "done"
SIGNAL_FAILED = "failed"

# Half-range of the torso sweep, in degrees. Managers are spread symmetrically
# across [-SPAN, +SPAN]; kept modest so the Lite's body_yaw stays well in range.
BODY_YAW_SPAN_DEG = 40.0

# Antenna pose per signal, as (left, right) degrees. Distinct magnitudes so the
# states are tellable apart at a glance: perked-up = wants you, drooped = failed.
ANTENNA_POSES: dict[str, tuple[float, float]] = {
    SIGNAL_IDLE: (0.0, 0.0),
    SIGNAL_WORKING: (15.0, 15.0),
    SIGNAL_ATTENTION: (45.0, 45.0),
    SIGNAL_DONE: (30.0, 30.0),
    SIGNAL_FAILED: (-30.0, -30.0),
}

# Defensive clamp so a bad config can never command the antennas past this.
ANTENNA_MAX_DEG = 60.0


def antenna_pose(signal: str) -> tuple[float, float]:
    """Return antenna ``(left, right)`` degrees for a *signal* (idle if unknown)."""
    left, right = ANTENNA_POSES.get(signal, ANTENNA_POSES[SIGNAL_IDLE])

    def _clamp(v: float) -> float:
        return max(-ANTENNA_MAX_DEG, min(ANTENNA_MAX_DEG, v))

    return (_clamp(left), _clamp(right))


def slot_yaw(index: int, count: int, *, span: float = BODY_YAW_SPAN_DEG) -> float:
    """Body-yaw angle (deg) for manager *index* of *count*, spread over ±*span*.

    One manager faces front (0°); N managers are evenly spaced and symmetric
    around 0 in roster (key-sorted) order, so a given manager always sits at the
    same angle regardless of how many polls have happened.
    """
    if count <= 1:
        return 0.0
    frac = index / (count - 1)  # 0 .. 1 across the row
    return -span + 2.0 * span * frac


def signal_state(m: ManagerSnapshot) -> str:
    """Return the normalized body-signal state for one manager.

    Prefers the manager's own emitted report (decision #21), falling back to the
    ``claude agents --json`` roster row. ``failed`` outranks ``attention`` so a
    crashed manager reads as failed rather than merely waiting.
    """
    norm = m.report.normalized_state if m.report is not None else None
    raw_state = (m.state or "").strip().lower()

    if norm == SENTINEL_FAILED or raw_state == "failed":
        return SIGNAL_FAILED
    if m.needs_attention:  # HUMAN_GATE / blocked / waitingFor (failed handled above)
        return SIGNAL_ATTENTION
    if norm == SENTINEL_COMPLETE or raw_state in ("done", "complete", "completed"):
        return SIGNAL_DONE
    if norm in (SENTINEL_RUNNING, SENTINEL_CONTINUE) or raw_state in (
        "running",
        "working",
        "busy",
        "active",
    ):
        return SIGNAL_WORKING
    return SIGNAL_IDLE


def _ordered(managers: Sequence[ManagerSnapshot]) -> list[ManagerSnapshot]:
    """Return managers in the stable key order used for slot assignment (matches U9)."""
    return sorted(managers, key=lambda m: m.key)


def choose_target(snapshot: FleetSnapshot) -> Optional[ManagerSnapshot]:
    """Pick the manager the body should face: the one most needing a human.

    Returns ``None`` when nobody needs attention. Among those that do, a
    ``failed`` manager wins over one merely waiting; ties break by key so the
    choice is deterministic across renderers and polls.
    """
    attn = [m for m in snapshot.managers if m.needs_attention]
    if not attn:
        return None
    attn.sort(key=lambda m: (0 if signal_state(m) == SIGNAL_FAILED else 1, m.key))
    return attn[0]


# ---------------------------------------------------------------------------
# BodyPose — the immutable physical command (degrees)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BodyPose:
    """An immutable body command: where to face + how the antennas sit.

    Pure data in **degrees** (value-comparable, so the renderer can skip
    re-issuing an unchanged pose). The hardware applier converts to radians.
    """

    body_yaw_deg: float = 0.0
    antenna_left_deg: float = 0.0
    antenna_right_deg: float = 0.0
    target_key: Optional[str] = None
    signal: str = SIGNAL_IDLE
    reason: str = ""

    def describe(self) -> str:
        """One-line human-readable summary (handy for logs / a CLI preview)."""
        who = self.target_key or "front"
        return (
            f"face {who} @ yaw {self.body_yaw_deg:+.0f}° · antennas "
            f"{self.antenna_left_deg:+.0f}/{self.antenna_right_deg:+.0f}° · {self.signal}"
            + (f" ({self.reason})" if self.reason else "")
        )


def _aggregate_signal(managers: Sequence[ManagerSnapshot]) -> str:
    """Whole-fleet signal when nobody needs attention: working > done > idle."""
    signals = [signal_state(m) for m in managers]
    if any(s == SIGNAL_WORKING for s in signals):
        return SIGNAL_WORKING
    if signals and all(s == SIGNAL_DONE for s in signals):
        return SIGNAL_DONE
    return SIGNAL_IDLE


def body_signal_for(
    snapshot: FleetSnapshot, *, span: float = BODY_YAW_SPAN_DEG
) -> BodyPose:
    """Map a fleet snapshot to the body command (pure).

    Faces the manager needing attention (its fixed slot angle) with antennas set
    to that manager's state; when nobody needs attention, faces front with a
    whole-fleet aggregate signal (working / done / idle).
    """
    ordered = _ordered(snapshot.managers)
    target = choose_target(snapshot)

    if target is None:
        sig = _aggregate_signal(ordered)
        left, right = antenna_pose(sig)
        reason = f"{len(ordered)} manager(s), none waiting"
        return BodyPose(
            body_yaw_deg=0.0,
            antenna_left_deg=left,
            antenna_right_deg=right,
            target_key=None,
            signal=sig,
            reason=reason,
        )

    keys = [m.key for m in ordered]
    idx = keys.index(target.key)
    yaw = slot_yaw(idx, len(ordered), span=span)
    sig = signal_state(target)
    left, right = antenna_pose(sig)
    label = target.name or target.key
    reason = f"{label} needs attention ({sig})"
    return BodyPose(
        body_yaw_deg=yaw,
        antenna_left_deg=left,
        antenna_right_deg=right,
        target_key=target.key,
        signal=sig,
        reason=reason,
    )


# ---------------------------------------------------------------------------
# Hardware applier — the motor seam (lazy imports; HUMAN-GATED)
# ---------------------------------------------------------------------------

# An applier turns a BodyPose into real (or fake) motion.
BodyApplier = Callable[[BodyPose], Any]


def make_goto_applier(
    reachy_mini: Any,
    movement_manager: Any,
    *,
    duration: float = 1.0,
) -> BodyApplier:
    """Build an applier that queues a ``GotoQueueMove`` for a :class:`BodyPose`.

    Degrees → radians here (the kinematics layer is radian-native). The current
    pose is read best-effort for a smooth interpolation start; on any read error
    it falls back to the move's neutral default rather than refusing to move.
    ``reachy_mini``/``GotoQueueMove`` are imported lazily so importing this module
    (for the pure mapping or the tests) never pulls in the motor stack.
    """

    def apply(pose: BodyPose) -> Any:
        from reachy_mini.utils import create_head_pose
        from ..dance_emotion_moves import GotoQueueMove

        # Body yaw carries the "who"; head stays neutral relative to the torso.
        target_head = create_head_pose(0, 0, 0, 0, 0, 0, degrees=True)

        start_head = None
        try:
            start_head = reachy_mini.get_current_head_pose()
        except Exception:  # noqa: BLE001 — best-effort start for smooth motion
            logger.debug("body applier: could not read current head pose", exc_info=True)

        start_body_yaw: Optional[float] = None
        start_antennas: Optional[tuple[float, float]] = None
        try:
            _, joints = reachy_mini.get_current_joint_positions()
            start_body_yaw = float(joints[0])
            start_antennas = (float(joints[1]), float(joints[2]))
        except Exception:  # noqa: BLE001 — fall back to neutral start
            logger.debug("body applier: could not read current joints", exc_info=True)

        move = GotoQueueMove(
            target_head_pose=target_head,
            start_head_pose=start_head,
            target_antennas=(
                math.radians(pose.antenna_left_deg),
                math.radians(pose.antenna_right_deg),
            ),
            start_antennas=start_antennas,
            target_body_yaw=math.radians(pose.body_yaw_deg),
            start_body_yaw=start_body_yaw,
            duration=duration,
        )
        movement_manager.queue_move(move)
        set_moving = getattr(movement_manager, "set_moving_state", None)
        if callable(set_moving):
            set_moving(duration)
        return move

    return apply


# ---------------------------------------------------------------------------
# FleetBodyRenderer — the FleetState subscriber
# ---------------------------------------------------------------------------


class FleetBodyRenderer:
    """Render FleetState on the robot body: a FleetState subscriber.

    Each fleet update is mapped to a :class:`BodyPose`; the pose is applied only
    when it changed from the last one, so a steady fleet leaves the body still
    (and a working manager doesn't get its turn interrupted just because a log
    line scrolled). The *applier* is injected — :func:`make_goto_applier` on the
    real robot, a recording fake in tests.
    """

    def __init__(self, applier: BodyApplier, *, span: float = BODY_YAW_SPAN_DEG) -> None:
        """Build a renderer that applies poses via *applier* (yaw span ±*span*)."""
        self._apply = applier
        self._span = span
        self._last_pose: Optional[BodyPose] = None

    @property
    def last_pose(self) -> Optional[BodyPose]:
        """The most recently applied pose (``None`` before the first update)."""
        return self._last_pose

    def pose_for(self, snapshot: FleetSnapshot) -> BodyPose:
        """Return the pose this renderer would command for *snapshot* (pure)."""
        return body_signal_for(snapshot, span=self._span)

    def on_snapshot(self, snapshot: FleetSnapshot) -> Optional[BodyPose]:
        """Compute + apply the pose for *snapshot*; skip if unchanged.

        Returns the pose if it was applied, else ``None``. A failing applier is
        logged, not raised, so one bad motion never breaks the pub/sub fan-out.
        """
        pose = self.pose_for(snapshot)
        if pose == self._last_pose:
            return None
        try:
            self._apply(pose)
        except Exception:  # noqa: BLE001 — a motor hiccup must not kill the poller
            logger.exception("body applier failed for pose %s", pose.describe())
            return None
        self._last_pose = pose
        logger.info("body → %s", pose.describe())
        return pose

    def attach(
        self, state: FleetState, *, fire_immediately: bool = True
    ) -> Callable[[], None]:
        """Subscribe to *state*; return a zero-arg unsubscribe handle."""

        def _sub(snapshot: FleetSnapshot) -> None:
            self.on_snapshot(snapshot)

        return state.subscribe(_sub, fire_immediately=fire_immediately)
