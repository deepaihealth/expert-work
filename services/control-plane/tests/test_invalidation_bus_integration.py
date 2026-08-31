"""Integration test for the invalidation bus over a real Redis (PR-E3a).

Spins up a real Redis 7 container (the ``test_redis_token_bucket_limiter_integration``
precedent) and drives the full path: publish on bus A → Redis pub/sub →
subscriber on bus B → handler applied. Also verifies self-delivery: the
publisher's own subscriber receives the event too (handlers are idempotent
and run on ALL pods including the publisher).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator
from uuid import uuid4

import pytest
import redis.asyncio as redis_async
from testcontainers.redis import RedisContainer

from control_plane.invalidation_bus import InvalidationBus, InvalidationEvent

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
async def redis_clients(
    redis_container: RedisContainer,
) -> AsyncIterator[tuple[redis_async.Redis, redis_async.Redis]]:
    url = _redis_url(redis_container)
    a = redis_async.from_url(url, encoding="utf-8", decode_responses=True)
    b = redis_async.from_url(url, encoding="utf-8", decode_responses=True)
    try:
        yield a, b
    finally:
        await a.aclose()
        await b.aclose()


async def _wait_until(predicate, timeout: float = 5.0) -> None:
    async def _poll() -> None:
        while not predicate():
            await asyncio.sleep(0.01)

    await asyncio.wait_for(_poll(), timeout)


@pytest.mark.asyncio
async def test_publish_on_a_dispatches_handler_on_b_and_on_a(
    redis_clients: tuple[redis_async.Redis, redis_async.Redis],
) -> None:
    client_a, client_b = redis_clients
    bus_a = InvalidationBus(redis_client=client_a, origin="pod-a")
    bus_b = InvalidationBus(redis_client=client_b, origin="pod-b")
    seen_a: list[InvalidationEvent] = []
    seen_b: list[InvalidationEvent] = []

    async def _on_a(event: InvalidationEvent) -> None:
        seen_a.append(event)

    async def _on_b(event: InvalidationEvent) -> None:
        seen_b.append(event)

    bus_a.start({"tenant_mcp": _on_a})
    bus_b.start({"tenant_mcp": _on_b})
    try:
        # Redis pub/sub has no replay: wait until both subscriptions are live
        # before publishing (channel shows 2 subscribers).
        async def _sub_count() -> int:
            counts = await client_a.pubsub_numsub("expert_work:invalidation")
            return int(counts[0][1]) if counts else 0

        for _ in range(500):
            if await _sub_count() >= 2:
                break
            await asyncio.sleep(0.01)
        assert await _sub_count() >= 2

        tid = str(uuid4())
        await bus_a.publish(InvalidationEvent(kind="tenant_mcp", tenant_id=tid))
        await _wait_until(lambda: seen_a and seen_b)
        # Peer pod received the event…
        assert seen_b[0].kind == "tenant_mcp"
        assert seen_b[0].tenant_id == tid
        assert seen_b[0].origin == "pod-a"
        # …and so did the publisher's own subscriber (self-delivery).
        assert seen_a[0].tenant_id == tid
    finally:
        await bus_a.stop()
        await bus_b.stop()
