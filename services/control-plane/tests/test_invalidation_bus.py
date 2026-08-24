"""Unit tests for the cross-replica cache-invalidation bus (PR-E3a)."""

from __future__ import annotations

import asyncio
import json
import logging
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from prometheus_client import REGISTRY

from control_plane.invalidation_bus import (
    CHANNEL,
    KINDS,
    InvalidationBus,
    InvalidationEvent,
    NoopInvalidationBus,
    build_invalidation_handlers,
)

# ---------------------------------------------------------------------------
# Fakes / spies
# ---------------------------------------------------------------------------


class _FakePubSub:
    def __init__(self, redis: _FakeRedis) -> None:
        self._redis = redis
        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._delivered = 0

    async def subscribe(self, channel: str) -> None:
        self._redis.subscribe_calls += 1
        self._redis.subscribed_channels.append(channel)
        if self._redis.subscribe_failures > 0:
            self._redis.subscribe_failures -= 1
            raise ConnectionError("subscribe refused")
        self._redis.queues.append(self._queue)

    async def aclose(self) -> None:
        if self._queue in self._redis.queues:
            self._redis.queues.remove(self._queue)

    async def listen(self):
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

    def __init__(
        self, *, subscribe_failures: int = 0, drop_after_messages: int | None = None
    ) -> None:
        self.published: list[tuple[str, str]] = []
        self.subscribe_calls = 0
        self.subscribed_channels: list[str] = []
        self.subscribe_failures = subscribe_failures
        self.drop_after_messages = drop_after_messages
        self.queues: list[asyncio.Queue[dict[str, Any]]] = []

    async def publish(self, channel: str, payload: str) -> None:
        self.published.append((channel, payload))
        for queue in list(self.queues):
            queue.put_nowait({"type": "message", "channel": channel, "data": payload})

    def pubsub(self) -> _FakePubSub:
        return _FakePubSub(self)


class _BrokenRedis:
    async def publish(self, channel: str, payload: str) -> None:
        raise ConnectionError("redis down")


class _SpyRuntime:
    def __init__(self) -> None:
        self.tenant_calls: list[Any] = []
        self.all_calls = 0
        self.user_calls: list[tuple[Any, str]] = []

    def invalidate_tenant(self, tenant_id: Any) -> None:
        self.tenant_calls.append(tenant_id)

    def invalidate_all(self) -> None:
        self.all_calls += 1

    def invalidate_user(self, tenant_id: Any, user_id: str) -> None:
        self.user_calls.append((tenant_id, user_id))


class _SpyTenantPool:
    def __init__(self) -> None:
        self.calls: list[Any] = []

    async def invalidate(self, tenant_id: Any) -> None:
        self.calls.append(tenant_id)


class _SpyPlatformPool:
    def __init__(self) -> None:
        self.calls = 0

    async def invalidate(self) -> None:
        self.calls += 1


class _SpyOAuthPool:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, str]] = []

    async def invalidate(self, tenant_id: Any, user_id: str) -> None:
        self.calls.append((tenant_id, user_id))


class _SpyTenantConfig:
    def __init__(self) -> None:
        self.calls: list[Any] = []

    def invalidate(self, tenant_id: Any) -> None:
        self.calls.append(tenant_id)


def _metric(name: str, labels: dict[str, str]) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


async def _wait_until(predicate, timeout: float = 2.0) -> None:
    async def _poll() -> None:
        while not predicate():
            await asyncio.sleep(0.005)

    await asyncio.wait_for(_poll(), timeout)


def _bus(redis: Any) -> InvalidationBus:
    return InvalidationBus(
        redis_client=redis,
        origin="pod-test",
        reconnect_initial_s=0.01,
        reconnect_max_s=0.05,
    )


# ---------------------------------------------------------------------------
# publish
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_publish_serializes_event_to_channel() -> None:
    redis = _FakeRedis()
    bus = _bus(redis)
    tid = str(uuid4())
    await bus.publish(InvalidationEvent(kind="agent_build_user", tenant_id=tid, user_id="emp-1"))
    assert len(redis.published) == 1
    channel, payload = redis.published[0]
    assert channel == CHANNEL == "expert_work:invalidation"
    data = json.loads(payload)
    assert data == {
        "kind": "agent_build_user",
        "tenant_id": tid,
        "user_id": "emp-1",
        # The bus stamps its own origin when the event carries none.
        "origin": "pod-test",
    }


@pytest.mark.asyncio
async def test_publish_never_raises_when_redis_down() -> None:
    bus = InvalidationBus(redis_client=_BrokenRedis(), origin="pod-test")
    before = _metric(
        "expert_work_control_plane_invalidation_bus_errors_total", {"stage": "publish"}
    )
    # Must NOT raise — the caller's local invalidation already happened.
    await bus.publish(InvalidationEvent(kind="agent_build", tenant_id=str(uuid4())))
    after = _metric("expert_work_control_plane_invalidation_bus_errors_total", {"stage": "publish"})
    assert after == before + 1


@pytest.mark.asyncio
async def test_publish_soon_fires_publish_from_sync_context() -> None:
    redis = _FakeRedis()
    bus = _bus(redis)
    bus.publish_soon(InvalidationEvent(kind="agent_build", tenant_id=str(uuid4())))
    await _wait_until(lambda: len(redis.published) == 1)


def test_publish_soon_without_running_loop_is_a_noop() -> None:
    bus = InvalidationBus(redis_client=_FakeRedis(), origin="pod-test")
    # No event loop here — must swallow, not raise.
    bus.publish_soon(InvalidationEvent(kind="agent_build", tenant_id=str(uuid4())))


# ---------------------------------------------------------------------------
# subscriber
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_subscriber_dispatches_by_kind() -> None:
    redis = _FakeRedis()
    bus = _bus(redis)
    seen_a: list[InvalidationEvent] = []
    seen_b: list[InvalidationEvent] = []

    async def _on_a(event: InvalidationEvent) -> None:
        seen_a.append(event)

    async def _on_b(event: InvalidationEvent) -> None:
        seen_b.append(event)

    bus.start({"agent_build": _on_a, "platform_mcp": _on_b})
    try:
        await _wait_until(lambda: redis.queues)
        tid = str(uuid4())
        await bus.publish(InvalidationEvent(kind="agent_build", tenant_id=tid))
        await bus.publish(InvalidationEvent(kind="platform_mcp"))
        await _wait_until(lambda: seen_a and seen_b)
        assert seen_a[0].kind == "agent_build"
        assert seen_a[0].tenant_id == tid
        assert seen_a[0].origin == "pod-test"
        assert seen_b[0].kind == "platform_mcp"
        assert seen_b[0].tenant_id is None
    finally:
        await bus.stop()


@pytest.mark.asyncio
async def test_subscriber_survives_handler_exception() -> None:
    redis = _FakeRedis()
    bus = _bus(redis)
    seen: list[str] = []

    async def _boom(event: InvalidationEvent) -> None:
        raise RuntimeError("handler blew up")

    async def _ok(event: InvalidationEvent) -> None:
        seen.append(event.kind)

    bus.start({"tenant_mcp": _boom, "agent_build": _ok})
    try:
        await _wait_until(lambda: redis.queues)
        before = _metric(
            "expert_work_control_plane_invalidation_bus_errors_total", {"stage": "handler"}
        )
        await bus.publish(InvalidationEvent(kind="tenant_mcp", tenant_id=str(uuid4())))
        await bus.publish(InvalidationEvent(kind="agent_build", tenant_id=str(uuid4())))
        await _wait_until(lambda: seen)  # loop survived the first handler's crash
        assert seen == ["agent_build"]
        after = _metric(
            "expert_work_control_plane_invalidation_bus_errors_total", {"stage": "handler"}
        )
        assert after == before + 1
    finally:
        await bus.stop()


@pytest.mark.asyncio
async def test_subscriber_ignores_unknown_kind_and_bad_payload() -> None:
    redis = _FakeRedis()
    bus = _bus(redis)
    seen: list[str] = []

    async def _ok(event: InvalidationEvent) -> None:
        seen.append(event.kind)

    bus.start({"agent_build": _ok})
    try:
        await _wait_until(lambda: redis.queues)
        await redis.publish(CHANNEL, "{not json")
        await redis.publish(CHANNEL, json.dumps({"kind": "no_such_kind"}))
        await bus.publish(InvalidationEvent(kind="agent_build", tenant_id=str(uuid4())))
        await _wait_until(lambda: seen)
        assert seen == ["agent_build"]
    finally:
        await bus.stop()


@pytest.mark.asyncio
async def test_subscriber_reconnects_after_connection_drop() -> None:
    # Every pubsub session dies right after delivering one message — the
    # subscriber must reconnect and keep dispatching (BUG-17 disease class:
    # a dead subscriber silently reintroduces cross-replica staleness).
    redis = _FakeRedis(drop_after_messages=1)
    bus = _bus(redis)
    seen: list[str | None] = []

    async def _ok(event: InvalidationEvent) -> None:
        seen.append(event.tenant_id)

    bus.start({"agent_build": _ok})
    try:
        await _wait_until(lambda: redis.queues)
        await bus.publish(InvalidationEvent(kind="agent_build", tenant_id="t-1"))
        await _wait_until(lambda: seen == ["t-1"])
        # First session dropped; wait for the re-subscribe.
        await _wait_until(lambda: redis.queues)
        await bus.publish(InvalidationEvent(kind="agent_build", tenant_id="t-2"))
        await _wait_until(lambda: seen == ["t-1", "t-2"])
        assert redis.subscribe_calls >= 2
    finally:
        await bus.stop()


@pytest.mark.asyncio
async def test_subscriber_retries_when_subscribe_itself_fails() -> None:
    redis = _FakeRedis(subscribe_failures=1)
    bus = _bus(redis)
    seen: list[str] = []

    async def _ok(event: InvalidationEvent) -> None:
        seen.append(event.kind)

    bus.start({"agent_build": _ok})
    try:
        await _wait_until(lambda: redis.subscribe_calls >= 2 and redis.queues)
        await bus.publish(InvalidationEvent(kind="agent_build", tenant_id=str(uuid4())))
        await _wait_until(lambda: seen)
    finally:
        await bus.stop()


# ---------------------------------------------------------------------------
# noop bus
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_noop_bus_is_inert_and_logs_once(caplog: pytest.LogCaptureFixture) -> None:
    bus = NoopInvalidationBus()
    with caplog.at_level(logging.INFO, logger="expert_work.control_plane.invalidation_bus"):
        await bus.publish(InvalidationEvent(kind="agent_build", tenant_id=str(uuid4())))
        bus.publish_soon(InvalidationEvent(kind="agent_build", tenant_id=str(uuid4())))
        bus.start({})
        await bus.stop()
    noop_lines = [r for r in caplog.records if "invalidation_bus.noop" in r.getMessage()]
    assert len(noop_lines) == 1


# ---------------------------------------------------------------------------
# handler wiring (two-layer invariant)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tenant_mcp_handler_hits_pool_and_agent_build_layers() -> None:
    tid = uuid4()
    pool = _SpyTenantPool()
    runtime = _SpyRuntime()
    state = SimpleNamespace(tenant_mcp_pool_service=pool, agent_runtime=runtime)
    handlers = build_invalidation_handlers(state)
    await handlers["tenant_mcp"](InvalidationEvent(kind="tenant_mcp", tenant_id=str(tid)))
    assert pool.calls == [tid]
    # Two-layer invariant: the inner (pool) layer is baked into BuiltAgent at
    # build time, so the agent-build layer for the same scope must drop too.
    assert runtime.tenant_calls == [tid]


@pytest.mark.asyncio
async def test_tenant_config_handler_hits_config_and_agent_build_layers() -> None:
    tid = uuid4()
    config = _SpyTenantConfig()
    runtime = _SpyRuntime()
    state = SimpleNamespace(tenant_config_service=config, agent_runtime=runtime)
    handlers = build_invalidation_handlers(state)
    await handlers["tenant_config"](InvalidationEvent(kind="tenant_config", tenant_id=str(tid)))
    assert config.calls == [tid]
    assert runtime.tenant_calls == [tid]


@pytest.mark.asyncio
async def test_platform_mcp_handler_hits_pool_and_invalidate_all() -> None:
    pool = _SpyPlatformPool()
    runtime = _SpyRuntime()
    state = SimpleNamespace(platform_mcp_pool_service=pool, agent_runtime=runtime)
    handlers = build_invalidation_handlers(state)
    await handlers["platform_mcp"](InvalidationEvent(kind="platform_mcp"))
    assert pool.calls == 1
    assert runtime.all_calls == 1


@pytest.mark.asyncio
async def test_user_mcp_oauth_handler_hits_pool_and_user_builds() -> None:
    tid = uuid4()
    pool = _SpyOAuthPool()
    runtime = _SpyRuntime()
    state = SimpleNamespace(user_mcp_oauth_pool_service=pool, agent_runtime=runtime)
    handlers = build_invalidation_handlers(state)
    await handlers["user_mcp_oauth"](
        InvalidationEvent(kind="user_mcp_oauth", tenant_id=str(tid), user_id="emp-7")
    )
    assert pool.calls == [(tid, "emp-7")]
    assert runtime.user_calls == [(tid, "emp-7")]


@pytest.mark.asyncio
async def test_agent_build_kind_handlers_hit_runtime() -> None:
    tid = uuid4()
    runtime = _SpyRuntime()
    state = SimpleNamespace(agent_runtime=runtime)
    handlers = build_invalidation_handlers(state)
    await handlers["agent_build"](InvalidationEvent(kind="agent_build", tenant_id=str(tid)))
    await handlers["agent_build_all"](InvalidationEvent(kind="agent_build_all"))
    await handlers["agent_build_user"](
        InvalidationEvent(kind="agent_build_user", tenant_id=str(tid), user_id="emp-2")
    )
    assert runtime.tenant_calls == [tid]
    assert runtime.all_calls == 1
    assert runtime.user_calls == [(tid, "emp-2")]


def test_kinds_constant_matches_the_wired_handlers() -> None:
    """``KINDS`` is the documented event vocabulary; the handler table is what
    the subscriber actually dispatches. A kind added to one and not the other
    is a silently-dropped event (``invalidation_bus.unknown_kind``), so the two
    are pinned to each other here."""
    assert set(build_invalidation_handlers(SimpleNamespace())) == KINDS


@pytest.mark.asyncio
async def test_handlers_are_noop_on_unwired_state() -> None:
    handlers = build_invalidation_handlers(SimpleNamespace())
    # Partially-wired apps (tests) must not crash the subscriber loop.
    for kind, handler in handlers.items():
        await handler(InvalidationEvent(kind=kind, tenant_id=str(uuid4()), user_id="emp-1"))


@pytest.mark.asyncio
async def test_end_to_end_publish_reaches_own_pod_handlers() -> None:
    # Handlers run on ALL pods including the publisher — self-delivery through
    # the (fake) broker must land in the wired caches.
    redis = _FakeRedis()
    bus = _bus(redis)
    tid = uuid4()
    pool = _SpyTenantPool()
    runtime = _SpyRuntime()
    state = SimpleNamespace(tenant_mcp_pool_service=pool, agent_runtime=runtime)
    bus.start(build_invalidation_handlers(state))
    try:
        await _wait_until(lambda: redis.queues)
        await bus.publish(InvalidationEvent(kind="tenant_mcp", tenant_id=str(tid)))
        await _wait_until(lambda: pool.calls and runtime.tenant_calls)
        assert pool.calls == [tid]
        assert runtime.tenant_calls == [tid]
    finally:
        await bus.stop()
