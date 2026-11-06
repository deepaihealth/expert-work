"""Unit tests for :class:`InMemoryUserUploadStore` —— 附件模型统一(spec 2026-08-17)。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from expert_work.persistence import InMemoryUserUploadStore


@pytest.mark.asyncio
async def test_insert_then_get_same_tenant() -> None:
    store = InMemoryUserUploadStore()
    tenant = uuid4()
    upload_id = uuid4()
    user = uuid4()
    thread = uuid4()
    row = await store.insert(
        upload_id=upload_id,
        tenant_id=tenant,
        user_id=user,
        thread_id=thread,
        kind="document",
        ref="uploads/report.pdf",
        mime_type="application/pdf",
        size_bytes=10,
        filename="report.pdf",
    )
    assert row.id == upload_id
    assert row.kind == "document"
    assert row.deleted_at is None
    assert (await store.get(upload_id=upload_id, tenant_id=tenant)) == row


@pytest.mark.asyncio
async def test_get_other_tenant_is_none() -> None:
    """Cross-tenant probe returns None — never raise."""
    store = InMemoryUserUploadStore()
    upload_id = uuid4()
    await store.insert(
        upload_id=upload_id,
        tenant_id=uuid4(),
        user_id=uuid4(),
        thread_id=uuid4(),
        kind="image",
        ref="expert_work://image/x/y/z.png",
        mime_type="image/png",
        size_bytes=1,
        filename="z.png",
    )
    assert await store.get(upload_id=upload_id, tenant_id=uuid4()) is None


@pytest.mark.asyncio
async def test_get_unknown_is_none() -> None:
    store = InMemoryUserUploadStore()
    assert await store.get(upload_id=uuid4(), tenant_id=uuid4()) is None


@pytest.mark.asyncio
async def test_get_does_not_filter_user() -> None:
    """``get`` only filters ``(id, tenant_id)`` — the caller compares
    ``user_id`` itself (same rule as ``image_upload.get``)."""
    store = InMemoryUserUploadStore()
    tenant = uuid4()
    upload_id = uuid4()
    row = await store.insert(
        upload_id=upload_id,
        tenant_id=tenant,
        user_id=uuid4(),
        thread_id=uuid4(),
        kind="document",
        ref="uploads/other.pdf",
        mime_type="application/pdf",
        size_bytes=1,
        filename="other.pdf",
    )
    # A different user_id still finds the row — get() doesn't filter on it.
    fetched = await store.get(upload_id=upload_id, tenant_id=tenant)
    assert fetched == row
    assert fetched.user_id != uuid4()


@pytest.mark.asyncio
async def test_delete_all_for_user_counts_and_scopes() -> None:
    store = InMemoryUserUploadStore()
    tenant = uuid4()
    other_tenant = uuid4()
    user1 = uuid4()
    user2 = uuid4()

    async def _seed(*, tenant_id: UUID, user_id: UUID) -> None:
        await store.insert(
            upload_id=uuid4(),
            tenant_id=tenant_id,
            user_id=user_id,
            thread_id=uuid4(),
            kind="document",
            ref="uploads/f.pdf",
            mime_type="application/pdf",
            size_bytes=1,
            filename="f.pdf",
        )

    # Two rows each for user1 and user2 in the same tenant.
    await _seed(tenant_id=tenant, user_id=user1)
    await _seed(tenant_id=tenant, user_id=user1)
    user2_id_a = uuid4()
    await store.insert(
        upload_id=user2_id_a,
        tenant_id=tenant,
        user_id=user2,
        thread_id=uuid4(),
        kind="document",
        ref="uploads/f.pdf",
        mime_type="application/pdf",
        size_bytes=1,
        filename="f.pdf",
    )
    user2_id_b = uuid4()
    await store.insert(
        upload_id=user2_id_b,
        tenant_id=tenant,
        user_id=user2,
        thread_id=uuid4(),
        kind="document",
        ref="uploads/f.pdf",
        mime_type="application/pdf",
        size_bytes=1,
        filename="f.pdf",
    )
    # Same user_id, different tenant — must not be touched by the delete below.
    other_tenant_id = uuid4()
    await store.insert(
        upload_id=other_tenant_id,
        tenant_id=other_tenant,
        user_id=user1,
        thread_id=uuid4(),
        kind="document",
        ref="uploads/f.pdf",
        mime_type="application/pdf",
        size_bytes=1,
        filename="f.pdf",
    )

    deleted = await store.delete_all_for_user(tenant_id=tenant, user_id=user1)
    assert deleted == 2

    # User2's rows in the same tenant are untouched.
    assert await store.get(upload_id=user2_id_a, tenant_id=tenant) is not None
    assert await store.get(upload_id=user2_id_b, tenant_id=tenant) is not None
    # The other tenant's row for user1 is untouched.
    assert await store.get(upload_id=other_tenant_id, tenant_id=other_tenant) is not None
    # Re-deleting is a safe no-op.
    assert await store.delete_all_for_user(tenant_id=tenant, user_id=user1) == 0


# ---------------------------------------------------------------------------
# 留存链 —— 上传 90 天:list_expired / soft_delete
# ---------------------------------------------------------------------------


async def _seed_upload(store: InMemoryUserUploadStore, *, tenant: UUID, age_days: int) -> UUID:
    upload_id = uuid4()
    await store.insert(
        upload_id=upload_id,
        tenant_id=tenant,
        user_id=uuid4(),
        thread_id=uuid4(),
        kind="document",
        ref="uploads/f.pdf",
        mime_type="application/pdf",
        size_bytes=1,
        filename="f.pdf",
    )
    row = store._rows[(tenant, upload_id)]
    store._rows[(tenant, upload_id)] = row.model_copy(
        update={"created_at": datetime.now(UTC) - timedelta(days=age_days)}
    )
    return upload_id


@pytest.mark.asyncio
async def test_list_expired_and_soft_delete() -> None:
    store = InMemoryUserUploadStore()
    tenant, other = uuid4(), uuid4()
    old = await _seed_upload(store, tenant=tenant, age_days=120)
    older = await _seed_upload(store, tenant=other, age_days=200)
    fresh = await _seed_upload(store, tenant=tenant, age_days=1)
    now = datetime.now(UTC)

    expired = await store.list_expired(now=now, default_days=90)
    assert [e.upload.id for e in expired] == [older, old]  # cross-tenant, earliest expiry first
    old_row = await store.get(upload_id=old, tenant_id=tenant)
    assert old_row is not None and expired[1].expires_at == old_row.created_at + timedelta(days=90)
    limited = await store.list_expired(now=now, default_days=90, limit=1)
    assert [e.upload.id for e in limited] == [older]

    assert await store.soft_delete(upload_id=old, tenant_id=tenant, now=now) is True
    assert await store.soft_delete(upload_id=old, tenant_id=tenant, now=now) is False  # idempotent
    assert await store.soft_delete(upload_id=old, tenant_id=other, now=now) is False  # wrong tenant
    assert await store.soft_delete(upload_id=uuid4(), tenant_id=tenant, now=now) is False
    row = await store.get(upload_id=old, tenant_id=tenant)
    assert row is not None and row.deleted_at == now
    # Already soft-deleted rows are no longer candidates; the fresh one never was.
    assert [e.upload.id for e in await store.list_expired(now=now, default_days=90)] == [older]
    fresh_row = await store.get(upload_id=fresh, tenant_id=tenant)
    assert fresh_row is not None and fresh_row.deleted_at is None


@pytest.mark.asyncio
async def test_a_recent_use_keeps_an_old_upload() -> None:
    """B-63 —— 保留期从最后一次使用算起:120 天前上传、10 天前用过 → 没到期;到期日跟着
    ``last_used_at`` 走。"""
    store = InMemoryUserUploadStore()
    tenant = uuid4()
    used = await _seed_upload(store, tenant=tenant, age_days=120)
    unused = await _seed_upload(store, tenant=tenant, age_days=120)
    now = datetime.now(UTC)
    ten_days_ago = now - timedelta(days=10)
    assert await store.touch(upload_id=used, tenant_id=tenant, now=ten_days_ago) is True

    expired = await store.list_expired(now=now, default_days=90)
    assert [e.upload.id for e in expired] == [unused]
    later = await store.list_expired(now=now + timedelta(days=81), default_days=90)
    assert {e.upload.id for e in later} == {used, unused}
    by_id = {e.upload.id: e.expires_at for e in later}
    assert by_id[used] == ten_days_ago + timedelta(days=90)


@pytest.mark.asyncio
async def test_touch_writes_at_most_once_a_day_and_never_on_deleted_rows() -> None:
    store = InMemoryUserUploadStore()
    tenant = uuid4()
    upload = await _seed_upload(store, tenant=tenant, age_days=5)
    now = datetime.now(UTC)

    assert await store.touch(upload_id=upload, tenant_id=tenant, now=now) is True
    assert (
        await store.touch(upload_id=upload, tenant_id=tenant, now=now + timedelta(hours=23))
        is False
    )
    row = await store.get(upload_id=upload, tenant_id=tenant)
    assert row is not None and row.last_used_at == now
    next_day = now + timedelta(days=1, minutes=1)
    assert await store.touch(upload_id=upload, tenant_id=tenant, now=next_day) is True
    assert await store.touch(upload_id=upload, tenant_id=uuid4(), now=next_day) is False
    await store.soft_delete(upload_id=upload, tenant_id=tenant, now=next_day)
    later = next_day + timedelta(days=2)
    assert await store.touch(upload_id=upload, tenant_id=tenant, now=later) is False


@pytest.mark.asyncio
async def test_list_documents_for_thread_keeps_this_threads_live_documents() -> None:
    """B-262 P3 —— 迁移按会话找它自己的上传:只要本会话、本租户、文档类、没删的,按上传先后。"""
    store = InMemoryUserUploadStore()
    tenant, thread, user = uuid4(), uuid4(), uuid4()

    async def _put(*, thread_id: UUID, tenant_id: UUID = tenant, kind: str = "document") -> UUID:
        uid = uuid4()
        await store.insert(
            upload_id=uid,
            tenant_id=tenant_id,
            user_id=user,
            thread_id=thread_id,
            kind=kind,  # type: ignore[arg-type]
            ref="uploads/a.pdf" if kind == "document" else "expert_work://image/x/y/z.png",
            mime_type="application/pdf",
            size_bytes=3,
            filename="a.pdf",
        )
        return uid

    first = await _put(thread_id=thread)
    second = await _put(thread_id=thread)
    await _put(thread_id=thread, kind="image")
    await _put(thread_id=uuid4())
    await _put(thread_id=thread, tenant_id=uuid4())
    gone = await _put(thread_id=thread)
    await store.soft_delete(upload_id=gone, tenant_id=tenant, now=datetime.now(UTC))

    rows = await store.list_documents_for_thread(tenant_id=tenant, thread_id=thread)
    assert [r.id for r in rows] == [first, second]


@pytest.mark.asyncio
async def test_insert_records_sha256_and_old_rows_have_none() -> None:
    """B-271 —— 上传时算出的 sha256 落进登记行;不给(老调用点)就是 ``None``。"""
    store = InMemoryUserUploadStore()
    tenant = uuid4()
    common = {
        "tenant_id": tenant,
        "user_id": uuid4(),
        "thread_id": uuid4(),
        "kind": "document",
        "ref": "uploads/a.pdf",
        "mime_type": "application/pdf",
        "size_bytes": 3,
        "filename": "a.pdf",
    }
    hashed = await store.insert(upload_id=uuid4(), sha256="ab" * 32, **common)  # type: ignore[arg-type]
    legacy = await store.insert(upload_id=uuid4(), **common)  # type: ignore[arg-type]
    assert hashed.sha256 == "ab" * 32
    assert legacy.sha256 is None
    fetched = await store.get(upload_id=hashed.id, tenant_id=tenant)
    assert fetched is not None and fetched.sha256 == "ab" * 32


@pytest.mark.asyncio
async def test_list_unexpired_by_ref_matches_tenant_user_and_ref_only_live_rows() -> None:
    """B-271 / B-63 —— 留存作业的引用检查:同租户、同用户、同 ``ref``、没删、没到期的行,不论
    会话。"""
    store = InMemoryUserUploadStore()
    tenant, user = uuid4(), uuid4()

    async def _put(
        *, tenant_id: UUID = tenant, user_id: UUID = user, ref: str = "uploads/a.pdf"
    ) -> UUID:
        uid = uuid4()
        await store.insert(
            upload_id=uid,
            tenant_id=tenant_id,
            user_id=user_id,
            thread_id=uuid4(),
            kind="document",
            ref=ref,
            mime_type="application/pdf",
            size_bytes=3,
            filename="a.pdf",
        )
        return uid

    first = await _put()
    second = await _put()
    await _put(ref="uploads/b.pdf")
    await _put(user_id=uuid4())
    await _put(tenant_id=uuid4())
    gone = await _put()
    await store.soft_delete(upload_id=gone, tenant_id=tenant, now=datetime.now(UTC))
    stale = await _put()
    store._rows[(tenant, stale)] = store._rows[(tenant, stale)].model_copy(
        update={"created_at": datetime.now(UTC) - timedelta(days=100)}
    )

    rows = await store.list_unexpired_by_ref(
        tenant_id=tenant,
        user_id=user,
        ref="uploads/a.pdf",
        now=datetime.now(UTC),
        default_days=90,
    )
    assert sorted(r.id for r in rows) == sorted([first, second])
