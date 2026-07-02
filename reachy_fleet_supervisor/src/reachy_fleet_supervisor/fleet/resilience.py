"""Rate-limit backoff + recovery paths for N managers sharing one Max pool (U23).

The design premise (plan.md — "2-4 concurrent managers (Max-plan rate limits)")
means several ``claude --bg`` managers can transiently collide on the SAME
underlying Claude Max usage pool: a spawn or a steer call can come back with a
rate-limit / overload error even though the command itself was well-formed.
Two complementary pieces close that gap:

- **Backoff** (:func:`retry_call` / :class:`RetryPolicy`): classify a
  :class:`~reachy_fleet_supervisor.fleet.manager.FleetManagerError` as
  rate-limit-shaped (:func:`is_rate_limit_error`) and retry with exponential
  backoff + jitter, capped at a max delay/attempt count. A non-rate-limit error
  (bad argv, missing binary, a genuine task failure) is NOT retried — it fails
  fast, exactly like today.
- **Recovery** (:func:`classify_manager_health` / :func:`recover_manager`): a
  background manager can go missing from the roster (``failed``/``stopped``/
  absent) for reasons ranging from "the human intentionally stopped it" to "it
  got killed mid rate-limit". :func:`recover_manager` respawns/resumes it
  (through the same backoff path) so a transient pool collision self-heals
  instead of silently dropping a manager off the fleet.

Pure/testable: no module here talks to a subprocess directly — it wraps
callables (``fn`` / ``spawn_fn``) so the retry/backoff/classification logic is
fully unit-tested without a real ``claude`` CLI.
"""

from __future__ import annotations
import time
import random
import logging
from typing import Callable, Optional, TypeVar
from dataclasses import dataclass

from .manager import AgentInfo, FleetManagerError


logger = logging.getLogger(__name__)

T = TypeVar("T")

# Substrings (lowercased) seen in Claude Code CLI stderr/stdout when a call is
# rejected due to Max-plan usage limits or upstream overload — NOT a real task
# failure, so it is worth retrying after a backoff. Kept broad but specific
# enough to avoid mis-classifying an ordinary error (e.g. "task failed").
RATE_LIMIT_MARKERS: tuple[str, ...] = (
    "rate limit",
    "rate_limit",
    "ratelimited",
    "usage limit",
    "usage_limit",
    "quota exceeded",
    "429",
    "too many requests",
    "overloaded",
    "capacity",
    "please try again later",
    "please retry",
)


def is_rate_limit_error(text: Optional[str]) -> bool:
    """Return True if *text* looks like a rate-limit / overload rejection.

    Pure string classification — case-insensitive substring match against
    :data:`RATE_LIMIT_MARKERS`. ``None``/blank text is never rate-limit-shaped.
    """
    if not text:
        return False
    lowered = text.lower()
    return any(marker in lowered for marker in RATE_LIMIT_MARKERS)


def is_retryable_exception(exc: BaseException) -> bool:
    """Default retry predicate: True if *exc*'s message is rate-limit-shaped."""
    return is_rate_limit_error(str(exc))


@dataclass(frozen=True)
class RetryPolicy:
    """Exponential backoff + jitter tuned for a shared Max-plan pool.

    ``max_attempts`` counts the FIRST try, so ``max_attempts=4`` means up to 3
    retries. Delay for attempt *n* (1-indexed, before the (n+1)-th try) is
    ``min(max_delay, base_delay * multiplier**(n-1))`` plus up to ``jitter``
    fraction of that value (uniform, to avoid every manager retrying in lockstep
    after a shared rate-limit hit).
    """

    max_attempts: int = 4
    base_delay: float = 2.0
    max_delay: float = 60.0
    multiplier: float = 2.0
    jitter: float = 0.2

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError(f"max_attempts must be >= 1, got {self.max_attempts!r}")
        if self.base_delay < 0:
            raise ValueError(f"base_delay must be >= 0, got {self.base_delay!r}")
        if self.max_delay < self.base_delay:
            raise ValueError("max_delay must be >= base_delay")
        if not (0.0 <= self.jitter < 1.0):
            raise ValueError(f"jitter must be in [0, 1), got {self.jitter!r}")


# Conservative default: don't hammer a shared pool that just rejected a call.
DEFAULT_RETRY_POLICY = RetryPolicy()


def compute_backoff_delay(
    attempt: int,
    policy: RetryPolicy = DEFAULT_RETRY_POLICY,
    *,
    rand: Callable[[], float] = random.random,
) -> float:
    """Return the delay (seconds) to sleep before retry attempt *attempt*.

    ``attempt`` is 1-indexed (the delay BEFORE the 2nd try is ``attempt=1``).
    Pure given an injected ``rand`` (``0.0``..``1.0``) — deterministic in tests.
    """
    if attempt < 1:
        raise ValueError(f"attempt must be >= 1, got {attempt!r}")
    raw = policy.base_delay * (policy.multiplier ** (attempt - 1))
    capped = min(policy.max_delay, raw)
    jittered = capped + capped * policy.jitter * rand()
    return jittered


def retry_call(
    fn: Callable[[], T],
    *,
    policy: RetryPolicy = DEFAULT_RETRY_POLICY,
    is_retryable: Callable[[BaseException], bool] = is_retryable_exception,
    sleep: Callable[[float], None] = time.sleep,
    rand: Callable[[], float] = random.random,
    on_retry: Optional[Callable[[int, BaseException, float], None]] = None,
) -> T:
    """Call ``fn()``, retrying on a rate-limit-shaped error with backoff.

    Retries up to ``policy.max_attempts`` total tries. Between tries, sleeps
    :func:`compute_backoff_delay` seconds (via the injected ``sleep``, so this
    is instant/deterministic in tests). A non-retryable exception (per
    ``is_retryable``) propagates immediately without consuming a retry. If every
    attempt is exhausted, the LAST exception is re-raised. ``on_retry`` (if
    given) is called with ``(attempt, exception, delay)`` before each sleep —
    useful for logging/telemetry without coupling this module to a logger
    format.
    """
    last_exc: Optional[BaseException] = None
    for attempt in range(1, policy.max_attempts + 1):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - reclassified below
            last_exc = exc
            if not is_retryable(exc):
                raise
            if attempt >= policy.max_attempts:
                break
            delay = compute_backoff_delay(attempt, policy, rand=rand)
            logger.warning(
                "retryable error on attempt %d/%d: %s (backing off %.1fs)",
                attempt, policy.max_attempts, exc, delay,
            )
            if on_retry is not None:
                on_retry(attempt, exc, delay)
            sleep(delay)
    assert last_exc is not None  # max_attempts >= 1 guarantees at least one try
    raise last_exc


# ---------------------------------------------------------------------------
# Recovery paths — a manager missing/failed from the roster
# ---------------------------------------------------------------------------

# Roster ``state`` values that mean "not actively running, but the daemon still
# knows about it" (a candidate for recovery, as opposed to genuinely gone).
RECOVERABLE_STATES = frozenset({"failed", "stopped"})


@dataclass(frozen=True)
class ManagerHealth:
    """Classification of one manager's health from its last-known roster row.

    ``status`` is one of:

    - ``"healthy"``  — actively tracked and running (``working``/``blocked``/
      ``done`` are all "the daemon has it"; only a hard failure/absence needs
      recovery).
    - ``"recoverable"`` — the roster still lists it but in a
      :data:`RECOVERABLE_STATES` state (e.g. killed mid rate-limit) — resumable
      via ``claude respawn``.
    - ``"missing"`` — not in the roster at all (``agent is None``); nothing to
      respawn from — the caller must re-``spawn`` a fresh manager if desired.
    """

    status: str
    agent: Optional[AgentInfo]

    @property
    def needs_recovery(self) -> bool:
        return self.status in ("recoverable", "missing")


def classify_manager_health(agent: Optional[AgentInfo]) -> ManagerHealth:
    """Classify a manager's health from its (possibly ``None``) roster row.

    Pure — takes the already-fetched :class:`AgentInfo` (or ``None`` if it
    vanished from ``claude agents --json`` entirely) so it needs no subprocess.
    """
    if agent is None:
        return ManagerHealth(status="missing", agent=None)
    if agent.state in RECOVERABLE_STATES:
        return ManagerHealth(status="recoverable", agent=agent)
    return ManagerHealth(status="healthy", agent=agent)


def recover_manager(
    manager: "object",
    *,
    policy: RetryPolicy = DEFAULT_RETRY_POLICY,
    sleep: Callable[[float], None] = time.sleep,
    rand: Callable[[], float] = random.random,
    control_timeout: Optional[float] = None,
) -> bool:
    """Best-effort recovery of one manager that dropped out of a "healthy" state.

    Takes a live :class:`~reachy_fleet_supervisor.fleet.manager.FleetManager`
    (typed loosely here to avoid an import cycle with callers that also need
    :class:`ManagerHealth`). Checks :meth:`FleetManager.info` to classify health
    via :func:`classify_manager_health`:

    - ``"healthy"`` -> no-op, returns ``False`` (nothing to recover).
    - ``"recoverable"`` -> ``claude respawn`` through :func:`retry_call` (so a
      respawn itself hitting the shared pool's rate limit is retried with
      backoff); returns ``True`` on success.
    - ``"missing"`` -> nothing this function can respawn FROM (no roster row to
      resume); returns ``False`` — the caller decides whether to re-``spawn`` a
      brand new manager.

    Raises whatever :meth:`FleetManager.resume` raises if every retry is
    exhausted (a genuine, non-rate-limit failure propagates immediately, per
    :func:`retry_call`).
    """
    kwargs = {} if control_timeout is None else {"timeout": control_timeout}
    agent = manager.info(**kwargs)
    health = classify_manager_health(agent)
    if health.status != "recoverable":
        return False
    logger.info(
        "recovering manager '%s' (id=%s) from state=%s",
        getattr(manager, "name", "?"), getattr(manager, "id", "?"), health.agent.state,
    )
    retry_call(
        lambda: manager.resume(**kwargs),
        policy=policy,
        sleep=sleep,
        rand=rand,
    )
    return True
