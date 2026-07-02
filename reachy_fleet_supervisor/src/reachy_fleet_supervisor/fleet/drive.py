"""Managers run ralph/drive loops (U13, decision #15) with an optional written
plan file per manager (U16, decision #11 "optional written plan file per
worker for plan-gated autonomy").

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
from .skill import FLEET_SKILL_NAME, write_fleet_skill_file
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
    - ``plan_content`` — optional plan.md-style text (task + gates) handed to the
      manager as ITS OWN written plan file (U16, decision #11). When set, it is
      written to ``plan_path`` (relative to ``repo`` unless absolute) before spawn
      — unless the file already exists, in which case the existing committed
      version wins (so a resumed manager keeps following its own prior edits,
      matching the drive contract's "durable committed state" pattern). ``None``
      (the default) means: no plan file is managed by this spec — the manager
      follows whatever ``command`` + the target repo's own files already say.
    - ``plan_path`` — where the plan file lives, relative to ``repo`` unless
      given as an absolute path. Defaults to ``"plan.md"``.
    - ``vision_command`` — optional shell command PREFIX for the worker-callable
      vision tool (U20, decision #9), e.g. built from
      :func:`~reachy_fleet_supervisor.fleet.vision.look_command`. When set, the
      task prompt advertises it so the manager can run
      ``<vision_command> "<question>"`` via Bash to LOOK at what it built and get
      an assessment back into its loop. ``None`` (default) = no vision clause.
    - ``install_skill`` — whether :func:`spawn_drive_manager` installs the fleet
      skill (U25, decision #18: a ``drive-loop`` variant teaching subagent
      orchestration / short spoken replies / gate-by-status) under
      ``repo/.claude/skills/`` before spawn. Defaults to ``True``.
    """

    name: str
    repo: str | Path
    command: str = DEFAULT_DRIVE_COMMAND
    sentinel_prefix: str = DEFAULT_SENTINEL_PREFIX
    max_iterations: Optional[int] = None
    status_dir: Optional[str | Path] = None
    extra_instructions: Optional[str] = None
    plan_content: Optional[str] = None
    plan_path: str | Path = "plan.md"
    vision_command: Optional[str] = None
    install_skill: bool = True

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
        if not str(self.plan_path).strip():
            raise FleetManagerError("DriveLoopSpec.plan_path must not be empty")
        if self.vision_command is not None and not str(self.vision_command).strip():
            raise FleetManagerError("DriveLoopSpec.vision_command must not be blank (use None)")

    def status_path(self) -> Path:
        """Absolute path of the ``status.json`` the manager must write."""
        return status_path_for(self.name, self.status_dir)

    def resolved_plan_path(self) -> Path:
        """Absolute path of the plan file (``plan_path`` resolved against ``repo``)."""
        p = Path(self.plan_path)
        return p if p.is_absolute() else Path(self.repo) / p


def write_plan_file(spec: DriveLoopSpec, *, overwrite: bool = False) -> Optional[Path]:
    """Materialize ``spec.plan_content`` at ``spec.resolved_plan_path()``, if any.

    No-op (returns ``None``) when ``spec.plan_content`` is ``None``. Otherwise
    creates parent directories as needed and writes the file, UNLESS it already
    exists and ``overwrite`` is ``False`` (default) — a manager's own prior edits
    to its plan file are the drive contract's committed state and must not be
    clobbered by a respawn/reconnect using the same spec. Returns the path
    written (or the pre-existing path left untouched).
    """
    if spec.plan_content is None:
        return None
    path = spec.resolved_plan_path()
    if path.exists() and not overwrite:
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(spec.plan_content, encoding="utf-8")
    return path


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

    plan_clause = ""
    if spec.plan_content is not None:
        plan_clause = f"""
You have a WRITTEN PLAN FILE at exactly this path (it has already been created
for you if it did not exist):

  {spec.resolved_plan_path()}

This plan file IS the drive contract's committed state for YOUR task (task +
gates), same role as this repo's own roadmap/plan docs — read it before your
first iteration and treat it as authoritative for what to do and which gates
apply. Update it (or the state file it points at) as you make progress, the
same way you already commit STATE/JOURNAL updates each iteration.
"""

    skill_clause = ""
    if spec.install_skill:
        skill_clause = f"""
You have a FLEET SKILL installed at .claude/skills/{FLEET_SKILL_NAME}/SKILL.md
(already materialized for you) — read it: it covers orchestrating via
subagents/Workflows, keeping spoken status summaries short, and escalating a
HUMAN_GATE through the status file (never invent a separate escalation
channel). Teach it to any subagent you spawn.
"""

    vision_clause = ""
    if spec.vision_command is not None:
        vision_clause = f"""
You have a VISION TOOL — "look at what I built" (screen capture -> Claude
assessment). When you need to visually VERIFY digital work (a UI, a chart, a
rendered page shown on screen), run via Bash:

  {spec.vision_command} "<your specific question about what should be visible>"

It captures the screen, assesses it against your question, and prints the
assessment — fold that verdict back into your loop (fix problems it reports).
Use it for perceptual checks only; do not substitute it for tests.
"""

    return f"""\
You are a fleet MANAGER running an autonomous ralph/drive loop on a target repo.

Target repo (your working directory): {spec.repo}
Drive command to run each iteration: {spec.command}
{iteration_clause}
{plan_clause}{skill_clause}{vision_clause}

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
    manager. If ``spec.plan_content`` is set, the plan file is materialized (via
    :func:`write_plan_file`, non-destructive of any pre-existing file) BEFORE
    spawn, so the manager finds it on its first read. Returns the tracked
    :class:`FleetManager`.
    """
    write_plan_file(spec)
    if spec.install_skill:
        write_fleet_skill_file(spec)
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
