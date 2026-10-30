"""Pool TTL expiry must not break agents that still hold the old clients;
invalidation and shutdown must still cut them off.

Production (2026-10-30): the platform MCP pool's 1800 s TTL closed every client
while cached ``BuiltAgent``s (their own 1800 s TTL, never closed on expiry) and
in-flight runs still held ``MCPTool``s bound to those client objects. Every MCP
call then failed in-process with ``StreamableHttpMCPClient '...' not started``
until the agent's cache entry expired too. TTL expiry now *retires* the clients
(holders keep calling on one-shot sessions); ``invalidate()`` (server removed /
disabled / disconnected) and ``close_all()`` (shutdown) still *close* them —
including clients an earlier TTL expiry already retired — so a holder can no
longer reach a server the platform took away.

The fake client below is a real :class:`StreamableHttpMCPClient` with only the
transport (``_open_session``) faked, so the pool, the tool binding and the
client's started / closed / retired state are all production code.
"""

from __future__ import annotations

import gc
import types
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest

from control_plane.platform_mcp_pool import PlatformMcpPoolService
from control_plane.tenant_mcp_pool import TenantMcpPoolService
from control_plane.tenant_scope import bypass_rls_session
from control_plane.user_mcp_oauth_pool import UserMcpOAuthPoolService
from expert_work.persistence import (
    InMemoryMcpConnectorCatalogStore,
    InMemoryMcpOAuthConnectionStore,
    InMemoryTenantMcpServerStore,
)
from expert_work.protocol import (
    McpConnectorAuthSchema,
    McpConnectorCatalogUpsert,
    McpOAuthConnectionPatch,
)
from orchestrator import ToolContext
from orchestrator.tools import ToolRegistry, register_mcp_tools
from orchestrator.tools.mcp import MCPClient, MCPServerConfig, StreamableHttpMCPClient

_SERVER = "deep-ai-health-mcp"
_NOW = datetime(2026, 10, 30, 17, 35, tzinfo=UTC)


class _EchoSession:
    async def list_tools(self) -> Any:
        return types.SimpleNamespace(
            tools=[
                types.SimpleNamespace(
                    name="echo",
                    description="echo",
                    inputSchema={"type": "object"},
                    annotations=None,
                )
            ]
        )

    async def call_tool(self, name: str, args: dict[str, Any]) -> Any:
        return types.SimpleNamespace(
            content=[types.SimpleNamespace(type="text", text=f"{name}:{args.get('text')}")],
            isError=False,
        )


@dataclass
class _FakeTransportClient(StreamableHttpMCPClient):
    """Production client; only the transport handshake is faked and counted."""

    opens: int = field(default=0, init=False)
    closes: int = field(default=0, init=False)

    async def _open_session(self) -> tuple[Any, Any]:
        self.opens += 1
        owner = self

        class _Stack:
            async def __aexit__(self, *_exc: object) -> None:
                owner.closes += 1

        return _Stack(), _EchoSession()


def _started_client_factory(built: list[_FakeTransportClient] | None) -> Any:
    """``built`` records every client; ``None`` keeps no reference to them."""

    async def _factory(config: MCPServerConfig) -> MCPClient:
        client = _FakeTransportClient(config=config)
        await client.start()  # pools only ever add started clients
        if built is not None:
            built.append(client)
        return client

    return _factory


async def _bind_tool_like_a_built_agent(client: MCPClient | None) -> Any:
    """Register the server's tools the way the agent builder does and return
    the bound :class:`MCPTool` (it holds the client object, not the pool)."""
    assert client is not None
    registry = ToolRegistry()
    await register_mcp_tools(server_name=_SERVER, client=client, registry=registry)
    return registry.get_required(f"mcp__{_SERVER}__echo")


async def _call(tool: Any) -> str:
    result = await tool.call({"text": "hi"}, ctx=ToolContext())
    return str(result.content)


async def _assert_hard_stop(tool: Any, client: _FakeTransportClient) -> None:
    opens = client.opens
    with pytest.raises(RuntimeError, match="not started"):
        await _call(tool)
    assert client.opens == opens  # the removed server is never dialed again


async def _seed_platform(
    store: InMemoryMcpConnectorCatalogStore, *, disabled_tools: list[str] | None = None
) -> None:
    async with bypass_rls_session():
        await store.create(
            upsert=McpConnectorCatalogUpsert(
                name=_SERVER,
                display_name="Deep AI Health",
                transport="streamable_http",
                url_template="https://mcp.example.com/mcp",
                auth_type="none",
                auth_schema=McpConnectorAuthSchema(),
                disabled_tools=disabled_tools or [],
            ),
            actor_id="sysadmin",
        )


@pytest.mark.asyncio
async def test_platform_pool_ttl_expiry_keeps_held_tool_working() -> None:
    store = InMemoryMcpConnectorCatalogStore()
    await _seed_platform(store)
    built: list[_FakeTransportClient] = []
    now = {"t": 0.0}
    svc = PlatformMcpPoolService(
        store=store, client_factory=_started_client_factory(built), clock=lambda: now["t"]
    )
    tool = await _bind_tool_like_a_built_agent((await svc.get_or_build()).get(_SERVER))
    assert await _call(tool) == "echo:hi"

    now["t"] = 1801.0
    await svc.get_or_build()  # another agent build: expires + closes the old pool

    assert len(built) == 2
    assert await _call(tool) == "echo:hi"  # the stale holder still works
    old = built[0]
    assert old.opens == old.closes  # one-shot session opened and closed again


@pytest.mark.asyncio
async def test_platform_pool_ttl_expiry_retires_through_tool_filter_wrapper() -> None:
    """A row with ``disabled_tools`` wraps the client in ``_ToolFilteringClient``;
    TTL expiry must retire the wrapped client, not fall back to closing it."""
    store = InMemoryMcpConnectorCatalogStore()
    await _seed_platform(store, disabled_tools=["other"])
    built: list[_FakeTransportClient] = []
    now = {"t": 0.0}
    svc = PlatformMcpPoolService(
        store=store, client_factory=_started_client_factory(built), clock=lambda: now["t"]
    )
    tool = await _bind_tool_like_a_built_agent((await svc.get_or_build()).get(_SERVER))

    now["t"] = 1801.0
    await svc.get_or_build()

    assert await _call(tool) == "echo:hi"
    assert built[0].opens == built[0].closes


@pytest.mark.asyncio
async def test_platform_pool_invalidate_is_a_hard_stop() -> None:
    store = InMemoryMcpConnectorCatalogStore()
    await _seed_platform(store)
    built: list[_FakeTransportClient] = []
    svc = PlatformMcpPoolService(store=store, client_factory=_started_client_factory(built))
    tool = await _bind_tool_like_a_built_agent((await svc.get_or_build()).get(_SERVER))

    await svc.invalidate()  # catalog row deleted / disabled mid-run

    await _assert_hard_stop(tool, built[0])


@pytest.mark.asyncio
async def test_platform_pool_shutdown_is_a_hard_stop() -> None:
    store = InMemoryMcpConnectorCatalogStore()
    await _seed_platform(store)
    built: list[_FakeTransportClient] = []
    svc = PlatformMcpPoolService(store=store, client_factory=_started_client_factory(built))
    tool = await _bind_tool_like_a_built_agent((await svc.get_or_build()).get(_SERVER))

    await svc.close_all()

    await _assert_hard_stop(tool, built[0])


async def _seed_tenant(store: InMemoryTenantMcpServerStore, tid: UUID) -> None:
    await store.create(
        tenant_id=tid,
        name=_SERVER,
        transport="streamable_http",
        url="https://mcp.example.com/mcp",
        auth_type="none",
        token_secret_ref=None,
        timeout_s=30.0,
        created_by="a@x",
    )


@pytest.mark.asyncio
async def test_tenant_pool_ttl_expiry_keeps_held_tool_working() -> None:
    store = InMemoryTenantMcpServerStore()
    tid = uuid4()
    await _seed_tenant(store, tid)
    built: list[_FakeTransportClient] = []
    now = {"t": 0.0}
    svc = TenantMcpPoolService(
        store=store,
        secret_store=None,
        client_factory=_started_client_factory(built),
        clock=lambda: now["t"],
    )
    tool = await _bind_tool_like_a_built_agent((await svc.get_or_build(tid)).get(_SERVER))

    now["t"] = 1801.0
    await svc.get_or_build(tid)

    assert len(built) == 2
    assert await _call(tool) == "echo:hi"
    assert built[0].opens == built[0].closes


@pytest.mark.asyncio
async def test_tenant_pool_invalidate_is_a_hard_stop() -> None:
    store = InMemoryTenantMcpServerStore()
    tid = uuid4()
    await _seed_tenant(store, tid)
    built: list[_FakeTransportClient] = []
    svc = TenantMcpPoolService(
        store=store, secret_store=None, client_factory=_started_client_factory(built)
    )
    tool = await _bind_tool_like_a_built_agent((await svc.get_or_build(tid)).get(_SERVER))

    await svc.invalidate(tid)  # tenant deleted / disabled the server mid-run

    await _assert_hard_stop(tool, built[0])


async def _seed_user_oauth(
    cat_store: InMemoryMcpConnectorCatalogStore,
    oauth_store: InMemoryMcpOAuthConnectionStore,
    tid: UUID,
    uid: str,
) -> None:
    async with bypass_rls_session():
        cat = await cat_store.create(
            upsert=McpConnectorCatalogUpsert(
                name=_SERVER,
                display_name="Deep AI Health",
                transport="streamable_http",
                url_template="https://mcp.example.com/mcp",
                auth_type="oauth2",
                auth_schema=McpConnectorAuthSchema(),
                oauth_client_id="cid",
                oauth_scopes="read",
            ),
            actor_id="seed",
        )
    conn = await oauth_store.create(
        tenant_id=tid,
        user_id=uid,
        catalog_id=cat.id,
        name=_SERVER,
        resolved_url="https://mcp.example.com/mcp",
        oauth_state="st",
        pkce_verifier="pv",
    )
    await oauth_store.update(
        connection_id=conn.id,
        tenant_id=tid,
        user_id=uid,
        patch=McpOAuthConnectionPatch(
            status="connected",
            access_token_ref=f"secret://expert-work/tenant/{tid}/mcp-oauth/{conn.id}/access",
            token_expires_at=_NOW + timedelta(hours=1),
            clear_flow_state=True,
        ),
    )


@pytest.mark.asyncio
async def test_user_oauth_pool_ttl_expiry_keeps_held_tool_working() -> None:
    cat_store = InMemoryMcpConnectorCatalogStore()
    oauth_store = InMemoryMcpOAuthConnectionStore()
    tid, uid = uuid4(), "user-1"
    await _seed_user_oauth(cat_store, oauth_store, tid, uid)
    built: list[_FakeTransportClient] = []
    now = {"t": 0.0}
    svc = UserMcpOAuthPoolService(
        oauth_store=oauth_store,
        catalog_store=cat_store,
        client_factory=_started_client_factory(built),
        clock=lambda: _NOW,
        monotonic_clock=lambda: now["t"],
    )
    tool = await _bind_tool_like_a_built_agent((await svc.get_or_build(tid, uid)).get(_SERVER))

    now["t"] = 1801.0
    await svc.get_or_build(tid, uid)

    assert len(built) == 2
    assert await _call(tool) == "echo:hi"
    assert built[0].opens == built[0].closes


@pytest.mark.asyncio
async def test_user_oauth_pool_invalidate_is_a_hard_stop() -> None:
    cat_store = InMemoryMcpConnectorCatalogStore()
    oauth_store = InMemoryMcpOAuthConnectionStore()
    tid, uid = uuid4(), "user-1"
    await _seed_user_oauth(cat_store, oauth_store, tid, uid)
    built: list[_FakeTransportClient] = []
    svc = UserMcpOAuthPoolService(
        oauth_store=oauth_store,
        catalog_store=cat_store,
        client_factory=_started_client_factory(built),
        clock=lambda: _NOW,
    )
    tool = await _bind_tool_like_a_built_agent((await svc.get_or_build(tid, uid)).get(_SERVER))

    await svc.invalidate(tid, uid)  # user disconnected the OAuth connector mid-run

    await _assert_hard_stop(tool, built[0])


# ---------------------------------------------------------------------------
# Review N1 — invalidation / shutdown must also reach clients an earlier TTL
# expiry retired (the pool already dropped them, but runs still hold them).
# ---------------------------------------------------------------------------


@dataclass
class _Rig:
    """One pool service driven through the same build / expire / revoke steps."""

    svc: Any
    built: list[_FakeTransportClient]
    now: dict[str, float]
    build: Callable[[], Awaitable[Any]]
    invalidate: Callable[[], Awaitable[None]]
    #: Invalidate a *different* key of the same service (``None``: the platform
    #: pool has a single key).
    invalidate_other: Callable[[], Awaitable[None]] | None


async def _platform_rig(*, keep_refs: bool = True) -> _Rig:
    store = InMemoryMcpConnectorCatalogStore()
    await _seed_platform(store)
    built: list[_FakeTransportClient] = []
    now = {"t": 0.0}
    svc = PlatformMcpPoolService(
        store=store,
        client_factory=_started_client_factory(built if keep_refs else None),
        clock=lambda: now["t"],
    )
    return _Rig(svc, built, now, svc.get_or_build, svc.invalidate, None)


async def _tenant_rig(*, keep_refs: bool = True) -> _Rig:
    store = InMemoryTenantMcpServerStore()
    tid, other = uuid4(), uuid4()
    await _seed_tenant(store, tid)
    await _seed_tenant(store, other)
    built: list[_FakeTransportClient] = []
    now = {"t": 0.0}
    svc = TenantMcpPoolService(
        store=store,
        secret_store=None,
        client_factory=_started_client_factory(built if keep_refs else None),
        clock=lambda: now["t"],
    )

    async def _build_other_then_invalidate() -> None:
        await svc.get_or_build(other)
        await svc.invalidate(other)

    return _Rig(
        svc,
        built,
        now,
        lambda: svc.get_or_build(tid),
        lambda: svc.invalidate(tid),
        _build_other_then_invalidate,
    )


async def _user_rig(*, keep_refs: bool = True) -> _Rig:
    cat_store = InMemoryMcpConnectorCatalogStore()
    oauth_store = InMemoryMcpOAuthConnectionStore()
    tid, uid = uuid4(), "user-1"
    await _seed_user_oauth(cat_store, oauth_store, tid, uid)
    built: list[_FakeTransportClient] = []
    now = {"t": 0.0}
    svc = UserMcpOAuthPoolService(
        oauth_store=oauth_store,
        catalog_store=cat_store,
        client_factory=_started_client_factory(built if keep_refs else None),
        clock=lambda: _NOW,
        monotonic_clock=lambda: now["t"],
    )
    return _Rig(
        svc,
        built,
        now,
        lambda: svc.get_or_build(tid, uid),
        lambda: svc.invalidate(tid, uid),
        lambda: svc.invalidate(tid, "user-2"),
    )


_RIGS = {"platform": _platform_rig, "tenant": _tenant_rig, "user_oauth": _user_rig}


async def _retired_holder(rig: _Rig) -> Any:
    """A run binds the tool, then another build expires the pool (TTL retire)."""
    tool = await _bind_tool_like_a_built_agent((await rig.build()).get(_SERVER))
    rig.now["t"] = 1801.0
    await rig.build()
    assert await _call(tool) == "echo:hi"  # retired, still serving
    return tool


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", sorted(_RIGS))
async def test_invalidate_hard_stops_a_client_retired_by_ttl(kind: str) -> None:
    rig = await _RIGS[kind]()
    tool = await _retired_holder(rig)

    await rig.invalidate()  # server deleted / disabled / disconnected

    await _assert_hard_stop(tool, rig.built[0])


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", sorted(_RIGS))
async def test_shutdown_hard_stops_a_client_retired_by_ttl(kind: str) -> None:
    rig = await _RIGS[kind]()
    tool = await _retired_holder(rig)

    await rig.svc.close_all()

    await _assert_hard_stop(tool, rig.built[0])


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["tenant", "user_oauth"])
async def test_invalidating_another_key_leaves_a_retired_client_serving(kind: str) -> None:
    rig = await _RIGS[kind]()
    tool = await _retired_holder(rig)
    assert rig.invalidate_other is not None

    await rig.invalidate_other()

    assert await _call(tool) == "echo:hi"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", sorted(_RIGS))
async def test_retired_clients_are_held_weakly(kind: str) -> None:
    """The service only tracks retired clients while a run still holds them."""
    rig = await _RIGS[kind](keep_refs=False)
    tool = await _retired_holder(rig)
    assert len(rig.svc._retired) == 1

    del tool
    gc.collect()

    assert len(rig.svc._retired) == 0
