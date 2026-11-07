"""Unit tests for the per-tenant remote MCP pool service (Stream V-D)."""

from __future__ import annotations

import asyncio
from uuid import UUID, uuid4

import pytest

from control_plane.tenant_mcp_pool import (
    _FAILED_RETRY_S,
    McpClientFactory,
    TenantMcpPoolService,
)
from expert_work.persistence import InMemoryTenantMcpServerStore
from orchestrator.tools.mcp import MCPServerConfig, MCPToolDef, RecordingMCPClient


def _client_factory_spy(calls: list[str]) -> McpClientFactory:
    async def _factory(config: MCPServerConfig):
        calls.append(config.name)
        return RecordingMCPClient(tools=(MCPToolDef(name="t", description="", input_schema={}),))

    return _factory


async def _seed(store: InMemoryTenantMcpServerStore, tenant_id, name="github", enabled=True):
    rec = await store.create(
        tenant_id=tenant_id,
        name=name,
        transport="streamable_http",
        url="https://mcp.example.com/mcp",
        auth_type="none",
        token_secret_ref=None,
        timeout_s=30.0,
        created_by="a@x",
    )
    if not enabled:
        from expert_work.protocol import TenantMcpServerPatch

        await store.update(
            tenant_id=tenant_id,
            name=name,
            patch=TenantMcpServerPatch(enabled=False),
        )
    return rec


@pytest.mark.asyncio
async def test_builds_pool_from_enabled_servers() -> None:
    store = InMemoryTenantMcpServerStore()
    tid = uuid4()
    await _seed(store, tid, "github")
    calls: list[str] = []
    svc = TenantMcpPoolService(
        store=store, secret_store=None, client_factory=_client_factory_spy(calls)
    )
    pool = await svc.get_or_build(tid)
    assert pool.names() == ["github"]
    assert calls == ["github"]


@pytest.mark.asyncio
async def test_disabled_servers_excluded() -> None:
    store = InMemoryTenantMcpServerStore()
    tid = uuid4()
    await _seed(store, tid, "github", enabled=True)
    await _seed(store, tid, "linear", enabled=False)
    svc = TenantMcpPoolService(
        store=store, secret_store=None, client_factory=_client_factory_spy([])
    )
    pool = await svc.get_or_build(tid)
    assert pool.names() == ["github"]


@pytest.mark.asyncio
async def test_second_call_returns_cached_pool_no_rebuild() -> None:
    store = InMemoryTenantMcpServerStore()
    tid = uuid4()
    await _seed(store, tid, "github")
    calls: list[str] = []
    svc = TenantMcpPoolService(
        store=store, secret_store=None, client_factory=_client_factory_spy(calls)
    )
    p1 = await svc.get_or_build(tid)
    p2 = await svc.get_or_build(tid)
    assert p1 is p2
    assert calls == ["github"]  # built once


@pytest.mark.asyncio
async def test_invalidate_closes_and_rebuilds() -> None:
    store = InMemoryTenantMcpServerStore()
    tid = uuid4()
    await _seed(store, tid, "github")
    calls: list[str] = []
    svc = TenantMcpPoolService(
        store=store, secret_store=None, client_factory=_client_factory_spy(calls)
    )
    p1 = await svc.get_or_build(tid)
    await svc.invalidate(tid)
    p2 = await svc.get_or_build(tid)
    assert p1 is not p2
    assert calls == ["github", "github"]  # rebuilt


@pytest.mark.asyncio
async def test_empty_when_no_servers() -> None:
    store = InMemoryTenantMcpServerStore()
    svc = TenantMcpPoolService(
        store=store, secret_store=None, client_factory=_client_factory_spy([])
    )
    pool = await svc.get_or_build(uuid4())
    assert pool.names() == []


@pytest.mark.asyncio
async def test_close_all_clears_cache() -> None:
    store = InMemoryTenantMcpServerStore()
    tid = uuid4()
    await _seed(store, tid, "github")
    svc = TenantMcpPoolService(
        store=store, secret_store=None, client_factory=_client_factory_spy([])
    )
    await svc.get_or_build(tid)
    await svc.close_all()
    # after close_all a fresh build is required again (cache cleared)
    assert svc._pools == {}


@pytest.mark.asyncio
async def test_invalidation_during_build_is_not_lost() -> None:
    """Mid-build invalidation must not leave a stale pool in the cache.

    The build is paused mid-flight via an asyncio.Event.  Invalidation lands
    while the build holds the tenant lock but has not yet written to _pools.
    After the build completes the cache must be empty so the next caller
    triggers a fresh rebuild — proving the generation counter prevents the
    lost-invalidation race.
    """
    import asyncio

    store = InMemoryTenantMcpServerStore()
    tid = uuid4()
    await _seed(store, tid, "github")

    started = asyncio.Event()
    release = asyncio.Event()

    async def _blocking_factory(config: MCPServerConfig) -> RecordingMCPClient:
        started.set()
        await release.wait()  # pause mid-build until invalidation has landed
        return RecordingMCPClient(tools=())

    svc = TenantMcpPoolService(store=store, secret_store=None, client_factory=_blocking_factory)

    build_task = asyncio.create_task(svc.get_or_build(tid))
    await started.wait()  # build is in-flight, pool not yet cached

    await svc.invalidate(tid)  # invalidation lands mid-build

    release.set()  # unblock the build
    served = await build_task

    # audit #2: the invalidation landed mid-build, so the first pool is closed
    # and the build is RETRIED — the in-flight caller gets a fresh, *usable*
    # pool (not a closed, empty one), and that fresh pool IS cached.
    assert served is not None
    assert served.names() == ["github"]  # usable, not an empty closed pool
    cached_entry = svc._pools.get(tid)
    assert cached_entry is not None and cached_entry[0] is served  # rebuilt fresh and cached


@pytest.mark.asyncio
async def test_servers_beyond_cap_are_closed_not_leaked() -> None:
    store = InMemoryTenantMcpServerStore()
    tid = uuid4()
    for i in range(6):  # one over the default cap of 5
        await _seed(store, tid, f"srv{i}")
    closed: list[str] = []

    def _factory() -> McpClientFactory:
        async def _f(config: MCPServerConfig) -> RecordingMCPClient:
            client = RecordingMCPClient(tools=())
            orig_close = client.close
            name = config.name

            async def _tracked() -> None:
                closed.append(name)
                await orig_close()

            client.close = _tracked  # type: ignore[method-assign]
            return client

        return _f

    svc = TenantMcpPoolService(store=store, secret_store=None, client_factory=_factory())
    pool = await svc.get_or_build(tid)
    assert len(pool.names()) == 5  # cap enforced
    assert len(closed) >= 1  # the over-cap client was closed, not leaked


@pytest.mark.asyncio
async def test_ttl_backstop_rebuilds_expired_pool_and_reuses_fresh_one() -> None:
    """PR-E3a: entries older than 1800s rebuild (old pool closed); younger reuse."""
    store = InMemoryTenantMcpServerStore()
    tid = uuid4()
    await _seed(store, tid, "github")
    calls: list[str] = []
    now = {"t": 0.0}
    svc = TenantMcpPoolService(
        store=store,
        secret_store=None,
        client_factory=_client_factory_spy(calls),
        clock=lambda: now["t"],
    )
    p1 = await svc.get_or_build(tid)
    now["t"] = 1799.0
    assert await svc.get_or_build(tid) is p1  # younger than TTL → reused
    assert calls == ["github"]
    now["t"] = 1801.0
    p2 = await svc.get_or_build(tid)
    assert p2 is not p1  # older than TTL → rebuilt
    assert calls == ["github", "github"]
    assert p1.names() == []  # the expired pool was closed (close_all clears clients)
    assert p2.names() == ["github"]


# ---------------------------------------------------------------------------
# B-292 —— 某个服务连接失败:后台退避重试,不让缓存池把它的工具藏 30 分钟
# ---------------------------------------------------------------------------


class _ManualSleep:
    """注入给池的 sleep:记下每次要睡多久,等测试放行。"""

    def __init__(self) -> None:
        self.durations: list[float] = []
        self._gates: list[asyncio.Event] = []
        self._entered = asyncio.Event()

    async def __call__(self, seconds: float) -> None:
        self.durations.append(seconds)
        self._entered.set()
        gate = asyncio.Event()
        self._gates.append(gate)
        await gate.wait()

    async def sleeping(self, count: int) -> None:
        """等到第 ``count`` 次 sleep 开始(后台重试是 create_task 起的,要让它跑起来)。"""
        async with asyncio.timeout(1):
            while len(self.durations) < count:
                self._entered.clear()
                await self._entered.wait()

    async def settle(self) -> None:
        """让已排的任务都跑到下一个挂起点(断言「没有再排」之前用)。"""
        for _ in range(5):
            await asyncio.sleep(0)

    def wake(self) -> None:
        self._gates[-1].set()


class _Dialer:
    """client factory 替身:``down`` 里的连不上;``gates`` 里的下一次拨号卡住直到放行
    (一次性);``created`` 记下每个服务拨出来的客户端。"""

    def __init__(self, down: set[str]) -> None:
        self.calls: list[str] = []
        self.down = set(down)
        self.gates: dict[str, asyncio.Event] = {}
        self.dialing = asyncio.Event()  # a gated dial has started
        self.created: dict[str, list[RecordingMCPClient]] = {}
        self.gated: list[RecordingMCPClient] = []  # clients whose dial went through a gate

    async def __call__(self, config: MCPServerConfig) -> RecordingMCPClient:
        self.calls.append(config.name)
        gate = self.gates.pop(config.name, None)
        if gate is not None:
            self.dialing.set()
            await gate.wait()
        if config.name in self.down:
            raise ConnectionError("server not ready")
        client = RecordingMCPClient(tools=(MCPToolDef(name="t", description="", input_schema={}),))
        self.created.setdefault(config.name, []).append(client)
        if gate is not None:
            self.gated.append(client)
        return client

    def last(self, name: str) -> RecordingMCPClient:
        return self.created[name][-1]

    async def gate_next(self, name: str) -> asyncio.Event:
        """Make the next dial of ``name`` hang; returns the gate that releases it."""
        self.dialing.clear()
        gate = asyncio.Event()
        self.gates[name] = gate
        return gate


class _Rig:
    def __init__(self, store: InMemoryTenantMcpServerStore, down: set[str]) -> None:
        self.tid = uuid4()
        self.dialer = _Dialer(down)
        self.sleeper = _ManualSleep()
        self.expired: list[UUID] = []
        self.now = {"t": 0.0}
        self.svc = TenantMcpPoolService(
            store=store,
            secret_store=None,
            client_factory=self.dialer,
            on_expire=self.expired.append,
            clock=lambda: self.now["t"],
            sleep=self.sleeper,
        )

    async def wake_retry(self) -> None:
        """放行排着的那一次重试并等它跑完。"""
        await self.sleeper.settle()
        (retry,) = self.svc._retry_tasks
        self.sleeper.wake()
        async with asyncio.timeout(1):
            await retry

    async def start_gated_retry(self, name: str) -> tuple[asyncio.Task[None], asyncio.Event]:
        """服务恢复;放行重试并让它卡在拨 ``name`` 上。返回重试任务和放行拨号的闸。"""
        await self.sleeper.sleeping(1)
        (retry,) = self.svc._retry_tasks
        self.dialer.down.clear()
        gate = await self.dialer.gate_next(name)
        self.sleeper.wake()
        async with asyncio.timeout(1):
            await self.dialer.dialing.wait()
        return retry, gate


async def _rig(*servers: str, down: set[str]) -> _Rig:
    store = InMemoryTenantMcpServerStore()
    rig = _Rig(store, down)
    for name in servers:
        await _seed(store, rig.tid, name)
    return rig


@pytest.mark.asyncio
async def test_failed_server_merged_after_backoff_keeps_healthy_client_and_evicts_builds() -> None:
    rig = await _rig("a", "b", down={"b"})
    p1 = await rig.svc.get_or_build(rig.tid)
    assert p1.names() == ["a"]
    a_client = p1.get("a")
    await rig.sleeper.sleeping(1)
    assert rig.sleeper.durations == [_FAILED_RETRY_S]
    assert await rig.svc.get_or_build(rig.tid) is p1  # inside the window: no redial
    assert rig.dialer.calls == ["a", "b"]

    rig.dialer.down.clear()  # the server finished restarting
    await rig.wake_retry()

    assert await rig.svc.get_or_build(rig.tid) is p1  # merged into the cached pool
    assert p1.names() == ["a", "b"]
    assert rig.dialer.calls == ["a", "b", "b"]  # only the missing server redialed
    assert p1.get("a") is a_client and not rig.dialer.last("a").closed
    assert rig.expired == [rig.tid]  # builds from the incomplete pool evicted once
    await rig.sleeper.settle()
    assert rig.sleeper.durations == [_FAILED_RETRY_S]  # complete: no further retry
    await rig.svc.close_all()


@pytest.mark.asyncio
async def test_still_failing_server_backs_off_until_pool_ttl_without_evicting_builds() -> None:
    rig = await _rig("a", "b", down={"b"})
    p1 = await rig.svc.get_or_build(rig.tid)
    await rig.sleeper.sleeping(1)
    for _ in range(8):  # bounded: a chain that never ends must fail, not hang
        if not rig.svc._retry_tasks:
            break
        await rig.wake_retry()
        await rig.sleeper.settle()
    # Doubling backoff; the chain stops before it would outlive the pool's TTL.
    assert rig.sleeper.durations == [60.0, 120.0, 240.0, 480.0, 960.0]
    assert rig.dialer.calls == ["a", "b"] + ["b"] * 5
    assert await rig.svc.get_or_build(rig.tid) is p1
    assert p1.names() == ["a"] and not rig.dialer.last("a").closed
    assert rig.expired == []  # nothing gained → cached builds stay


@pytest.mark.asyncio
async def test_retry_dial_does_not_block_get_or_build() -> None:
    rig = await _rig("a", "b", down={"b"})
    p1 = await rig.svc.get_or_build(rig.tid)
    retry, gate = await rig.start_gated_retry("b")
    async with asyncio.timeout(1):  # the retry is stuck dialing b
        assert await rig.svc.get_or_build(rig.tid) is p1
    gate.set()
    await retry
    assert p1.names() == ["a", "b"]
    await rig.svc.close_all()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "event", ["invalidate", "server_changed", "ttl_expired", "ttl_passed_idle"]
)
async def test_retry_landing_after_pool_changed_closes_what_it_opened(event: str) -> None:
    rig = await _rig("a", "b", down={"b"})
    p1 = await rig.svc.get_or_build(rig.tid)
    retry, gate = await rig.start_gated_retry("b")

    if event == "invalidate":
        await rig.svc.invalidate(rig.tid)
    elif event == "server_changed":
        await rig.svc.server_changed(rig.tid, "b")
    elif event == "ttl_passed_idle":
        rig.now["t"] = 1801.0  # still cached, but expired: nobody asked for it yet
    else:
        rig.now["t"] = 1801.0
        async with asyncio.timeout(1):  # TTL path rebuilds (b is up now)
            rebuilt = await rig.svc.get_or_build(rig.tid)
        assert rebuilt is not p1 and rebuilt.names() == ["a", "b"]
    evicted_before = list(rig.expired)

    gate.set()
    await retry
    (opened,) = rig.dialer.gated  # the retry's client
    assert opened.closed  # stale result: closed, not merged, not leaked
    assert "b" not in p1.names()
    assert rig.expired == evicted_before  # the stale retry evicts nothing
    await rig.svc.close_all()


@pytest.mark.asyncio
async def test_retry_waking_after_pool_ttl_does_not_dial() -> None:
    rig = await _rig("a", "b", down={"b"})
    p1 = await rig.svc.get_or_build(rig.tid)
    rig.dialer.down.clear()
    rig.now["t"] = 1800.0  # the pool expired while the retry slept
    await rig.sleeper.sleeping(1)
    await rig.wake_retry()
    assert rig.dialer.calls == ["a", "b"]  # left to the TTL path
    assert p1.names() == ["a"]
    p2 = await rig.svc.get_or_build(rig.tid)
    assert p2 is not p1 and p2.names() == ["a", "b"]
    await rig.svc.close_all()


@pytest.mark.asyncio
async def test_retry_for_a_replaced_pool_does_not_dial() -> None:
    rig = await _rig("a", "b", down={"b"})
    await rig.svc.get_or_build(rig.tid)
    await rig.sleeper.sleeping(1)
    rig.dialer.down.clear()
    await rig.svc.invalidate(rig.tid)
    complete = await rig.svc.get_or_build(rig.tid)  # rebuilt complete before the retry woke
    assert complete.names() == ["a", "b"]
    calls = list(rig.dialer.calls)
    await rig.wake_retry()
    assert rig.dialer.calls == calls
    assert rig.expired == []
    await rig.svc.close_all()


@pytest.mark.asyncio
async def test_close_all_mid_retry_closes_opened_clients_and_stops_scheduling() -> None:
    rig = await _rig("a", "b", "c", down={"b", "c"})
    await rig.svc.get_or_build(rig.tid)
    retry, _gate = await rig.start_gated_retry("c")  # b dialed fine, c hangs
    await rig.svc.close_all()
    assert retry.cancelled()
    assert rig.dialer.last("b").closed  # opened by the retry, not leaked

    rig.dialer.down.add("b")
    await rig.svc.get_or_build(rig.tid)  # a degraded build after shutdown …
    await rig.sleeper.settle()
    assert not rig.svc._retry_tasks  # … schedules no retry nobody would cancel


@pytest.mark.asyncio
async def test_merged_server_is_carried_by_b259_like_any_member() -> None:
    rig = await _rig("a", "b", down={"b"})
    p1 = await rig.svc.get_or_build(rig.tid)
    rig.dialer.down.clear()
    await rig.sleeper.sleeping(1)
    await rig.wake_retry()
    b_client = p1.get("b")
    await rig.svc.server_changed(rig.tid, "a")  # B-259: only a is retired, b is carried
    p2 = await rig.svc.get_or_build(rig.tid)
    assert p2.get("b") is b_client  # reused, not redialed
    assert rig.dialer.calls == ["a", "b", "b", "a"]
    await rig.svc.close_all()


@pytest.mark.asyncio
async def test_rebuilt_degraded_pool_reuses_the_pending_retry() -> None:
    rig = await _rig("a", "b", down={"b"})
    await rig.svc.get_or_build(rig.tid)
    await rig.sleeper.sleeping(1)
    await rig.svc.invalidate(rig.tid)
    p2 = await rig.svc.get_or_build(rig.tid)  # rebuilt, b still down
    await rig.sleeper.settle()
    assert len(rig.svc._retry_tasks) == 1  # no second retry chain for the tenant
    rig.dialer.down.clear()
    await rig.wake_retry()  # the pending retry now serves the rebuilt pool
    assert p2.names() == ["a", "b"]
    await rig.svc.close_all()


@pytest.mark.asyncio
async def test_retried_server_that_no_longer_fits_is_closed_and_not_retried() -> None:
    # a0 fails first, then s1..s5 fill the pool to its cap of 5.
    rig = await _rig("a0", "s1", "s2", "s3", "s4", "s5", down={"a0"})
    p1 = await rig.svc.get_or_build(rig.tid)
    assert p1.names() == ["s1", "s2", "s3", "s4", "s5"]
    rig.dialer.down.clear()
    await rig.sleeper.sleeping(1)
    await rig.wake_retry()
    assert rig.dialer.last("a0").closed  # dialed, could not fit: closed, not leaked
    assert "a0" not in p1.names()
    assert rig.expired == []
    await rig.sleeper.settle()
    assert not rig.svc._retry_tasks  # cannot fit until a rebuild: no more retries
    await rig.svc.close_all()
