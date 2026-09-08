"""Unit tests for ``TenantStatusService`` — Stream U (PR E)."""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from control_plane.tenant_status import TenantStatusService
from expert_work.persistence.rls import bypass_rls_var, current_tenant_id_var
from expert_work.persistence.tenant_config import InMemoryTenantConfigStore
from expert_work.protocol import TenantConfigRecord


@pytest.mark.asyncio
async def test_is_suspended_reads_scoped_to_the_tenant_it_asks_about() -> None:
    """``tenant_config`` is FORCE-RLS. The front-door check runs in the auth
    middleware, *outside* ``RLSContextMiddleware`` (it is the outer layer), so
    no tenant context is set yet — unscoped, the read fails closed under RLS
    enforcement, reads "not suspended", and poisons the TTL cache with it.
    The service therefore scopes its own read to the tenant it is asked
    about, and leaves the caller's context exactly as it found it."""
    seen: list[tuple[bool, UUID | None]] = []

    class _RecordingStore(InMemoryTenantConfigStore):
        async def get(self, *, tenant_id: UUID) -> TenantConfigRecord | None:
            seen.append((bypass_rls_var.get(), current_tenant_id_var.get()))
            return await super().get(tenant_id=tenant_id)

    tid = uuid4()
    svc = TenantStatusService(store=_RecordingStore())
    outer_tenant = uuid4()
    token = current_tenant_id_var.set(outer_tenant)
    try:
        assert await svc.is_suspended(tid) is False
        assert current_tenant_id_var.get() == outer_tenant
        assert bypass_rls_var.get() is False
    finally:
        current_tenant_id_var.reset(token)

    assert seen == [(False, tid)]


@pytest.mark.asyncio
async def test_missing_row_reads_as_not_suspended() -> None:
    svc = TenantStatusService(store=InMemoryTenantConfigStore())
    assert await svc.is_suspended(uuid4()) is False


@pytest.mark.asyncio
async def test_suspended_after_set_status() -> None:
    store = InMemoryTenantConfigStore()
    tid = uuid4()
    await store.create(tenant_id=tid, display_name="Acme", actor_id="seed")
    svc = TenantStatusService(store=store)

    assert await svc.is_suspended(tid) is False
    await store.set_status(tenant_id=tid, status="suspended", actor_id="admin")
    svc.invalidate(tid)
    assert await svc.is_suspended(tid) is True


@pytest.mark.asyncio
async def test_cache_hit_within_ttl_does_not_reread() -> None:
    """A stale cached value persists until ``invalidate`` while the clock is frozen."""
    store = InMemoryTenantConfigStore()
    tid = uuid4()
    await store.create(tenant_id=tid, display_name="Acme", actor_id="seed")
    # Frozen clock → TTL never expires within the test.
    svc = TenantStatusService(store=store, ttl_seconds=30.0, clock=lambda: 100.0)

    assert await svc.is_suspended(tid) is False  # caches False
    await store.set_status(tenant_id=tid, status="suspended", actor_id="admin")
    # Cache still holds the stale False (no invalidate, clock frozen).
    assert await svc.is_suspended(tid) is False

    # invalidate forces a re-read → now reflects the suspended state.
    svc.invalidate(tid)
    assert await svc.is_suspended(tid) is True


@pytest.mark.asyncio
async def test_ttl_expiry_rereads() -> None:
    store = InMemoryTenantConfigStore()
    tid = uuid4()
    await store.create(tenant_id=tid, display_name="Acme", actor_id="seed")
    now = {"t": 0.0}
    svc = TenantStatusService(store=store, ttl_seconds=10.0, clock=lambda: now["t"])

    assert await svc.is_suspended(tid) is False
    await store.set_status(tenant_id=tid, status="suspended", actor_id="admin")
    now["t"] = 11.0  # past the TTL → re-reads from store
    assert await svc.is_suspended(tid) is True
