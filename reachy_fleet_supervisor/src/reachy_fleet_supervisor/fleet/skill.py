"""Fleet skill (U25, decision #18) — a ``drive-loop`` variant installed into
manager sessions.

Decision #18: *"a `drive-loop` variant installed into manager sessions teaching
the fleet conventions: orchestrate via subagents/Workflows, keep spoken replies
short, escalate `HUMAN_GATE` by voice. Doubles as our headless dev/test
harness."*

Rather than teaching the manager anything new about ITS OWN drive loop (that is
already :func:`~.drive.build_drive_task`'s job — the exact command to run, the
exact ``status.json`` path, the sentinel vocabulary), this module packages
those SAME conventions as an installable Claude Code **skill**
(``.claude/skills/<name>/SKILL.md``, the standard on-disk skill format also
used by this repo's own ``.claude/commands`` / skills). Two reasons a real
skill file earns its keep over the prompt clause alone:

- it is **discoverable independent of the spawn prompt** — a manager that
  spawns its OWN subagents/workflows (plan.md decision #15: "we delegate
  orchestration to Claude rather than building a scheduler") can have those
  subagents pick up the same fleet conventions by reading the skill, without
  the top-level prompt having to re-explain everything every time;
- it is **testable headless** (pure string builder + a materializer that
  writes real bytes to a temp dir), which is this unit's acceptance bar — no
  robot, no live spawn required.

:func:`build_fleet_skill_markdown` is the pure content builder (frontmatter +
body); :func:`write_fleet_skill_file` materializes it under
``<repo>/.claude/skills/<FLEET_SKILL_NAME>/SKILL.md``; :func:`spawn_drive_manager`
(``.drive``) installs it before every drive-loop spawn, mirroring
:func:`~.drive.write_plan_file`'s materialize-before-spawn pattern — except the
skill file IS supervisor-authored teaching content (not the manager's own
output), so it is safe to overwrite on every spawn by default.
"""

from __future__ import annotations
from typing import TYPE_CHECKING, Optional
from pathlib import Path

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .drive import DriveLoopSpec


# The skill's stable directory name under ``.claude/skills/`` — also its
# frontmatter ``name:``.
FLEET_SKILL_NAME = "fleet-conventions"


def build_fleet_skill_markdown(
    *,
    status_path: str | Path,
    sentinel_prefix: str = "ROADMAP_STATE",
    vision_command: Optional[str] = None,
) -> str:
    """Build the ``SKILL.md`` content teaching a manager the fleet conventions.

    Pure — no filesystem/subprocess. Names the exact ``status_path`` a manager
    (or any subagent it spawns) must keep current, and encodes three
    conventions from decision #18:

    1. **Orchestrate via subagents/Workflows** — this manager IS the
       orchestrator (plan.md decision #15); delegate individual units of work
       to fresh-context subagents rather than doing everything inline.
    2. **Keep spoken replies short** — Reachy (the Realtime voice host) speaks
       status changes; a manager's own "summary" text becomes spoken words, so
       it must read as ONE short sentence, not a transcript dump.
    3. **Escalate HUMAN_GATE by voice** — writing ``state: HUMAN_GATE`` +
       "gate" text to the status file is what surfaces on Reachy's voice (U15)
       and the dashboard/body — never invent another escalation channel.

    When ``vision_command`` is given, an extra clause documents the
    worker-callable vision tool (U20) as an available orchestration primitive.
    """
    status_path = str(status_path)
    vision_clause = ""
    if vision_command is not None and str(vision_command).strip():
        vision_clause = f"""
## Looking at what you built

You have a vision tool for perceptual checks — screen capture -> Claude
assessment. Run via Bash:

    {vision_command} "<your specific question about what should be visible>"

Use it to VERIFY digital work (a UI, a chart, a rendered page), not as a
substitute for tests.
"""

    return f"""\
---
name: {FLEET_SKILL_NAME}
description: Fleet conventions for a Claude Code manager running inside the Reachy Fleet Supervisor (orchestrate via subagents/Workflows, keep spoken replies short, escalate HUMAN_GATE by voice). Use for every iteration of this manager's work, and teach it to any subagent you spawn.
---

# Fleet conventions

You are a fleet MANAGER hosted by the Reachy Fleet Supervisor. A physical
robot (Reachy Mini) speaks your status changes aloud and shows them on its
body/dashboard — a human is listening, not reading your raw transcript. Three
conventions apply to every iteration:

## 1. Orchestrate, don't do everything yourself

You are the orchestrator (not a single-shot worker): delegate individual units
of work to fresh-context **subagents**, or a dynamic **Workflow**, rather than
doing an entire multi-step task inline in one long turn. This mirrors the
`drive-loop` skill's ralph pattern — one PR-sized unit per iteration, durable
state committed to git, not held in your own context. If you spawn subagents,
brief them with the SAME conventions in this file (orchestrate further /
short replies / gate-by-status, as applicable to their own scope).

## 2. Keep spoken replies short

Whatever you write to `"summary"` in your status report (see below) is a
candidate for Reachy to SPEAK aloud, not read as text. Keep it to ONE short,
natural sentence — "fixed the auth bug and tests are green", not a bulleted
changelog. Save the detail for your commit messages / JOURNAL, which the human
reads on their own schedule.

## 3. Escalate HUMAN_GATE by voice — through the status file, not ad hoc

The ONLY escalation channel is the status convention (decision #21): write
your report to EXACTLY this path every iteration (create parent directories
as needed, overwrite the whole file each time):

    {status_path}

with keys `"state"` (`RUNNING` | `CONTINUE` | `HUMAN_GATE` | `COMPLETE` | `FAILED` — the
same vocabulary as this manager's own `{sentinel_prefix}: <STATE>` sentinel), `"summary"`
(the short spoken line from #2), `"name"`, optionally `"unit"`, and — when
`"state"` is `HUMAN_GATE` — `"gate"` with the copy-pasteable steps or question
the human must act on. This file is what the fleet poller reads into
FleetState, which is what Reachy's voice renderer and the dashboard both watch
— NEVER invent a separate way to ask the human something; writing `state:
HUMAN_GATE` with a clear `gate` string IS how you ask, and it is spoken aloud
automatically. After a HUMAN_GATE, STOP and wait; do not keep looping past a
blocker.
{vision_clause}
This skill doubles as the fleet's own headless dev/test harness: everything
above is verifiable without the robot (the status file, the spoken-summary
convention, the gate escalation) except actually HEARING Reachy speak it,
which is a hardware-gated check elsewhere in the roadmap.
"""


def fleet_skill_path(repo: str | Path) -> Path:
    """Where the fleet skill lives inside a manager's ``repo`` working directory."""
    return Path(repo) / ".claude" / "skills" / FLEET_SKILL_NAME / "SKILL.md"


def write_fleet_skill_file(
    spec: "DriveLoopSpec",
    *,
    overwrite: bool = True,
) -> Path:
    """Materialize the fleet skill under ``spec.repo/.claude/skills/...``.

    Unlike :func:`~.drive.write_plan_file` (never clobbers a manager's own
    committed edits), this file is supervisor-authored teaching content, not
    manager output — safe to overwrite on every spawn by default so a stale
    convention never lingers across a respawn with an updated spec. Pass
    ``overwrite=False`` to preserve a human/manager-edited copy instead.
    """
    path = fleet_skill_path(spec.repo)
    if path.exists() and not overwrite:
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    content = build_fleet_skill_markdown(
        status_path=spec.status_path(),
        sentinel_prefix=spec.sentinel_prefix,
        vision_command=spec.vision_command,
    )
    path.write_text(content, encoding="utf-8")
    return path
