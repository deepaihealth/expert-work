"""Unit tests for :class:`InMemoryImageUploadStore` — Stream J.6.补强-3."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from expert_work.persistence import InMemoryImageUploadStore


@pytest.mark.asyncio
async def test_insert_and_get_round_trip() -> None:
    store = InMemoryImageUploadStore()
    tenant = uuid4()
    image_id = uuid4()
    row = await store.insert(
        image_id=image_id,
        tenant_id=tenant,
        thread_id=uuid4(),
        user_id=uuid4(),
        object_key="tenants/x/uploads/y.png",
        size_bytes=128,
        mime_type="image/png",
        sha256="deadbeef" * 8,
    )
    assert row.id == image_id
    fetched = await store.get(image_id=image_id, tenant_id=tenant)
    assert fetched is not None
    assert fetched.deleted_at is None
    assert fetched.size_bytes == 128


@pytest.mark.asyncio
async def test_get_returns_none_for_other_tenant() -> None:
    """Cross-tenant probe returns None — never raise."""
    store = InMemoryImageUploadStore()
    image_id = uuid4()
    await store.insert(
        image_id=image_id,
        tenant_id=uuid4(),
        thread_id=uuid4(),
        user_id=None,
        object_key="x",
        size_bytes=1,
        mime_type="image/png",
        sha256="x",
    )
    assert await store.get(image_id=image_id, tenant_id=uuid4()) is None


@pytest.mark.asyncio
async def test_soft_delete_flips_deleted_at() -> None:
    store = InMemoryImageUploadStore()
    tenant = uuid4()
    image_id = uuid4()
    await store.insert(
        image_id=image_id,
        tenant_id=tenant,
        thread_id=uuid4(),
        user_id=None,
        object_key="x",
        size_bytes=1,
        mime_type="image/png",
        sha256="x",
    )
    now = datetime.now(UTC)
    assert await store.soft_delete(image_id=image_id, tenant_id=tenant, now=now)
    row = await store.get(image_id=image_id, tenant_id=tenant)
    assert row is not None and row.deleted_at == now
    # Second call is a no-op.
    assert not await store.soft_delete(image_id=image_id, tenant_id=tenant, now=now)


@pytest.mark.asyncio
async def test_list_active_for_thread_excludes_soft_deleted() -> None:
    store = InMemoryImageUploadStore()
    tenant = uuid4()
    thread = uuid4()
    alive_id = uuid4()
    dead_id = uuid4()
    for image_id in (alive_id, dead_id):
        await store.insert(
            image_id=image_id,
            tenant_id=tenant,
            thread_id=thread,
            user_id=None,
            object_key=f"k-{image_id}",
            size_bytes=1,
            mime_type="image/png",
            sha256="x",
        )
    await store.soft_delete(image_id=dead_id, tenant_id=tenant, now=datetime.now(UTC))

    active = await store.list_active_for_thread(tenant_id=tenant, thread_id=thread)
    assert [r.id for r in active] == [alive_id]


@pytest.mark.asyncio
async def test_list_reapable_and_hard_delete() -> None:
    """``list_reapable`` finds rows older than the cutoff regardless of
    soft-delete state; ``hard_delete`` purges them by id."""
    store = InMemoryImageUploadStore()
    tenant = uuid4()
    image_id = uuid4()
    await store.insert(
        image_id=image_id,
        tenant_id=tenant,
        thread_id=uuid4(),
        user_id=None,
        object_key="x",
        size_bytes=1,
        mime_type="image/png",
        sha256="x",
    )

    expired = await store.list_reapable(now=datetime.now(UTC) + timedelta(days=91), default_days=90)
    assert [r.id for r in expired] == [image_id]

    removed = await store.hard_delete(image_ids=[image_id])
    assert removed == 1
    assert await store.get(image_id=image_id, tenant_id=tenant) is None
    # Idempotent — re-deleting reports 0.
    assert await store.hard_delete(image_ids=[image_id]) == 0


@pytest.mark.asyncio
async def test_list_reapable_predicate_covers_old_and_soft_deleted() -> None:
    """``list_reapable`` matches ``(last use + days < now) OR (deleted_at IS
    NOT NULL)`` — a manually soft-deleted row is swept even when it's too
    young to be caught by the age predicate alone (the PR1 fix: hand-deleted
    images no longer wait out the retention window before the sweep reaps
    them)."""
    store = InMemoryImageUploadStore()
    tenant = uuid4()
    now = datetime.now(UTC)
    old_time = now - timedelta(days=200)

    new_active = uuid4()
    old_active = uuid4()
    new_soft_deleted = uuid4()
    old_soft_deleted = uuid4()

    for image_id in (new_active, old_active, new_soft_deleted, old_soft_deleted):
        await store.insert(
            image_id=image_id,
            tenant_id=tenant,
            thread_id=uuid4(),
            user_id=None,
            object_key=f"k-{image_id}",
            size_bytes=1,
            mime_type="image/png",
            sha256="x",
        )

    # Backdate the two "old" rows to before the cutoff; leave the two "new"
    # rows at their just-inserted created_at.
    for image_id in (old_active, old_soft_deleted):
        store._rows[image_id] = store._rows[image_id].model_copy(update={"created_at": old_time})

    # Soft-delete the two "*_soft_deleted" rows (new_soft_deleted stays
    # recent — this is the new behavior under test).
    for image_id in (new_soft_deleted, old_soft_deleted):
        store._rows[image_id] = store._rows[image_id].model_copy(update={"deleted_at": now})

    reapable = await store.list_reapable(now=now, default_days=90)

    reapable_ids = {r.id for r in reapable}
    assert reapable_ids == {old_active, new_soft_deleted, old_soft_deleted}
    assert new_active not in reapable_ids
    # created_at ascending.
    assert [r.created_at for r in reapable] == sorted(r.created_at for r in reapable)


@pytest.mark.asyncio
async def test_list_for_user_includes_soft_deleted_rows_and_scopes_by_tenant_and_user() -> None:
    """Deletion-hygiene purge (Task 8) — ``list_for_user`` must return
    soft-deleted rows too (purge needs their ``object_key`` to clean up the
    blob), and must not leak another tenant's or another user's rows."""
    store = InMemoryImageUploadStore()
    tenant = uuid4()
    user = uuid4()
    active_id = uuid4()
    soft_deleted_id = uuid4()
    other_user_id = uuid4()
    other_tenant_id = uuid4()

    await store.insert(
        image_id=active_id,
        tenant_id=tenant,
        thread_id=uuid4(),
        user_id=user,
        object_key="active",
        size_bytes=1,
        mime_type="image/png",
        sha256="x",
    )
    await store.insert(
        image_id=soft_deleted_id,
        tenant_id=tenant,
        thread_id=uuid4(),
        user_id=user,
        object_key="soft-deleted",
        size_bytes=1,
        mime_type="image/png",
        sha256="x",
    )
    await store.soft_delete(image_id=soft_deleted_id, tenant_id=tenant, now=datetime.now(UTC))
    # Another user in the same tenant — must not appear.
    await store.insert(
        image_id=other_user_id,
        tenant_id=tenant,
        thread_id=uuid4(),
        user_id=uuid4(),
        object_key="other-user",
        size_bytes=1,
        mime_type="image/png",
        sha256="x",
    )
    # Same user id, different tenant — must not appear.
    await store.insert(
        image_id=other_tenant_id,
        tenant_id=uuid4(),
        thread_id=uuid4(),
        user_id=user,
        object_key="other-tenant",
        size_bytes=1,
        mime_type="image/png",
        sha256="x",
    )

    rows = await store.list_for_user(tenant_id=tenant, user_id=user)
    assert {r.id for r in rows} == {active_id, soft_deleted_id}
    # created_at ascending.
    assert [r.created_at for r in rows] == sorted(r.created_at for r in rows)


@pytest.mark.asyncio
async def test_list_for_user_respects_limit() -> None:
    store = InMemoryImageUploadStore()
    tenant = uuid4()
    user = uuid4()
    for _ in range(3):
        await store.insert(
            image_id=uuid4(),
            tenant_id=tenant,
            thread_id=uuid4(),
            user_id=user,
            object_key="k",
            size_bytes=1,
            mime_type="image/png",
            sha256="x",
        )
    rows = await store.list_for_user(tenant_id=tenant, user_id=user, limit=2)
    assert len(rows) == 2


async def _old_image(store: InMemoryImageUploadStore, tenant: UUID, *, age_days: int) -> UUID:
    image_id = uuid4()
    await store.insert(
        image_id=image_id,
        tenant_id=tenant,
        thread_id=uuid4(),
        user_id=None,
        object_key=f"k-{image_id}",
        size_bytes=1,
        mime_type="image/png",
        sha256="x",
    )
    created = datetime.now(UTC) - timedelta(days=age_days)
    store._rows[image_id] = store._rows[image_id].model_copy(update={"created_at": created})
    return image_id


@pytest.mark.asyncio
async def test_a_recent_download_keeps_an_old_image() -> None:
    """B-283 —— 到期按最后使用:200 天前上传、10 天前被下载的图还在保留期里。"""
    store = InMemoryImageUploadStore()
    tenant = uuid4()
    used = await _old_image(store, tenant, age_days=200)
    unused = await _old_image(store, tenant, age_days=200)
    now = datetime.now(UTC)
    assert await store.touch(image_id=used, tenant_id=tenant, now=now - timedelta(days=10))

    reapable = {r.id for r in await store.list_reapable(now=now, default_days=90)}

    assert reapable == {unused}


@pytest.mark.asyncio
async def test_touch_writes_at_most_once_a_day_and_only_for_its_tenant() -> None:
    store = InMemoryImageUploadStore()
    tenant = uuid4()
    image = await _old_image(store, tenant, age_days=5)
    now = datetime.now(UTC)

    assert await store.touch(image_id=image, tenant_id=tenant, now=now) is True
    assert (
        await store.touch(image_id=image, tenant_id=tenant, now=now + timedelta(hours=23)) is False
    )
    next_day = now + timedelta(days=1, minutes=1)
    assert await store.touch(image_id=image, tenant_id=uuid4(), now=next_day) is False
    row = await store.get(image_id=image, tenant_id=tenant)
    assert row is not None and row.last_used_at == now
    assert await store.touch(image_id=image, tenant_id=tenant, now=next_day) is True
    await store.soft_delete(image_id=image, tenant_id=tenant, now=next_day)
    assert (
        await store.touch(image_id=image, tenant_id=tenant, now=next_day + timedelta(days=2))
        is False
    )
