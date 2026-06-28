"""FleetManager — spawn and control one Claude Code background (``--bg``) manager.

A *manager* is a durable, supervisor-hosted Claude Code session created with
``claude --bg``. Per the verified fleet mechanics (Claude Code 2.1.195, see the
project memory ``ref-claude-code-fleet-mechanics``), background agents are the
fleet's backbone: they return immediately on spawn, survive terminal/agent-view
close and sleep, are observable non-interactively via ``claude agents --json``,
and are controllable via ``claude stop`` / ``claude rm``.

This module wraps exactly that surface for ONE manager (U2). Higher layers build
on it: U3 ``SessionManager`` tracks N of these and reconnects on restart; U4
``FleetState`` polls ``claude agents --json`` for live status.

Spawn shape (decision #19 — ``run_mode="background"``)::

    claude --bg --name <name> --session-id <uuid> --permission-mode <mode> \
        [--model <m>] [--mcp-config <c> ...] [-w [name]] <task>

We pass a generated UUID via ``--session-id`` (honored on setups that support
it), but do NOT assume it: verified on 2.1.195/Windows, ``claude --bg`` mints
its own session id. So the short id parsed from the spawn output is the
authoritative handle (it is what ``claude stop``/``rm`` accept), and the full
``sessionId`` is resolved by looking that short id up in ``claude agents
--json``.

Design split for testability: the argv-building and output-parsing are pure
functions (unit-tested without a subprocess); the spawn/stop/rm/info methods
shell out to the real ``claude`` CLI (integration-tested against a real trivial
background session that is always cleaned up).
"""

from __future__ import annotations
import json
import time
import uuid
import shutil
import logging
import subprocess
from typing import Optional, Sequence
from pathlib import Path
from dataclasses import dataclass


logger = logging.getLogger(__name__)


# The CLI prints rows separated by a middle dot (U+00B7), e.g.
#   "backgrounded · 197d4014 · my-manager"
_SPAWN_SEP = "·"
_SPAWN_MARKER = "backgrounded"

# Default permission mode for managers: they run autonomously inside the user's
# approved intent (matches WorkerSession in claude_brain.py). Override per spawn.
DEFAULT_PERMISSION_MODE = "bypassPermissions"

# Default timeouts (seconds). Spawn returns immediately but allow headroom for
# the daemon to start; stop/rm/list are quick.
DEFAULT_SPAWN_TIMEOUT = 120.0
DEFAULT_CONTROL_TIMEOUT = 60.0


class FleetManagerError(RuntimeError):
    """Raised when spawning or controlling a background manager fails."""


# ---------------------------------------------------------------------------
# Pure helpers (no subprocess) — unit-testable
# ---------------------------------------------------------------------------


def _opt_str(value: object) -> Optional[str]:
    """Coerce a JSON value to ``Optional[str]`` (``None`` stays ``None``)."""
    return None if value is None else str(value)


def _opt_int(value: object) -> Optional[int]:
    """Return *value* if it is an ``int`` (and not ``bool``), else ``None``."""
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def short_id_for(session_id: str) -> str:
    """Return the short id Claude Code uses for *session_id* (its first segment)."""
    return session_id.split("-", 1)[0]


def build_spawn_argv(
    task: str,
    *,
    name: str,
    session_id: str,
    permission_mode: str = DEFAULT_PERMISSION_MODE,
    model: Optional[str] = None,
    mcp_configs: Sequence[str] = (),
    worktree: Optional[str | bool] = None,
    extra_args: Sequence[str] = (),
    claude_bin: str = "claude",
) -> list[str]:
    """Build the ``claude --bg …`` argv for spawning one manager.

    ``worktree`` may be ``True`` (``-w`` with an auto name), a string (``-w
    <name>``), or ``None``/``False`` (omit — background sessions still
    auto-isolate into ``.claude/worktrees/`` on first edit). ``mcp_configs`` and
    ``extra_args`` are appended verbatim. The ``task`` prompt is always last.
    """
    if not task or not task.strip():
        raise FleetManagerError("manager task/prompt must not be empty")
    if not name or not name.strip():
        raise FleetManagerError("manager name must not be empty")
    if not session_id or not session_id.strip():
        raise FleetManagerError("manager session_id must not be empty")

    argv: list[str] = [
        claude_bin,
        "--bg",
        "--name",
        name,
        "--session-id",
        session_id,
        "--permission-mode",
        permission_mode,
    ]
    if model:
        argv += ["--model", model]
    for cfg in mcp_configs:
        argv += ["--mcp-config", cfg]
    if worktree is True:
        argv += ["-w"]
    elif isinstance(worktree, str) and worktree:
        argv += ["-w", worktree]
    argv += list(extra_args)
    argv += [task]
    return argv


def parse_spawn_output(stdout: str) -> tuple[str, Optional[str]]:
    """Parse ``claude --bg`` stdout into ``(short_id, name)``.

    The marker line looks like ``backgrounded · <shortID> · <name>``. ``name``
    may be absent in some outputs. Raises :class:`FleetManagerError` if no
    marker line is found.
    """
    for raw_line in stdout.splitlines():
        line = raw_line.strip()
        if not line.lower().startswith(_SPAWN_MARKER):
            continue
        parts = [p.strip() for p in line.split(_SPAWN_SEP)]
        # parts == ["backgrounded", "<shortID>", "<name>"]
        if len(parts) >= 2 and parts[1]:
            short_id = parts[1]
            name = parts[2] if len(parts) >= 3 and parts[2] else None
            return short_id, name
    raise FleetManagerError(
        f"could not find the 'backgrounded' confirmation line in claude output:\n{stdout}"
    )


@dataclass
class AgentInfo:
    """One row from ``claude agents --json`` (background or interactive)."""

    session_id: str
    id: Optional[str] = None          # short id (background sessions only)
    name: Optional[str] = None
    cwd: Optional[str] = None
    kind: Optional[str] = None        # "background" | "interactive"
    state: Optional[str] = None       # working | blocked | done | failed | stopped
    status: Optional[str] = None      # busy | idle (while alive)
    pid: Optional[int] = None
    started_at: Optional[int] = None
    waiting_for: Optional[str] = None  # gate signal, e.g. "permission prompt"

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "AgentInfo":
        """Build from one ``claude agents --json`` array element."""
        return cls(
            session_id=str(data.get("sessionId", "")),
            id=_opt_str(data.get("id")),
            name=_opt_str(data.get("name")),
            cwd=_opt_str(data.get("cwd")),
            kind=_opt_str(data.get("kind")),
            state=_opt_str(data.get("state")),
            status=_opt_str(data.get("status")),
            pid=_opt_int(data.get("pid")),
            started_at=_opt_int(data.get("startedAt")),
            waiting_for=_opt_str(data.get("waitingFor")),
        )

    @property
    def is_background(self) -> bool:
        """True if this is a ``--bg`` session (vs an interactive terminal)."""
        return self.kind == "background"


def parse_agents_json(text: str) -> list[AgentInfo]:
    """Parse the JSON array printed by ``claude agents --json``."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise FleetManagerError(f"could not parse `claude agents --json` output: {exc}") from exc
    if not isinstance(data, list):
        raise FleetManagerError("`claude agents --json` did not return a JSON array")
    return [AgentInfo.from_dict(item) for item in data if isinstance(item, dict)]


# ---------------------------------------------------------------------------
# CLI runner (subprocess) — integration surface
# ---------------------------------------------------------------------------


def resolve_claude_bin(claude_bin: str = "claude") -> str:
    """Resolve *claude_bin* to an absolute path, or return it unchanged.

    On Windows ``claude`` is ``claude.exe``; ``shutil.which`` finds it so the
    subprocess never needs ``shell=True``.
    """
    return shutil.which(claude_bin) or claude_bin


def _run_claude(
    args: Sequence[str],
    *,
    claude_bin: str = "claude",
    cwd: Optional[str | Path] = None,
    timeout: float = DEFAULT_CONTROL_TIMEOUT,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run ``claude <args>`` capturing text output. Raise on failure if *check*."""
    binary = resolve_claude_bin(claude_bin)
    argv = [binary, *args]
    logger.debug("running: %s (cwd=%s)", argv, cwd)
    try:
        proc = subprocess.run(
            argv,
            cwd=str(cwd) if cwd is not None else None,
            capture_output=True,
            text=True,
            # The CLI prints UTF-8 (middle-dot separators, box drawing). Force
            # UTF-8 decoding so it isn't mangled by the Windows locale codepage.
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise FleetManagerError(
            f"claude CLI not found ({binary!r}); is Claude Code installed and on PATH?"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise FleetManagerError(f"`claude {' '.join(args)}` timed out after {timeout}s") from exc
    if check and proc.returncode != 0:
        raise FleetManagerError(
            f"`claude {' '.join(args)}` failed (exit {proc.returncode}):\n"
            f"{proc.stderr.strip() or proc.stdout.strip()}"
        )
    return proc


def list_agents(
    *,
    cwd: Optional[str | Path] = None,
    include_all: bool = False,
    claude_bin: str = "claude",
    timeout: float = DEFAULT_CONTROL_TIMEOUT,
) -> list[AgentInfo]:
    """Return parsed rows from ``claude agents --json``.

    ``include_all`` adds completed sessions (``--all``); ``cwd`` filters to
    sessions started under that path (``--cwd``). Note: ``--cwd`` is passed to
    the CLI as a filter, NOT used as the process working directory.
    """
    args = ["agents", "--json"]
    if include_all:
        args.append("--all")
    if cwd is not None:
        args += ["--cwd", str(cwd)]
    proc = _run_claude(args, claude_bin=claude_bin, timeout=timeout)
    return parse_agents_json(proc.stdout)


@dataclass
class FleetManager:
    """A single Claude Code background manager session.

    Construct via :meth:`spawn` (the normal path) or directly from a known
    ``session_id`` (e.g. when reconnecting in U3). Holds the identifiers needed
    to observe and control the session; ``stop``/``rm`` map to the CLI commands.
    """

    session_id: str
    id: str                      # short id (sessionId's first segment)
    name: str
    cwd: Optional[str] = None
    task: Optional[str] = None
    claude_bin: str = "claude"

    # ---- construction -----------------------------------------------------

    @classmethod
    def spawn(
        cls,
        task: str,
        *,
        name: str,
        cwd: Optional[str | Path] = None,
        session_id: Optional[str] = None,
        permission_mode: str = DEFAULT_PERMISSION_MODE,
        model: Optional[str] = None,
        mcp_configs: Sequence[str] = (),
        worktree: Optional[str | bool] = None,
        extra_args: Sequence[str] = (),
        claude_bin: str = "claude",
        timeout: float = DEFAULT_SPAWN_TIMEOUT,
        resolve_retries: int = 10,
        resolve_delay: float = 0.3,
    ) -> "FleetManager":
        """Spawn a background manager and return a handle to it.

        The *short id* parsed from the spawn output is authoritative (it is what
        ``claude stop``/``rm`` accept). The full ``sessionId`` is then resolved
        from ``claude agents --json`` by matching that short id — NOT assumed
        from the requested ``--session-id``, because ``claude --bg`` does not
        always honor a pre-assigned id (verified on 2.1.195/Windows: it mints a
        fresh UUID). A ``session_id`` is still passed through for setups that do
        honor it. Raises :class:`FleetManagerError` on spawn or resolve failure.
        """
        sid = session_id or str(uuid.uuid4())
        argv = build_spawn_argv(
            task,
            name=name,
            session_id=sid,
            permission_mode=permission_mode,
            model=model,
            mcp_configs=mcp_configs,
            worktree=worktree,
            extra_args=extra_args,
            claude_bin=claude_bin,
        )
        # argv[0] is claude_bin; _run_claude re-resolves it, so pass the rest.
        proc = _run_claude(
            argv[1:], claude_bin=claude_bin, cwd=cwd, timeout=timeout, check=True
        )
        short_id, parsed_name = parse_spawn_output(proc.stdout)

        # Resolve the real sessionId from the roster (short, retried — the row
        # is registered by the time spawn prints, but allow for tiny lag).
        resolved = cls._resolve_by_short_id(
            short_id, claude_bin=claude_bin, retries=resolve_retries, delay=resolve_delay
        )
        if resolved is None:
            raise FleetManagerError(
                f"spawned manager '{name}' (id={short_id}) did not appear in "
                "`claude agents --json`; cannot resolve its sessionId"
            )
        logger.info(
            "spawned background manager '%s' (id=%s session=%s)",
            name, short_id, resolved.session_id,
        )
        return cls(
            session_id=resolved.session_id,
            id=short_id,
            name=resolved.name or parsed_name or name,
            cwd=resolved.cwd or (str(cwd) if cwd is not None else None),
            task=task,
            claude_bin=claude_bin,
        )

    @classmethod
    def from_agent_info(
        cls,
        agent: AgentInfo,
        *,
        task: Optional[str] = None,
        claude_bin: str = "claude",
    ) -> "FleetManager":
        """Build a handle for an EXISTING background session from a roster row.

        This is the reconnect path (U3 ``SessionManager``): after an app restart
        the live session is rediscovered in ``claude agents --json`` and wrapped
        back into a :class:`FleetManager` so it can be observed/controlled again.
        Requires a background row (one with a short ``id``); raises otherwise,
        since interactive sessions are not fleet managers.
        """
        if agent.id is None:
            raise FleetManagerError(
                "cannot reconnect a FleetManager from a row without a short id "
                f"(session {agent.session_id!r} is not a background session)"
            )
        return cls(
            session_id=agent.session_id,
            id=agent.id,
            name=agent.name or agent.id,
            cwd=agent.cwd,
            task=task,
            claude_bin=claude_bin,
        )

    @staticmethod
    def _resolve_by_short_id(
        short_id: str,
        *,
        claude_bin: str = "claude",
        retries: int = 10,
        delay: float = 0.3,
    ) -> Optional[AgentInfo]:
        """Find the just-spawned background row by its short id, with light retry."""
        for attempt in range(max(1, retries)):
            for agent in list_agents(include_all=True, claude_bin=claude_bin):
                if agent.id == short_id:
                    return agent
            if attempt < retries - 1:
                time.sleep(delay)
        return None

    # ---- observation ------------------------------------------------------

    def info(
        self, *, include_all: bool = True, timeout: float = DEFAULT_CONTROL_TIMEOUT
    ) -> Optional[AgentInfo]:
        """Return this manager's live :class:`AgentInfo`, or ``None`` if gone.

        Matches on ``session_id`` (stable) and falls back to the short ``id``.
        """
        for agent in list_agents(
            include_all=include_all, claude_bin=self.claude_bin, timeout=timeout
        ):
            if agent.session_id == self.session_id or (agent.id and agent.id == self.id):
                return agent
        return None

    def is_running(self, *, timeout: float = DEFAULT_CONTROL_TIMEOUT) -> bool:
        """Return True if this manager appears as an active background session."""
        agent = self.info(include_all=False, timeout=timeout)
        return agent is not None and agent.is_background

    # ---- control ----------------------------------------------------------

    def stop(
        self, *, check: bool = True, timeout: float = DEFAULT_CONTROL_TIMEOUT
    ) -> subprocess.CompletedProcess[str]:
        """Stop the session (``claude stop <id>``). Conversation/state persist."""
        return _run_claude(
            ["stop", self.id], claude_bin=self.claude_bin, timeout=timeout, check=check
        )

    def rm(
        self, *, check: bool = False, timeout: float = DEFAULT_CONTROL_TIMEOUT
    ) -> subprocess.CompletedProcess[str]:
        """Remove the session from the roster (``claude rm <id>``).

        Defaults to ``check=False`` so removing an already-gone session is a
        no-op rather than an error (idempotent teardown).
        """
        return _run_claude(
            ["rm", self.id], claude_bin=self.claude_bin, timeout=timeout, check=check
        )

    def stop_and_remove(self, *, timeout: float = DEFAULT_CONTROL_TIMEOUT) -> None:
        """Best-effort teardown: stop then remove, tolerating an already-gone session."""
        try:
            self.stop(check=False, timeout=timeout)
        finally:
            self.rm(check=False, timeout=timeout)
