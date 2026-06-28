"""Status convention — managers emit, the fleet reads (U5, decision #21).

"How's X doing?" must be answered by **reading** the fleet, never by interrupting
the agent (which would cost an agent turn and would behave differently in the two
run modes). So every manager **emits its own status to a known place once per
iteration**, and :class:`~reachy_fleet_supervisor.fleet.state.FleetState` merges
that emitted status alongside the ``claude agents --json`` roster row it already
polls. Reachy, the dashboard and the CLI all read the merged picture.

This module defines:

- :class:`ManagerStatus` — the small, JSON-serializable record a manager writes
  (its drive-loop report: a sentinel *state*, a one-line *summary*, the current
  unit, and any HUMAN_GATE *gate* text). Frozen + value-comparable so it feeds
  FleetState's "notify only on real change" logic; ``updated_at`` is excluded
  from equality so a no-op re-emit doesn't spuriously fire subscribers.
- The **known-place convention**: a fleet *status directory* containing one
  ``<name>/status.json`` per manager. A manager keys by its own *name* (the
  stable handle it was spawned with — it does not need to know the short id the
  daemon mints); :func:`write_status` writes it atomically.
- The read side: :func:`read_status` / :func:`read_status_for` (best-effort —
  missing or corrupt files yield ``None``, never raise, so a poll loop never dies
  on a half-written file) and :func:`read_statuses_for_agents`, which maps each
  roster row to its emitted status keyed the way FleetState matches managers.

The sentinel vocabulary mirrors the drive contract
(``CONTINUE|HUMAN_GATE|COMPLETE|FAILED``) plus a ``RUNNING`` mid-iteration state,
but ``state`` is a free string so other loop conventions fit too.
"""

from __future__ import annotations
import os
import json
import logging
from typing import Any, Mapping, Optional, Sequence
from pathlib import Path
from dataclasses import field, asdict, dataclass

from .manager import AgentInfo


logger = logging.getLogger(__name__)

# The on-disk filename a manager writes inside its per-manager status dir.
STATUS_FILENAME = "status.json"

# Env var a spawned manager can read to find the fleet status directory.
STATUS_DIR_ENV = "REACHY_FLEET_STATUS_DIR"

# Sentinel states (mirrors the /drive-* contract; RUNNING = mid-iteration).
SENTINEL_RUNNING = "RUNNING"
SENTINEL_CONTINUE = "CONTINUE"
SENTINEL_HUMAN_GATE = "HUMAN_GATE"
SENTINEL_COMPLETE = "COMPLETE"
SENTINEL_FAILED = "FAILED"

# States that should pull a human's (and the robot's) attention.
ATTENTION_SENTINELS = frozenset({SENTINEL_HUMAN_GATE, SENTINEL_FAILED, "BLOCKED"})


def default_status_dir() -> Path:
    """The fleet status directory: ``$REACHY_FLEET_STATUS_DIR`` or a home default.

    Defaults to ``~/.reachy_fleet/status`` so it survives app restarts and is
    shared by every manager and every renderer on the host.
    """
    env = os.environ.get(STATUS_DIR_ENV)
    if env and env.strip():
        return Path(env).expanduser()
    return Path.home() / ".reachy_fleet" / "status"


def status_path_for(name: str, base_dir: Optional[str | Path] = None) -> Path:
    """Path of the ``status.json`` a manager named *name* writes.

    ``<base_dir>/<name>/status.json`` — one directory per manager so a manager's
    journal or other artifacts can sit alongside its status. *base_dir* defaults
    to :func:`default_status_dir`.
    """
    if not name or not name.strip():
        raise ValueError("manager name must not be blank for a status path")
    root = Path(base_dir).expanduser() if base_dir is not None else default_status_dir()
    return root / name / STATUS_FILENAME


# ---------------------------------------------------------------------------
# ManagerStatus — the emitted record (frozen, value-comparable)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ManagerStatus:
    """A manager's self-reported status — its drive-loop report, on disk.

    Written by the manager once per iteration and merged into its
    :class:`~reachy_fleet_supervisor.fleet.state.ManagerSnapshot`. ``state`` is a
    sentinel (see the ``SENTINEL_*`` constants), ``summary`` is the one-line
    report, ``gate`` carries the copy-pasteable HUMAN_GATE steps/question when the
    loop is waiting on a human. ``updated_at`` is excluded from equality so an
    unchanged re-emit (only the timestamp moved) is not treated as a change.
    """

    state: str = SENTINEL_RUNNING
    summary: Optional[str] = None
    name: Optional[str] = None
    id: Optional[str] = None
    unit: Optional[str] = None
    phase: Optional[int] = None
    iteration: Optional[int] = None
    gate: Optional[str] = None
    extra: Mapping[str, Any] = field(default_factory=dict)
    updated_at: Optional[float] = field(default=None, compare=False)

    @property
    def normalized_state(self) -> str:
        """``state`` upper-cased and stripped (sentinels are case-insensitive)."""
        return (self.state or "").strip().upper()

    @property
    def needs_attention(self) -> bool:
        """True if this status is a HUMAN_GATE / FAILED / BLOCKED sentinel."""
        return self.normalized_state in ATTENTION_SENTINELS

    @property
    def is_terminal(self) -> bool:
        """True once the manager has finished its work (COMPLETE / FAILED)."""
        return self.normalized_state in (SENTINEL_COMPLETE, SENTINEL_FAILED)

    # ---- (de)serialization ------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready mapping (``extra`` is flattened to a plain ``dict``)."""
        data = asdict(self)
        data["extra"] = dict(self.extra)
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ManagerStatus":
        """Build from a parsed mapping, tolerating unknown/missing keys.

        Unknown keys are folded into ``extra`` rather than rejected, so a
        project's drive loop can record its own fields without breaking the read.
        """
        known = {f for f in cls.__dataclass_fields__ if f != "extra"}
        extra: dict[str, Any] = dict(data.get("extra") or {})
        kwargs: dict[str, Any] = {}
        for key, value in data.items():
            if key == "extra":
                continue
            if key in known:
                kwargs[key] = value
            else:
                extra[key] = value
        kwargs["extra"] = extra
        # state must be a non-empty string; fall back to RUNNING.
        if not kwargs.get("state"):
            kwargs["state"] = SENTINEL_RUNNING
        return cls(**kwargs)

    @classmethod
    def from_report_line(
        cls,
        line: str,
        *,
        sentinel_prefix: str = "ROADMAP_STATE",
        **fields: Any,
    ) -> "ManagerStatus":
        """Parse a drive-loop sentinel line (``<PREFIX>: STATE``) into a status.

        Recognizes e.g. ``ROADMAP_STATE: HUMAN_GATE``; the parsed state populates
        ``state`` and the raw line becomes the ``summary`` unless one is passed in
        ``fields``. Extra keyword fields (``unit``, ``gate``, …) are passed
        through. If no sentinel is found the whole line is the summary and the
        state defaults to RUNNING.
        """
        state = parse_sentinel(line, sentinel_prefix=sentinel_prefix)
        fields.setdefault("summary", line.strip() or None)
        return cls(state=state or SENTINEL_RUNNING, **fields)


def parse_sentinel(text: str, *, sentinel_prefix: str = "ROADMAP_STATE") -> Optional[str]:
    """Return the sentinel state from a ``<PREFIX>: STATE`` line, else ``None``.

    Scans *text* line by line (last match wins, as the sentinel is the final
    line of a drive-loop report) for ``<sentinel_prefix>: <STATE>`` and returns
    the upper-cased state token.
    """
    marker = f"{sentinel_prefix}:".lower()
    found: Optional[str] = None
    for raw in text.splitlines():
        line = raw.strip()
        if line.lower().startswith(marker):
            rest = line[len(marker):].strip()
            if rest:
                found = rest.split()[0].strip().upper()
    return found


# ---------------------------------------------------------------------------
# Write side (manager) — atomic
# ---------------------------------------------------------------------------


def write_status(
    status: ManagerStatus,
    *,
    base_dir: Optional[str | Path] = None,
    name: Optional[str] = None,
) -> Path:
    """Write *status* to its known place atomically; return the path written.

    The target is :func:`status_path_for` of *name* (defaulting to
    ``status.name``). The write is atomic (temp file in the same directory then
    ``os.replace``) so a concurrent reader never sees a half-written file. Raises
    ``ValueError`` if no name is available.
    """
    target_name = name or status.name
    if not target_name:
        raise ValueError("write_status needs a manager name (set status.name or pass name=)")
    path = status_path_for(target_name, base_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(status.to_dict(), indent=2, sort_keys=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(payload, encoding="utf-8")
    os.replace(tmp, path)
    return path


# ---------------------------------------------------------------------------
# Read side (fleet) — best-effort, never raises
# ---------------------------------------------------------------------------


def read_status(path: str | Path) -> Optional[ManagerStatus]:
    """Read one ``status.json`` → :class:`ManagerStatus`, or ``None``.

    Best-effort: a missing file, unreadable file, bad JSON, or non-object
    payload all yield ``None`` (logged at debug) rather than raising, so a
    polling loop is never killed by a transient/half-written status file.
    """
    p = Path(path)
    try:
        raw = p.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.debug("status file %s is not valid JSON; ignoring", p)
        return None
    if not isinstance(data, dict):
        logger.debug("status file %s is not a JSON object; ignoring", p)
        return None
    try:
        return ManagerStatus.from_dict(data)
    except (TypeError, ValueError):
        logger.debug("status file %s did not match ManagerStatus; ignoring", p)
        return None


def read_status_for(
    name: str, base_dir: Optional[str | Path] = None
) -> Optional[ManagerStatus]:
    """Read the status a manager named *name* emitted, or ``None`` if absent."""
    return read_status(status_path_for(name, base_dir))


def read_statuses_for_agents(
    agents: Sequence[AgentInfo],
    base_dir: Optional[str | Path] = None,
) -> dict[str, ManagerStatus]:
    """Map roster rows to their emitted status, keyed for FleetState matching.

    For each agent with a name, read ``<base_dir>/<name>/status.json``; the
    result is keyed by the agent's lookup key (short ``id`` falling back to
    ``session_id``) — the same key
    :meth:`~reachy_fleet_supervisor.fleet.state.FleetState.apply` uses — so the
    status lands on the right manager even though the manager wrote it under its
    *name*. Agents without a name, or with no status file, are simply omitted.
    """
    out: dict[str, ManagerStatus] = {}
    for agent in agents:
        if not agent.name:
            continue
        status = read_status_for(agent.name, base_dir)
        if status is not None:
            out[agent.id or agent.session_id] = status
    return out
