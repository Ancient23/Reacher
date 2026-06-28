"""Hardware demo for the body renderer (U8 HUMAN_GATE).

Runs the real :class:`~reachy_fleet_supervisor.fleet.body.FleetBodyRenderer`
against the physical robot, driving it through a **scripted sequence of synthetic
fleet states** so a human can confirm — without needing live ``claude`` agents —
that the body signals the right manager:

- two managers both working  → face front, antennas in the *working* pose;
- the right-hand manager hits a HUMAN_GATE → torso turns toward it, antennas perk
  up (*attention*);
- the left-hand manager fails → torso turns the other way, antennas droop
  (*failed*);
- both finish → face front, antennas in the *done* pose.

The scenario itself (:func:`demo_steps`) is pure and unit-tested; only
:func:`main` touches the motor stack (lazy imports), so importing this module is
safe in tests and on machines without a robot.

Run on the robot (powered via the **rear USB-C** port)::

    cd reachy_fleet_supervisor
    uv run python -m reachy_fleet_supervisor.fleet.body_demo --hold 4
"""

from __future__ import annotations
import time
import logging
import argparse
from typing import Optional, Sequence
from dataclasses import field, dataclass

from .body import FleetBodyRenderer, body_signal_for, make_goto_applier
from .state import FleetState
from .status import (
    SENTINEL_FAILED,
    SENTINEL_RUNNING,
    SENTINEL_COMPLETE,
    SENTINEL_HUMAN_GATE,
    ManagerStatus,
)
from .manager import AgentInfo


logger = logging.getLogger(__name__)

# Two stable managers; keys sort as "build" < "tests" → build = left slot
# (yaw -span), tests = right slot (yaw +span). Names are what the human hears.
_BUILD = "build"
_TESTS = "tests"


def _row(short_id: str, name: str, state: str) -> AgentInfo:
    return AgentInfo.from_dict(
        {
            "id": short_id,
            "sessionId": f"{short_id}-0000-0000-0000-000000000000",
            "kind": "background",
            "name": name,
            "cwd": "/demo",
            "state": state,
            "status": state,
        }
    )


@dataclass(frozen=True)
class DemoStep:
    """One scripted beat: a label, the rows to apply, and emitted statuses."""

    label: str
    agents: tuple[AgentInfo, ...]
    statuses: dict[str, ManagerStatus] = field(default_factory=dict)


def demo_steps() -> list[DemoStep]:
    """Return the scripted fleet-state sequence the demo plays (pure)."""
    build_working = _row(_BUILD, _BUILD, "working")
    tests_working = _row(_TESTS, _TESTS, "working")
    return [
        DemoStep(
            label="both managers working",
            agents=(build_working, tests_working),
            statuses={
                _BUILD: ManagerStatus(state=SENTINEL_RUNNING, name=_BUILD),
                _TESTS: ManagerStatus(state=SENTINEL_RUNNING, name=_TESTS),
            },
        ),
        DemoStep(
            label=f"'{_TESTS}' hits a HUMAN_GATE (needs you)",
            agents=(build_working, _row(_TESTS, _TESTS, "blocked")),
            statuses={
                _BUILD: ManagerStatus(state=SENTINEL_RUNNING, name=_BUILD),
                _TESTS: ManagerStatus(
                    state=SENTINEL_HUMAN_GATE, name=_TESTS, summary="approve deploy?"
                ),
            },
        ),
        DemoStep(
            label=f"'{_BUILD}' fails",
            agents=(_row(_BUILD, _BUILD, "failed"), tests_working),
            statuses={
                _BUILD: ManagerStatus(state=SENTINEL_FAILED, name=_BUILD, summary="build broke"),
                _TESTS: ManagerStatus(state=SENTINEL_RUNNING, name=_TESTS),
            },
        ),
        DemoStep(
            label="both managers complete",
            agents=(_row(_BUILD, _BUILD, "done"), _row(_TESTS, _TESTS, "done")),
            statuses={
                _BUILD: ManagerStatus(state=SENTINEL_COMPLETE, name=_BUILD),
                _TESTS: ManagerStatus(state=SENTINEL_COMPLETE, name=_TESTS),
            },
        ),
    ]


def _apply_step(state: FleetState, step: DemoStep, *, now: float) -> str:
    """Apply one step to *state* and return the expected body pose description."""
    snap = state.apply(step.agents, statuses=step.statuses, now=now)
    return body_signal_for(snap).describe()


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Drive the physical robot through the scripted scenario."""
    parser = argparse.ArgumentParser(description="U8 body-renderer hardware demo")
    parser.add_argument(
        "--hold", type=float, default=4.0, help="seconds to hold each step (default 4)"
    )
    parser.add_argument(
        "--robot-name", default=None, help="optional Reachy Mini robot name"
    )
    parser.add_argument(
        "--duration", type=float, default=1.0, help="motion duration per move (s)"
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    # Lazy hardware imports so the module imports cleanly without a robot.
    from reachy_mini import ReachyMini
    from ..moves import MovementManager

    robot_kwargs = {"robot_name": args.robot_name} if args.robot_name else {}
    print("Connecting to Reachy Mini (rear USB-C port should be used)...")
    robot = ReachyMini(**robot_kwargs)
    movement_manager = MovementManager(current_robot=robot)
    movement_manager.start()

    state = FleetState()
    applier = make_goto_applier(robot, movement_manager, duration=args.duration)
    renderer = FleetBodyRenderer(applier)
    unsubscribe = renderer.attach(state, fire_immediately=False)

    try:
        for i, step in enumerate(demo_steps(), start=1):
            expected = _apply_step(state, step, now=float(i))
            print(f"\n[{i}] {step.label}")
            print(f"    LOOK FOR: {expected}")
            time.sleep(args.hold)
        print("\nDemo complete. Returning to neutral.")
    finally:
        unsubscribe()
        movement_manager.stop()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
