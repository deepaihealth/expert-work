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

import asyncio
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
    McpConnectorCatalogPatch,
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


async def _bind_tool_like_a_built_agent(client: MCPClient | None, server: str = _SERVER) -> Any:
    """Register the server's tools the way the agent builder does and return
    the bound :class:`MCPTool` (it holds the client object, not the pool)."""
    assert client is not None
    registry = ToolRegistry()
    await register_mcp_tools(server_name=server, client=client, registry=registry)
    return registry.get_required(f"mcp__{server}__echo")


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


async def _seed_oauth_catalog(
    cat_store: InMemoryMcpConnectorCatalogStore, name: str = _SERVER
) -> UUID:
    async with bypass_rls_session():
        cat = await cat_store.create(
            upsert=McpConnectorCatalogUpsert(
                name=name,
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
    return cat.id


async def _seed_user_oauth(
    cat_store: InMemoryMcpConnectorCatalogStore,
    oauth_store: InMemoryMcpOAuthConnectionStore,
    tid: UUID,
    uid: str,
) -> None:
    await _connect_user(oauth_store, tid, uid, await _seed_oauth_catalog(cat_store), _SERVER)


async def _connect_user(
    oauth_store: InMemoryMcpOAuthConnectionStore,
    tid: UUID,
    uid: str,
    catalog_id: UUID,
    name: str,
) -> None:
    conn = await oauth_store.create(
        tenant_id=tid,
        user_id=uid,
        catalog_id=catalog_id,
        name=name,
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
    #: B-248 — the scope ``on_expire`` should report, and what it reported.
    scope: tuple[object, ...] = ()
    expired: list[tuple[object, ...]] = field(default_factory=list)


async def _platform_rig(*, keep_refs: bool = True) -> _Rig:
    store = InMemoryMcpConnectorCatalogStore()
    await _seed_platform(store)
    built: list[_FakeTransportClient] = []
    now = {"t": 0.0}
    expired: list[tuple[object, ...]] = []
    svc = PlatformMcpPoolService(
        store=store,
        client_factory=_started_client_factory(built if keep_refs else None),
        clock=lambda: now["t"],
        on_expire=lambda: expired.append(()),
    )
    return _Rig(svc, built, now, svc.get_or_build, svc.invalidate, None, (), expired)


async def _tenant_rig(*, keep_refs: bool = True) -> _Rig:
    store = InMemoryTenantMcpServerStore()
    tid, other = uuid4(), uuid4()
    await _seed_tenant(store, tid)
    await _seed_tenant(store, other)
    built: list[_FakeTransportClient] = []
    now = {"t": 0.0}
    expired: list[tuple[object, ...]] = []
    svc = TenantMcpPoolService(
        store=store,
        secret_store=None,
        client_factory=_started_client_factory(built if keep_refs else None),
        clock=lambda: now["t"],
        on_expire=lambda tenant_id: expired.append((tenant_id,)),
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
        (tid,),
        expired,
    )


async def _user_rig(*, keep_refs: bool = True) -> _Rig:
    cat_store = InMemoryMcpConnectorCatalogStore()
    oauth_store = InMemoryMcpOAuthConnectionStore()
    tid, uid = uuid4(), "user-1"
    await _seed_user_oauth(cat_store, oauth_store, tid, uid)
    built: list[_FakeTransportClient] = []
    now = {"t": 0.0}
    expired: list[tuple[object, ...]] = []
    svc = UserMcpOAuthPoolService(
        oauth_store=oauth_store,
        catalog_store=cat_store,
        client_factory=_started_client_factory(built if keep_refs else None),
        clock=lambda: _NOW,
        monotonic_clock=lambda: now["t"],
        on_expire=lambda tenant_id, user_id: expired.append((tenant_id, user_id)),
    )
    return _Rig(
        svc,
        built,
        now,
        lambda: svc.get_or_build(tid, uid),
        lambda: svc.invalidate(tid, uid),
        lambda: svc.invalidate(tid, "user-2"),
        (tid, uid),
        expired,
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


# ---------------------------------------------------------------------------
# B-248 — TTL expiry also evicts the agent builds holding the retired clients
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", sorted(_RIGS))
async def test_ttl_expiry_reports_its_scope_for_build_eviction(kind: str) -> None:
    """Cached agents used to keep the retired clients for up to their own
    1800 s TTL, paying a full connect + initialize + teardown on every call."""
    rig = await _RIGS[kind]()
    await rig.build()
    rig.now["t"] = 1799.0
    await rig.build()  # fresh: reused
    await rig.invalidate()  # invalidation evicts builds itself (bus handlers)
    await rig.build()
    assert rig.expired == []

    rig.now["t"] = 1799.0 + 1801.0
    await rig.build()

    assert rig.expired == [rig.scope]


# ---------------------------------------------------------------------------
# B-248 — the pool served uncached on the last attempt
# ---------------------------------------------------------------------------


def _invalidate_on_every_read(rig: _Rig, switch: dict[str, bool] | None = None) -> None:
    """Each registry read lands an invalidation mid-build, so ``get_or_build``
    runs out of retries and serves its last pool uncached (while
    ``switch["on"]``, when given)."""
    svc = rig.svc
    store: object
    if isinstance(svc, PlatformMcpPoolService):
        store, attr = svc._store, "list"
    elif isinstance(svc, TenantMcpPoolService):
        store, attr = svc._store, "list_for_tenant"
    else:
        store, attr = svc._oauth_store, "list_for_user"
    read = getattr(store, attr)

    async def _read(*args: Any, **kwargs: Any) -> Any:
        if switch is None or switch["on"]:
            await rig.invalidate()
        return await read(*args, **kwargs)

    setattr(store, attr, _read)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", sorted(_RIGS))
@pytest.mark.parametrize("revoke", ["invalidate", "shutdown"])
async def test_pool_served_uncached_is_retired_and_still_revocable(kind: str, revoke: str) -> None:
    """No cache entry owns that pool: its long-lived sessions were never
    closed, and neither invalidation nor shutdown could reach its clients."""
    rig = await _RIGS[kind]()
    _invalidate_on_every_read(rig)
    pool = await rig.build()
    served = rig.built[-1]
    assert len(rig.built) == 5  # every attempt built a pool; the last is served
    assert [c.opens == c.closes for c in rig.built] == [True] * 5  # no session left open

    tool = await _bind_tool_like_a_built_agent(pool.get(_SERVER))
    assert await _call(tool) == "echo:hi"  # still serves, on one-shot sessions
    assert served.opens == served.closes

    await (rig.invalidate() if revoke == "invalidate" else rig.svc.close_all())

    await _assert_hard_stop(tool, served)


@pytest.mark.asyncio
async def test_app_wires_pool_expiry_to_build_eviction() -> None:
    """The app hands each pool the runtime's eviction for the same scope."""
    from control_plane.app import create_app
    from control_plane.settings import Settings
    from tests.auth_fixtures import build_test_jwt_verifier

    app = create_app(
        settings=Settings(checkpointer_backend="memory"),
        jwt_verifier=build_test_jwt_verifier(),
        enable_reaper=False,
        enable_scheduler=False,
    )
    async with app.router.lifespan_context(app):
        runtime = app.state.agent_runtime
        assert app.state.platform_mcp_pool_service._on_expire == runtime.invalidate_all
        assert app.state.tenant_mcp_pool_service._on_expire == runtime.invalidate_tenant
        assert app.state.user_mcp_oauth_pool_service._on_expire == runtime.invalidate_user


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", sorted(_RIGS))
async def test_uncached_pool_retire_runs_outside_the_build_lock(
    kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review L2: retiring the uncached pool's clients (network teardown) must
    not hold the build lock that every other build of the same key waits on."""
    rig = await _RIGS[kind]()
    invalidating = {"on": True}
    _invalidate_on_every_read(rig, invalidating)
    gate, entered = asyncio.Event(), asyncio.Event()
    retire = _FakeTransportClient.retire

    async def _slow_retire(self: _FakeTransportClient) -> None:
        entered.set()
        await gate.wait()
        await retire(self)

    monkeypatch.setattr(_FakeTransportClient, "retire", _slow_retire)
    first: asyncio.Future[Any] = asyncio.ensure_future(rig.build())
    await entered.wait()
    invalidating["on"] = False

    second = await asyncio.wait_for(rig.build(), timeout=2)

    assert second.names() == [_SERVER]
    gate.set()
    assert (await first).names() == [_SERVER]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", sorted(_RIGS))
async def test_failing_on_expire_never_fails_the_build(kind: str) -> None:
    """Review L1: build eviction is best effort, like ``retire_all``."""
    rig = await _RIGS[kind]()
    await rig.build()

    def _boom(*_scope: object) -> None:
        raise RuntimeError("runtime gone")

    rig.svc._on_expire = _boom
    rig.now["t"] = 1801.0

    assert (await rig.build()).names() == [_SERVER]


# ---------------------------------------------------------------------------
# B-248 review H1 — one oauth2 connector switched off / on for every user
# ---------------------------------------------------------------------------

_OTHER = "other-mcp"


@dataclass
class _TwoConnectors:
    """user-1 connected to ``_SERVER`` and ``_OTHER``; user-2 only to ``_OTHER``."""

    svc: UserMcpOAuthPoolService
    cat_store: InMemoryMcpConnectorCatalogStore
    server_id: UUID
    tid: UUID
    now: dict[str, float]
    expired: list[tuple[UUID, str]]

    async def pool(self, uid: str) -> Any:
        return await self.svc.get_or_build(self.tid, uid)

    async def set_enabled(self, enabled: bool) -> None:
        async with bypass_rls_session():
            await self.cat_store.update(
                catalog_id=self.server_id, patch=McpConnectorCatalogPatch(enabled=enabled)
            )


async def _two_connectors() -> _TwoConnectors:
    cat_store = InMemoryMcpConnectorCatalogStore()
    oauth_store = InMemoryMcpOAuthConnectionStore()
    tid = uuid4()
    server_id = await _seed_oauth_catalog(cat_store, _SERVER)
    other_id = await _seed_oauth_catalog(cat_store, _OTHER)
    await _connect_user(oauth_store, tid, "user-1", server_id, _SERVER)
    await _connect_user(oauth_store, tid, "user-1", other_id, _OTHER)
    await _connect_user(oauth_store, tid, "user-2", other_id, _OTHER)
    now = {"t": 0.0}
    expired: list[tuple[UUID, str]] = []
    svc = UserMcpOAuthPoolService(
        oauth_store=oauth_store,
        catalog_store=cat_store,
        client_factory=_started_client_factory(None),
        clock=lambda: _NOW,
        monotonic_clock=lambda: now["t"],
        on_expire=lambda t, u: expired.append((t, u)),
    )
    return _TwoConnectors(svc, cat_store, server_id, tid, now, expired)


class _GatedSession(_EchoSession):
    def __init__(self) -> None:
        self.gate, self.entered = asyncio.Event(), asyncio.Event()

    async def call_tool(self, name: str, args: dict[str, Any]) -> Any:
        self.entered.set()
        await self.gate.wait()
        return await super().call_tool(name, args)


@pytest.mark.asyncio
async def test_connector_off_stops_only_that_connector() -> None:
    """The platform disables one connector: it is cut off for every user, while
    the same user's other connector finishes its in-flight call and keeps
    serving (a whole-pool invalidate made it fail "not started")."""
    c = await _two_connectors()
    pool1, pool2 = await c.pool("user-1"), await c.pool("user-2")
    off_tool = await _bind_tool_like_a_built_agent(pool1.get(_SERVER))
    other = pool1.get(_OTHER)
    other_tool = await _bind_tool_like_a_built_agent(other, _OTHER)
    session = _GatedSession()
    other._session = session
    in_flight = asyncio.create_task(_call(other_tool))
    await session.entered.wait()

    await c.svc.connector_off(_SERVER)

    await _assert_hard_stop(off_tool, off_tool.client)
    session.gate.set()
    assert await in_flight == "echo:hi"
    assert await _call(other_tool) == "echo:hi"  # retired: one-shot sessions
    assert other._closed is False
    assert c.expired == [(c.tid, "user-1")]
    assert await c.pool("user-2") is pool2  # had no such connector: untouched


@pytest.mark.asyncio
async def test_connector_off_reaches_copies_an_earlier_ttl_retired() -> None:
    c = await _two_connectors()
    pool1 = await c.pool("user-1")
    off_tool = await _bind_tool_like_a_built_agent(pool1.get(_SERVER))
    other_tool = await _bind_tool_like_a_built_agent(pool1.get(_OTHER), _OTHER)
    c.now["t"] = 1801.0
    await c.pool("user-1")  # TTL: both retired, still held by a run

    await c.svc.connector_off(_SERVER)

    await _assert_hard_stop(off_tool, off_tool.client)
    assert await _call(other_tool) == "echo:hi"


@pytest.mark.asyncio
async def test_connector_on_expires_only_pools_built_without_it() -> None:
    c = await _two_connectors()
    await c.set_enabled(False)
    pool1, pool2 = await c.pool("user-1"), await c.pool("user-2")
    assert pool1.names() == [_OTHER]
    other_tool = await _bind_tool_like_a_built_agent(pool1.get(_OTHER), _OTHER)
    await c.set_enabled(True)

    await c.svc.connector_on(_SERVER)

    assert sorted((await c.pool("user-1")).names()) == sorted([_SERVER, _OTHER])
    assert await _call(other_tool) == "echo:hi"  # retired, not stopped
    assert other_tool.client._closed is False
    assert await c.pool("user-2") is pool2
    assert c.expired == [(c.tid, "user-1")]


@pytest.mark.asyncio
async def test_connector_off_mid_build_makes_the_build_retry() -> None:
    """A build that read the catalog before the switch must not cache it."""
    c = await _two_connectors()
    factory = c.svc._client_factory
    gate, entered = asyncio.Event(), asyncio.Event()

    async def _paused_once(config: MCPServerConfig) -> MCPClient:
        if not entered.is_set():
            entered.set()
            await gate.wait()
        return await factory(config)

    c.svc._client_factory = _paused_once
    build = asyncio.create_task(c.pool("user-1"))
    await entered.wait()
    await c.set_enabled(False)
    await c.svc.connector_off(_SERVER)
    gate.set()

    assert (await build).names() == [_OTHER]


@pytest.mark.asyncio
async def test_bulk_release_is_concurrent_and_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    """Review M1: a catalog switch tears down many users' clients from the
    request and the bus subscriber loop; one stuck server must not stall it."""
    from control_plane import mcp_retired_clients
    from orchestrator.tools.mcp import RecordingMCPClient

    monkeypatch.setattr(mcp_retired_clients, "TEARDOWN_BUDGET_S", 0.2)

    class _Hangs:
        async def close(self) -> None:
            await asyncio.Event().wait()

    fast = RecordingMCPClient(tools=())
    loop = asyncio.get_running_loop()
    started = loop.time()

    await mcp_retired_clients.release_clients(close=[_Hangs()], retire=[fast])  # type: ignore[list-item]

    assert loop.time() - started < 1.0
    assert fast.closed is True  # no ``retire``: closed, and not stuck behind the hang


@pytest.mark.asyncio
@pytest.mark.parametrize("switched_back", [False, True])
async def test_connector_off_mid_build_stops_it_in_a_last_attempt_pool(
    switched_back: bool,
) -> None:
    """Review-3 L1: the build read the catalog before the switch and then ran
    out of retries; its uncached pool must not serve the switched-off
    connector (it is not cached, so ``connector_off`` could not see it) —
    unless it was switched back on meanwhile."""
    c = await _two_connectors()
    read = c.svc._oauth_store.list_for_user
    first = {"read": True}

    async def _read(*args: Any, **kwargs: Any) -> Any:
        if first["read"]:
            first["read"] = False
            await c.svc.connector_off(_SERVER)  # the catalog read still says enabled
            if switched_back:
                await c.svc.connector_on(_SERVER)
        await c.svc.invalidate(c.tid, "user-1")
        return await read(*args, **kwargs)

    c.svc._oauth_store.list_for_user = _read  # type: ignore[method-assign]
    pool = await c.pool("user-1")

    assert sorted(pool.names()) == sorted([_SERVER, _OTHER])  # served uncached
    off = pool.get(_SERVER)
    assert isinstance(off, _FakeTransportClient)
    opens = off.opens
    if switched_back:
        assert (await off.call_tool("echo", {"text": "hi"})).content == "echo:hi"
    else:
        with pytest.raises(RuntimeError, match="not started"):
            await off.call_tool("echo", {"text": "hi"})
        assert off.opens == opens  # never dialed
    other_tool = await _bind_tool_like_a_built_agent(pool.get(_OTHER), _OTHER)
    assert await _call(other_tool) == "echo:hi"


@pytest.mark.asyncio
@pytest.mark.parametrize("on", [False, True])
async def test_connector_switch_evicts_builds_before_teardown(on: bool) -> None:
    """Review-3 L6: no new run may pick a cached agent while its clients are
    being torn down."""
    c = await _two_connectors()
    if on:
        await c.set_enabled(False)
    pool1 = await c.pool("user-1")
    watched = pool1.get(_OTHER if on else _SERVER)
    released_at_eviction: list[bool] = []
    c.svc._on_expire = lambda t, u: released_at_eviction.append(watched._closed or watched._retired)

    await (c.svc.connector_on(_SERVER) if on else c.svc.connector_off(_SERVER))

    assert released_at_eviction == [False]
    assert watched._retired if on else watched._closed
