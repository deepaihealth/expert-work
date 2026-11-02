"""B-259 —— 改 MCP 目录 / 租户 MCP 服务不再打断正在用的连接。

以前目录里任何一次新建 / 修改 / 删除(连改一句说明)都把整个平台池关掉,本副本和
其它副本上每个平台 MCP 连接都被硬停,正在跑的 run 后面的调用全部 "not started"
(10-30 事故的症状,日常改目录就能触发);租户 MCP 服务的改动同样整池关掉该租户。

现在只动改到的那一项:只改展示字段什么都不动;改连接配置退役它(在途调用跑完,
之后走一次性会话,构建清掉后新 run 用新配置);停用 / 删除只硬停它;新建什么都不停。
其它项的长连接原样留给下一次构建复用。

客户端是真的 :class:`StreamableHttpMCPClient`,只把握手换成假的(见
``test_mcp_pool_closed_client_holders``),池、工具绑定、退役 / 硬停状态都是生产代码。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from control_plane.app import create_app
from control_plane.invalidation_bus import InvalidationEvent, build_invalidation_handlers
from control_plane.platform_mcp_pool import PlatformMcpPoolService
from control_plane.tenant_mcp_pool import TenantMcpPoolService, classify_server_patch
from control_plane.tenant_scope import bypass_rls_session
from expert_work.common.lifecycle import Lifecycle
from expert_work.persistence import InMemoryMcpConnectorCatalogStore, InMemoryTenantMcpServerStore
from expert_work.protocol import (
    McpConnectorAuthSchema,
    McpConnectorCatalogPatch,
    McpConnectorCatalogUpsert,
    Role,
    TenantMcpServerPatch,
)
from orchestrator.tools.mcp import MCPToolDef
from tests.auth_fixtures import build_test_jwt_verifier, make_test_jwt
from tests.test_mcp_catalog_api import _build_settings
from tests.test_mcp_pool_closed_client_holders import (
    _assert_hard_stop,
    _bind_tool_like_a_built_agent,
    _call,
    _FakeTransportClient,
    _GatedSession,
    _started_client_factory,
)

_URL = "https://mcp.example.com/mcp"
_NEW_URL = "https://mcp2.example.com/mcp"


async def _probe_ok(**_kwargs: object) -> list[MCPToolDef]:
    return [MCPToolDef(name="echo", description="echo", input_schema={})]


@pytest.fixture(autouse=True)
def _patch_probes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("control_plane.api.mcp_catalog.probe_remote_mcp", _probe_ok)
    monkeypatch.setattr("control_plane.api.mcp_servers.probe_remote_mcp", _probe_ok)


class _Runtime:
    def __init__(self) -> None:
        self.all_calls = 0
        self.tenant_calls: list[UUID] = []

    def invalidate_all(self) -> None:
        self.all_calls += 1

    def invalidate_tenant(self, tenant_id: UUID) -> None:
        self.tenant_calls.append(tenant_id)


async def _start_gated_call(client: Any, tool: Any) -> tuple[_GatedSession, asyncio.Task[str]]:
    """A call that is running on the client's long-lived session right now."""
    session = _GatedSession()
    client._session = session
    task = asyncio.create_task(_call(tool))
    await session.entered.wait()
    return session, task


async def _drained(client: _FakeTransportClient) -> None:
    """Wait for the background close of a session parked by ``retire``."""
    if client._drain_close is not None:
        await asyncio.gather(client._drain_close)


def _live(client: _FakeTransportClient) -> bool:
    """Neither retired nor stopped: still serving on its long-lived session."""
    return not client._retired and not client._closed


# ---------------------------------------------------------------------------
# Platform catalog, through the real admin API and a real platform pool
# ---------------------------------------------------------------------------


class _CatalogRig:
    def __init__(self, client: AsyncClient, app: Any, headers: dict[str, str]) -> None:
        self.client = client
        self.app = app
        self.headers = headers
        self.built: list[_FakeTransportClient] = []
        self.svc = PlatformMcpPoolService(
            store=app.state.mcp_connector_catalog_store,
            client_factory=_started_client_factory(self.built),
        )
        self.runtime = _Runtime()
        app.state.platform_mcp_pool_service = self.svc
        app.state.agent_runtime = self.runtime
        self.ids: dict[str, str] = {}

    async def create(self, name: str) -> None:
        resp = await self.client.post(
            "/v1/platform/mcp-catalog",
            json={
                "name": name,
                "display_name": name,
                "description": "d",
                "transport": "streamable_http",
                "url_template": _URL,
                "auth_type": "none",
            },
            headers=self.headers,
        )
        assert resp.status_code == 201, resp.text
        self.ids[name] = resp.json()["data"]["id"]

    async def patch(self, name: str, body: dict[str, object]) -> None:
        resp = await self.client.patch(
            f"/v1/platform/mcp-catalog/{self.ids[name]}", json=body, headers=self.headers
        )
        assert resp.status_code == 200, resp.text

    async def delete(self, name: str) -> None:
        resp = await self.client.delete(
            f"/v1/platform/mcp-catalog/{self.ids[name]}", headers=self.headers
        )
        assert resp.status_code == 204, resp.text

    async def bound(self) -> tuple[Any, dict[str, Any], dict[str, Any]]:
        """Build the pool and bind each server's tool like a built agent."""
        pool = await self.svc.get_or_build()
        clients = {name: pool.get(name) for name in pool.names()}
        tools = {
            name: await _bind_tool_like_a_built_agent(client, name)
            for name, client in clients.items()
        }
        self.runtime.all_calls = 0
        return pool, clients, tools


@pytest.fixture
async def rig() -> AsyncIterator[_CatalogRig]:
    lifecycle = Lifecycle()
    lifecycle.mark_ready()
    app = create_app(
        settings=_build_settings(), lifecycle=lifecycle, jwt_verifier=build_test_jwt_verifier()
    )
    sys_admin_id = uuid4()
    await app.state.role_binding_repo.create(
        subject_type="user",
        subject_id=sys_admin_id,
        tenant_id=None,
        role=Role.SYSTEM_ADMIN,
        platform_scope=True,
        granted_by="seed",
    )
    headers = {
        "Authorization": f"Bearer {make_test_jwt(tenant_id=uuid4(), subject=str(sys_admin_id))}"
    }
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://cp.test") as client:
        r = _CatalogRig(client, app, headers)
        await r.create("deep")
        await r.create("other")
        yield r
        await r.svc.close_all()


@pytest.mark.asyncio
async def test_description_edit_mid_call_keeps_every_connection(rig: _CatalogRig) -> None:
    pool, clients, tools = await rig.bound()
    session, in_flight = await _start_gated_call(clients["deep"], tools["deep"])

    await rig.patch("deep", {"description": "edited mid-run", "display_name": "Deep 2"})

    session.gate.set()
    assert await in_flight == "echo:hi"
    assert await _call(tools["deep"]) == "echo:hi"
    assert all(_live(c) for c in clients.values())
    assert clients["deep"].opens == 1  # never redialed
    assert await rig.svc.get_or_build() is pool  # the cached pool is untouched
    assert rig.runtime.all_calls == 0


@pytest.mark.asyncio
async def test_url_change_lets_the_call_finish_then_goes_one_shot(rig: _CatalogRig) -> None:
    pool, clients, tools = await rig.bound()
    deep, other = clients["deep"], clients["other"]
    session, in_flight = await _start_gated_call(deep, tools["deep"])

    await rig.patch("deep", {"url_template": _NEW_URL})

    session.gate.set()
    assert await in_flight == "echo:hi"  # finished on the session it started on
    await _drained(deep)
    assert deep._retired and not deep._closed
    opens = deep.opens
    assert await _call(tools["deep"]) == "echo:hi"  # a later call: one-shot session
    assert deep.opens == opens + 1 == deep.closes
    assert rig.runtime.all_calls == 1  # builds evicted: new runs take the new URL
    rebuilt = await rig.svc.get_or_build()
    assert rebuilt is not pool
    assert rebuilt.get("deep").config.url == _NEW_URL  # type: ignore[union-attr]
    # The other server's long-lived connection is reused, not redialed / retired.
    assert _live(other) and other.opens == 1
    assert rebuilt.get("other") is other
    assert await _call(tools["other"]) == "echo:hi"


@pytest.mark.asyncio
async def test_disable_stops_only_that_server(rig: _CatalogRig) -> None:
    _pool, clients, tools = await rig.bound()
    deep, other = clients["deep"], clients["other"]
    session, in_flight = await _start_gated_call(other, tools["other"])

    await rig.patch("deep", {"enabled": False})

    await _assert_hard_stop(tools["deep"], deep)
    session.gate.set()
    assert await in_flight == "echo:hi"  # the other server's call is untouched
    assert await _call(tools["other"]) == "echo:hi"
    assert _live(other) and other.opens == 1
    assert rig.runtime.all_calls == 1
    rebuilt = await rig.svc.get_or_build()
    assert rebuilt.names() == ["other"]
    assert rebuilt.get("other") is other


@pytest.mark.asyncio
async def test_delete_stops_only_that_server(rig: _CatalogRig) -> None:
    _pool, clients, tools = await rig.bound()
    deep, other = clients["deep"], clients["other"]
    session, in_flight = await _start_gated_call(other, tools["other"])

    await rig.delete("deep")

    await _assert_hard_stop(tools["deep"], deep)
    session.gate.set()
    assert await in_flight == "echo:hi"
    assert _live(other) and other.opens == 1
    assert rig.runtime.all_calls == 1
    assert (await rig.svc.get_or_build()).get("other") is other


@pytest.mark.asyncio
async def test_create_stops_nothing_and_the_next_build_adds_it(rig: _CatalogRig) -> None:
    _pool, clients, tools = await rig.bound()
    session, in_flight = await _start_gated_call(clients["deep"], tools["deep"])

    await rig.create("third")

    session.gate.set()
    assert await in_flight == "echo:hi"
    assert all(_live(c) and c.opens == 1 for c in clients.values())
    assert rig.runtime.all_calls == 1  # builds evicted so agents pick it up
    rebuilt = await rig.svc.get_or_build()
    assert sorted(rebuilt.names()) == ["deep", "other", "third"]
    assert rebuilt.get("deep") is clients["deep"]
    assert rebuilt.get("other") is clients["other"]


# ---------------------------------------------------------------------------
# Tenant MCP servers, through the real tenant API and a real tenant pool
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tenant_server_edit_reaches_only_that_server() -> None:
    from tests.test_mcp_servers_api import _make_app_with_admin

    app, headers, tenant_id = await _make_app_with_admin()
    built: list[_FakeTransportClient] = []
    svc = TenantMcpPoolService(
        store=app.state.tenant_mcp_server_store,  # type: ignore[attr-defined]
        secret_store=None,
        client_factory=_started_client_factory(built),
    )
    runtime = _Runtime()
    app.state.tenant_mcp_pool_service = svc  # type: ignore[attr-defined]
    app.state.agent_runtime = runtime  # type: ignore[attr-defined]
    transport = ASGITransport(app=app)  # type: ignore[arg-type]
    async with AsyncClient(transport=transport, base_url="http://cp.test") as client:
        for name in ("github", "linear"):
            resp = await client.post(
                "/v1/mcp-servers",
                json={"name": name, "transport": "sse", "url": _URL, "auth_type": "none"},
                headers=headers,
            )
            assert resp.status_code == 201, resp.text
        pool = await svc.get_or_build(tenant_id)
        github, linear = pool.get("github"), pool.get("linear")
        github_tool = await _bind_tool_like_a_built_agent(github, "github")
        linear_tool = await _bind_tool_like_a_built_agent(linear, "linear")
        session, in_flight = await _start_gated_call(github, github_tool)
        runtime.tenant_calls.clear()

        url = await client.patch("/v1/mcp-servers/github", json={"url": _NEW_URL}, headers=headers)
        assert url.status_code == 200, url.text
        session.gate.set()
        assert await in_flight == "echo:hi"
        assert github._retired and not github._closed  # type: ignore[union-attr]
        assert _live(linear)  # type: ignore[arg-type]
        assert runtime.tenant_calls == [tenant_id]
        rebuilt = await svc.get_or_build(tenant_id)
        assert rebuilt.get("linear") is linear

        off = await client.patch("/v1/mcp-servers/linear", json={"enabled": False}, headers=headers)
        assert off.status_code == 200, off.text
        await _assert_hard_stop(linear_tool, linear)  # type: ignore[arg-type]
        assert await _call(github_tool) == "echo:hi"  # retired, still serving
    await svc.close_all()


# ---------------------------------------------------------------------------
# Pool mechanics behind the above (platform pool, no API)
# ---------------------------------------------------------------------------


async def _platform(
    *, names: tuple[str, ...] = ("deep", "other"), now: dict[str, float] | None = None
) -> tuple[PlatformMcpPoolService, InMemoryMcpConnectorCatalogStore, dict[str, UUID]]:
    store = InMemoryMcpConnectorCatalogStore()
    ids: dict[str, UUID] = {}
    async with bypass_rls_session():
        for name in names:
            record = await store.create(
                upsert=McpConnectorCatalogUpsert(
                    name=name,
                    display_name=name,
                    transport="streamable_http",
                    url_template=_URL,
                    auth_type="none",
                    auth_schema=McpConnectorAuthSchema(),
                ),
                actor_id="sysadmin",
            )
            ids[name] = record.id
    svc = PlatformMcpPoolService(
        store=store,
        client_factory=_started_client_factory(None),
        clock=(lambda: now["t"]) if now is not None else time.monotonic,
    )
    return svc, store, ids


async def _set_url(store: InMemoryMcpConnectorCatalogStore, catalog_id: UUID, url: str) -> None:
    async with bypass_rls_session():
        await store.update(catalog_id=catalog_id, patch=McpConnectorCatalogPatch(url_template=url))


@pytest.mark.asyncio
async def test_a_kept_client_whose_config_changed_is_not_reused() -> None:
    """``other`` changed without its own event reaching this replica: the next
    build must not hand out the old connection, and retires it."""
    svc, store, ids = await _platform()
    pool = await svc.get_or_build()
    other = pool.get("other")
    await _set_url(store, ids["other"], _NEW_URL)

    await svc.server_changed("deep")
    rebuilt = await svc.get_or_build()

    assert rebuilt.get("other") is not other
    assert rebuilt.get("other").config.url == _NEW_URL  # type: ignore[union-attr]
    assert other._retired and not other._closed  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_reuse_does_not_stretch_the_ttl_backstop() -> None:
    now = {"t": 0.0}
    svc, _store, _ids = await _platform(now=now)
    first = await svc.get_or_build()
    other = first.get("other")
    now["t"] = 1000.0
    await svc.server_changed("deep")
    second = await svc.get_or_build()
    assert second.get("other") is other

    now["t"] = 1801.0  # 1801 s after ``other`` was dialed, 801 s after the rebuild
    third = await svc.get_or_build()

    assert third is not second
    assert other._retired  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_invalidate_and_shutdown_stop_kept_clients() -> None:
    for teardown in ("invalidate", "close_all"):
        svc, _store, _ids = await _platform()
        pool = await svc.get_or_build()
        other = pool.get("other")
        tool = await _bind_tool_like_a_built_agent(other, "other")
        await svc.server_changed("deep")  # ``other`` is now kept for the next build

        await getattr(svc, teardown)()

        await _assert_hard_stop(tool, other)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_removed_reaches_ttl_retired_copies_of_that_server_only() -> None:
    now = {"t": 0.0}
    svc, _store, _ids = await _platform(now=now)
    pool = await svc.get_or_build()
    deep, other = pool.get("deep"), pool.get("other")
    deep_tool = await _bind_tool_like_a_built_agent(deep, "deep")
    other_tool = await _bind_tool_like_a_built_agent(other, "other")
    now["t"] = 1801.0
    await svc.get_or_build()  # TTL: both retired, still held by a run

    await svc.server_removed("deep")

    await _assert_hard_stop(deep_tool, deep)  # type: ignore[arg-type]
    assert await _call(other_tool) == "echo:hi"


@pytest.mark.asyncio
async def test_a_switch_mid_build_never_closes_kept_clients() -> None:
    """The build in flight reused ``other`` from the kept set; a switch landing
    mid-build makes it retry — it must close only what it opened itself."""
    svc, store, _ids = await _platform()
    pool = await svc.get_or_build()
    other = pool.get("other")
    await svc.server_changed("deep")
    gate, entered = asyncio.Event(), asyncio.Event()
    real_list = store.list
    reads = {"n": 0}

    async def _gated_list(**kwargs: Any) -> Any:
        reads["n"] += 1
        if reads["n"] == 1:
            entered.set()
            await gate.wait()
        return await real_list(**kwargs)

    store.list = _gated_list  # type: ignore[method-assign]
    build = asyncio.create_task(svc.get_or_build())
    await entered.wait()
    await svc.server_changed("deep")
    gate.set()
    rebuilt = await build

    assert reads["n"] == 2  # retried
    assert rebuilt.get("other") is other
    assert _live(other)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_builds_are_evicted_before_the_server_is_stopped() -> None:
    svc, _store, _ids = await _platform()
    pool = await svc.get_or_build()
    deep = pool.get("deep")
    seen: list[bool] = []
    svc._on_expire = lambda: seen.append(deep._closed)  # type: ignore[union-attr]

    await svc.server_removed("deep")

    assert seen == [False]
    assert deep._closed  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# Peers — the bus handlers apply the same per-server action
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_platform_peer_handlers_reach_only_the_named_server() -> None:
    svc, _store, _ids = await _platform()
    pool = await svc.get_or_build()
    deep, other = pool.get("deep"), pool.get("other")
    runtime = _Runtime()
    handlers = build_invalidation_handlers(
        SimpleNamespace(platform_mcp_pool_service=svc, agent_runtime=runtime)
    )

    await handlers["platform_mcp_server_changed"](
        InvalidationEvent(kind="platform_mcp_server_changed", connector="deep")
    )
    assert deep._retired and not deep._closed  # type: ignore[union-attr]
    assert _live(other)  # type: ignore[arg-type]
    assert runtime.all_calls == 1

    rebuilt = await svc.get_or_build()
    await handlers["platform_mcp_server_removed"](
        InvalidationEvent(kind="platform_mcp_server_removed", connector="deep")
    )
    assert deep._closed and rebuilt.get("deep")._closed  # type: ignore[union-attr]
    assert _live(other)  # type: ignore[arg-type]
    assert runtime.all_calls == 2

    # No name (malformed / foreign publisher): nothing at all.
    await handlers["platform_mcp_server_removed"](
        InvalidationEvent(kind="platform_mcp_server_removed")
    )
    assert _live(other) and runtime.all_calls == 2  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_tenant_peer_handlers_reach_only_the_named_server() -> None:
    store = InMemoryTenantMcpServerStore()
    tid, bystander = uuid4(), uuid4()
    for t in (tid, bystander):
        for name in ("github", "linear"):
            await store.create(
                tenant_id=t,
                name=name,
                transport="streamable_http",
                url=_URL,
                auth_type="none",
                token_secret_ref=None,
                timeout_s=30.0,
                created_by="a@x",
            )
    svc = TenantMcpPoolService(
        store=store, secret_store=None, client_factory=_started_client_factory(None)
    )
    pool, other_tenant = await svc.get_or_build(tid), await svc.get_or_build(bystander)
    runtime = _Runtime()
    handlers = build_invalidation_handlers(
        SimpleNamespace(tenant_mcp_pool_service=svc, agent_runtime=runtime)
    )

    await store.update(
        tenant_id=tid, name="github", patch=TenantMcpServerPatch(url=_NEW_URL, enabled=None)
    )
    await handlers["tenant_mcp_server_changed"](
        InvalidationEvent(kind="tenant_mcp_server_changed", tenant_id=str(tid), connector="github")
    )
    assert pool.get("github")._retired  # type: ignore[union-attr]
    assert _live(pool.get("linear"))  # type: ignore[arg-type]
    assert runtime.tenant_calls == [tid]
    rebuilt = await svc.get_or_build(tid)
    assert rebuilt.get("linear") is pool.get("linear")

    await handlers["tenant_mcp_server_removed"](
        InvalidationEvent(kind="tenant_mcp_server_removed", tenant_id=str(tid), connector="linear")
    )
    assert pool.get("linear")._closed  # type: ignore[union-attr]
    assert _live(rebuilt.get("github"))  # type: ignore[arg-type]
    # Another tenant's servers of the same name are never touched.
    assert await svc.get_or_build(bystander) is other_tenant
    assert all(_live(other_tenant.get(n)) for n in other_tenant.names())  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Tenant side: classification and pool mechanics (review follow-ups)
# ---------------------------------------------------------------------------


class _Bus:
    def __init__(self) -> None:
        self.events: list[tuple[str, str | None, str | None]] = []

    async def publish(self, event: InvalidationEvent) -> None:
        self.events.append((event.kind, event.tenant_id, event.connector))


@pytest.mark.asyncio
async def test_tenant_header_repaste_retires_the_server() -> None:
    """Same header names → same secret ref and names on the row: only the
    values behind the ref change, so the fingerprint cannot see it."""
    from tests.test_mcp_servers_api import _make_app_with_admin

    app, headers, tenant_id = await _make_app_with_admin()
    svc = TenantMcpPoolService(
        store=app.state.tenant_mcp_server_store,  # type: ignore[attr-defined]
        secret_store=None,
        client_factory=_started_client_factory(None),
    )
    bus = _Bus()
    app.state.tenant_mcp_pool_service = svc  # type: ignore[attr-defined]
    app.state.invalidation_bus = bus  # type: ignore[attr-defined]
    transport = ASGITransport(app=app)  # type: ignore[arg-type]
    async with AsyncClient(transport=transport, base_url="http://cp.test") as client:
        resp = await client.post(
            "/v1/mcp-servers",
            json={
                "name": "github",
                "transport": "sse",
                "url": _URL,
                "auth_type": "none",
                "custom_headers": {"X-Org": "acme"},
            },
            headers=headers,
        )
        assert resp.status_code == 201, resp.text
        github = (await svc.get_or_build(tenant_id)).get("github")
        bus.events.clear()

        patch = await client.patch(
            "/v1/mcp-servers/github", json={"custom_headers": {"X-Org": "other"}}, headers=headers
        )
        assert patch.status_code == 200, patch.text

    assert bus.events == [("tenant_mcp_server_changed", str(tenant_id), "github")]
    assert github._retired and not github._closed  # type: ignore[union-attr]
    await svc.close_all()


async def _tenant_record(**update: object) -> Any:
    store = InMemoryTenantMcpServerStore()
    record = await store.create(
        tenant_id=uuid4(),
        name="github",
        transport="streamable_http",
        url=_URL,
        auth_type="none",
        token_secret_ref=None,
        timeout_s=30.0,
        created_by="a@x",
    )
    return record.model_copy(update=update)


@pytest.mark.asyncio
async def test_tenant_patch_that_leaves_it_disabled_is_always_a_stop() -> None:
    """``before`` is not read under a row lock — a concurrent enable may have
    built the server meanwhile, so disabled → disabled still stops it."""
    off = await _tenant_record(enabled=False)

    assert classify_server_patch(off, off, secret_rewritten=False) == "removed"


@pytest.mark.asyncio
async def test_tenant_classifier_defaults_unknown_fields_to_runtime() -> None:
    before = await _tenant_record()

    names_only = before.model_copy(update={"custom_header_names": ["X-New"]})
    probe_only = before.model_copy(update={"last_probe_status": "ok"})

    assert classify_server_patch(before, names_only, secret_rewritten=False) == "changed"
    assert classify_server_patch(before, probe_only, secret_rewritten=False) == "none"


async def _tenant_pool(now: dict[str, float]) -> tuple[TenantMcpPoolService, UUID]:
    store = InMemoryTenantMcpServerStore()
    tid = uuid4()
    for name in ("github", "linear"):
        await store.create(
            tenant_id=tid,
            name=name,
            transport="streamable_http",
            url=_URL,
            auth_type="none",
            token_secret_ref=None,
            timeout_s=30.0,
            created_by="a@x",
        )
    svc = TenantMcpPoolService(
        store=store,
        secret_store=None,
        client_factory=_started_client_factory(None),
        clock=lambda: now["t"],
    )
    return svc, tid


@pytest.mark.asyncio
async def test_tenant_removed_reaches_ttl_retired_copies_of_that_server_only() -> None:
    now = {"t": 0.0}
    svc, tid = await _tenant_pool(now)
    pool = await svc.get_or_build(tid)
    github, linear = pool.get("github"), pool.get("linear")
    github_tool = await _bind_tool_like_a_built_agent(github, "github")
    linear_tool = await _bind_tool_like_a_built_agent(linear, "linear")
    now["t"] = 1801.0
    await svc.get_or_build(tid)  # TTL: both retired, still held by a run

    await svc.server_removed(tid, "github")

    await _assert_hard_stop(github_tool, github)  # type: ignore[arg-type]
    assert await _call(linear_tool) == "echo:hi"


@pytest.mark.asyncio
async def test_tenant_reuse_does_not_stretch_the_ttl_backstop() -> None:
    now = {"t": 0.0}
    svc, tid = await _tenant_pool(now)
    linear = (await svc.get_or_build(tid)).get("linear")
    now["t"] = 1000.0
    await svc.server_changed(tid, "github")
    second = await svc.get_or_build(tid)
    assert second.get("linear") is linear

    now["t"] = 1801.0  # 1801 s after ``linear`` was dialed, 801 s after the rebuild
    third = await svc.get_or_build(tid)

    assert third is not second
    assert linear._retired  # type: ignore[union-attr]
