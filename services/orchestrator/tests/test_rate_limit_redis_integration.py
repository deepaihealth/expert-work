"""Integration test for :class:`RedisRpmLimiter` — real Redis, real Lua.

波 2 线 A. The unit tests re-implement the bucket arithmetic in a fake, so
they cannot catch a wrong Lua. This file runs the script itself and pins its
**units** (B-32: the quota bucket shipped with a 1000x refill error that its
own retry formula agreed with):

* a deficit of one token at 1 req/s is reported as 1000 ms, not 1 s or 1 µs;
* 50 ms later the bucket is still empty (refill is not 1000x too fast);
* ~1 s later it has a token back (refill is not zero).

Time is **injected**: the limiter takes ``clock`` (what it stamps into
``now_ms`` for the Lua — production uses the wall clock the same way, the
script never reads the server clock) and ``sleep`` (here: record the
requested duration and advance the clock). Every assertion is therefore
exact and independent of CI speed, while the arithmetic under test is still
Redis's.

Plus the properties the design rests on: two limiters on one key share the
bucket and the 4th call waits for the refill; different keys are independent;
``SCRIPT FLUSH`` is survived; an unreachable Redis degrades to the local
bucket instead of failing the call.
"""

from __future__ import annotations

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


class _FakeClock:
    """Injected wall clock: ``sleep`` records the duration the limiter asked
    for and advances the clock instead of waiting. Kept in whole
    milliseconds so the ``now_ms`` the limiter stamps is exact."""

    def __init__(self) -> None:
        self.now_ms = 1_700_000_000_000
        self.sleeps: list[float] = []

    def time(self) -> float:
        return self.now_ms / 1000

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now_ms += round(seconds * 1000)


def _limiter(
    client: redis_async.Redis, clock: _FakeClock, *, key: str, rpm: int, period_s: float
) -> RedisRpmLimiter:
    return RedisRpmLimiter(
        redis_client=client,
        bucket_key=key,
        rate_limit_rpm=rpm,
        time_period_s=period_s,
        clock=clock.time,
        sleep=clock.sleep,
    )


# --------------------------------------------------------------------- units


@pytest.mark.asyncio
async def test_lua_units_retry_in_ms_refill_per_second(redis_client: redis_async.Redis) -> None:
    """rpm=2 over 2 s → capacity 2, refill 1 req/s."""
    clock = _FakeClock()
    limiter = _limiter(redis_client, clock, key="rl:llm:it:units", rpm=2, period_s=2.0)

    await limiter.acquire()
    await limiter.acquire()  # capacity of 2 spent
    assert clock.sleeps == []

    await limiter.acquire()
    assert clock.sleeps == [pytest.approx(1.0, abs=0.001)], (
        "one token owed at 1 req/s is 1000 ms — not 1 (seconds) and not 1e-3 (µs)"
    )

    # The sleep above advanced the clock exactly to the refill point and the
    # call went through, so the bucket is empty again at ``now``.
    clock.now_ms += 50
    await limiter.acquire()
    assert clock.sleeps[-1] == pytest.approx(0.95, abs=0.001), (
        "50 ms at 1 req/s refills 0.05 token, so 0.95 is still owed (B-32: not 1000x too fast)"
    )

    clock.now_ms += 1000
    await limiter.acquire()
    assert len(clock.sleeps) == 2, (
        "a full second at 1 req/s owes no wait at all (refill is not zero)"
    )


# ------------------------------------------------------------------ sharing


@pytest.mark.asyncio
async def test_two_limiters_share_one_bucket_and_wait_for_real_refill(
    redis_client: redis_async.Redis,
) -> None:
    """rpm=3 over 1 s: A,B,A pass; the 4th (on B) waits one refill of the
    shared bucket — 1/3 s, as reported by the Lua — then goes through."""
    clock = _FakeClock()
    replica_a = _limiter(redis_client, clock, key="rl:llm:it:shared", rpm=3, period_s=1.0)
    replica_b = _limiter(redis_client, clock, key="rl:llm:it:shared", rpm=3, period_s=1.0)

    await replica_a.acquire()
    await replica_b.acquire()
    await replica_a.acquire()
    assert clock.sleeps == [], "3 calls fit the shared 3-token bucket"

    await replica_b.acquire()
    assert clock.sleeps == [pytest.approx(1 / 3, abs=0.002)], (
        "4th call across replicas waits one refill (ceil(333.3 ms) reported by the Lua)"
    )
    assert replica_a.degraded is False and replica_b.degraded is False


@pytest.mark.asyncio
async def test_different_keys_are_independent(redis_client: redis_async.Redis) -> None:
    clock = _FakeClock()
    primary = _limiter(redis_client, clock, key="rl:llm:it:p", rpm=1, period_s=60.0)
    fallback = _limiter(redis_client, clock, key="rl:llm:it:f", rpm=1, period_s=60.0)

    await primary.acquire()
    await fallback.acquire()

    assert clock.sleeps == [], "each key has its own 1-token bucket"


# --------------------------------------------------------------- resilience


@pytest.mark.asyncio
async def test_script_flush_is_survived(redis_client: redis_async.Redis) -> None:
    clock = _FakeClock()
    limiter = _limiter(redis_client, clock, key="rl:llm:it:noscript", rpm=10, period_s=60.0)
    await limiter.acquire()
    await redis_client.script_flush()

    await limiter.acquire()

    assert limiter.degraded is False
    tokens = await redis_client.hget("rl:llm:it:noscript", "tokens")
    assert tokens is not None and float(tokens) == pytest.approx(8.0, abs=0.001)


@pytest.mark.asyncio
async def test_unreachable_redis_degrades_to_local_bucket() -> None:
    """Asserts the limiter's own state, not caplog: the CI integration job
    runs the whole repo in one process, and alembic's ``env.py`` calls
    ``logging.config.fileConfig`` (default ``disable_existing_loggers=True``)
    during the persistence migration tests, which switches off every logger
    created at collection time — this module's included. "Logs once per
    outage" is pinned by the unit tests with a fake client."""
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
    assert limiter.last_degrade_error == "ConnectionError", (
        "the real redis-py refusal is what tripped the bucket"
    )
    # Connection refused is immediate; the bound only guards against the
    # call being stuck on Redis (the limiter's own op timeout is 2 s).
    assert elapsed < 2.0, "the call is served locally, not stuck on Redis"
