"""B-297 — the per-upstream LLM circuit breaker trips like the industry default.

* OPEN on 3 consecutive failed attempts, or a >=50 % failure rate over >=5
  attempts inside a 60 s sliding window.
* cooldown 30 s, doubling on each consecutive re-open, capped at 300 s; back to
  30 s once a half-open probe succeeds; a vendor Retry-After stretches it.
* HALF_OPEN admits exactly one in-flight probe.

All timing uses an injected fake clock — no real sleeps.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

import pytest

from expert_work.runtime.middleware import BreakerRegistry, CircuitBreaker


@dataclass
class FakeClock:
    now: float = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _breaker(clock: FakeClock) -> CircuitBreaker:
    return CircuitBreaker(clock=clock)


async def _fail(breaker: CircuitBreaker, n: int, **kwargs: object) -> list[bool]:
    opened: list[bool] = []
    for _ in range(n):
        assert await breaker.acquire() != "rejected"
        opened.append(await breaker.record_failure(**kwargs))  # type: ignore[arg-type]
    return opened


# ---------------------------------------------------------------------------
# Trip conditions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_three_consecutive_failures_open() -> None:
    clock = FakeClock()
    breaker = _breaker(clock)
    assert await _fail(breaker, 3) == [False, False, True]
    assert await breaker.check_state() == "OPEN"
    assert await breaker.acquire() == "rejected"


@pytest.mark.asyncio
async def test_two_failures_then_success_stays_closed() -> None:
    clock = FakeClock()
    breaker = _breaker(clock)
    await _fail(breaker, 2)
    assert await breaker.acquire() == "allowed"
    await breaker.record_success()
    # Out of the rate window, so only the consecutive count can trip it.
    clock.advance(61.0)
    assert await _fail(breaker, 2) == [False, False]
    assert await breaker.check_state() == "CLOSED"


@pytest.mark.asyncio
async def test_failure_rate_over_window_opens_without_three_in_a_row() -> None:
    """F S F S F → 3/5 = 60 % with 5 attempts → OPEN, although never 3 in a row."""
    clock = FakeClock()
    breaker = _breaker(clock)
    outcomes = ["F", "S", "F", "S", "F"]
    opened = False
    for outcome in outcomes:
        assert await breaker.acquire() == "allowed"
        clock.advance(1.0)
        if outcome == "F":
            opened = await breaker.record_failure()
        else:
            await breaker.record_success()
    assert opened is True
    assert await breaker.check_state() == "OPEN"


@pytest.mark.asyncio
async def test_failure_rate_needs_minimum_attempts() -> None:
    """F S F S → 50 % but only 4 attempts → stays CLOSED."""
    clock = FakeClock()
    breaker = _breaker(clock)
    for outcome in ["F", "S", "F", "S"]:
        await breaker.acquire()
        if outcome == "F":
            await breaker.record_failure()
        else:
            await breaker.record_success()
    assert await breaker.check_state() == "CLOSED"


@pytest.mark.asyncio
async def test_failure_rate_window_slides() -> None:
    """Old failures fall out of the 60 s window and stop counting."""
    clock = FakeClock()
    breaker = _breaker(clock)
    for outcome in ["F", "S", "F", "S"]:
        await breaker.acquire()
        if outcome == "F":
            await breaker.record_failure()
        else:
            await breaker.record_success()
    clock.advance(61.0)
    # Window now empty; one S + one F is 1/2 with 2 attempts → no trip.
    await breaker.acquire()
    await breaker.record_success()
    await breaker.acquire()
    assert await breaker.record_failure() is False
    assert await breaker.check_state() == "CLOSED"


@pytest.mark.asyncio
async def test_neutral_release_does_not_reset_consecutive_count() -> None:
    """A 4xx (neutral) between failures neither resets nor counts."""
    clock = FakeClock()
    breaker = _breaker(clock)
    await _fail(breaker, 2)
    await breaker.acquire()
    await breaker.release(probe=False)
    assert breaker.consecutive_failures == 2
    assert await _fail(breaker, 1) == [True]


# ---------------------------------------------------------------------------
# Cooldown + exponential re-open
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cooldown_30s_then_single_probe() -> None:
    clock = FakeClock()
    breaker = _breaker(clock)
    await _fail(breaker, 3)
    clock.advance(29.9)
    assert await breaker.acquire() == "rejected"
    clock.advance(0.2)
    assert await breaker.acquire() == "probe"
    # A second concurrent caller is refused while the probe is in flight.
    assert await breaker.acquire() == "rejected"
    assert await breaker.acquire() == "rejected"


@pytest.mark.asyncio
async def test_probe_success_closes_and_resets_cooldown() -> None:
    clock = FakeClock()
    breaker = _breaker(clock)
    await _fail(breaker, 3)
    clock.advance(30.0)
    assert await breaker.acquire() == "probe"
    await breaker.record_success(probe=True)
    assert await breaker.check_state() == "CLOSED"
    assert breaker.consecutive_failures == 0
    # Re-open later → cooldown is back to 30 s, not doubled.
    await _fail(breaker, 3)
    clock.advance(30.0)
    assert await breaker.acquire() == "probe"


@pytest.mark.asyncio
async def test_probe_failure_reopens_with_doubled_cooldown_capped_at_300() -> None:
    clock = FakeClock()
    breaker = _breaker(clock)
    await _fail(breaker, 3)
    expected = [60.0, 120.0, 240.0, 300.0, 300.0]
    clock.advance(30.0)
    for cooldown in expected:
        assert await breaker.acquire() == "probe"
        assert await breaker.record_failure(probe=True) is True
        clock.advance(cooldown - 0.1)
        assert await breaker.acquire() == "rejected", cooldown
        clock.advance(0.1)
    assert await breaker.acquire() == "probe"


@pytest.mark.asyncio
async def test_neutral_probe_outcome_frees_the_probe_slot() -> None:
    clock = FakeClock()
    breaker = _breaker(clock)
    await _fail(breaker, 3)
    clock.advance(30.0)
    assert await breaker.acquire() == "probe"
    await breaker.release(probe=True)
    assert await breaker.check_state() == "HALF_OPEN"
    assert await breaker.acquire() == "probe"


@pytest.mark.asyncio
async def test_retry_after_stretches_open_duration_capped() -> None:
    clock = FakeClock()
    breaker = _breaker(clock)
    await _fail(breaker, 2)
    assert await breaker.record_failure(retry_after_s=90.0) is True
    clock.advance(89.0)
    assert await breaker.acquire() == "rejected"
    clock.advance(1.0)
    assert await breaker.acquire() == "probe"


@pytest.mark.asyncio
async def test_retry_after_open_duration_capped_at_300() -> None:
    clock = FakeClock()
    breaker = _breaker(clock)
    await _fail(breaker, 2)
    await breaker.record_failure(retry_after_s=3600.0)
    clock.advance(300.0)
    assert await breaker.acquire() == "probe"


@pytest.mark.asyncio
async def test_late_non_probe_failure_in_half_open_does_not_reopen() -> None:
    """A call admitted before the breaker opened, failing during HALF_OPEN, is
    stale evidence — only the probe decides."""
    clock = FakeClock()
    breaker = _breaker(clock)
    await _fail(breaker, 3)
    clock.advance(30.0)
    assert await breaker.acquire() == "probe"
    assert await breaker.record_failure(probe=False) is False
    await breaker.record_success(probe=True)
    assert await breaker.check_state() == "CLOSED"


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_registry_returns_same_breaker_per_key() -> None:
    registry = BreakerRegistry()
    a1 = await registry.get("a")
    a2 = await registry.get("a")
    b = await registry.get("b")
    assert a1 is a2
    assert a1 is not b


def test_registry_is_safe_across_threads_and_loops() -> None:
    """One process-wide registry serves agents on any loop/thread."""
    import asyncio

    registry = BreakerRegistry()
    seen: list[CircuitBreaker] = []

    def _worker() -> None:
        seen.append(asyncio.run(registry.get("shared")))

    threads = [threading.Thread(target=_worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len({id(b) for b in seen}) == 1
