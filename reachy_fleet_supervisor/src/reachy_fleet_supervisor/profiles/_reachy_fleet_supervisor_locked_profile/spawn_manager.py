"""spawn_manager — spawn a durable, watchable fleet manager by voice (U10).

Two voice paths delegate coding to Claude Code, and they are different on
purpose:

- ``ask_claude_code`` runs a quick *in-process* Claude Code worker (one shared
  session in a sandbox). It is great for "just do this real quick" — but it is
  NOT a ``claude --bg`` agent, so it never appears in ``claude agents --json``
  and therefore never shows on the fleet dashboard or the robot body.
- ``spawn_manager`` (this tool) starts a real ``claude --bg`` **background
  manager** on a project the user names, routed through the shared
  :class:`~reachy_fleet_supervisor.fleet.session_manager.SessionManager`. Because
  it lands in the roster, the running :class:`FleetPoller` folds it into the
  shared :class:`FleetState`, so it shows up on ``/fleet`` and on Reachy's body —
  the fleet the user can watch. Use this whenever the user asks to "spawn / start
  a manager" or to run a job they want to monitor.
"""

from __future__ import annotations
import os
import asyncio
import logging
from typing import Any, Dict
from pathlib import Path

from reachy_fleet_supervisor.tools.core_tools import Tool, ToolDependencies


logger = logging.getLogger(__name__)


class SpawnManager(Tool):
    """Spawn a durable ``claude --bg`` fleet manager visible on the dashboard."""

    name = "spawn_manager"
    description = (
        "Spawn a NEW background coding manager (a durable Claude Code session) on a project "
        "on disk, so the user can WATCH it on the fleet dashboard and on Reachy's body. Use "
        "this whenever the user asks to 'spawn' or 'start' a manager, or to run a longer job "
        "on a specific repo they want to monitor (as opposed to ask_claude_code, which does a "
        "quick task in-process and is not shown on the dashboard). Provide a short name for the "
        "manager, the project path on disk, and the task in plain language. Returns a short "
        "confirmation you then say out loud."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "A short, unique voice handle for the manager, e.g. 'tester'.",
            },
            "path": {
                "type": "string",
                "description": (
                    "Absolute path to the project/repo the manager should work in. "
                    "If omitted, falls back to FLEET_WORKER_CWD."
                ),
            },
            "task": {
                "type": "string",
                "description": "What the manager should do, described in plain language.",
            },
            "run_mode": {
                "type": "string",
                "enum": ["background", "remote-control"],
                "description": (
                    "Optional. How the manager is hosted. Leave unset for 'background' (a durable "
                    "claude --bg session you watch on the dashboard and Reachy's body, steered by "
                    "voice). Use 'remote-control' ONLY if the user explicitly asks to watch/steer "
                    "it from claude.ai or their phone — that is a separate steerable session that "
                    "needs claude.ai login and does NOT appear on the fleet dashboard."
                ),
            },
            "permission_mode": {
                "type": "string",
                "enum": ["acceptEdits", "auto", "bypassPermissions", "default", "dontAsk", "plan"],
                "description": (
                    "Optional. How autonomous the manager is. Leave unset for the trusted-local "
                    "default (bypassPermissions) so it runs without deadlocking on a permission "
                    "prompt it cannot answer. Only set this if the user explicitly asks to run a "
                    "manager more cautiously (e.g. 'acceptEdits' to gate shell commands)."
                ),
            },
        },
        "required": ["name", "task"],
    }

    async def __call__(self, deps: ToolDependencies, **kwargs: Any) -> Dict[str, Any]:
        name = (kwargs.get("name") or "").strip()
        task = (kwargs.get("task") or "").strip()
        raw_path = (kwargs.get("path") or "").strip() or os.getenv("FLEET_WORKER_CWD") or ""
        if not name:
            return {"error": "a short manager name is required"}
        if not task:
            return {"error": "a task is required"}
        if not raw_path:
            return {"error": "a project path is required (or set FLEET_WORKER_CWD)"}

        cwd = str(Path(raw_path).expanduser())
        if not Path(cwd).is_dir():
            return {"error": f"project path not found: {cwd}"}

        # Resolve the permission mode (explicit arg > $FLEET_PERMISSION_MODE >
        # bypassPermissions). A background manager has NO TTY, so a prompting mode
        # would deadlock it (state: blocked, waitingFor: permission prompt) — the
        # reported U10 bug. bypassPermissions runs it non-interactively.
        from reachy_fleet_supervisor.fleet.manager import (
            FleetManagerError,
            resolve_run_mode,
            resolve_permission_mode,
        )

        try:
            run_mode = resolve_run_mode(kwargs.get("run_mode"))
            permission_mode = resolve_permission_mode(kwargs.get("permission_mode"))
        except FleetManagerError as e:
            return {"error": str(e)}

        try:
            from reachy_fleet_supervisor.fleet.runtime import get_fleet_runtime

            runtime = get_fleet_runtime()
            # spawn() shells out (background) or launches a detached RC session —
            # blocking either way, so keep it off the event loop.
            manager = await asyncio.to_thread(
                lambda: runtime.session_manager.spawn(
                    task,
                    name=name,
                    cwd=cwd,
                    run_mode=run_mode,
                    permission_mode=permission_mode,
                )
            )
            # Nudge an immediate poll so the dashboard/body update without waiting
            # for the next interval. Best-effort: the loop will catch up regardless.
            # (Remote-control managers aren't roster-visible, so this is a no-op for
            # them, but it is harmless and keeps the background path snappy.)
            try:
                await asyncio.to_thread(runtime.poller.poll_once)
            except Exception:  # noqa: BLE001
                logger.debug("post-spawn poll_once failed; poller will catch up", exc_info=True)

            logger.info(
                "spawn_manager: spawned '%s' (id=%s, run_mode=%s, permission_mode=%s) on %s",
                manager.name, manager.id, run_mode, permission_mode, cwd,
            )
            if run_mode == "remote-control":
                result = (
                    f"Started a remote-control manager {manager.name} you can watch and steer "
                    "from claude.ai or your phone. It won't show on the fleet dashboard."
                )
            else:
                result = f"Spawned manager {manager.name} on the project. Watch it on the dashboard."
            return {
                "status": "spawned",
                "name": manager.name,
                "id": manager.id,
                "run_mode": run_mode,
                "result": result,
            }
        except Exception as e:  # noqa: BLE001
            logger.exception("spawn_manager failed")
            return {"error": str(e)}
