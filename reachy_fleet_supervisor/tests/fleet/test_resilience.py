"""Tests for rate-limit backoff + recovery paths (U23)."""

from __future__ import annotations

import pytest

from reachy_fleet_supervisor.fleet import (
    AgentInfo,
    FleetManagerError,
)
from reachy_fleet_supervisor.fleet.resilience import (
    RATE_LIMIT_MARKERS,
    RECOVERABLE_STATES,
    DEFAULT_RETRY_POLICY,
    ManagerHealth,
    RetryPolicy,
    classify_manager_health,
    compute_backoff_delay,
    is_rate_limit_error,
    is_retryable_exception,
    recover_manager,
    retry_call,
)


# ---------------------------------------------------------------------------
# is_rate_limit_error / is_retryable_exception
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "rate limit exceeded, try again later",
        "Rate_Limit hit for this session",
        "HTTP 429 Too Many Requests",
        "usage limit reached for this plan",
        "upstream overloaded, please retry",
        "server is at capacity right now",
    ],
)
def test_is_rate_limit_error_true_for_known_markers(text: str) -> None:
    assert is_rate_limit_error(text) is True


@pytest.mark.parametrize(
    "text",
    [
        None,
        "",
        "task failed: file not found",
        "invalid permission mode 'bogus'",
        "claude CLI not found",
    ],
)
def test_is_rate_limit_error_false_for_other_errors(text) -> None:
    assert is_rate_limit_error(text) is False


def test_is_rate_limit_error_case_insensitive() -> None:
    assert is_rate_limit_error("RATE LIMIT") is True
    assert is_rate_limit_error("Please Try Again Later") is True


def test_is_retryable_exception_delegates_to_message() -> None:
    assert is_retryable_exception(FleetManagerError("429 too many requests")) is True
    assert is_retryable_exception(FleetManagerError("bad argv")) is False


def test_rate_limit_markers_nonempty_and_lowercase() -> None:
    assert RATE_LIMIT_MARKERS
    assert all(marker == marker.lower() for marker in RATE_LIMIT_MARKERS)


# ---------------------------------------------------------------------------
# RetryPolicy validation
# ---------------------------------------------------------------------------


def test_retry_policy_defaults_are_sane() -> None:
    policy = DEFAULT_RETRY_POLICY
    assert policy.max_attempts >= 1
    assert 0 <= policy.jitter < 1
    assert policy.max_delay >= policy.base_delay


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_attempts": 0},
        {"base_delay": -1},
        {"max_delay": 0.5, "base_delay": 1.0},
        {"jitter": 1.0},
        {"jitter": -0.1},
    ],
)
def test_retry_policy_rejects_invalid_values(kwargs) -> None:
    with pytest.raises(ValueError):
        RetryPolicy(**kwargs)


# ---------------------------------------------------------------------------
# compute_backoff_delay
# ---------------------------------------------------------------------------


def test_compute_backoff_delay_grows_exponentially_and_caps() -> None:
    policy = RetryPolicy(base_delay=1.0, max_delay=10.0, multiplier=2.0, jitter=0.0)
    # jitter=0 -> deterministic, rand() unused.
    assert compute_backoff_delay(1, policy) == pytest.approx(1.0)
    assert compute_backoff_delay(2, policy) == pytest.approx(2.0)
    assert compute_backoff_delay(3, policy) == pytest.approx(4.0)
    assert compute_backoff_delay(4, policy) == pytest.approx(8.0)
    # Would be 16.0 uncapped; capped at max_delay=10.0.
    assert compute_backoff_delay(5, policy) == pytest.approx(10.0)


def test_compute_backoff_delay_applies_jitter_within_bound() -> None:
    policy = RetryPolicy(base_delay=2.0, max_delay=60.0, multiplier=2.0, jitter=0.5)
    delay_no_jitter = compute_backoff_delay(1, policy, rand=lambda: 0.0)
    delay_full_jitter = compute_backoff_delay(1, policy, rand=lambda: 1.0)
    assert delay_no_jitter == pytest.approx(2.0)
    assert delay_full_jitter == pytest.approx(2.0 + 2.0 * 0.5)
    assert delay_no_jitter <= delay_full_jitter


def test_compute_backoff_delay_rejects_nonpositive_attempt() -> None:
    with pytest.raises(ValueError):
        compute_backoff_delay(0, DEFAULT_RETRY_POLICY)


# ---------------------------------------------------------------------------
# retry_call
# ---------------------------------------------------------------------------


def test_retry_call_returns_immediately_on_success() -> None:
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        return "ok"

    result = retry_call(fn, policy=RetryPolicy(max_attempts=3), sleep=lambda s: None)
    assert result == "ok"
    assert calls["n"] == 1


def test_retry_call_retries_rate_limit_error_then_succeeds() -> None:
    calls = {"n": 0}
    sleeps: list[float] = []

    def fn():
        calls["n"] += 1
        if calls["n"] < 3:
            raise FleetManagerError("429 too many requests")
        return "recovered"

    result = retry_call(
        fn,
        policy=RetryPolicy(max_attempts=5, base_delay=1.0, jitter=0.0),
        sleep=sleeps.append,
    )
    assert result == "recovered"
    assert calls["n"] == 3
    assert len(sleeps) == 2  # slept before attempt 2 and attempt 3


def test_retry_call_raises_last_exception_after_exhausting_attempts() -> None:
    def fn():
        raise FleetManagerError("rate limit exceeded")

    with pytest.raises(FleetManagerError, match="rate limit"):
        retry_call(
            fn,
            policy=RetryPolicy(max_attempts=3, base_delay=0.0, jitter=0.0),
            sleep=lambda s: None,
        )


def test_retry_call_does_not_retry_non_retryable_error() -> None:
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        raise FleetManagerError("invalid permission mode 'bogus'")

    with pytest.raises(FleetManagerError, match="invalid permission mode"):
        retry_call(
            fn, policy=RetryPolicy(max_attempts=5), sleep=lambda s: pytest.fail("must not sleep")
        )
    assert calls["n"] == 1


def test_retry_call_invokes_on_retry_callback() -> None:
    events: list[tuple[int, float]] = []

    def fn():
        if not events:
            raise FleetManagerError("overloaded")
        return "ok"

    def on_retry(attempt, exc, delay):
        events.append((attempt, delay))

    retry_call(
        fn,
        policy=RetryPolicy(max_attempts=2, base_delay=1.0, jitter=0.0),
        sleep=lambda s: None,
        on_retry=on_retry,
    )
    assert events == [(1, 1.0)]


# ---------------------------------------------------------------------------
# classify_manager_health
# ---------------------------------------------------------------------------


def _agent(state: str | None, **extra) -> AgentInfo:
    return AgentInfo(session_id="s-1", id="abc123", name="worker", state=state, **extra)


def test_classify_manager_health_missing_when_no_agent() -> None:
    health = classify_manager_health(None)
    assert health.status == "missing"
    assert health.needs_recovery is True
    assert health.agent is None


@pytest.mark.parametrize("state", sorted(RECOVERABLE_STATES))
def test_classify_manager_health_recoverable_states(state: str) -> None:
    health = classify_manager_health(_agent(state))
    assert health.status == "recoverable"
    assert health.needs_recovery is True


@pytest.mark.parametrize("state", ["working", "blocked", "done"])
def test_classify_manager_health_healthy_states(state: str) -> None:
    health = classify_manager_health(_agent(state))
    assert health.status == "healthy"
    assert health.needs_recovery is False


# ---------------------------------------------------------------------------
# recover_manager
# ---------------------------------------------------------------------------


class _FakeManager:
    """Stand-in for FleetManager: records .info()/.resume() calls."""

    def __init__(self, *, id: str, name: str, agent: AgentInfo | None, resume_error=None):
        self.id = id
        self.name = name
        self._agent = agent
        self.resume_calls = 0
        self._resume_error = resume_error

    def info(self, **kwargs):
        return self._agent

    def resume(self, **kwargs):
        self.resume_calls += 1
        if self._resume_error is not None:
            raise self._resume_error
        return "resumed"


def test_recover_manager_no_op_when_healthy() -> None:
    manager = _FakeManager(id="abc", name="worker", agent=_agent("working"))
    recovered = recover_manager(manager, sleep=lambda s: None)
    assert recovered is False
    assert manager.resume_calls == 0


def test_recover_manager_no_op_when_missing() -> None:
    manager = _FakeManager(id="abc", name="worker", agent=None)
    recovered = recover_manager(manager, sleep=lambda s: None)
    assert recovered is False
    assert manager.resume_calls == 0


def test_recover_manager_resumes_when_recoverable() -> None:
    manager = _FakeManager(id="abc", name="worker", agent=_agent("failed"))
    recovered = recover_manager(manager, sleep=lambda s: None)
    assert recovered is True
    assert manager.resume_calls == 1


def test_recover_manager_retries_resume_on_rate_limit() -> None:
    calls = {"n": 0}

    class FlakyManager(_FakeManager):
        def resume(self, **kwargs):
            calls["n"] += 1
            if calls["n"] < 2:
                raise FleetManagerError("rate limit exceeded")
            return "resumed"

    manager = FlakyManager(id="abc", name="worker", agent=_agent("stopped"))
    recovered = recover_manager(
        manager, policy=RetryPolicy(max_attempts=3, base_delay=0.0, jitter=0.0), sleep=lambda s: None
    )
    assert recovered is True
    assert calls["n"] == 2


def test_recover_manager_propagates_non_retryable_resume_error() -> None:
    manager = _FakeManager(
        id="abc", name="worker", agent=_agent("failed"),
        resume_error=FleetManagerError("respawn: session permanently gone"),
    )
    with pytest.raises(FleetManagerError, match="permanently gone"):
        recover_manager(manager, sleep=lambda s: None)
