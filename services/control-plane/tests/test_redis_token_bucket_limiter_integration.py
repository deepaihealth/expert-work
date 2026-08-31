"""Integration test for :class:`RedisTokenBucketLimiter` — Stream C.6.

Spins up a real Redis 7 container, hits the Lua bucket end-to-end, and
verifies:

* Burst exhaustion: capacity=N → N successful acquires, N+1 denied.
* Refill: after sleeping ``> 1 / refill_per_sec`` seconds the bucket
  has at least one token back.
* Bucket isolation: ``(dim_a, key)`` and ``(dim_b, key)`` are separate
  hashes; draining one does not drain the other.
* ``NOSCRIPT`` resilience: forcibly flushing the Lua cache between
  calls still serves the next request (the limiter re-loads + retries).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator

import pytest
import redis.asyncio as redis_async
from testcontainers.redis import RedisContainer

from control_plane.ratelimit import RedisTokenBucketLimiter

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def redis_container() -> Iterator[RedisContainer]:
    with RedisContainer("public.ecr.aws/docker/library/redis:7-alpine") as container:
        yield container


def _redis_url(container: RedisContainer) -> str:
    host = container.get_container_host_ip()
    port = container.get_exposed_port(6379)
    return f"redis://{host}:{port}/0"


@pytest.fixture
async def redis_client(redis_container: RedisContainer) -> AsyncIterator[redis_async.Redis]:
    client = redis_async.from_url(
        _redis_url(redis_container), encoding="utf-8", decode_responses=True
    )
    try:
        # Clean keyspace between tests so each one starts from zero.
        await client.flushdb()
        yield client
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_burst_then_deny_then_refill(redis_client: redis_async.Redis) -> None:
    limiter = RedisTokenBucketLimiter(
        redis_client=redis_client,
        capacity=3,
        refill_per_sec=5.0,  # 1 token every 200 ms
    )

    # First 3 calls drain the bucket.
    for _ in range(3):
        decision = await limiter.acquire(dimension="tenant", key="t-1")
        assert decision.allowed is True

    # 4th is denied.
    denied = await limiter.acquire(dimension="tenant", key="t-1")
    assert denied.allowed is False
    assert denied.retry_after_s > 0

    # Wait a bit longer than the refill interval. 5 tokens/s → 1 token
    # every 200 ms; 350 ms gives us at least one refill plus headroom
    # for clock jitter.
    await asyncio.sleep(0.35)

    refilled = await limiter.acquire(dimension="tenant", key="t-1")
    assert refilled.allowed is True


@pytest.mark.asyncio
async def test_dimension_isolation(redis_client: redis_async.Redis) -> None:
    """Draining ``tenant:t-1`` must not touch ``apikey:t-1``."""
    limiter = RedisTokenBucketLimiter(redis_client=redis_client, capacity=1, refill_per_sec=0.01)
    first = await limiter.acquire(dimension="tenant", key="t-1")
    assert first.allowed is True
    second = await limiter.acquire(dimension="tenant", key="t-1")
    assert second.allowed is False

    # Same key but different dimension → fresh bucket.
    other = await limiter.acquire(dimension="apikey", key="t-1")
    assert other.allowed is True


@pytest.mark.asyncio
async def test_noscript_recovery(redis_client: redis_async.Redis) -> None:
    """Wiping the script cache mid-flight still serves the next call."""
    limiter = RedisTokenBucketLimiter(redis_client=redis_client, capacity=5, refill_per_sec=5.0)

    first = await limiter.acquire(dimension="tenant", key="t-resilience")
    assert first.allowed is True

    # Force-evict the Lua cache; next acquire must reload + succeed.
    await redis_client.script_flush()

    second = await limiter.acquire(dimension="tenant", key="t-resilience")
    assert second.allowed is True
