"""Managers run ralph/drive loops (U13, decision #15).

A fleet *manager* is a full orchestrator: rather than a single goal it runs a
**ralph/drive loop** — a fresh-context iteration of a ``/drive-*`` command on a
target repo, sentinel-gated, with durable committed git state (plan.md decision
#15: we delegate orchestration to Claude rather than building a scheduler). This
module is the seam that turns "spawn a manager" into "spawn a manager that
**drives** a drive loop and reports each iteration's sentinel into the fleet".

It reuses — and deliberately does NOT reinvent — the existing fleet seams:

- the **status convention** (U5, decision #21): the manager emits its drive-loop
  report to ``<status_dir>/<name>/status.json`` once per iteration; the
  :class:`~reachy_fleet_supervisor.fleet.state.FleetPoller` already reads that
  directory and merges it into :class:`~reachy_fleet_supervisor.fleet.state.FleetState`,
  so a ``HUMAN_GATE`` sentinel surfaces on the dashboard / body / CLI WITHOUT
  interrupting the agent. The absolute status path is baked into the task prompt
  (a ``claude --bg`` session does not reliably inherit the shell environment, so
  we do not depend on :data:`~reachy_fleet_supervisor.fleet.status.STATUS_DIR_ENV`
  being visible inside it — we tell it exactly where to write).
- :class:`~reachy_fleet_supervisor.fleet.session_manager.SessionManager` /
  :class:`~reachy_fleet_supervisor.fleet.manager.FleetManager` spawn (U2/U3): a
  drive-loop manager is just a ``claude --bg`` manager whose *task* is the
  drive-loop prompt this module builds, spawned with ``cwd`` = the target repo.

The design split mirrors the rest of the package: :func:`build_drive_task` is a
pure prompt builder (unit-tested without a subprocess) and
:func:`spawn_drive_manager` is the thin spawn wrapper (integration-tested against
a real trivial ``claude --bg`` session driving a trivial sample repo).
"""

from __future__ import annotations
from typing import TYPE_CHECKING, Any, Optional, Sequence
from pathlib import Path
from dataclasses import dataclass

from .status import status_path_for
from .manager import (
    DEFAULT_RUN_MODE,
    DEFAULT_PERMISSION_MODE,
    FleetManager,
    FleetManagerError,
)


if TYPE_CHECKING:  # pragma: no cover - typing only
    from .session_manager import SessionManager


# The default ``/drive-*`` command a manager loops and the sentinel prefix its
# report line uses (mirrors this repo's own drive contract — see
# ``.claude/commands/drive-roadmap.md`` and ``docs/roadmap/STATE.yaml``).
DEFAULT_DRIVE_COMMAND = "/drive-roadmap"
DEFAULT_SENTINEL_PREFIX = "ROADMAP_STATE"


@dataclass(frozen=True)
class DriveLoopSpec:
    """What it takes to point a manager at a ``/drive-*`` loop on one repo.

    - ``name`` — the manager's stable voice handle and the key it writes its
      ``status.json`` under (the fleet matches the emitted status to the roster
      row by this name).
    - ``repo`` — the target repository; becomes the manager's working directory.
    - ``command`` — the ``/drive-*`` slash command (or natural-language drive
      instruction) run once per iteration. Defaults to ``/drive-roadmap``.
    - ``sentinel_prefix`` — the prefix of the loop's final sentinel line
      (``<PREFIX>: <STATE>``); defaults to ``ROADMAP_STATE``.
    - ``max_iterations`` — optional bound on how many iterations the manager runs
      this session (``None`` = run until a HUMAN_GATE / COMPLETE / FAILED).
    - ``status_dir`` — where the manager writes ``status.json`` (defaults to the
      fleet default status dir); the absolute per-manager path is computed from
      it and baked into the prompt.
    - ``extra_instructions`` — optional extra guidance appended to the prompt.
    """

    name: str
    repo: str | Path
    command: str = DEFAULT_DRIVE_COMMAND
    sentinel_prefix: str = DEFAULT_SENTINEL_PREFIX
    max_iterations: Optional[int] = None
    status_dir: Optional[str | Path] = None
    extra_instructions: Optional[str] = None

    def __post_init__(self) -> None:
        """Validate the spec (non-empty name/repo/command, sane max_iterations)."""
        if not str(self.name).strip():
            raise FleetManagerError("DriveLoopSpec.name must not be empty")
        if not str(self.repo).strip():
            raise FleetManagerError("DriveLoopSpec.repo must not be empty")
        if not str(self.command).strip():
            raise FleetManagerError("DriveLoopSpec.command must not be empty")
        if not str(self.sentinel_prefix).strip():
            raise FleetManagerError("DriveLoopSpec.sentinel_prefix must not be empty")
        if self.max_iterations is not None and self.max_iterations < 1:
            raise FleetManagerError(
                f"DriveLoopSpec.max_iterations must be >= 1, got {self.max_iterations!r}"
            )

    def status_path(self) -> Path:
        """Absolute path of the ``status.json`` the manager must write."""
        return status_path_for(self.name, self.status_dir)


def build_drive_task(spec: DriveLoopSpec) -> str:
    """Build the ``claude --bg`` task prompt that makes a manager DRIVE a loop.

    Pure (no subprocess): it composes the instruction telling the manager to run
    ``spec.command`` as a ralph loop on ``spec.repo`` and — crucially — to emit
    its drive-loop report to ``spec.status_path()`` once per iteration using the
    status convention (decision #21), escalating a ``HUMAN_GATE`` and stopping
    rather than looping past a blocker. The emitted ``status.json`` is exactly
    what :class:`~reachy_fleet_supervisor.fleet.state.FleetPoller` reads, so the
    sentinel flows into :class:`~reachy_fleet_supervisor.fleet.state.FleetState`.
    """
    status_path = spec.status_path()
    prefix = spec.sentinel_prefix

    if spec.max_iterations is not None:
        iteration_clause = (
            f"Run at most {spec.max_iterations} iteration(s) this session."
        )
        next_clause = f", until you have run {spec.max_iterations} iteration(s)"
    else:
        iteration_clause = (
            "Run iterations until you reach a HUMAN_GATE, COMPLETE, or FAILED."
        )
        next_clause = ""

    extra_clause = ""
    if spec.extra_instructions and spec.extra_instructions.strip():
        extra_clause = f"\nAdditional instructions:\n{spec.extra_instructions.strip()}\n"

    return f"""\
You are a fleet MANAGER running an autonomous ralph/drive loop on a target repo.

Target repo (your working directory): {spec.repo}
Drive command to run each iteration: {spec.command}
{iteration_clause}

How a drive loop works: each iteration is ONE fresh-context unit of work driven
by `{spec.command}`. The loop's durable state lives in the repo's committed git
state, so you pick up where the previous iteration left off. Run the command, let
it do one unit, then read its final sentinel line `{prefix}: <STATE>` where STATE
is one of CONTINUE | HUMAN_GATE | COMPLETE | FAILED.

CRITICAL — after EVERY iteration, REPORT your status so the fleet (Reachy + the
dashboard) can watch you WITHOUT interrupting you. Write a JSON object to EXACTLY
this file path (create parent directories if needed; overwrite the whole file
each time):

  {status_path}

with these keys:
  - "state":   the sentinel STATE you just reached — CONTINUE | HUMAN_GATE |
               COMPLETE | FAILED (use RUNNING while an iteration is mid-flight).
  - "summary": a one-line report of what just happened.
  - "name":    "{spec.name}"
  - "unit":    the unit / id you worked on this iteration, if known (else omit).
  - "gate":    when state is HUMAN_GATE, the copy-pasteable steps or question the
               human must act on (else omit).
This file IS how Reachy and the dashboard see you — keep it current.

Loop control:
  - On CONTINUE: write the status, then start the next iteration{next_clause}.
  - On HUMAN_GATE: write the status WITH the "gate" text, then STOP and wait — do
    NOT keep looping; a human (via Reachy) will answer and resume your session.
  - On COMPLETE or FAILED: write the final status and STOP.
{extra_clause}
Begin now."""


def spawn_drive_manager(
    session_manager: "SessionManager",
    spec: DriveLoopSpec,
    *,
    run_mode: str = DEFAULT_RUN_MODE,
    permission_mode: str = DEFAULT_PERMISSION_MODE,
    model: Optional[str] = None,
    mcp_configs: Sequence[str] = (),
    worktree: Optional[str | bool] = None,
    extra_args: Sequence[str] = (),
    **spawn_kwargs: Any,
) -> FleetManager:
    """Spawn a manager that drives *spec*'s ``/drive-*`` loop on its target repo.

    Thin wrapper over :meth:`SessionManager.spawn`: builds the drive-loop prompt
    via :func:`build_drive_task` and spawns it with ``name`` = ``spec.name`` and
    ``cwd`` = ``spec.repo``. All hosting knobs (``run_mode`` / ``permission_mode``
    / ``model`` / ``mcp_configs`` / ``worktree``) pass straight through, so a
    drive-loop manager is observed and controlled exactly like any other fleet
    manager. Returns the tracked :class:`FleetManager`.
    """
    task = build_drive_task(spec)
    return session_manager.spawn(
        task,
        name=spec.name,
        cwd=spec.repo,
        run_mode=run_mode,
        permission_mode=permission_mode,
        model=model,
        mcp_configs=mcp_configs,
        worktree=worktree,
        extra_args=extra_args,
        **spawn_kwargs,
    )
