"""API tests for /v1/platform/mcp-catalog — Stream W (W-3).

Covers the system_admin CRUD surface, the platform-scope gating (tenant admin →
403), duplicate / not-found mappings, the Upsert + merged-record validators
(422), and the FK-RESTRICT in-use delete (409 CATALOG_IN_USE) via a fake store.

Fixtures mirror test_platform_config_api.py: system_admin is established by
seeding a SYSTEM_ADMIN role binding (the middleware augments the principal with
``is_system_admin``); a plain ``admin`` JWT stands in for a tenant admin.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from control_plane.app import create_app
from control_plane.audit import build_default_audit_logger
from control_plane.mcp_probe import McpProbeError
from control_plane.settings import Settings
from expert_work.common.lifecycle import Lifecycle
from expert_work.persistence import McpConnectorCatalogInUseError
from expert_work.persistence.audit_log import InMemoryAuditLogStore
from expert_work.protocol import AuditQuery, McpOAuthConnectionPatch, Role
from expert_work.runtime.secret_store.base import SecretNotFoundError
from orchestrator.tools.mcp import MCPToolDef
from tests.auth_fixtures import (
    TEST_AUDIENCE,
    TEST_ISSUER,
    build_test_jwt_verifier,
    make_test_jwt,
)


async def _fake_probe_ok(**_kwargs: object) -> list[MCPToolDef]:
    return [MCPToolDef(name="list_issues", description="List issues", input_schema={})]


@pytest.fixture(autouse=True)
def _patch_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    """Create/patch now connect-probe the server (W form-polish). Stub the probe
    so CRUD tests don't reach the network; tools-endpoint tests override it."""
    monkeypatch.setattr("control_plane.api.mcp_catalog.probe_remote_mcp", _fake_probe_ok)


def _build_settings() -> Settings:
    return Settings(
        service_name="control_plane_test",
        env="dev",
        auth_mode="dev",
        db_dsn="postgresql+asyncpg://test@localhost/test",
        rate_limit_burst=10_000,
        rate_limit_per_second=10_000.0,
        oidc_issuer=TEST_ISSUER,
        oidc_audience=[TEST_AUDIENCE],
    )


def _valid_entry(name: str = "github", *, category: str = "vcs") -> dict[str, object]:
    return {
        "name": name,
        "display_name": "GitHub",
        "description": "Official GitHub connector",
        "category": category,
        "transport": "streamable_http",
        "url_template": "https://mcp.github.com/mcp",
        "auth_type": "bearer",
        "auth_schema": {
            "fields": [{"key": "token", "label": "API Token", "kind": "secret", "required": True}]
        },
        "required_tier": "free",
    }


class _Ctx:
    def __init__(
        self,
        client: AsyncClient,
        audit_store: InMemoryAuditLogStore,
        app: object,
        admin_tenant: UUID,
        admin_headers: dict[str, str],
        tenant_admin_headers: dict[str, str],
    ) -> None:
        self.client = client
        self.audit_store = audit_store
        self.app = app
        self.admin_tenant = admin_tenant
        self.admin_headers = admin_headers
        self.tenant_admin_headers = tenant_admin_headers


@pytest.fixture
async def ctx() -> AsyncIterator[_Ctx]:
    lifecycle = Lifecycle()
    lifecycle.mark_ready()
    audit_store = InMemoryAuditLogStore()
    app = create_app(
        settings=_build_settings(),
        lifecycle=lifecycle,
        jwt_verifier=build_test_jwt_verifier(),
        audit_logger=build_default_audit_logger(audit_store),
    )
    # Seed a SYSTEM_ADMIN role binding so the middleware sets is_system_admin.
    sys_admin_id = uuid4()
    await app.state.role_binding_repo.create(  # type: ignore[attr-defined]
        subject_type="user",
        subject_id=sys_admin_id,
        tenant_id=None,
        role=Role.SYSTEM_ADMIN,
        platform_scope=True,
        granted_by="seed",
    )
    admin_tenant = uuid4()
    admin_jwt = make_test_jwt(tenant_id=admin_tenant, subject=str(sys_admin_id))
    admin_headers = {"Authorization": f"Bearer {admin_jwt}"}
    # A plain tenant admin (no platform scope) — has mcp_catalog RBAC via ADMIN
    # but must be rejected by the inline is_system_admin check.
    tenant_admin_jwt = make_test_jwt(tenant_id=uuid4(), subject=str(uuid4()), roles=("admin",))
    tenant_admin_headers = {"Authorization": f"Bearer {tenant_admin_jwt}"}
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://cp.test") as client:
        yield _Ctx(client, audit_store, app, admin_tenant, admin_headers, tenant_admin_headers)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_get_list_patch_delete(ctx: _Ctx) -> None:
    # create → 201
    create = await ctx.client.post(
        "/v1/platform/mcp-catalog", json=_valid_entry(), headers=ctx.admin_headers
    )
    assert create.status_code == 201, create.text
    body = create.json()
    assert body["success"] is True
    assert body["data"]["name"] == "github"
    catalog_id = body["data"]["id"]

    # get_by_id reflects it
    got = await ctx.client.get(f"/v1/platform/mcp-catalog/{catalog_id}", headers=ctx.admin_headers)
    assert got.status_code == 200
    assert got.json()["data"]["display_name"] == "GitHub"

    # list reflects it
    lst = await ctx.client.get("/v1/platform/mcp-catalog", headers=ctx.admin_headers)
    assert lst.status_code == 200
    names = {r["name"] for r in lst.json()["data"]}
    assert "github" in names

    # patch changes a field
    patch = await ctx.client.patch(
        f"/v1/platform/mcp-catalog/{catalog_id}",
        json={"display_name": "GitHub (official)", "enabled": False},
        headers=ctx.admin_headers,
    )
    assert patch.status_code == 200, patch.text
    assert patch.json()["data"]["display_name"] == "GitHub (official)"
    assert patch.json()["data"]["enabled"] is False

    # delete → 204
    delete = await ctx.client.delete(
        f"/v1/platform/mcp-catalog/{catalog_id}", headers=ctx.admin_headers
    )
    assert delete.status_code == 204
    gone = await ctx.client.get(f"/v1/platform/mcp-catalog/{catalog_id}", headers=ctx.admin_headers)
    assert gone.status_code == 404


@pytest.mark.asyncio
async def test_timeouts_round_trip_and_default_null(ctx: _Ctx) -> None:
    tuned = await ctx.client.post(
        "/v1/platform/mcp-catalog",
        json={
            "name": "tuned",
            "display_name": "Tuned",
            "transport": "sse",
            "url_template": "https://mcp.example.com/sse",
            "auth_type": "none",
            "timeout_s": 12,
            "sse_read_timeout_s": 600,
        },
        headers=ctx.admin_headers,
    )
    assert tuned.status_code == 201, tuned.text
    data = tuned.json()["data"]
    assert data["timeout_s"] == 12
    assert data["sse_read_timeout_s"] == 600

    # patch raises only timeout_s; sse_read_timeout_s is left unchanged.
    patch = await ctx.client.patch(
        f"/v1/platform/mcp-catalog/{data['id']}",
        json={"timeout_s": 45},
        headers=ctx.admin_headers,
    )
    assert patch.status_code == 200, patch.text
    assert patch.json()["data"]["timeout_s"] == 45
    assert patch.json()["data"]["sse_read_timeout_s"] == 600

    # Omitted on create → NULL (orchestrator defaults apply at runtime).
    plain = await ctx.client.post(
        "/v1/platform/mcp-catalog",
        json={
            "name": "plain",
            "display_name": "Plain",
            "transport": "sse",
            "url_template": "https://mcp.example.com/x",
            "auth_type": "none",
        },
        headers=ctx.admin_headers,
    )
    assert plain.status_code == 201, plain.text
    assert plain.json()["data"]["timeout_s"] is None
    assert plain.json()["data"]["sse_read_timeout_s"] is None


@pytest.mark.asyncio
async def test_create_rejects_unreachable_server_422(
    ctx: _Ctx, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _probe_fail(**_kwargs: object) -> list[MCPToolDef]:
        raise McpProbeError("MCP_SERVER_PROBE_FAILED", "connection refused")

    monkeypatch.setattr("control_plane.api.mcp_catalog.probe_remote_mcp", _probe_fail)
    resp = await ctx.client.post(
        "/v1/platform/mcp-catalog",
        json={
            "name": "down",
            "display_name": "Down",
            "transport": "sse",
            "url_template": "https://mcp.example.com/down",
            "auth_type": "none",
        },
        headers=ctx.admin_headers,
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"]["code"] == "MCP_SERVER_PROBE_FAILED"
    # Not persisted: a follow-up list must not contain it.
    lst = await ctx.client.get("/v1/platform/mcp-catalog", headers=ctx.admin_headers)
    assert "down" not in {r["name"] for r in lst.json()["data"]}


@pytest.mark.asyncio
async def test_tools_endpoint_ok_unreachable_and_oauth(
    ctx: _Ctx, monkeypatch: pytest.MonkeyPatch
) -> None:
    # ok — probe lists tools.
    created = await ctx.client.post(
        "/v1/platform/mcp-catalog",
        json={
            "name": "weather",
            "display_name": "Weather",
            "transport": "sse",
            "url_template": "https://mcp.example.com/weather",
            "auth_type": "none",
        },
        headers=ctx.admin_headers,
    )
    cid = created.json()["data"]["id"]
    ok = await ctx.client.post(f"/v1/platform/mcp-catalog/{cid}/tools", headers=ctx.admin_headers)
    assert ok.status_code == 200, ok.text
    assert ok.json()["data"]["status"] == "ok"
    tool = ok.json()["data"]["tools"][0]
    assert tool["name"] == "list_issues"
    assert "input_schema" in tool
    assert tool["disabled"] is False

    # unreachable — probe raises → status unreachable (still 200, with error code).
    async def _probe_fail(**_kwargs: object) -> list[MCPToolDef]:
        raise McpProbeError("MCP_SERVER_PROBE_FAILED", "refused")

    monkeypatch.setattr("control_plane.api.mcp_catalog.probe_remote_mcp", _probe_fail)
    bad = await ctx.client.post(f"/v1/platform/mcp-catalog/{cid}/tools", headers=ctx.admin_headers)
    assert bad.status_code == 200
    assert bad.json()["data"]["status"] == "unreachable"
    assert bad.json()["data"]["error"] == "MCP_SERVER_PROBE_FAILED"

    # oauth2 — not probeable platform-side (per-user token).
    oauth = await ctx.client.post(
        "/v1/platform/mcp-catalog",
        json={
            "name": "linear",
            "display_name": "Linear",
            "transport": "sse",
            "url_template": "https://mcp.example.com/linear",
            "auth_type": "oauth2",
            "oauth_client_id": "cid",
        },
        headers=ctx.admin_headers,
    )
    oid = oauth.json()["data"]["id"]
    na = await ctx.client.post(f"/v1/platform/mcp-catalog/{oid}/tools", headers=ctx.admin_headers)
    assert na.json()["data"]["status"] == "not_probeable"


@pytest.mark.asyncio
async def test_patch_disabled_tools_round_trip(ctx: _Ctx) -> None:
    created = await ctx.client.post(
        "/v1/platform/mcp-catalog",
        json={
            "name": "maps",
            "display_name": "Maps",
            "transport": "sse",
            "url_template": "https://mcp.example.com/maps",
            "auth_type": "none",
        },
        headers=ctx.admin_headers,
    )
    assert created.json()["data"]["disabled_tools"] == []
    cid = created.json()["data"]["id"]

    patch = await ctx.client.patch(
        f"/v1/platform/mcp-catalog/{cid}",
        json={"disabled_tools": ["drive", "transit"]},
        headers=ctx.admin_headers,
    )
    assert patch.status_code == 200, patch.text
    assert patch.json()["data"]["disabled_tools"] == ["drive", "transit"]

    # The tools endpoint flags the disabled ones.
    tools = await ctx.client.post(
        f"/v1/platform/mcp-catalog/{cid}/tools", headers=ctx.admin_headers
    )
    assert tools.json()["data"]["status"] == "ok"

    # Re-enable all by sending an empty list.
    cleared = await ctx.client.patch(
        f"/v1/platform/mcp-catalog/{cid}",
        json={"disabled_tools": []},
        headers=ctx.admin_headers,
    )
    assert cleared.json()["data"]["disabled_tools"] == []


@pytest.mark.asyncio
async def test_list_category_filter(ctx: _Ctx) -> None:
    await ctx.client.post(
        "/v1/platform/mcp-catalog",
        json=_valid_entry("github", category="vcs"),
        headers=ctx.admin_headers,
    )
    await ctx.client.post(
        "/v1/platform/mcp-catalog",
        json=_valid_entry("postgres", category="database"),
        headers=ctx.admin_headers,
    )
    lst = await ctx.client.get(
        "/v1/platform/mcp-catalog", params={"category": "vcs"}, headers=ctx.admin_headers
    )
    assert lst.status_code == 200
    names = {r["name"] for r in lst.json()["data"]}
    assert names == {"github"}


# ---------------------------------------------------------------------------
# Error mappings
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_duplicate_name_409(ctx: _Ctx) -> None:
    first = await ctx.client.post(
        "/v1/platform/mcp-catalog", json=_valid_entry(), headers=ctx.admin_headers
    )
    assert first.status_code == 201
    dup = await ctx.client.post(
        "/v1/platform/mcp-catalog", json=_valid_entry(), headers=ctx.admin_headers
    )
    assert dup.status_code == 409
    assert dup.json()["detail"]["code"] == "CATALOG_DUPLICATE"


@pytest.mark.asyncio
async def test_get_patch_delete_missing_404(ctx: _Ctx) -> None:
    missing = str(uuid4())
    g = await ctx.client.get(f"/v1/platform/mcp-catalog/{missing}", headers=ctx.admin_headers)
    assert g.status_code == 404
    p = await ctx.client.patch(
        f"/v1/platform/mcp-catalog/{missing}",
        json={"display_name": "x"},
        headers=ctx.admin_headers,
    )
    assert p.status_code == 404
    d = await ctx.client.delete(f"/v1/platform/mcp-catalog/{missing}", headers=ctx.admin_headers)
    assert d.status_code == 404


@pytest.mark.asyncio
async def test_create_invalid_body_422(ctx: _Ctx) -> None:
    """bearer auth_type with no secret field in auth_schema → Upsert validator rejects."""
    bad = _valid_entry()
    bad["auth_schema"] = {"fields": []}  # bearer requires exactly one secret field
    resp = await ctx.client.post("/v1/platform/mcp-catalog", json=bad, headers=ctx.admin_headers)
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_patch_violates_merged_invariant_422(ctx: _Ctx) -> None:
    """A bearer entry patched to an auth_schema with no secret field → 422."""
    create = await ctx.client.post(
        "/v1/platform/mcp-catalog", json=_valid_entry(), headers=ctx.admin_headers
    )
    assert create.status_code == 201
    catalog_id = create.json()["data"]["id"]
    resp = await ctx.client.patch(
        f"/v1/platform/mcp-catalog/{catalog_id}",
        json={"auth_schema": {"fields": []}},  # drops the only secret field
        headers=ctx.admin_headers,
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "CATALOG_INVALID"


# ---------------------------------------------------------------------------
# Platform gating
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tenant_admin_forbidden_on_every_endpoint(ctx: _Ctx) -> None:
    h = ctx.tenant_admin_headers
    some_id = str(uuid4())
    # Bind each response before asserting: a request call inside an ``assert``
    # expression is stripped under ``python -O`` (CodeQL py/side-effect-in-assert,
    # [memory:no-side-effect-in-assert]).
    created = await ctx.client.post("/v1/platform/mcp-catalog", json=_valid_entry(), headers=h)
    listed = await ctx.client.get("/v1/platform/mcp-catalog", headers=h)
    got = await ctx.client.get(f"/v1/platform/mcp-catalog/{some_id}", headers=h)
    patched = await ctx.client.patch(
        f"/v1/platform/mcp-catalog/{some_id}", json={"display_name": "x"}, headers=h
    )
    deleted = await ctx.client.delete(f"/v1/platform/mcp-catalog/{some_id}", headers=h)
    assert created.status_code == 403
    assert listed.status_code == 403
    assert got.status_code == 403
    assert patched.status_code == 403
    assert deleted.status_code == 403


# ---------------------------------------------------------------------------
# Delete-in-use → 409 (fake store)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_in_use_409(ctx: _Ctx) -> None:
    create = await ctx.client.post(
        "/v1/platform/mcp-catalog", json=_valid_entry(), headers=ctx.admin_headers
    )
    assert create.status_code == 201
    catalog_id = create.json()["data"]["id"]

    real_store = ctx.app.state.mcp_connector_catalog_store  # type: ignore[attr-defined]

    class _InUseStore:
        def __init__(self, inner: object) -> None:
            self._inner = inner

        async def get_by_id(self, cid: UUID) -> object:
            return await self._inner.get_by_id(cid)

        async def delete(self, cid: UUID) -> None:
            raise McpConnectorCatalogInUseError(catalog_id=cid)

    ctx.app.state.mcp_connector_catalog_store = _InUseStore(real_store)  # type: ignore[attr-defined]
    resp = await ctx.client.delete(
        f"/v1/platform/mcp-catalog/{catalog_id}", headers=ctx.admin_headers
    )
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "CATALOG_IN_USE"


# ---------------------------------------------------------------------------
# Delete guard: user OAuth connections (deletion-hygiene PR2 Task 8)
# ---------------------------------------------------------------------------


async def _seed_oauth_connection(
    ctx: _Ctx, *, catalog_id: UUID, tenant_id: UUID, user_id: str, name: str
) -> tuple[UUID, str, str]:
    """Seed a ``connected`` OAuth connection with real token secrets in the
    app's in-memory stores (bypasses the authorize/callback HTTP flow, which
    this endpoint's delete-guard doesn't need). Returns
    ``(connection_id, access_secret_name, refresh_secret_name)``."""
    oauth_store = ctx.app.state.mcp_oauth_connection_store  # type: ignore[attr-defined]
    secret_store = ctx.app.state.secret_store  # type: ignore[attr-defined]
    conn = await oauth_store.create(
        tenant_id=tenant_id,
        user_id=user_id,
        catalog_id=catalog_id,
        name=name,
        resolved_url="https://mcp.example.com/sse",
    )
    access_name = f"expert-work/oauth/{conn.id}/access"
    refresh_name = f"expert-work/oauth/{conn.id}/refresh"
    await secret_store.put(access_name, "access-token-value")
    await secret_store.put(refresh_name, "refresh-token-value")
    await oauth_store.update(
        connection_id=conn.id,
        tenant_id=tenant_id,
        user_id=user_id,
        patch=McpOAuthConnectionPatch(
            status="connected",
            access_token_ref=f"secret://{access_name}",
            refresh_token_ref=f"secret://{refresh_name}",
        ),
    )
    return conn.id, access_name, refresh_name


@pytest.mark.asyncio
async def test_delete_no_oauth_connections_deletes_normally(ctx: _Ctx) -> None:
    create = await ctx.client.post(
        "/v1/platform/mcp-catalog", json=_valid_entry(), headers=ctx.admin_headers
    )
    catalog_id = create.json()["data"]["id"]
    resp = await ctx.client.delete(
        f"/v1/platform/mcp-catalog/{catalog_id}", headers=ctx.admin_headers
    )
    assert resp.status_code == 204


@pytest.mark.asyncio
async def test_delete_with_oauth_connections_409_without_force(ctx: _Ctx) -> None:
    create = await ctx.client.post(
        "/v1/platform/mcp-catalog", json=_valid_entry(), headers=ctx.admin_headers
    )
    catalog_id = UUID(create.json()["data"]["id"])
    await _seed_oauth_connection(
        ctx, catalog_id=catalog_id, tenant_id=uuid4(), user_id="user-1", name="github"
    )
    await _seed_oauth_connection(
        ctx, catalog_id=catalog_id, tenant_id=uuid4(), user_id="user-2", name="github"
    )

    resp = await ctx.client.delete(
        f"/v1/platform/mcp-catalog/{catalog_id}", headers=ctx.admin_headers
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["code"] == "CATALOG_HAS_OAUTH_CONNECTIONS"
    assert resp.json()["detail"]["count"] == 2

    # Connections + the catalog row both survive the blocked delete.
    oauth_store = ctx.app.state.mcp_oauth_connection_store  # type: ignore[attr-defined]
    assert await oauth_store.count_for_catalog(catalog_id=catalog_id) == 2
    still_there = await ctx.client.get(
        f"/v1/platform/mcp-catalog/{catalog_id}", headers=ctx.admin_headers
    )
    assert still_there.status_code == 200


@pytest.mark.asyncio
async def test_delete_force_cascades_connections_and_secrets(ctx: _Ctx) -> None:
    create = await ctx.client.post(
        "/v1/platform/mcp-catalog", json=_valid_entry(), headers=ctx.admin_headers
    )
    catalog_id = UUID(create.json()["data"]["id"])
    _, access_1, refresh_1 = await _seed_oauth_connection(
        ctx, catalog_id=catalog_id, tenant_id=uuid4(), user_id="user-1", name="github"
    )
    _, access_2, refresh_2 = await _seed_oauth_connection(
        ctx, catalog_id=catalog_id, tenant_id=uuid4(), user_id="user-2", name="github"
    )

    resp = await ctx.client.delete(
        f"/v1/platform/mcp-catalog/{catalog_id}",
        params={"force": "true"},
        headers=ctx.admin_headers,
    )
    assert resp.status_code == 204, resp.text

    oauth_store = ctx.app.state.mcp_oauth_connection_store  # type: ignore[attr-defined]
    secret_store = ctx.app.state.secret_store  # type: ignore[attr-defined]
    assert await oauth_store.count_for_catalog(catalog_id=catalog_id) == 0
    for name in (access_1, refresh_1, access_2, refresh_2):
        with pytest.raises(SecretNotFoundError):
            await secret_store.get(name)

    page = await ctx.audit_store.query(AuditQuery(tenant_id=ctx.admin_tenant))
    delete_entries = [r for r in page.entries if r.action.value == "mcp_catalog:delete"]
    assert len(delete_entries) == 1
    details = delete_entries[0].details
    assert details["connections_removed"] == 2
    assert details["secrets_removed"] == 4
    assert details["secrets_failed"] == 0


class _DeleteAlwaysFailsSecretStore:
    """Wraps a real ``SecretStore`` but makes ``delete`` always raise.

    Mirrors ``test_mcp_servers_api.py``'s ``_DeleteAlwaysFailsSecretStore``
    (M-3, Task 8 review) — proves the catalog force cascade's best-effort
    secret cleanup never lets a secret-store failure escape as a 500: the
    delete must still 204, the connection rows must still be removed, and the
    audit ``secrets_failed`` count must reflect every failed delete while
    ``secrets_removed`` stays 0.
    """

    def __init__(self, inner: object) -> None:
        self._inner = inner

    async def get(self, name: str, *, version: str | None = None) -> str:
        return await self._inner.get(name, version=version)  # type: ignore[attr-defined]

    async def put(self, name: str, value: str) -> None:
        await self._inner.put(name, value)  # type: ignore[attr-defined]

    async def delete(self, name: str) -> None:
        raise RuntimeError("boom: secret store unavailable")


@pytest.mark.asyncio
async def test_delete_force_secrets_failed_branch_does_not_abort(ctx: _Ctx) -> None:
    create = await ctx.client.post(
        "/v1/platform/mcp-catalog", json=_valid_entry(), headers=ctx.admin_headers
    )
    catalog_id = UUID(create.json()["data"]["id"])
    await _seed_oauth_connection(
        ctx, catalog_id=catalog_id, tenant_id=uuid4(), user_id="user-1", name="github"
    )
    await _seed_oauth_connection(
        ctx, catalog_id=catalog_id, tenant_id=uuid4(), user_id="user-2", name="github"
    )

    ctx.app.state.secret_store = _DeleteAlwaysFailsSecretStore(  # type: ignore[attr-defined]
        ctx.app.state.secret_store  # type: ignore[attr-defined]
    )

    resp = await ctx.client.delete(
        f"/v1/platform/mcp-catalog/{catalog_id}",
        params={"force": "true"},
        headers=ctx.admin_headers,
    )
    assert resp.status_code == 204, resp.text

    oauth_store = ctx.app.state.mcp_oauth_connection_store  # type: ignore[attr-defined]
    assert await oauth_store.count_for_catalog(catalog_id=catalog_id) == 0

    page = await ctx.audit_store.query(AuditQuery(tenant_id=ctx.admin_tenant))
    delete_entries = [r for r in page.entries if r.action.value == "mcp_catalog:delete"]
    assert len(delete_entries) == 1
    details = delete_entries[0].details
    assert details["connections_removed"] == 2
    assert details["secrets_removed"] == 0
    assert details["secrets_failed"] == 4


@pytest.mark.asyncio
async def test_delete_tenant_mcp_server_in_use_blocks_even_with_force(ctx: _Ctx) -> None:
    """``?force=true`` only cascades OAuth connections — it never bypasses the
    tenant_mcp_server RESTRICT guard; an instantiated instance must still be
    removed first."""
    create = await ctx.client.post(
        "/v1/platform/mcp-catalog", json=_valid_entry(), headers=ctx.admin_headers
    )
    assert create.status_code == 201
    catalog_id = create.json()["data"]["id"]

    real_store = ctx.app.state.mcp_connector_catalog_store  # type: ignore[attr-defined]

    class _InUseStore:
        def __init__(self, inner: object) -> None:
            self._inner = inner

        async def get_by_id(self, cid: UUID) -> object:
            return await self._inner.get_by_id(cid)

        async def delete(self, cid: UUID) -> None:
            raise McpConnectorCatalogInUseError(catalog_id=cid)

    ctx.app.state.mcp_connector_catalog_store = _InUseStore(real_store)  # type: ignore[attr-defined]
    resp = await ctx.client.delete(
        f"/v1/platform/mcp-catalog/{catalog_id}",
        params={"force": "true"},
        headers=ctx.admin_headers,
    )
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "CATALOG_IN_USE"


@pytest.mark.asyncio
async def test_delete_force_overlapping_instantiation_and_oauth_leaves_both_intact(
    ctx: _Ctx,
) -> None:
    """T8 review I-1: an overlapping state — the catalog entry is BOTH
    instantiated into ``tenant_mcp_server`` AND has a live OAuth connection —
    must block on the ``tenant_mcp_server`` in-use check BEFORE the ``force``
    cascade ever touches the OAuth connection. Before the fix, the force
    cascade ran first and destroyed the real connection + its secrets, only
    for ``store.delete`` to then raise ``McpConnectorCatalogInUseError`` with
    zero audit trail — a silent data-destroying 409."""
    create = await ctx.client.post(
        "/v1/platform/mcp-catalog", json=_valid_entry(), headers=ctx.admin_headers
    )
    assert create.status_code == 201
    catalog_id = UUID(create.json()["data"]["id"])

    # A tenant genuinely instantiated this platform connector.
    tenant_mcp_server_store = ctx.app.state.tenant_mcp_server_store  # type: ignore[attr-defined]
    await tenant_mcp_server_store.create(
        tenant_id=uuid4(),
        name="github",
        transport="streamable_http",
        url="https://mcp.github.com/mcp",
        auth_type="none",
        token_secret_ref=None,
        timeout_s=30.0,
        created_by="admin@acme",
        catalog_id=catalog_id,
    )

    # A different tenant/user holds a live OAuth connection + real secrets.
    _, access_name, refresh_name = await _seed_oauth_connection(
        ctx, catalog_id=catalog_id, tenant_id=uuid4(), user_id="user-1", name="github"
    )

    resp = await ctx.client.delete(
        f"/v1/platform/mcp-catalog/{catalog_id}",
        params={"force": "true"},
        headers=ctx.admin_headers,
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["code"] == "CATALOG_IN_USE"

    # The OAuth connection row and its secrets must survive — force never got
    # a chance to touch them.
    oauth_store = ctx.app.state.mcp_oauth_connection_store  # type: ignore[attr-defined]
    assert await oauth_store.count_for_catalog(catalog_id=catalog_id) == 1
    secret_store = ctx.app.state.secret_store  # type: ignore[attr-defined]
    assert await secret_store.get(access_name) == "access-token-value"
    assert await secret_store.get(refresh_name) == "refresh-token-value"

    # No delete audit row was emitted for this blocked attempt.
    page = await ctx.audit_store.query(AuditQuery(tenant_id=ctx.admin_tenant))
    delete_entries = [r for r in page.entries if r.action.value == "mcp_catalog:delete"]
    assert delete_entries == []


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_and_delete_emit_audit(ctx: _Ctx) -> None:
    create = await ctx.client.post(
        "/v1/platform/mcp-catalog", json=_valid_entry(), headers=ctx.admin_headers
    )
    assert create.status_code == 201
    catalog_id = create.json()["data"]["id"]
    delete = await ctx.client.delete(
        f"/v1/platform/mcp-catalog/{catalog_id}", headers=ctx.admin_headers
    )
    assert delete.status_code == 204

    page = await ctx.audit_store.query(AuditQuery(tenant_id=ctx.admin_tenant))
    actions = {r.action.value for r in page.entries}
    assert "mcp_catalog:create" in actions
    assert "mcp_catalog:delete" in actions
    # No secret value ever lands in audit details.
    for r in page.entries:
        assert "ghp_" not in str(r.details)


# ---------------------------------------------------------------------------
# Platform shared-bearer server (P1 — mcp-platform-servers)
# ---------------------------------------------------------------------------


def _platform_bearer_entry(name: str = "sharedbearer") -> dict[str, object]:
    """A platform-configured shared-bearer (A) server — platform supplies the
    token (no tenant-filled auth_schema secret field)."""
    return {
        "name": name,
        "display_name": "Shared Bearer",
        "category": "tools",
        "transport": "streamable_http",
        "url_template": "https://mcp.example.com/mcp",
        "auth_type": "bearer",
        "bearer_token": "platform-secret-token",
        "required_tier": "free",
    }


@pytest.mark.asyncio
async def test_create_platform_bearer_stores_token_as_ref(ctx: _Ctx) -> None:
    resp = await ctx.client.post(
        "/v1/platform/mcp-catalog", json=_platform_bearer_entry(), headers=ctx.admin_headers
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()["data"]
    # The response exposes a flag, never the ref or the plaintext.
    assert data["has_bearer_token"] is True
    assert "bearer_token_ref" not in data
    assert "bearer_token" not in data
    # The token landed in the SecretStore under the platform path; only the ref
    # is persisted on the row.
    stored = await ctx.app.state.secret_store.get(  # type: ignore[attr-defined]
        "expert-work/platform/mcp/sharedbearer/token"
    )
    assert stored == "platform-secret-token"
    # A subsequent GET still reports the flag (ref persisted).
    cid = data["id"]
    got = await ctx.client.get(f"/v1/platform/mcp-catalog/{cid}", headers=ctx.admin_headers)
    assert got.json()["data"]["has_bearer_token"] is True


@pytest.mark.asyncio
async def test_repaste_platform_bearer_via_patch(ctx: _Ctx) -> None:
    create = await ctx.client.post(
        "/v1/platform/mcp-catalog", json=_platform_bearer_entry(), headers=ctx.admin_headers
    )
    cid = create.json()["data"]["id"]
    patch = await ctx.client.patch(
        f"/v1/platform/mcp-catalog/{cid}",
        json={"bearer_token": "rotated-token"},
        headers=ctx.admin_headers,
    )
    assert patch.status_code == 200, patch.text
    assert patch.json()["data"]["has_bearer_token"] is True
    stored = await ctx.app.state.secret_store.get(  # type: ignore[attr-defined]
        "expert-work/platform/mcp/sharedbearer/token"
    )
    assert stored == "rotated-token"


@pytest.mark.asyncio
async def test_bearer_token_plus_secret_field_rejected(ctx: _Ctx) -> None:
    bad = _platform_bearer_entry()
    bad["auth_schema"] = {
        "fields": [{"key": "t", "label": "T", "kind": "secret", "required": True}]
    }
    resp = await ctx.client.post("/v1/platform/mcp-catalog", json=bad, headers=ctx.admin_headers)
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_none_with_bearer_token_rejected(ctx: _Ctx) -> None:
    bad = _platform_bearer_entry()
    bad["auth_type"] = "none"
    resp = await ctx.client.post("/v1/platform/mcp-catalog", json=bad, headers=ctx.admin_headers)
    assert resp.status_code == 422, resp.text


# ---------------------------------------------------------------------------
# Stream MCP platform-servers (P1b) — catalog mutations invalidate the shared
# pool + every cached agent (the pool feeds all tenants' builds).
# ---------------------------------------------------------------------------


class _SpyPoolService:
    def __init__(self) -> None:
        self.invalidated = 0

    async def invalidate(self) -> None:
        self.invalidated += 1


class _SpyRuntime:
    def __init__(self) -> None:
        self.invalidated_all = 0

    def invalidate_all(self) -> None:
        self.invalidated_all += 1


@pytest.mark.asyncio
async def test_create_patch_delete_invalidate_platform_pool(ctx: _Ctx) -> None:
    pool = _SpyPoolService()
    runtime = _SpyRuntime()
    ctx.app.state.platform_mcp_pool_service = pool  # type: ignore[attr-defined]
    ctx.app.state.agent_runtime = runtime  # type: ignore[attr-defined]

    create = await ctx.client.post(
        "/v1/platform/mcp-catalog", json=_valid_entry(), headers=ctx.admin_headers
    )
    assert create.status_code == 201, create.text
    assert (pool.invalidated, runtime.invalidated_all) == (1, 1)

    cid = create.json()["data"]["id"]
    patch = await ctx.client.patch(
        f"/v1/platform/mcp-catalog/{cid}",
        json={"display_name": "GitHub 2"},
        headers=ctx.admin_headers,
    )
    assert patch.status_code == 200, patch.text
    assert (pool.invalidated, runtime.invalidated_all) == (2, 2)

    delete = await ctx.client.delete(f"/v1/platform/mcp-catalog/{cid}", headers=ctx.admin_headers)
    assert delete.status_code == 204, delete.text
    assert (pool.invalidated, runtime.invalidated_all) == (3, 3)


# ---------------------------------------------------------------------------
# B-248 — switching an oauth2 entry off (disable / force-delete) or back on
# reaches the user OAuth pools as ONE event per connector; a save that does
# not change ``enabled`` (the admin form always sends it) reaches nothing.
# ---------------------------------------------------------------------------


class _SpyUserPoolService:
    def __init__(self) -> None:
        self.switched: list[tuple[str, str]] = []

    async def connector_off(self, name: str) -> None:
        self.switched.append(("off", name))

    async def connector_on(self, name: str) -> None:
        self.switched.append(("on", name))


class _SpyBus:
    def __init__(self) -> None:
        self.events: list[tuple[str, str | None]] = []

    async def publish(self, event: object) -> None:
        self.events.append((event.kind, event.connector))  # type: ignore[attr-defined]


async def _oauth_entry_with_users(ctx: _Ctx) -> tuple[str, _SpyUserPoolService, _SpyBus]:
    """An oauth2 entry two users connected to."""
    resp = await ctx.client.post(
        "/v1/platform/mcp-catalog",
        json={
            "name": "linear",
            "display_name": "Linear",
            "transport": "sse",
            "url_template": "https://mcp.example.com/linear",
            "auth_type": "oauth2",
            "oauth_client_id": "cid",
        },
        headers=ctx.admin_headers,
    )
    assert resp.status_code == 201, resp.text
    catalog_id = UUID(resp.json()["data"]["id"])
    for user_id in ("user-1", "user-2"):
        await _seed_oauth_connection(
            ctx, catalog_id=catalog_id, tenant_id=uuid4(), user_id=user_id, name="linear"
        )
    pools, bus = _SpyUserPoolService(), _SpyBus()
    ctx.app.state.user_mcp_oauth_pool_service = pools  # type: ignore[attr-defined]
    ctx.app.state.invalidation_bus = bus  # type: ignore[attr-defined]
    return str(catalog_id), pools, bus


def _form_body(enabled: bool) -> dict[str, object]:
    """The body the admin edit form sends on every save (CatalogConfigForm)."""
    return {
        "display_name": "Linear 2",
        "description": "",
        "category": "",
        "icon": "",
        "url_template": "https://mcp.example.com/linear",
        "required_tier": "free",
        "enabled": enabled,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body", [{"enabled": True}, _form_body(True), {"display_name": "Linear"}], ids=str
)
async def test_save_without_switching_oauth2_entry_leaves_user_pools(
    ctx: _Ctx, body: dict[str, object]
) -> None:
    cid, pools, bus = await _oauth_entry_with_users(ctx)

    resp = await ctx.client.patch(
        f"/v1/platform/mcp-catalog/{cid}", json=body, headers=ctx.admin_headers
    )

    assert resp.status_code == 200, resp.text
    assert pools.switched == []
    assert bus.events == [("platform_mcp", None)]


@pytest.mark.asyncio
async def test_disable_then_enable_oauth2_entry_switches_it_once_each(ctx: _Ctx) -> None:
    cid, pools, bus = await _oauth_entry_with_users(ctx)

    off = await ctx.client.patch(
        f"/v1/platform/mcp-catalog/{cid}", json=_form_body(False), headers=ctx.admin_headers
    )
    again = await ctx.client.patch(
        f"/v1/platform/mcp-catalog/{cid}", json=_form_body(False), headers=ctx.admin_headers
    )
    on = await ctx.client.patch(
        f"/v1/platform/mcp-catalog/{cid}", json={"enabled": True}, headers=ctx.admin_headers
    )

    assert (off.status_code, again.status_code, on.status_code) == (200, 200, 200)
    assert pools.switched == [("off", "linear"), ("on", "linear")]
    assert bus.events == [
        ("user_mcp_oauth_connector_off", "linear"),
        ("platform_mcp", None),
        ("platform_mcp", None),
        ("user_mcp_oauth_connector_on", "linear"),
        ("platform_mcp", None),
    ]


@pytest.mark.asyncio
async def test_force_delete_oauth2_entry_switches_it_off(ctx: _Ctx) -> None:
    cid, pools, bus = await _oauth_entry_with_users(ctx)

    resp = await ctx.client.delete(
        f"/v1/platform/mcp-catalog/{cid}", params={"force": "true"}, headers=ctx.admin_headers
    )

    assert resp.status_code == 204, resp.text
    assert pools.switched == [("off", "linear")]
    assert bus.events == [("user_mcp_oauth_connector_off", "linear"), ("platform_mcp", None)]
