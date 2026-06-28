"""Claude supervisor brain (Option A) — Phase 1: a single Claude Code worker session.

A `WorkerSession` wraps a persistent `ClaudeSDKClient` running in a project directory
(a git worktree later). You feed it the user's spoken request; it streams back events:
thinking / tool-use (→ "working") / text (→ speak) / done. Architected so a
`SessionManager` can hold N of these for the multi-session fleet later.

All Claude work is async and touches no robot hardware — the app runs this in a
dedicated thread and bridges audio via queues (see supervisor_app.py).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import AsyncIterator, Optional

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    TextBlock,
    ThinkingBlock,
    ToolUseBlock,
)

logger = logging.getLogger(__name__)


DEFAULT_SYSTEM_PROMPT = (
    "You are a coding worker supervised by Reachy Mini, a small expressive robot that "
    "reads your replies ALOUD to a human. Because your words are spoken by a little robot:\n"
    "- Keep spoken replies short and conversational — usually one or two sentences.\n"
    "- Do the work; when you finish a step, say so briefly and plainly.\n"
    "- If a request is genuinely ambiguous, ask ONE short clarifying question, then stop.\n"
    "- Avoid code blocks, file paths, and long lists in your spoken reply unless asked — "
    "they don't read well aloud.\n"
    "You are operating autonomously within the user's approved intent; act, don't over-ask."
)


class WorkerStatus(str, Enum):
    """Lifecycle state of a worker, used to drive Reachy's embodiment."""

    IDLE = "idle"
    THINKING = "thinking"
    WORKING = "working"
    SPEAKING = "speaking"
    ASKING = "asking"
    ERROR = "error"


@dataclass
class WorkerEvent:
    """A streamed event from a worker turn."""

    kind: str  # "thinking" | "tool" | "text" | "done" | "error"
    text: str = ""
    tool: str = ""


class WorkerSession:
    """A single persistent Claude Code session bound to one project directory."""

    def __init__(
        self,
        project_dir: str | Path,
        *,
        name: str = "worker",
        system_prompt: str = DEFAULT_SYSTEM_PROMPT,
        permission_mode: str = os.getenv("FLEET_PERMISSION_MODE", "bypassPermissions"),
        # Fast, capable default for snappy voice replies; override per-machine.
        # Bump to claude-opus-4-8 + effort high for the hardest coding tasks.
        model: Optional[str] = os.getenv("FLEET_WORKER_MODEL") or "claude-sonnet-4-6",
        effort: Optional[str] = os.getenv("FLEET_WORKER_EFFORT") or "low",
    ) -> None:
        self.name = name
        self.project_dir = str(Path(project_dir).expanduser().resolve())
        self.status = WorkerStatus.IDLE
        opts: dict = dict(
            cwd=self.project_dir,
            system_prompt=system_prompt,
            permission_mode=permission_mode,
            model=model,
        )
        if effort:
            opts["effort"] = effort
        self._options = ClaudeAgentOptions(**opts)
        self._client: Optional[ClaudeSDKClient] = None

    async def start(self) -> None:
        """Connect the underlying Claude Code session."""
        Path(self.project_dir).mkdir(parents=True, exist_ok=True)
        self._client = ClaudeSDKClient(options=self._options)
        await self._client.connect()
        logger.info("Worker '%s' connected (cwd=%s)", self.name, self.project_dir)

    async def ask(self, user_text: str) -> AsyncIterator[WorkerEvent]:
        """Send one spoken request and stream WorkerEvents until the turn completes."""
        if self._client is None:
            raise RuntimeError("WorkerSession.start() must be called first")

        self.status = WorkerStatus.THINKING
        await self._client.query(user_text)
        try:
            async for msg in self._client.receive_response():
                if isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if isinstance(block, ThinkingBlock):
                            self.status = WorkerStatus.THINKING
                            yield WorkerEvent("thinking")
                        elif isinstance(block, ToolUseBlock):
                            self.status = WorkerStatus.WORKING
                            yield WorkerEvent("tool", tool=getattr(block, "name", ""))
                        elif isinstance(block, TextBlock):
                            yield WorkerEvent("text", text=block.text)
                elif isinstance(msg, ResultMessage):
                    yield WorkerEvent("done")
        except Exception as exc:  # surface, don't crash the brain loop
            self.status = WorkerStatus.ERROR
            logger.exception("Worker '%s' turn failed", self.name)
            yield WorkerEvent("error", text=str(exc))
        finally:
            if self.status not in (WorkerStatus.ERROR,):
                self.status = WorkerStatus.IDLE

    async def interrupt(self) -> None:
        if self._client is not None:
            await self._client.interrupt()

    async def stop(self) -> None:
        if self._client is not None:
            await self._client.disconnect()
            self._client = None
            logger.info("Worker '%s' disconnected", self.name)
