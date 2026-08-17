"""Unit tests for :class:`InMemoryUserUploadStore` —— 附件模型统一(spec 2026-08-17)。"""

from __future__ import annotations

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
