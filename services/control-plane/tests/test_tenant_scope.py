"""Unit tests for :mod:`control_plane.tenant_scope` — Stream N (Mini-ADR N-3..N-5)."""

from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi import HTTPException

from control_plane.tenant_scope import (
    CrossTenant,
    SingleTenant,
    bypass_rls_session,
    ensure_tenant_scope,
)
from expert_work.persistence.audit_log import InMemoryAuditLogStore
from expert_work.persistence.rls import bypass_rls_var, current_tenant_id_var
from expert_work.protocol import AuditAction, Principal
from expert_work.runtime.audit.fallback import InMemoryAuditFallbackQueue
from expert_work.runtime.audit.logger import AuditLogger
from expert_work.runtime.audit.redactor import DefaultSecretRedactor


def _audit_for(store: InMemoryAuditLogStore) -> AuditLogger:
    return AuditLogger(
        store=store,
        redactor=DefaultSecretRedactor(),
        fallback=InMemoryAuditFallbackQueue(),
    )


def _tenant_user(*, tenant: object = None, allowed: tuple[object, ...] | None = None) -> Principal:
    t = tenant if tenant is not None else uuid4()
    allowed_tenants = allowed if allowed is not None else (t,)
    return Principal(
        subject_id=str(uuid4()),
        subject_type="user",
        tenant_id=t,  # type: ignore[arg-type]
        roles=("admin",),
        allowed_tenants=allowed_tenants,  # type: ignore[arg-type]
        is_system_admin=False,
    )


def _system_admin(*, home_tenant: object = None) -> Principal:
    return Principal(
        subject_id=str(uuid4()),
        subject_type="user",
        tenant_id=home_tenant if home_tenant is not None else uuid4(),  # type: ignore[arg-type]
        roles=("admin",),
        allowed_tenants="*",
        is_system_admin=True,
    )


# ---------------------------------------------------------------------------
# requested_tenant_id = "*"
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cross_tenant_query_by_system_admin_returns_cross_tenant_and_emits_audit() -> None:
    store = InMemoryAuditLogStore()
    audit = _audit_for(store)
    principal = _system_admin()

    result = await ensure_tenant_scope(
        principal,
        "*",
        audit,
        trace_id="trace-1",
        endpoint="GET /v1/agents",
    )
    assert isinstance(result, CrossTenant)

    rows = list(store._rows.values())
    assert len(rows) == 1
    assert rows[0].action == AuditAction.SYSTEM_CROSS_TENANT_QUERY
    assert rows[0].resource_type == "system"
    assert rows[0].resource_id == "GET /v1/agents"
    assert rows[0].actor_id == principal.subject_id
    assert rows[0].tenant_id == principal.tenant_id  # attribution under home tenant


@pytest.mark.asyncio
async def test_cross_tenant_query_by_tenant_user_403() -> None:
    audit = _audit_for(InMemoryAuditLogStore())
    principal = _tenant_user()
    with pytest.raises(HTTPException) as exc:
        await ensure_tenant_scope(principal, "*", audit)
    assert exc.value.status_code == 403
    assert exc.value.detail["code"] == "CROSS_TENANT_FORBIDDEN"  # type: ignore[index]


# ---------------------------------------------------------------------------
# requested_tenant_id = home tenant
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_request_home_tenant_returns_single_tenant_no_audit() -> None:
    store = InMemoryAuditLogStore()
    audit = _audit_for(store)
    tenant = uuid4()
    principal = _tenant_user(tenant=tenant)
    result = await ensure_tenant_scope(principal, tenant, audit)
    assert isinstance(result, SingleTenant)
    assert result.tenant_id == tenant
    assert list(store._rows.values()) == []  # no audit row


@pytest.mark.asyncio
async def test_no_tenant_id_falls_back_to_home_tenant() -> None:
    store = InMemoryAuditLogStore()
    audit = _audit_for(store)
    tenant = uuid4()
    principal = _tenant_user(tenant=tenant)
    result = await ensure_tenant_scope(principal, None, audit)
    assert isinstance(result, SingleTenant)
    assert result.tenant_id == tenant
    assert list(store._rows.values()) == []


# ---------------------------------------------------------------------------
# requested_tenant_id = other tenant
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_other_tenant_for_tenant_user_403() -> None:
    audit = _audit_for(InMemoryAuditLogStore())
    own, other = uuid4(), uuid4()
    principal = _tenant_user(tenant=own, allowed=(own,))
    with pytest.raises(HTTPException) as exc:
        await ensure_tenant_scope(principal, other, audit)
    assert exc.value.status_code == 403
    assert exc.value.detail["code"] == "TENANT_NOT_ALLOWED"  # type: ignore[index]


@pytest.mark.asyncio
async def test_other_tenant_for_system_admin_returns_single_tenant_and_emits_switch_audit() -> None:
    store = InMemoryAuditLogStore()
    audit = _audit_for(store)
    home, target = uuid4(), uuid4()
    principal = _system_admin(home_tenant=home)
    result = await ensure_tenant_scope(
        principal, target, audit, trace_id="t-2", endpoint="GET /v1/skills"
    )
    assert isinstance(result, SingleTenant)
    assert result.tenant_id == target

    rows = list(store._rows.values())
    assert len(rows) == 1
    assert rows[0].action == AuditAction.SYSTEM_TENANT_SWITCH
    assert rows[0].resource_type == "system"
    assert rows[0].tenant_id == target  # attribution under target tenant
    assert rows[0].details["home_tenant"] == str(home)
    assert rows[0].details["mode"] == "switch"
    assert rows[0].details["endpoint"] == "GET /v1/skills"
    assert rows[0].details["intent"] == "read"


@pytest.mark.asyncio
async def test_switch_audit_write_endpoint_derives_write_intent() -> None:
    store = InMemoryAuditLogStore()
    audit = _audit_for(store)
    principal = _system_admin()
    result = await ensure_tenant_scope(
        principal, uuid4(), audit, endpoint="PUT /v1/tenants/{tenant_id}/config"
    )
    assert isinstance(result, SingleTenant)

    rows = list(store._rows.values())
    assert len(rows) == 1
    assert rows[0].details["mode"] == "switch"
    assert rows[0].details["intent"] == "write"


@pytest.mark.asyncio
async def test_switch_audit_without_endpoint_omits_intent() -> None:
    store = InMemoryAuditLogStore()
    audit = _audit_for(store)
    principal = _system_admin()
    result = await ensure_tenant_scope(principal, uuid4(), audit)
    assert isinstance(result, SingleTenant)

    rows = list(store._rows.values())
    assert len(rows) == 1
    assert rows[0].details["mode"] == "switch"
    assert "intent" not in rows[0].details
    assert "endpoint" not in rows[0].details


@pytest.mark.asyncio
async def test_other_tenant_in_allowed_list_for_mtls_principal_no_switch_audit() -> None:
    """mTLS service principal (allowed_tenants=tuple, is_system_admin=False)
    can switch tenants but does NOT emit SYSTEM_TENANT_SWITCH (which is reserved
    for human system_admin actions — Mini-ADR N-5 spec scope)."""
    store = InMemoryAuditLogStore()
    audit = _audit_for(store)
    home, target = uuid4(), uuid4()
    principal = Principal(
        subject_id="mtls-service",
        subject_type="service",
        tenant_id=home,
        roles=(),
        allowed_tenants=(home, target),
        is_system_admin=False,
    )
    result = await ensure_tenant_scope(principal, target, audit)
    assert isinstance(result, SingleTenant)
    assert result.tenant_id == target
    assert list(store._rows.values()) == []  # no switch audit for non-system-admin


# ---------------------------------------------------------------------------
# bypass_rls_session
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bypass_rls_session_flips_contextvars_and_resets() -> None:
    # Sanity:both vars start at their defaults.
    assert bypass_rls_var.get() is False
    assert current_tenant_id_var.get() is None

    async with bypass_rls_session():
        assert bypass_rls_var.get() is True
        assert current_tenant_id_var.get() is None  # cleared on entry

    # Reset on exit.
    assert bypass_rls_var.get() is False


@pytest.mark.asyncio
async def test_bypass_rls_session_resets_pre_existing_tenant() -> None:
    """A tenant-scoped request that switches to cross-tenant must have
    its existing ContextVar reset on entry and restored on exit."""
    outer_tenant = uuid4()
    token = current_tenant_id_var.set(outer_tenant)
    try:
        async with bypass_rls_session():
            assert bypass_rls_var.get() is True
            assert current_tenant_id_var.get() is None

        # After exit, the outer tenant is restored.
        assert current_tenant_id_var.get() == outer_tenant
        assert bypass_rls_var.get() is False
    finally:
        current_tenant_id_var.reset(token)


# ---------------------------------------------------------------------------
# cross_tenant_enabled=False — the HX-8 deployment switch (Mini-ADR HX-H4)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_switch_off_blocks_aggregate_with_audit() -> None:
    store = InMemoryAuditLogStore()
    audit = _audit_for(store)
    principal = _system_admin()

    with pytest.raises(HTTPException) as exc:
        await ensure_tenant_scope(
            principal,
            "*",
            audit,
            endpoint="GET /v1/agents",
            cross_tenant_enabled=False,
        )
    assert exc.value.status_code == 403
    assert exc.value.detail["code"] == "CROSS_TENANT_DISABLED"  # type: ignore[index]

    rows = list(store._rows.values())
    assert len(rows) == 1
    assert rows[0].action == AuditAction.SYSTEM_CROSS_TENANT_BLOCKED
    assert rows[0].details["mode"] == "aggregate"
    assert rows[0].tenant_id == principal.tenant_id  # home-tenant attribution


@pytest.mark.asyncio
async def test_switch_off_blocks_explicit_tenant_switch_with_audit() -> None:
    store = InMemoryAuditLogStore()
    audit = _audit_for(store)
    principal = _system_admin()
    other = uuid4()

    with pytest.raises(HTTPException) as exc:
        await ensure_tenant_scope(
            principal,
            other,
            audit,
            endpoint="GET /v1/sessions",
            cross_tenant_enabled=False,
        )
    assert exc.value.status_code == 403
    assert exc.value.detail["code"] == "CROSS_TENANT_DISABLED"  # type: ignore[index]

    rows = list(store._rows.values())
    assert len(rows) == 1
    assert rows[0].action == AuditAction.SYSTEM_CROSS_TENANT_BLOCKED
    assert rows[0].details["mode"] == "switch"
    assert rows[0].details["target_tenant"] == str(other)


@pytest.mark.asyncio
async def test_switch_off_home_tenant_access_unaffected() -> None:
    audit = _audit_for(InMemoryAuditLogStore())
    principal = _system_admin()

    result = await ensure_tenant_scope(
        principal,
        principal.tenant_id,
        audit,
        cross_tenant_enabled=False,
    )
    assert isinstance(result, SingleTenant)
    assert result.tenant_id == principal.tenant_id


@pytest.mark.asyncio
async def test_switch_off_plain_user_allowed_tenants_untouched() -> None:
    # A non-system-admin whose allowed_tenants spans two tenants (the mTLS
    # shape) keeps that access — the switch only confines system_admin.
    audit = _audit_for(InMemoryAuditLogStore())
    home, other = uuid4(), uuid4()
    principal = _tenant_user(tenant=home, allowed=(home, other))

    result = await ensure_tenant_scope(
        principal,
        other,
        audit,
        cross_tenant_enabled=False,
    )
    assert isinstance(result, SingleTenant)
    assert result.tenant_id == other
