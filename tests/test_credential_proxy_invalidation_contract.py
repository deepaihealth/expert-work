"""Cross-package contract: control-plane's invalidation bus ↔ credential-proxy's
subscriber (B-31 ①).

credential-proxy is a separate package and must not import ``control_plane``,
so it carries its own copy of the channel name and the payload parser. This
file is the only place both sides meet: control-plane serialises with the real
``InvalidationBus``, credential-proxy parses with its own code. If either side
drifts — channel renamed, a field renamed, a subscribed kind dropped — a test
here goes red instead of the proxy silently going deaf.

The last test drives the whole path through a real Redis (testcontainers,
``integration`` marker): bus ``publish`` → Redis pub/sub → proxy subscriber →
``SecretCache`` emptied.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Iterator
from uuid import uuid4

import pytest

from control_plane.invalidation_bus import CHANNEL, KINDS, InvalidationBus, InvalidationEvent
from credential_proxy.cache import SecretCache
from credential_proxy.invalidation import (
    INVALIDATION_CHANNEL,
    SECRET_VALUE_KINDS,
    InvalidationSubscriber,
    parse_invalidation_event,
)

# ---------------------------------------------------------------------------
# Wire-shape contract (no Redis)
# ---------------------------------------------------------------------------


class _CapturingRedis:
    """Records what ``InvalidationBus.publish`` would send to Redis."""

    def __init__(self) -> None:
        self.published: list[tuple[str, str]] = []

    async def publish(self, channel: str, payload: str) -> None:
        self.published.append((channel, payload))


def test_both_sides_name_the_same_channel() -> None:
    assert INVALIDATION_CHANNEL == CHANNEL


def test_every_kind_the_proxy_subscribes_to_is_one_control_plane_publishes() -> None:
    """A kind renamed on the publisher side would leave the proxy listening
    for something that never arrives."""
    assert SECRET_VALUE_KINDS <= KINDS, sorted(SECRET_VALUE_KINDS - KINDS)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", sorted(SECRET_VALUE_KINDS))
async def test_a_real_publish_parses_on_the_proxy_side(kind: str) -> None:
    redis = _CapturingRedis()
    bus = InvalidationBus(redis_client=redis, origin="pod-a")
    tenant = str(uuid4())

    await bus.publish(InvalidationEvent(kind=kind, tenant_id=tenant))

    (channel, payload) = redis.published[0]
    assert channel == INVALIDATION_CHANNEL
    event = parse_invalidation_event(payload)
    assert event is not None
    assert (event.kind, event.tenant_id, event.origin) == (kind, tenant, "pod-a")


@pytest.mark.asyncio
async def test_an_unscoped_publish_parses_with_no_tenant() -> None:
    redis = _CapturingRedis()
    bus = InvalidationBus(redis_client=redis, origin="pod-a")

    await bus.publish(InvalidationEvent(kind="platform_secrets"))

    event = parse_invalidation_event(redis.published[0][1])
    assert event is not None
    assert event.tenant_id is None


# ---------------------------------------------------------------------------
# End to end over a real Redis
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def redis_container() -> Iterator[object]:
    from testcontainers.redis import RedisContainer

    with RedisContainer("public.ecr.aws/docker/library/redis:7-alpine") as container:
        yield container


@pytest.fixture
async def redis_url(redis_container: object) -> AsyncIterator[str]:
    host = redis_container.get_container_host_ip()  # type: ignore[attr-defined]
    port = redis_container.get_exposed_port(6379)  # type: ignore[attr-defined]
    yield f"redis://{host}:{port}/0"


async def _wait_until(predicate: Callable[[], object], timeout: float = 5.0) -> None:
    async def _poll() -> None:
        while not predicate():
            await asyncio.sleep(0.01)

    await asyncio.wait_for(_poll(), timeout)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_control_plane_publish_empties_the_proxy_cache_over_redis(redis_url: str) -> None:
    import redis.asyncio as redis_async

    publisher = redis_async.from_url(redis_url, encoding="utf-8", decode_responses=True)
    subscriber_client = redis_async.from_url(redis_url, encoding="utf-8", decode_responses=True)
    bus = InvalidationBus(redis_client=publisher, origin="control-plane-pod")

    tenant_x, tenant_y = uuid4(), uuid4()
    cache = SecretCache(max_size=16, ttl_s=3600.0)
    cache.put((tenant_x, "expert-work/platform/llm/anthropic"), "sk-x")
    cache.put((tenant_y, "expert-work/tenant/mcp/token"), "sk-y")
    subscriber = InvalidationSubscriber(redis_client=subscriber_client, cache=cache)
    subscriber.start()
    try:
        # Pub/sub has no replay: wait until the proxy's subscription is live.
        async def _sub_count() -> int:
            counts = await publisher.pubsub_numsub(CHANNEL)
            return int(counts[0][1]) if counts else 0

        for _ in range(500):
            if await _sub_count() >= 1:
                break
            await asyncio.sleep(0.01)
        assert await _sub_count() >= 1

        # One channel, one publisher connection, sequential consumer: delivery
        # order is publish order. So once the tenant_mcp event below has
        # evicted Y, the agent_build event for X has already been processed —
        # X still being present proves an unrelated kind does not evict, over
        # the real wire, without any timing assumption.
        await bus.publish(InvalidationEvent(kind="agent_build", tenant_id=str(tenant_x)))
        await bus.publish(InvalidationEvent(kind="tenant_mcp", tenant_id=str(tenant_y)))
        await _wait_until(lambda: cache.get((tenant_y, "expert-work/tenant/mcp/token")) is None)
        assert cache.get((tenant_x, "expert-work/platform/llm/anthropic")) == "sk-x"

        # The rotation broadcast platform_config.py emits (unscoped) → empty.
        await bus.publish(InvalidationEvent(kind="platform_secrets"))
        await _wait_until(lambda: len(cache) == 0)
    finally:
        await subscriber.stop()
        await publisher.aclose()
