"""B-260 —— 子智能体构建缓存(``make_child_agent_builder``)的失效代次保护。

与主智能体缓存同一个竞态:子代构建先拿到 MCP 池子客户端,失效随后关掉它们并清
缓存,构建结束时再写回缓存 → 整个 TTL 里委派都拿到已关闭的客户端。失效走真实的
``AgentRuntime.invalidate_*`` → 注册的挂钩,和线上同一条路。

失效都在构建「进行中」发出:或由 ``build_agent`` 桩同步触发,或用 ``asyncio.Event``
把构建卡住再从外面发 —— 不靠 sleep。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import UUID, uuid4

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from control_plane import subagent_runtime as subagent_mod
from control_plane.runtime import MAX_AGENT_BUILD_ATTEMPTS, AgentRuntime
from control_plane.subagent_runtime import make_child_agent_builder
from expert_work.persistence.agent_spec import InMemoryAgentSpecStore
from expert_work.protocol import AgentSpec, AgentSpecRecord
from expert_work.runtime.runs import RunManager
from expert_work.runtime.stream_bridge import InMemoryStreamBridge
from expert_work.testing import InMemorySecretStore
from orchestrator import BuiltAgent, ToolEnv
from orchestrator.tools import ChildAgentBuilder, MCPServerPool, RecordingMCPClient

_SHA = "a" * 64

_SPEC = AgentSpec.model_validate(
    {
        "apiVersion": "expert_work.io/v1",
        "kind": "Agent",
        "metadata": {"name": "researcher", "version": "1.0.0", "tenant": "t"},
        "spec": {
            "tenant_config": {},
            "model": {"provider": "anthropic", "name": "claude"},
            "system_prompt": {"template": "x"},
            "sandbox": {
                "resources": {"cpu": "1", "memory": "1Gi"},
                "network": {"egress": "proxy", "allowlist": ["a.com"]},
                "filesystem": {},
            },
        },
    }
)


class _Builds:
    """Stands in for ``build_agent``; build #n returns ``system_prompt="b<n>"``.
    ``during`` runs inside build #n, after it started and before it returns."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.during: Callable[[int], Awaitable[None] | None] | None = None

    async def __call__(self, spec: AgentSpec, **kwargs: Any) -> BuiltAgent:
        self.calls.append(kwargs)
        n = len(self.calls)
        if self.during is not None:
            pending = self.during(n)
            if pending is not None:
                _ = await pending  # 构建在这里卡住,等外面发失效
        return BuiltAgent(graph=object(), system_prompt=f"b{n}", max_steps=1)  # type: ignore[arg-type]


@pytest.fixture
def builds(monkeypatch: pytest.MonkeyPatch) -> _Builds:
    fake = _Builds()
    monkeypatch.setattr(subagent_mod, "build_agent", fake)
    return fake


def _runtime() -> AgentRuntime:
    async def _unused(spec: AgentSpec, **_: Any) -> BuiltAgent:
        raise AssertionError("top-level build not expected")

    return AgentRuntime(
        run_manager=RunManager(store=None),
        stream_bridge=InMemoryStreamBridge(),
        agent_builder=_unused,
    )


async def _store(*tenants: UUID) -> InMemoryAgentSpecStore:
    store = InMemoryAgentSpecStore()
    for tenant in tenants:
        await store.create(tenant_id=tenant, spec=_SPEC, spec_sha256=_SHA, created_by="test")
    return store


def _child_builder(
    rt: AgentRuntime,
    store: Any,
    user_pool_provider: Callable[[UUID, str], Awaitable[MCPServerPool]] | None = None,
) -> ChildAgentBuilder:
    return make_child_agent_builder(
        spec_store=store,
        secret_store=InMemorySecretStore(),  # type: ignore[arg-type]
        checkpointer=InMemorySaver(),
        base_tool_env=ToolEnv(),
        user_mcp_oauth_pool_provider=user_pool_provider,
        register_invalidation=rt.register_invalidation_hook,
        register_invalidation_all=rt.register_invalidation_all,
        register_user_invalidation=rt.register_user_invalidation_hook,
    )


async def _delegate(builder: ChildAgentBuilder, tenant: UUID, user: str | None = None) -> str:
    built = await builder(
        tenant_id=tenant, name="researcher", version="1.0.0", depth=1, oauth_user_id=user
    )
    return built.system_prompt


def _invalidator(rt: AgentRuntime, scope: str, tenant: UUID) -> Callable[[], None]:
    if scope == "all":
        return rt.invalidate_all
    if scope == "tenant":
        return lambda: rt.invalidate_tenant(tenant)
    return lambda: rt.invalidate_user(tenant, "u1")


@pytest.mark.parametrize("scope", ["all", "tenant", "user"])
async def test_invalidation_mid_child_build_discards_and_rebuilds(
    builds: _Builds, scope: str
) -> None:
    """失效落在子代构建进行中 → 这次构建既不交出也不缓存;重建的那份入缓存,
    下一次委派直接命中。"""
    rt = _runtime()
    tenant = uuid4()
    builder = _child_builder(rt, await _store(tenant))
    invalidate = _invalidator(rt, scope, tenant)

    def _during(n: int) -> None:
        if n == 1:
            invalidate()

    builds.during = _during

    assert await _delegate(builder, tenant, "u1") == "b2"
    assert len(builds.calls) == 2
    assert await _delegate(builder, tenant, "u1") == "b2"
    assert len(builds.calls) == 2


async def test_unrelated_invalidations_keep_the_child_build(builds: _Builds) -> None:
    """别的租户、同租户别的用户、别的租户里的同名用户失效,都不丢弃这次子代构建。"""
    rt = _runtime()
    tenant, other = uuid4(), uuid4()
    builder = _child_builder(rt, await _store(tenant))

    def _during(n: int) -> None:
        rt.invalidate_tenant(other)
        rt.invalidate_user(tenant, "u2")
        rt.invalidate_user(other, "u1")

    builds.during = _during

    assert await _delegate(builder, tenant, "u1") == "b1"
    assert len(builds.calls) == 1
    assert await _delegate(builder, tenant, "u1") == "b1"
    assert len(builds.calls) == 1


async def test_child_retry_cap_serves_last_build_uncached(builds: _Builds) -> None:
    rt = _runtime()
    tenant = uuid4()
    builder = _child_builder(rt, await _store(tenant))
    builds.during = lambda n: rt.invalidate_tenant(tenant)

    assert await _delegate(builder, tenant) == f"b{MAX_AGENT_BUILD_ATTEMPTS}"
    assert len(builds.calls) == MAX_AGENT_BUILD_ATTEMPTS

    builds.during = None
    # 上一次没入缓存 → 重建
    assert await _delegate(builder, tenant) == f"b{MAX_AGENT_BUILD_ATTEMPTS + 1}"
    assert await _delegate(builder, tenant) == f"b{MAX_AGENT_BUILD_ATTEMPTS + 1}"


async def test_concurrent_child_builds_late_starter_caches_early_one_reuses_it(
    builds: _Builds,
) -> None:
    """A 在失效前开始构建、B 在失效后开始:B 的入缓存;A 丢弃自己那份,重试时直接
    命中 B 的缓存,不再多构建一次。"""
    rt = _runtime()
    tenant = uuid4()
    builder = _child_builder(rt, await _store(tenant))
    started = {1: asyncio.Event(), 2: asyncio.Event()}
    release = {1: asyncio.Event(), 2: asyncio.Event()}

    async def _hold(n: int) -> None:
        started[n].set()
        await release[n].wait()

    builds.during = lambda n: _hold(n) if n in started else None

    task_a = asyncio.create_task(_delegate(builder, tenant))
    await started[1].wait()
    rt.invalidate_tenant(tenant)
    task_b = asyncio.create_task(_delegate(builder, tenant))
    await started[2].wait()

    release[2].set()
    assert await task_b == "b2"
    release[1].set()
    assert await task_a == "b2"
    assert len(builds.calls) == 2


async def _oauth_pool() -> MCPServerPool:
    pool = MCPServerPool()
    await pool.add("linear", RecordingMCPClient())
    return pool


async def test_oauth_refresh_while_keying_does_not_force_a_child_rebuild(
    builds: _Builds,
) -> None:
    """定键时取用户池,OAuth 刷新令牌会同步调 ``invalidate_user``(此时还没开始构建)
    —— 不该让这次子代构建被丢弃重来。"""
    rt = _runtime()
    tenant = uuid4()
    pool = await _oauth_pool()
    refreshed: list[int] = []

    async def _provider(tenant_id: UUID, user_id: str) -> MCPServerPool:
        if not refreshed:
            refreshed.append(1)
            rt.invalidate_user(tenant_id, user_id)  # 刷新令牌后的本地失效
        return pool

    builder = _child_builder(rt, await _store(tenant), _provider)

    assert await _delegate(builder, tenant, "u1") == "b1"
    assert refreshed == [1]
    assert len(builds.calls) == 1


async def test_pool_replaced_while_keying_rekeys_and_attaches_the_new_pool(
    builds: _Builds,
) -> None:
    """定键时拿到的用户池随即被失效换掉(失效落在读 manifest 的 await 上)—— 重新定键、
    不白构建,构建挂新池,而不是定键时那份已关闭的旧池。"""
    rt = _runtime()
    tenant = uuid4()
    pools = {"current": await _oauth_pool()}
    old_pool = pools["current"]
    inner = await _store(tenant)
    replaced: list[int] = []

    class _Store:
        async def get(self, **kwargs: Any) -> AgentSpecRecord | None:
            if not replaced:
                replaced.append(1)
                # 池子先失效(换新池)、运行时后失效,与线上顺序相同。
                pools["current"] = await _oauth_pool()
                rt.invalidate_user(tenant, "u1")
            return await inner.get(**kwargs)

    async def _provider(tenant_id: UUID, user_id: str) -> MCPServerPool:
        return pools["current"]

    builder = _child_builder(rt, _Store(), _provider)

    assert await _delegate(builder, tenant, "u1") == "b1"
    assert len(builds.calls) == 1
    attached = builds.calls[0]["tool_env"].user_mcp_oauth_pool
    assert attached is pools["current"]
    assert attached is not old_pool


async def test_user_connecting_while_keying_never_lands_under_the_shared_key(
    builds: _Builds,
) -> None:
    """定键时用户 A 没有授权连接(共享键),读 manifest 期间 A 连上了。构建挂的必须是
    定键时那份(空),共享键下的那份不能带着 A 的池 —— 否则同租户 B 委派时命中它、
    拿 A 的令牌调 A 的工具。"""
    rt = _runtime()
    tenant = uuid4()
    inner = await _store(tenant)
    pools: dict[str, MCPServerPool] = {"A": MCPServerPool(), "B": MCPServerPool()}
    connected: list[int] = []

    class _Store:
        async def get(self, **kwargs: Any) -> AgentSpecRecord | None:
            if not connected:
                connected.append(1)
                pools["A"] = await _oauth_pool()
                rt.invalidate_user(tenant, "A")
            return await inner.get(**kwargs)

    async def _provider(tenant_id: UUID, user_id: str) -> MCPServerPool:
        return pools[user_id]

    builder = _child_builder(rt, _Store(), _provider)

    await _delegate(builder, tenant, "A")
    await _delegate(builder, tenant, "B")
    for call in builds.calls:
        attached = call["tool_env"].user_mcp_oauth_pool
        assert attached is None or attached is pools["A"]
    b_build = builds.calls[-1]["tool_env"].user_mcp_oauth_pool
    assert b_build is None  # B 自己构建、不带 A 的池(没拿到 A 那份缓存)
    assert len(builds.calls) == 2


async def test_user_connecting_before_the_runtime_bump_lands_never_reaches_other_users(
    builds: _Builds,
) -> None:
    """线上顺序:池子先换新(连上),运行时 ``invalidate_user`` 要等 ``await
    pool.close_all()`` 之后才到。这期间 A 的委派定键(共享键)、构建、入缓存都已完成
    —— 子代只挂定键时那份(空),所以共享键下没有 A 的池,B 委派不会拿到 A 的工具。"""
    rt = _runtime()
    tenant = uuid4()
    inner = await _store(tenant)
    pools: dict[str, MCPServerPool] = {"A": MCPServerPool(), "B": MCPServerPool()}
    swapped: list[int] = []

    class _Store:
        async def get(self, **kwargs: Any) -> AgentSpecRecord | None:
            if not swapped:
                swapped.append(1)
                pools["A"] = await _oauth_pool()  # 运行时失效还没到
            return await inner.get(**kwargs)

    async def _provider(tenant_id: UUID, user_id: str) -> MCPServerPool:
        return pools[user_id]

    builder = _child_builder(rt, _Store(), _provider)

    await _delegate(builder, tenant, "A")
    rt.invalidate_user(tenant, "A")  # 迟到的运行时失效
    await _delegate(builder, tenant, "B")
    assert all(call["tool_env"].user_mcp_oauth_pool is None for call in builds.calls)


async def test_user_pool_retired_mid_build_is_not_cached(builds: _Builds) -> None:
    """定键时的用户池在构建中途到期退役(客户端被清空,到期不 +1 运行时代次)——
    挂着空池的构建不入缓存;重建挂上新池。"""
    rt = _runtime()
    tenant = uuid4()
    pools = {"u1": await _oauth_pool()}
    first = pools["u1"]

    async def _expire(n: int) -> None:
        if n == 1:
            await first.retire_all()
            pools["u1"] = await _oauth_pool()

    async def _provider(tenant_id: UUID, user_id: str) -> MCPServerPool:
        return pools[user_id]

    builds.during = _expire
    builder = _child_builder(rt, await _store(tenant), _provider)

    assert await _delegate(builder, tenant, "u1") == "b2"
    assert builds.calls[1]["tool_env"].user_mcp_oauth_pool is pools["u1"]
    assert await _delegate(builder, tenant, "u1") == "b2"
