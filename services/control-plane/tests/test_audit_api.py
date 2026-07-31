"""End-to-end tests for the H.4 PR 3 audit query API.

Covers the new ``GET /v1/audit`` + ``GET /v1/audit/{id}`` endpoints —
filter pass-through, cursor opaqueness, self-audit emission, the
``from_ts > to_ts`` 400 case, cross-tenant gating, and the
``actor_tenant_id`` invariant required by ``AuditLogger.query`` when
``tenant_id='*'`` (Stream N).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from control_plane.app import create_app
from control_plane.audit import build_default_audit_logger
from control_plane.settings import DEFAULT_DEV_TENANT_ID, Settings
from expert_work.persistence.audit_log import InMemoryAuditLogStore
from expert_work.protocol import AuditAction, AuditEntry, AuditResult, Role
from tests.agent_fixtures import stub_agent_runtime
from tests.auth_fixtures import (
    TEST_AUDIENCE,
    TEST_ISSUER,
    build_test_jwt_verifier,
    make_test_jwt,
)

_TENANT = DEFAULT_DEV_TENANT_ID
_TENANT_OTHER = UUID("99999999-9999-9999-9999-999999999999")


class _Ctx:
    def __init__(self, client: AsyncClient, store: InMemoryAuditLogStore, app: object) -> None:
        self.client = client
        self.store = store
        self.app = app


def _make_entry(
    *,
    tenant_id: UUID = _TENANT,
    action: AuditAction = AuditAction.MANIFEST_READ,
    resource_type: str = "manifest",
    actor_id: str = "dev-user",
    result: AuditResult = AuditResult.SUCCESS,
) -> AuditEntry:
    return AuditEntry(
        tenant_id=tenant_id,
        actor_type="user",
        actor_id=actor_id,
        action=action,
        resource_type=resource_type,
        resource_id="rsrc-1",
        result=result,
    )


@pytest.fixture
async def ctx() -> AsyncIterator[_Ctx]:
    settings = Settings(
        env="dev",
        auth_mode="dev",
        rate_limit_burst=10_000,
        rate_limit_per_second=10_000.0,
        oidc_issuer=TEST_ISSUER,
        oidc_audience=[TEST_AUDIENCE],
    )
    audit_store = InMemoryAuditLogStore()
    audit_logger = build_default_audit_logger(audit_store)
    app = create_app(
        settings=settings,
        audit_logger=audit_logger,
        jwt_verifier=build_test_jwt_verifier(),
        agent_runtime=stub_agent_runtime(),
        enable_scheduler=False,
        enable_curation_worker=False,
    )
    transport = ASGITransport(app=app)
    headers = {"Authorization": f"Bearer {make_test_jwt(tenant_id=_TENANT)}"}
    async with AsyncClient(
        transport=transport, base_url="http://control-plane.test", headers=headers
    ) as client:
        yield _Ctx(client, audit_store, app)


# --- happy path -----------------------------------------------------------


@pytest.mark.asyncio
async def test_list_audit_returns_seeded_rows(ctx: _Ctx) -> None:
    for _ in range(3):
        await ctx.store.append(_make_entry())

    resp = await ctx.client.get("/v1/audit")
    assert resp.status_code == 200
    body = resp.json()
    # 3 seeded + the self-audit row emitted by AuditLogger.query.
    assert len(body["items"]) >= 3
    assert body["applied_scope"] == str(_TENANT)
    assert "has_more" in body


@pytest.mark.asyncio
async def test_list_audit_filters_by_action(ctx: _Ctx) -> None:
    await ctx.store.append(_make_entry(action=AuditAction.MANIFEST_READ))
    await ctx.store.append(_make_entry(action=AuditAction.MANIFEST_WRITE))
    resp = await ctx.client.get("/v1/audit", params={"action": "manifest:write"})
    assert resp.status_code == 200
    actions = {e["action"] for e in resp.json()["items"]}
    assert "manifest:write" in actions
    assert "manifest:read" not in actions


@pytest.mark.asyncio
async def test_list_audit_filters_by_result(ctx: _Ctx) -> None:
    await ctx.store.append(_make_entry(result=AuditResult.SUCCESS))
    await ctx.store.append(_make_entry(result=AuditResult.ERROR))
    resp = await ctx.client.get("/v1/audit", params={"result": "error"})
    assert resp.status_code == 200
    results = {e["result"] for e in resp.json()["items"]}
    assert results == {"error"}


@pytest.mark.asyncio
async def test_list_audit_invalid_time_range_400(ctx: _Ctx) -> None:
    resp = await ctx.client.get(
        "/v1/audit",
        params={
            "from_ts": "2026-05-26T12:00:00Z",
            "to_ts": "2026-05-26T11:00:00Z",
        },
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["code"] == "INVALID_TIME_RANGE"


@pytest.mark.asyncio
async def test_list_audit_limit_clamped_to_max(ctx: _Ctx) -> None:
    # _MAX_AUDIT_LIMIT = 500; FastAPI's Query validator rejects > 500.
    resp = await ctx.client.get("/v1/audit", params={"limit": 999})
    assert resp.status_code == 422  # FastAPI parameter validation


@pytest.mark.asyncio
async def test_list_audit_emits_self_audit_row(ctx: _Ctx) -> None:
    """Every read writes a ``audit:read`` row (AuditLogger.query invariant)."""
    before = await ctx.store.query(
        # cast to AuditQuery via the model so we get the same code path
        _make_q := __import__("expert_work.protocol", fromlist=["AuditQuery"]).AuditQuery(
            tenant_id=_TENANT
        )
    )
    before_count = len(before.entries)
    resp = await ctx.client.get("/v1/audit")
    assert resp.status_code == 200
    after = await ctx.store.query(_make_q)
    # ≥1 new audit:read row landed.
    new_rows = [e for e in after.entries if e.action is AuditAction.AUDIT_READ]
    assert len(new_rows) >= 1
    assert len(after.entries) > before_count


# --- detail endpoint ------------------------------------------------------


@pytest.mark.asyncio
async def test_get_audit_entry_by_id(ctx: _Ctx) -> None:
    stored = await ctx.store.append(_make_entry())
    assert stored.id is not None
    resp = await ctx.client.get(f"/v1/audit/{stored.id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == stored.id
    assert body["action"] == AuditAction.MANIFEST_READ.value
    assert body["result"] == "success"


@pytest.mark.asyncio
async def test_get_audit_entry_404_when_missing(ctx: _Ctx) -> None:
    resp = await ctx.client.get("/v1/audit/999999")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_get_audit_entry_404_when_other_tenant(ctx: _Ctx) -> None:
    """Detail GET must not leak existence across tenants."""
    other = await ctx.store.append(_make_entry(tenant_id=_TENANT_OTHER))
    assert other.id is not None
    resp = await ctx.client.get(f"/v1/audit/{other.id}")
    assert resp.status_code == 404


# --- cross-tenant gating --------------------------------------------------


@pytest.mark.asyncio
async def test_tenant_admin_cross_tenant_is_403(ctx: _Ctx) -> None:
    resp = await ctx.client.get("/v1/audit", params={"tenant_id": "*"})
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_system_admin_cross_tenant_aggregates(ctx: _Ctx) -> None:
    sys_admin_id = uuid4()
    await ctx.app.state.role_binding_repo.create(  # type: ignore[attr-defined]
        subject_type="user",
        subject_id=sys_admin_id,
        tenant_id=None,
        role=Role.SYSTEM_ADMIN,
        platform_scope=True,
        granted_by="seed",
    )
    # Seed rows in two tenants.
    await ctx.store.append(_make_entry(tenant_id=_TENANT))
    await ctx.store.append(_make_entry(tenant_id=_TENANT_OTHER))
    token = make_test_jwt(tenant_id=_TENANT, subject=str(sys_admin_id))
    resp = await ctx.client.get(
        "/v1/audit",
        params={"tenant_id": "*"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["applied_scope"] == "cross_tenant"
    # ≥ 2 seeded rows visible across tenants.
    assert len(body["items"]) >= 2


# ---------------------------------------------------------------------------
# W2 — audit 详情读端点接跨租户 scope(系统管理员租户切换器)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_audit_entry_system_admin_target_tenant_200(ctx: _Ctx) -> None:
    stored = await ctx.store.append(_make_entry(tenant_id=_TENANT_OTHER))
    assert stored.id is not None
    sys_admin_id = uuid4()
    await ctx.app.state.role_binding_repo.create(  # type: ignore[attr-defined]
        subject_type="user",
        subject_id=sys_admin_id,
        tenant_id=None,
        role=Role.SYSTEM_ADMIN,
        platform_scope=True,
        granted_by="seed",
    )
    token = make_test_jwt(tenant_id=_TENANT, subject=str(sys_admin_id))
    resp = await ctx.client.get(
        f"/v1/audit/{stored.id}",
        params={"tenant_id": str(_TENANT_OTHER)},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["id"] == stored.id
    assert body["tenant_id"] == str(_TENANT_OTHER)


@pytest.mark.asyncio
async def test_get_audit_entry_foreign_tenant_403(ctx: _Ctx) -> None:
    resp = await ctx.client.get("/v1/audit/1", params={"tenant_id": str(_TENANT_OTHER)})
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"]["code"] == "TENANT_NOT_ALLOWED"


@pytest.mark.asyncio
async def test_get_audit_entry_tenant_id_star_400(ctx: _Ctx) -> None:
    resp = await ctx.client.get("/v1/audit/1", params={"tenant_id": "*"})
    assert resp.status_code == 400, resp.text
    assert resp.json()["detail"]["code"] == "SCOPE_ALL_NOT_SUPPORTED"
