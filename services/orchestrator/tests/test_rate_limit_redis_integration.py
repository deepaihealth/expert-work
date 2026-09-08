"""Integration test for :class:`RedisRpmLimiter` — real Redis, real Lua.

波 2 线 A. The unit tests re-implement the bucket arithmetic in a fake, so
they cannot catch a wrong Lua. This file runs the script itself and pins its
**units** (B-32: the quota bucket shipped with a 1000x refill error that its
own retry formula agreed with):

* a deficit of one token at 1 req/s is reported as ~1000 ms, not 1 s or 1 µs;
* 50 ms later the bucket is still empty (refill is not 1000x too fast);
* ~1 s later it has a token back (refill is not zero).

Plus the properties the design rests on: two limiters on one key share the
bucket and the 4th call really waits for the refill; different keys are
independent; ``SCRIPT FLUSH`` is survived; an unreachable Redis degrades to
the local bucket instead of failing the call.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Iterator

import pytest
import redis.asyncio as redis_async
from testcontainers.redis import RedisContainer

from orchestrator.llm.rate_limit_redis import RedisRpmLimiter

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def redis_container() -> Iterator[RedisContainer]:
    with RedisContainer("public.ecr.aws/docker/library/redis:7-alpine") as container:
        yield container


@pytest.fixture
async def redis_client(redis_container: RedisContainer) -> AsyncIterator[redis_async.Redis]:
    host = redis_container.get_container_host_ip()
    port = redis_container.get_exposed_port(6379)
    client = redis_async.from_url(
        f"redis://{host}:{port}/0", encoding="utf-8", decode_responses=True
    )
    try:
        await client.flushdb()
        yield client
    finally:
        await client.aclose()


class _AbortError(Exception):
    """Raised by the recording sleep so a denied acquire surfaces the Lua's
    retry hint instead of actually waiting."""


class _RecordingSleep:
    def __init__(self) -> None:
        self.seconds: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.seconds.append(seconds)
        raise _AbortError


# --------------------------------------------------------------------- units


@pytest.mark.asyncio
async def test_lua_units_retry_in_ms_refill_per_second(redis_client: redis_async.Redis) -> None:
    """rpm=2 over 2 s → capacity 2, refill 1 req/s."""
    sleep = _RecordingSleep()
    limiter = RedisRpmLimiter(
        redis_client=redis_client,
        bucket_key="rl:llm:it:units",
        rate_limit_rpm=2,
        time_period_s=2.0,
        sleep=sleep,
    )

    await limiter.acquire()
    await limiter.acquire()  # capacity of 2 spent

    with pytest.raises(_AbortError):
        await limiter.acquire()
    assert 0.9 <= sleep.seconds[-1] <= 1.0, (
        f"one token owed at 1 req/s is ~1000 ms, got {sleep.seconds[-1]}s"
    )

    await asyncio.sleep(0.05)
    with pytest.raises(_AbortError):
        await limiter.acquire()
    assert 0.85 <= sleep.seconds[-1] <= 0.96, (
        "50 ms at 1 req/s must NOT refill a token (B-32: not 1000x too fast)"
    )

    await asyncio.sleep(1.0)
    await limiter.acquire()  # no sleep → no _AbortError: a token came back
    assert len(sleep.seconds) == 2, "1.05 s at 1 req/s owes exactly one token"


# ------------------------------------------------------------------ sharing


@pytest.mark.asyncio
async def test_two_limiters_share_one_bucket_and_wait_for_real_refill(
    redis_client: redis_async.Redis,
) -> None:
    """rpm=3 over 1 s: A,B,A pass instantly; the 4th waits ~333 ms on the
    shared bucket, then goes through."""
    def _replica() -> RedisRpmLimiter:
        return RedisRpmLimiter(
            redis_client=redis_client,
            bucket_key="rl:llm:it:shared",
            rate_limit_rpm=3,
            time_period_s=1.0,
        )

    replica_a = _replica()
    replica_b = _replica()

    start = time.monotonic()
    await replica_a.acquire()
    await replica_b.acquire()
    await replica_a.acquire()
    burst = time.monotonic() - start
    await replica_b.acquire()
    total = time.monotonic() - start

    assert burst < 0.3, f"3 calls fit the shared 3-token bucket; took {burst:.3f}s"
    assert 0.25 <= total < 1.0, (
        f"4th call across replicas waits one refill (~0.333s); total {total:.3f}s"
    )
    assert replica_a.degraded is False and replica_b.degraded is False


@pytest.mark.asyncio
async def test_different_keys_are_independent(redis_client: redis_async.Redis) -> None:
    primary = RedisRpmLimiter(
        redis_client=redis_client, bucket_key="rl:llm:it:p", rate_limit_rpm=1, time_period_s=60.0
    )
    fallback = RedisRpmLimiter(
        redis_client=redis_client, bucket_key="rl:llm:it:f", rate_limit_rpm=1, time_period_s=60.0
    )

    start = time.monotonic()
    await primary.acquire()
    await fallback.acquire()

    assert time.monotonic() - start < 0.3


# --------------------------------------------------------------- resilience


@pytest.mark.asyncio
async def test_script_flush_is_survived(redis_client: redis_async.Redis) -> None:
    limiter = RedisRpmLimiter(
        redis_client=redis_client,
        bucket_key="rl:llm:it:noscript",
        rate_limit_rpm=10,
        time_period_s=60.0,
    )
    await limiter.acquire()
    await redis_client.script_flush()

    await limiter.acquire()

    assert limiter.degraded is False
    tokens = await redis_client.hget("rl:llm:it:noscript", "tokens")
    assert tokens is not None and float(tokens) == pytest.approx(8.0, abs=0.01)


@pytest.mark.asyncio
async def test_unreachable_redis_degrades_to_local_bucket(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger="orchestrator.llm.rate_limit_redis")
    dead = redis_async.from_url(
        "redis://127.0.0.1:1/0", encoding="utf-8", decode_responses=True, socket_connect_timeout=0.2
    )
    try:
        limiter = RedisRpmLimiter(
            redis_client=dead, bucket_key="rl:llm:it:dead", rate_limit_rpm=10, time_period_s=60.0
        )
        start = time.monotonic()
        await limiter.acquire()
        await limiter.acquire()
        elapsed = time.monotonic() - start
    finally:
        await dead.aclose()

    assert limiter.degraded is True
    assert elapsed < 2.0, "the call is served locally, not stuck on Redis"
    warnings = [r for r in caplog.records if "rate_limit.redis_degraded" in r.getMessage()]
    assert len(warnings) == 1
