"""Unit tests for the platform MCP pool service (P1b — mcp-platform-servers)."""

from __future__ import annotations

import pytest

from control_plane.platform_mcp_pool import (
    _FAILED_RETRY_S,
    McpClientFactory,
    PlatformMcpPoolService,
)
from expert_work.persistence import InMemoryMcpConnectorCatalogStore
from expert_work.protocol import (
    McpConnectorAuthField,
    McpConnectorAuthSchema,
    McpConnectorCatalogUpsert,
)
from orchestrator.tools.mcp import (
    DEFAULT_TIMEOUT_S,
    MCPServerConfig,
    MCPToolDef,
    RecordingMCPClient,
)
from tests.test_tenant_mcp_pool import _flaky_factory, _ManualSleep


def _factory_spy(configs: list[MCPServerConfig]) -> McpClientFactory:
    async def _factory(config: MCPServerConfig):
        configs.append(config)
        return RecordingMCPClient(tools=(MCPToolDef(name="t", description="", input_schema={}),))

    return _factory


async def _add(
    store: InMemoryMcpConnectorCatalogStore,
    *,
    name: str,
    auth_type: str = "none",
    enabled: bool = True,
    bearer_token_ref: str | None = None,
    oauth_client_id: str | None = None,
    auth_schema: McpConnectorAuthSchema | None = None,
    timeout_s: float | None = None,
    sse_read_timeout_s: float | None = None,
    disabled_tools: list[str] | None = None,
) -> None:
    await store.create(
        upsert=McpConnectorCatalogUpsert(
            name=name,
            display_name=name.title(),
            transport="streamable_http",
            url_template=f"https://mcp.example.com/{name}",
            auth_type=auth_type,  # type: ignore[arg-type]
            enabled=enabled,
            bearer_token_ref=bearer_token_ref,
            oauth_client_id=oauth_client_id,
            auth_schema=auth_schema or McpConnectorAuthSchema(),
            timeout_s=timeout_s,
            sse_read_timeout_s=sse_read_timeout_s,
            disabled_tools=disabled_tools or [],
        ),
        actor_id="sysadmin",
    )


@pytest.mark.asyncio
async def test_disabled_tools_filtered_from_list_tools() -> None:
    store = InMemoryMcpConnectorCatalogStore()
    await _add(store, name="maps", disabled_tools=["drive"])

    async def _factory(_config: MCPServerConfig) -> RecordingMCPClient:
        return RecordingMCPClient(
            tools=(
                MCPToolDef(name="walk", description="", input_schema={}),
                MCPToolDef(name="drive", description="", input_schema={}),
            )
        )

    svc = PlatformMcpPoolService(store=store, client_factory=_factory)
    pool = await svc.get_or_build()
    client = pool.get("maps")
    assert client is not None
    listed = [t.name for t in await client.list_tools()]
    # "drive" is platform-disabled → never advertised to the agent layer.
    assert listed == ["walk"]


@pytest.mark.asyncio
async def test_timeouts_map_to_config_else_defaults() -> None:
    store = InMemoryMcpConnectorCatalogStore()
    await _add(store, name="tuned", timeout_s=12.0, sse_read_timeout_s=600.0)
    await _add(store, name="default")  # NULL timeouts → orchestrator defaults
    configs: list[MCPServerConfig] = []
    svc = PlatformMcpPoolService(store=store, client_factory=_factory_spy(configs))

    await svc.get_or_build()

    by_name = {c.name: c for c in configs}
    assert by_name["tuned"].timeout_s == 12.0
    assert by_name["tuned"].sse_read_timeout_s == 600.0
    # NULL row keeps the MCPServerConfig defaults (don't override timeout_s).
    assert by_name["default"].timeout_s == DEFAULT_TIMEOUT_S
    assert by_name["default"].sse_read_timeout_s is None


@pytest.mark.asyncio
async def test_builds_pool_from_none_and_platform_bearer() -> None:
    store = InMemoryMcpConnectorCatalogStore()
    await _add(store, name="weather", auth_type="none")
    await _add(
        store,
        name="search",
        auth_type="bearer",
        bearer_token_ref="secret://expert-work/platform/mcp/search/token",
    )
    configs: list[MCPServerConfig] = []
    svc = PlatformMcpPoolService(store=store, client_factory=_factory_spy(configs))

    pool = await svc.get_or_build()

    assert sorted(pool.names()) == ["search", "weather"]
    by_name = {c.name: c for c in configs}
    # bearer carries the platform token_ref; none carries nothing.
    assert by_name["search"].auth_config["token_ref"].endswith("/search/token")
    assert by_name["weather"].auth_config == {}
    assert by_name["weather"].url == "https://mcp.example.com/weather"


@pytest.mark.asyncio
async def test_excludes_disabled_oauth2_and_legacy_bearer() -> None:
    store = InMemoryMcpConnectorCatalogStore()
    await _add(store, name="ok", auth_type="none")
    await _add(store, name="off", auth_type="none", enabled=False)
    await _add(store, name="oauthsrv", auth_type="oauth2", oauth_client_id="cid")
    # Legacy tenant-fills bearer: a secret field, no platform token_ref.
    await _add(
        store,
        name="legacy",
        auth_type="bearer",
        auth_schema=McpConnectorAuthSchema(
            fields=[McpConnectorAuthField(key="tok", label="Token", kind="secret")]
        ),
    )
    svc = PlatformMcpPoolService(store=store, client_factory=_factory_spy([]))

    pool = await svc.get_or_build()

    assert pool.names() == ["ok"]


@pytest.mark.asyncio
async def test_caches_then_rebuilds_on_invalidate() -> None:
    store = InMemoryMcpConnectorCatalogStore()
    await _add(store, name="a", auth_type="none")
    configs: list[MCPServerConfig] = []
    svc = PlatformMcpPoolService(store=store, client_factory=_factory_spy(configs))

    first = await svc.get_or_build()
    second = await svc.get_or_build()
    assert first is second  # cached, no rebuild
    assert [c.name for c in configs] == ["a"]

    await _add(store, name="b", auth_type="none")
    await svc.invalidate()
    rebuilt = await svc.get_or_build()
    assert sorted(rebuilt.names()) == ["a", "b"]


@pytest.mark.asyncio
async def test_close_all_drops_cache() -> None:
    store = InMemoryMcpConnectorCatalogStore()
    await _add(store, name="a", auth_type="none")
    svc = PlatformMcpPoolService(store=store, client_factory=_factory_spy([]))
    await svc.get_or_build()
    await svc.close_all()
    # After close, a fresh build still works (rebuilds from the catalog).
    pool = await svc.get_or_build()
    assert pool.names() == ["a"]


@pytest.mark.asyncio
async def test_ttl_backstop_rebuilds_expired_pool_and_reuses_fresh_one() -> None:
    """PR-E3a: entries older than 1800s rebuild (old pool closed); younger reuse."""
    store = InMemoryMcpConnectorCatalogStore()
    await _add(store, name="maps")
    configs: list[MCPServerConfig] = []
    now = {"t": 0.0}
    svc = PlatformMcpPoolService(
        store=store, client_factory=_factory_spy(configs), clock=lambda: now["t"]
    )
    p1 = await svc.get_or_build()
    now["t"] = 1799.0
    assert await svc.get_or_build() is p1  # younger than TTL → reused
    assert len(configs) == 1
    now["t"] = 1801.0
    p2 = await svc.get_or_build()
    assert p2 is not p1  # older than TTL → rebuilt
    assert len(configs) == 2
    assert p1.names() == []  # the expired pool was closed (close_all clears clients)
    assert p2.names() == ["maps"]


# B-292 —— 同租户池:连不上的目录项隔一段时间在后台重试,连着的不重连。


async def _run_pending_retry(svc: PlatformMcpPoolService, sleeper: _ManualSleep) -> None:
    (retry,) = svc._retry_tasks
    sleeper.wake()
    await retry


@pytest.mark.asyncio
async def test_failed_server_retried_after_backoff_reuses_healthy_and_evicts_builds() -> None:
    store = InMemoryMcpConnectorCatalogStore()
    await _add(store, name="a")
    await _add(store, name="b")
    calls: list[str] = []
    down = {"b"}
    expired: list[None] = []
    sleeper = _ManualSleep()
    svc = PlatformMcpPoolService(
        store=store,
        client_factory=_flaky_factory(calls, down),
        on_expire=lambda: expired.append(None),
        sleep=sleeper,
    )
    p1 = await svc.get_or_build()
    assert p1.names() == ["a"]
    a_client = p1.get("a")
    await sleeper.sleeping(1)
    assert sleeper.durations == [_FAILED_RETRY_S]
    assert await svc.get_or_build() is p1  # inside the window: no redial
    assert sorted(calls) == ["a", "b"]

    down.clear()
    await _run_pending_retry(svc, sleeper)

    p2 = await svc.get_or_build()
    assert sorted(p2.names()) == ["a", "b"]
    assert sorted(calls) == ["a", "b", "b"]  # only the failed one redialed
    assert p2.get("a") is a_client
    assert isinstance(a_client, RecordingMCPClient) and not a_client.closed
    assert expired == [None]  # every build evicted once (platform pool is process-global)
    await sleeper.settle()
    assert sleeper.durations == [_FAILED_RETRY_S]
    await svc.close_all()


@pytest.mark.asyncio
async def test_still_failing_server_retried_once_per_window_without_evicting_builds() -> None:
    store = InMemoryMcpConnectorCatalogStore()
    await _add(store, name="a")
    await _add(store, name="b")
    calls: list[str] = []
    expired: list[None] = []
    sleeper = _ManualSleep()
    svc = PlatformMcpPoolService(
        store=store,
        client_factory=_flaky_factory(calls, {"b"}),
        on_expire=lambda: expired.append(None),
        sleep=sleeper,
    )
    p1 = await svc.get_or_build()
    a_client = p1.get("a")
    await sleeper.sleeping(1)
    await _run_pending_retry(svc, sleeper)

    p2 = await svc.get_or_build()
    assert p2.names() == ["a"]
    assert sorted(calls) == ["a", "b", "b"]
    assert p2.get("a") is a_client
    assert isinstance(a_client, RecordingMCPClient) and not a_client.closed
    assert expired == []
    await sleeper.sleeping(2)
    assert sleeper.durations == [_FAILED_RETRY_S, _FAILED_RETRY_S]
    assert await svc.get_or_build() is p2
    assert sorted(calls) == ["a", "b", "b"]

    (pending,) = svc._retry_tasks
    await svc.close_all()
    assert pending.cancelled()
