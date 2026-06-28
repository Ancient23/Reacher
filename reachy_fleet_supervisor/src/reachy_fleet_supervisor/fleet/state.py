"""FleetState — single source of truth + pub/sub poller (U4).

The fleet's design is **one source of truth, many renderers** (plan.md §3): the
robot body, the web dashboard and the headless CLI are all just *subscribers* to
a single :class:`FleetState`. Nothing polls a worker directly — a
:class:`FleetPoller` reads ``claude agents --json`` (and optionally
``claude logs <id>``) on an interval, feeds the result into :class:`FleetState`,
and the state notifies its subscribers whenever the picture changes (decision
#21: read status, never interrupt the agent).

Layering:

- :class:`ManagerSnapshot` — an immutable, value-comparable view of one manager
  (its ``claude agents --json`` row plus a recent-log ring buffer).
- :class:`FleetSnapshot` — an immutable view of the whole fleet at one instant.
- :class:`FleetState` — holds the current snapshot, merges per-manager transcript
  ring buffers across polls, and is the pub/sub hub (``subscribe`` / ``apply``).
  Pure and thread-safe; takes already-fetched data so it is trivially testable.
- :class:`FleetPoller` — the subprocess surface: a background thread that fetches
  agents (+ logs) and calls :meth:`FleetState.apply`. Sources are injectable so
  the poll loop itself is testable without a real ``claude`` CLI.

Built on U2/U3: rows come from :func:`list_agents`; identity (short id, name)
matches the :class:`~reachy_fleet_supervisor.fleet.manager.FleetManager` handles
the :class:`~reachy_fleet_supervisor.fleet.session_manager.SessionManager`
tracks.
"""

from __future__ import annotations
import time
import logging
import threading
from typing import Callable, Iterator, Optional, Sequence
from dataclasses import field, dataclass

from .manager import (
    DEFAULT_CONTROL_TIMEOUT,
    AgentInfo,
    FleetManagerError,
    _run_claude,
    list_agents,
)
from .status import (
    ManagerStatus,
    read_statuses_for_agents,
)


logger = logging.getLogger(__name__)

# States that should pull a human's (and the robot's) attention.
ATTENTION_STATES = frozenset({"blocked", "failed"})

# How many recent log lines to keep per manager by default.
DEFAULT_LOG_LINES = 200

# Default poll cadence (seconds). 2–4 managers on the Max pool; cheap CLI calls.
DEFAULT_POLL_INTERVAL = 2.0

# A subscriber is any callable that accepts the latest FleetSnapshot.
Subscriber = Callable[["FleetSnapshot"], None]
AgentPredicate = Callable[[AgentInfo], bool]


# ---------------------------------------------------------------------------
# Snapshots (immutable, value-comparable)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ManagerSnapshot:
    """Immutable view of one manager at a poll instant.

    Combines the ``claude agents --json`` row (state/status/waitingFor) with a
    recent-output ``transcript`` ring buffer (newest last). Frozen so equality is
    by value — :class:`FleetState` uses that to notify subscribers only on real
    change.
    """

    id: Optional[str]
    session_id: str
    name: Optional[str] = None
    cwd: Optional[str] = None
    kind: Optional[str] = None
    state: Optional[str] = None
    status: Optional[str] = None
    waiting_for: Optional[str] = None
    pid: Optional[int] = None
    started_at: Optional[int] = None
    transcript: tuple[str, ...] = ()
    report: Optional[ManagerStatus] = None

    @classmethod
    def from_agent_info(
        cls,
        agent: AgentInfo,
        *,
        transcript: Sequence[str] = (),
        report: Optional[ManagerStatus] = None,
    ) -> "ManagerSnapshot":
        """Build a snapshot from a roster row, an optional recent-log tail, and
        the manager's own emitted status (its drive-loop report, decision #21)."""
        return cls(
            id=agent.id,
            session_id=agent.session_id,
            name=agent.name,
            cwd=agent.cwd,
            kind=agent.kind,
            state=agent.state,
            status=agent.status,
            waiting_for=agent.waiting_for,
            pid=agent.pid,
            started_at=agent.started_at,
            transcript=tuple(transcript),
            report=report,
        )

    @property
    def key(self) -> str:
        """Stable lookup key: the short id if present, else the session id."""
        return self.id or self.session_id

    @property
    def is_background(self) -> bool:
        """True for ``--bg`` managers (vs interactive terminals)."""
        return self.kind == "background"

    @property
    def last_line(self) -> Optional[str]:
        """The most recent log line, or ``None`` if no transcript yet."""
        return self.transcript[-1] if self.transcript else None

    @property
    def headline(self) -> Optional[str]:
        """Best one-line summary: the manager's emitted report, else last log line.

        Renderers (body/dashboard/CLI) prefer the manager's own self-reported
        summary (decision #21) and fall back to the raw transcript tail.
        """
        if self.report is not None and self.report.summary:
            return self.report.summary
        return self.last_line

    @property
    def needs_attention(self) -> bool:
        """True if blocked/failed/waiting on a gate — from the roster row OR the
        manager's own emitted status (a HUMAN_GATE/FAILED report)."""
        if self.report is not None and self.report.needs_attention:
            return True
        return bool(self.waiting_for) or (self.state in ATTENTION_STATES)


@dataclass(frozen=True)
class FleetSnapshot:
    """Immutable view of the whole fleet at ``updated_at`` (epoch seconds)."""

    managers: tuple[ManagerSnapshot, ...] = ()
    updated_at: float = 0.0

    def __len__(self) -> int:
        return len(self.managers)

    def __iter__(self) -> Iterator[ManagerSnapshot]:
        return iter(self.managers)

    def get(self, key: str) -> Optional[ManagerSnapshot]:
        """Return the manager whose short id/session id OR name is *key*.

        Id (unique) is matched first; a name match returns the first such
        manager. Returns ``None`` if nothing matches.
        """
        for m in self.managers:
            if key in (m.id, m.session_id):
                return m
        for m in self.managers:
            if m.name == key:
                return m
        return None

    def ids(self) -> list[str]:
        """Lookup keys (short id or session id) of all managers."""
        return [m.key for m in self.managers]

    def names(self) -> list[str]:
        """Names of all managers (``None`` for unnamed rows kept as-is)."""
        return [m.name for m in self.managers if m.name is not None]

    @property
    def attention(self) -> tuple[ManagerSnapshot, ...]:
        """Managers currently needing attention (blocked/failed/waiting)."""
        return tuple(m for m in self.managers if m.needs_attention)


# ---------------------------------------------------------------------------
# Transcript ring-buffer merge (pure)
# ---------------------------------------------------------------------------


def _merge_tail(existing: Sequence[str], incoming: Sequence[str], *, maxlen: int) -> tuple[str, ...]:
    """Append *incoming* onto *existing*, de-duping the overlapping suffix.

    ``claude logs <id>`` returns a *tail* of recent output, so consecutive polls
    overlap. We append only the genuinely new lines (the longest suffix of
    ``existing`` that is also a prefix of ``incoming`` is treated as the overlap)
    and cap the result at *maxlen* lines — a real accumulating ring buffer rather
    than just the latest tail.
    """
    existing = list(existing)
    incoming = list(incoming)
    if not incoming:
        merged = existing
    elif not existing:
        merged = incoming
    else:
        max_k = min(len(existing), len(incoming))
        overlap = 0
        for k in range(max_k, 0, -1):
            if existing[-k:] == incoming[:k]:
                overlap = k
                break
        merged = existing + incoming[overlap:]
    if maxlen >= 0 and len(merged) > maxlen:
        merged = merged[-maxlen:]
    return tuple(merged)


# ---------------------------------------------------------------------------
# FleetState — pub/sub hub (pure, thread-safe)
# ---------------------------------------------------------------------------


class FleetState:
    """The fleet's single source of truth: holds a snapshot, publishes updates.

    Call :meth:`apply` with freshly fetched agent rows (and optional per-id log
    tails); it merges each manager's transcript ring buffer, builds a new
    :class:`FleetSnapshot`, stores it, and notifies subscribers **only when the
    managers actually changed** (timestamp-only ticks don't fire). Subscribers
    register with :meth:`subscribe` and get the latest snapshot on every change.

    Thread-safe: a :class:`FleetPoller` may call :meth:`apply` from a background
    thread while renderers read :attr:`snapshot` / (un)subscribe.
    """

    def __init__(self, *, log_lines: int = DEFAULT_LOG_LINES) -> None:
        self._log_lines = log_lines
        self._lock = threading.RLock()
        self._snapshot = FleetSnapshot(managers=(), updated_at=0.0)
        self._transcripts: dict[str, tuple[str, ...]] = {}
        self._subscribers: list[Subscriber] = []

    # ---- read -------------------------------------------------------------

    @property
    def snapshot(self) -> FleetSnapshot:
        """The current immutable fleet snapshot."""
        with self._lock:
            return self._snapshot

    def get(self, key: str) -> Optional[ManagerSnapshot]:
        """Convenience: look up one manager in the current snapshot."""
        return self.snapshot.get(key)

    # ---- pub/sub ----------------------------------------------------------

    def subscribe(
        self, callback: Subscriber, *, fire_immediately: bool = False
    ) -> Callable[[], None]:
        """Register *callback*; return a zero-arg unsubscribe handle.

        With ``fire_immediately`` the callback is invoked once with the current
        snapshot before returning (handy for a renderer that wants to paint the
        existing state on connect).
        """
        with self._lock:
            self._subscribers.append(callback)
            current = self._snapshot
        if fire_immediately:
            self._safe_notify(callback, current)
        return lambda: self.unsubscribe(callback)

    def unsubscribe(self, callback: Subscriber) -> None:
        """Remove a previously registered subscriber (no-op if absent)."""
        with self._lock:
            try:
                self._subscribers.remove(callback)
            except ValueError:
                pass

    @staticmethod
    def _safe_notify(callback: Subscriber, snapshot: FleetSnapshot) -> None:
        """Invoke one subscriber, logging (not raising) on its error."""
        try:
            callback(snapshot)
        except Exception:  # noqa: BLE001 — one bad subscriber must not break others
            logger.exception("fleet subscriber raised; continuing")

    # ---- write ------------------------------------------------------------

    def apply(
        self,
        agents: Sequence[AgentInfo],
        *,
        logs: Optional[dict[str, Sequence[str]]] = None,
        statuses: Optional[dict[str, ManagerStatus]] = None,
        now: Optional[float] = None,
    ) -> FleetSnapshot:
        """Fold a fresh poll into the state and (if changed) notify subscribers.

        *agents* are the rows from ``claude agents --json``; *logs* optionally
        maps a manager's lookup key (short id, falling back to session id) to a
        recent-log tail to merge into its transcript ring buffer; *statuses* maps
        that same key to the manager's own emitted :class:`ManagerStatus` (its
        drive-loop report, decision #21). Returns the new snapshot. Transcripts
        of managers no longer present are dropped.
        """
        logs = logs or {}
        statuses = statuses or {}
        timestamp = time.time() if now is None else now
        with self._lock:
            new_transcripts: dict[str, tuple[str, ...]] = {}
            managers: list[ManagerSnapshot] = []
            for agent in agents:
                key = agent.id or agent.session_id
                incoming = logs.get(key, ())
                merged = _merge_tail(
                    self._transcripts.get(key, ()), incoming, maxlen=self._log_lines
                )
                new_transcripts[key] = merged
                managers.append(
                    ManagerSnapshot.from_agent_info(
                        agent, transcript=merged, report=statuses.get(key)
                    )
                )
            self._transcripts = new_transcripts
            new_snapshot = FleetSnapshot(managers=tuple(managers), updated_at=timestamp)
            changed = new_snapshot.managers != self._snapshot.managers
            self._snapshot = new_snapshot
            subscribers = list(self._subscribers)
        if changed:
            for callback in subscribers:
                self._safe_notify(callback, new_snapshot)
        return new_snapshot


# ---------------------------------------------------------------------------
# Log tailing (subprocess) — integration surface
# ---------------------------------------------------------------------------


def tail_logs(
    agent_id: str,
    *,
    lines: int = 40,
    claude_bin: str = "claude",
    timeout: float = DEFAULT_CONTROL_TIMEOUT,
) -> list[str]:
    """Return up to *lines* recent log lines for a background agent.

    Wraps ``claude logs <id>`` and tails the output in Python (no assumption
    about a CLI line-count flag). Best-effort: if the call fails (the agent is
    gone, logs not ready, …) it returns ``[]`` rather than raising, so a polling
    loop never dies on a transient log read.
    """
    try:
        proc = _run_claude(
            ["logs", agent_id], claude_bin=claude_bin, timeout=timeout, check=True
        )
    except FleetManagerError:
        logger.debug("tail_logs(%s) failed; treating as no output", agent_id)
        return []
    out = proc.stdout.splitlines()
    return out[-lines:] if lines and lines > 0 else out


# ---------------------------------------------------------------------------
# FleetPoller — background thread that feeds FleetState
# ---------------------------------------------------------------------------


@dataclass
class FleetPoller:
    """Periodically fetch agents (+ logs) and push them into a :class:`FleetState`.

    The default sources are :func:`list_agents` and :func:`tail_logs`, but both
    are injectable (``agents_source`` / ``logs_source``) so the loop is testable
    without a real ``claude`` CLI. ``predicate`` scopes which rows are kept (e.g.
    a fleet's cwd or name prefix) so unrelated background agents on the machine
    are ignored. Use :meth:`start`/:meth:`stop` or as a context manager; one-shot
    :meth:`poll_once` is also exposed for synchronous/test use.
    """

    state: FleetState
    interval: float = DEFAULT_POLL_INTERVAL
    include_all: bool = False
    fetch_logs: bool = False
    log_lines: int = 40
    predicate: Optional[AgentPredicate] = None
    claude_bin: str = "claude"
    status_dir: Optional[str] = None
    agents_source: Optional[Callable[[], Sequence[AgentInfo]]] = None
    logs_source: Optional[Callable[[str], Sequence[str]]] = None
    statuses_source: Optional[
        Callable[[Sequence[AgentInfo]], dict[str, "ManagerStatus"]]
    ] = None

    _thread: Optional[threading.Thread] = field(default=None, init=False, repr=False)
    _stop: threading.Event = field(default_factory=threading.Event, init=False, repr=False)

    # ---- sources ----------------------------------------------------------

    def _fetch_agents(self) -> list[AgentInfo]:
        if self.agents_source is not None:
            agents = list(self.agents_source())
        else:
            agents = list_agents(include_all=self.include_all, claude_bin=self.claude_bin)
        if self.predicate is not None:
            agents = [a for a in agents if self.predicate(a)]
        return agents

    def _fetch_logs(self, agents: Sequence[AgentInfo]) -> dict[str, Sequence[str]]:
        if not self.fetch_logs:
            return {}
        source = self.logs_source
        logs: dict[str, Sequence[str]] = {}
        for agent in agents:
            key = agent.id or agent.session_id
            if source is not None:
                logs[key] = list(source(key))
            elif agent.id is not None:  # `claude logs` needs the short id
                logs[key] = tail_logs(
                    agent.id, lines=self.log_lines, claude_bin=self.claude_bin
                )
        return logs

    def _fetch_statuses(self, agents: Sequence[AgentInfo]) -> dict[str, ManagerStatus]:
        """Read each manager's emitted status (decision #21), keyed for apply().

        Uses ``statuses_source`` if injected (tests), else reads the on-disk
        status convention from ``status_dir`` when set; otherwise no statuses.
        """
        if self.statuses_source is not None:
            return dict(self.statuses_source(agents))
        if self.status_dir is not None:
            return read_statuses_for_agents(agents, self.status_dir)
        return {}

    # ---- one poll ---------------------------------------------------------

    def poll_once(self) -> FleetSnapshot:
        """Run a single fetch → :meth:`FleetState.apply` cycle; return the snapshot."""
        agents = self._fetch_agents()
        logs = self._fetch_logs(agents)
        statuses = self._fetch_statuses(agents)
        return self.state.apply(agents, logs=logs, statuses=statuses)

    # ---- lifecycle --------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.poll_once()
            except Exception:  # noqa: BLE001 — a transient CLI error must not kill the loop
                logger.exception("fleet poll failed; will retry next interval")
            # Wait returns True if stopped early, so we exit promptly on stop().
            self._stop.wait(self.interval)

    def start(self) -> "FleetPoller":
        """Start the background poll thread (idempotent while already running)."""
        if self.running:
            return self
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="fleet-poller", daemon=True
        )
        self._thread.start()
        return self

    def stop(self, *, timeout: float = 5.0) -> None:
        """Signal the poll thread to stop and join it (best-effort)."""
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=timeout)
        self._thread = None

    def __enter__(self) -> "FleetPoller":
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()
