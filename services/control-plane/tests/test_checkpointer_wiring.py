"""Tests for the E.1 checkpointer wiring — settings + lifespan swap.

The Postgres checkpointer itself is integration-tested by
``test_checkpointer_factory.py`` in expert-work-runtime. Here we cover the
control-plane wiring: settings defaults, the ``make_agent_builder``
seam, and the lifespan branch that swaps in the durable checkpointer.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from control_plane.app import create_app
from control_plane.runtime import (
    AgentRuntime,
    _load_mcp_server_configs,
    build_mcp_pool,
    build_middleware_env,
    build_sandbox_runtime,
    build_tool_env,
    make_agent_builder,
    make_agent_runtime,
    resolve_web_search_client,
)
from control_plane.settings import Settings
from control_plane.tenancy import TenantConfigNotConfiguredError
from expert_work.persistence import InMemoryKnowledgeStore
from expert_work.runtime.secret_store import LocalDevSecretStore
from expert_work.runtime.storage import InMemoryObjectStore
from orchestrator.llm import FakeEmbedder
from orchestrator.multimodal import ObjectStoreImageResolver
from orchestrator.tools import (
    HTTPSupervisorRuntime,
    KnowledgeRetriever,
    MCPClient,
    MCPServerConfig,
    RecordingMCPClient,
    SearXNGClient,
)
from tests.auth_fixtures import build_test_jwt_verifier


def _secret_store() -> LocalDevSecretStore:
    return LocalDevSecretStore.from_mapping({})


def test_checkpointer_settings_default_to_memory() -> None:
    settings = Settings()
    assert settings.checkpointer_backend == "memory"
    assert settings.checkpointer_dsn is None


def test_make_agent_builder_returns_distinct_callables() -> None:
    store = _secret_store()
    a = make_agent_builder(store, InMemorySaver())
    b = make_agent_builder(store, InMemorySaver())
    assert callable(a)
    assert callable(b)
    assert a is not b


def test_make_agent_runtime_builds_runtime() -> None:
    runtime = make_agent_runtime(_secret_store())
    assert isinstance(runtime, AgentRuntime)
    assert callable(runtime.agent_builder)


@pytest.mark.asyncio
async def test_lifespan_postgres_without_dsn_raises() -> None:
    """``checkpointer_backend='postgres'`` with no DSN fails fast at boot."""
    settings = Settings(checkpointer_backend="postgres", checkpointer_dsn=None)
    app = create_app(
        settings=settings,
        jwt_verifier=build_test_jwt_verifier(),
        enable_reaper=False,
    )
    with pytest.raises(RuntimeError, match="checkpointer_dsn"):
        async with app.router.lifespan_context(app):
            pass  # pragma: no cover - lifespan raises before yield


@pytest.mark.asyncio
async def test_lifespan_swaps_builder_to_inject_envs() -> None:
    """Lifespan rebuilds the agent builder with the tool / middleware
    env bundles — the create_app-body placeholder is replaced."""
    settings = Settings(checkpointer_backend="memory")
    app = create_app(
        settings=settings,
        jwt_verifier=build_test_jwt_verifier(),
        enable_reaper=False,
    )
    builder_before = app.state.agent_runtime.agent_builder
    async with app.router.lifespan_context(app):
        assert app.state.agent_runtime.agent_builder is not builder_before


# ---------------------------------------------------------------------------
# ToolEnv / MiddlewareEnv backends (PR1.5)
# ---------------------------------------------------------------------------


@dataclass
class _FakeTenantConfigService:
    """Minimal stand-in — ``build_tool_env``'s provider only calls ``get``."""

    allowlist: list[str] | None  # None → the tenant has no config row
    denylist: list[str] | None = None

    async def get(self, *, tenant_id: UUID, actor_id: str | None = None) -> object:
        if self.allowlist is None:
            raise TenantConfigNotConfiguredError(tenant_id=tenant_id)
        return SimpleNamespace(
            http_tool_allowlist=self.allowlist,
            http_tool_denylist=self.denylist or [],
        )


def test_build_middleware_env_wires_cache_langfuse_and_redactor() -> None:
    env = build_middleware_env()
    assert env.response_cache is not None
    assert env.langfuse_client is not None
    assert env.redact_text is not None


def test_build_middleware_env_redact_text_masks_secrets() -> None:
    env = build_middleware_env()
    assert env.redact_text is not None
    out = env.redact_text("token sk-ABCDEFGHIJKLMNOPQRSTUVWX here", None)
    assert "sk-ABCDEFGHIJKLMNOPQRSTUVWX" not in out


def test_build_tool_env_wires_allowlist_provider_only() -> None:
    env = build_tool_env(_FakeTenantConfigService(allowlist=[]))
    assert env.allowlist_provider is not None
    # The denylist provider is wired alongside the allowlist (E.8 denylist model).
    assert env.denylist_provider is not None
    # web_search (Tavily), the sandbox runtime, and the mcp pool are opt-in.
    assert env.web_search_client is None
    assert env.sandbox_runtime is None
    assert env.mcp_pool is None


@pytest.mark.asyncio
async def test_allowlist_provider_returns_tenant_patterns() -> None:
    env = build_tool_env(_FakeTenantConfigService(allowlist=["https://api.github.com/*"]))
    assert env.allowlist_provider is not None
    assert await env.allowlist_provider(uuid4()) == ["https://api.github.com/*"]


@pytest.mark.asyncio
async def test_allowlist_provider_empty_for_headerless_tenant() -> None:
    env = build_tool_env(_FakeTenantConfigService(allowlist=["x"]))
    assert env.allowlist_provider is not None
    assert await env.allowlist_provider(None) == []


@pytest.mark.asyncio
async def test_allowlist_provider_empty_when_tenant_unconfigured() -> None:
    env = build_tool_env(_FakeTenantConfigService(allowlist=None))
    assert env.allowlist_provider is not None
    assert await env.allowlist_provider(uuid4()) == []


@pytest.mark.asyncio
async def test_denylist_provider_returns_tenant_hosts() -> None:
    env = build_tool_env(_FakeTenantConfigService(allowlist=[], denylist=["internal.example.com"]))
    assert env.denylist_provider is not None
    assert await env.denylist_provider(uuid4()) == ["internal.example.com"]


@pytest.mark.asyncio
async def test_denylist_provider_empty_for_headerless_tenant() -> None:
    env = build_tool_env(_FakeTenantConfigService(allowlist=["x"], denylist=["y"]))
    assert env.denylist_provider is not None
    assert await env.denylist_provider(None) == []


@pytest.mark.asyncio
async def test_denylist_provider_empty_when_tenant_unconfigured() -> None:
    env = build_tool_env(_FakeTenantConfigService(allowlist=None))
    assert env.denylist_provider is not None
    assert await env.denylist_provider(uuid4()) == []


# ---------------------------------------------------------------------------
# web_search / Tavily wiring (#164)
# ---------------------------------------------------------------------------


def test_resolve_web_search_client_none_without_searxng() -> None:
    # No SearXNG configured → no builtin backend (Tavily moved to MCP in M2).
    assert resolve_web_search_client(searxng_base_url=None) is None


def test_resolve_web_search_client_builds_searxng() -> None:
    client = resolve_web_search_client(searxng_base_url="http://searxng:8080")
    assert isinstance(client, SearXNGClient)
    assert client.base_url == "http://searxng:8080"


def test_build_tool_env_carries_web_search_client() -> None:
    client = resolve_web_search_client(searxng_base_url="http://searxng:8080")
    env = build_tool_env(_FakeTenantConfigService(allowlist=[]), web_search_client=client)
    assert env.web_search_client is client


# ---------------------------------------------------------------------------
# Sandbox runtime wiring (Stream F.11)
# ---------------------------------------------------------------------------


def test_build_sandbox_runtime_none_url_yields_none() -> None:
    assert build_sandbox_runtime(Settings(sandbox_supervisor_url=None)) is None


def test_build_sandbox_runtime_builds_http_client() -> None:
    client = build_sandbox_runtime(
        Settings(sandbox_supervisor_url="http://sandbox-supervisor:8000")
    )
    assert isinstance(client, HTTPSupervisorRuntime)
    assert client.base_url == "http://sandbox-supervisor:8000"


def test_build_tool_env_carries_sandbox_runtime() -> None:
    client = build_sandbox_runtime(
        Settings(sandbox_supervisor_url="http://sandbox-supervisor:8000")
    )
    env = build_tool_env(_FakeTenantConfigService(allowlist=[]), sandbox_runtime=client)
    assert env.sandbox_runtime is client


def test_build_tool_env_carries_knowledge_retriever() -> None:
    retriever = KnowledgeRetriever(store=InMemoryKnowledgeStore(), embedder=FakeEmbedder())
    env = build_tool_env(_FakeTenantConfigService(allowlist=[]), knowledge_retriever=retriever)
    assert env.knowledge_retriever is retriever


def test_build_tool_env_carries_image_resolver() -> None:
    resolver = ObjectStoreImageResolver(store=InMemoryObjectStore())
    env = build_tool_env(_FakeTenantConfigService(allowlist=[]), image_resolver=resolver)
    assert env.image_resolver is resolver


# ---------------------------------------------------------------------------
# MCP server pool wiring (#165 / Mini-ADR E-17)
# ---------------------------------------------------------------------------


def test_load_mcp_server_configs_parses_entries(tmp_path: Path) -> None:
    cfg_file = tmp_path / "mcp.json"
    cfg_file.write_text(
        json.dumps(
            [
                {"name": "gitlab", "command": ["node", "gitlab.js"], "env": {"TOKEN": "x"}},
                {"name": "fs", "command": ["mcp-fs"]},
            ]
        )
    )
    configs = _load_mcp_server_configs(str(cfg_file))
    assert [c.name for c in configs] == ["gitlab", "fs"]
    assert configs[0].command == ["node", "gitlab.js"]
    assert configs[0].env == {"TOKEN": "x"}
    assert configs[1].env == {}


def test_load_mcp_server_configs_rejects_non_array(tmp_path: Path) -> None:
    cfg_file = tmp_path / "mcp.json"
    cfg_file.write_text(json.dumps({"name": "not-a-list"}))
    with pytest.raises(ValueError, match="JSON array"):
        _load_mcp_server_configs(str(cfg_file))


@pytest.mark.asyncio
async def test_build_mcp_pool_none_yields_empty_pool() -> None:
    async with build_mcp_pool(None) as pool:
        assert pool.names() == []


@pytest.mark.asyncio
async def test_build_mcp_pool_loads_servers_and_tears_down(tmp_path: Path) -> None:
    cfg_file = tmp_path / "mcp.json"
    cfg_file.write_text(json.dumps([{"name": "gitlab", "command": ["x"]}]))
    created: list[RecordingMCPClient] = []

    async def _factory(_config: MCPServerConfig) -> MCPClient:
        client = RecordingMCPClient()
        created.append(client)
        return client

    async with build_mcp_pool(str(cfg_file), client_factory=_factory) as pool:
        assert pool.names() == ["gitlab"]
    # Exiting the context closed every server.
    assert created[0].closed is True


@pytest.mark.asyncio
async def test_build_tool_env_carries_mcp_pool() -> None:
    async with build_mcp_pool(None) as pool:
        env = build_tool_env(_FakeTenantConfigService(allowlist=[]), mcp_pool=pool)
        assert env.mcp_pool is pool


# ---------------------------------------------------------------------------
# Stream V-D — tenant_mcp_pool_provider wiring in make_agent_builder
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_agent_builder_sets_tenant_mcp_pool_from_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When a tenant_mcp_pool_provider is supplied and returns a non-empty
    pool, the pool reaches build_agent via tool_env.tenant_mcp_pool."""
    from copy import deepcopy
    from uuid import uuid4

    from orchestrator import BuiltAgent, ToolEnv
    from orchestrator.tools import MCPServerPool, RecordingMCPClient

    tenant_pool = MCPServerPool()
    # Add a server so names() is non-empty — the builder skips empty pools.
    client = RecordingMCPClient()
    await tenant_pool.add("github", client)

    captured: dict[str, object] = {}

    async def _fake_build_agent(spec: object, **kwargs: object) -> BuiltAgent:
        captured["tool_env"] = kwargs.get("tool_env")
        return BuiltAgent(graph=object(), system_prompt="", max_steps=1)  # type: ignore[arg-type]

    monkeypatch.setattr("control_plane.runtime.build_agent", _fake_build_agent)

    async def _provider(tid: object) -> MCPServerPool:
        return tenant_pool

    from expert_work.protocol import AgentSpec

    spec = AgentSpec.model_validate(
        deepcopy(
            {
                "apiVersion": "expert_work.io/v1",
                "kind": "Agent",
                "metadata": {"name": "test-agent", "version": "1.0.0", "tenant": "t"},
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
    )

    builder = make_agent_builder(
        _secret_store(),
        InMemorySaver(),
        tool_env=ToolEnv(),
        tenant_mcp_pool_provider=_provider,
    )
    await builder(spec, tenant_id=uuid4())

    tool_env = captured.get("tool_env")
    assert tool_env is not None
    assert tool_env.tenant_mcp_pool is tenant_pool  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_agent_builder_skips_empty_tenant_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the tenant pool is empty (no servers), tenant_mcp_pool stays None."""
    from copy import deepcopy
    from uuid import uuid4

    from orchestrator import BuiltAgent, ToolEnv
    from orchestrator.tools import MCPServerPool

    captured: dict[str, object] = {}

    async def _fake_build_agent(spec: object, **kwargs: object) -> BuiltAgent:
        captured["tool_env"] = kwargs.get("tool_env")
        return BuiltAgent(graph=object(), system_prompt="", max_steps=1)  # type: ignore[arg-type]

    monkeypatch.setattr("control_plane.runtime.build_agent", _fake_build_agent)

    empty_pool = MCPServerPool()  # no servers added

    async def _provider(tid: object) -> MCPServerPool:
        return empty_pool

    from expert_work.protocol import AgentSpec

    spec = AgentSpec.model_validate(
        deepcopy(
            {
                "apiVersion": "expert_work.io/v1",
                "kind": "Agent",
                "metadata": {"name": "test-agent", "version": "1.0.0", "tenant": "t"},
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
    )

    builder = make_agent_builder(
        _secret_store(),
        InMemorySaver(),
        tool_env=ToolEnv(),
        tenant_mcp_pool_provider=_provider,
    )
    await builder(spec, tenant_id=uuid4())

    tool_env = captured.get("tool_env")
    assert tool_env is not None
    assert tool_env.tenant_mcp_pool is None  # type: ignore[union-attr]
