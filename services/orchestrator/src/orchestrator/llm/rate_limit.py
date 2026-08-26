"""Per-provider-key token-bucket rate limiter — Stream E.12.

Wraps any :class:`~orchestrator.llm.router.LLMProvider` with an async
leaky bucket so over-limit calls **await** instead of dispatching and
getting rejected with HTTP 429. Done at the provider layer (not the
router) because:

1. **Per-key isolation** — two ``ProviderHandle`` entries for the same
   vendor (e.g. ``anthropic:primary`` + ``anthropic:fallback``) need
   independent buckets (Mini-ADR E-4: breakers / limits are per
   upstream key, not per vendor). Wrapping at provider construction
   time gives each handle its own limiter automatically.
2. **No 429 → breaker poison** — if we let the limiter trip a 429 at
   the vendor, the E.4 ``LLMErrorHandlingMiddleware`` records a
   :class:`LLMRateLimitError` and that counts toward circuit-breaker
   failure threshold. Awaiting instead keeps the breaker clean and
   the fallback chain quiet.
3. **Composability** — :class:`RateLimitedProvider` implements the
   :class:`LLMProvider` Protocol, so it stacks transparently:
   ``LLMRouter → RateLimitedProvider → AnthropicProvider`` is one
   configuration; ``LLMRouter → RateLimitedProvider → MockProvider``
   for tests is another.

Per [STREAM-E-DESIGN § 2.4 + Mini-ADR E-4](../../../../../docs/streams/STREAM-E-DESIGN.md):
"每个 ProviderHandle 内部维护 ``aiolimiter.AsyncLimiter(rate_limit_rpm, 60)``,
超限 await (不直接抛 429)". This module realises that contract.

``aiolimiter.AsyncLimiter`` implements a leaky-bucket — tokens refill
continuously at ``max_rate / time_period`` per second, not in
discrete tick boundaries. So a `2-rpm` limiter doesn't queue 60s
between the 2nd and 3rd call; it refills at 1 token per 30s.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from typing import Self

from aiolimiter import AsyncLimiter
from langchain_core.messages import AIMessage, BaseMessage

from expert_work.protocol import StructuredOutputSpec
from orchestrator.llm.providers._streaming import LLMDelta
from orchestrator.llm.router import LLMProvider
from orchestrator.tools.registry import ToolSpec

logger = logging.getLogger(__name__)

#: 60-second sliding window — the canonical RPM denominator.
#: Test code that wants faster bucket cycles passes a custom
#: ``time_period_s`` to :meth:`RateLimitedProvider.with_rpm`.
DEFAULT_TIME_PERIOD_S = 60.0

#: PROD-12(多副本)—— 进程内令牌桶乘 N 副本 = 对上游的真实压力放大 N 倍。
#: 全局令牌桶(Redis)前的除法版:每副本配额 = ceil(rpm / 副本数),总量上界
#: 恒等于配置值(单副本空闲时另一副本吃不到它的份额 —— 保守但正确)。
#: 副本数走 env(prod overlay 与 replicas patch 同处维护);缺省 1 = 单副本
#: 语义 byte-identical。
_REPLICA_COUNT_ENV = "EXPERT_WORK_REPLICA_COUNT"


def effective_rpm(rate_limit_rpm: int) -> int:
    """Per-replica RPM share: ``ceil(rpm / replicas)``, floor 1.

    读环境变量而不是构造期快照:测试可 monkeypatch;错误值(非整数 / <1)
    按 1 处理并不抛 —— 限流配置错不该让 Agent 构建失败。
    """
    import math
    import os

    raw = os.environ.get(_REPLICA_COUNT_ENV, "1")
    try:
        replicas = max(1, int(raw))
    except ValueError:
        logger.warning("rate_limit.bad_replica_count value=%r treated_as=1", raw)
        replicas = 1
    return max(1, math.ceil(rate_limit_rpm / replicas))


@dataclass
class RateLimitedProvider:
    """Wraps an :class:`LLMProvider` with a per-instance token bucket.

    Each instance holds its own :class:`AsyncLimiter`, so creating two
    :class:`RateLimitedProvider` around the same inner provider yields
    two independent buckets — that's how primary + fallback keys for
    the same vendor stay isolated (one key getting throttled doesn't
    block the other).

    Construct via :meth:`with_rpm` for the common ``rpm-per-60s`` case;
    pass a hand-built :class:`AsyncLimiter` directly when test code
    needs a non-default ``time_period_s`` to keep tests fast.
    """

    inner: LLMProvider
    limiter: AsyncLimiter

    @classmethod
    def with_rpm(
        cls,
        inner: LLMProvider,
        *,
        rate_limit_rpm: int,
        time_period_s: float = DEFAULT_TIME_PERIOD_S,
    ) -> Self:
        """Build a :class:`RateLimitedProvider` from an RPM number.

        ``rate_limit_rpm`` tokens refill across ``time_period_s``
        seconds. Tests can shorten ``time_period_s`` to validate
        bucket behaviour without 60-second waits — the ratio is what
        matters, not the absolute window.
        """
        if rate_limit_rpm <= 0:
            raise ValueError(f"rate_limit_rpm must be positive, got {rate_limit_rpm!r}")
        if time_period_s <= 0:
            raise ValueError(f"time_period_s must be positive, got {time_period_s!r}")
        return cls(
            inner=inner,
            limiter=AsyncLimiter(max_rate=rate_limit_rpm, time_period=time_period_s),
        )

    async def complete(
        self,
        *,
        messages: Sequence[BaseMessage],
        tools: Sequence[ToolSpec],
        output_schema: StructuredOutputSpec | None = None,
    ) -> AIMessage:
        """Acquire a token, then delegate to the wrapped provider.

        Exceptions from the inner provider propagate **unchanged** —
        the limiter only governs admission, not error semantics. This
        keeps the E.4 ``LLMErrorHandlingMiddleware`` classification +
        E.11 ``LLMRouter`` fallback logic intact.

        ``output_schema`` (Stream RT-1) is forwarded only when set so a
        pre-RT-1 inner provider (without the parameter) keeps working
        on unstructured calls.

        Acquisition is **fair** in the sense that aiolimiter wakes
        waiters in roughly arrival order, but ``aiolimiter`` does not
        guarantee strict FIFO — under high contention the throughput
        property is what we care about, not request order.
        """
        async with self.limiter:
            if output_schema is None:
                return await self.inner.complete(messages=messages, tools=tools)
            return await self.inner.complete(
                messages=messages, tools=tools, output_schema=output_schema
            )

    async def stream(
        self,
        *,
        messages: Sequence[BaseMessage],
        tools: Sequence[ToolSpec],
        output_schema: StructuredOutputSpec | None = None,
    ) -> AsyncIterator[LLMDelta]:
        """Acquire a token (admission), then delegate to the inner
        provider's stream. Only called when the inner provider is
        streaming-capable (the router probes via ``supports_streaming``).
        The token bucket gates admission; ``aiolimiter`` does not refund
        on exit, so spanning the iteration is harmless."""
        async with self.limiter:
            if output_schema is None:
                inner_stream = self.inner.stream(messages=messages, tools=tools)  # type: ignore[attr-defined]
            else:
                inner_stream = self.inner.stream(  # type: ignore[attr-defined]
                    messages=messages, tools=tools, output_schema=output_schema
                )
            async for delta in inner_stream:
                yield delta
