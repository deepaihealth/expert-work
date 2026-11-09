"""Unit tests for the platform MCP pool service (P1b — mcp-platform-servers)."""

from __future__ import annotations

import asyncio

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
from tests.test_tenant_mcp_pool import _Dialer, _ManualSleep


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


# B-292 —— 同租户池:连不上的目录项后台退避重试,锁外拨号,并进同一个缓存池。


class _PlatformRig:
    def __init__(self, store: InMemoryMcpConnectorCatalogStore, down: set[str]) -> None:
        self.dialer = _Dialer(down)
        self.now = {"t": 0.0}
        self.sleeper = _ManualSleep(self.now)
        self.expired: list[None] = []
        self.svc = PlatformMcpPoolService(
            store=store,
            client_factory=self.dialer,
            clock=lambda: self.now["t"],
            on_expire=lambda: self.expired.append(None),
            sleep=self.sleeper,
        )

    async def wake_retry(self) -> None:
        await self.sleeper.settle()
        (retry,) = self.svc._retry_tasks
        self.sleeper.wake()
        async with asyncio.timeout(1):
            await retry

    async def start_gated_retry(self, name: str) -> tuple[asyncio.Task[None], asyncio.Event]:
        await self.sleeper.sleeping(1)
        (retry,) = self.svc._retry_tasks
        self.dialer.down.clear()
        gate = await self.dialer.gate_next(name)
        self.sleeper.wake()
        async with asyncio.timeout(1):
            await self.dialer.dialing.wait()
        return retry, gate


async def _platform_rig(*names: str, down: set[str]) -> _PlatformRig:
    store = InMemoryMcpConnectorCatalogStore()
    for name in names:
        # "b" hides tool "t": the retried client must get the same denylist wrapper.
        await _add(store, name=name, disabled_tools=["t"] if name == "b" else None)
    return _PlatformRig(store, down)


@pytest.mark.asyncio
async def test_failed_server_merged_after_backoff_keeps_healthy_client_and_evicts_builds() -> None:
    rig = await _platform_rig("a", "b", down={"b"})
    p1 = await rig.svc.get_or_build()
    assert p1.names() == ["a"]
    a_client = p1.get("a")
    await rig.sleeper.sleeping(1)
    assert rig.sleeper.durations == [_FAILED_RETRY_S]
    assert await rig.svc.get_or_build() is p1  # inside the window: no redial
    assert sorted(rig.dialer.calls) == ["a", "b"]

    rig.dialer.down.clear()
    await rig.wake_retry()

    assert await rig.svc.get_or_build() is p1  # merged into the cached pool
    assert sorted(p1.names()) == ["a", "b"]
    assert sorted(rig.dialer.calls) == ["a", "b", "b"]  # only the missing one redialed
    assert p1.get("a") is a_client and not rig.dialer.last("a").closed
    retried = p1.get("b")
    assert retried is not None and list(await retried.list_tools()) == []  # denylist applied
    assert rig.expired == [None]  # every build evicted once (process-global pool)
    await rig.sleeper.settle()
    assert rig.sleeper.durations == [_FAILED_RETRY_S]
    await rig.svc.close_all()


@pytest.mark.asyncio
async def test_still_failing_server_backs_off_until_pool_ttl_without_evicting_builds() -> None:
    rig = await _platform_rig("a", "b", down={"b"})
    p1 = await rig.svc.get_or_build()
    await rig.sleeper.sleeping(1)
    for _ in range(20):  # bounded: a chain that never ends must fail, not hang
        if not rig.svc._retry_tasks:
            break
        await rig.wake_retry()
        await rig.sleeper.settle()
    assert rig.sleeper.durations == [60.0] + [120.0] * 14  # capped at 120 s, ends at TTL
    assert sorted(rig.dialer.calls) == ["a"] + ["b"] * 16
    assert await rig.svc.get_or_build() is p1
    assert rig.expired == []


@pytest.mark.asyncio
async def test_retry_dial_does_not_block_get_or_build() -> None:
    rig = await _platform_rig("a", "b", down={"b"})
    p1 = await rig.svc.get_or_build()
    retry, gate = await rig.start_gated_retry("b")
    async with asyncio.timeout(1):  # the retry is stuck dialing b
        assert await rig.svc.get_or_build() is p1
    gate.set()
    await retry
    assert sorted(p1.names()) == ["a", "b"]
    await rig.svc.close_all()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "event", ["invalidate", "server_changed", "ttl_expired", "ttl_passed_idle"]
)
async def test_retry_landing_after_pool_changed_closes_what_it_opened(event: str) -> None:
    rig = await _platform_rig("a", "b", down={"b"})
    p1 = await rig.svc.get_or_build()
    retry, gate = await rig.start_gated_retry("b")
    if event == "invalidate":
        await rig.svc.invalidate()
    elif event == "server_changed":
        await rig.svc.server_changed("b")
    elif event == "ttl_passed_idle":
        rig.now["t"] = 1801.0  # still cached, but expired: nobody asked for it yet
    else:
        rig.now["t"] = 1801.0
        async with asyncio.timeout(1):
            assert await rig.svc.get_or_build() is not p1
    evicted_before = list(rig.expired)
    gate.set()
    await retry
    (opened,) = rig.dialer.gated
    assert opened.closed  # stale result: closed, not merged, not leaked
    assert "b" not in p1.names()
    assert rig.expired == evicted_before
    await rig.svc.close_all()


@pytest.mark.asyncio
async def test_retry_waking_after_pool_ttl_does_not_dial() -> None:
    rig = await _platform_rig("a", "b", down={"b"})
    await rig.svc.get_or_build()
    rig.dialer.down.clear()
    rig.now["t"] = 1800.0
    await rig.sleeper.sleeping(1)
    await rig.wake_retry()
    assert sorted(rig.dialer.calls) == ["a", "b"]
    await rig.svc.close_all()


@pytest.mark.asyncio
async def test_close_all_mid_retry_closes_opened_clients_and_stops_scheduling() -> None:
    rig = await _platform_rig("a", "b", "c", down={"b", "c"})
    await rig.svc.get_or_build()
    retry, _gate = await rig.start_gated_retry("c")  # b dialed fine, c hangs
    await rig.svc.close_all()
    assert retry.cancelled()
    assert rig.dialer.last("b").closed

    rig.dialer.down.add("b")
    await rig.svc.get_or_build()
    await rig.sleeper.settle()
    assert not rig.svc._retry_tasks


@pytest.mark.asyncio
async def test_merged_server_is_carried_by_b259_like_any_member() -> None:
    rig = await _platform_rig("a", "b", down={"b"})
    p1 = await rig.svc.get_or_build()
    rig.dialer.down.clear()
    await rig.sleeper.sleeping(1)
    await rig.wake_retry()
    b_client = p1.get("b")
    await rig.svc.server_changed("a")  # B-259: only a is retired, b is carried
    p2 = await rig.svc.get_or_build()
    assert p2.get("b") is b_client  # reused, not redialed
    assert sorted(rig.dialer.calls) == ["a", "a", "b", "b"]
    await rig.svc.close_all()
