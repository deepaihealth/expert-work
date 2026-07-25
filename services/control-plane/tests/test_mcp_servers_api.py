"""API tests for /v1/mcp-servers — Stream V-C.

Tests probe→persist→encrypt, probe-fail→422, SSRF→422, non-admin→403,
duplicate-name→409, and delete-unreferenced→200.

Fixture helpers mirror the pattern from test_platform_config_api.py and
test_members_api.py: create_app with test settings + jwt_verifier, then embed
role in the JWT claim (no role-binding seeding needed for tenant-scope roles).
"""

from __future__ import annotations

import hashlib
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from control_plane.app import create_app
from control_plane.mcp_probe import McpProbeError
from control_plane.settings import Settings
from control_plane.tenant_scope import bypass_rls_session
from expert_work.common.lifecycle import Lifecycle
from expert_work.protocol import (
    AgentSpec,
    AgentSpecStatus,
    McpConnectorCatalogUpsert,
    TenantConfigPatch,
)
from expert_work.runtime.secret_store import SecretNotFoundError, parse_secret_ref
from orchestrator.tools.mcp import MCPToolDef
from tests.auth_fixtures import (
    TEST_AUDIENCE,
    TEST_ISSUER,
    build_test_jwt_verifier,
    make_test_jwt,
)

# ---------------------------------------------------------------------------
# Fake probe callables — injected via monkeypatch
# ---------------------------------------------------------------------------


async def _fake_probe_ok(**kwargs: object) -> list[MCPToolDef]:
    return [MCPToolDef(name="create_issue", description="", input_schema={})]


async def _fake_probe_fail(**kwargs: object) -> list[MCPToolDef]:
    raise McpProbeError("MCP_SERVER_PROBE_FAILED", "connection refused")


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


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


async def _make_app_with_admin() -> tuple[object, dict[str, str], UUID]:
    """Build an in-memory control-plane app and return (app, admin_headers, tenant_id).

    No role-binding seeding is required: the JWT ``roles`` claim carries
    ``("admin",)`` which the RBAC layer reads directly (same pattern as
    test_members_api.py / test_agents_api.py).
    """
    lifecycle = Lifecycle()
    lifecycle.mark_ready()
    settings = _build_settings()
    jwt_verifier = build_test_jwt_verifier()
    app = create_app(settings=settings, lifecycle=lifecycle, jwt_verifier=jwt_verifier)
    tenant_id = uuid4()
    token = make_test_jwt(tenant_id=tenant_id, subject=str(uuid4()), roles=("admin",))
    admin_headers = {"Authorization": f"Bearer {token}"}
    return app, admin_headers, tenant_id


async def _seed_viewer_headers(app: object, tenant_id: UUID) -> dict[str, str]:
    """Return headers for a viewer-role principal on the same tenant.

    We accept the tenant_id explicitly so the viewer is scoped to the same
    tenant as the admin created by _make_app_with_admin.
    """
    token = make_test_jwt(tenant_id=tenant_id, subject=str(uuid4()), roles=("viewer",))
    return {"Authorization": f"Bearer {token}"}


def _agent_spec(name: str, tools: list[dict[str, object]]) -> AgentSpec:
    """Minimal valid AgentSpec with the given ``spec.tools`` entries."""
    return AgentSpec.model_validate(
        {
            "apiVersion": "expert_work.io/v1",
            "kind": "Agent",
            "metadata": {"name": name, "version": "1.0.0", "tenant": "test-tenant"},
            "spec": {
                "tenant_config": {},
                "model": {
                    "provider": "anthropic",
                    "name": "claude-sonnet-4-6",
                    "api_key_ref": "secret://expert-work/dev/llm/anthropic",
                },
                "system_prompt": {"template": "you are a test agent"},
                "sandbox": {
                    "resources": {"cpu": "1.0", "memory": "1Gi"},
                    "network": {"egress": "proxy", "allowlist": ["api.anthropic.com"]},
                    "filesystem": {"readonly_root": True, "writable": ["/workspace"]},
                },
                "tools": tools,
            },
        }
    )


async def _seed_agent_spec(
    app: object,
    tenant_id: UUID,
    name: str,
    tools: list[dict[str, object]],
    *,
    status: AgentSpecStatus | None = None,
) -> None:
    """Create an agent spec row directly in the in-memory store, optionally
    moving it to a non-ACTIVE status afterwards (create always inserts ACTIVE)."""
    repo = app.state.agent_spec_repo  # type: ignore[attr-defined]
    await repo.create(
        tenant_id=tenant_id,
        spec=_agent_spec(name, tools),
        spec_sha256=hashlib.sha256(name.encode()).hexdigest(),
        created_by="test",
    )
    if status is not None:
        await repo.update_status(tenant_id=tenant_id, name=name, version="1.0.0", status=status)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_probes_persists_and_encrypts_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """POST probe→persist→encrypt: token not in response, resolvable in secret store."""
    app, admin_headers, tenant_id = await _make_app_with_admin()
    monkeypatch.setattr("control_plane.api.mcp_servers.probe_remote_mcp", _fake_probe_ok)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://cp.test") as client:
        resp = await client.post(
            "/v1/mcp-servers",
            json={
                "name": "github",
                "transport": "streamable_http",
                "url": "https://mcp.example.com/mcp",
                "auth_type": "bearer",
                "token": "ghp_REALTOKEN",
                "timeout_s": 30.0,
            },
            headers=admin_headers,
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["success"] is True
        assert body["data"]["name"] == "github"
        assert body["data"]["tool_count"] == 1
        # The raw token must not appear in the response body
        assert "ghp_REALTOKEN" not in resp.text
        # token_secret_ref is stripped from the public payload
        assert "token_secret_ref" not in body["data"]
        # The raw token IS resolvable from the secret store under the tenant path
        ref_name = f"expert-work/tenant/{tenant_id}/mcp/github/token"
        resolved = await app.state.secret_store.get(ref_name)  # type: ignore[attr-defined]
        assert resolved == "ghp_REALTOKEN"


@pytest.mark.asyncio
async def test_post_probe_failure_does_not_persist(monkeypatch: pytest.MonkeyPatch) -> None:
    """POST probe-fail → 422 + nothing persisted."""
    app, admin_headers, _ = await _make_app_with_admin()
    monkeypatch.setattr("control_plane.api.mcp_servers.probe_remote_mcp", _fake_probe_fail)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://cp.test") as client:
        resp = await client.post(
            "/v1/mcp-servers",
            json={
                "name": "down",
                "transport": "sse",
                "url": "https://down.example.com/sse",
                "auth_type": "none",
            },
            headers=admin_headers,
        )
        assert resp.status_code == 422
        assert resp.json()["detail"]["code"] == "MCP_SERVER_PROBE_FAILED"
        # Nothing persisted — list returns empty
        lst = await client.get("/v1/mcp-servers", headers=admin_headers)
        assert lst.status_code == 200
        assert lst.json()["data"] == []


@pytest.mark.asyncio
async def test_post_ssrf_url_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """POST with a link-local/SSRF URL → 422 MCP_SERVER_INVALID_URL before probe."""
    app, admin_headers, _ = await _make_app_with_admin()
    monkeypatch.setattr("control_plane.api.mcp_servers.probe_remote_mcp", _fake_probe_ok)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://cp.test") as client:
        resp = await client.post(
            "/v1/mcp-servers",
            json={
                "name": "evil",
                "transport": "streamable_http",
                "url": "http://169.254.169.254/x",
                "auth_type": "none",
            },
            headers=admin_headers,
        )
        assert resp.status_code == 422
        assert resp.json()["detail"]["code"] == "MCP_SERVER_INVALID_URL"


@pytest.mark.asyncio
async def test_non_admin_forbidden(monkeypatch: pytest.MonkeyPatch) -> None:
    """Non-admin (viewer) → 403 on POST."""
    app, _, tenant_id = await _make_app_with_admin()
    viewer_headers = await _seed_viewer_headers(app, tenant_id)
    monkeypatch.setattr("control_plane.api.mcp_servers.probe_remote_mcp", _fake_probe_ok)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://cp.test") as client:
        resp = await client.post(
            "/v1/mcp-servers",
            json={
                "name": "x",
                "transport": "sse",
                "url": "https://x.example.com/sse",
                "auth_type": "none",
            },
            headers=viewer_headers,
        )
        assert resp.status_code == 403


@pytest.mark.asyncio
async def test_duplicate_name_conflict(monkeypatch: pytest.MonkeyPatch) -> None:
    """Registering the same name twice → 409 MCP_SERVER_DUPLICATE on second POST."""
    app, admin_headers, _ = await _make_app_with_admin()
    monkeypatch.setattr("control_plane.api.mcp_servers.probe_remote_mcp", _fake_probe_ok)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://cp.test") as client:
        payload = {
            "name": "github",
            "transport": "sse",
            "url": "https://x.example.com/sse",
            "auth_type": "none",
        }
        first = await client.post("/v1/mcp-servers", json=payload, headers=admin_headers)
        assert first.status_code == 201
        dup = await client.post("/v1/mcp-servers", json=payload, headers=admin_headers)
        assert dup.status_code == 409
        assert dup.json()["detail"]["code"] == "MCP_SERVER_DUPLICATE"


@pytest.mark.asyncio
async def test_post_none_auth_with_token_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """POST auth_type='none' + token set → 422 MCP_SERVER_TOKEN_NOT_ALLOWED; nothing persisted."""
    app, admin_headers, _ = await _make_app_with_admin()
    monkeypatch.setattr("control_plane.api.mcp_servers.probe_remote_mcp", _fake_probe_ok)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://cp.test") as client:
        resp = await client.post(
            "/v1/mcp-servers",
            json={
                "name": "noauth",
                "transport": "sse",
                "url": "https://x.example.com/sse",
                "auth_type": "none",
                "token": "x",
            },
            headers=admin_headers,
        )
        assert resp.status_code == 422
        assert resp.json()["detail"]["code"] == "MCP_SERVER_TOKEN_NOT_ALLOWED"
        lst = await client.get("/v1/mcp-servers", headers=admin_headers)
        assert lst.json()["data"] == []


@pytest.mark.asyncio
async def test_post_bearer_empty_token_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """POST auth_type='bearer' + empty token → 422 MCP_SERVER_TOKEN_REQUIRED; nothing persisted."""
    app, admin_headers, _ = await _make_app_with_admin()
    monkeypatch.setattr("control_plane.api.mcp_servers.probe_remote_mcp", _fake_probe_ok)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://cp.test") as client:
        resp = await client.post(
            "/v1/mcp-servers",
            json={
                "name": "bearer-empty",
                "transport": "streamable_http",
                "url": "https://x.example.com/mcp",
                "auth_type": "bearer",
                "token": "",
            },
            headers=admin_headers,
        )
        assert resp.status_code == 422
        assert resp.json()["detail"]["code"] == "MCP_SERVER_TOKEN_REQUIRED"
        lst = await client.get("/v1/mcp-servers", headers=admin_headers)
        assert lst.json()["data"] == []


@pytest.mark.asyncio
async def test_post_invalid_name_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """POST name='Bad Name!' (fails pattern) → 422 request-validation error; nothing persisted."""
    app, admin_headers, _ = await _make_app_with_admin()
    monkeypatch.setattr("control_plane.api.mcp_servers.probe_remote_mcp", _fake_probe_ok)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://cp.test") as client:
        resp = await client.post(
            "/v1/mcp-servers",
            json={
                "name": "Bad Name!",
                "transport": "sse",
                "url": "https://x.example.com/sse",
                "auth_type": "none",
            },
            headers=admin_headers,
        )
        assert resp.status_code == 422
        lst = await client.get("/v1/mcp-servers", headers=admin_headers)
        assert lst.json()["data"] == []


@pytest.mark.asyncio
async def test_delete_succeeds_when_unreferenced(monkeypatch: pytest.MonkeyPatch) -> None:
    """DELETE an existing server that is not referenced by any agent → 200."""
    app, admin_headers, _ = await _make_app_with_admin()
    monkeypatch.setattr("control_plane.api.mcp_servers.probe_remote_mcp", _fake_probe_ok)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://cp.test") as client:
        create_resp = await client.post(
            "/v1/mcp-servers",
            json={
                "name": "github",
                "transport": "sse",
                "url": "https://x.example.com/sse",
                "auth_type": "none",
            },
            headers=admin_headers,
        )
        assert create_resp.status_code == 201
        delete_resp = await client.delete("/v1/mcp-servers/github", headers=admin_headers)
        assert delete_resp.status_code == 200
        body = delete_resp.json()
        assert body["success"] is True
        assert body["data"]["implicit_all_agents"] == 0
        # Verify it's gone from the list
        lst = await client.get("/v1/mcp-servers", headers=admin_headers)
        assert lst.json()["data"] == []


@pytest.mark.asyncio
async def test_delete_conflicts_when_active_agent_references(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DELETE a server explicitly referenced by an ACTIVE agent → 409 IN_USE."""
    app, admin_headers, tenant_id = await _make_app_with_admin()
    monkeypatch.setattr("control_plane.api.mcp_servers.probe_remote_mcp", _fake_probe_ok)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://cp.test") as client:
        create_resp = await client.post(
            "/v1/mcp-servers",
            json={
                "name": "github",
                "transport": "sse",
                "url": "https://x.example.com/sse",
                "auth_type": "none",
            },
            headers=admin_headers,
        )
        assert create_resp.status_code == 201
        await _seed_agent_spec(
            app,
            tenant_id,
            "consumer",
            [{"type": "mcp", "servers": ["github"], "allow_tools": []}],
        )
        delete_resp = await client.delete("/v1/mcp-servers/github", headers=admin_headers)
        assert delete_resp.status_code == 409
        detail = delete_resp.json()["detail"]
        assert detail["code"] == "MCP_SERVER_IN_USE"
        assert "consumer" in detail["message"]
        # Row must survive the refused delete.
        lst = await client.get("/v1/mcp-servers", headers=admin_headers)
        assert [row["name"] for row in lst.json()["data"]] == ["github"]


@pytest.mark.asyncio
async def test_delete_conflicts_when_deprecated_agent_references(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DEPRECATED specs still count as live reference surface (only DELETED
    is filtered out) — an explicit reference from one keeps the 409."""
    app, admin_headers, tenant_id = await _make_app_with_admin()
    monkeypatch.setattr("control_plane.api.mcp_servers.probe_remote_mcp", _fake_probe_ok)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://cp.test") as client:
        create_resp = await client.post(
            "/v1/mcp-servers",
            json={
                "name": "github",
                "transport": "sse",
                "url": "https://x.example.com/sse",
                "auth_type": "none",
            },
            headers=admin_headers,
        )
        assert create_resp.status_code == 201
        await _seed_agent_spec(
            app,
            tenant_id,
            "sunsetting",
            [{"type": "mcp", "servers": ["github"], "allow_tools": []}],
            status=AgentSpecStatus.DEPRECATED,
        )
        delete_resp = await client.delete("/v1/mcp-servers/github", headers=admin_headers)
        assert delete_resp.status_code == 409
        assert delete_resp.json()["detail"]["code"] == "MCP_SERVER_IN_USE"


@pytest.mark.asyncio
async def test_delete_ignores_soft_deleted_agent_reference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bug regression sentinel: a soft-deleted agent's explicit reference must
    NOT block server deletion. Before the fix the reference check read ALL
    specs (including DELETED tombstones), so one dead agent produced a false
    409 that locked the server row forever."""
    app, admin_headers, tenant_id = await _make_app_with_admin()
    monkeypatch.setattr("control_plane.api.mcp_servers.probe_remote_mcp", _fake_probe_ok)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://cp.test") as client:
        create_resp = await client.post(
            "/v1/mcp-servers",
            json={
                "name": "github",
                "transport": "sse",
                "url": "https://x.example.com/sse",
                "auth_type": "none",
            },
            headers=admin_headers,
        )
        assert create_resp.status_code == 201
        await _seed_agent_spec(
            app,
            tenant_id,
            "ghost",
            [{"type": "mcp", "servers": ["github"], "allow_tools": []}],
            status=AgentSpecStatus.DELETED,
        )
        delete_resp = await client.delete("/v1/mcp-servers/github", headers=admin_headers)
        assert delete_resp.status_code == 200, delete_resp.text
        lst = await client.get("/v1/mcp-servers", headers=admin_headers)
        assert lst.json()["data"] == []


@pytest.mark.asyncio
async def test_delete_conflicts_when_reference_is_on_a_later_spec_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bug regression sentinel: the reference check must walk EVERY spec page.

    It used to read a single ``limit=1000`` page, so a tenant holding more
    specs than that page hid every reference past the boundary and the in-use
    server got deleted anyway. ``_SPEC_PAGE_SIZE`` is shrunk here so three
    specs already straddle the boundary (keeps the test fast); the referencing
    spec is seeded first so — the store lists newest-first — it lands on the
    second page.
    """
    page_size = 2
    app, admin_headers, tenant_id = await _make_app_with_admin()
    monkeypatch.setattr("control_plane.api.mcp_servers.probe_remote_mcp", _fake_probe_ok)
    monkeypatch.setattr("control_plane.api.mcp_servers._SPEC_PAGE_SIZE", page_size)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://cp.test") as client:
        create_resp = await client.post(
            "/v1/mcp-servers",
            json={
                "name": "github",
                "transport": "sse",
                "url": "https://x.example.com/sse",
                "auth_type": "none",
            },
            headers=admin_headers,
        )
        assert create_resp.status_code == 201
        await _seed_agent_spec(
            app,
            tenant_id,
            "page-two-consumer",
            [{"type": "mcp", "servers": ["github"], "allow_tools": []}],
        )
        for filler in ("filler-a", "filler-b"):
            await _seed_agent_spec(
                app,
                tenant_id,
                filler,
                [{"type": "mcp", "servers": ["unrelated"], "allow_tools": []}],
            )
        repo = app.state.agent_spec_repo  # type: ignore[attr-defined]
        # Guard the premise: at this page size the referencing spec must NOT sit
        # on the first page — otherwise a single-page check would still find it
        # and this sentinel would pass without paginating anything.
        first_page = await repo.list_by_tenant(tenant_id=tenant_id, limit=page_size, offset=0)
        first_page_names = [s.name for s in first_page]
        assert "page-two-consumer" not in first_page_names

        # Record the sweep's own paging arguments. Without this, a regression
        # that hardcodes a large single page (the original ``limit=1000``) still
        # swallows all three seeded specs and the 409 below stays green.
        sweep_calls: list[tuple[object, object]] = []
        real_list_by_tenant = repo.list_by_tenant

        async def _recording_list_by_tenant(**kwargs: object) -> object:
            sweep_calls.append((kwargs.get("limit"), kwargs.get("offset", 0)))
            return await real_list_by_tenant(**kwargs)

        monkeypatch.setattr(repo, "list_by_tenant", _recording_list_by_tenant)

        delete_resp = await client.delete("/v1/mcp-servers/github", headers=admin_headers)
        assert delete_resp.status_code == 409, delete_resp.text
        detail = delete_resp.json()["detail"]
        assert detail["code"] == "MCP_SERVER_IN_USE"
        assert "page-two-consumer" in detail["message"]
        assert len(sweep_calls) > 1, sweep_calls
        assert all(limit == page_size for limit, _ in sweep_calls), sweep_calls
        # Row must survive the refused delete.
        lst = await client.get("/v1/mcp-servers", headers=admin_headers)
        assert [row["name"] for row in lst.json()["data"]] == ["github"]


@pytest.mark.asyncio
async def test_delete_reports_implicit_all_agents_in_response_and_audit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """D3: servers empty/absent = dynamic "every available server" wildcard —
    not a hard reference (delete proceeds), but the impact count is surfaced
    in the delete response data AND the MCP_SERVER_DELETE audit details.
    Soft-deleted specs and specs without mcp tools do not count."""
    from control_plane.audit import build_default_audit_logger
    from expert_work.persistence.audit_log import InMemoryAuditLogStore
    from expert_work.protocol import AuditAction, AuditQuery

    lifecycle = Lifecycle()
    lifecycle.mark_ready()
    audit_store = InMemoryAuditLogStore()
    app = create_app(
        settings=_build_settings(),
        lifecycle=lifecycle,
        jwt_verifier=build_test_jwt_verifier(),
        audit_logger=build_default_audit_logger(audit_store),
    )
    tenant_id = uuid4()
    token = make_test_jwt(tenant_id=tenant_id, subject=str(uuid4()), roles=("admin",))
    admin_headers = {"Authorization": f"Bearer {token}"}
    monkeypatch.setattr("control_plane.api.mcp_servers.probe_remote_mcp", _fake_probe_ok)
    mcp_wildcard: list[dict[str, object]] = [{"type": "mcp", "servers": [], "allow_tools": []}]
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://cp.test") as client:
        create_resp = await client.post(
            "/v1/mcp-servers",
            json={
                "name": "github",
                "transport": "sse",
                "url": "https://x.example.com/sse",
                "auth_type": "none",
            },
            headers=admin_headers,
        )
        assert create_resp.status_code == 201
        # Two live wildcard followers…
        await _seed_agent_spec(app, tenant_id, "implicit-a", mcp_wildcard)
        await _seed_agent_spec(app, tenant_id, "implicit-b", mcp_wildcard)
        # …one agent without any mcp tool (counts 0)…
        await _seed_agent_spec(
            app, tenant_id, "no-mcp", [{"type": "builtin", "name": "web_search"}]
        )
        # …and one soft-deleted wildcard follower (must not count).
        await _seed_agent_spec(
            app, tenant_id, "implicit-ghost", mcp_wildcard, status=AgentSpecStatus.DELETED
        )

        delete_resp = await client.delete("/v1/mcp-servers/github", headers=admin_headers)
        assert delete_resp.status_code == 200, delete_resp.text
        body = delete_resp.json()
        assert body["success"] is True
        assert body["data"]["implicit_all_agents"] == 2

    page = await audit_store.query(AuditQuery(tenant_id=tenant_id))
    delete_entries = [r for r in page.entries if r.action == AuditAction.MCP_SERVER_DELETE]
    assert len(delete_entries) == 1
    assert delete_entries[0].details["name"] == "github"
    assert delete_entries[0].details["implicit_all_agents"] == 2


@pytest.mark.asyncio
async def test_delete_counts_deprecated_wildcard_follower_in_implicit_all(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A DEPRECATED spec with an empty-``servers`` (wildcard) mcp tool still
    counts toward ``implicit_all_agents`` — the reference-check's active-spec
    filter is "not DELETED" (DEPRECATED counts), and the wildcard tally reads
    that same ``active_specs`` list. Companion to
    ``test_delete_conflicts_when_deprecated_agent_references`` (DEPRECATED +
    explicit reference → 409); this one is DEPRECATED + wildcard → 200 with
    the follower counted, not silently dropped."""
    app, admin_headers, tenant_id = await _make_app_with_admin()
    monkeypatch.setattr("control_plane.api.mcp_servers.probe_remote_mcp", _fake_probe_ok)
    mcp_wildcard: list[dict[str, object]] = [{"type": "mcp", "servers": [], "allow_tools": []}]
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://cp.test") as client:
        create_resp = await client.post(
            "/v1/mcp-servers",
            json={
                "name": "github",
                "transport": "sse",
                "url": "https://x.example.com/sse",
                "auth_type": "none",
            },
            headers=admin_headers,
        )
        assert create_resp.status_code == 201
        await _seed_agent_spec(
            app,
            tenant_id,
            "sunsetting-wildcard",
            mcp_wildcard,
            status=AgentSpecStatus.DEPRECATED,
        )

        delete_resp = await client.delete("/v1/mcp-servers/github", headers=admin_headers)
        assert delete_resp.status_code == 200, delete_resp.text
        assert delete_resp.json()["data"]["implicit_all_agents"] == 1


@pytest.mark.asyncio
async def test_post_and_delete_invalidate_tenant_mcp_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """POST and DELETE both call pool_service.invalidate + agent_runtime.invalidate_tenant."""
    app, admin_headers, tenant_id = await _make_app_with_admin()
    monkeypatch.setattr("control_plane.api.mcp_servers.probe_remote_mcp", _fake_probe_ok)

    class _PoolSpy:
        def __init__(self) -> None:
            self.invalidated: list[UUID] = []

        async def invalidate(self, tid: UUID) -> None:
            self.invalidated.append(tid)

    class _RuntimeSpy:
        def __init__(self) -> None:
            self.invalidated: list[UUID] = []

        def invalidate_tenant(self, tid: UUID) -> None:
            self.invalidated.append(tid)

    pool_spy = _PoolSpy()
    rt_spy = _RuntimeSpy()
    app.state.tenant_mcp_pool_service = pool_spy
    app.state.agent_runtime = rt_spy

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://cp.test") as client:
        post_resp = await client.post(
            "/v1/mcp-servers",
            json={
                "name": "github",
                "transport": "sse",
                "url": "https://x.example.com/sse",
                "auth_type": "none",
            },
            headers=admin_headers,
        )
        assert post_resp.status_code == 201
        delete_resp = await client.delete("/v1/mcp-servers/github", headers=admin_headers)
        assert delete_resp.status_code == 200

    assert pool_spy.invalidated.count(tenant_id) == 2
    assert rt_spy.invalidated.count(tenant_id) == 2


@pytest.mark.asyncio
async def test_delete_removes_token_and_headers_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    """DELETE clears both the token and custom_headers secret-store blobs.

    This domain is paste-only (no ref-mode secrets), so every ref on the row
    is platform-managed and safe to delete outright.
    """
    app, admin_headers, tenant_id = await _make_app_with_admin()
    monkeypatch.setattr("control_plane.api.mcp_servers.probe_remote_mcp", _fake_probe_ok)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://cp.test") as client:
        create_resp = await client.post(
            "/v1/mcp-servers",
            json={
                "name": "github",
                "transport": "streamable_http",
                "url": "https://mcp.example.com/mcp",
                "auth_type": "bearer",
                "token": "ghp_REALTOKEN",
                "custom_headers": {"X-Org": "acme"},
                "timeout_s": 30.0,
            },
            headers=admin_headers,
        )
        assert create_resp.status_code == 201, create_resp.text
        token_name = f"expert-work/tenant/{tenant_id}/mcp/github/token"
        headers_name = f"expert-work/tenant/{tenant_id}/mcp/github/headers"
        # Sanity: both secrets exist before delete.
        assert await app.state.secret_store.get(token_name) == "ghp_REALTOKEN"  # type: ignore[attr-defined]
        await app.state.secret_store.get(headers_name)  # type: ignore[attr-defined]  # does not raise

        delete_resp = await client.delete("/v1/mcp-servers/github", headers=admin_headers)
        assert delete_resp.status_code == 200

    with pytest.raises(SecretNotFoundError):
        await app.state.secret_store.get(token_name)  # type: ignore[attr-defined]
    with pytest.raises(SecretNotFoundError):
        await app.state.secret_store.get(headers_name)  # type: ignore[attr-defined]


class _DeleteAlwaysFailsSecretStore:
    """Wraps a real ``SecretStore`` but makes ``delete`` always raise.

    Used to prove the (c2) best-effort secret cleanup in the DELETE handler
    never lets a secret-store failure escape as a 500 — it must be caught,
    logged, and the request must still complete (200 + row gone + audit
    written). Regression guard for the ``extra={"name": ...}`` LogRecord
    collision that used to turn this into an unhandled ``KeyError``.
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
async def test_delete_best_effort_secret_cleanup_does_not_abort_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DELETE must still 200 + remove the row + write the audit row even when
    secret_store.delete raises. This also regression-guards the fixed
    logger.warning(..., extra={"server_name": name}) call: before the fix,
    ``extra={"name": name}`` collided with the reserved ``LogRecord.name``
    attribute and raised ``KeyError`` from inside the except-block, turning
    a best-effort cleanup failure into an unhandled 500.
    """
    from control_plane.audit import build_default_audit_logger
    from expert_work.persistence.audit_log import InMemoryAuditLogStore
    from expert_work.protocol import AuditAction, AuditQuery

    lifecycle = Lifecycle()
    lifecycle.mark_ready()
    audit_store = InMemoryAuditLogStore()
    app = create_app(
        settings=_build_settings(),
        lifecycle=lifecycle,
        jwt_verifier=build_test_jwt_verifier(),
        audit_logger=build_default_audit_logger(audit_store),
    )
    tenant_id = uuid4()
    token = make_test_jwt(tenant_id=tenant_id, subject=str(uuid4()), roles=("admin",))
    admin_headers = {"Authorization": f"Bearer {token}"}
    monkeypatch.setattr("control_plane.api.mcp_servers.probe_remote_mcp", _fake_probe_ok)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://cp.test") as client:
        create_resp = await client.post(
            "/v1/mcp-servers",
            json={
                "name": "github",
                "transport": "streamable_http",
                "url": "https://mcp.example.com/mcp",
                "auth_type": "bearer",
                "token": "ghp_REALTOKEN",
                "timeout_s": 30.0,
            },
            headers=admin_headers,
        )
        assert create_resp.status_code == 201, create_resp.text

        # Swap in a secret store whose delete() always raises, *after* the
        # create above so the token secret is genuinely persisted first.
        app.state.secret_store = _DeleteAlwaysFailsSecretStore(app.state.secret_store)  # type: ignore[attr-defined]

        delete_resp = await client.delete("/v1/mcp-servers/github", headers=admin_headers)

    # (1) The request completes with the normal success status — the
    # secret-store failure never escapes as a 500.
    assert delete_resp.status_code == 200

    # (2) The row is really gone (delete happened before the best-effort
    # secret cleanup, so this is unaffected by the injected failure).
    assert await app.state.tenant_mcp_server_store.get(tenant_id=tenant_id, name="github") is None  # type: ignore[attr-defined]

    # (3) The audit row was written — best-effort cleanup failing does not
    # block the audit trail either.
    page = await audit_store.query(AuditQuery(tenant_id=tenant_id))
    actions = {r.action for r in page.entries}
    assert AuditAction.MCP_SERVER_DELETE in actions


@pytest.mark.asyncio
async def test_test_connection_probes_without_persisting(monkeypatch: pytest.MonkeyPatch) -> None:
    """POST /test probes the connection and returns tool_count — nothing is persisted."""
    app, admin_headers, _ = await _make_app_with_admin()
    monkeypatch.setattr("control_plane.api.mcp_servers.probe_remote_mcp", _fake_probe_ok)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://cp.test") as client:
        r = await client.post(
            "/v1/mcp-servers/test",
            json={
                "transport": "streamable_http",
                "url": "https://mcp.example.com/mcp",
                "auth_type": "none",
            },
            headers=admin_headers,
        )
        assert r.status_code == 200, r.text
        assert r.json()["data"]["tool_count"] == 1
        # nothing persisted
        lst = await client.get("/v1/mcp-servers", headers=admin_headers)
        assert lst.json()["data"] == []


@pytest.mark.asyncio
async def test_test_connection_failure_returns_422(monkeypatch: pytest.MonkeyPatch) -> None:
    """POST /test with a failing probe → 422 MCP_SERVER_PROBE_FAILED."""
    app, admin_headers, _ = await _make_app_with_admin()
    monkeypatch.setattr("control_plane.api.mcp_servers.probe_remote_mcp", _fake_probe_fail)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://cp.test") as client:
        r = await client.post(
            "/v1/mcp-servers/test",
            json={"transport": "sse", "url": "https://down.example.com/sse", "auth_type": "none"},
            headers=admin_headers,
        )
        assert r.status_code == 422
        assert r.json()["detail"]["code"] == "MCP_SERVER_PROBE_FAILED"


@pytest.mark.asyncio
async def test_available_lists_tenant_servers(monkeypatch: pytest.MonkeyPatch) -> None:
    """GET /available returns tenant-registered servers with source='tenant'.

    Platform-allowlist seeding requires a tenant_config row which is not seeded
    in the basic in-memory harness; that half is covered by the tenant_config
    service unit tests. This test asserts the tenant-server half only.
    """
    app, admin_headers, _ = await _make_app_with_admin()
    monkeypatch.setattr("control_plane.api.mcp_servers.probe_remote_mcp", _fake_probe_ok)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://cp.test") as client:
        await client.post(
            "/v1/mcp-servers",
            json={
                "name": "github",
                "transport": "sse",
                "url": "https://x.example.com/sse",
                "auth_type": "none",
            },
            headers=admin_headers,
        )
        r = await client.get("/v1/mcp-servers/available", headers=admin_headers)
        assert r.status_code == 200
        names = {item["name"] for item in r.json()["data"]}
        assert "github" in names
        sources = {item["name"]: item["source"] for item in r.json()["data"]}
        assert sources["github"] == "tenant"


@pytest.mark.asyncio
async def test_server_tools_lists_live_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    """GET /{name}/tools returns the live tool list via probe."""
    app, admin_headers, _ = await _make_app_with_admin()
    monkeypatch.setattr("control_plane.api.mcp_servers.probe_remote_mcp", _fake_probe_ok)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://cp.test") as client:
        await client.post(
            "/v1/mcp-servers",
            json={
                "name": "github",
                "transport": "sse",
                "url": "https://x.example.com/sse",
                "auth_type": "none",
            },
            headers=admin_headers,
        )
        r = await client.get("/v1/mcp-servers/github/tools", headers=admin_headers)
        assert r.status_code == 200
        assert r.json()["data"][0]["name"] == "create_issue"


@pytest.mark.asyncio
async def test_server_tools_unknown_404(monkeypatch: pytest.MonkeyPatch) -> None:
    """GET /{name}/tools for an unregistered server → 404 MCP_SERVER_NOT_FOUND."""
    app, admin_headers, _ = await _make_app_with_admin()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://cp.test") as client:
        r = await client.get("/v1/mcp-servers/nope/tools", headers=admin_headers)
        assert r.status_code == 404


async def _seed_catalog_entry(app: object, upsert: McpConnectorCatalogUpsert) -> None:
    async with bypass_rls_session():
        await app.state.mcp_connector_catalog_store.create(  # type: ignore[attr-defined]
            upsert=upsert, actor_id="seed"
        )


async def _enable_for_tenant(app: object, tenant_id: UUID, name: str) -> None:
    await app.state.tenant_config_service.upsert(  # type: ignore[attr-defined]
        tenant_id=tenant_id,
        patch=TenantConfigPatch(display_name="Acme", mcp_allowlist=[name]),
        actor_id="seed",
    )


@pytest.mark.asyncio
async def test_server_tools_platform_bearer_via_allowlist(monkeypatch: pytest.MonkeyPatch) -> None:
    """A platform catalog server the tenant enabled (mcp_allowlist) is probeable —
    the bearer is resolved from its bearer_token_ref. Regression: this path used
    to 404 because the endpoint only looked at tenant-private servers."""
    app, admin_headers, tenant_id = await _make_app_with_admin()
    monkeypatch.setattr("control_plane.api.mcp_servers.probe_remote_mcp", _fake_probe_ok)
    await app.state.secret_store.put(parse_secret_ref("secret://amap"), "tok")  # type: ignore[attr-defined]
    await _seed_catalog_entry(
        app,
        McpConnectorCatalogUpsert(
            name="amap-maps",
            display_name="Amap",
            transport="streamable_http",
            url_template="https://mcp.amap.test/mcp",
            auth_type="bearer",
            bearer_token_ref="secret://amap",
        ),
    )
    await _enable_for_tenant(app, tenant_id, "amap-maps")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://cp.test") as client:
        r = await client.get("/v1/mcp-servers/amap-maps/tools", headers=admin_headers)
        assert r.status_code == 200, r.text
        assert r.json()["data"][0]["name"] == "create_issue"


@pytest.mark.asyncio
async def test_server_tools_platform_not_enabled_404(monkeypatch: pytest.MonkeyPatch) -> None:
    """A platform catalog server NOT in the tenant's allowlist → 404 (no leak)."""
    app, admin_headers, _ = await _make_app_with_admin()
    monkeypatch.setattr("control_plane.api.mcp_servers.probe_remote_mcp", _fake_probe_ok)
    await _seed_catalog_entry(
        app,
        McpConnectorCatalogUpsert(
            name="amap-maps",
            display_name="Amap",
            transport="streamable_http",
            url_template="https://mcp.amap.test/mcp",
            auth_type="none",
        ),
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://cp.test") as client:
        r = await client.get("/v1/mcp-servers/amap-maps/tools", headers=admin_headers)
        assert r.status_code == 404


@pytest.mark.asyncio
async def test_server_tools_platform_oauth2_409(monkeypatch: pytest.MonkeyPatch) -> None:
    """An enabled platform OAuth2 server can't be shared-probed → 409 (per-user)."""
    app, admin_headers, tenant_id = await _make_app_with_admin()
    await _seed_catalog_entry(
        app,
        McpConnectorCatalogUpsert(
            name="linear",
            display_name="Linear",
            transport="sse",
            url_template="https://mcp.linear.app/sse",
            auth_type="oauth2",
            oauth_client_id="expert-work-linear",
            oauth_scopes="read",
        ),
    )
    await _enable_for_tenant(app, tenant_id, "linear")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://cp.test") as client:
        r = await client.get("/v1/mcp-servers/linear/tools", headers=admin_headers)
        assert r.status_code == 409
        assert r.json()["detail"]["code"] == "MCP_SERVER_OAUTH_PROBE_UNSUPPORTED"


@pytest.mark.asyncio
async def test_patch_invalidates_tenant_mcp_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PATCH calls pool_service.invalidate + agent_runtime.invalidate_tenant."""
    app, admin_headers, tenant_id = await _make_app_with_admin()
    monkeypatch.setattr("control_plane.api.mcp_servers.probe_remote_mcp", _fake_probe_ok)

    class _PoolSpy:
        def __init__(self) -> None:
            self.invalidated: list[UUID] = []

        async def invalidate(self, tid: UUID) -> None:
            self.invalidated.append(tid)

    class _RuntimeSpy:
        def __init__(self) -> None:
            self.invalidated: list[UUID] = []

        def invalidate_tenant(self, tid: UUID) -> None:
            self.invalidated.append(tid)

    pool_spy = _PoolSpy()
    rt_spy = _RuntimeSpy()
    app.state.tenant_mcp_pool_service = pool_spy
    app.state.agent_runtime = rt_spy

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://cp.test") as client:
        # Register first
        post_resp = await client.post(
            "/v1/mcp-servers",
            json={
                "name": "github",
                "transport": "sse",
                "url": "https://x.example.com/sse",
                "auth_type": "none",
            },
            headers=admin_headers,
        )
        assert post_resp.status_code == 201
        # Reset spy counts after POST
        pool_spy.invalidated.clear()
        rt_spy.invalidated.clear()
        # PATCH to disable
        patch_resp = await client.patch(
            "/v1/mcp-servers/github",
            json={"enabled": False},
            headers=admin_headers,
        )
        assert patch_resp.status_code == 200

    assert pool_spy.invalidated.count(tenant_id) == 1
    assert rt_spy.invalidated.count(tenant_id) == 1


@pytest.mark.asyncio
async def test_probe_endpoints_have_dedicated_rate_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Audit #6: probe-bearing endpoints draw on a tight dedicated bucket — a
    second probe past the (capacity-1) bucket is 429'd before the outbound call."""
    lifecycle = Lifecycle()
    lifecycle.mark_ready()
    settings = Settings(
        service_name="control_plane_test",
        env="dev",
        auth_mode="dev",
        db_dsn="postgresql+asyncpg://test@localhost/test",
        rate_limit_burst=10_000,
        rate_limit_per_second=10_000.0,
        oidc_issuer=TEST_ISSUER,
        oidc_audience=[TEST_AUDIENCE],
        # Tight probe bucket: 1 token, near-zero refill so the 2nd call is denied.
        mcp_probe_rate_limit_capacity=1,
        mcp_probe_rate_limit_refill_per_sec=0.001,
    )
    app = create_app(settings=settings, lifecycle=lifecycle, jwt_verifier=build_test_jwt_verifier())
    monkeypatch.setattr("control_plane.api.mcp_servers.probe_remote_mcp", _fake_probe_ok)
    token = make_test_jwt(tenant_id=uuid4(), subject=str(uuid4()), roles=("admin",))
    headers = {"Authorization": f"Bearer {token}"}
    body = {
        "transport": "streamable_http",
        "url": "https://mcp.example.com/mcp",
        "auth_type": "none",
        "timeout_s": 30.0,
    }
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://cp.test") as client:
        r1 = await client.post("/v1/mcp-servers/test", json=body, headers=headers)
        r2 = await client.post("/v1/mcp-servers/test", json=body, headers=headers)
    assert r1.status_code == 200, r1.text
    assert r2.status_code == 429
    assert r2.json()["detail"]["code"] == "MCP_PROBE_RATE_LIMITED"


# ---------------------------------------------------------------------------
# Connectivity health (#2)
# ---------------------------------------------------------------------------


async def _register_ok(
    client: AsyncClient, headers: dict[str, str], *, name: str = "github"
) -> None:
    resp = await client.post(
        "/v1/mcp-servers",
        json={
            "name": name,
            "transport": "streamable_http",
            "url": "https://mcp.example.com/mcp",
            "auth_type": "none",
            "timeout_s": 30.0,
        },
        headers=headers,
    )
    assert resp.status_code == 201, resp.text


@pytest.mark.asyncio
async def test_register_seeds_health_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    """A successful registration probe seeds last_probe_status=ok on the row."""
    app, admin_headers, _ = await _make_app_with_admin()
    monkeypatch.setattr("control_plane.api.mcp_servers.probe_remote_mcp", _fake_probe_ok)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://cp.test") as client:
        resp = await client.post(
            "/v1/mcp-servers",
            json={
                "name": "github",
                "transport": "streamable_http",
                "url": "https://mcp.example.com/mcp",
                "auth_type": "none",
                "timeout_s": 30.0,
            },
            headers=admin_headers,
        )
        assert resp.status_code == 201, resp.text
        data = resp.json()["data"]
        assert data["last_probe_status"] == "ok"
        assert data["last_probe_at"] is not None
        assert data["last_probe_error"] is None


@pytest.mark.asyncio
async def test_tools_failure_persists_error_health(monkeypatch: pytest.MonkeyPatch) -> None:
    """An on-demand tools probe that fails persists last_probe_status=error,
    visible on the subsequent list."""
    app, admin_headers, _ = await _make_app_with_admin()
    monkeypatch.setattr("control_plane.api.mcp_servers.probe_remote_mcp", _fake_probe_ok)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://cp.test") as client:
        await _register_ok(client, admin_headers)  # health starts ok
        # Now the server becomes unreachable.
        monkeypatch.setattr("control_plane.api.mcp_servers.probe_remote_mcp", _fake_probe_fail)
        tools = await client.get("/v1/mcp-servers/github/tools", headers=admin_headers)
        assert tools.status_code == 502, tools.text
        listed = await client.get("/v1/mcp-servers", headers=admin_headers)
        row = next(r for r in listed.json()["data"] if r["name"] == "github")
        assert row["last_probe_status"] == "error"
        assert row["last_probe_error"] == "MCP_SERVER_PROBE_FAILED"


@pytest.mark.asyncio
async def test_tools_success_sets_health_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    """A recovered on-demand tools probe flips health back to ok and clears error."""
    app, admin_headers, _ = await _make_app_with_admin()
    monkeypatch.setattr("control_plane.api.mcp_servers.probe_remote_mcp", _fake_probe_ok)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://cp.test") as client:
        await _register_ok(client, admin_headers)
        monkeypatch.setattr("control_plane.api.mcp_servers.probe_remote_mcp", _fake_probe_fail)
        await client.get("/v1/mcp-servers/github/tools", headers=admin_headers)  # -> error
        monkeypatch.setattr("control_plane.api.mcp_servers.probe_remote_mcp", _fake_probe_ok)
        ok = await client.get("/v1/mcp-servers/github/tools", headers=admin_headers)
        assert ok.status_code == 200, ok.text
        listed = await client.get("/v1/mcp-servers", headers=admin_headers)
        row = next(r for r in listed.json()["data"] if r["name"] == "github")
        assert row["last_probe_status"] == "ok"
        assert row["last_probe_error"] is None


# ---------------------------------------------------------------------------
# Custom HTTP headers (M1)
# ---------------------------------------------------------------------------


async def _capturing_probe(captured: dict[str, object]):
    async def _probe(**kwargs: object) -> list[MCPToolDef]:
        captured.update(kwargs)
        return [MCPToolDef(name="t", description="", input_schema={})]

    return _probe


@pytest.mark.asyncio
async def test_post_custom_headers_encrypted_and_names_returned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Custom headers: values land encrypted in the secret store as one blob,
    only the (non-secret) names surface in the API; ref is masked."""
    app, admin_headers, tenant_id = await _make_app_with_admin()
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        "control_plane.api.mcp_servers.probe_remote_mcp", await _capturing_probe(captured)
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://cp.test") as client:
        resp = await client.post(
            "/v1/mcp-servers",
            json={
                "name": "svc",
                "transport": "streamable_http",
                "url": "https://mcp.example.com/mcp",
                "auth_type": "none",
                "custom_headers": {"X-API-Key": "SECRET-KEY", "X-Org": "acme"},
                "sse_read_timeout_s": 120.0,
            },
            headers=admin_headers,
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert sorted(body["data"]["custom_header_names"]) == ["X-API-Key", "X-Org"]
        assert body["data"]["sse_read_timeout_s"] == 120.0
        # secret value never echoed; ref masked
        assert "SECRET-KEY" not in resp.text
        assert "custom_headers_ref" not in body["data"]
        # probe received the unwrapped headers
        assert captured["custom_headers"] == {"X-API-Key": "SECRET-KEY", "X-Org": "acme"}
        # blob resolvable, contains the values
        ref_name = f"expert-work/tenant/{tenant_id}/mcp/svc/headers"
        import json as _json

        blob = await app.state.secret_store.get(ref_name)  # type: ignore[attr-defined]
        assert _json.loads(blob)["X-API-Key"] == "SECRET-KEY"


@pytest.mark.asyncio
async def test_post_bearer_with_custom_authorization_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A custom Authorization header alongside bearer → 422 (would be shadowed)."""
    app, admin_headers, _ = await _make_app_with_admin()
    monkeypatch.setattr("control_plane.api.mcp_servers.probe_remote_mcp", _fake_probe_ok)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://cp.test") as client:
        resp = await client.post(
            "/v1/mcp-servers",
            json={
                "name": "svc",
                "transport": "streamable_http",
                "url": "https://mcp.example.com/mcp",
                "auth_type": "bearer",
                "token": "ghp_X",
                "custom_headers": {"Authorization": "Bearer sneaky"},
            },
            headers=admin_headers,
        )
        assert resp.status_code == 422
        assert resp.json()["detail"]["code"] == "MCP_SERVER_HEADER_CONFLICT"


@pytest.mark.asyncio
async def test_post_invalid_header_name_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """A header name with a forbidden char (CRLF/colon/space) → 422."""
    app, admin_headers, _ = await _make_app_with_admin()
    monkeypatch.setattr("control_plane.api.mcp_servers.probe_remote_mcp", _fake_probe_ok)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://cp.test") as client:
        resp = await client.post(
            "/v1/mcp-servers",
            json={
                "name": "svc",
                "transport": "streamable_http",
                "url": "https://mcp.example.com/mcp",
                "auth_type": "none",
                "custom_headers": {"Bad Header": "v"},
            },
            headers=admin_headers,
        )
        assert resp.status_code == 422
        assert resp.json()["detail"]["code"] == "MCP_SERVER_HEADERS_INVALID"


# ---------------------------------------------------------------------------
# Tenant enablement of platform shared servers (P2 — opt-in "租户选择使用")
# ---------------------------------------------------------------------------


async def _seed_platform_none(app: object, *, name: str = "weather") -> UUID:
    async with bypass_rls_session():
        rec = await app.state.mcp_connector_catalog_store.create(  # type: ignore[attr-defined]
            upsert=McpConnectorCatalogUpsert(
                name=name,
                display_name=name.title(),
                transport="streamable_http",
                url_template=f"https://mcp.example.com/{name}",
                auth_type="none",
            ),
            actor_id="seed",
        )
    return rec.id


async def _configure_tenant(app: object, tenant_id: UUID) -> None:
    await app.state.tenant_config_service.upsert(  # type: ignore[attr-defined]
        tenant_id=tenant_id,
        patch=TenantConfigPatch(display_name="Acme"),
        actor_id="seed",
    )


@pytest.mark.asyncio
async def test_enable_then_disable_platform_server() -> None:
    app, headers, tenant_id = await _make_app_with_admin()
    await _configure_tenant(app, tenant_id)
    cat_id = await _seed_platform_none(app)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://cp.test") as client:
        en = await client.post(f"/v1/mcp-servers/catalog/{cat_id}/enable", headers=headers)
        assert en.status_code == 200, en.text
        assert en.json()["data"]["tenant_enabled"] is True
        # The catalog list reflects the opt-in state.
        lst = await client.get("/v1/mcp-servers/catalog", headers=headers)
        row = next(r for r in lst.json()["data"] if r["name"] == "weather")
        assert row["tenant_enabled"] is True
        # Idempotent re-enable.
        again = await client.post(f"/v1/mcp-servers/catalog/{cat_id}/enable", headers=headers)
        assert again.status_code == 200
        # Disable removes it.
        dis = await client.delete(f"/v1/mcp-servers/catalog/{cat_id}/enable", headers=headers)
        assert dis.status_code == 200
        assert dis.json()["data"]["tenant_enabled"] is False
        lst2 = await client.get("/v1/mcp-servers/catalog", headers=headers)
        row2 = next(r for r in lst2.json()["data"] if r["name"] == "weather")
        assert row2["tenant_enabled"] is False


@pytest.mark.asyncio
async def test_enable_unconfigured_tenant_409() -> None:
    app, headers, _tenant_id = await _make_app_with_admin()
    cat_id = await _seed_platform_none(app)  # tenant has no config row
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://cp.test") as client:
        resp = await client.post(f"/v1/mcp-servers/catalog/{cat_id}/enable", headers=headers)
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "TENANT_NOT_CONFIGURED"


@pytest.mark.asyncio
async def test_enable_unknown_catalog_404() -> None:
    app, headers, tenant_id = await _make_app_with_admin()
    await _configure_tenant(app, tenant_id)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://cp.test") as client:
        resp = await client.post(f"/v1/mcp-servers/catalog/{uuid4()}/enable", headers=headers)
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "MCP_CATALOG_NOT_FOUND"


@pytest.mark.asyncio
async def test_enable_disable_emit_audit() -> None:
    from control_plane.audit import build_default_audit_logger
    from expert_work.persistence.audit_log import InMemoryAuditLogStore
    from expert_work.protocol import AuditQuery

    lifecycle = Lifecycle()
    lifecycle.mark_ready()
    audit_store = InMemoryAuditLogStore()
    app = create_app(
        settings=_build_settings(),
        lifecycle=lifecycle,
        jwt_verifier=build_test_jwt_verifier(),
        audit_logger=build_default_audit_logger(audit_store),
    )
    tenant_id = uuid4()
    token = make_test_jwt(tenant_id=tenant_id, subject=str(uuid4()), roles=("admin",))
    headers = {"Authorization": f"Bearer {token}"}
    await _configure_tenant(app, tenant_id)
    cat_id = await _seed_platform_none(app)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://cp.test") as client:
        await client.post(f"/v1/mcp-servers/catalog/{cat_id}/enable", headers=headers)
        await client.delete(f"/v1/mcp-servers/catalog/{cat_id}/enable", headers=headers)
    page = await audit_store.query(AuditQuery(tenant_id=tenant_id))
    actions = {r.action.value for r in page.entries}
    assert "mcp_catalog:enable" in actions
    assert "mcp_catalog:disable" in actions


@pytest.mark.asyncio
async def test_available_platform_row_enriched(monkeypatch: pytest.MonkeyPatch) -> None:
    """A tenant-enabled platform server carries display_name/auth_type/catalog_id."""
    app, admin_headers, tenant_id = await _make_app_with_admin()
    await _seed_catalog_entry(
        app,
        McpConnectorCatalogUpsert(
            name="amap-maps",
            display_name="高德地图",
            transport="streamable_http",
            url_template="https://mcp.amap.test/mcp",
            auth_type="oauth2",
            oauth_client_id="cid",
        ),
    )
    await _enable_for_tenant(app, tenant_id, "amap-maps")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://cp.test") as client:
        r = await client.get("/v1/mcp-servers/available", headers=admin_headers)
    assert r.status_code == 200, r.text
    rows = {item["name"]: item for item in r.json()["data"]}
    row = rows["amap-maps"]
    assert row["source"] == "platform"
    assert row["display_name"] == "高德地图"
    assert row["auth_type"] == "oauth2"
    assert row.get("catalog_id")


@pytest.mark.asyncio
async def test_available_platform_row_degrades_when_catalog_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stale allowlist name (no catalog entry) degrades to {name, source} — no 500."""
    app, admin_headers, tenant_id = await _make_app_with_admin()
    await _enable_for_tenant(app, tenant_id, "ghost-server")  # not seeded in catalog
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://cp.test") as client:
        r = await client.get("/v1/mcp-servers/available", headers=admin_headers)
    assert r.status_code == 200, r.text
    rows = {item["name"]: item for item in r.json()["data"]}
    assert rows["ghost-server"] == {"name": "ghost-server", "source": "platform"}
