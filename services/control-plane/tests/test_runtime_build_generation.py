"""B-260 —— 智能体缓存的失效代次保护。

竞态:构建先拿到 MCP 池子客户端,失效(``invalidate_all`` / ``invalidate_tenant`` /
``invalidate_user``)随后关掉它们并清缓存,构建结束时再把 BuiltAgent 写回缓存 →
整个 TTL 里每次 run 都拿到已关闭的客户端。修法与三个池子相同:构建前记代次,入缓存
前比对,变了就丢弃重建(有上限)。

失效都在构建「进行中」发出:或由构建桩同步触发(等价于构建里某个 await 之后到达),
或用 ``asyncio.Event`` 把构建卡住再从外面发 —— 不靠 sleep。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, cast
from uuid import UUID, uuid4

import pytest

from control_plane import runtime as runtime_mod
from control_plane.runtime import MAX_AGENT_BUILD_ATTEMPTS, AgentRuntime
from expert_work.protocol import AgentSpec
from expert_work.runtime.runs import RunManager
from expert_work.runtime.stream_bridge import InMemoryStreamBridge

_MANIFEST: dict[str, Any] = {
    "apiVersion": "expert_work.io/v1",
    "kind": "Agent",
    "metadata": {"name": "x", "version": "1", "tenant": "test-tenant"},
    "spec": {
        "tenant_config": {},
        "model": {"provider": "anthropic", "name": "claude-haiku-4-5"},
        "system_prompt": {"template": "you help"},
        "sandbox": {
            "resources": {"cpu": "1.0", "memory": "1Gi"},
            "network": {"egress": "none", "allowlist": []},
            "filesystem": {"readonly_root": True, "writable": []},
        },
    },
}
_SPEC = AgentSpec.model_validate(_MANIFEST)


class _Built:
    """Stand-in BuiltAgent: just remembers which build produced it."""

    def __init__(self, n: int) -> None:
        self.n = n

    def __repr__(self) -> str:
        return f"_Built({self.n})"


class _Builder:
    """Counting builder; ``during`` runs inside build #n (1-based), i.e. after
    the build has started and before it returns — where the pool clients would
    already be in hand."""

    def __init__(self) -> None:
        self.calls = 0
        self.during: Callable[[int], Awaitable[None] | None] | None = None

    async def __call__(
        self, spec: AgentSpec, *, tenant_id: UUID | None = None, user_id: str | None = None
    ) -> _Built:
        self.calls += 1
        n = self.calls
        if self.during is not None:
            pending = self.during(n)
            if pending is not None:
                await pending
        return _Built(n)


def _runtime(builder: _Builder) -> AgentRuntime:
    return AgentRuntime(
        run_manager=RunManager(store=None),
        stream_bridge=InMemoryStreamBridge(),
        agent_builder=builder,  # type: ignore[arg-type]
    )


def _cached_ns(rt: AgentRuntime) -> list[int]:
    return [cast(_Built, b).n for b, _ in rt._cache.values()]


async def _get(rt: AgentRuntime, tenant: UUID, user_id: str | None = None) -> Any:
    return await rt.get_agent(tenant_id=tenant, name="x", version="1", spec=_SPEC, user_id=user_id)


def _invalidator(rt: AgentRuntime, scope: str, tenant: UUID) -> Callable[[], None]:
    if scope == "all":
        return rt.invalidate_all
    if scope == "tenant":
        return lambda: rt.invalidate_tenant(tenant)
    return lambda: rt.invalidate_user(tenant, "u1")


@pytest.mark.parametrize("scope", ["all", "tenant", "user"])
async def test_invalidation_mid_build_discards_and_rebuilds(scope: str) -> None:
    """失效落在构建进行中 → 这次构建既不入缓存也不交出去;重建的那份入缓存,
    下一次 get 直接命中它。"""
    builder = _Builder()
    rt = _runtime(builder)
    tenant = uuid4()
    invalidate = _invalidator(rt, scope, tenant)

    def _during(n: int) -> None:
        if n == 1:
            invalidate()

    builder.during = _during

    got = await _get(rt, tenant, user_id="u1")

    assert got.n == 2  # 撞上失效的 #1 没被交出去
    assert builder.calls == 2
    assert _cached_ns(rt) == [2]  # 缓存里是重建的那份
    assert (await _get(rt, tenant, user_id="u1")).n == 2
    assert builder.calls == 2


async def test_unrelated_tenant_invalidation_keeps_the_build() -> None:
    builder = _Builder()
    rt = _runtime(builder)
    tenant, other = uuid4(), uuid4()
    builder.during = lambda n: rt.invalidate_tenant(other)

    got = await _get(rt, tenant)

    assert got.n == 1
    assert builder.calls == 1
    assert len(rt._cache) == 1


async def test_unrelated_user_invalidation_keeps_the_build() -> None:
    """同租户别的用户、别的租户的同名用户,都不该丢弃这次构建。"""
    builder = _Builder()
    rt = _runtime(builder)
    tenant, other = uuid4(), uuid4()

    def _during(n: int) -> None:
        rt.invalidate_user(tenant, "u2")
        rt.invalidate_user(other, "u1")

    builder.during = _during

    got = await _get(rt, tenant, user_id="u1")

    assert got.n == 1
    assert builder.calls == 1
    assert len(rt._cache) == 1


async def test_retry_cap_serves_last_build_uncached() -> None:
    """每次构建都撞上失效 → 只构建 ``MAX_AGENT_BUILD_ATTEMPTS`` 次,交出最后一次
    且不入缓存;下一次 get 重新构建。"""
    builder = _Builder()
    rt = _runtime(builder)
    tenant = uuid4()
    builder.during = lambda n: rt.invalidate_tenant(tenant)

    got = await _get(rt, tenant)

    assert MAX_AGENT_BUILD_ATTEMPTS == 3
    assert builder.calls == MAX_AGENT_BUILD_ATTEMPTS
    assert got.n == MAX_AGENT_BUILD_ATTEMPTS
    assert len(rt._cache) == 0

    builder.during = None
    assert (await _get(rt, tenant)).n == MAX_AGENT_BUILD_ATTEMPTS + 1
    assert len(rt._cache) == 1


async def test_invalidated_build_not_served_even_when_catalog_uncacheable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """知识目录不符的构建本就只交出不入缓存;撞上失效时也不能就这么交出去。"""
    monkeypatch.setattr(runtime_mod, "catalog_build_cacheable", lambda *a, **k: False)
    builder = _Builder()
    rt = _runtime(builder)
    tenant = uuid4()

    def _during(n: int) -> None:
        if n == 1:
            rt.invalidate_tenant(tenant)

    builder.during = _during

    got = await _get(rt, tenant)

    assert got.n == 2
    assert len(rt._cache) == 0


async def test_concurrent_gets_late_starter_caches_early_one_reuses_it() -> None:
    """A 在失效前开始构建、B 在失效后开始:B 的构建入缓存;A 丢弃自己那份,
    重试时直接命中 B 的缓存,不再多构建一次。"""
    builder = _Builder()
    rt = _runtime(builder)
    tenant = uuid4()
    started = {1: asyncio.Event(), 2: asyncio.Event()}
    release = {1: asyncio.Event(), 2: asyncio.Event()}

    async def _hold(n: int) -> None:
        started[n].set()
        await release[n].wait()

    builder.during = lambda n: _hold(n) if n in started else None

    task_a = asyncio.create_task(_get(rt, tenant))
    await started[1].wait()
    rt.invalidate_tenant(tenant)
    task_b = asyncio.create_task(_get(rt, tenant))
    await started[2].wait()

    release[2].set()
    got_b = await task_b
    release[1].set()
    got_a = await task_a

    assert got_b.n == 2
    assert got_a is got_b
    assert builder.calls == 2
    assert _cached_ns(rt) == [2]


async def test_concurrent_gets_both_in_flight_both_rebuild() -> None:
    """两次 get 都在失效前开始:两份构建都不入缓存,各自重建;最终缓存里是
    失效之后的构建。"""
    builder = _Builder()
    rt = _runtime(builder)
    tenant = uuid4()
    started = {1: asyncio.Event(), 2: asyncio.Event()}
    release = asyncio.Event()

    async def _hold(n: int) -> None:
        started[n].set()
        await release.wait()

    builder.during = lambda n: _hold(n) if n in started else None

    task_a = asyncio.create_task(_get(rt, tenant))
    task_b = asyncio.create_task(_get(rt, tenant))
    await started[1].wait()
    await started[2].wait()
    rt.invalidate_tenant(tenant)
    release.set()
    got = await asyncio.gather(task_a, task_b)

    assert all(g.n > 2 for g in got)  # #1/#2 都撞上了失效
    cached = _cached_ns(rt)
    assert cached and all(n > 2 for n in cached)


async def test_oauth_refresh_while_keying_does_not_force_a_rebuild() -> None:
    """定键时取用户池,OAuth 刷新令牌会同步调 ``invalidate_user``(此时还没开始构建)
    —— 不该让这次构建被丢弃重来。"""
    from orchestrator.tools.mcp import MCPServerPool

    builder = _Builder()
    rt = _runtime(builder)
    tenant = uuid4()
    refreshed: list[int] = []
    pool = MCPServerPool()

    async def _provider(tenant_id: UUID, user_id: str) -> MCPServerPool:
        if not refreshed:
            refreshed.append(1)
            rt.invalidate_user(tenant_id, user_id)  # 刷新令牌后的本地失效
        return pool

    rt.user_oauth_pool_provider = _provider

    got = await _get(rt, tenant, user_id="u1")

    assert refreshed == [1]
    assert got.n == 1
    assert builder.calls == 1
    assert _cached_ns(rt) == [1]


async def test_user_connecting_mid_build_never_lands_under_the_shared_key() -> None:
    """定键时用户 A 没有授权连接(共享键,同租户所有人共用),构建过程中 A 连上了
    (池子换成新对象,运行时失效还没到)。构建挂上了 A 的池 → 不能按共享键入缓存,
    否则同租户别的用户命中它、拿 A 的令牌调 A 的工具。"""
    from orchestrator.tools.mcp import MCPServerPool, MCPToolDef, RecordingMCPClient

    builder = _Builder()
    rt = _runtime(builder)
    tenant = uuid4()
    pools = {"A": MCPServerPool(), "B": MCPServerPool()}

    async def _provider(tenant_id: UUID, user_id: str) -> MCPServerPool:
        return pools[user_id]

    async def _connect_a(n: int) -> None:
        if n == 1:
            # 池子先出缓存换新(连上),运行时的 ``invalidate_user`` 在 ``await
            # pool.close_all()`` 之后才到 —— 这次构建先比完了。
            connected = MCPServerPool()
            tools = (MCPToolDef(name="t", description="", input_schema={}),)
            await connected.add("linear", RecordingMCPClient(tools=tools))
            pools["A"] = connected

    rt.user_oauth_pool_provider = _provider
    builder.during = _connect_a

    await _get(rt, tenant, user_id="A")
    shared = [k for k in rt._cache if k[4] is None]
    assert shared == []
    # B 没有授权连接 → 共享键;必须自己构建,不能拿到 A 的那份。
    got_b = await _get(rt, tenant, user_id="B")
    assert got_b.n == builder.calls
    assert got_b.n != 1
