"""Integration tests for :class:`control_plane.auth.AuthMiddleware`."""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import UUID

import pytest
from httpx import ASGITransport, AsyncClient

from control_plane.app import create_app
from control_plane.audit import build_default_audit_logger
from control_plane.settings import DEFAULT_DEV_TENANT_ID, Settings
from expert_work.persistence.audit_log import InMemoryAuditLogStore
from expert_work.protocol import AuditAction, AuditQuery
from tests.auth_fixtures import (
    TEST_AUDIENCE,
    TEST_ISSUER,
    build_test_jwt_verifier,
    make_test_jwt,
)

_TENANT = DEFAULT_DEV_TENANT_ID
_NIL_TENANT = UUID("00000000-0000-0000-0000-000000000000")

_AGENT_YAML = """\
apiVersion: expert_work.io/v1
kind: Agent
metadata:
  name: alpha
  version: "1.0.0"
  tenant: t
spec:
  tenant_config: {}
  model:
    provider: anthropic
    name: claude-sonnet-4-6
  system_prompt:
    template: "x"
  sandbox:
    resources: { cpu: "1.0", memory: "1Gi" }
    network:
      egress: proxy
      allowlist: ["api.anthropic.com"]
    filesystem:
      readonly_root: true
      writable: ["/workspace"]
"""


@pytest.fixture
def audit_store() -> InMemoryAuditLogStore:
    return InMemoryAuditLogStore()


@pytest.fixture
async def auth_client(audit_store: InMemoryAuditLogStore) -> AsyncIterator[AsyncClient]:
    settings = Settings(
        env="dev",
        auth_mode="dev",
        rate_limit_burst=10_000,
        rate_limit_per_second=10_000.0,
        oidc_issuer=TEST_ISSUER,
        oidc_audience=[TEST_AUDIENCE],
    )
    app = create_app(
        settings=settings,
        audit_logger=build_default_audit_logger(audit_store),
        jwt_verifier=build_test_jwt_verifier(),
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://control-plane.test") as client:
        yield client


# ---------------------------------------------------------------------------
# happy path / exemption / failure shapes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_token_returns_401_with_envelope(auth_client: AsyncClient) -> None:
    response = await auth_client.get("/v1/agents")
    assert response.status_code == 401
    body = response.json()
    assert body["success"] is False
    assert body["error"]["code"] == "AUTH_MISSING_CREDENTIALS"
    assert response.headers["WWW-Authenticate"].startswith("Bearer")


@pytest.mark.asyncio
async def test_invalid_token_returns_401(auth_client: AsyncClient) -> None:
    response = await auth_client.get(
        "/v1/agents",
        headers={"Authorization": "Bearer not.a.real.jwt"},
    )
    assert response.status_code == 401
    body = response.json()
    assert body["error"]["code"] == "AUTH_INVALID_TOKEN"


@pytest.mark.asyncio
async def test_expired_token_returns_401(auth_client: AsyncClient) -> None:
    expired = make_test_jwt(tenant_id=_TENANT, ttl_s=-120)
    response = await auth_client.get("/v1/agents", headers={"Authorization": f"Bearer {expired}"})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "AUTH_TOKEN_EXPIRED"


@pytest.mark.asyncio
async def test_valid_token_passes_through(auth_client: AsyncClient) -> None:
    token = make_test_jwt(tenant_id=_TENANT, subject="alice", roles=("admin",))
    response = await auth_client.get("/v1/agents", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_health_endpoint_is_exempt(auth_client: AsyncClient) -> None:
    """``/healthz/live`` must be reachable without a token (k8s liveness)."""
    response = await auth_client.get("/healthz/live")
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_metrics_endpoint_is_exempt(auth_client: AsyncClient) -> None:
    response = await auth_client.get("/metrics")
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_x_expert_work_tenant_header_can_no_longer_impersonate(
    auth_client: AsyncClient,
) -> None:
    """Regression: dev-mode header trust was retired in C.1."""
    bogus_tenant = "11111111-1111-1111-1111-111111111111"
    response = await auth_client.get("/v1/agents", headers={"X-Expert-Work-Tenant": bogus_tenant})
    assert response.status_code == 401


# ---------------------------------------------------------------------------
# audit emission
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_failed_auth_emits_login_failed_audit(
    auth_client: AsyncClient, audit_store: InMemoryAuditLogStore
) -> None:
    await auth_client.get("/v1/agents")  # no bearer → fails

    page = await audit_store.query(AuditQuery(tenant_id=_NIL_TENANT))
    failed = [
        row
        for row in page.entries
        if row.action is AuditAction.AUTH_LOGIN_FAILED and row.result.value == "denied"
    ]
    assert failed, "expected an AUTH_LOGIN_FAILED row"
    row = failed[0]
    assert row.actor_id == "unauthenticated"
    assert row.reason == "AUTH_MISSING_CREDENTIALS"
    assert row.details["path"] == "/v1/agents"


@pytest.mark.asyncio
async def test_valid_token_does_not_emit_login_failed(
    auth_client: AsyncClient, audit_store: InMemoryAuditLogStore
) -> None:
    token = make_test_jwt(tenant_id=_TENANT)
    await auth_client.get("/v1/agents", headers={"Authorization": f"Bearer {token}"})
    page = await audit_store.query(AuditQuery(tenant_id=_NIL_TENANT))
    assert not any(row.action is AuditAction.AUTH_LOGIN_FAILED for row in page.entries)


# ---------------------------------------------------------------------------
# principal projection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_principal_drives_audit_tenant(
    auth_client: AsyncClient, audit_store: InMemoryAuditLogStore
) -> None:
    """JWT's ``tenant_id`` must end up on subsequent audit rows."""
    other_tenant = UUID("33333333-3333-3333-3333-333333333333")
    token = make_test_jwt(tenant_id=other_tenant, subject="svc-1")

    response = await auth_client.post(
        "/v1/agents",
        json={"manifest_yaml": _AGENT_YAML},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 201

    page = await audit_store.query(AuditQuery(tenant_id=other_tenant))
    assert any(row.action.value == "manifest:write" for row in page.entries)
