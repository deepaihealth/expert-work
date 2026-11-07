"""Unit tests for :class:`LLMErrorHandlingMiddleware` (Stream E.4).

Uses an in-test fake clock + no-op sleeper to keep state-machine
transitions deterministic and runtime under 100 ms.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from expert_work.runtime.middleware import (
    BreakerRegistry,
    CircuitBreaker,
    CircuitOpenError,
    LLMClientError,
    LLMErrorHandlingMiddleware,
    LLMKeyUnavailableError,
    LLMNetworkError,
    LLMRateLimitError,
    LLMServerError,
    LLMStreamStaleError,
    Middleware,
    MiddlewareContext,
)

# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


@dataclass
class FakeClock:
    """Manually-advanced monotonic clock for breaker timing tests."""

    now: float = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@dataclass
class SleepTracker:
    """Captures sleeper delays for backoff-timing assertions."""

    sleeps: list[float] = field(default_factory=list)

    async def __call__(self, delay: float) -> None:
        self.sleeps.append(delay)


class SequenceTerminal:
    """Terminal handler that walks an exception sequence call-by-call.

    Each fresh instance has its own ``calls`` counter — important when
    test bodies loop over multiple invocations and need each one to
    start from a clean state.
    """

    def __init__(self, exc_seq: list[Exception | None]) -> None:
        self._exc_seq = exc_seq
        self.calls = 0

    async def __call__(self, _ctx: MiddlewareContext) -> None:
        idx = self.calls
        self.calls += 1
        if idx < len(self._exc_seq):
            exc = self._exc_seq[idx]
            if exc is not None:
                raise exc


def _ctx(provider_key: str = "test-key", *, has_fallback: bool = False) -> MiddlewareContext:
    return MiddlewareContext(payload={"provider_key": provider_key, "has_fallback": has_fallback})


def _mw(
    *,
    clock: FakeClock | None = None,
    sleep_tracker: SleepTracker | None = None,
    max_retries: int = 3,
    failure_threshold: int = 5,
    cooldown_s: float = 30.0,
    base_delay_s: float = 1.0,
    max_delay_s: float = 30.0,
) -> tuple[LLMErrorHandlingMiddleware, SleepTracker]:
    sleep_tracker = sleep_tracker or SleepTracker()
    clock = clock or FakeClock()
    registry = BreakerRegistry(
        failure_threshold=failure_threshold,
        cooldown_s=cooldown_s,
        clock=clock,
    )
    mw = LLMErrorHandlingMiddleware(
        breaker_registry=registry,
        max_retries=max_retries,
        base_delay_s=base_delay_s,
        max_delay_s=max_delay_s,
        sleeper=sleep_tracker,
    )
    return mw, sleep_tracker


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_first_attempt_success_records_closed() -> None:
    mw, tracker = _mw()
    terminal = SequenceTerminal([None])
    await mw(_ctx(), terminal)
    assert terminal.calls == 1
    assert tracker.sleeps == []
    breaker = await mw.breaker_registry.get("test-key")
    assert await breaker.check_state() == "CLOSED"
    assert breaker.consecutive_failures == 0


@pytest.mark.asyncio
async def test_5xx_retry_eventually_succeeds() -> None:
    mw, tracker = _mw()
    terminal = SequenceTerminal([LLMServerError(), LLMServerError(), None])
    await mw(_ctx(), terminal)
    assert terminal.calls == 3
    # base_delay 1s, exponents 0/1 → 1.0s, 2.0s.
    assert tracker.sleeps == [1.0, 2.0]
    breaker = await mw.breaker_registry.get("test-key")
    assert breaker.consecutive_failures == 0


@pytest.mark.asyncio
async def test_rate_limit_treated_as_retryable() -> None:
    mw, _ = _mw()
    terminal = SequenceTerminal([LLMRateLimitError(), None])
    await mw(_ctx(), terminal)
    assert terminal.calls == 2


@pytest.mark.asyncio
async def test_network_error_treated_as_retryable() -> None:
    mw, _ = _mw()
    terminal = SequenceTerminal([LLMNetworkError(), None])
    await mw(_ctx(), terminal)
    assert terminal.calls == 2


# ---------------------------------------------------------------------------
# Non-retryable client errors
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_4xx_not_retried_and_does_not_trip_breaker() -> None:
    mw, tracker = _mw()
    terminal = SequenceTerminal([LLMClientError("bad request")])
    with pytest.raises(LLMClientError):
        await mw(_ctx(), terminal)
    assert terminal.calls == 1
    assert tracker.sleeps == []
    breaker = await mw.breaker_registry.get("test-key")
    assert breaker.consecutive_failures == 0


@pytest.mark.asyncio
async def test_stream_stale_not_retried_falls_over_promptly() -> None:
    # Stream L (P1) — a first-token / stream-stale timeout is a plain
    # LLMError (not LLMServerError), so E.4 must NOT retry it in-place: the
    # terminal is invoked exactly once and the error propagates for the
    # router to fall over to the next provider.
    mw, tracker = _mw()
    terminal = SequenceTerminal([LLMStreamStaleError("provider hung")])
    with pytest.raises(LLMStreamStaleError):
        await mw(_ctx(), terminal)
    assert terminal.calls == 1
    assert tracker.sleeps == []


# ---------------------------------------------------------------------------
# Circuit breaker state machine
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exhausted_retries_records_failure_and_raises() -> None:
    mw, _ = _mw(max_retries=2)
    terminal = SequenceTerminal([LLMServerError()] * 3)
    with pytest.raises(LLMServerError):
        await mw(_ctx(), terminal)
    assert terminal.calls == 3  # 1 initial + 2 retries
    breaker = await mw.breaker_registry.get("test-key")
    # B-297 — every failed upstream attempt counts, not one per exhausted call.
    assert breaker.consecutive_failures == 3


@pytest.mark.asyncio
async def test_five_consecutive_exhausted_failures_open_breaker() -> None:
    """failure_threshold=5 + max_retries=0 → 5 consecutive raises → OPEN."""
    mw, _ = _mw(max_retries=0, failure_threshold=5)
    for _ in range(5):
        terminal = SequenceTerminal([LLMServerError()])
        with pytest.raises(LLMServerError):
            await mw(_ctx(), terminal)
    breaker = await mw.breaker_registry.get("test-key")
    assert await breaker.check_state() == "OPEN"
    assert breaker.consecutive_failures == 5


@pytest.mark.asyncio
async def test_open_breaker_raises_immediately_without_call() -> None:
    mw, _ = _mw(max_retries=0, failure_threshold=2)
    # Trip the breaker.
    for _ in range(2):
        terminal = SequenceTerminal([LLMServerError()])
        with pytest.raises(LLMServerError):
            await mw(_ctx(), terminal)

    probe = SequenceTerminal([None])
    # B-297 — an OPEN breaker short-circuits only when the router has a
    # fallback to move on to (a single-model chain still gets its attempt).
    with pytest.raises(CircuitOpenError) as excinfo:
        await mw(_ctx(has_fallback=True), probe)
    assert excinfo.value.key == "test-key"
    assert probe.calls == 0, "terminal must not be invoked when breaker OPEN"


@pytest.mark.asyncio
async def test_half_open_success_closes_breaker() -> None:
    clock = FakeClock()
    mw, _ = _mw(clock=clock, max_retries=0, failure_threshold=2, cooldown_s=30.0)
    for _ in range(2):
        terminal = SequenceTerminal([LLMServerError()])
        with pytest.raises(LLMServerError):
            await mw(_ctx(), terminal)

    clock.advance(31.0)
    breaker = await mw.breaker_registry.get("test-key")
    assert await breaker.check_state() == "HALF_OPEN"

    await mw(_ctx(), SequenceTerminal([None]))
    assert await breaker.check_state() == "CLOSED"
    assert breaker.consecutive_failures == 0


@pytest.mark.asyncio
async def test_half_open_failure_reopens_breaker() -> None:
    clock = FakeClock()
    mw, _ = _mw(clock=clock, max_retries=0, failure_threshold=2, cooldown_s=30.0)
    for _ in range(2):
        terminal = SequenceTerminal([LLMServerError()])
        with pytest.raises(LLMServerError):
            await mw(_ctx(), terminal)

    clock.advance(31.0)
    breaker = await mw.breaker_registry.get("test-key")
    assert await breaker.check_state() == "HALF_OPEN"

    with pytest.raises(LLMServerError):
        await mw(_ctx(), SequenceTerminal([LLMServerError()]))
    assert await breaker.check_state() == "OPEN"


# ---------------------------------------------------------------------------
# Per-key isolation + default key
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_per_key_breakers_are_isolated() -> None:
    mw, _ = _mw(max_retries=0, failure_threshold=2)
    # Trip key A.
    for _ in range(2):
        with pytest.raises(LLMServerError):
            await mw(_ctx("key-A"), SequenceTerminal([LLMServerError()]))

    breaker_a = await mw.breaker_registry.get("key-A")
    breaker_b = await mw.breaker_registry.get("key-B")
    assert await breaker_a.check_state() == "OPEN"
    assert await breaker_b.check_state() == "CLOSED"

    await mw(_ctx("key-B"), SequenceTerminal([None]))


@pytest.mark.asyncio
async def test_missing_provider_key_uses_default() -> None:
    mw, _ = _mw()
    ctx = MiddlewareContext(payload={})
    await mw(ctx, SequenceTerminal([None]))
    breaker = await mw.breaker_registry.get("default")
    assert breaker.consecutive_failures == 0


# ---------------------------------------------------------------------------
# Backoff timing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exponential_backoff_caps_at_max_delay() -> None:
    mw, tracker = _mw(max_retries=5, base_delay_s=1.0, max_delay_s=4.0)
    terminal = SequenceTerminal([LLMServerError()] * 6)
    with pytest.raises(LLMServerError):
        await mw(_ctx(), terminal)
    # Attempts 1-6; sleeps after the first 5: 2^0..2^4 capped at 4 → 1, 2, 4, 4, 4.
    assert tracker.sleeps == [1.0, 2.0, 4.0, 4.0, 4.0]


# ---------------------------------------------------------------------------
# Contract
# ---------------------------------------------------------------------------


def test_satisfies_middleware_protocol() -> None:
    assert isinstance(LLMErrorHandlingMiddleware(), Middleware)


def test_breaker_consecutive_failures_starts_at_zero() -> None:
    breaker = CircuitBreaker()
    assert breaker.consecutive_failures == 0


def test_circuit_open_error_carries_key() -> None:
    err = CircuitOpenError("anthropic-primary")
    assert err.key == "anthropic-primary"
    assert "anthropic-primary" in str(err)


# ---------------------------------------------------------------------------
# Y-MK — LLMKeyUnavailableError: account/key dead (欠费/quota/撤销)
# Not retried (backoff is pointless on a dead account) but DOES trip the
# breaker so the key is skipped during its cooldown.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_key_unavailable_not_retried_records_failure() -> None:
    mw, tracker = _mw()
    terminal = SequenceTerminal([LLMKeyUnavailableError("402 insufficient balance")])
    with pytest.raises(LLMKeyUnavailableError):
        await mw(_ctx(), terminal)
    assert terminal.calls == 1  # no retry
    assert tracker.sleeps == []  # no backoff
    breaker = await mw.breaker_registry.get("test-key")
    assert breaker.consecutive_failures == 1  # counted toward breaker


@pytest.mark.asyncio
async def test_key_unavailable_opens_breaker_at_threshold() -> None:
    mw, _ = _mw(failure_threshold=3)
    for _ in range(3):
        terminal = SequenceTerminal([LLMKeyUnavailableError("402")])
        with pytest.raises(LLMKeyUnavailableError):
            await mw(_ctx(), terminal)
    breaker = await mw.breaker_registry.get("test-key")
    assert await breaker.check_state() == "OPEN"
