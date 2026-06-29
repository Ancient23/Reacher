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

import asyncio
import logging
import os
from pathlib import Path
from typing import Any, Dict

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

        try:
            from reachy_fleet_supervisor.fleet.runtime import get_fleet_runtime

            runtime = get_fleet_runtime()
            # spawn() shells out to `claude --bg` (blocking) — keep it off the loop.
            manager = await asyncio.to_thread(
                runtime.session_manager.spawn, task, name=name, cwd=cwd
            )
            # Nudge an immediate poll so the dashboard/body update without waiting
            # for the next interval. Best-effort: the loop will catch up regardless.
            try:
                await asyncio.to_thread(runtime.poller.poll_once)
            except Exception:  # noqa: BLE001
                logger.debug("post-spawn poll_once failed; poller will catch up", exc_info=True)

            logger.info("spawn_manager: spawned '%s' (id=%s) on %s", manager.name, manager.id, cwd)
            return {
                "status": "spawned",
                "name": manager.name,
                "id": manager.id,
                "result": f"Spawned manager {manager.name} on the project. Watch it on the dashboard.",
            }
        except Exception as e:  # noqa: BLE001
            logger.exception("spawn_manager failed")
            return {"error": str(e)}
