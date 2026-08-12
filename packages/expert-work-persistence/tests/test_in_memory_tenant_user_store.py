"""Unit tests for InMemoryTenantUserStore — Stream J.14 contract."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from expert_work.persistence import InMemoryTenantUserStore


@pytest.mark.asyncio
async def test_resolve_creates_then_returns_same_row() -> None:
    store = InMemoryTenantUserStore()
    tenant_id = uuid4()

    first = await store.resolve(tenant_id=tenant_id, subject_type="user", subject_id="oidc-sub-1")
    assert first.tenant_id == tenant_id
    assert first.subject_id == "oidc-sub-1"
    assert first.created_at is not None

    again = await store.resolve(tenant_id=tenant_id, subject_type="user", subject_id="oidc-sub-1")
    # Idempotent: same identity resolves to the same surrogate id.
    assert again.id == first.id
    assert again.created_at == first.created_at


@pytest.mark.asyncio
async def test_resolve_bumps_last_active_at() -> None:
    store = InMemoryTenantUserStore()
    tenant_id = uuid4()
    first = await store.resolve(tenant_id=tenant_id, subject_type="user", subject_id="u")
    again = await store.resolve(tenant_id=tenant_id, subject_type="user", subject_id="u")
    assert again.last_active_at is not None
    assert first.last_active_at is not None
    assert again.last_active_at >= first.last_active_at


@pytest.mark.asyncio
async def test_resolve_display_name_set_then_preserved() -> None:
    store = InMemoryTenantUserStore()
    tenant_id = uuid4()

    await store.resolve(
        tenant_id=tenant_id, subject_type="user", subject_id="u", display_name="Ada"
    )
    # A later resolve with no display_name must not clobber the stored value.
    preserved = await store.resolve(tenant_id=tenant_id, subject_type="user", subject_id="u")
    assert preserved.display_name == "Ada"
    # An explicit new value overwrites.
    renamed = await store.resolve(
        tenant_id=tenant_id, subject_type="user", subject_id="u", display_name="Ada L."
    )
    assert renamed.display_name == "Ada L."


@pytest.mark.asyncio
async def test_resolve_distinguishes_identity_axes() -> None:
    store = InMemoryTenantUserStore()
    tenant_a, tenant_b = uuid4(), uuid4()

    u1 = await store.resolve(tenant_id=tenant_a, subject_type="user", subject_id="x")
    # Different tenant → different user.
    u2 = await store.resolve(tenant_id=tenant_b, subject_type="user", subject_id="x")
    # Different subject_type → different user.
    u3 = await store.resolve(tenant_id=tenant_a, subject_type="service_account", subject_id="x")
    # Different subject_id → different user.
    u4 = await store.resolve(tenant_id=tenant_a, subject_type="user", subject_id="y")

    assert len({u1.id, u2.id, u3.id, u4.id}) == 4


@pytest.mark.asyncio
async def test_get_filters_by_tenant() -> None:
    store = InMemoryTenantUserStore()
    owner, other = uuid4(), uuid4()
    user = await store.resolve(tenant_id=owner, subject_type="user", subject_id="u")

    assert await store.get(user.id, tenant_id=owner) is not None
    assert await store.get(user.id, tenant_id=other) is None
    assert await store.get(uuid4(), tenant_id=owner) is None


@pytest.mark.asyncio
async def test_get_by_subject_hits_then_misses() -> None:
    store = InMemoryTenantUserStore()
    tenant_id = uuid4()
    created = await store.resolve(tenant_id=tenant_id, subject_type="user", subject_id="u-1")

    found = await store.get_by_subject(tenant_id=tenant_id, subject_type="user", subject_id="u-1")
    assert found is not None
    assert found.id == created.id

    assert (
        await store.get_by_subject(
            tenant_id=tenant_id, subject_type="user", subject_id="never-seen"
        )
        is None
    )


@pytest.mark.asyncio
async def test_get_by_subject_is_not_visible_cross_tenant() -> None:
    store = InMemoryTenantUserStore()
    owner, other = uuid4(), uuid4()
    await store.resolve(tenant_id=owner, subject_type="user", subject_id="shared-id")

    # Same subject_id, different tenant → must not be found.
    assert (
        await store.get_by_subject(tenant_id=other, subject_type="user", subject_id="shared-id")
        is None
    )
    found = await store.get_by_subject(tenant_id=owner, subject_type="user", subject_id="shared-id")
    assert found is not None


@pytest.mark.asyncio
async def test_get_by_subject_distinguishes_subject_type() -> None:
    store = InMemoryTenantUserStore()
    tenant_id = uuid4()
    svc = await store.resolve(tenant_id=tenant_id, subject_type="service_account", subject_id="x")

    assert (
        await store.get_by_subject(tenant_id=tenant_id, subject_type="user", subject_id="x") is None
    )
    found = await store.get_by_subject(
        tenant_id=tenant_id, subject_type="service_account", subject_id="x"
    )
    assert found is not None
    assert found.id == svc.id


@pytest.mark.asyncio
async def test_get_by_subject_returns_deactivated_row_like_get() -> None:
    """``get_by_subject`` mirrors ``get``'s ``deleted_at`` semantics — a
    purged row is still returned (not silently dropped like
    ``list_by_tenant``); the caller decides what to do with it."""
    store = InMemoryTenantUserStore()
    tenant_id = uuid4()
    user = await store.resolve(tenant_id=tenant_id, subject_type="user", subject_id="x")
    assert await store.deactivate(user.id, tenant_id=tenant_id, now=datetime.now(UTC)) is True

    found = await store.get_by_subject(tenant_id=tenant_id, subject_type="user", subject_id="x")
    assert found is not None
    assert found.id == user.id
    assert found.deleted_at is not None


@pytest.mark.asyncio
async def test_get_many_batches_and_filters_by_tenant() -> None:
    store = InMemoryTenantUserStore()
    owner, other = uuid4(), uuid4()
    a = await store.resolve(tenant_id=owner, subject_type="user", subject_id="a")
    b = await store.resolve(tenant_id=owner, subject_type="user", subject_id="b")
    foreign = await store.resolve(tenant_id=other, subject_type="user", subject_id="c")

    got = await store.get_many([a.id, b.id, foreign.id, uuid4()], tenant_id=owner)
    # Foreign-tenant + unknown ids are absent, not errors.
    assert set(got) == {a.id, b.id}
    assert got[a.id].subject_id == "a"

    assert await store.get_many([], tenant_id=owner) == {}


@pytest.mark.asyncio
async def test_list_by_tenant_filters_type_and_tenant() -> None:
    store = InMemoryTenantUserStore()
    owner, other = uuid4(), uuid4()
    u1 = await store.resolve(tenant_id=owner, subject_type="user", subject_id="u1")
    u2 = await store.resolve(tenant_id=owner, subject_type="user", subject_id="u2")
    await store.resolve(tenant_id=owner, subject_type="service_account", subject_id="svc")
    await store.resolve(tenant_id=other, subject_type="user", subject_id="foreign")

    # subject_type="user" excludes the service account; the other tenant's
    # user is never returned.
    rows = await store.list_by_tenant(owner, subject_type="user")
    assert {r.id for r in rows} == {u1.id, u2.id}

    # No subject_type filter → both principal kinds in this tenant (3 rows).
    assert len(await store.list_by_tenant(owner)) == 3


@pytest.mark.asyncio
async def test_list_by_tenant_orders_by_last_active_desc() -> None:
    store = InMemoryTenantUserStore()
    tenant = uuid4()
    first = await store.resolve(tenant_id=tenant, subject_type="user", subject_id="first")
    await store.resolve(tenant_id=tenant, subject_type="user", subject_id="second")
    # Re-resolving ``first`` bumps its last_active_at to newest.
    await store.resolve(tenant_id=tenant, subject_type="user", subject_id="first")

    rows = await store.list_by_tenant(tenant, subject_type="user")
    assert rows[0].id == first.id  # most-recently-active first


@pytest.mark.asyncio
async def test_list_by_tenant_paginates() -> None:
    store = InMemoryTenantUserStore()
    tenant = uuid4()
    for i in range(5):
        await store.resolve(tenant_id=tenant, subject_type="user", subject_id=f"u{i}")

    page = await store.list_by_tenant(tenant, subject_type="user", limit=2, offset=2)
    assert len(page) == 2
    full = await store.list_by_tenant(tenant, subject_type="user")
    assert [r.id for r in page] == [r.id for r in full[2:4]]


# ---------------------------------------------------------------------------
# PR1 Task 5 — hard_delete_deactivated() retention sweep
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hard_delete_deactivated_only_reaps_old_deactivated() -> None:
    """Only rows deactivated before the cutoff are physically removed — an
    active row and a recently-deactivated row are both left alone."""
    store = InMemoryTenantUserStore()
    tenant = uuid4()
    active = await store.resolve(tenant_id=tenant, subject_type="user", subject_id="active")
    old = await store.resolve(tenant_id=tenant, subject_type="user", subject_id="old")
    recent = await store.resolve(tenant_id=tenant, subject_type="user", subject_id="recent")

    assert (
        await store.deactivate(
            old.id, tenant_id=tenant, now=datetime.now(UTC) - timedelta(days=100)
        )
        is True
    )
    assert (
        await store.deactivate(
            recent.id, tenant_id=tenant, now=datetime.now(UTC) - timedelta(days=10)
        )
        is True
    )

    cutoff = datetime.now(UTC) - timedelta(days=90)
    assert await store.hard_delete_deactivated(before=cutoff, limit=100) == 1

    assert await store.get(active.id, tenant_id=tenant) is not None
    assert await store.get(old.id, tenant_id=tenant) is None
    got_recent = await store.get(recent.id, tenant_id=tenant)
    assert got_recent is not None
    assert got_recent.deleted_at is not None


@pytest.mark.asyncio
async def test_hard_delete_deactivated_respects_limit() -> None:
    """Two rows are both past the cutoff; ``limit=1`` only physically
    removes the oldest one (``deleted_at`` ascending)."""
    store = InMemoryTenantUserStore()
    tenant = uuid4()
    first = await store.resolve(tenant_id=tenant, subject_type="user", subject_id="first")
    second = await store.resolve(tenant_id=tenant, subject_type="user", subject_id="second")
    assert (
        await store.deactivate(
            first.id, tenant_id=tenant, now=datetime.now(UTC) - timedelta(days=100)
        )
        is True
    )
    assert (
        await store.deactivate(
            second.id, tenant_id=tenant, now=datetime.now(UTC) - timedelta(days=95)
        )
        is True
    )

    cutoff = datetime.now(UTC) - timedelta(days=90)
    assert await store.hard_delete_deactivated(before=cutoff, limit=1) == 1

    assert await store.get(first.id, tenant_id=tenant) is None
    assert await store.get(second.id, tenant_id=tenant) is not None


@pytest.mark.asyncio
async def test_hard_delete_deactivated_sweeps_across_tenants() -> None:
    """``hard_delete_deactivated`` has no tenant predicate — a single call
    reaps expired rows across every tenant."""
    store = InMemoryTenantUserStore()
    tenant_a, tenant_b = uuid4(), uuid4()
    user_a = await store.resolve(tenant_id=tenant_a, subject_type="user", subject_id="a")
    user_b = await store.resolve(tenant_id=tenant_b, subject_type="user", subject_id="b")
    old = datetime.now(UTC) - timedelta(days=100)
    assert await store.deactivate(user_a.id, tenant_id=tenant_a, now=old) is True
    assert await store.deactivate(user_b.id, tenant_id=tenant_b, now=old) is True

    cutoff = datetime.now(UTC) - timedelta(days=90)
    assert await store.hard_delete_deactivated(before=cutoff, limit=100) == 2

    assert await store.get(user_a.id, tenant_id=tenant_a) is None
    assert await store.get(user_b.id, tenant_id=tenant_b) is None


@pytest.mark.asyncio
async def test_hard_delete_deactivated_revival_not_swept() -> None:
    """A deactivated identity that RE-``resolve``s before the sweep runs
    clears ``deleted_at`` (reactivation) — the row must not be swept even
    though it was deactivated long enough ago."""
    store = InMemoryTenantUserStore()
    tenant = uuid4()
    u = await store.resolve(tenant_id=tenant, subject_type="user", subject_id="x")
    old = datetime.now(UTC) - timedelta(days=100)
    assert await store.deactivate(u.id, tenant_id=tenant, now=old) is True

    # The identity returns — resolve() clears deleted_at (reactivation).
    revived = await store.resolve(tenant_id=tenant, subject_type="user", subject_id="x")
    assert revived.id == u.id
    assert revived.deleted_at is None

    cutoff = datetime.now(UTC) - timedelta(days=90)
    assert await store.hard_delete_deactivated(before=cutoff, limit=100) == 0
    assert await store.get(u.id, tenant_id=tenant) is not None
