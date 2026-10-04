"""API tests for /v1/mcp-oauth initiate + callback — Stream MCP-OAUTH (OA-3a).

The OAuth engine (discover/exchange) is monkeypatched — no real network.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from control_plane.app import create_app
from control_plane.mcp_oauth import OAuthServerMetadata, TokenResponse
from control_plane.settings import Settings
from control_plane.tenant_scope import bypass_rls_session
from expert_work.common.lifecycle import Lifecycle
from expert_work.protocol import (
    AuditQuery,
    McpConnectorAuthSchema,
    McpConnectorCatalogUpsert,
    TenantConfigPatch,
)
from expert_work.runtime.secret_store import SecretNotFoundError
from tests.auth_fixtures import (
    TEST_AUDIENCE,
    TEST_ISSUER,
    build_test_jwt_verifier,
    make_test_jwt,
)

_REDIRECT = "https://app.test/v1/mcp-oauth/callback"
_META = OAuthServerMetadata(
    authorization_endpoint="https://auth.linear.test/authorize",
    token_endpoint="https://auth.linear.test/token",
    resource="https://mcp.linear.app/sse",
    scopes_supported=("read", "write"),
)


def _settings(
    *, redirect_uri: str | None = _REDIRECT, allowlist: list[str] | None = None
) -> Settings:
    return Settings(
        service_name="control_plane_test",
        env="dev",
        auth_mode="dev",
        db_dsn="postgresql+asyncpg://test@localhost/test",
        rate_limit_burst=10_000,
        rate_limit_per_second=10_000.0,
        oidc_issuer=TEST_ISSUER,
        oidc_audience=[TEST_AUDIENCE],
        mcp_oauth_redirect_uri=redirect_uri,
        mcp_oauth_redirect_allowlist=allowlist or [],
    )


async def _make_app(
    *, redirect_uri: str | None = _REDIRECT, allowlist: list[str] | None = None
) -> tuple[object, dict[str, str], UUID, str]:
    lifecycle = Lifecycle()
    lifecycle.mark_ready()
    app = create_app(
        settings=_settings(redirect_uri=redirect_uri, allowlist=allowlist),
        lifecycle=lifecycle,
        jwt_verifier=build_test_jwt_verifier(),
    )
    tenant_id = uuid4()
    user_id = str(uuid4())
    token = make_test_jwt(tenant_id=tenant_id, subject=user_id, roles=("admin",))
    return app, {"Authorization": f"Bearer {token}"}, tenant_id, user_id


async def _enable_for_tenant(app: object, tenant_id: UUID, name: str) -> None:
    """Opt the tenant into a platform connector (P2 gate) so users may authorize."""
    await app.state.tenant_config_service.upsert(  # type: ignore[attr-defined]
        tenant_id=tenant_id,
        patch=TenantConfigPatch(display_name="Test Tenant", mcp_allowlist=[name]),
        actor_id="seed",
    )


async def _seed_oauth2_entry(
    app: object, *, name: str = "linear", enable_for: UUID | None = None
) -> UUID:
    upsert = McpConnectorCatalogUpsert(
        name=name,
        display_name="Linear",
        transport="sse",
        url_template="https://mcp.linear.app/sse",
        auth_type="oauth2",
        auth_schema=McpConnectorAuthSchema(),
        oauth_client_id="expert-work-linear-app",
        oauth_scopes="read",
    )
    async with bypass_rls_session():
        rec = await app.state.mcp_connector_catalog_store.create(  # type: ignore[attr-defined]
            upsert=upsert, actor_id="seed"
        )
    if enable_for is not None:
        await _enable_for_tenant(app, enable_for, name)
    return rec.id


async def _seed_none_entry(app: object, *, name: str = "plain") -> UUID:
    upsert = McpConnectorCatalogUpsert(
        name=name,
        display_name="Plain",
        transport="sse",
        url_template="https://mcp.plain.test/sse",
        auth_type="none",
    )
    async with bypass_rls_session():
        rec = await app.state.mcp_connector_catalog_store.create(  # type: ignore[attr-defined]
            upsert=upsert, actor_id="seed"
        )
    return rec.id


async def _fake_discover(**_kwargs: object) -> OAuthServerMetadata:
    return _META


async def _fake_exchange(**_kwargs: object) -> TokenResponse:
    return TokenResponse(access_token="AT", refresh_token="RT", expires_in=3600, scope="read")


@pytest.mark.asyncio
async def test_initiate_returns_authorize_url(monkeypatch: pytest.MonkeyPatch) -> None:
    app, headers, tenant_id, user_id = await _make_app()
    monkeypatch.setattr("control_plane.api.mcp_oauth_api.discover_oauth_metadata", _fake_discover)
    cat_id = await _seed_oauth2_entry(app, enable_for=tenant_id)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://cp.test") as client:
        resp = await client.post(
            f"/v1/mcp-servers/catalog/{cat_id}/oauth/initiate", headers=headers
        )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "pending"
    assert body["authorize_url"].startswith("https://auth.linear.test/authorize?")
    assert "client_id=expert-work-linear-app" in body["authorize_url"]
    # A pending connection now exists for this user+connector.
    conn = await app.state.mcp_oauth_connection_store.get_for_connector(  # type: ignore[attr-defined]
        tenant_id=tenant_id, user_id=user_id, catalog_id=cat_id
    )
    assert conn is not None and conn.status == "pending" and conn.pkce_verifier


@pytest.mark.asyncio
async def test_full_oauth_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    app, headers, tenant_id, user_id = await _make_app()
    monkeypatch.setattr("control_plane.api.mcp_oauth_api.discover_oauth_metadata", _fake_discover)
    monkeypatch.setattr("control_plane.api.mcp_oauth_api.exchange_code", _fake_exchange)
    cat_id = await _seed_oauth2_entry(app, enable_for=tenant_id)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://cp.test") as client:
        init = await client.post(
            f"/v1/mcp-servers/catalog/{cat_id}/oauth/initiate", headers=headers
        )
        assert init.status_code == 201
        conn = await app.state.mcp_oauth_connection_store.get_for_connector(  # type: ignore[attr-defined]
            tenant_id=tenant_id, user_id=user_id, catalog_id=cat_id
        )
        state = conn.oauth_state
        cb = await client.get(
            "/v1/mcp-oauth/callback", params={"state": state, "code": "authcode"}, headers=headers
        )
    assert cb.status_code == 200, cb.text
    assert cb.json()["status"] == "connected"
    # Token persisted to the secret store; connection marked connected + flow cleared.
    final = await app.state.mcp_oauth_connection_store.get_for_connector(  # type: ignore[attr-defined]
        tenant_id=tenant_id, user_id=user_id, catalog_id=cat_id
    )
    assert final.status == "connected"
    assert final.access_token_ref is not None
    assert final.oauth_state is None and final.pkce_verifier is None
    stored = await app.state.secret_store.get(  # type: ignore[attr-defined]
        final.access_token_ref.removeprefix("secret://")
    )
    assert stored == "AT"
    # Audit resource_type is the actual resource (mcp_oauth_connection), not
    # the tenant_mcp_server mislabel.
    page = await app.state.audit_logger.query(  # type: ignore[attr-defined]
        AuditQuery(tenant_id=tenant_id, limit=1000), actor_id="test-harness"
    )
    callback_entries = [e for e in page.entries if e.details.get("source") == "oauth_callback"]
    assert len(callback_entries) == 1
    assert callback_entries[0].resource_type == "mcp_oauth_connection"


@pytest.mark.asyncio
async def test_callback_bad_state_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    app, headers, tenant_id, _ = await _make_app()
    monkeypatch.setattr("control_plane.api.mcp_oauth_api.discover_oauth_metadata", _fake_discover)
    cat_id = await _seed_oauth2_entry(app, enable_for=tenant_id)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://cp.test") as client:
        await client.post(f"/v1/mcp-servers/catalog/{cat_id}/oauth/initiate", headers=headers)
        cb = await client.get(
            "/v1/mcp-oauth/callback", params={"state": "wrong", "code": "x"}, headers=headers
        )
    assert cb.status_code == 400
    assert cb.json()["detail"]["code"] == "MCP_OAUTH_STATE_INVALID"


@pytest.mark.asyncio
async def test_initiate_non_oauth2_entry_rejected() -> None:
    app, headers, _, _ = await _make_app()
    cat_id = await _seed_none_entry(app)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://cp.test") as client:
        resp = await client.post(
            f"/v1/mcp-servers/catalog/{cat_id}/oauth/initiate", headers=headers
        )
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "MCP_CATALOG_NOT_OAUTH"


@pytest.mark.asyncio
async def test_initiate_not_configured_returns_503() -> None:
    app, headers, tenant_id, _ = await _make_app(redirect_uri=None)
    cat_id = await _seed_oauth2_entry(app, enable_for=tenant_id)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://cp.test") as client:
        resp = await client.post(
            f"/v1/mcp-servers/catalog/{cat_id}/oauth/initiate", headers=headers
        )
    assert resp.status_code == 503
    assert resp.json()["detail"]["code"] == "MCP_OAUTH_NOT_CONFIGURED"


# --- OA-4: list + disconnect ----------------------------------------------


async def _connect(client: AsyncClient, cat_id: UUID, headers: dict[str, str]) -> str:
    init = await client.post(f"/v1/mcp-servers/catalog/{cat_id}/oauth/initiate", headers=headers)
    assert init.status_code == 201
    return init.json()["connection_id"]


@pytest.mark.asyncio
async def test_list_connections_excludes_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    app, headers, tenant_id, user_id = await _make_app()
    monkeypatch.setattr("control_plane.api.mcp_oauth_api.discover_oauth_metadata", _fake_discover)
    monkeypatch.setattr("control_plane.api.mcp_oauth_api.exchange_code", _fake_exchange)
    cat_id = await _seed_oauth2_entry(app, enable_for=tenant_id)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://cp.test") as client:
        await _connect(client, cat_id, headers)
        conn = await app.state.mcp_oauth_connection_store.get_for_connector(  # type: ignore[attr-defined]
            tenant_id=tenant_id, user_id=user_id, catalog_id=cat_id
        )
        cb = await client.get(
            "/v1/mcp-oauth/callback",
            params={"state": conn.oauth_state, "code": "c"},
            headers=headers,
        )
        assert cb.status_code == 200
        listed = await client.get("/v1/mcp-oauth/connections", headers=headers)
    assert listed.status_code == 200
    items = listed.json()["items"]
    assert len(items) == 1
    item = items[0]
    assert item["status"] == "connected"
    assert item["name"] == "linear"
    # Token refs + flow secrets must never be exposed.
    for leaked in ("access_token_ref", "refresh_token_ref", "oauth_state", "pkce_verifier"):
        assert leaked not in item


@pytest.mark.asyncio
async def test_disconnect_revokes_and_removes(monkeypatch: pytest.MonkeyPatch) -> None:
    app, headers, tenant_id, user_id = await _make_app()
    monkeypatch.setattr("control_plane.api.mcp_oauth_api.discover_oauth_metadata", _fake_discover)
    monkeypatch.setattr("control_plane.api.mcp_oauth_api.exchange_code", _fake_exchange)
    cat_id = await _seed_oauth2_entry(app, enable_for=tenant_id)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://cp.test") as client:
        cid = await _connect(client, cat_id, headers)
        conn = await app.state.mcp_oauth_connection_store.get_for_connector(  # type: ignore[attr-defined]
            tenant_id=tenant_id, user_id=user_id, catalog_id=cat_id
        )
        await client.get(
            "/v1/mcp-oauth/callback",
            params={"state": conn.oauth_state, "code": "c"},
            headers=headers,
        )
        access_ref = (
            await app.state.mcp_oauth_connection_store.get(  # type: ignore[attr-defined]
                connection_id=conn.id, tenant_id=tenant_id, user_id=user_id
            )
        ).access_token_ref
        dele = await client.delete(f"/v1/mcp-oauth/connections/{cid}", headers=headers)
    assert dele.status_code == 204
    assert dele.content == b""  # 204 carries no body (uvicorn rejects one)
    # Row gone.
    gone = await app.state.mcp_oauth_connection_store.get_for_connector(  # type: ignore[attr-defined]
        tenant_id=tenant_id, user_id=user_id, catalog_id=cat_id
    )
    assert gone is None
    # Token secret is truly deleted (not overwritten with an empty string).
    assert access_ref is not None
    with pytest.raises(SecretNotFoundError):
        await app.state.secret_store.get(access_ref.removeprefix("secret://"))  # type: ignore[attr-defined]
    # Audit resource_type is the actual resource (mcp_oauth_connection), not
    # the tenant_mcp_server mislabel.
    page = await app.state.audit_logger.query(  # type: ignore[attr-defined]
        AuditQuery(tenant_id=tenant_id, limit=1000), actor_id="test-harness"
    )
    disconnect_entries = [e for e in page.entries if e.details.get("source") == "oauth_disconnect"]
    assert len(disconnect_entries) == 1
    assert disconnect_entries[0].resource_type == "mcp_oauth_connection"


@pytest.mark.asyncio
async def test_disconnect_unknown_returns_404() -> None:
    app, headers, _, _ = await _make_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://cp.test") as client:
        resp = await client.delete(f"/v1/mcp-oauth/connections/{uuid4()}", headers=headers)
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "MCP_OAUTH_CONNECTION_NOT_FOUND"


@pytest.mark.asyncio
async def test_operator_can_initiate_own_oauth(monkeypatch: pytest.MonkeyPatch) -> None:
    """The per-user scenario: a regular employee (operator role, not admin)
    must be able to authorize their own OAuth connection. Guards the new
    ``mcp_oauth`` RBAC resource (operator gets read/write/delete on own)."""
    app, _admin_headers, tenant_id, _ = await _make_app()
    monkeypatch.setattr("control_plane.api.mcp_oauth_api.discover_oauth_metadata", _fake_discover)
    cat_id = await _seed_oauth2_entry(app, enable_for=tenant_id)
    # An operator-role JWT (the regular logged-in employee), NOT admin.
    op_token = make_test_jwt(tenant_id=tenant_id, subject=str(uuid4()), roles=("operator",))
    op_headers = {"Authorization": f"Bearer {op_token}"}
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://cp.test") as client:
        resp = await client.post(
            f"/v1/mcp-servers/catalog/{cat_id}/oauth/initiate", headers=op_headers
        )
        assert resp.status_code == 201, resp.text
        # And can list their own connections.
        lst = await client.get("/v1/mcp-oauth/connections", headers=op_headers)
        assert lst.status_code == 200
        assert len(lst.json()["items"]) == 1


@pytest.mark.asyncio
async def test_initiate_with_client_redirect_stores_and_uses_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Multi-client OAuth: a client-supplied (allowlisted) redirect_uri is used
    in the authorize URL and persisted on the connection for the callback."""
    client_redirect = "https://client.app/oauth/cb"
    app, headers, tenant_id, user_id = await _make_app(allowlist=[client_redirect])
    monkeypatch.setattr("control_plane.api.mcp_oauth_api.discover_oauth_metadata", _fake_discover)
    cat_id = await _seed_oauth2_entry(app, enable_for=tenant_id)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://cp.test") as client:
        resp = await client.post(
            f"/v1/mcp-servers/catalog/{cat_id}/oauth/initiate",
            headers=headers,
            json={"redirect_uri": client_redirect},
        )
    assert resp.status_code == 201, resp.text
    from urllib.parse import quote

    assert quote(client_redirect, safe="") in resp.json()["authorize_url"]
    conn = await app.state.mcp_oauth_connection_store.get_for_connector(  # type: ignore[attr-defined]
        tenant_id=tenant_id, user_id=user_id, catalog_id=cat_id
    )
    assert conn is not None and conn.redirect_uri == client_redirect


@pytest.mark.asyncio
async def test_initiate_rejects_non_allowlisted_redirect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A client redirect not on the allowlist → 422 (open-redirect guard)."""
    app, headers, tenant_id, _ = await _make_app(allowlist=["https://client.app/cb"])
    monkeypatch.setattr("control_plane.api.mcp_oauth_api.discover_oauth_metadata", _fake_discover)
    cat_id = await _seed_oauth2_entry(app, enable_for=tenant_id)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://cp.test") as client:
        resp = await client.post(
            f"/v1/mcp-servers/catalog/{cat_id}/oauth/initiate",
            headers=headers,
            json={"redirect_uri": "https://evil.test/steal"},
        )
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "MCP_OAUTH_REDIRECT_NOT_ALLOWED"


@pytest.mark.asyncio
async def test_initiate_loopback_redirect_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """RFC 8252 native client: a loopback redirect is accepted (any port)."""
    app, headers, tenant_id, _ = await _make_app(allowlist=[])
    monkeypatch.setattr("control_plane.api.mcp_oauth_api.discover_oauth_metadata", _fake_discover)
    cat_id = await _seed_oauth2_entry(app, enable_for=tenant_id)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://cp.test") as client:
        resp = await client.post(
            f"/v1/mcp-servers/catalog/{cat_id}/oauth/initiate",
            headers=headers,
            json={"redirect_uri": "http://127.0.0.1:53122/cb"},
        )
    assert resp.status_code == 201, resp.text


@pytest.mark.asyncio
async def test_initiate_rejects_when_tenant_not_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Opt-in gate (P2): without the tenant enabling the connector, a user
    cannot authorize it — 403 MCP_CATALOG_NOT_ENABLED."""
    app, headers, _tenant_id, _ = await _make_app()
    monkeypatch.setattr("control_plane.api.mcp_oauth_api.discover_oauth_metadata", _fake_discover)
    cat_id = await _seed_oauth2_entry(app)  # NOT enabled for the tenant
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://cp.test") as client:
        resp = await client.post(
            f"/v1/mcp-servers/catalog/{cat_id}/oauth/initiate", headers=headers
        )
    assert resp.status_code == 403
    assert resp.json()["detail"]["code"] == "MCP_CATALOG_NOT_ENABLED"
