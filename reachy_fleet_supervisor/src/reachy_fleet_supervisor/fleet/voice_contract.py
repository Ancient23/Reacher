"""Voice interaction contract for launching coding jobs (decision #20, U17).

Pure, unit-testable helpers backing the persona's spoken confirm-before-kickoff
flow for :mod:`spawn_manager` (the fleet-spawn voice tool):

- **Confirm before kickoff** — before a manager is actually spawned, Reachy
  reads back the resolved settings (task + location + run_mode + any
  non-default permission/gate note) and waits for a spoken OK. A quick "just
  do it" (detected here by :func:`wants_immediate_kickoff`) skips the
  readback and spawns right away.
- **Explain on request** — :data:`EXPLAIN_OPTIONS_TEXT` is a short, spoken
  explanation of the available options and how to ask for them, with
  examples, for the persona to say when the user asks what it can do.

This module has no I/O and no dependency on the fleet runtime, so it's fully
software-testable; the tool wiring (spawn_manager.py) and the persona
instructions (instructions.txt) are the HUMAN_GATE surface — whether the
readback actually sounds natural and the resume/skip paths work by voice on
the robot.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from .manager import DEFAULT_PERMISSION_MODE, DEFAULT_RUN_MODE

# Phrases that mean "skip the readback, spawn now" when spoken up front
# alongside (or instead of) a normal spawn request. Matched case-insensitively
# as a substring, so "yeah just do it" / "just do it, thanks" also match.
_SKIP_PHRASES = (
    "just do it",
    "just go ahead",
    "go ahead and do it",
    "skip the confirmation",
    "skip confirmation",
    "no need to confirm",
    "don't ask, just",
    "just spawn it",
    "just start it",
)

# Phrases that mean "yes, that's right, kick it off" once Reachy has already
# read the settings back.
_CONFIRM_PHRASES = (
    "go ahead",
    "yes",
    "yep",
    "yup",
    "sounds good",
    "do it",
    "confirmed",
    "correct",
    "that's right",
    "start it",
    "spawn it",
    "kick it off",
)


def wants_immediate_kickoff(text: Optional[str]) -> bool:
    """True if ``text`` asks to skip the confirmation readback (e.g. "just do it")."""
    if not text:
        return False
    lowered = text.lower()
    return any(phrase in lowered for phrase in _SKIP_PHRASES)


def sounds_like_confirmation(text: Optional[str]) -> bool:
    """True if ``text`` sounds like a spoken 'yes, go ahead' to a readback."""
    if not text:
        return False
    lowered = text.strip().lower()
    return any(phrase in lowered for phrase in _CONFIRM_PHRASES)


def _location_label(cwd: str) -> str:
    """A short, speakable label for a filesystem path (its folder name)."""
    name = Path(cwd).name
    return name or cwd


def summarize_spawn_confirmation(
    *,
    name: str,
    task: str,
    cwd: str,
    run_mode: str = DEFAULT_RUN_MODE,
    permission_mode: str = DEFAULT_PERMISSION_MODE,
    gate_note: Optional[str] = None,
) -> str:
    """Build the spoken readback Reachy says before spawning ``name``.

    Always mentions task + location; only calls out run_mode / permission_mode
    / gate_note when they differ from the trusted-local defaults, so the
    common case stays a short sentence.
    """
    location = _location_label(cwd)
    task_clause = task.strip().rstrip(".")
    parts = [f"Here's what I'll spawn: {name} on {location}, to {task_clause}."]

    if run_mode and run_mode != DEFAULT_RUN_MODE:
        parts.append(f"Run mode: {run_mode}.")
    if permission_mode and permission_mode != DEFAULT_PERMISSION_MODE:
        parts.append(f"Permission mode: {permission_mode}.")
    if gate_note:
        parts.append(gate_note)

    parts.append("Say go ahead, or tell me what to change.")
    return " ".join(parts)


EXPLAIN_OPTIONS_TEXT = (
    "I can spawn a background manager on any project on disk. Just tell me a name, "
    "the project, and what to do — like 'spawn a manager called tester on the fleet "
    "project to add unit tests'. By default it runs in the background, watchable on "
    "the dashboard and my body, and works autonomously without stopping for "
    "permission. If you want to steer it live from claude.ai or your phone instead, "
    "say 'run it in remote-control mode'. If you want it to check with you before "
    "risky steps, say 'have it ask before running commands'. I'll normally read the "
    "settings back and wait for your OK before starting — say 'just do it' up front "
    "to skip that and kick off right away."
)


__all__ = [
    "wants_immediate_kickoff",
    "sounds_like_confirmation",
    "summarize_spawn_confirmation",
    "EXPLAIN_OPTIONS_TEXT",
]
