"""Unit tests for the per-(tenant, user) MCP OAuth pool — Stream MCP-OAUTH (OA-3b)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from control_plane.tenant_scope import bypass_rls_session
from control_plane.user_mcp_oauth_pool import (
    _FAILED_RETRY_S,
    McpClientFactory,
    UserMcpOAuthPoolService,
)
from expert_work.persistence import (
    InMemoryMcpConnectorCatalogStore,
    InMemoryMcpOAuthConnectionStore,
)
from expert_work.protocol import (
    McpConnectorAuthSchema,
    McpConnectorCatalogPatch,
    McpConnectorCatalogUpsert,
    McpOAuthConnectionPatch,
    McpOAuthConnectionRecord,
)
from orchestrator.tools.mcp import MCPServerConfig, MCPServerPool, MCPToolDef, RecordingMCPClient
from tests.test_tenant_mcp_pool import _Dialer

_NOW = datetime(2026, 6, 6, 12, 0, tzinfo=UTC)


def _factory_spy(calls: list[str]) -> McpClientFactory:
    async def _factory(config: MCPServerConfig) -> RecordingMCPClient:
        calls.append(config.name)
        return RecordingMCPClient(tools=(MCPToolDef(name="t", description="", input_schema={}),))

    return _factory


async def _seed_catalog(store: InMemoryMcpConnectorCatalogStore, *, name: str = "linear") -> UUID:
    async with bypass_rls_session():
        rec = await store.create(
            upsert=McpConnectorCatalogUpsert(
                name=name,
                display_name="Linear",
                transport="sse",
                url_template="https://mcp.linear.app/sse",
                auth_type="oauth2",
                auth_schema=McpConnectorAuthSchema(),
                oauth_client_id="cid",
                oauth_scopes="read",
            ),
            actor_id="seed",
        )
    return rec.id


async def _seed_connection(
    store: InMemoryMcpOAuthConnectionStore,
    *,
    tenant_id: UUID,
    user_id: str,
    catalog_id: UUID,
    name: str = "linear",
    status: str = "connected",
    expires_at: datetime | None = None,
) -> None:
    rec = await store.create(
        tenant_id=tenant_id,
        user_id=user_id,
        catalog_id=catalog_id,
        name=name,
        resolved_url="https://mcp.linear.app/sse",
        oauth_state="st",
        pkce_verifier="pv",
    )
    if status == "connected":
        await store.update(
            connection_id=rec.id,
            tenant_id=tenant_id,
            user_id=user_id,
            patch=McpOAuthConnectionPatch(
                status="connected",
                access_token_ref=f"secret://expert-work/tenant/{tenant_id}/mcp-oauth/{rec.id}/access",
                token_expires_at=expires_at,
                clear_flow_state=True,
            ),
        )


def _svc(
    oauth_store: InMemoryMcpOAuthConnectionStore,
    catalog_store: InMemoryMcpConnectorCatalogStore,
    calls: list[str],
) -> UserMcpOAuthPoolService:
    return UserMcpOAuthPoolService(
        oauth_store=oauth_store,
        catalog_store=catalog_store,
        client_factory=_factory_spy(calls),
        clock=lambda: _NOW,
    )


@pytest.mark.asyncio
async def test_connected_unexpired_in_pool() -> None:
    cat_store = InMemoryMcpConnectorCatalogStore()
    oauth_store = InMemoryMcpOAuthConnectionStore()
    tid, uid = uuid4(), "user-1"
    cat_id = await _seed_catalog(cat_store)
    await _seed_connection(
        oauth_store,
        tenant_id=tid,
        user_id=uid,
        catalog_id=cat_id,
        expires_at=_NOW + timedelta(hours=1),
    )
    calls: list[str] = []
    pool = await _svc(oauth_store, cat_store, calls).get_or_build(tid, uid)
    assert pool.names() == ["linear"]
    assert calls == ["linear"]


@pytest.mark.asyncio
async def test_expired_token_skipped() -> None:
    cat_store = InMemoryMcpConnectorCatalogStore()
    oauth_store = InMemoryMcpOAuthConnectionStore()
    tid, uid = uuid4(), "user-1"
    cat_id = await _seed_catalog(cat_store)
    await _seed_connection(
        oauth_store,
        tenant_id=tid,
        user_id=uid,
        catalog_id=cat_id,
        expires_at=_NOW - timedelta(seconds=1),
    )
    calls: list[str] = []
    pool = await _svc(oauth_store, cat_store, calls).get_or_build(tid, uid)
    assert pool.names() == []


@pytest.mark.asyncio
async def test_pending_connection_skipped() -> None:
    cat_store = InMemoryMcpConnectorCatalogStore()
    oauth_store = InMemoryMcpOAuthConnectionStore()
    tid, uid = uuid4(), "user-1"
    cat_id = await _seed_catalog(cat_store)
    await _seed_connection(
        oauth_store,
        tenant_id=tid,
        user_id=uid,
        catalog_id=cat_id,
        status="pending",
    )
    calls: list[str] = []
    pool = await _svc(oauth_store, cat_store, calls).get_or_build(tid, uid)
    assert pool.names() == []


@pytest.mark.asyncio
async def test_connector_disabled_in_catalog_skipped_without_refresh() -> None:
    """B-248 — disabling an oauth2 catalog entry turns it off for every user
    already connected (a new connect is refused already); it used to keep
    attaching, and its token kept being refreshed."""
    cat_store = InMemoryMcpConnectorCatalogStore()
    oauth_store = InMemoryMcpOAuthConnectionStore()
    tid, uid = uuid4(), "user-1"
    off_id = await _seed_catalog(cat_store, name="linear")
    on_id = await _seed_catalog(cat_store, name="notion")
    for cat_id, name in ((off_id, "linear"), (on_id, "notion")):
        await _seed_connection(
            oauth_store,
            tenant_id=tid,
            user_id=uid,
            catalog_id=cat_id,
            name=name,
            expires_at=_NOW + timedelta(hours=1),
        )
    async with bypass_rls_session():
        await cat_store.update(catalog_id=off_id, patch=McpConnectorCatalogPatch(enabled=False))
    refreshed: list[str] = []

    class _SpyRefresher:
        async def ensure_fresh(self, record: McpOAuthConnectionRecord) -> McpOAuthConnectionRecord:
            refreshed.append(record.name)
            return record

    calls: list[str] = []
    svc = UserMcpOAuthPoolService(
        oauth_store=oauth_store,
        catalog_store=cat_store,
        client_factory=_factory_spy(calls),
        refresher=_SpyRefresher(),  # type: ignore[arg-type]
        clock=lambda: _NOW,
    )

    pool = await svc.get_or_build(tid, uid)

    assert pool.names() == ["notion"]
    assert calls == ["notion"]
    assert refreshed == ["notion"]


@pytest.mark.asyncio
async def test_cache_hit_no_rebuild() -> None:
    cat_store = InMemoryMcpConnectorCatalogStore()
    oauth_store = InMemoryMcpOAuthConnectionStore()
    tid, uid = uuid4(), "user-1"
    cat_id = await _seed_catalog(cat_store)
    await _seed_connection(
        oauth_store,
        tenant_id=tid,
        user_id=uid,
        catalog_id=cat_id,
        expires_at=_NOW + timedelta(hours=1),
    )
    calls: list[str] = []
    svc = _svc(oauth_store, cat_store, calls)
    await svc.get_or_build(tid, uid)
    await svc.get_or_build(tid, uid)
    assert calls == ["linear"]  # built once, second served from cache


@pytest.mark.asyncio
async def test_invalidate_rebuilds() -> None:
    cat_store = InMemoryMcpConnectorCatalogStore()
    oauth_store = InMemoryMcpOAuthConnectionStore()
    tid, uid = uuid4(), "user-1"
    cat_id = await _seed_catalog(cat_store)
    await _seed_connection(
        oauth_store,
        tenant_id=tid,
        user_id=uid,
        catalog_id=cat_id,
        expires_at=_NOW + timedelta(hours=1),
    )
    calls: list[str] = []
    svc = _svc(oauth_store, cat_store, calls)
    await svc.get_or_build(tid, uid)
    await svc.invalidate(tid, uid)
    await svc.get_or_build(tid, uid)
    assert calls == ["linear", "linear"]  # rebuilt after invalidate


@pytest.mark.asyncio
async def test_refresher_renews_near_expiry_and_attaches() -> None:
    """OA-6: a wired refresher renews a near-expiry token so the connector
    still attaches (instead of being skipped as it would be without one)."""
    import httpx

    from control_plane.mcp_oauth_refresh import McpOAuthRefresher
    from expert_work.testing import InMemorySecretStore

    cat_store = InMemoryMcpConnectorCatalogStore()
    oauth_store = InMemoryMcpOAuthConnectionStore()
    sec = InMemorySecretStore()
    tid, uid = uuid4(), "user-1"
    cat_id = await _seed_catalog(cat_store)
    rec = await oauth_store.create(
        tenant_id=tid,
        user_id=uid,
        catalog_id=cat_id,
        name="linear",
        resolved_url="https://mcp.linear.app/sse",
        oauth_state="st",
        pkce_verifier="pv",
    )
    access_ref = f"secret://expert-work/tenant/{tid}/mcp-oauth/{rec.id}/access"
    refresh_ref = f"secret://expert-work/tenant/{tid}/mcp-oauth/{rec.id}/refresh"
    await sec.put(access_ref.removeprefix("secret://"), "AT1")
    await sec.put(refresh_ref.removeprefix("secret://"), "RT1")
    await oauth_store.update(
        connection_id=rec.id,
        tenant_id=tid,
        user_id=uid,
        patch=McpOAuthConnectionPatch(
            status="connected",
            access_token_ref=access_ref,
            refresh_token_ref=refresh_ref,
            token_expires_at=_NOW + timedelta(seconds=10),
            clear_flow_state=True,
        ),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/.well-known/oauth-protected-resource":
            return httpx.Response(
                200,
                json={
                    "authorization_servers": ["https://auth.linear.app"],
                    "resource": "https://mcp.linear.app/sse",
                },
            )
        if path == "/.well-known/oauth-authorization-server":
            return httpx.Response(
                200,
                json={
                    "authorization_endpoint": "https://auth.linear.app/authorize",
                    "token_endpoint": "https://auth.linear.app/token",
                },
            )
        if path == "/token":
            return httpx.Response(200, json={"access_token": "AT2", "expires_in": 3600})
        return httpx.Response(404)

    refresher = McpOAuthRefresher(
        oauth_store=oauth_store,
        catalog_store=cat_store,
        secret_store=sec,
        http_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        clock=lambda: _NOW,
    )
    calls: list[str] = []
    svc = UserMcpOAuthPoolService(
        oauth_store=oauth_store,
        catalog_store=cat_store,
        client_factory=_factory_spy(calls),
        refresher=refresher,
        clock=lambda: _NOW,
    )
    pool = await svc.get_or_build(tid, uid)
    assert pool.names() == ["linear"]  # refreshed + attached, not skipped
    assert await sec.get(access_ref.removeprefix("secret://")) == "AT2"


@pytest.mark.asyncio
async def test_users_isolated() -> None:
    cat_store = InMemoryMcpConnectorCatalogStore()
    oauth_store = InMemoryMcpOAuthConnectionStore()
    tid = uuid4()
    cat_id = await _seed_catalog(cat_store)
    await _seed_connection(
        oauth_store,
        tenant_id=tid,
        user_id="user-1",
        catalog_id=cat_id,
        expires_at=_NOW + timedelta(hours=1),
    )
    calls: list[str] = []
    svc = _svc(oauth_store, cat_store, calls)
    p1 = await svc.get_or_build(tid, "user-1")
    p2 = await svc.get_or_build(tid, "user-2")
    assert p1.names() == ["linear"]
    assert p2.names() == []  # user-2 has no connections


@pytest.mark.asyncio
async def test_ttl_backstop_rebuilds_expired_pool_and_reuses_fresh_one() -> None:
    """PR-E3a: entries older than 1800s rebuild (old pool closed); younger reuse."""
    cat_store = InMemoryMcpConnectorCatalogStore()
    oauth_store = InMemoryMcpOAuthConnectionStore()
    tid, uid = uuid4(), "user-1"
    cat_id = await _seed_catalog(cat_store)
    await _seed_connection(
        oauth_store,
        tenant_id=tid,
        user_id=uid,
        catalog_id=cat_id,
        expires_at=_NOW + timedelta(hours=1),
    )
    calls: list[str] = []
    now = {"t": 0.0}
    svc = UserMcpOAuthPoolService(
        oauth_store=oauth_store,
        catalog_store=cat_store,
        client_factory=_factory_spy(calls),
        clock=lambda: _NOW,
        monotonic_clock=lambda: now["t"],
    )
    p1 = await svc.get_or_build(tid, uid)
    now["t"] = 1799.0
    assert await svc.get_or_build(tid, uid) is p1  # younger than TTL → reused
    assert calls == ["linear"]
    now["t"] = 1801.0
    p2 = await svc.get_or_build(tid, uid)
    assert p2 is not p1  # older than TTL → rebuilt
    assert calls == ["linear", "linear"]
    assert p1.names() == []  # the expired pool was closed (close_all clears clients)
    assert p2.names() == ["linear"]


# B-292 —— 某个连接没连上:到点后的那次取池起一个后台重拨(用户池每次 run 都会取),
# 不挡这次取池;只拨缺的,连上了才换新池对象(B-260 按对象认池)、清这个用户的构建。


class _RecordingRefresher:
    """Refresher spy: records each connection it is asked about; ``unusable`` names
    come back as ``None`` (revoked / expired and not refreshable)."""

    def __init__(self) -> None:
        self.seen: list[str] = []
        self.unusable: set[str] = set()
        self.gates: dict[str, asyncio.Event] = {}  # one-shot: hold this refresh mid-way
        self.entered = asyncio.Event()
        self.completed: list[str] = []  # refreshes that ran to the end (token persisted)

    async def ensure_fresh(
        self, record: McpOAuthConnectionRecord
    ) -> McpOAuthConnectionRecord | None:
        self.seen.append(record.name)
        gate = self.gates.pop(record.name, None)
        if gate is not None:
            self.entered.set()  # e.g. the token endpoint has rotated the refresh token …
            await gate.wait()  # … and the new one is not persisted yet
        self.completed.append(record.name)
        return None if record.name in self.unusable else record


class _UserRig:
    def __init__(self, down: set[str]) -> None:
        self.cat_store = InMemoryMcpConnectorCatalogStore()
        self.oauth_store = InMemoryMcpOAuthConnectionStore()
        self.tid, self.uid = uuid4(), "user-1"
        self.dialer = _Dialer(down)
        self.refresher = _RecordingRefresher()
        self.catalog_ids: dict[str, UUID] = {}
        self.now = {"t": 0.0}
        self.expired: list[tuple[UUID, str]] = []
        self.svc = UserMcpOAuthPoolService(
            oauth_store=self.oauth_store,
            catalog_store=self.cat_store,
            client_factory=self.dialer,
            refresher=self.refresher,  # type: ignore[arg-type]
            clock=lambda: _NOW,
            monotonic_clock=lambda: self.now["t"],
            on_expire=lambda t, u: self.expired.append((t, u)),
        )

    async def set_connector_enabled(self, name: str, enabled: bool) -> None:
        """The platform flips the catalog entry, then the bus fans out connector_off/on."""
        async with bypass_rls_session():
            await self.cat_store.update(
                catalog_id=self.catalog_ids[name], patch=McpConnectorCatalogPatch(enabled=enabled)
            )
        if enabled:
            await self.svc.connector_on(name)
        else:
            await self.svc.connector_off(name)

    async def get(self) -> MCPServerPool:
        async with asyncio.timeout(1):  # never waits on a retry's dial
            return await self.svc.get_or_build(self.tid, self.uid)

    async def settle(self) -> None:
        for _ in range(5):
            await asyncio.sleep(0)

    async def run_retry(self) -> None:
        """等后台重拨跑完(它不睡,可能已经跑完了;有没有拨过由 ``dialer.calls`` 断言)。"""
        async with asyncio.timeout(1):
            while self.svc._retry_tasks:
                await asyncio.gather(*self.svc._retry_tasks)


async def _user_rig(*names: str, down: set[str]) -> _UserRig:
    rig = _UserRig(down)
    for name in names:
        cat_id = await _seed_catalog(rig.cat_store, name=name)
        rig.catalog_ids[name] = cat_id
        await _seed_connection(
            rig.oauth_store,
            tenant_id=rig.tid,
            user_id=rig.uid,
            catalog_id=cat_id,
            name=name,
            expires_at=_NOW + timedelta(hours=1),
        )
    return rig


@pytest.mark.asyncio
async def test_failed_connection_retried_in_background_then_new_pool_and_builds_evicted() -> None:
    rig = await _user_rig("linear", "notion", down={"notion"})
    p1 = await rig.get()
    assert p1.names() == ["linear"]
    linear = p1.get("linear")
    rig.now["t"] = _FAILED_RETRY_S - 1
    assert await rig.get() is p1  # inside the window: no retry
    await rig.settle()
    assert not rig.svc._retry_tasks
    assert rig.dialer.calls == ["linear", "notion"]

    rig.dialer.down.clear()
    rig.now["t"] = _FAILED_RETRY_S
    assert await rig.get() is p1  # due: the retry starts in the background
    await rig.run_retry()

    p2 = await rig.get()
    assert p2 is not p1  # gained a server → new object (B-260 identity check)
    assert p2.names() == ["linear", "notion"]
    assert rig.dialer.calls == ["linear", "notion", "notion"]  # only the missing one
    assert p2.get("linear") is linear and not rig.dialer.last("linear").closed
    assert rig.expired == [(rig.tid, rig.uid)]

    rig.now["t"] = 1000.0
    assert await rig.get() is p2  # complete: no more retries
    await rig.settle()
    assert not rig.svc._retry_tasks
    rig.now["t"] = 1801.0  # the retry did not extend the original TTL
    assert await rig.get() is not p2


@pytest.mark.asyncio
async def test_still_failing_connection_keeps_pool_object_and_backs_off() -> None:
    rig = await _user_rig("linear", "notion", down={"notion"})
    p1 = await rig.get()
    rig.now["t"] = _FAILED_RETRY_S
    await rig.get()
    await rig.run_retry()
    assert await rig.get() is p1  # nothing gained → same object, in-flight builds keep it
    assert rig.expired == []
    assert rig.dialer.calls == ["linear", "notion", "notion"]

    rig.now["t"] = 179.0  # backoff grew to 120 s: next retry at 180
    await rig.get()
    await rig.run_retry()
    assert rig.dialer.calls == ["linear", "notion", "notion"]  # not due yet: no redial
    rig.now["t"] = 180.0
    await rig.get()
    await rig.run_retry()
    assert rig.dialer.calls == ["linear", "notion", "notion", "notion"]
    rig.now["t"] = 299.0  # capped at 120 s (not 240): next retry at 300
    await rig.get()
    await rig.run_retry()
    assert rig.dialer.calls.count("notion") == 3
    rig.now["t"] = 300.0
    await rig.get()
    await rig.run_retry()
    assert rig.dialer.calls.count("notion") == 4
    assert not rig.dialer.last("linear").closed


@pytest.mark.asyncio
async def test_retry_dial_does_not_block_get_or_build() -> None:
    rig = await _user_rig("linear", "notion", down={"notion"})
    p1 = await rig.get()
    rig.dialer.down.clear()
    gate = await rig.dialer.gate_next("notion")
    rig.now["t"] = _FAILED_RETRY_S
    assert await rig.get() is p1  # starts the retry, does not wait for it
    async with asyncio.timeout(1):
        await rig.dialer.dialing.wait()
    assert await rig.get() is p1  # a concurrent run while the dial hangs …
    assert len(rig.svc._retry_tasks) == 1  # … starts no second retry
    gate.set()
    await rig.run_retry()
    assert (await rig.get()).names() == ["linear", "notion"]


@pytest.mark.asyncio
@pytest.mark.parametrize("event", ["invalidate", "connector_off", "ttl_expired", "ttl_passed_idle"])
async def test_retry_landing_after_pool_changed_closes_what_it_opened(event: str) -> None:
    rig = await _user_rig("linear", "notion", down={"notion"})
    p1 = await rig.get()
    rig.dialer.down.clear()
    gate = await rig.dialer.gate_next("notion")
    rig.now["t"] = _FAILED_RETRY_S
    await rig.get()
    async with asyncio.timeout(1):
        await rig.dialer.dialing.wait()

    if event == "invalidate":
        await rig.svc.invalidate(rig.tid, rig.uid)
    elif event == "connector_off":
        await rig.svc.connector_off("linear")
    elif event == "ttl_passed_idle":
        rig.now["t"] = 1801.0  # still cached, but expired: nobody asked for it yet
    else:
        rig.now["t"] = 1801.0
        assert await rig.get() is not p1  # TTL path rebuilt
    evicted_before = list(rig.expired)

    gate.set()
    await rig.run_retry()
    (opened,) = rig.dialer.gated
    assert opened.closed  # stale: closed, not swapped in, not leaked
    assert rig.expired == evicted_before


@pytest.mark.asyncio
async def test_close_all_mid_retry_closes_opened_clients_and_stops_scheduling() -> None:
    rig = await _user_rig("linear", "notion", "jira", down={"notion", "jira"})
    await rig.get()
    rig.dialer.down.clear()
    gate = await rig.dialer.gate_next("notion")  # the store lists by name: jira first
    rig.now["t"] = _FAILED_RETRY_S
    await rig.get()
    async with asyncio.timeout(1):
        await rig.dialer.dialing.wait()  # jira dialed fine, notion hangs
    (retry,) = rig.svc._retry_tasks
    await rig.svc.close_all()
    assert retry.cancelled()
    assert rig.dialer.last("jira").closed  # opened by the retry, not leaked
    gate.set()

    rig.dialer.down.add("jira")
    await rig.get()  # a degraded build after shutdown …
    calls = list(rig.dialer.calls)
    rig.now["t"] += _FAILED_RETRY_S
    await rig.get()  # … and a due hit
    await rig.run_retry()
    assert rig.dialer.calls == calls  # start no retry nobody would cancel


@pytest.mark.asyncio
async def test_retried_connection_that_no_longer_fits_is_closed_and_pool_kept() -> None:
    # Listed by name: a0 fails first, then s1..s5 fill the pool to its cap of 5.
    rig = await _user_rig("a0", "s1", "s2", "s3", "s4", "s5", down={"a0"})
    p1 = await rig.get()
    assert p1.names() == ["s1", "s2", "s3", "s4", "s5"]
    rig.dialer.down.clear()
    rig.now["t"] = _FAILED_RETRY_S
    await rig.get()
    await rig.run_retry()
    assert rig.dialer.last("a0").closed  # dialed, could not fit: closed, not leaked
    assert await rig.get() is p1  # nothing swapped in → same object
    assert rig.expired == []
    calls = list(rig.dialer.calls)
    rig.now["t"] = 1000.0
    await rig.get()
    await rig.run_retry()
    assert rig.dialer.calls == calls  # cannot fit until a rebuild: no more retries


@pytest.mark.asyncio
async def test_retry_does_not_reattach_a_switched_off_connector_and_resumes_after_on() -> None:
    """B-248 + B-292 —— the probe: pool [linear], notion down → platform switches notion
    off → notion recovers → the retry must not bring it back. Switched back on, the
    pending retry picks it up (connector_on only expires pools that skipped it as off)."""
    rig = await _user_rig("linear", "notion", down={"notion"})
    p1 = await rig.get()
    await rig.set_connector_enabled("notion", False)
    rig.dialer.down.clear()
    rig.now["t"] = _FAILED_RETRY_S
    await rig.get()
    await rig.run_retry()
    assert (await rig.get()).names() == ["linear"]
    assert rig.dialer.calls == ["linear", "notion"]  # switched off: not even dialed
    assert rig.expired == []

    await rig.set_connector_enabled("notion", True)
    rig.now["t"] = 180.0  # next retry (backoff 120 s)
    await rig.get()
    await rig.run_retry()
    p2 = await rig.get()
    assert p2 is not p1 and p2.names() == ["linear", "notion"]
    assert rig.expired == [(rig.tid, rig.uid)]


@pytest.mark.asyncio
async def test_connector_switched_off_mid_dial_is_closed_not_attached() -> None:
    rig = await _user_rig("linear", "notion", down={"notion"})
    p1 = await rig.get()
    rig.dialer.down.clear()
    gate = await rig.dialer.gate_next("notion")
    rig.now["t"] = _FAILED_RETRY_S
    await rig.get()
    async with asyncio.timeout(1):
        await rig.dialer.dialing.wait()
    await rig.set_connector_enabled("notion", False)  # lands while notion is being dialed
    gate.set()
    await rig.run_retry()
    (opened,) = rig.dialer.gated
    assert opened.closed  # closed, not attached
    assert await rig.get() is p1 and p1.names() == ["linear"]
    assert rig.expired == []  # builds not evicted


@pytest.mark.asyncio
async def test_aborted_retry_pushes_the_next_one_forward() -> None:
    rig = await _user_rig("linear", "notion", down={"notion"})
    await rig.get()
    rig.dialer.down.clear()
    gate = await rig.dialer.gate_next("notion")
    rig.now["t"] = _FAILED_RETRY_S
    await rig.get()
    async with asyncio.timeout(1):
        await rig.dialer.dialing.wait()
    await rig.svc.connector_on("unrelated")  # bumps generations, leaves the pool cached
    gate.set()
    await rig.run_retry()
    (opened,) = rig.dialer.gated
    assert opened.closed  # the aborted retry's client is not attached
    calls = list(rig.dialer.calls)
    await rig.get()  # same moment: the next retry is not due yet …
    await rig.run_retry()
    assert rig.dialer.calls == calls  # … so no immediate redial
    rig.now["t"] = 180.0
    await rig.get()
    await rig.run_retry()
    assert (await rig.get()).names() == ["linear", "notion"]


@pytest.mark.asyncio
async def test_retry_resolves_the_connection_through_the_refresher() -> None:
    rig = await _user_rig("linear", "notion", down={"notion"})
    await rig.get()
    assert rig.refresher.seen == ["linear", "notion"]  # the build consults it
    rig.dialer.down.clear()
    rig.now["t"] = _FAILED_RETRY_S
    await rig.get()
    await rig.run_retry()
    assert rig.refresher.seen == ["linear", "notion", "notion"]  # so does the retry
    assert (await rig.get()).names() == ["linear", "notion"]


@pytest.mark.asyncio
async def test_retry_skips_a_connection_that_is_no_longer_usable() -> None:
    rig = await _user_rig("linear", "notion", down={"notion"})
    p1 = await rig.get()
    rig.dialer.down.clear()
    rig.refresher.unusable.add("notion")  # revoked since the build
    rig.now["t"] = _FAILED_RETRY_S
    await rig.get()
    await rig.run_retry()
    assert rig.dialer.calls == ["linear", "notion"]  # not dialed with a dead token
    assert await rig.get() is p1
    assert rig.expired == []


@pytest.mark.asyncio
async def test_retry_rereads_the_catalog_even_without_the_bus_event() -> None:
    """A replica that has not (yet) seen ``connector_off`` still must not reattach a
    connector the platform disabled: the retry reads the catalog like a build does."""
    rig = await _user_rig("linear", "notion", down={"notion"})
    p1 = await rig.get()
    async with bypass_rls_session():
        await rig.cat_store.update(
            catalog_id=rig.catalog_ids["notion"], patch=McpConnectorCatalogPatch(enabled=False)
        )
    rig.dialer.down.clear()
    rig.now["t"] = _FAILED_RETRY_S
    await rig.get()
    await rig.run_retry()
    assert rig.dialer.calls == ["linear", "notion"]
    assert await rig.get() is p1


@pytest.mark.asyncio
async def test_close_all_lets_an_in_flight_retry_refresh_finish() -> None:
    """Shutdown cancels retries; a cancel landing after the token endpoint rotated the
    refresh token but before the new one was persisted would lose it (the next refresh
    then marks the connection revoked). The refresh runs to the end — and close_all
    waits for it — while the dial that would follow is still cancelled."""
    rig = await _user_rig("linear", "notion", down={"notion"})
    await rig.get()
    rig.dialer.down.clear()
    gate = rig.refresher.gates["notion"] = asyncio.Event()
    rig.now["t"] = _FAILED_RETRY_S
    await rig.get()
    async with asyncio.timeout(1):
        await rig.refresher.entered.wait()  # the retry is mid-refresh
    (retry,) = rig.svc._retry_tasks

    closing = asyncio.create_task(rig.svc.close_all())
    await rig.settle()
    assert not closing.done()  # shutdown waits for the refresh to persist …
    gate.set()
    async with asyncio.timeout(1):
        await closing
    assert retry.cancelled()
    assert rig.refresher.completed == ["linear", "notion", "notion"]  # … which completed
    assert rig.dialer.calls == ["linear", "notion"]  # and nothing was dialed / attached after
