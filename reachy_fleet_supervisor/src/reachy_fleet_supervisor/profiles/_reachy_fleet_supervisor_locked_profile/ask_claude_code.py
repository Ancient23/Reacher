"""ask_claude_code — delegate real engineering work to Claude Code (Max plan).

The OpenAI Realtime assistant calls this tool whenever the user wants actual
development done. It runs a persistent Claude Code worker session (claude-agent-sdk)
in the user's project directory and returns Claude's short spoken summary, which the
Realtime voice then reads aloud. Coding stays on the Claude Max plan; the voice/
personality layer is OpenAI Realtime.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any, Dict

from reachy_fleet_supervisor.tools.core_tools import Tool, ToolDependencies

logger = logging.getLogger(__name__)

_WORKER: Any = None
_WORKER_LOCK = asyncio.Lock()


async def _get_worker() -> Any:
    """Lazily start one persistent Claude Code worker (reused across calls)."""
    global _WORKER
    async with _WORKER_LOCK:
        if _WORKER is None:
            from reachy_fleet_supervisor.claude_brain import WorkerSession

            cwd = os.getenv("FLEET_WORKER_CWD") or str(Path.home() / "reachy_worker_sandbox")
            Path(cwd).mkdir(parents=True, exist_ok=True)
            _WORKER = WorkerSession(cwd, name="voice-delegate")
            await _WORKER.start()
            logger.info("ask_claude_code: Claude Code worker started (cwd=%s)", cwd)
    return _WORKER


class AskClaudeCode(Tool):
    """Delegate a coding/engineering task to Claude Code, running on the Max plan."""

    name = "ask_claude_code"
    description = (
        "Delegate a real coding or engineering task to Claude Code, which works in the user's "
        "project on disk — writing or editing code, running commands, creating or reading files, "
        "fixing bugs, investigating a codebase, etc. Call this whenever the user asks you to "
        "actually DO development work (not just chat about it). Pass the task in plain language; "
        "it returns a short summary of what Claude did, which you then say out loud."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "task": {
                "type": "string",
                "description": "The coding task or question, described in plain language.",
            },
        },
        "required": ["task"],
    }

    async def __call__(self, deps: ToolDependencies, **kwargs: Any) -> Dict[str, Any]:
        task = (kwargs.get("task") or "").strip()
        if not task:
            return {"error": "task is required"}
        logger.info("ask_claude_code task: %s", task)
        try:
            worker = await _get_worker()
            parts = []
            async for ev in worker.ask(task):
                if ev.kind == "text" and ev.text.strip():
                    parts.append(ev.text.strip())
                elif ev.kind == "error":
                    return {"error": ev.text}
            return {"status": "done", "result": " ".join(parts).strip() or "Done."}
        except Exception as e:  # noqa: BLE001
            logger.exception("ask_claude_code failed")
            return {"error": str(e)}
