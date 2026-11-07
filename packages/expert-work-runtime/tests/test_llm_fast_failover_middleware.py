"""B-297 — LLMErrorHandlingMiddleware fails over fast when a backup exists.

The router tells the middleware whether a later provider group can still
serve (``ctx.payload["has_fallback"]``). With a backup: at most one
same-provider retry, a short jittered backoff and a 15 s per-provider budget.
Without one: the long-standing 3-retry exponential behaviour. Every failed
upstream attempt counts toward the breaker.

Fake clock + recording sleeper (the sleeper advances the clock) — no real sleeps.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from expert_work.runtime.middleware import (
    BreakerRegistry,
    CircuitOpenError,
    LLMClientError,
    LLMErrorHandlingMiddleware,
    LLMKeyUnavailableError,
    LLMNetworkError,
    LLMRateLimitError,
    LLMServerError,
    LLMStreamInterruptedError,
    LLMStreamStaleError,
    MiddlewareContext,
)


@dataclass
class FakeClock:
    now: float = 0.0

    def __call__(self) -> float:
        return self.now


@dataclass
class ClockSleeper:
    clock: FakeClock
    sleeps: list[float] = field(default_factory=list)

    async def __call__(self, delay: float) -> None:
        self.sleeps.append(delay)
        self.clock.now += delay


class Terminal:
    """Raises the scripted exceptions in turn; each call takes ``call_s`` of clock."""

    def __init__(self, seq: list[Exception | None], clock: FakeClock, call_s: float = 0.0) -> None:
        self._seq = seq
        self._clock = clock
        self._call_s = call_s
        self.calls = 0

    async def __call__(self, _ctx: MiddlewareContext) -> None:
        idx = self.calls
        self.calls += 1
        self._clock.now += self._call_s
        if idx < len(self._seq) and self._seq[idx] is not None:
            raise self._seq[idx]  # type: ignore[misc]


def _ctx(*, has_fallback: bool, key: str = "p:m") -> MiddlewareContext:
    return MiddlewareContext(payload={"provider_key": key, "has_fallback": has_fallback})


def _mw(
    clock: FakeClock, sleeper: ClockSleeper, *, rand: float = 1.0
) -> LLMErrorHandlingMiddleware:
    return LLMErrorHandlingMiddleware(
        breaker_registry=BreakerRegistry(clock=clock),
        sleeper=sleeper,
        clock=clock,
        rand=lambda: rand,
    )


def _err(cls: type[Exception], retry_after_s: float | None = None) -> Exception:
    exc = cls("boom")
    if retry_after_s is not None:
        exc.retry_after_s = retry_after_s  # type: ignore[attr-defined]
    return exc


# ---------------------------------------------------------------------------
# Budget depends on whether a fallback exists
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_with_fallback_at_most_one_retry_with_short_backoff() -> None:
    clock = FakeClock()
    sleeper = ClockSleeper(clock)
    mw = _mw(clock, sleeper)
    terminal = Terminal([LLMServerError()] * 5, clock, call_s=1.0)
    with pytest.raises(LLMServerError):
        await mw(_ctx(has_fallback=True), terminal)
    assert terminal.calls == 2
    assert len(sleeper.sleeps) == 1
    assert sleeper.sleeps[0] <= 1.0


@pytest.mark.asyncio
async def test_with_fallback_backoff_is_jittered() -> None:
    clock = FakeClock()
    low = ClockSleeper(clock)
    await _run_swallow(_mw(clock, low, rand=0.0), Terminal([LLMServerError()] * 2, clock))
    high = ClockSleeper(clock)
    await _run_swallow(_mw(clock, high, rand=1.0), Terminal([LLMServerError()] * 2, clock))
    assert low.sleeps[0] < high.sleeps[0]
    assert 0 < low.sleeps[0]


async def _run_swallow(mw: LLMErrorHandlingMiddleware, terminal: Terminal) -> None:
    try:
        await mw(_ctx(has_fallback=True), terminal)
    except LLMServerError:
        pass


@pytest.mark.asyncio
async def test_without_fallback_keeps_three_exponential_retries() -> None:
    clock = FakeClock()
    sleeper = ClockSleeper(clock)
    mw = _mw(clock, sleeper)
    terminal = Terminal([LLMServerError()] * 5, clock, call_s=1.0)
    with pytest.raises(LLMServerError):
        await mw(_ctx(has_fallback=False), terminal)
    # Today's budget, unchanged: 1 + 3 attempts, 1 s / 2 s / 4 s. The breaker
    # opens at the 3rd failed attempt but a chain with nowhere else to go is
    # not short-circuited by it (it would only turn a maybe-transient blip
    # into a certain failure).
    assert terminal.calls == 4
    assert sleeper.sleeps == [1.0, 2.0, 4.0]
    breaker = await mw.breaker_registry.get("p:m")
    assert await breaker.check_state() == "OPEN"


@pytest.mark.asyncio
async def test_without_fallback_open_breaker_does_not_block_the_only_model() -> None:
    clock = FakeClock()
    sleeper = ClockSleeper(clock)
    mw = _mw(clock, sleeper)
    with pytest.raises(LLMServerError):
        await mw(_ctx(has_fallback=True), Terminal([LLMServerError()] * 2, clock))
    with pytest.raises(LLMServerError):
        await mw(_ctx(has_fallback=True), Terminal([LLMServerError()], clock))
    breaker = await mw.breaker_registry.get("p:m")
    assert await breaker.check_state() == "OPEN"
    # A backed-up chain is refused at once (router fails over) ...
    with pytest.raises(CircuitOpenError):
        await mw(_ctx(has_fallback=True), Terminal([None], clock))
    # ... a single-model chain still gets its real attempt.
    only = Terminal([None], clock)
    await mw(_ctx(has_fallback=False), only)
    assert only.calls == 1


@pytest.mark.asyncio
async def test_missing_has_fallback_means_no_fallback() -> None:
    clock = FakeClock()
    sleeper = ClockSleeper(clock)
    mw = _mw(clock, sleeper)
    terminal = Terminal([LLMServerError(), LLMServerError(), None], clock)
    await mw(MiddlewareContext(payload={"provider_key": "p:m"}), terminal)
    assert terminal.calls == 3
    assert sleeper.sleeps == [1.0, 2.0]


@pytest.mark.asyncio
async def test_with_fallback_no_retry_when_budget_would_be_exceeded() -> None:
    """A slow failure (10 s) leaves no room for another ~10 s attempt in 15 s."""
    clock = FakeClock()
    sleeper = ClockSleeper(clock)
    mw = _mw(clock, sleeper)
    terminal = Terminal([LLMServerError(), None], clock, call_s=10.0)
    with pytest.raises(LLMServerError):
        await mw(_ctx(has_fallback=True), terminal)
    assert terminal.calls == 1
    assert sleeper.sleeps == []


@pytest.mark.asyncio
async def test_with_fallback_fast_failure_retry_then_success() -> None:
    clock = FakeClock()
    sleeper = ClockSleeper(clock)
    mw = _mw(clock, sleeper)
    terminal = Terminal([LLMNetworkError(), None], clock, call_s=0.5)
    await mw(_ctx(has_fallback=True), terminal)
    assert terminal.calls == 2


# ---------------------------------------------------------------------------
# Retry-After
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retry_after_beyond_budget_with_fallback_fails_over_at_once() -> None:
    clock = FakeClock()
    sleeper = ClockSleeper(clock)
    mw = _mw(clock, sleeper)
    terminal = Terminal([_err(LLMRateLimitError, 20.0), None], clock)
    with pytest.raises(LLMRateLimitError):
        await mw(_ctx(has_fallback=True), terminal)
    assert terminal.calls == 1
    assert sleeper.sleeps == []


@pytest.mark.asyncio
async def test_retry_after_within_budget_with_fallback_is_honoured() -> None:
    clock = FakeClock()
    sleeper = ClockSleeper(clock)
    mw = _mw(clock, sleeper)
    terminal = Terminal([_err(LLMRateLimitError, 3.0), None], clock)
    await mw(_ctx(has_fallback=True), terminal)
    assert terminal.calls == 2
    assert sleeper.sleeps == [3.0]


@pytest.mark.asyncio
async def test_retry_after_without_fallback_sleeps_min_of_it_and_max_delay() -> None:
    clock = FakeClock()
    sleeper = ClockSleeper(clock)
    mw = _mw(clock, sleeper)
    terminal = Terminal([_err(LLMServerError, 45.0), _err(LLMServerError, 5.0), None], clock)
    await mw(_ctx(has_fallback=False), terminal)
    assert sleeper.sleeps == [30.0, 5.0]


@pytest.mark.asyncio
async def test_breaker_open_duration_honours_retry_after() -> None:
    clock = FakeClock()
    sleeper = ClockSleeper(clock)
    mw = _mw(clock, sleeper)
    for _ in range(3):
        with pytest.raises(LLMRateLimitError):
            await mw(_ctx(has_fallback=True), Terminal([_err(LLMRateLimitError, 120.0)], clock))
    breaker = await mw.breaker_registry.get("p:m")
    clock.now += 60.0
    assert await breaker.acquire() == "rejected"
    clock.now += 60.0
    assert await breaker.acquire() == "probe"


# ---------------------------------------------------------------------------
# Breaker accounting: every attempt counts; 4xx neutral
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_each_failed_attempt_counts_toward_breaker() -> None:
    clock = FakeClock()
    sleeper = ClockSleeper(clock)
    mw = _mw(clock, sleeper)
    with pytest.raises(LLMServerError):
        await mw(_ctx(has_fallback=True), Terminal([LLMServerError()] * 2, clock))
    breaker = await mw.breaker_registry.get("p:m")
    assert breaker.consecutive_failures == 2
    # The next call's first failure is the 3rd consecutive → OPEN; no retry.
    terminal = Terminal([LLMServerError()] * 2, clock)
    with pytest.raises(LLMServerError):
        await mw(_ctx(has_fallback=True), terminal)
    assert terminal.calls == 1
    assert await breaker.check_state() == "OPEN"
    probe = Terminal([None], clock)
    with pytest.raises(CircuitOpenError):
        await mw(_ctx(has_fallback=True), probe)
    assert probe.calls == 0


@pytest.mark.parametrize(
    "exc",
    [
        LLMStreamStaleError("no first token"),
        LLMStreamInterruptedError("died after first token"),
        LLMKeyUnavailableError("402"),
    ],
)
@pytest.mark.asyncio
async def test_non_retried_failures_still_count(exc: Exception) -> None:
    clock = FakeClock()
    sleeper = ClockSleeper(clock)
    mw = _mw(clock, sleeper)
    terminal = Terminal([exc], clock)
    with pytest.raises(type(exc)):
        await mw(_ctx(has_fallback=True), terminal)
    assert terminal.calls == 1
    breaker = await mw.breaker_registry.get("p:m")
    assert breaker.consecutive_failures == 1


@pytest.mark.asyncio
async def test_client_error_is_neutral_does_not_reset() -> None:
    clock = FakeClock()
    sleeper = ClockSleeper(clock)
    mw = _mw(clock, sleeper)
    with pytest.raises(LLMServerError):
        await mw(_ctx(has_fallback=True), Terminal([LLMServerError()] * 2, clock))
    with pytest.raises(LLMClientError):
        await mw(_ctx(has_fallback=True), Terminal([LLMClientError("400")], clock))
    breaker = await mw.breaker_registry.get("p:m")
    assert breaker.consecutive_failures == 2


@pytest.mark.asyncio
async def test_half_open_admits_one_probe_others_fail_over() -> None:
    clock = FakeClock()
    sleeper = ClockSleeper(clock)
    mw = _mw(clock, sleeper)
    with pytest.raises(LLMServerError):
        await mw(_ctx(has_fallback=True), Terminal([LLMServerError()] * 2, clock))
    with pytest.raises(LLMServerError):
        await mw(_ctx(has_fallback=True), Terminal([LLMServerError()], clock))
    clock.now += 31.0

    class _Blocking:
        """The probe parks inside the upstream call while a second caller arrives."""

        def __init__(self) -> None:
            self.calls = 0

        async def __call__(self, _ctx: MiddlewareContext) -> None:
            self.calls += 1
            second = Terminal([None], clock)
            with pytest.raises(CircuitOpenError):
                await mw(_ctx_for_second, second)
            assert second.calls == 0

    _ctx_for_second = _ctx(has_fallback=True)
    probe = _Blocking()
    await mw(_ctx(has_fallback=True), probe)
    assert probe.calls == 1
    breaker = await mw.breaker_registry.get("p:m")
    assert await breaker.check_state() == "CLOSED"


@pytest.mark.asyncio
async def test_breaker_key_prefers_breaker_key_payload() -> None:
    clock = FakeClock()
    sleeper = ClockSleeper(clock)
    mw = _mw(clock, sleeper)
    ctx = MiddlewareContext(
        payload={"provider_key": "p:m", "breaker_key": "p:m@abc", "has_fallback": True}
    )
    with pytest.raises(LLMServerError):
        await mw(ctx, Terminal([LLMServerError()] * 2, clock))
    assert (await mw.breaker_registry.get("p:m@abc")).consecutive_failures == 2
    assert (await mw.breaker_registry.get("p:m")).consecutive_failures == 0


@pytest.mark.asyncio
async def test_breaker_open_metric_labelled_by_provider_key() -> None:
    from prometheus_client import REGISTRY

    def _value() -> float:
        v = REGISTRY.get_sample_value(
            "expert_work_llm_breaker_open_total", {"provider_key": "metric:model"}
        )
        return v or 0.0

    before = _value()
    clock = FakeClock()
    sleeper = ClockSleeper(clock)
    mw = _mw(clock, sleeper)
    ctx = MiddlewareContext(
        payload={"provider_key": "metric:model", "breaker_key": "metric:model@x"}
    )
    with pytest.raises(LLMServerError):
        await mw(ctx, Terminal([LLMServerError()] * 4, clock))
    assert _value() == before + 1
