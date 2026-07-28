"""HTTP tests for the C.5b admission wire on sessions:create / runs:create.

These exercise the path:
    POST /v1/sessions    → 429 on QPS deny
    POST /v1/sessions/{tid}/runs → 429 on QPS deny

For the happy path the existing ``test_sessions_api.py`` /
``test_runs_api.py`` modules cover the 201 / SSE flow; here we only
pin the new admission contract.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from redis.exceptions import RedisError

from control_plane.app import create_app
from control_plane.audit import build_default_audit_logger
from control_plane.settings import DEFAULT_DEV_TENANT_ID, Settings
from expert_work.persistence.audit_log import InMemoryAuditLogStore
from expert_work.persistence.quota import InMemoryTenantQuotaStore
from expert_work.protocol import (
    AuditAction,
    AuditQuery,
    CheckRequest,
    CheckResult,
    CommitRequest,
    QuotaDimension,
    ReserveRequest,
    ReserveResult,
    TenantQuotaPatch,
)
from tests.agent_fixtures import stub_agent_runtime
from tests.auth_fixtures import TEST_AUDIENCE, TEST_ISSUER, build_test_jwt_verifier, make_test_jwt

_TENANT = DEFAULT_DEV_TENANT_ID


@dataclass
class _RedisDownQuotaService:
    """Quota engine whose Redis backend is unreachable — every ``check`` raises."""

    async def check(self, req: CheckRequest) -> CheckResult:
        raise RedisError("connection refused")

    async def reserve_tokens(self, req: ReserveRequest) -> ReserveResult:
        raise NotImplementedError

    async def commit_tokens(self, req: CommitRequest) -> None:
        raise NotImplementedError

    async def release_tokens(self, reservation_id: UUID, *, tenant_id: UUID) -> None:
        raise NotImplementedError


_AGENT_YAML = """\
apiVersion: expert_work.io/v1
kind: Agent
metadata:
  name: code-reviewer
  version: "1.0.0"
  tenant: platform-eng
spec:
  tenant_config: {}
  model:
    provider: anthropic
    name: claude-sonnet-4-5
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
async def app_factory(
    audit_store: InMemoryAuditLogStore,
) -> AsyncIterator[tuple[FastAPI, InMemoryTenantQuotaStore]]:
    """Build an app with a quota store we can seed from each test."""
    quota_store = InMemoryTenantQuotaStore()
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
        tenant_quota_repo=quota_store,
        enable_reaper=False,
        agent_runtime=stub_agent_runtime(),
    )
    yield app, quota_store


@pytest.fixture
async def admission_client(
    app_factory: tuple[FastAPI, InMemoryTenantQuotaStore],
) -> AsyncIterator[AsyncClient]:
    app, _ = app_factory
    transport = ASGITransport(app=app)
    headers = {"Authorization": f"Bearer {make_test_jwt(tenant_id=_TENANT)}"}
    async with AsyncClient(
        transport=transport,
        base_url="http://control-plane.test",
        headers=headers,
    ) as client:
        await client.post("/v1/agents", json={"manifest_yaml": _AGENT_YAML})
        yield client


# ---------------------------------------------------------------------------
# Redis outage — fail CLOSED (subsystems/16 § 5.2, § 6)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_session_returns_503_when_quota_engine_redis_down(
    audit_store: InMemoryAuditLogStore,
) -> None:
    """A quota-engine Redis outage must deny admission (fail CLOSED) — the
    opposite of the rate-limit tiers, which fail open. We can't verify the
    quota decision, so we must not silently allow unmetered spend."""
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
        quota_service=_RedisDownQuotaService(),
        enable_reaper=False,
        agent_runtime=stub_agent_runtime(),
    )
    transport = ASGITransport(app=app)
    headers = {"Authorization": f"Bearer {make_test_jwt(tenant_id=_TENANT)}"}
    async with AsyncClient(
        transport=transport,
        base_url="http://control-plane.test",
        headers=headers,
    ) as client:
        await client.post("/v1/agents", json={"manifest_yaml": _AGENT_YAML})
        response = await client.post(
            "/v1/sessions",
            json={"agent_name": "code-reviewer", "agent_version": "1.0.0"},
        )
    assert response.status_code == 503
    assert response.json()["detail"] == "quota_engine_unavailable"


# ---------------------------------------------------------------------------
# sessions:create
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_session_denied_when_quota_exhausted(
    app_factory: tuple[FastAPI, InMemoryTenantQuotaStore],
    admission_client: AsyncClient,
    audit_store: InMemoryAuditLogStore,
) -> None:
    _, quota_store = app_factory
    # Tight QPS: burst=1 → first call consumes, second is denied.
    await quota_store.upsert(
        tenant_id=_TENANT,
        patch=TenantQuotaPatch(
            dimension=QuotaDimension.QPS,
            scope={},
            limit_value=1,
            burst=1,
        ),
        updated_by="test",
    )

    first = await admission_client.post(
        "/v1/sessions",
        json={"agent_name": "code-reviewer", "agent_version": "1.0.0"},
    )
    assert first.status_code == 201

    denied = await admission_client.post(
        "/v1/sessions",
        json={"agent_name": "code-reviewer", "agent_version": "1.0.0"},
    )
    assert denied.status_code == 429
    body = denied.json()
    assert body["success"] is False
    assert body["error"]["code"] == "RATE_LIMIT_EXCEEDED"
    assert body["error"]["dimension"] == "qps"
    assert body["error"]["retry_after_s"] >= 0
    assert denied.headers["Retry-After"] == str(body["error"]["retry_after_s"])

    # Audit row emitted with the right action.
    page = await audit_store.query(
        AuditQuery(tenant_id=_TENANT, action=AuditAction.QUOTA_RATE_LIMIT_DENIED)
    )
    assert len(page.entries) >= 1
    assert page.entries[0].details.get("dimension") == "qps"
    assert page.entries[0].resource_id == "session"


# ---------------------------------------------------------------------------
# runs:create
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_trigger_run_denied_when_quota_exhausted(
    app_factory: tuple[FastAPI, InMemoryTenantQuotaStore],
    admission_client: AsyncClient,
    audit_store: InMemoryAuditLogStore,
) -> None:
    _, quota_store = app_factory
    # Seed BEFORE any HTTP calls: the QuotaService caches resolved
    # quotas for 60s (subsystems/16 § 5.3) so seeding after the first
    # call would land outside the cache window.
    # burst=2: session create consumes 1, first run consumes the
    # other, second run is denied.
    await quota_store.upsert(
        tenant_id=_TENANT,
        patch=TenantQuotaPatch(
            dimension=QuotaDimension.QPS,
            scope={},
            limit_value=1,
            burst=2,
        ),
        updated_by="test",
    )

    created = await admission_client.post(
        "/v1/sessions",
        json={"agent_name": "code-reviewer", "agent_version": "1.0.0"},
    )
    assert created.status_code == 201
    thread_id = created.json()["data"]["thread_id"]

    first_run = await admission_client.post(
        f"/v1/sessions/{thread_id}/runs",
        json={"input": "go"},
    )
    assert first_run.status_code == 200  # SSE stream

    denied = await admission_client.post(
        f"/v1/sessions/{thread_id}/runs",
        json={"input": "go"},
    )
    assert denied.status_code == 429
    body = denied.json()
    assert body["error"]["code"] == "RATE_LIMIT_EXCEEDED"
    assert denied.headers["Retry-After"] is not None

    page = await audit_store.query(
        AuditQuery(tenant_id=_TENANT, action=AuditAction.QUOTA_RATE_LIMIT_DENIED)
    )
    assert any(entry.resource_id == "run" for entry in page.entries)


# ---------------------------------------------------------------------------
# admission allows when no quota is configured
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_session_unlimited_without_quota_row(
    admission_client: AsyncClient,
) -> None:
    """No tenant_quota row + no default_qps_limit → admission is a no-op."""
    response = await admission_client.post(
        "/v1/sessions",
        json={"agent_name": "code-reviewer", "agent_version": "1.0.0"},
    )
    assert response.status_code == 201


@pytest.mark.asyncio
async def test_admission_scope_matches_agent(
    app_factory: tuple[FastAPI, InMemoryTenantQuotaStore],
    admission_client: AsyncClient,
) -> None:
    """An agent-scoped quota for ``other`` should not throttle ``code-reviewer``."""
    _, quota_store = app_factory
    await quota_store.upsert(
        tenant_id=_TENANT,
        patch=TenantQuotaPatch(
            dimension=QuotaDimension.QPS,
            scope={"agent": "other-agent"},
            limit_value=1,
            burst=1,
        ),
        updated_by="test",
    )
    # Should not be throttled even though there's a 1-burst quota row.
    for _ in range(3):
        resp = await admission_client.post(
            "/v1/sessions",
            json={"agent_name": "code-reviewer", "agent_version": "1.0.0"},
        )
        assert resp.status_code == 201


# ---------------------------------------------------------------------------
# tenant isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_quota_buckets_isolated_per_tenant(
    app_factory: tuple[FastAPI, InMemoryTenantQuotaStore],
    admission_client: AsyncClient,
) -> None:
    """A tight quota on tenant A doesn't drain tenant B's bucket."""
    _, quota_store = app_factory
    other_tenant = uuid4()
    await quota_store.upsert(
        tenant_id=other_tenant,
        patch=TenantQuotaPatch(
            dimension=QuotaDimension.QPS,
            scope={},
            limit_value=1,
            burst=1,
        ),
        updated_by="test",
    )
    # Tenant A (DEFAULT) has no quota → unlimited. 3 in a row pass.
    for _ in range(3):
        resp = await admission_client.post(
            "/v1/sessions",
            json={"agent_name": "code-reviewer", "agent_version": "1.0.0"},
        )
        assert resp.status_code == 201
