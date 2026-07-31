"""HTTP tests for ``/v1/quota/*`` (internal) + ``/v1/tenants/{t}/quotas`` (admin)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from redis.exceptions import RedisError

from control_plane.app import create_app
from control_plane.audit import build_default_audit_logger
from control_plane.settings import DEFAULT_DEV_TENANT_ID, Settings
from expert_work.persistence.audit_log import InMemoryAuditLogStore
from expert_work.protocol import (
    AuditAction,
    AuditQuery,
    CheckRequest,
    CheckResult,
    CommitRequest,
    QuotaDimension,
    ReserveRequest,
    ReserveResult,
    Role,
    TenantQuotaPatch,
)
from tests.auth_fixtures import (
    TEST_AUDIENCE,
    TEST_ISSUER,
    build_test_jwt_verifier,
    make_test_jwt,
)

_TENANT = DEFAULT_DEV_TENANT_ID


@dataclass
class _RedisDownQuotaService:
    """Quota engine whose Redis backend is unreachable — every call raises.

    Mirrors ``test_quota_admission.py``'s fixture of the same name (kept
    local rather than imported so this file stays self-contained).
    """

    async def check(self, req: CheckRequest) -> CheckResult:
        raise RedisError("connection refused")

    async def reserve_tokens(self, req: ReserveRequest) -> ReserveResult:
        raise RedisError("connection refused")

    async def commit_tokens(self, req: CommitRequest) -> None:
        raise RedisError("connection refused")

    async def release_tokens(self, reservation_id: UUID, *, tenant_id: UUID) -> None:
        raise RedisError("connection refused")


@pytest.fixture
def audit_store() -> InMemoryAuditLogStore:
    return InMemoryAuditLogStore()


@pytest.fixture
async def quota_client(audit_store: InMemoryAuditLogStore) -> AsyncIterator[AsyncClient]:
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
        # Reaper would start a periodic asyncio task on top of the test
        # event loop; disable for endpoint-only tests.
        enable_reaper=False,
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://control-plane.test") as c:
        yield c


@pytest.fixture
async def redis_down_quota_client(
    audit_store: InMemoryAuditLogStore,
) -> AsyncIterator[AsyncClient]:
    """Client wired to a quota engine whose Redis backend is down."""
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
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://control-plane.test") as c:
        yield c


def _admin_token(tenant: UUID = _TENANT) -> str:
    return make_test_jwt(tenant_id=tenant, subject="admin-user", roles=("admin",))


def _operator_token(tenant: UUID = _TENANT) -> str:
    return make_test_jwt(tenant_id=tenant, subject="op-user", roles=("operator",))


def _viewer_token(tenant: UUID = _TENANT) -> str:
    return make_test_jwt(tenant_id=tenant, subject="viewer-user", roles=("viewer",))


@pytest.fixture
async def quota_sysadmin(
    audit_store: InMemoryAuditLogStore,
) -> AsyncIterator[tuple[AsyncClient, UUID]]:
    """Client whose subject holds a platform-scope binding → resolved to a
    system_admin (``allowed_tenants == "*"``). Yields ``(client, subject_id)``."""
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
        enable_reaper=False,
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
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://control-plane.test") as c:
        yield c, sys_admin_id


# ---------------------------------------------------------------------------
# admin endpoints — tenant_quotas
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_system_admin_lists_other_tenant_quotas(
    quota_sysadmin: tuple[AsyncClient, UUID],
) -> None:
    """Regression: a system_admin (``allowed_tenants == "*"``) listing another
    tenant's quotas must not 500 in ``_ensure_tenant_match`` — ``UUID in "*"``
    raised ``TypeError``. The cross-tenant guard now passes on the sentinel; an
    unseeded target tenant returns an empty list (not 500, not 403)."""
    client, sys_admin_id = quota_sysadmin
    other_tenant = uuid4()
    resp = await client.get(
        f"/v1/tenants/{other_tenant}/quotas",
        headers={
            "Authorization": f"Bearer {make_test_jwt(tenant_id=uuid4(), subject=str(sys_admin_id))}"
        },
    )
    assert resp.status_code == 200
    assert resp.json()["data"] == []


@pytest.mark.asyncio
async def test_admin_can_upsert_and_list_quotas(quota_client: AsyncClient) -> None:
    token = _admin_token()
    payload = TenantQuotaPatch(
        dimension=QuotaDimension.QPS,
        scope={},
        limit_value=10,
        burst=20,
    )

    create = await quota_client.post(
        f"/v1/tenants/{_TENANT}/quotas",
        headers={"Authorization": f"Bearer {token}"},
        json=payload.model_dump(mode="json"),
    )
    assert create.status_code == 201
    created = create.json()["data"]
    assert created["dimension"] == "qps"
    assert created["limit_value"] == 10

    listed = await quota_client.get(
        f"/v1/tenants/{_TENANT}/quotas",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert listed.status_code == 200
    rows = listed.json()["data"]
    assert len(rows) == 1
    assert rows[0]["id"] == created["id"]


@pytest.mark.asyncio
async def test_admin_can_delete_quota(quota_client: AsyncClient) -> None:
    token = _admin_token()
    payload = TenantQuotaPatch(
        dimension=QuotaDimension.QPS,
        scope={},
        limit_value=10,
        burst=20,
    )
    create = await quota_client.post(
        f"/v1/tenants/{_TENANT}/quotas",
        headers={"Authorization": f"Bearer {token}"},
        json=payload.model_dump(mode="json"),
    )
    quota_id = create.json()["data"]["id"]

    delete = await quota_client.delete(
        f"/v1/tenants/{_TENANT}/quotas/{quota_id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert delete.status_code == 204

    listed = await quota_client.get(
        f"/v1/tenants/{_TENANT}/quotas",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert listed.json()["data"] == []


@pytest.mark.asyncio
async def test_operator_cannot_upsert_quota(quota_client: AsyncClient) -> None:
    """OPERATOR has quota:read+check but not quota:write."""
    payload = TenantQuotaPatch(
        dimension=QuotaDimension.QPS,
        scope={},
        limit_value=10,
        burst=20,
    )
    resp = await quota_client.post(
        f"/v1/tenants/{_TENANT}/quotas",
        headers={"Authorization": f"Bearer {_operator_token()}"},
        json=payload.model_dump(mode="json"),
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_viewer_can_read_but_not_write(quota_client: AsyncClient) -> None:
    """VIEWER has quota:read (matrix) but not quota:write/delete/check."""
    read = await quota_client.get(
        f"/v1/tenants/{_TENANT}/quotas",
        headers={"Authorization": f"Bearer {_viewer_token()}"},
    )
    assert read.status_code == 200

    payload = TenantQuotaPatch(
        dimension=QuotaDimension.QPS,
        scope={},
        limit_value=1,
        burst=1,
    )
    write = await quota_client.post(
        f"/v1/tenants/{_TENANT}/quotas",
        headers={"Authorization": f"Bearer {_viewer_token()}"},
        json=payload.model_dump(mode="json"),
    )
    assert write.status_code == 403


@pytest.mark.asyncio
async def test_admin_cannot_edit_other_tenants(quota_client: AsyncClient) -> None:
    other_tenant = uuid4()
    payload = TenantQuotaPatch(
        dimension=QuotaDimension.QPS,
        scope={},
        limit_value=10,
        burst=20,
    )
    resp = await quota_client.post(
        f"/v1/tenants/{other_tenant}/quotas",
        headers={"Authorization": f"Bearer {_admin_token()}"},
        json=payload.model_dump(mode="json"),
    )
    assert resp.status_code == 403
    assert resp.json()["detail"]["code"] == "TENANT_MISMATCH"


# ---------------------------------------------------------------------------
# internal endpoints — /v1/quota/*
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_internal_check_allows_under_quota(quota_client: AsyncClient) -> None:
    token = _operator_token()
    resp = await quota_client.post(
        "/v1/quota/check",
        headers={"Authorization": f"Bearer {token}"},
        json={"tenant_id": str(_TENANT), "cost": 1},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["allowed"] is True


@pytest.mark.asyncio
async def test_internal_reserve_and_commit_flow(quota_client: AsyncClient) -> None:
    token = _operator_token()
    thread_id = uuid4()
    reserve = await quota_client.post(
        "/v1/quota/reserve",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "tenant_id": str(_TENANT),
            "agent": "alpha",
            "thread_id": str(thread_id),
            "estimated_tokens": 100,
        },
    )
    assert reserve.status_code == 200
    body = reserve.json()
    assert body["granted"] is True
    reservation_id = body["reservation_id"]

    commit = await quota_client.post(
        "/v1/quota/commit",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "reservation_id": reservation_id,
            "tenant_id": str(_TENANT),
            "actual_tokens": 87,
        },
    )
    assert commit.status_code == 204


@pytest.mark.asyncio
async def test_internal_release_404_when_unknown(quota_client: AsyncClient) -> None:
    token = _operator_token()
    bogus_id = uuid4()
    resp = await quota_client.post(
        f"/v1/quota/release/{bogus_id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "RESERVATION_NOT_FOUND"


@pytest.mark.asyncio
async def test_internal_check_returns_503_when_redis_down(
    redis_down_quota_client: AsyncClient,
) -> None:
    """RedisError inside ``quota.check`` must map to 503
    ``quota_engine_unavailable`` (subsystems/16 § 6 fail-closed contract for
    quota admission) rather than an unhandled 500."""
    token = _operator_token()
    resp = await redis_down_quota_client.post(
        "/v1/quota/check",
        headers={"Authorization": f"Bearer {token}"},
        json={"tenant_id": str(_TENANT), "cost": 1},
    )
    assert resp.status_code == 503
    assert resp.json()["detail"] == "quota_engine_unavailable"


@pytest.mark.asyncio
async def test_internal_reserve_returns_503_when_redis_down(
    redis_down_quota_client: AsyncClient,
) -> None:
    """Same fail-closed contract for ``/v1/quota/reserve``."""
    token = _operator_token()
    resp = await redis_down_quota_client.post(
        "/v1/quota/reserve",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "tenant_id": str(_TENANT),
            "agent": "alpha",
            "thread_id": str(uuid4()),
            "estimated_tokens": 100,
        },
    )
    assert resp.status_code == 503
    assert resp.json()["detail"] == "quota_engine_unavailable"


@pytest.mark.asyncio
async def test_internal_check_requires_quota_check_permission(quota_client: AsyncClient) -> None:
    """VIEWER lacks quota:check → 403."""
    resp = await quota_client.post(
        "/v1/quota/check",
        headers={"Authorization": f"Bearer {_viewer_token()}"},
        json={"tenant_id": str(_TENANT), "cost": 1},
    )
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# audit emission
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_admin_upsert_emits_quota_config_write_audit(
    quota_client: AsyncClient, audit_store: InMemoryAuditLogStore
) -> None:
    token = _admin_token()
    payload = TenantQuotaPatch(
        dimension=QuotaDimension.QPS,
        scope={},
        limit_value=10,
        burst=20,
    )
    await quota_client.post(
        f"/v1/tenants/{_TENANT}/quotas",
        headers={"Authorization": f"Bearer {token}"},
        json=payload.model_dump(mode="json"),
    )

    entries = await audit_store.query(
        AuditQuery(tenant_id=_TENANT, action=AuditAction.QUOTA_CONFIG_WRITE)
    )
    assert len(entries.entries) == 1
    entry = entries.entries[0]
    assert entry.details.get("dimension") == "qps"
    assert entry.details.get("limit_value") == 10


@pytest.mark.asyncio
async def test_reserve_denial_emits_budget_exceeded_audit(
    quota_client: AsyncClient, audit_store: InMemoryAuditLogStore
) -> None:
    """Set a tiny budget then over-reserve to trigger the audit branch."""
    # Need to seed the budget via the in-process store. Reach into the
    # app's state — same pattern other test files use.
    transport = quota_client._transport  # type: ignore[attr-defined]
    app = transport.app  # type: ignore[attr-defined]
    store = app.state.token_reservation_repo

    from datetime import UTC, datetime

    month = datetime.now(tz=UTC).date().replace(day=1)
    await store.set_budget_total_for_test(tenant_id=_TENANT, month=month, budget_total=10)

    token = _operator_token()
    resp = await quota_client.post(
        "/v1/quota/reserve",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "tenant_id": str(_TENANT),
            "agent": "alpha",
            "thread_id": str(uuid4()),
            "estimated_tokens": 1000,
        },
    )
    assert resp.status_code == 200
    assert resp.json()["granted"] is False

    entries = await audit_store.query(
        AuditQuery(tenant_id=_TENANT, action=AuditAction.QUOTA_BUDGET_EXCEEDED)
    )
    assert len(entries.entries) == 1


@pytest.mark.asyncio
async def test_internal_commit_returns_503_when_redis_down(
    redis_down_quota_client: AsyncClient,
) -> None:
    """RedisError inside ``quota.commit_tokens`` must map to 503
    ``quota_engine_unavailable`` (fail-closed contract)."""
    token = _operator_token()
    resp = await redis_down_quota_client.post(
        "/v1/quota/commit",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "reservation_id": str(uuid4()),
            "tenant_id": str(_TENANT),
            "actual_tokens": 50,
        },
    )
    assert resp.status_code == 503
    assert resp.json()["detail"] == "quota_engine_unavailable"


@pytest.mark.asyncio
async def test_internal_release_returns_503_when_redis_down(
    redis_down_quota_client: AsyncClient,
) -> None:
    """RedisError inside ``quota.release_tokens`` must map to 503
    ``quota_engine_unavailable`` (fail-closed contract)."""
    token = _operator_token()
    reservation_id = uuid4()
    resp = await redis_down_quota_client.post(
        f"/v1/quota/release/{reservation_id}?tenant_id={_TENANT}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 503
    assert resp.json()["detail"] == "quota_engine_unavailable"


# ---------------------------------------------------------------------------
# release — ?tenant_id= scope gate
# ---------------------------------------------------------------------------

_SYSTEM_TENANT = UUID("ffffffff-ffff-ffff-ffff-ffffffffffff")


@pytest.fixture
async def mtls_quota_client(audit_store: InMemoryAuditLogStore) -> AsyncIterator[AsyncClient]:
    """Client with the mTLS branch enabled (service principals via XFCC)."""
    settings = Settings(
        env="dev",
        auth_mode="dev",
        rate_limit_burst=10_000,
        rate_limit_per_second=10_000.0,
        oidc_issuer=TEST_ISSUER,
        oidc_audience=[TEST_AUDIENCE],
        mtls_enabled=True,
        mtls_allowed_service_subjects=["orchestrator"],
        mtls_system_tenant_id=_SYSTEM_TENANT,
    )
    app = create_app(
        settings=settings,
        audit_logger=build_default_audit_logger(audit_store),
        jwt_verifier=build_test_jwt_verifier(),
        enable_reaper=False,
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://control-plane.test") as c:
        yield c


@pytest.mark.asyncio
async def test_release_cross_tenant_target_forbidden_for_user(quota_client: AsyncClient) -> None:
    """A tenant-A user naming tenant B via ``?tenant_id=`` must get 403
    ``TENANT_NOT_ALLOWED`` — not silently operate on the other tenant's
    reservations (previously the query param was trusted unchecked)."""
    other_tenant = uuid4()
    resp = await quota_client.post(
        f"/v1/quota/release/{uuid4()}?tenant_id={other_tenant}",
        headers={"Authorization": f"Bearer {_operator_token()}"},
    )
    assert resp.status_code == 403
    assert resp.json()["detail"]["code"] == "TENANT_NOT_ALLOWED"


@pytest.mark.asyncio
async def test_release_cross_tenant_target_passes_gate_for_system_admin(
    quota_sysadmin: tuple[AsyncClient, UUID],
) -> None:
    """system_admin (``allowed_tenants == "*"``) may target any tenant — the
    scope gate passes and the unknown reservation surfaces as 404."""
    client, sys_admin_id = quota_sysadmin
    resp = await client.post(
        f"/v1/quota/release/{uuid4()}?tenant_id={uuid4()}",
        headers={
            "Authorization": f"Bearer {make_test_jwt(tenant_id=uuid4(), subject=str(sys_admin_id))}"
        },
    )
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "RESERVATION_NOT_FOUND"


@pytest.mark.asyncio
async def test_release_cross_tenant_target_passes_gate_for_mtls_service(
    mtls_quota_client: AsyncClient,
) -> None:
    """mTLS service principals keep the documented semantics: they sit on the
    system tenant and release on behalf of any tenant via ``?tenant_id=``."""
    resp = await mtls_quota_client.post(
        f"/v1/quota/release/{uuid4()}?tenant_id={uuid4()}",
        headers={"X-Forwarded-Client-Cert": 'Subject="CN=orchestrator,O=expert_work";Hash=abc'},
    )
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "RESERVATION_NOT_FOUND"


@pytest.mark.asyncio
async def test_release_without_query_param_uses_principal_tenant(
    quota_client: AsyncClient,
) -> None:
    """Regression: no ``?tenant_id=`` still falls back to the caller's home
    tenant — a real reservation reserved under it releases with 204."""
    token = _operator_token()
    reserve = await quota_client.post(
        "/v1/quota/reserve",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "tenant_id": str(_TENANT),
            "agent": "alpha",
            "thread_id": str(uuid4()),
            "estimated_tokens": 100,
        },
    )
    assert reserve.status_code == 200
    reservation_id = reserve.json()["reservation_id"]

    release = await quota_client.post(
        f"/v1/quota/release/{reservation_id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert release.status_code == 204
