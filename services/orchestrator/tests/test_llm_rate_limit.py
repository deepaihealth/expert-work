"""Unit tests for :class:`RateLimitedProvider` (Stream E.12).

Covers the test-matrix #23 supplement from STREAM-E-DESIGN § 5:

- bucket exhaustion forces concurrent calls to await (≥ expected delay)
- low-rate usage incurs no delay
- exceptions propagate unchanged
- protocol contract is satisfied

Tests use a small ``time_period_s`` (≤ 1s) so the bucket-refill window
is CI-friendly. The spec's "rate_limit_rpm=2 + 5 concurrent → ≥ 60s"
shape is preserved as a property test (5 calls / 2 tokens-per-window =
≥ 1 full window of waiting on the slowest call), just at a faster
scale.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Sequence
from dataclasses import dataclass, field

import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage

from expert_work.runtime.middleware import LLMServerError
from orchestrator.llm import (
    DEFAULT_TIME_PERIOD_S,
    LLMProvider,
    RateLimitedProvider,
    effective_rpm,
)
from orchestrator.tools.registry import ToolSpec

# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


@dataclass
class _CountingProvider:
    """Records every call's monotonic timestamp so tests can verify
    bucket-driven spacing rather than just call ordering."""

    response: AIMessage = field(default_factory=lambda: AIMessage(content="ok"))
    raise_with: BaseException | None = None
    call_times: list[float] = field(default_factory=list)

    async def complete(
        self,
        *,
        messages: Sequence[BaseMessage],
        tools: Sequence[ToolSpec],
    ) -> AIMessage:
        self.call_times.append(time.monotonic())
        if self.raise_with is not None:
            raise self.raise_with
        return self.response


def _msgs() -> list[BaseMessage]:
    return [HumanMessage(content="hi")]


# ---------------------------------------------------------------------------
# Bucket exhaustion — the main load-bearing property
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_five_concurrent_calls_at_rpm_2_take_at_least_one_window() -> None:
    """rate=2, 5 concurrent → first 2 instant, then 3 more refill over
    the window. Total wall clock must be ≥ ``time_period_s`` (the 3rd
    call has to wait for the first refill).

    Mirrors the design-doc's spec test (rpm=2, 5 concurrent → ≥ 60s)
    at compressed time scale so CI stays fast."""
    inner = _CountingProvider()
    window_s = 0.5
    wrapped = RateLimitedProvider.with_rpm(
        inner,
        rate_limit_rpm=2,
        time_period_s=window_s,
    )

    start = time.monotonic()
    await asyncio.gather(*[wrapped.complete(messages=_msgs(), tools=[]) for _ in range(5)])
    elapsed = time.monotonic() - start

    assert len(inner.call_times) == 5
    assert elapsed >= window_s, (
        f"expected ≥ {window_s}s for 5 calls at rate=2 over {window_s}s window, got {elapsed:.3f}s"
    )


@pytest.mark.asyncio
async def test_calls_within_burst_capacity_dispatch_immediately() -> None:
    """If we stay within the bucket's capacity (= ``rate_limit_rpm``),
    we should pay essentially zero wait — verify the limiter doesn't
    serialise calls that fit in the bucket."""
    inner = _CountingProvider()
    wrapped = RateLimitedProvider.with_rpm(inner, rate_limit_rpm=10, time_period_s=1.0)

    start = time.monotonic()
    await asyncio.gather(*[wrapped.complete(messages=_msgs(), tools=[]) for _ in range(5)])
    elapsed = time.monotonic() - start

    # 5 calls in a 10-rpm-per-second bucket — bucket starts full, no waits.
    # Allow generous slack for asyncio scheduling overhead on slow CI.
    assert elapsed < 0.3, f"expected near-instant dispatch, got {elapsed:.3f}s"


@pytest.mark.asyncio
async def test_serial_calls_well_under_rate_no_delay() -> None:
    """Sequential calls spaced well below the rate should never block."""
    inner = _CountingProvider()
    wrapped = RateLimitedProvider.with_rpm(inner, rate_limit_rpm=60, time_period_s=1.0)

    start = time.monotonic()
    for _ in range(3):
        await wrapped.complete(messages=_msgs(), tools=[])
    elapsed = time.monotonic() - start

    assert elapsed < 0.3
    assert len(inner.call_times) == 3


# ---------------------------------------------------------------------------
# Per-instance bucket isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_two_wrappers_have_independent_buckets() -> None:
    """Two ``RateLimitedProvider`` instances around the same inner —
    or different inners — must NOT share a bucket. This is how
    primary + fallback keys for one vendor stay isolated."""
    inner_a = _CountingProvider()
    inner_b = _CountingProvider()
    wrapped_a = RateLimitedProvider.with_rpm(inner_a, rate_limit_rpm=1, time_period_s=0.5)
    wrapped_b = RateLimitedProvider.with_rpm(inner_b, rate_limit_rpm=1, time_period_s=0.5)

    start = time.monotonic()
    # Both wrappers fire concurrently — each has 1 token, both should
    # dispatch instantly. If they shared a bucket, one would wait.
    await asyncio.gather(
        wrapped_a.complete(messages=_msgs(), tools=[]),
        wrapped_b.complete(messages=_msgs(), tools=[]),
    )
    elapsed = time.monotonic() - start

    assert elapsed < 0.1, (
        f"two independent rate=1 buckets should both dispatch instantly; "
        f"got {elapsed:.3f}s — likely sharing state"
    )


# ---------------------------------------------------------------------------
# Error semantics — limiter doesn't perturb the inner provider's exceptions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_inner_exception_propagates_unchanged() -> None:
    boom = LLMServerError("simulated 503")
    inner = _CountingProvider(raise_with=boom)
    wrapped = RateLimitedProvider.with_rpm(inner, rate_limit_rpm=60)

    with pytest.raises(LLMServerError) as exc_info:
        await wrapped.complete(messages=_msgs(), tools=[])

    assert exc_info.value is boom
    # Token was acquired before the inner call; that's expected — the
    # limiter governs admission, not error semantics.
    assert len(inner.call_times) == 1


@pytest.mark.asyncio
async def test_token_consumed_even_when_inner_raises() -> None:
    """A failed call still consumes a token from the bucket. This is
    by design — the request hit the upstream provider regardless of
    outcome, so it should count toward the rate budget. (If retries
    are wanted, they go through the E.4 middleware which is
    OUTSIDE the limiter wrapper.)"""
    inner = _CountingProvider(raise_with=LLMServerError("503"))
    wrapped = RateLimitedProvider.with_rpm(inner, rate_limit_rpm=2, time_period_s=0.5)

    # 2 calls fail, exhausting the bucket; 3rd call must wait for refill.
    for _ in range(2):
        with pytest.raises(LLMServerError):
            await wrapped.complete(messages=_msgs(), tools=[])

    start = time.monotonic()
    inner.raise_with = None  # let the 3rd call succeed
    inner.response = AIMessage(content="recovered")
    result = await wrapped.complete(messages=_msgs(), tools=[])
    elapsed = time.monotonic() - start

    assert result.content == "recovered"
    # Should have waited for at least one refill tick.
    assert elapsed >= 0.2, f"expected refill wait, got {elapsed:.3f}s"


# ---------------------------------------------------------------------------
# Construction / Protocol contract
# ---------------------------------------------------------------------------


def test_wrapper_satisfies_llm_provider_protocol() -> None:
    """The whole point of the wrapper is composability — it must look
    like an ``LLMProvider`` to the router."""
    inner = _CountingProvider()
    wrapped = RateLimitedProvider.with_rpm(inner, rate_limit_rpm=60)
    assert isinstance(wrapped, LLMProvider)


def test_with_rpm_default_time_period_is_60s() -> None:
    """RPM-per-60s is the canonical denominator — defaulting elsewhere
    would silently distort the rate semantics."""
    inner = _CountingProvider()
    wrapped = RateLimitedProvider.with_rpm(inner, rate_limit_rpm=10)
    assert wrapped.limiter.time_period == 60.0
    assert wrapped.limiter.max_rate == 10
    assert DEFAULT_TIME_PERIOD_S == 60.0


def test_with_rpm_rejects_non_positive_rate() -> None:
    inner = _CountingProvider()
    with pytest.raises(ValueError, match="rate_limit_rpm must be positive"):
        RateLimitedProvider.with_rpm(inner, rate_limit_rpm=0)
    with pytest.raises(ValueError, match="rate_limit_rpm must be positive"):
        RateLimitedProvider.with_rpm(inner, rate_limit_rpm=-1)


def test_with_rpm_rejects_non_positive_window() -> None:
    inner = _CountingProvider()
    with pytest.raises(ValueError, match="time_period_s must be positive"):
        RateLimitedProvider.with_rpm(inner, rate_limit_rpm=60, time_period_s=0.0)
    with pytest.raises(ValueError, match="time_period_s must be positive"):
        RateLimitedProvider.with_rpm(inner, rate_limit_rpm=60, time_period_s=-1.0)


def test_direct_construction_with_custom_limiter_works() -> None:
    """The ``inner`` + ``limiter`` constructor path supports power
    users who want to share a limiter across wrappers or use an
    AsyncLimiter built with non-standard parameters."""
    from aiolimiter import AsyncLimiter

    inner = _CountingProvider()
    custom = AsyncLimiter(max_rate=5, time_period=10)
    wrapped = RateLimitedProvider(inner=inner, limiter=custom)
    assert wrapped.limiter is custom


# ---------------------------------------------------------------------------
# PROD-12(多副本)—— 每副本 RPM 份额
# ---------------------------------------------------------------------------


def test_effective_rpm_defaults_to_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EXPERT_WORK_REPLICA_COUNT", raising=False)
    assert effective_rpm(60) == 60


def test_effective_rpm_divides_by_replica_count(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXPERT_WORK_REPLICA_COUNT", "2")
    assert effective_rpm(60) == 30
    # ceil —— 宁可各副本多 1,不把总量削到配置值以下太多;下限 1。
    monkeypatch.setenv("EXPERT_WORK_REPLICA_COUNT", "3")
    assert effective_rpm(10) == 4
    assert effective_rpm(1) == 1


def test_effective_rpm_survives_bad_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # 限流配置错不该让 Agent 构建失败 —— 按单副本处理。
    monkeypatch.setenv("EXPERT_WORK_REPLICA_COUNT", "two")
    assert effective_rpm(60) == 60
    monkeypatch.setenv("EXPERT_WORK_REPLICA_COUNT", "0")
    assert effective_rpm(60) == 60
