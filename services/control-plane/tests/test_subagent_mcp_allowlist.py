"""B-123 —— delegated builds (static sub-agent / dynamic worker) apply the
tenant's ``tenant_config.mcp_allowlist`` with the same semantics as the main
build (``control_plane.runtime.make_agent_builder``): gate only when a
platform pool (operator file pool or shared-catalog pool) is attached, and
only restrict when the resolved allowlist is non-empty."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from control_plane.subagent_runtime import make_child_agent_builder, make_worker_build_fn
from control_plane.tenancy import TenantConfigNotConfiguredError
from expert_work.persistence.agent_spec import InMemoryAgentSpecStore
from expert_work.protocol import AgentSpec
from expert_work.testing import InMemorySecretStore
from orchestrator import BuiltAgent, ToolEnv
from orchestrator.tools import MCPServerPool, RecordingMCPClient

_SHA = "a" * 64

_SANDBOX = {
    "resources": {"cpu": "1.0", "memory": "1Gi"},
    "network": {"egress": "proxy", "allowlist": []},
    "filesystem": {"readonly_root": True, "writable": ["/workspace"]},
}


def _parent(tools: list[dict[str, object]] | None = None) -> AgentSpec:
    """Parent spec for the worker build (synthesize_worker_spec input)."""
    return AgentSpec.model_validate(
        {
            "apiVersion": "expert_work.io/v1",
            "kind": "Agent",
            "metadata": {"name": "boss", "version": "1.0.0", "tenant": "t"},
            "spec": {
                "tenant_config": {},
                "model": {"provider": "deepseek", "name": "deepseek-v4-pro"},
                "system_prompt": {"template": "You are the parent."},
                "sandbox": _SANDBOX,
                "tools": tools or [{"type": "builtin", "name": "web_search", "config": {}}],
                "workflow": {"type": "react", "max_iterations": 12},
            },
        }
    )


def _spec(name: str, version: str = "1.0.0") -> AgentSpec:
    """Spec for the static sub-agent (spec_store lookup)."""
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


class _FakeTenantConfigService:
    """Duck-typed ``TenantConfigService`` — ``allowlist=None`` means the
    tenant has no ``tenant_config`` row yet (``TenantConfigNotConfiguredError``,
    the "not configured" case), matching ``test_mcp_allowlist_provider.py``'s
    ``_Svc``. Records every ``get`` call so a test can assert the provider was
    never consulted (no-pools guard, parity with the main build)."""

    def __init__(self, allowlist: list[str] | None) -> None:
        self._allowlist = allowlist
        self.calls: list[UUID] = []

    async def get(self, *, tenant_id: UUID, actor_id: str | None = None) -> Any:
        self.calls.append(tenant_id)
        if self._allowlist is None:
            raise TenantConfigNotConfiguredError(tenant_id=tenant_id)
        return SimpleNamespace(mcp_allowlist=self._allowlist)


@pytest.fixture
def build_calls(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Replace ``build_agent`` with a recorder — copied verbatim from
    ``test_subagent_runtime.py`` / ``test_worker_tool_policy.py``."""
    calls: list[dict[str, Any]] = []

    async def _fake_build_agent(spec: AgentSpec, **kwargs: Any) -> BuiltAgent:
        calls.append({"spec": spec, **kwargs})
        return BuiltAgent(graph=object(), system_prompt="", max_steps=1)  # type: ignore[arg-type]

    monkeypatch.setattr("control_plane.subagent_runtime.build_agent", _fake_build_agent)
    return calls


async def _platform_pool_with(*names: str) -> MCPServerPool:
    pool = MCPServerPool()
    for name in names:
        await pool.add(name, RecordingMCPClient())
    return pool


# ---------------------------------------------------------------------------
# 1 — worker build applies a non-empty allowlist.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_worker_build_applies_tenant_mcp_allowlist(
    build_calls: list[dict[str, Any]],
) -> None:
    platform_pool = await _platform_pool_with("shared-a")

    async def _platform_provider() -> MCPServerPool:
        return platform_pool

    tenant_config = _FakeTenantConfigService(["shared-a"])
    build_fn = make_worker_build_fn(
        secret_store=InMemorySecretStore(),
        checkpointer=InMemorySaver(),
        base_tool_env=ToolEnv(),
        max_iterations=8,
        allowed_toolsets=[],
        platform_mcp_pool_provider=_platform_provider,
        tenant_config_service=tenant_config,  # type: ignore[arg-type]
    )

    await build_fn(_parent(), tenant_id=uuid4(), role="probe", depth=1)

    assert build_calls[0]["tool_env"].mcp_allowlist == ("shared-a",)


# ---------------------------------------------------------------------------
# 2 — static child builder applies the same allowlist.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_child_builder_applies_tenant_mcp_allowlist(
    build_calls: list[dict[str, Any]],
) -> None:
    platform_pool = await _platform_pool_with("shared-a")

    async def _platform_provider() -> MCPServerPool:
        return platform_pool

    tenant = uuid4()
    store = InMemoryAgentSpecStore()
    await store.create(
        tenant_id=tenant, spec=_spec("researcher"), spec_sha256=_SHA, created_by="test"
    )
    tenant_config = _FakeTenantConfigService(["shared-a"])
    builder = make_child_agent_builder(
        spec_store=store,
        secret_store=InMemorySecretStore(),
        checkpointer=InMemorySaver(),
        base_tool_env=ToolEnv(),
        platform_mcp_pool_provider=_platform_provider,
        tenant_config_service=tenant_config,  # type: ignore[arg-type]
    )

    await builder(tenant_id=tenant, name="researcher", version="1.0.0", depth=1)

    assert build_calls[0]["tool_env"].mcp_allowlist == ("shared-a",)


# ---------------------------------------------------------------------------
# 3 — empty allowlist / tenant config not configured → base value (parity).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("allowlist", [[], None], ids=["configured-empty", "not-configured"])
async def test_worker_build_empty_or_unconfigured_allowlist_leaves_base_value(
    build_calls: list[dict[str, Any]], allowlist: list[str] | None
) -> None:
    platform_pool = await _platform_pool_with("shared-a")

    async def _platform_provider() -> MCPServerPool:
        return platform_pool

    tenant_config = _FakeTenantConfigService(allowlist)
    build_fn = make_worker_build_fn(
        secret_store=InMemorySecretStore(),
        checkpointer=InMemorySaver(),
        base_tool_env=ToolEnv(),
        max_iterations=8,
        allowed_toolsets=[],
        platform_mcp_pool_provider=_platform_provider,
        tenant_config_service=tenant_config,  # type: ignore[arg-type]
    )

    await build_fn(_parent(), tenant_id=uuid4(), role="probe", depth=1)

    assert build_calls[0]["tool_env"].mcp_allowlist == ()


# ---------------------------------------------------------------------------
# 4 — no pools attached → allowlist provider not consulted (main build's guard).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_worker_build_skips_allowlist_lookup_when_no_pools_attached(
    build_calls: list[dict[str, Any]],
) -> None:
    tenant_config = _FakeTenantConfigService(["shared-a"])
    build_fn = make_worker_build_fn(
        secret_store=InMemorySecretStore(),
        checkpointer=InMemorySaver(),
        base_tool_env=ToolEnv(),
        max_iterations=8,
        allowed_toolsets=[],
        tenant_config_service=tenant_config,  # type: ignore[arg-type]
    )

    await build_fn(_parent(), tenant_id=uuid4(), role="probe", depth=1)

    assert tenant_config.calls == []
    assert build_calls[0]["tool_env"].mcp_allowlist == ()


@pytest.mark.asyncio
async def test_child_builder_skips_allowlist_lookup_when_no_pools_attached(
    build_calls: list[dict[str, Any]],
) -> None:
    tenant = uuid4()
    store = InMemoryAgentSpecStore()
    await store.create(
        tenant_id=tenant, spec=_spec("researcher"), spec_sha256=_SHA, created_by="test"
    )
    tenant_config = _FakeTenantConfigService(["shared-a"])
    builder = make_child_agent_builder(
        spec_store=store,
        secret_store=InMemorySecretStore(),
        checkpointer=InMemorySaver(),
        base_tool_env=ToolEnv(),
        tenant_config_service=tenant_config,  # type: ignore[arg-type]
    )

    await builder(tenant_id=tenant, name="researcher", version="1.0.0", depth=1)

    assert tenant_config.calls == []
    assert build_calls[0]["tool_env"].mcp_allowlist == ()


# ---------------------------------------------------------------------------
# 5 — worker build keeps worker_policy True alongside the allowlist.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_worker_build_keeps_worker_policy_with_allowlist(
    build_calls: list[dict[str, Any]],
) -> None:
    platform_pool = await _platform_pool_with("shared-a")

    async def _platform_provider() -> MCPServerPool:
        return platform_pool

    tenant_config = _FakeTenantConfigService(["shared-a"])
    build_fn = make_worker_build_fn(
        secret_store=InMemorySecretStore(),
        checkpointer=InMemorySaver(),
        base_tool_env=ToolEnv(),
        max_iterations=8,
        allowed_toolsets=[],
        platform_mcp_pool_provider=_platform_provider,
        tenant_config_service=tenant_config,  # type: ignore[arg-type]
    )

    await build_fn(_parent(), tenant_id=uuid4(), role="probe", depth=1)

    tool_env = build_calls[0]["tool_env"]
    assert tool_env.mcp_allowlist == ("shared-a",)
    assert tool_env.worker_policy is True
