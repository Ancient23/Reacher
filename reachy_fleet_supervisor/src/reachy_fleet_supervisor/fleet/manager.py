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
import os
import json
import time
import uuid
import shutil
import logging
import subprocess
from typing import Literal, Callable, Optional, Sequence
from pathlib import Path
from dataclasses import field, dataclass


logger = logging.getLogger(__name__)


# The CLI prints rows separated by a middle dot (U+00B7), e.g.
#   "backgrounded · 197d4014 · my-manager"
_SPAWN_SEP = "·"
_SPAWN_MARKER = "backgrounded"

# Valid ``--permission-mode`` values, verified against Claude Code 2.1.196
# (``claude --help`` choices: acceptEdits, auto, bypassPermissions, default,
# dontAsk, plan). We validate against this set so an invalid mode fails loudly
# in-process rather than being rejected mid-spawn by the CLI.
VALID_PERMISSION_MODES = frozenset(
    {"acceptEdits", "auto", "bypassPermissions", "default", "dontAsk", "plan"}
)

# Default permission mode for managers. A fleet manager is a TRUSTED, LOCAL
# ``claude --bg`` agent with NO TTY: it cannot answer an interactive permission
# prompt, so any mode that still prompts (``default``/``acceptEdits`` for
# bash/commands) DEADLOCKS it forever (the reported U10 bug — ``state: blocked``,
# ``waitingFor: permission prompt``). ``bypassPermissions`` runs fully
# non-interactively, matching the project's premise: managers drive drive-loops
# autonomously and escalate genuine blockers as a HUMAN_GATE in FleetState, not
# via a dead TUI prompt. SECURITY: ``bypassPermissions`` SKIPS ALL permission
# checks for that session (file writes, shell, network) — only point a manager
# at a repo you trust it to act in unattended. Override per spawn / per project /
# via the ``FLEET_PERMISSION_MODE`` env var (see :func:`resolve_permission_mode`).
DEFAULT_PERMISSION_MODE = "bypassPermissions"

# Env var that overrides the built-in default for the live/voice spawn path
# (mirrors claude_brain.py's WorkerSession knob). A per-spawn argument still wins.
PERMISSION_MODE_ENV = "FLEET_PERMISSION_MODE"

# Per-manager run mode (decision #19). How a manager session is HOSTED, which is
# orthogonal to the work pattern it runs:
#   "background"     -> ``claude --bg``: supervisor-durable, survives sleep/restart,
#                       observed via ``claude agents --json``, steered by Reachy's
#                       voice. The fleet backbone and the default — the whole of Phase 2.
#   "remote-control" -> ``claude --remote-control``: a claude.ai/mobile-STEERABLE
#                       session. Per decision #16 it does NOT compose with ``--bg``
#                       (passing both, ``--bg`` silently wins) and needs FULL claude.ai
#                       OAuth, so it is a SEPARATE, must-stay-alive interactive process
#                       and is NOT roster-visible the way a ``--bg`` manager is.
RunMode = Literal["background", "remote-control"]
VALID_RUN_MODES: frozenset[str] = frozenset({"background", "remote-control"})
DEFAULT_RUN_MODE: RunMode = "background"

# Env var that overrides the built-in default run mode (a per-spawn arg still wins).
RUN_MODE_ENV = "FLEET_RUN_MODE"

# ``claude remote-control`` SERVER-mode --spawn choices (verified vs
# ``claude remote-control --help`` on 2.1.196). The server is one long-lived host
# that serves N claude.ai/mobile-steered sessions (decision #19).
VALID_RC_SPAWN_MODES: frozenset[str] = frozenset({"same-dir", "worktree", "session"})
DEFAULT_RC_SPAWN_MODE = "worktree"

# Default timeouts (seconds). Spawn returns immediately but allow headroom for
# the daemon to start; stop/rm/list are quick. Steering (``--resume … -p``) runs
# a real agent turn, so it gets a much larger budget.
DEFAULT_SPAWN_TIMEOUT = 120.0
DEFAULT_CONTROL_TIMEOUT = 60.0
DEFAULT_STEER_TIMEOUT = 600.0


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


def validate_permission_mode(mode: str) -> str:
    """Return *mode* if it is a valid ``--permission-mode`` value, else raise.

    Guards against typos / invented values reaching the CLI (where a bad value is
    a hard spawn failure). Validated against :data:`VALID_PERMISSION_MODES`.
    """
    cleaned = (mode or "").strip()
    if cleaned not in VALID_PERMISSION_MODES:
        raise FleetManagerError(
            f"invalid permission mode {mode!r}; valid values are "
            f"{sorted(VALID_PERMISSION_MODES)}"
        )
    return cleaned


def resolve_permission_mode(override: Optional[str] = None) -> str:
    """Resolve the effective permission mode for a spawn.

    Precedence (highest first): an explicit *override* argument, then the
    ``FLEET_PERMISSION_MODE`` environment variable, then
    :data:`DEFAULT_PERMISSION_MODE`. The result is validated. This is the single
    configurable knob the voice ``spawn_manager`` tool and the runtime use so a
    trusted local manager runs non-interactively by default but the user can
    change it without editing code.
    """
    candidate = override if (override and override.strip()) else os.getenv(PERMISSION_MODE_ENV)
    candidate = candidate if (candidate and candidate.strip()) else DEFAULT_PERMISSION_MODE
    return validate_permission_mode(candidate)


def validate_run_mode(mode: str) -> str:
    """Return *mode* if it is a valid :data:`RunMode`, else raise.

    Guards against typos / invented run modes before they reach a spawn path.
    """
    cleaned = (mode or "").strip()
    if cleaned not in VALID_RUN_MODES:
        raise FleetManagerError(
            f"invalid run mode {mode!r}; valid values are {sorted(VALID_RUN_MODES)}"
        )
    return cleaned


def resolve_run_mode(override: Optional[str] = None) -> str:
    """Resolve the effective run mode for a spawn.

    Precedence (highest first): explicit *override*, then the ``FLEET_RUN_MODE``
    environment variable, then :data:`DEFAULT_RUN_MODE` (``background``). The
    result is validated. This is the single knob the voice ``spawn_manager`` tool
    and the CLI use so the default is the durable background backbone but the user
    can ask for a steerable remote-control session per task.
    """
    candidate = override if (override and override.strip()) else os.getenv(RUN_MODE_ENV)
    candidate = candidate if (candidate and candidate.strip()) else DEFAULT_RUN_MODE
    return validate_run_mode(candidate)


def build_spawn_argv(
    task: str,
    *,
    name: str,
    session_id: str,
    run_mode: str = DEFAULT_RUN_MODE,
    permission_mode: str = DEFAULT_PERMISSION_MODE,
    model: Optional[str] = None,
    mcp_configs: Sequence[str] = (),
    worktree: Optional[str | bool] = None,
    remote_control_name_prefix: Optional[str] = None,
    extra_args: Sequence[str] = (),
    claude_bin: str = "claude",
) -> list[str]:
    """Build the ``claude`` argv for spawning one manager in the given *run_mode*.

    Two hosting modes (decision #19), each verified vs ``claude --help`` on Claude
    Code 2.1.196 — these are NOT combinable on one session:

    - ``run_mode="background"`` -> ``claude --bg --name <name> --session-id <id>
      --permission-mode <mode> … <task>`` (the durable, roster-visible backbone).
    - ``run_mode="remote-control"`` -> ``claude --remote-control <name>
      --session-id <id> --permission-mode <mode> … <task>``: an interactive,
      claude.ai/mobile-steerable session. NO ``--bg`` (they don't compose). The
      manager *name* is passed as the Remote Control session name shown in
      claude.ai; ``remote_control_name_prefix`` maps to
      ``--remote-control-session-name-prefix``.

    ``worktree`` may be ``True`` (``-w`` with an auto name), a string (``-w
    <name>``), or ``None``/``False`` (omit). ``mcp_configs`` and ``extra_args``
    are appended verbatim. The ``task`` prompt is always last.
    """
    if not task or not task.strip():
        raise FleetManagerError("manager task/prompt must not be empty")
    if not name or not name.strip():
        raise FleetManagerError("manager name must not be empty")
    if not session_id or not session_id.strip():
        raise FleetManagerError("manager session_id must not be empty")
    # Fail loudly here rather than letting the CLI reject an invalid value.
    run_mode = validate_run_mode(run_mode)
    permission_mode = validate_permission_mode(permission_mode)

    if run_mode == "remote-control":
        # Interactive Remote Control session (claude.ai/mobile-steerable). The
        # optional [name] for --remote-control becomes the steering session name.
        argv: list[str] = [
            claude_bin,
            "--remote-control",
            name,
            "--session-id",
            session_id,
            "--permission-mode",
            permission_mode,
        ]
        if remote_control_name_prefix:
            argv += ["--remote-control-session-name-prefix", remote_control_name_prefix]
    else:  # background — the durable, roster-visible backbone
        argv = [
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


def build_remote_control_server_argv(
    *,
    name: Optional[str] = None,
    spawn: str = DEFAULT_RC_SPAWN_MODE,
    capacity: Optional[int] = None,
    permission_mode: str = DEFAULT_PERMISSION_MODE,
    name_prefix: Optional[str] = None,
    extra_args: Sequence[str] = (),
    claude_bin: str = "claude",
) -> list[str]:
    """Assemble the Remote Control SERVER-mode host argv: ``claude remote-control …``.

    Server mode (decision #19) is a single, long-lived host that serves up to N
    claude.ai/mobile-steered sessions — it takes NO task/prompt (sessions are
    started from claude.ai). Flags verified vs ``claude remote-control --help`` on
    Claude Code 2.1.196: ``--spawn {same-dir,worktree,session}``, ``--capacity N``,
    ``--name``, ``--permission-mode``, ``--remote-control-session-name-prefix``.

    Like the interactive form it needs full claude.ai OAuth and is a separate,
    must-stay-alive process (kept alive by the launcher) — NOT roster-visible.
    """
    permission_mode = validate_permission_mode(permission_mode)
    cleaned_spawn = (spawn or "").strip()
    if cleaned_spawn not in VALID_RC_SPAWN_MODES:
        raise FleetManagerError(
            f"invalid remote-control --spawn mode {spawn!r}; valid values are "
            f"{sorted(VALID_RC_SPAWN_MODES)}"
        )
    if capacity is not None and capacity < 1:
        raise FleetManagerError(f"remote-control --capacity must be >= 1, got {capacity!r}")

    argv: list[str] = [claude_bin, "remote-control", "--spawn", cleaned_spawn]
    if capacity is not None:
        argv += ["--capacity", str(capacity)]
    if name:
        argv += ["--name", name]
    if name_prefix:
        argv += ["--remote-control-session-name-prefix", name_prefix]
    argv += ["--permission-mode", permission_mode]
    argv += list(extra_args)
    return argv


def build_steer_argv(
    session_id: str,
    message: str,
    *,
    permission_mode: str = DEFAULT_PERMISSION_MODE,
    model: Optional[str] = None,
    output_format: Optional[str] = None,
    extra_args: Sequence[str] = (),
    claude_bin: str = "claude",
) -> list[str]:
    """Assemble the verified NON-INTERACTIVE steering argv for a *background* manager.

    This resolves the decision #16 open item — "non-interactive *steering* of a
    running bg session from the shell." Of the surfaces Claude Code 2.1.196
    exposes for an existing background session:

    - ``claude attach <id>`` — INTERACTIVE: opens the session in this terminal
      (detach with Ctrl+Z). It needs a TTY, so it is unusable headlessly from the
      fleet (the dashboard / CLI / voice paths have no terminal to give it).
    - ``claude --resume <sessionId> -p "<message>"`` — NON-INTERACTIVE: resume the
      conversation by its full session id and print the reply. No TTY required.

    So the fleet steers a background manager with the SECOND form (this function).
    The *message* is appended to the manager's OWN durable conversation (the same
    ``sessionId``; we deliberately do NOT pass ``--fork-session``, which would
    branch a new session) and the manager continues from where it left off — which
    is exactly the state a manager sitting on a HUMAN_GATE, ``blocked`` or ``done``
    is in. We pass ``--permission-mode`` (default ``bypassPermissions``, matching
    the spawn default, decision #22) so the steering turn itself never deadlocks on
    a permission prompt.

    IMPORTANT (decision #16): the daemon REJECTS ``--resume … -p`` while the
    session is still a LIVE background agent. The caller must ``claude stop <id>``
    FIRST to hand the session back, then run this argv — that is exactly what
    :meth:`FleetManager.send_message` does. (Passing ``--fork-session`` here would
    sidestep the stop but branch a *copy* instead of steering in place.)

    NB ``--resume`` keys on the FULL ``sessionId`` (a UUID), NOT the short id.
    """
    if not session_id or not session_id.strip():
        raise FleetManagerError("cannot steer: session_id must not be empty")
    if not message or not message.strip():
        raise FleetManagerError("cannot steer: message must not be empty")
    permission_mode = validate_permission_mode(permission_mode)
    argv: list[str] = [
        claude_bin,
        "--resume",
        session_id,
        "--permission-mode",
        permission_mode,
    ]
    if model:
        argv += ["--model", model]
    if output_format:
        argv += ["--output-format", output_format]
    argv += list(extra_args)
    # The prompt flag + message go last so they are never swallowed by an option.
    argv += ["-p", message]
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


# A launcher starts a long-lived (interactive) process from an argv and returns a
# handle to it (a ``Popen``, or any object exposing ``poll``/``terminate``). It is
# injectable so the remote-control spawn path is software-testable WITHOUT a live
# claude.ai session (tests pass a fake that records the argv).
RemoteControlLauncher = Callable[..., "subprocess.Popen[str]"]


def _launch_detached(
    argv: Sequence[str],
    *,
    claude_bin: str = "claude",
    cwd: Optional[str | Path] = None,
) -> "subprocess.Popen[str]":
    """Start *argv* as a detached, non-blocking process and return the ``Popen``.

    Used for the Remote Control run mode: that session is INTERACTIVE and must
    stay alive (it talks to claude.ai), so unlike a ``--bg`` spawn we must NOT
    wait on it. We launch it in its own process group so it is not killed by a
    transient parent and can outlive the spawn call.
    """
    binary = resolve_claude_bin(claude_bin)
    full_argv = [binary, *list(argv)[1:]] if argv and argv[0] == claude_bin else [binary, *argv]
    cwd_str = str(cwd) if cwd is not None else None
    logger.info("launching remote-control session: %s (cwd=%s)", full_argv, cwd)
    if os.name == "nt":
        # New process group + detached console so the RC session is independent
        # and survives the spawn call (it must stay alive to talk to claude.ai).
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP | getattr(
            subprocess, "DETACHED_PROCESS", 0
        )
        return subprocess.Popen(full_argv, cwd=cwd_str, text=True, creationflags=creationflags)
    return subprocess.Popen(  # pragma: no cover - posix path not exercised on Windows host
        full_argv, cwd=cwd_str, text=True, start_new_session=True
    )


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
    run_mode: str = DEFAULT_RUN_MODE
    # For a remote-control manager: the launched, must-stay-alive interactive
    # process. ``None`` for background managers (those live in the daemon roster
    # and are controlled via ``claude stop``/``rm``). Excluded from eq/repr.
    process: Optional["subprocess.Popen[str]"] = field(
        default=None, compare=False, repr=False
    )

    # ---- construction -----------------------------------------------------

    @classmethod
    def spawn(
        cls,
        task: str,
        *,
        name: str,
        cwd: Optional[str | Path] = None,
        session_id: Optional[str] = None,
        run_mode: str = DEFAULT_RUN_MODE,
        permission_mode: str = DEFAULT_PERMISSION_MODE,
        model: Optional[str] = None,
        mcp_configs: Sequence[str] = (),
        worktree: Optional[str | bool] = None,
        remote_control_name_prefix: Optional[str] = None,
        extra_args: Sequence[str] = (),
        claude_bin: str = "claude",
        timeout: float = DEFAULT_SPAWN_TIMEOUT,
        resolve_retries: int = 10,
        resolve_delay: float = 0.3,
        launcher: Optional[RemoteControlLauncher] = None,
    ) -> "FleetManager":
        """Spawn a manager in the given *run_mode* and return a handle to it.

        ``run_mode="background"`` (default) shells out to ``claude --bg`` and
        resolves the durable session from the roster (the Phase-2 backbone).
        ``run_mode="remote-control"`` dispatches to :meth:`spawn_remote_control`
        (a claude.ai/mobile-steerable interactive session). Raises
        :class:`FleetManagerError` on spawn or resolve failure.

        For background: the *short id* parsed from the spawn output is
        authoritative (it is what ``claude stop``/``rm`` accept). The full
        ``sessionId`` is then resolved from ``claude agents --json`` by matching
        that short id — NOT assumed from the requested ``--session-id``, because
        ``claude --bg`` does not always honor a pre-assigned id (verified on
        2.1.195/Windows: it mints a fresh UUID).
        """
        run_mode = validate_run_mode(run_mode)
        if run_mode == "remote-control":
            return cls.spawn_remote_control(
                task,
                name=name,
                cwd=cwd,
                session_id=session_id,
                permission_mode=permission_mode,
                model=model,
                mcp_configs=mcp_configs,
                worktree=worktree,
                remote_control_name_prefix=remote_control_name_prefix,
                extra_args=extra_args,
                claude_bin=claude_bin,
                launcher=launcher,
            )

        sid = session_id or str(uuid.uuid4())
        argv = build_spawn_argv(
            task,
            name=name,
            session_id=sid,
            run_mode="background",
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
            run_mode="background",
        )

    @classmethod
    def spawn_remote_control(
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
        remote_control_name_prefix: Optional[str] = None,
        extra_args: Sequence[str] = (),
        claude_bin: str = "claude",
        launcher: Optional[RemoteControlLauncher] = None,
    ) -> "FleetManager":
        """Launch a Remote Control (claude.ai/mobile-steerable) manager session.

        Remote Control does NOT compose with ``--bg`` (decision #16, verified):
        it is a separate, must-stay-alive INTERACTIVE process and needs FULL
        claude.ai OAuth. So unlike a background manager it is NOT registered in
        ``claude agents --json`` and cannot be resolved/observed there — the
        live connect is a HUMAN_GATE. We assemble the verified ``claude
        --remote-control <name> …`` argv (:func:`build_spawn_argv`) and start it
        as a detached, non-blocking process via *launcher* (default
        :func:`_launch_detached`; injectable so the command assembly is testable
        without a live session). The returned handle records
        ``run_mode="remote-control"`` and keeps the launched process so it can be
        torn down. The ``session_id`` we pass is the handle's id (no roster to
        resolve a minted one from).
        """
        sid = session_id or str(uuid.uuid4())
        argv = build_spawn_argv(
            task,
            name=name,
            session_id=sid,
            run_mode="remote-control",
            permission_mode=permission_mode,
            model=model,
            mcp_configs=mcp_configs,
            worktree=worktree,
            remote_control_name_prefix=remote_control_name_prefix,
            extra_args=extra_args,
            claude_bin=claude_bin,
        )
        launch = launcher or _launch_detached
        process = launch(argv, claude_bin=claude_bin, cwd=cwd)
        logger.info(
            "launched remote-control manager '%s' (session=%s) — steer it from claude.ai",
            name, sid,
        )
        return cls(
            session_id=sid,
            id=short_id_for(sid),
            name=name,
            cwd=str(cwd) if cwd is not None else None,
            task=task,
            claude_bin=claude_bin,
            run_mode="remote-control",
            process=process,
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

    @property
    def is_remote_control(self) -> bool:
        """True if this is a ``--remote-control`` session (not a ``--bg`` agent)."""
        return self.run_mode == "remote-control"

    def info(
        self, *, include_all: bool = True, timeout: float = DEFAULT_CONTROL_TIMEOUT
    ) -> Optional[AgentInfo]:
        """Return this manager's live :class:`AgentInfo`, or ``None`` if gone.

        Matches on ``session_id`` (stable) and falls back to the short ``id``.
        Remote-control managers are never in the ``claude agents --json`` roster,
        so this always returns ``None`` for them (use :meth:`is_running`).
        """
        if self.is_remote_control:
            return None
        for agent in list_agents(
            include_all=include_all, claude_bin=self.claude_bin, timeout=timeout
        ):
            if agent.session_id == self.session_id or (agent.id and agent.id == self.id):
                return agent
        return None

    def is_running(self, *, timeout: float = DEFAULT_CONTROL_TIMEOUT) -> bool:
        """Return True if this manager is alive.

        Background: it appears as an active background session in the roster.
        Remote-control: its launched, must-stay-alive process has not exited.
        """
        if self.is_remote_control:
            return self.process is not None and self.process.poll() is None
        agent = self.info(include_all=False, timeout=timeout)
        return agent is not None and agent.is_background

    # ---- control ----------------------------------------------------------

    def terminate_process(self, *, timeout: float = DEFAULT_CONTROL_TIMEOUT) -> None:
        """Terminate a remote-control session's launched process (best-effort).

        Background managers have no attached process — this is a no-op for them
        (they are stopped via :meth:`stop`/``claude stop``).
        """
        proc = self.process
        if proc is None:
            return
        try:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    proc.kill()
        except Exception:  # noqa: BLE001 — teardown must never raise
            logger.debug("terminate of remote-control process failed", exc_info=True)

    def stop(
        self, *, check: bool = True, timeout: float = DEFAULT_CONTROL_TIMEOUT
    ) -> Optional[subprocess.CompletedProcess[str]]:
        """Stop the session. Conversation/state persist.

        Background: ``claude stop <id>``. Remote-control: terminate the launched
        interactive process (there is no ``claude stop`` for an RC session) and
        return ``None``.
        """
        if self.is_remote_control:
            self.terminate_process(timeout=timeout)
            return None
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
        """Best-effort teardown: stop then remove, tolerating an already-gone session.

        For a remote-control manager there is no roster row to ``claude rm``;
        terminating its process (via :meth:`stop`) is the whole teardown.
        """
        if self.is_remote_control:
            self.stop(check=False, timeout=timeout)
            return
        try:
            self.stop(check=False, timeout=timeout)
        finally:
            self.rm(check=False, timeout=timeout)

    # ---- interactive steering (U12) --------------------------------------

    def send_message(
        self,
        message: str,
        *,
        permission_mode: str = DEFAULT_PERMISSION_MODE,
        model: Optional[str] = None,
        timeout: float = DEFAULT_STEER_TIMEOUT,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        """Steer a BACKGROUND manager IN PLACE: inject *message* into its conversation.

        Implements the decision #16 steering path. The SAME call powers every U12
        "talk to a manager" control — they differ only in what you say:

        - **send a message** — an ad-hoc instruction or question;
        - **approve / answer a gate** — the reply that unblocks a manager waiting on
          a HUMAN_GATE (e.g. "yes, go ahead" / "skip it");
        - **reassign** — a new task ("stop that and work on X instead").

        Mechanism (verified, Claude Code 2.1.196 — decision #16): the daemon REFUSES
        ``claude --resume <id> -p`` while the session is a LIVE background agent
        ("Session … is currently running as a background agent … add --fork-session
        to branch off a copy"). To steer the SAME conversation in place we therefore
        ``claude stop <id>`` FIRST — that hands the session back from the daemon —
        and only THEN run ``claude --resume <sessionId> -p <message>``
        (:func:`build_steer_argv`), which continues the manager's own durable
        conversation and prints the reply. (The non-disruptive alternative,
        ``--resume --fork-session -p``, works WITHOUT stopping but branches a *copy*,
        so it is not an in-place steer; see :func:`build_steer_argv`.)

        Returns the completed process (its ``stdout`` is the manager's reply).
        Remote-control managers are steered from claude.ai / the Claude mobile app,
        NOT the fleet shell (decision #16/#19), so this raises for them.
        """
        if self.is_remote_control:
            raise FleetManagerError(
                f"manager '{self.name}' is run_mode=remote-control; steer it from "
                "claude.ai or the Claude mobile app, not the fleet shell"
            )
        # Release the session from the daemon first: a LIVE bg agent rejects
        # `--resume … -p` (decision #16). `claude stop` keeps the conversation, so
        # the subsequent resume continues it in place rather than forking a copy.
        self.stop(check=False, timeout=timeout)
        argv = build_steer_argv(
            self.session_id,
            message,
            permission_mode=permission_mode,
            model=model,
            claude_bin=self.claude_bin,
        )
        # argv[0] is claude_bin; _run_claude re-resolves it, so pass the rest.
        return _run_claude(
            argv[1:], claude_bin=self.claude_bin, cwd=self.cwd, timeout=timeout, check=check
        )

    def pause(
        self, *, check: bool = True, timeout: float = DEFAULT_CONTROL_TIMEOUT
    ) -> Optional[subprocess.CompletedProcess[str]]:
        """Pause the manager — semantic alias of :meth:`stop`.

        ``claude stop`` halts a background session but KEEPS its conversation, so
        it can be resumed later with :meth:`resume`. For a remote-control manager
        this terminates its process (same as :meth:`stop`).
        """
        return self.stop(check=check, timeout=timeout)

    def resume(
        self, *, check: bool = True, timeout: float = DEFAULT_CONTROL_TIMEOUT
    ) -> subprocess.CompletedProcess[str]:
        """Resume a paused/exited BACKGROUND manager (``claude respawn <id>``).

        ``claude respawn`` restarts the background session (picking up where it left
        off and on the current binary). Remote-control managers are not in the
        roster, so there is no ``respawn`` for them — relaunch is out of scope here.
        """
        if self.is_remote_control:
            raise FleetManagerError(
                f"manager '{self.name}' is run_mode=remote-control and has no roster "
                "row to respawn; relaunch it from claude.ai or re-spawn it"
            )
        return _run_claude(
            ["respawn", self.id], claude_bin=self.claude_bin, timeout=timeout, check=check
        )
