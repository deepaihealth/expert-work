"""In-process token-bucket :class:`RateLimiter` — M0 only.

ADR B-1 calls out that this implementation assumes **one control-plane
replica**: every replica owns an independent bucket, so deploying two
behind a single LB would defeat the limit. Stream C.6 replaces this with
a Redis-backed atomic implementation (:class:`RedisTokenBucketLimiter`);
``create_app``'s ``lifespan`` (W1-PR2 Task 5) enforces the constraint at
startup — ``single_instance=False`` with no ``EXPERT_WORK_QUOTA_REDIS_URL``
raises ``RuntimeError`` instead of silently falling back to this
in-process bucket.

The bucket map is bounded only by unique ``(dimension, key)`` pairs. For
dev / single-tenant M0 traffic this is fine; if it ever grows we'll
either evict (LRU) or migrate to Redis early.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass

from control_plane.ratelimit.base import RateLimitDecision

Clock = Callable[[], int]


def _default_clock() -> int:
    """Return current time as milliseconds since the epoch."""
    return time.monotonic_ns() // 1_000_000


@dataclass
class _TokenBucket:
    tokens: float
    last_refill_ms: int


class InProcessTokenBucketLimiter:
    """Asyncio-safe token bucket keyed by ``(dimension, key)``."""

    def __init__(
        self,
        *,
        capacity: int,
        refill_per_sec: float,
        clock: Clock | None = None,
    ) -> None:
        if capacity <= 0:
            msg = f"capacity must be > 0, got {capacity}"
            raise ValueError(msg)
        if refill_per_sec <= 0:
            msg = f"refill_per_sec must be > 0, got {refill_per_sec}"
            raise ValueError(msg)
        self._capacity = float(capacity)
        self._refill_per_sec = float(refill_per_sec)
        self._clock = clock or _default_clock
        self._buckets: dict[tuple[str, str], _TokenBucket] = {}
        self._lock = asyncio.Lock()

    async def acquire(
        self,
        *,
        dimension: str,
        key: str,
        capacity: int | None = None,
        refill_per_sec: float | None = None,
    ) -> RateLimitDecision:
        # Per-call override (Stream C.6 rate_limit_override) falls back to the
        # limiter's configured defaults.
        cap = float(capacity) if capacity is not None else self._capacity
        refill = refill_per_sec if refill_per_sec is not None else self._refill_per_sec
        async with self._lock:
            now_ms = self._clock()
            bucket = self._buckets.get((dimension, key))
            if bucket is None:
                bucket = _TokenBucket(tokens=cap, last_refill_ms=now_ms)
                self._buckets[(dimension, key)] = bucket

            elapsed_s = max(0, now_ms - bucket.last_refill_ms) / 1000.0
            bucket.tokens = min(cap, bucket.tokens + elapsed_s * refill)
            bucket.last_refill_ms = now_ms

            if bucket.tokens >= 1.0:
                bucket.tokens -= 1.0
                return RateLimitDecision(
                    allowed=True,
                    retry_after_s=0.0,
                    remaining=bucket.tokens,
                )

            deficit = 1.0 - bucket.tokens
            retry_after_s = deficit / refill
            return RateLimitDecision(
                allowed=False,
                retry_after_s=retry_after_s,
                remaining=bucket.tokens,
            )
