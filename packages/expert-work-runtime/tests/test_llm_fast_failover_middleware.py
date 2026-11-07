"""B-297 — LLMErrorHandlingMiddleware fails over fast when a backup exists.

The router tells the middleware whether a later provider group can still
serve (``ctx.payload["has_fallback"]``). With a backup: at most one
same-provider retry, a short jittered backoff and a 15 s per-provider budget.
Without one: the long-standing 3-retry exponential behaviour. Every failed
upstream attempt counts toward the breaker.

Fake clock + recording sleeper (the sleeper advances the clock) — no real sleeps.
"""

from __future__ import annotations

import asyncio
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
    notify_first_progress,
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
    # ... and its success is evidence the upstream answers: the breaker closes.
    assert await breaker.check_state() == "CLOSED"


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
async def test_retry_after_longer_than_fast_backoff_fails_over_at_once() -> None:
    """With a backup, a Retry-After above the fast-path max delay (1 s) is not
    waited out on the primary — the backup serves now (LiteLLM behaviour)."""
    clock = FakeClock()
    sleeper = ClockSleeper(clock)
    mw = _mw(clock, sleeper)
    terminal = Terminal([_err(LLMServerError, 3.0), None], clock)
    with pytest.raises(LLMServerError):
        await mw(_ctx(has_fallback=True), terminal)
    assert terminal.calls == 1
    assert sleeper.sleeps == []


@pytest.mark.asyncio
async def test_short_retry_after_with_fallback_is_honoured() -> None:
    clock = FakeClock()
    sleeper = ClockSleeper(clock)
    mw = _mw(clock, sleeper)
    terminal = Terminal([_err(LLMServerError, 0.8), None], clock)
    await mw(_ctx(has_fallback=True), terminal)
    assert terminal.calls == 2
    assert sleeper.sleeps == [0.8]


@pytest.mark.asyncio
async def test_retry_after_without_fallback_capped_at_max_delay() -> None:
    clock = FakeClock()
    sleeper = ClockSleeper(clock)
    mw = _mw(clock, sleeper)
    terminal = Terminal([_err(LLMServerError, 45.0), _err(LLMServerError, 5.0), None], clock)
    await mw(_ctx(has_fallback=False), terminal)
    assert sleeper.sleeps == [30.0, 5.0]


@pytest.mark.asyncio
async def test_retry_after_without_fallback_never_below_exponential_floor() -> None:
    """A tiny Retry-After does not shorten the single-model backoff."""
    clock = FakeClock()
    sleeper = ClockSleeper(clock)
    mw = _mw(clock, sleeper)
    seq: list[Exception | None] = [_err(LLMServerError, 0.1), _err(LLMServerError, 0.1), None]
    await mw(_ctx(has_fallback=False), Terminal(seq, clock))
    assert sleeper.sleeps == [1.0, 2.0]


@pytest.mark.asyncio
async def test_retry_after_without_fallback_total_wait_capped() -> None:
    """Single-model chain: Retry-After waits stop once 60 s in total would pass."""
    clock = FakeClock()
    sleeper = ClockSleeper(clock)
    mw = _mw(clock, sleeper)
    terminal = Terminal([_err(LLMServerError, 25.0)] * 4, clock)
    with pytest.raises(LLMServerError):
        await mw(_ctx(has_fallback=False), terminal)
    assert sleeper.sleeps == [25.0, 25.0]
    assert terminal.calls == 3


# ---------------------------------------------------------------------------
# Sibling keys (Y-MK): another key of the same group is an alternative
# ---------------------------------------------------------------------------


def _sibling_ctx() -> MiddlewareContext:
    return MiddlewareContext(
        payload={"provider_key": "p:m#0", "has_fallback": False, "has_alternative": True}
    )


@pytest.mark.asyncio
async def test_open_breaker_refused_when_only_a_sibling_key_remains() -> None:
    clock = FakeClock()
    sleeper = ClockSleeper(clock)
    mw = _mw(clock, sleeper)
    breaker = await mw.breaker_registry.get("p:m#0")
    for _ in range(3):
        await breaker.acquire()
        await breaker.record_failure()
    terminal = Terminal([None], clock)
    with pytest.raises(CircuitOpenError):
        await mw(_sibling_ctx(), terminal)
    assert terminal.calls == 0


@pytest.mark.parametrize("ctx_factory", [_sibling_ctx, lambda: _ctx(has_fallback=True)])
@pytest.mark.asyncio
async def test_rate_limit_not_retried_in_place_when_an_alternative_exists(
    ctx_factory: object,
) -> None:
    clock = FakeClock()
    sleeper = ClockSleeper(clock)
    mw = _mw(clock, sleeper)
    terminal = Terminal([LLMRateLimitError("429"), None], clock)
    with pytest.raises(LLMRateLimitError):
        await mw(ctx_factory(), terminal)  # type: ignore[operator]
    assert terminal.calls == 1
    assert sleeper.sleeps == []


@pytest.mark.asyncio
async def test_server_error_with_only_siblings_keeps_full_budget() -> None:
    """A 5xx hits every sibling key alike, so siblings don't shorten the budget."""
    clock = FakeClock()
    sleeper = ClockSleeper(clock)
    mw = _mw(clock, sleeper)
    terminal = Terminal([LLMServerError(), LLMServerError(), None], clock)
    await mw(_sibling_ctx(), terminal)
    assert sleeper.sleeps == [1.0, 2.0]


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
    slept = list(sleeper.sleeps)
    # The next call's first failure is the 3rd consecutive → OPEN; no retry,
    # and no backoff sleep before giving up either.
    terminal = Terminal([LLMServerError()] * 2, clock)
    with pytest.raises(LLMServerError):
        await mw(_ctx(has_fallback=True), terminal)
    assert terminal.calls == 1
    assert sleeper.sleeps == slept
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


# ---------------------------------------------------------------------------
# Half-open probe resolution
# ---------------------------------------------------------------------------


async def _open_then_half_open(mw: LLMErrorHandlingMiddleware, clock: FakeClock) -> None:
    breaker = await mw.breaker_registry.get("p:m")
    for _ in range(3):
        await breaker.acquire()
        await breaker.record_failure()
    clock.now += 31.0


@pytest.mark.parametrize("make_exc", [asyncio.CancelledError, lambda: RuntimeError("adapter bug")])
@pytest.mark.asyncio
async def test_probe_ending_without_an_llm_outcome_frees_the_slot(make_exc: object) -> None:
    clock = FakeClock()
    mw = _mw(clock, ClockSleeper(clock))
    await _open_then_half_open(mw, clock)
    breaker = await mw.breaker_registry.get("p:m")

    async def _boom(_ctx: MiddlewareContext) -> None:
        assert await breaker.acquire() == "rejected"  # we are the probe
        raise make_exc()  # type: ignore[operator]

    with pytest.raises((asyncio.CancelledError, RuntimeError)):
        await mw(_ctx(has_fallback=True), _boom)
    assert await breaker.acquire() == "probe"


@pytest.mark.asyncio
async def test_cancelled_hung_probe_frees_the_slot() -> None:
    clock = FakeClock()
    mw = _mw(clock, ClockSleeper(clock))
    await _open_then_half_open(mw, clock)
    breaker = await mw.breaker_registry.get("p:m")
    started = asyncio.Event()

    async def _hang(_ctx: MiddlewareContext) -> None:
        started.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(mw(_ctx(has_fallback=True), _hang))
    await started.wait()
    assert await breaker.acquire() == "rejected"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await breaker.acquire() == "probe"


@pytest.mark.asyncio
async def test_probe_resolves_at_first_progress_not_at_stream_end() -> None:
    """The probe's first token proves the upstream answers: the breaker closes
    then, not after a long generation; a later stream failure counts as an
    ordinary failure."""
    clock = FakeClock()
    mw = _mw(clock, ClockSleeper(clock))
    await _open_then_half_open(mw, clock)
    breaker = await mw.breaker_registry.get("p:m")

    async def _streams(_ctx: MiddlewareContext) -> None:
        await notify_first_progress()
        assert await breaker.check_state() == "CLOSED"
        assert await breaker.acquire() == "allowed"
        raise LLMStreamInterruptedError("died mid-generation")

    with pytest.raises(LLMStreamInterruptedError):
        await mw(_ctx(has_fallback=True), _streams)
    assert await breaker.check_state() == "CLOSED"
    assert breaker.consecutive_failures == 1


@pytest.mark.asyncio
async def test_first_progress_outside_a_probe_is_a_no_op() -> None:
    clock = FakeClock()
    mw = _mw(clock, ClockSleeper(clock))
    await notify_first_progress()  # no middleware around: must not raise

    async def _ok(_ctx: MiddlewareContext) -> None:
        await notify_first_progress()

    await mw(_ctx(has_fallback=True), _ok)
    breaker = await mw.breaker_registry.get("p:m")
    assert await breaker.check_state() == "CLOSED"


@pytest.mark.asyncio
async def test_first_progress_hook_does_not_outlive_the_attempt() -> None:
    from expert_work.runtime.middleware.llm_error_handling import _first_progress_hook

    clock = FakeClock()
    mw = _mw(clock, ClockSleeper(clock))
    await _open_then_half_open(mw, clock)
    with pytest.raises(LLMServerError):
        await mw(_ctx(has_fallback=True), Terminal([LLMServerError()], clock))
    assert _first_progress_hook.get() is None


@pytest.mark.asyncio
async def test_first_progress_of_a_normal_attempt_does_not_count_as_success() -> None:
    """Only a probe resolves early; an ordinary attempt that streams and then
    dies is one more failure on top of the earlier ones."""
    clock = FakeClock()
    mw = _mw(clock, ClockSleeper(clock))
    breaker = await mw.breaker_registry.get("p:m")
    for _ in range(2):
        await breaker.acquire()
        await breaker.record_failure()

    async def _streams_then_dies(_ctx: MiddlewareContext) -> None:
        await notify_first_progress()
        raise LLMStreamInterruptedError("died mid-generation")

    with pytest.raises(LLMStreamInterruptedError):
        await mw(_ctx(has_fallback=True), _streams_then_dies)
    assert breaker.consecutive_failures == 3
    assert await breaker.check_state() == "OPEN"


@pytest.mark.asyncio
async def test_task_spawned_in_a_probe_cannot_resolve_it_after_the_attempt() -> None:
    clock = FakeClock()
    mw = _mw(clock, ClockSleeper(clock))
    await _open_then_half_open(mw, clock)
    breaker = await mw.breaker_registry.get("p:m")
    go = asyncio.Event()
    children: list[asyncio.Task[None]] = []

    async def _late_notify() -> None:
        await go.wait()
        await notify_first_progress()

    async def _spawns_then_fails(_ctx: MiddlewareContext) -> None:
        children.append(asyncio.create_task(_late_notify()))
        raise LLMServerError("probe failed")

    with pytest.raises(LLMServerError):
        await mw(_ctx(has_fallback=True), _spawns_then_fails)
    assert await breaker.check_state() == "OPEN"
    go.set()
    await children[0]
    assert await breaker.check_state() == "OPEN"


@pytest.mark.asyncio
async def test_probe_lease_follows_the_routers_attempt_deadline() -> None:
    clock = FakeClock()
    mw = _mw(clock, ClockSleeper(clock))
    await _open_then_half_open(mw, clock)
    breaker = await mw.breaker_registry.get("p:m")
    checked: list[str] = []

    async def _slow_but_healthy(_ctx: MiddlewareContext) -> None:
        clock.now += 100.0  # beyond the 60 s default lease, within 120 s
        checked.append(await breaker.check_state())

    ctx = MiddlewareContext(
        payload={"provider_key": "p:m", "has_fallback": True, "attempt_deadline_s": 120.0}
    )
    await mw(ctx, _slow_but_healthy)
    assert checked == ["HALF_OPEN"]
    assert await breaker.check_state() == "CLOSED"
