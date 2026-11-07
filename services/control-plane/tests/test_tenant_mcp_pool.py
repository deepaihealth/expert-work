"""Unit tests for the per-tenant remote MCP pool service (Stream V-D)."""

from __future__ import annotations

import asyncio
from uuid import uuid4

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
# B-292 —— 某个服务连接失败:隔一段时间重试,不让缓存池把它的工具藏 30 分钟
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


def _flaky_factory(calls: list[str], down: set[str]) -> McpClientFactory:
    """``down`` 里的服务连不上(还没就绪);其余正常。"""

    async def _factory(config: MCPServerConfig) -> RecordingMCPClient:
        calls.append(config.name)
        if config.name in down:
            raise ConnectionError("server not ready")
        return RecordingMCPClient(tools=(MCPToolDef(name="t", description="", input_schema={}),))

    return _factory


async def _run_pending_retry(svc: TenantMcpPoolService, sleeper: _ManualSleep) -> None:
    (retry,) = svc._retry_tasks
    sleeper.wake()
    await retry


@pytest.mark.asyncio
async def test_failed_server_retried_after_backoff_reuses_healthy_and_evicts_builds() -> None:
    store = InMemoryTenantMcpServerStore()
    tid = uuid4()
    await _seed(store, tid, "a")
    await _seed(store, tid, "b")
    calls: list[str] = []
    down = {"b"}
    expired: list[object] = []
    sleeper = _ManualSleep()
    svc = TenantMcpPoolService(
        store=store,
        secret_store=None,
        client_factory=_flaky_factory(calls, down),
        on_expire=expired.append,
        sleep=sleeper,
    )
    p1 = await svc.get_or_build(tid)
    assert p1.names() == ["a"]
    a_client = p1.get("a")
    await sleeper.sleeping(1)
    assert sleeper.durations == [_FAILED_RETRY_S]  # one retry scheduled
    # Requests inside the backoff window serve the cached pool, no redial.
    assert await svc.get_or_build(tid) is p1
    assert calls == ["a", "b"]

    down.clear()  # the server finished restarting
    await _run_pending_retry(svc, sleeper)

    p2 = await svc.get_or_build(tid)
    assert p2.names() == ["a", "b"]
    assert calls == ["a", "b", "b"]  # only the failed server was redialed
    assert p2.get("a") is a_client  # healthy long-lived client reused …
    assert isinstance(a_client, RecordingMCPClient) and not a_client.closed  # … not torn down
    assert expired == [tid]  # builds made from the incomplete pool are evicted
    await sleeper.settle()
    assert sleeper.durations == [_FAILED_RETRY_S]  # complete pool: no further retry
    await svc.close_all()


@pytest.mark.asyncio
async def test_still_failing_server_retried_once_per_window_without_evicting_builds() -> None:
    store = InMemoryTenantMcpServerStore()
    tid = uuid4()
    await _seed(store, tid, "a")
    await _seed(store, tid, "b")
    calls: list[str] = []
    expired: list[object] = []
    sleeper = _ManualSleep()
    svc = TenantMcpPoolService(
        store=store,
        secret_store=None,
        client_factory=_flaky_factory(calls, {"b"}),
        on_expire=expired.append,
        sleep=sleeper,
    )
    p1 = await svc.get_or_build(tid)
    a_client = p1.get("a")

    await sleeper.sleeping(1)
    await _run_pending_retry(svc, sleeper)

    p2 = await svc.get_or_build(tid)
    assert p2.names() == ["a"]
    assert calls == ["a", "b", "b"]  # one redial of the failed server, healthy one untouched
    assert p2.get("a") is a_client
    assert isinstance(a_client, RecordingMCPClient) and not a_client.closed
    assert expired == []  # nothing gained → cached builds stay
    await sleeper.sleeping(2)
    assert sleeper.durations == [_FAILED_RETRY_S, _FAILED_RETRY_S]  # next window scheduled
    assert await svc.get_or_build(tid) is p2  # no redial per request
    assert calls == ["a", "b", "b"]

    (pending,) = svc._retry_tasks
    await svc.close_all()
    assert pending.cancelled()  # shutdown cancels the pending retry
