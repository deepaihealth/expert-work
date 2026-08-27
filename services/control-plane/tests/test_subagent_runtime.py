"""Tests for the J.4 ``ChildAgentBuilder`` wiring — ``make_child_agent_builder``."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from control_plane.runtime import make_agent_builder
from control_plane.subagent_runtime import (
    SubAgentNotFoundError,
    make_child_agent_builder,
    make_worker_build_fn,
)
from expert_work.common.credentials import CredentialsResolver
from expert_work.persistence.agent_spec import InMemoryAgentSpecStore
from expert_work.protocol import AgentSpec, AgentSpecStatus, TenantConfigRecord, TenantPlan
from expert_work.runtime.secret_store import LocalDevSecretStore
from expert_work.testing import InMemorySecretStore
from orchestrator import BuiltAgent, LLMActionJudge, LLMOutputJudge, ToolEnv

_SHA = "a" * 64


def _spec(name: str, version: str = "1.0.0") -> AgentSpec:
    return AgentSpec.model_validate(
        {
            "apiVersion": "expert_work.io/v1",
            "kind": "Agent",
            "metadata": {"name": name, "version": version, "tenant": "t"},
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


@pytest.fixture
def build_calls(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Replace ``build_agent`` with a recorder so the wiring is tested
    without real LLM provider clients."""
    calls: list[dict[str, Any]] = []

    async def _fake_build_agent(spec: AgentSpec, **kwargs: Any) -> BuiltAgent:
        calls.append({"spec": spec, **kwargs})
        return BuiltAgent(graph=object(), system_prompt="", max_steps=1)  # type: ignore[arg-type]

    monkeypatch.setattr("control_plane.subagent_runtime.build_agent", _fake_build_agent)
    return calls


@pytest.mark.asyncio
async def test_resolves_and_builds_subagent(build_calls: list[dict[str, Any]]) -> None:
    tenant = uuid4()
    store = InMemoryAgentSpecStore()
    await store.create(
        tenant_id=tenant, spec=_spec("researcher"), spec_sha256=_SHA, created_by="test"
    )
    builder = make_child_agent_builder(
        spec_store=store,
        secret_store=InMemorySecretStore(),
        checkpointer=InMemorySaver(),
        base_tool_env=ToolEnv(),
    )

    built = await builder(tenant_id=tenant, name="researcher", version="1.0.0", depth=1)

    assert isinstance(built, BuiltAgent)
    assert len(build_calls) == 1
    # The child builds at the depth the SubAgentTool requested.
    assert build_calls[0]["subagent_depth"] == 1


@pytest.mark.asyncio
async def test_depth_keyed_cache_hits(build_calls: list[dict[str, Any]]) -> None:
    tenant = uuid4()
    store = InMemoryAgentSpecStore()
    await store.create(
        tenant_id=tenant, spec=_spec("researcher"), spec_sha256=_SHA, created_by="test"
    )
    builder = make_child_agent_builder(
        spec_store=store,
        secret_store=InMemorySecretStore(),
        checkpointer=InMemorySaver(),
        base_tool_env=ToolEnv(),
    )

    first = await builder(tenant_id=tenant, name="researcher", version="1.0.0", depth=1)
    second = await builder(tenant_id=tenant, name="researcher", version="1.0.0", depth=1)

    assert second is first
    assert len(build_calls) == 1  # second call served from the cache


@pytest.mark.asyncio
async def test_same_manifest_different_depth_rebuilds(build_calls: list[dict[str, Any]]) -> None:
    # Depth is part of the cache key — the same manifest at depth 2 builds
    # a different graph (fewer / no SubAgentTools) than at depth 1.
    tenant = uuid4()
    store = InMemoryAgentSpecStore()
    await store.create(
        tenant_id=tenant, spec=_spec("researcher"), spec_sha256=_SHA, created_by="test"
    )
    builder = make_child_agent_builder(
        spec_store=store,
        secret_store=InMemorySecretStore(),
        checkpointer=InMemorySaver(),
        base_tool_env=ToolEnv(),
    )

    await builder(tenant_id=tenant, name="researcher", version="1.0.0", depth=1)
    await builder(tenant_id=tenant, name="researcher", version="1.0.0", depth=2)

    assert len(build_calls) == 2
    assert {c["subagent_depth"] for c in build_calls} == {1, 2}


@pytest.mark.asyncio
async def test_child_tool_env_carries_the_builder(build_calls: list[dict[str, Any]]) -> None:
    # A sub-agent's own ToolEnv carries the same builder, so a child can
    # delegate to a grandchild.
    tenant = uuid4()
    store = InMemoryAgentSpecStore()
    await store.create(
        tenant_id=tenant, spec=_spec("researcher"), spec_sha256=_SHA, created_by="test"
    )
    builder = make_child_agent_builder(
        spec_store=store,
        secret_store=InMemorySecretStore(),
        checkpointer=InMemorySaver(),
        base_tool_env=ToolEnv(),
    )

    await builder(tenant_id=tenant, name="researcher", version="1.0.0", depth=1)

    assert build_calls[0]["tool_env"].child_agent_builder is builder


@pytest.mark.asyncio
async def test_unknown_agent_ref_raises(build_calls: list[dict[str, Any]]) -> None:
    builder = make_child_agent_builder(
        spec_store=InMemoryAgentSpecStore(),
        secret_store=InMemorySecretStore(),
        checkpointer=InMemorySaver(),
        base_tool_env=ToolEnv(),
    )
    with pytest.raises(SubAgentNotFoundError):
        await builder(tenant_id=uuid4(), name="ghost", version="1.0.0", depth=1)
    assert build_calls == []


@pytest.mark.asyncio
async def test_register_invalidation_clears_subagent_cache(
    build_calls: list[dict[str, Any]],
) -> None:
    """Audit #1: a registered invalidator evicts cached sub-agents for a tenant,
    so a tenant MCP registry change rebuilds the delegated sub-agent (whose
    ToolEnv would otherwise hold a now-closed tenant MCP pool)."""
    tenant = uuid4()
    other = uuid4()
    store = InMemoryAgentSpecStore()
    for tid in (tenant, other):
        await store.create(
            tenant_id=tid, spec=_spec("researcher"), spec_sha256=_SHA, created_by="test"
        )

    invalidators: list[Any] = []
    builder = make_child_agent_builder(
        spec_store=store,
        secret_store=InMemorySecretStore(),
        checkpointer=InMemorySaver(),
        base_tool_env=ToolEnv(),
        register_invalidation=invalidators.append,
    )
    # The builder registered exactly one invalidator with the runtime.
    assert len(invalidators) == 1
    invalidate = invalidators[0]

    await builder(tenant_id=tenant, name="researcher", version="1.0.0", depth=1)
    await builder(tenant_id=other, name="researcher", version="1.0.0", depth=1)
    assert len(build_calls) == 2

    invalidate(tenant)  # evict only `tenant`'s cached sub-agents

    await builder(tenant_id=tenant, name="researcher", version="1.0.0", depth=1)
    await builder(tenant_id=other, name="researcher", version="1.0.0", depth=1)
    # `tenant` rebuilt (3rd build); `other` still cached (no 4th build).
    assert len(build_calls) == 3


@pytest.mark.asyncio
async def test_soft_deleted_agent_ref_raises(build_calls: list[dict[str, Any]]) -> None:
    tenant = uuid4()
    store = InMemoryAgentSpecStore()
    await store.create(
        tenant_id=tenant, spec=_spec("researcher"), spec_sha256=_SHA, created_by="test"
    )
    await store.update_status(
        tenant_id=tenant, name="researcher", version="1.0.0", status=AgentSpecStatus.DELETED
    )
    builder = make_child_agent_builder(
        spec_store=store,
        secret_store=InMemorySecretStore(),
        checkpointer=InMemorySaver(),
        base_tool_env=ToolEnv(),
    )
    with pytest.raises(SubAgentNotFoundError):
        await builder(tenant_id=tenant, name="researcher", version="1.0.0", depth=1)


# ---------------------------------------------------------------------------
# Stream V-D — tenant_mcp_pool_provider wiring in make_child_agent_builder
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_child_builder_sets_tenant_mcp_pool_from_provider(
    build_calls: list[dict[str, Any]],
) -> None:
    """When a tenant_mcp_pool_provider is given and returns a non-empty pool,
    the pool reaches build_agent via tool_env.tenant_mcp_pool."""
    from orchestrator.tools import MCPServerPool, RecordingMCPClient

    tenant_pool = MCPServerPool()
    client = RecordingMCPClient()
    await tenant_pool.add("github", client)

    async def _provider(tid: object) -> MCPServerPool:
        return tenant_pool

    tenant = uuid4()
    store = InMemoryAgentSpecStore()
    await store.create(
        tenant_id=tenant, spec=_spec("researcher"), spec_sha256=_SHA, created_by="test"
    )
    builder = make_child_agent_builder(
        spec_store=store,
        secret_store=InMemorySecretStore(),
        checkpointer=InMemorySaver(),
        base_tool_env=ToolEnv(),
        tenant_mcp_pool_provider=_provider,
    )

    await builder(tenant_id=tenant, name="researcher", version="1.0.0", depth=1)

    assert len(build_calls) == 1
    tool_env = build_calls[0]["tool_env"]
    assert tool_env.tenant_mcp_pool is tenant_pool


@pytest.mark.asyncio
async def test_child_builder_skips_empty_tenant_pool(
    build_calls: list[dict[str, Any]],
) -> None:
    """When the tenant pool is empty, tenant_mcp_pool stays None in the child ToolEnv."""
    from orchestrator.tools import MCPServerPool

    empty_pool = MCPServerPool()  # no servers

    async def _provider(tid: object) -> MCPServerPool:
        return empty_pool

    tenant = uuid4()
    store = InMemoryAgentSpecStore()
    await store.create(
        tenant_id=tenant, spec=_spec("researcher"), spec_sha256=_SHA, created_by="test"
    )
    builder = make_child_agent_builder(
        spec_store=store,
        secret_store=InMemorySecretStore(),
        checkpointer=InMemorySaver(),
        base_tool_env=ToolEnv(),
        tenant_mcp_pool_provider=_provider,
    )

    await builder(tenant_id=tenant, name="researcher", version="1.0.0", depth=1)

    assert len(build_calls) == 1
    tool_env = build_calls[0]["tool_env"]
    assert tool_env.tenant_mcp_pool is None


# ---------------------------------------------------------------------------
# Stream MCP platform-servers (P1b) — platform_mcp_pool_provider in children
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_child_builder_sets_platform_mcp_pool_from_provider(
    build_calls: list[dict[str, Any]],
) -> None:
    """A non-empty platform_mcp_pool_provider reaches build_agent via
    tool_env.platform_mcp_pool, so delegated children see shared catalog servers."""
    from orchestrator.tools import MCPServerPool, RecordingMCPClient

    platform_pool = MCPServerPool()
    await platform_pool.add("weather", RecordingMCPClient())

    async def _provider() -> MCPServerPool:
        return platform_pool

    tenant = uuid4()
    store = InMemoryAgentSpecStore()
    await store.create(
        tenant_id=tenant, spec=_spec("researcher"), spec_sha256=_SHA, created_by="test"
    )
    builder = make_child_agent_builder(
        spec_store=store,
        secret_store=InMemorySecretStore(),
        checkpointer=InMemorySaver(),
        base_tool_env=ToolEnv(),
        platform_mcp_pool_provider=_provider,
    )

    await builder(tenant_id=tenant, name="researcher", version="1.0.0", depth=1)

    assert len(build_calls) == 1
    assert build_calls[0]["tool_env"].platform_mcp_pool is platform_pool


@pytest.mark.asyncio
async def test_child_builder_skips_empty_platform_pool(
    build_calls: list[dict[str, Any]],
) -> None:
    """An empty platform pool leaves platform_mcp_pool None in the child ToolEnv."""
    from orchestrator.tools import MCPServerPool

    async def _provider() -> MCPServerPool:
        return MCPServerPool()  # no servers

    tenant = uuid4()
    store = InMemoryAgentSpecStore()
    await store.create(
        tenant_id=tenant, spec=_spec("researcher"), spec_sha256=_SHA, created_by="test"
    )
    builder = make_child_agent_builder(
        spec_store=store,
        secret_store=InMemorySecretStore(),
        checkpointer=InMemorySaver(),
        base_tool_env=ToolEnv(),
        platform_mcp_pool_provider=_provider,
    )

    await builder(tenant_id=tenant, name="researcher", version="1.0.0", depth=1)

    assert len(build_calls) == 1
    assert build_calls[0]["tool_env"].platform_mcp_pool is None


@pytest.mark.asyncio
async def test_register_invalidation_all_clears_subagent_cache(
    build_calls: list[dict[str, Any]],
) -> None:
    """The clear-all hook (fired on a platform-pool change) drops every cached
    sub-agent across tenants, mirroring the top-level cache."""
    clear_alls: list[Any] = []
    tenant = uuid4()
    store = InMemoryAgentSpecStore()
    await store.create(
        tenant_id=tenant, spec=_spec("researcher"), spec_sha256=_SHA, created_by="test"
    )
    builder = make_child_agent_builder(
        spec_store=store,
        secret_store=InMemorySecretStore(),
        checkpointer=InMemorySaver(),
        base_tool_env=ToolEnv(),
        register_invalidation_all=clear_alls.append,
    )
    await builder(tenant_id=tenant, name="researcher", version="1.0.0", depth=1)
    assert len(build_calls) == 1

    # Cached — no rebuild.
    await builder(tenant_id=tenant, name="researcher", version="1.0.0", depth=1)
    assert len(build_calls) == 1

    # Fire the registered clear-all → next build rebuilds.
    assert len(clear_alls) == 1
    clear_alls[0]()
    await builder(tenant_id=tenant, name="researcher", version="1.0.0", depth=1)
    assert len(build_calls) == 2


# ---------------------------------------------------------------------------
# MCP-OAUTH (OA-3b-后续) — user_mcp_oauth_pool_provider passthrough
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_child_builder_injects_user_oauth_pool(
    build_calls: list[dict[str, Any]],
) -> None:
    """A delegated child inherits the caller's per-user OAuth pool when an
    oauth_user_id + provider are supplied (OA-3b-后续)."""
    from orchestrator.tools import MCPServerPool, RecordingMCPClient

    user_pool = MCPServerPool()
    await user_pool.add("linear", RecordingMCPClient())

    seen: list[tuple[object, str]] = []

    async def _user_provider(tid: object, uid: str) -> MCPServerPool:
        seen.append((tid, uid))
        return user_pool

    tenant = uuid4()
    store = InMemoryAgentSpecStore()
    await store.create(
        tenant_id=tenant, spec=_spec("researcher"), spec_sha256=_SHA, created_by="test"
    )
    builder = make_child_agent_builder(
        spec_store=store,
        secret_store=InMemorySecretStore(),
        checkpointer=InMemorySaver(),
        base_tool_env=ToolEnv(),
        user_mcp_oauth_pool_provider=_user_provider,
    )

    await builder(
        tenant_id=tenant, name="researcher", version="1.0.0", depth=1, oauth_user_id="kc-user-a"
    )

    assert seen == [(tenant, "kc-user-a")]
    assert len(build_calls) == 1
    assert build_calls[0]["tool_env"].user_mcp_oauth_pool is user_pool


@pytest.mark.asyncio
async def test_child_builder_oauth_pool_not_shared_across_users(
    build_calls: list[dict[str, Any]],
) -> None:
    """The cache key includes the OAuth subject, so user B never gets user A's
    cached child build (no cross-user OAuth pool leak)."""
    from orchestrator.tools import MCPServerPool, RecordingMCPClient

    pools: dict[str, MCPServerPool] = {}

    async def _user_provider(tid: object, uid: str) -> MCPServerPool:
        if uid not in pools:
            p = MCPServerPool()
            # one server so the pool is non-empty (extends the cache key)
            await p.add(f"srv-{uid}", RecordingMCPClient())
            pools[uid] = p
        return pools[uid]

    tenant = uuid4()
    store = InMemoryAgentSpecStore()
    await store.create(
        tenant_id=tenant, spec=_spec("researcher"), spec_sha256=_SHA, created_by="test"
    )
    builder = make_child_agent_builder(
        spec_store=store,
        secret_store=InMemorySecretStore(),
        checkpointer=InMemorySaver(),
        base_tool_env=ToolEnv(),
        user_mcp_oauth_pool_provider=_user_provider,
    )

    await builder(tenant_id=tenant, name="researcher", version="1.0.0", depth=1, oauth_user_id="A")
    await builder(tenant_id=tenant, name="researcher", version="1.0.0", depth=1, oauth_user_id="B")

    # Two distinct builds (not one shared) with each user's own pool.
    assert len(build_calls) == 2
    assert build_calls[0]["tool_env"].user_mcp_oauth_pool is pools["A"]
    assert build_calls[1]["tool_env"].user_mcp_oauth_pool is pools["B"]


@pytest.mark.asyncio
async def test_child_builder_no_oauth_id_shares_cache(
    build_calls: list[dict[str, Any]],
) -> None:
    """Without an oauth_user_id the child build is shared (no per-user key) and
    carries no user OAuth pool — the common no-OAuth path is unchanged."""
    tenant = uuid4()
    store = InMemoryAgentSpecStore()
    await store.create(
        tenant_id=tenant, spec=_spec("researcher"), spec_sha256=_SHA, created_by="test"
    )
    builder = make_child_agent_builder(
        spec_store=store,
        secret_store=InMemorySecretStore(),
        checkpointer=InMemorySaver(),
        base_tool_env=ToolEnv(),
    )

    await builder(tenant_id=tenant, name="researcher", version="1.0.0", depth=1)
    await builder(tenant_id=tenant, name="researcher", version="1.0.0", depth=1)

    assert len(build_calls) == 1  # cached, shared
    assert build_calls[0]["tool_env"].user_mcp_oauth_pool is None


@pytest.mark.asyncio
async def test_child_builder_forwards_skill_store_to_build_agent(
    build_calls: list[dict[str, Any]],
) -> None:
    """skill_store 必须转发进 build_agent(2026-08-26 真栈事故)。

    此前只喂了 skill_resolver、漏了 skill_store 本体——而 build_agent 对
    「spec 声明技能创作 builtin(remember/author_skill 系)」有硬闸:无
    skill_store 直接 AgentFactoryError。现网 Agent 全带这类工具,等于静态
    子 Agent 委派全数在构建期炸掉。kwargs 里根本没有这个键,所以本断言在
    旧代码下必红(KeyError),不是恒真。
    """
    tenant = uuid4()
    store = InMemoryAgentSpecStore()
    await store.create(
        tenant_id=tenant, spec=_spec("researcher"), spec_sha256=_SHA, created_by="test"
    )
    sentinel = object()
    builder = make_child_agent_builder(
        spec_store=store,
        secret_store=InMemorySecretStore(),
        checkpointer=InMemorySaver(),
        base_tool_env=ToolEnv(),
        skill_store=sentinel,  # type: ignore[arg-type]
    )

    await builder(tenant_id=tenant, name="researcher", version="1.0.0", depth=1)

    assert build_calls[0]["skill_store"] is sentinel


@pytest.mark.asyncio
async def test_worker_build_fn_forwards_skill_store_to_build_agent(
    build_calls: list[dict[str, Any]],
) -> None:
    """spawn_worker 的 worker 构建同病同修:worker 继承父 spec,父带技能
    创作 builtin 时 worker 构建撞同一道闸(真栈 run 90163f46 四次委派全
    败于此,主 Agent 只能自己扛完子任务)。"""
    sentinel = object()
    build_fn = make_worker_build_fn(
        secret_store=InMemorySecretStore(),
        checkpointer=InMemorySaver(),
        base_tool_env=ToolEnv(),
        max_iterations=8,
        allowed_toolsets=[],
        skill_store=sentinel,  # type: ignore[arg-type]
    )

    await build_fn(_spec("parent"), tenant_id=uuid4(), role="probe", depth=1)

    assert build_calls[0]["skill_store"] is sentinel


@pytest.mark.asyncio
async def test_child_builder_strips_manage_task_from_the_spec(
    build_calls: list[dict[str, Any]],
) -> None:
    """BUG-19b —— 「子 Agent 不排任务」的正确表达是 spec 层剥工具。

    此前的表达是「不给 trigger_store」,而 build_agent 对「声明 manage_task
    无 TriggerStore」是硬闸——父/目标 Agent 带 manage_task 时整个委派构建
    直接炸。旧代码把 spec 原样传给 build_agent,本断言必红。
    """
    tenant = uuid4()
    store = InMemoryAgentSpecStore()
    doc = _spec("researcher").model_dump(by_alias=True, exclude_none=True)
    doc["spec"]["tools"] = [
        {"type": "builtin", "name": "web_search", "config": {}},
        {"type": "builtin", "name": "manage_task", "config": {}},
    ]
    spec = AgentSpec.model_validate(doc)
    await store.create(tenant_id=tenant, spec=spec, spec_sha256=_SHA, created_by="test")
    builder = make_child_agent_builder(
        spec_store=store,
        secret_store=InMemorySecretStore(),
        checkpointer=InMemorySaver(),
        base_tool_env=ToolEnv(),
    )

    await builder(tenant_id=tenant, name="researcher", version="1.0.0", depth=1)

    built_spec = build_calls[0]["spec"]
    names = {getattr(t, "name", None) for t in built_spec.spec.tools}
    assert "manage_task" not in names
    assert "web_search" in names


@pytest.mark.asyncio
async def test_child_builder_forwards_audit_logger(build_calls: list[dict[str, Any]]) -> None:
    """BUG-19b —— 子代的技能创作工具要挂审计(#1302 起可构建,不能裸奔)。"""
    tenant = uuid4()
    store = InMemoryAgentSpecStore()
    await store.create(
        tenant_id=tenant, spec=_spec("researcher"), spec_sha256=_SHA, created_by="test"
    )
    sentinel = object()
    builder = make_child_agent_builder(
        spec_store=store,
        secret_store=InMemorySecretStore(),
        checkpointer=InMemorySaver(),
        base_tool_env=ToolEnv(),
        audit_logger=sentinel,  # type: ignore[arg-type]
    )

    await builder(tenant_id=tenant, name="researcher", version="1.0.0", depth=1)

    assert build_calls[0]["audit_logger"] is sentinel


@pytest.mark.asyncio
async def test_worker_build_fn_forwards_audit_logger(build_calls: list[dict[str, Any]]) -> None:
    sentinel = object()
    build_fn = make_worker_build_fn(
        secret_store=InMemorySecretStore(),
        checkpointer=InMemorySaver(),
        base_tool_env=ToolEnv(),
        max_iterations=8,
        allowed_toolsets=[],
        audit_logger=sentinel,  # type: ignore[arg-type]
    )

    await build_fn(_spec("parent"), tenant_id=uuid4(), role="probe", depth=1)

    assert build_calls[0]["audit_logger"] is sentinel


# ---------------------------------------------------------------------------
# B-26 — 委派构建的防御参数与主路径同源决议(judges / tool budget / deadline /
# token_usage_kind)。此前五参全漏传:judges 恒 None、tool_budget 只剩 env 兜底、
# deadline 恒 0、计量恒 "conversation" —— 主 Agent 有的防御,子代默默没有。
# ---------------------------------------------------------------------------

_ANTHROPIC_KEY_NAME = "anthropic-test"


class _StubTenantConfig:
    """Minimal tenant-config getter for the credentials resolver."""

    async def get(self, *, tenant_id: UUID, actor_id: str | None = None) -> TenantConfigRecord:
        now = datetime.now(UTC)
        return TenantConfigRecord(
            tenant_id=tenant_id,
            display_name="t",
            plan=TenantPlan.FREE,
            created_at=now,
            updated_at=now,
            updated_by="test",
        )


def _anthropic_credentials_resolver() -> CredentialsResolver:
    return CredentialsResolver(
        platform_provider_credentials={"anthropic": f"secret://{_ANTHROPIC_KEY_NAME}"},  # type: ignore[arg-type]
        platform_tool_credentials={},  # type: ignore[arg-type]
        tenant_config_getter=_StubTenantConfig(),  # type: ignore[arg-type]
    )


def _judge_secret_store() -> LocalDevSecretStore:
    return LocalDevSecretStore.from_mapping({_ANTHROPIC_KEY_NAME: "sk-ant-test"})


def _spec_with_defenses(name: str, version: str = "1.0.0") -> AgentSpec:
    doc = _spec(name, version).model_dump(by_alias=True, exclude_none=True)
    doc["spec"]["model"] = {"provider": "anthropic", "name": "claude-haiku-4-5"}
    doc["spec"]["defenses"] = {"output_judge": "block", "action_screen": "block"}
    return AgentSpec.model_validate(doc)


class _FakeToolBudgetConfig:
    """Stub PlatformToolBudgetConfigService — fixed effective switch."""

    def __init__(self, enabled: bool) -> None:
        self._enabled = enabled

    async def effective_enabled(self) -> bool:
        return self._enabled


@pytest.mark.asyncio
async def test_child_builder_resolves_defense_params_like_main_path(
    build_calls: list[dict[str, Any]],
) -> None:
    """B-26 —— 子 Agent 构建必须带上与主路径同源决议的防御参数。

    此前 make_child_agent_builder 对 build_agent 的调用漏传 output_judge /
    action_judge / platform_tool_budget_enabled / default_run_deadline_s 四参:
    子代 manifest 声明了 judges 也落 None 被静默关掉、tool_budget 的 DB 配置
    失效、run deadline 落 0 无墙钟。旧代码 kwargs 里根本没有这些键,断言必红
    (KeyError),不是恒真。
    """
    tenant = uuid4()
    store = InMemoryAgentSpecStore()
    await store.create(
        tenant_id=tenant,
        spec=_spec_with_defenses("researcher"),
        spec_sha256=_SHA,
        created_by="test",
    )
    builder = make_child_agent_builder(
        spec_store=store,
        secret_store=_judge_secret_store(),
        checkpointer=InMemorySaver(),
        base_tool_env=ToolEnv(),
        credentials_resolver=_anthropic_credentials_resolver(),
        platform_tool_budget_config_service=_FakeToolBudgetConfig(True),  # type: ignore[arg-type]
        default_run_deadline_s=1800,
    )

    await builder(tenant_id=tenant, name="researcher", version="1.0.0", depth=1)

    kw = build_calls[0]
    assert isinstance(kw["output_judge"], LLMOutputJudge)
    assert isinstance(kw["action_judge"], LLMActionJudge)
    assert kw["platform_tool_budget_enabled"] is True
    assert kw["default_run_deadline_s"] == 1800


@pytest.mark.asyncio
async def test_child_builder_undeclared_defenses_stay_none(
    build_calls: list[dict[str, Any]],
) -> None:
    """决议与主路径同语义:子代 spec 没声明 judges 就照样 None(修的是
    「声明了却无效」,不是强制全开);tool budget 服务缺席时传 None(env 兜底)。"""
    tenant = uuid4()
    store = InMemoryAgentSpecStore()
    await store.create(
        tenant_id=tenant, spec=_spec("researcher"), spec_sha256=_SHA, created_by="test"
    )
    builder = make_child_agent_builder(
        spec_store=store,
        secret_store=_judge_secret_store(),
        checkpointer=InMemorySaver(),
        base_tool_env=ToolEnv(),
        credentials_resolver=_anthropic_credentials_resolver(),
    )

    await builder(tenant_id=tenant, name="researcher", version="1.0.0", depth=1)

    kw = build_calls[0]
    assert kw["output_judge"] is None
    assert kw["action_judge"] is None
    assert kw["platform_tool_budget_enabled"] is None
    assert kw["default_run_deadline_s"] == 0


@pytest.mark.asyncio
async def test_worker_build_fn_resolves_defense_params_like_main_path(
    build_calls: list[dict[str, Any]],
) -> None:
    """spawn_worker 的 worker 构建同病同修:worker 继承父 spec 的 defenses,
    五参此前同样全漏。"""
    build_fn = make_worker_build_fn(
        secret_store=_judge_secret_store(),
        checkpointer=InMemorySaver(),
        base_tool_env=ToolEnv(),
        max_iterations=8,
        allowed_toolsets=[],
        credentials_resolver=_anthropic_credentials_resolver(),
        platform_tool_budget_config_service=_FakeToolBudgetConfig(True),  # type: ignore[arg-type]
        default_run_deadline_s=1800,
    )

    await build_fn(_spec_with_defenses("parent"), tenant_id=uuid4(), role="probe", depth=1)

    kw = build_calls[0]
    assert isinstance(kw["output_judge"], LLMOutputJudge)
    assert isinstance(kw["action_judge"], LLMActionJudge)
    assert kw["platform_tool_budget_enabled"] is True
    assert kw["default_run_deadline_s"] == 1800


@pytest.mark.asyncio
async def test_child_builder_forwards_token_usage_kind(
    build_calls: list[dict[str, Any]],
) -> None:
    """B-26 —— 委派构建入口接受并转发 token_usage_kind(父 run 是
    skill_evolution,子代计量也该是,而不是恒落 "conversation")。"""
    tenant = uuid4()
    store = InMemoryAgentSpecStore()
    await store.create(
        tenant_id=tenant, spec=_spec("researcher"), spec_sha256=_SHA, created_by="test"
    )
    builder = make_child_agent_builder(
        spec_store=store,
        secret_store=InMemorySecretStore(),
        checkpointer=InMemorySaver(),
        base_tool_env=ToolEnv(),
    )

    await builder(
        tenant_id=tenant,
        name="researcher",
        version="1.0.0",
        depth=1,
        token_usage_kind="skill_evolution",
    )

    assert build_calls[0]["token_usage_kind"] == "skill_evolution"


@pytest.mark.asyncio
async def test_child_builder_kind_defaults_to_conversation(
    build_calls: list[dict[str, Any]],
) -> None:
    tenant = uuid4()
    store = InMemoryAgentSpecStore()
    await store.create(
        tenant_id=tenant, spec=_spec("researcher"), spec_sha256=_SHA, created_by="test"
    )
    builder = make_child_agent_builder(
        spec_store=store,
        secret_store=InMemorySecretStore(),
        checkpointer=InMemorySaver(),
        base_tool_env=ToolEnv(),
    )

    await builder(tenant_id=tenant, name="researcher", version="1.0.0", depth=1)

    assert build_calls[0]["token_usage_kind"] == "conversation"


@pytest.mark.asyncio
async def test_child_cache_not_shared_across_usage_kinds(
    build_calls: list[dict[str, Any]],
) -> None:
    """kind 进缓存键:skill_evolution 回放构建的子代若被 conversation 委派
    命中(或反之),整个 TTL 窗口内计量都会挂错 —— 必须各建各的。"""
    tenant = uuid4()
    store = InMemoryAgentSpecStore()
    await store.create(
        tenant_id=tenant, spec=_spec("researcher"), spec_sha256=_SHA, created_by="test"
    )
    builder = make_child_agent_builder(
        spec_store=store,
        secret_store=InMemorySecretStore(),
        checkpointer=InMemorySaver(),
        base_tool_env=ToolEnv(),
    )

    await builder(tenant_id=tenant, name="researcher", version="1.0.0", depth=1)
    await builder(
        tenant_id=tenant,
        name="researcher",
        version="1.0.0",
        depth=1,
        token_usage_kind="skill_evolution",
    )
    # Same-kind repeat still hits the cache.
    await builder(tenant_id=tenant, name="researcher", version="1.0.0", depth=1)

    assert len(build_calls) == 2
    assert [c["token_usage_kind"] for c in build_calls] == ["conversation", "skill_evolution"]


@pytest.mark.asyncio
async def test_worker_build_fn_forwards_token_usage_kind(
    build_calls: list[dict[str, Any]],
) -> None:
    build_fn = make_worker_build_fn(
        secret_store=InMemorySecretStore(),
        checkpointer=InMemorySaver(),
        base_tool_env=ToolEnv(),
        max_iterations=8,
        allowed_toolsets=[],
    )

    await build_fn(
        _spec("parent"),
        tenant_id=uuid4(),
        role="probe",
        depth=1,
        token_usage_kind="skill_evolution",
    )

    assert build_calls[0]["token_usage_kind"] == "skill_evolution"


@pytest.mark.asyncio
async def test_child_and_main_path_resolve_defenses_identically(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """对齐断言:同一 spec + 同一平台配置下,主路径与 child 路径交给
    build_agent 的五个防御参数决议结果一致(同一份决议逻辑,不是两份抄本)。"""
    calls: list[dict[str, Any]] = []

    async def _fake_build_agent(spec: AgentSpec, **kwargs: Any) -> BuiltAgent:
        calls.append({"spec": spec, **kwargs})
        return BuiltAgent(graph=object(), system_prompt="", max_steps=1)  # type: ignore[arg-type]

    monkeypatch.setattr("control_plane.runtime.build_agent", _fake_build_agent)
    monkeypatch.setattr("control_plane.subagent_runtime.build_agent", _fake_build_agent)

    tenant = uuid4()
    spec = _spec_with_defenses("researcher")
    store = InMemoryAgentSpecStore()
    await store.create(tenant_id=tenant, spec=spec, spec_sha256=_SHA, created_by="test")

    main_builder = make_agent_builder(
        _judge_secret_store(),
        InMemorySaver(),
        credentials_resolver=_anthropic_credentials_resolver(),
        platform_tool_budget_config_service=_FakeToolBudgetConfig(True),  # type: ignore[arg-type]
        default_run_deadline_s=1800,
    )
    child_builder = make_child_agent_builder(
        spec_store=store,
        secret_store=_judge_secret_store(),
        checkpointer=InMemorySaver(),
        base_tool_env=ToolEnv(),
        credentials_resolver=_anthropic_credentials_resolver(),
        platform_tool_budget_config_service=_FakeToolBudgetConfig(True),  # type: ignore[arg-type]
        default_run_deadline_s=1800,
    )

    await main_builder(spec, tenant_id=tenant)
    await child_builder(tenant_id=tenant, name="researcher", version="1.0.0", depth=1)

    main_kw, child_kw = calls
    assert type(main_kw["output_judge"]) is type(child_kw["output_judge"]) is LLMOutputJudge
    assert type(main_kw["action_judge"]) is type(child_kw["action_judge"]) is LLMActionJudge
    assert main_kw["platform_tool_budget_enabled"] == child_kw["platform_tool_budget_enabled"]
    assert main_kw["default_run_deadline_s"] == child_kw["default_run_deadline_s"] == 1800
    assert main_kw["token_usage_kind"] == child_kw["token_usage_kind"] == "conversation"
