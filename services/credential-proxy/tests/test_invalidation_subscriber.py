"""The proxy's secret cache must drop when control-plane rotates a secret (B-31 ①).

Until now the only way to evict :class:`SecretCache` early was
``POST /admin/cache/invalidate`` — which nothing in the repo calls — so a
rotated key stayed in use for up to ``cache_ttl_s`` (60s). With more than
one replica an HTTP call would also only reach one pod. The subscriber
under test listens on the same Redis channel control-plane already
broadcasts its own cache invalidations on and evicts locally.

All Redis traffic is faked here (the ``_FakeRedis`` double mirrors
control-plane's ``test_invalidation_bus.py``); the real-Redis path lives in
``tests/test_credential_proxy_invalidation_contract.py`` at the repo root.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any
from uuid import uuid4

import pytest

from credential_proxy.cache import SecretCache
from credential_proxy.invalidation import (
    INVALIDATION_CHANNEL,
    SECRET_VALUE_KINDS,
    InvalidationSubscriber,
    build_invalidation_subscriber,
    parse_invalidation_event,
)
from credential_proxy.settings import CredentialProxySettings

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakePubSub:
    def __init__(self, redis: _FakeRedis) -> None:
        self._redis = redis
        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._delivered = 0

    async def subscribe(self, channel: str) -> None:
        self._redis.subscribe_calls += 1
        self._redis.subscribed_channels.append(channel)
        self._redis.queues.append(self._queue)

    async def aclose(self) -> None:
        if self._queue in self._redis.queues:
            self._redis.queues.remove(self._queue)

    async def listen(self) -> Any:
        while True:
            item = await self._queue.get()
            yield item
            self._delivered += 1
            if (
                self._redis.drop_after_messages is not None
                and self._delivered >= self._redis.drop_after_messages
            ):
                raise ConnectionError("connection dropped")


class _FakeRedis:
    """In-memory pub/sub double: publish fans out to every live subscription."""

    def __init__(self, *, drop_after_messages: int | None = None) -> None:
        self.subscribe_calls = 0
        self.subscribed_channels: list[str] = []
        self.drop_after_messages = drop_after_messages
        self.queues: list[asyncio.Queue[dict[str, Any]]] = []
        self.closed = False

    async def publish(self, channel: str, payload: str) -> None:
        for queue in list(self.queues):
            queue.put_nowait({"type": "message", "channel": channel, "data": payload})

    def pubsub(self) -> _FakePubSub:
        return _FakePubSub(self)

    async def aclose(self) -> None:
        self.closed = True


def _payload(kind: str, *, tenant_id: str | None = None) -> str:
    """The wire shape control-plane's ``InvalidationBus.publish`` emits."""
    return json.dumps({"kind": kind, "tenant_id": tenant_id, "user_id": None, "origin": "pod-a"})


async def _wait_until(predicate: Any, timeout: float = 2.0) -> None:
    async def _poll() -> None:
        while not predicate():
            await asyncio.sleep(0.005)

    await asyncio.wait_for(_poll(), timeout)


async def _wait_for_subscription(redis: _FakeRedis, count: int = 1) -> None:
    await _wait_until(lambda: redis.subscribe_calls >= count)


def _cache_with(*keys: tuple[Any, str]) -> SecretCache:
    cache = SecretCache(max_size=16, ttl_s=60.0)
    for key in keys:
        cache.put(key, "sk-value")
    return cache


# ---------------------------------------------------------------------------
# Cache — tenant-scoped eviction
# ---------------------------------------------------------------------------


def test_invalidate_tenant_drops_only_that_tenant() -> None:
    tenant_a, tenant_b = uuid4(), uuid4()
    cache = _cache_with((tenant_a, "ref-1"), (tenant_a, "ref-2"), (tenant_b, "ref-1"))

    cache.invalidate_tenant(tenant_a)

    assert cache.get((tenant_a, "ref-1")) is None
    assert cache.get((tenant_a, "ref-2")) is None
    assert cache.get((tenant_b, "ref-1")) == "sk-value"


# ---------------------------------------------------------------------------
# Subscriber — dispatch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", sorted(SECRET_VALUE_KINDS))
async def test_secret_value_kind_without_a_tenant_clears_the_whole_cache(kind: str) -> None:
    tenant_a, tenant_b = uuid4(), uuid4()
    cache = _cache_with((tenant_a, "ref"), (tenant_b, "ref"))
    redis = _FakeRedis()
    subscriber = InvalidationSubscriber(redis_client=redis, cache=cache)
    subscriber.start()
    try:
        await _wait_for_subscription(redis)
        await redis.publish(INVALIDATION_CHANNEL, _payload(kind))
        await _wait_until(lambda: len(cache) == 0)
    finally:
        await subscriber.stop()


@pytest.mark.asyncio
async def test_tenant_scoped_event_drops_only_that_tenant() -> None:
    tenant_a, tenant_b = uuid4(), uuid4()
    cache = _cache_with((tenant_a, "ref"), (tenant_b, "ref"))
    redis = _FakeRedis()
    subscriber = InvalidationSubscriber(redis_client=redis, cache=cache)
    subscriber.start()
    try:
        await _wait_for_subscription(redis)
        await redis.publish(INVALIDATION_CHANNEL, _payload("tenant_mcp", tenant_id=str(tenant_a)))
        await _wait_until(lambda: cache.get((tenant_a, "ref")) is None)
        assert cache.get((tenant_b, "ref")) == "sk-value"
    finally:
        await subscriber.stop()


@pytest.mark.asyncio
async def test_unparsable_tenant_id_falls_back_to_a_full_clear() -> None:
    """A malformed scope must fail toward freshness, not toward staleness."""
    cache = _cache_with((uuid4(), "ref"), (uuid4(), "ref"))
    redis = _FakeRedis()
    subscriber = InvalidationSubscriber(redis_client=redis, cache=cache)
    subscriber.start()
    try:
        await _wait_for_subscription(redis)
        await redis.publish(INVALIDATION_CHANNEL, _payload("tenant_mcp", tenant_id="not-a-uuid"))
        await _wait_until(lambda: len(cache) == 0)
    finally:
        await subscriber.stop()


@pytest.mark.asyncio
async def test_unrelated_kind_leaves_the_cache_alone() -> None:
    """Built-agent / config-plane events carry no secret value change."""
    tenant = uuid4()
    cache = _cache_with((tenant, "ref"))
    redis = _FakeRedis()
    subscriber = InvalidationSubscriber(redis_client=redis, cache=cache)
    subscriber.start()
    try:
        await _wait_for_subscription(redis)
        await redis.publish(INVALIDATION_CHANNEL, _payload("agent_build", tenant_id=str(tenant)))
        await redis.publish(INVALIDATION_CHANNEL, _payload("platform_judge"))
        # A relevant event after the irrelevant ones proves the loop is still
        # consuming — and pins the ordering: the cache was intact until now.
        await redis.publish(INVALIDATION_CHANNEL, _payload("platform_secrets"))
        await _wait_until(lambda: len(cache) == 0)
    finally:
        await subscriber.stop()


@pytest.mark.asyncio
async def test_unrelated_kind_does_not_evict_even_after_settling() -> None:
    tenant = uuid4()
    cache = _cache_with((tenant, "ref"))
    redis = _FakeRedis()
    subscriber = InvalidationSubscriber(redis_client=redis, cache=cache)
    subscriber.start()
    try:
        await _wait_for_subscription(redis)
        await redis.publish(INVALIDATION_CHANNEL, _payload("agent_build", tenant_id=str(tenant)))
        # Give the loop a real chance to (wrongly) act before asserting.
        for _ in range(20):
            await asyncio.sleep(0)
        assert cache.get((tenant, "ref")) == "sk-value"
    finally:
        await subscriber.stop()


@pytest.mark.asyncio
async def test_bad_payload_is_ignored_logged_and_does_not_kill_the_loop(
    caplog: pytest.LogCaptureFixture,
) -> None:
    cache = _cache_with((uuid4(), "ref"))
    redis = _FakeRedis()
    subscriber = InvalidationSubscriber(redis_client=redis, cache=cache)
    subscriber.start()
    try:
        await _wait_for_subscription(redis)
        with caplog.at_level(logging.WARNING, logger="credential_proxy.invalidation"):
            await redis.publish(INVALIDATION_CHANNEL, "this is not json")
            await redis.publish(INVALIDATION_CHANNEL, json.dumps({"tenant_id": "x"}))  # no kind
            await redis.publish(INVALIDATION_CHANNEL, _payload("platform_secrets"))
            await _wait_until(lambda: len(cache) == 0)
        bad = [r for r in caplog.records if "bad_payload" in r.getMessage()]
        assert len(bad) == 2
    finally:
        await subscriber.stop()


@pytest.mark.asyncio
async def test_subscribes_to_the_shared_channel() -> None:
    redis = _FakeRedis()
    subscriber = InvalidationSubscriber(redis_client=redis, cache=_cache_with())
    subscriber.start()
    try:
        await _wait_for_subscription(redis)
        assert redis.subscribed_channels == ["expert_work:invalidation"]
    finally:
        await subscriber.stop()


# ---------------------------------------------------------------------------
# Subscriber — resilience
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reconnects_after_the_connection_drops() -> None:
    """A subscriber that dies silently would reintroduce the 60s staleness
    without anyone noticing — it must come back on its own."""
    cache = _cache_with((uuid4(), "ref"))
    redis = _FakeRedis(drop_after_messages=1)
    subscriber = InvalidationSubscriber(
        redis_client=redis, cache=cache, reconnect_initial_s=0.01, reconnect_max_s=0.02
    )
    subscriber.start()
    try:
        await _wait_for_subscription(redis, 1)
        await redis.publish(INVALIDATION_CHANNEL, _payload("agent_build"))  # drops after this
        await _wait_for_subscription(redis, 2)
        await redis.publish(INVALIDATION_CHANNEL, _payload("platform_secrets"))
        await _wait_until(lambda: len(cache) == 0)
    finally:
        await subscriber.stop()


@pytest.mark.asyncio
async def test_stop_cancels_the_loop_and_closes_the_client() -> None:
    redis = _FakeRedis()
    subscriber = InvalidationSubscriber(redis_client=redis, cache=_cache_with())
    subscriber.start()
    await _wait_for_subscription(redis)

    await subscriber.stop()

    assert redis.closed is True
    assert redis.queues == [], "the pubsub connection must be released on stop"


# ---------------------------------------------------------------------------
# Factory — unconfigured means off, loudly once
# ---------------------------------------------------------------------------


def test_unset_redis_url_builds_no_subscriber(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="credential_proxy.invalidation"):
        subscriber = build_invalidation_subscriber(
            CredentialProxySettings(redis_url=None), _cache_with()
        )
    assert subscriber is None
    assert any("invalidation.disabled" in r.getMessage() for r in caplog.records)


def test_malformed_redis_url_disables_the_bus_instead_of_raising(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The bus is optional; a bad URL must degrade to "unconfigured", not
    take the proxy down. Only the exception type is logged — the URL
    carries the Redis password."""
    bad_url = "not-a-redis-url://secret-password@nowhere"
    with caplog.at_level(logging.WARNING, logger="credential_proxy.invalidation"):
        subscriber = build_invalidation_subscriber(
            CredentialProxySettings(redis_url=bad_url), _cache_with()
        )
    assert subscriber is None
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "invalidation.disabled" in warnings[0]
    assert "ValueError" in warnings[0]
    assert "secret-password" not in warnings[0]
    assert bad_url not in warnings[0]


@pytest.mark.asyncio
async def test_app_boots_and_admin_invalidate_works_with_a_malformed_redis_url(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The real ``create_app`` startup path (DB engine + secret store are
    lazy, egress off): a malformed bus URL must not stop the pod from
    serving, and the manual ``/admin/cache/invalidate`` fallback must still
    work."""
    from aiohttp.test_utils import TestClient, TestServer

    from credential_proxy.app import CACHE_KEY, create_app

    settings = CredentialProxySettings(
        redis_url="not-a-redis-url://secret-password@nowhere",
        egress_enabled=False,
        admin_token="admin-token-for-tests",
    )
    app = create_app(settings)
    with caplog.at_level(logging.WARNING, logger="credential_proxy.invalidation"):
        async with TestClient(TestServer(app)) as client:
            health = await client.get("/admin/health")
            assert health.status == 200

            cache = app[CACHE_KEY]
            cache.put((uuid4(), "anthropic/api-key"), "sk-value")
            resp = await client.post(
                "/admin/cache/invalidate",
                headers={"Authorization": "Bearer admin-token-for-tests"},
            )
            assert resp.status == 204
            assert len(cache) == 0

    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("invalidation.disabled" in m and "ValueError" in m for m in warnings)
    assert not any("secret-password" in m for m in warnings)


def test_redis_url_setting_reads_the_prefixed_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXPERT_WORK_CRED_PROXY_REDIS_URL", "redis://:pw@redis:6379/0")
    assert CredentialProxySettings().redis_url == "redis://:pw@redis:6379/0"


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def test_parse_reads_the_control_plane_payload_shape() -> None:
    tenant = str(uuid4())
    event = parse_invalidation_event(_payload("platform_secrets", tenant_id=tenant))
    assert event is not None
    assert (event.kind, event.tenant_id, event.origin) == ("platform_secrets", tenant, "pod-a")


@pytest.mark.parametrize("raw", ["", "nope", "[]", json.dumps({"tenant_id": "x"}), None])
def test_parse_returns_none_for_garbage(raw: object) -> None:
    assert parse_invalidation_event(raw) is None
