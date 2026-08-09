"""Unit tests for InMemoryUserWorkspaceStore — Stream J.15 contract."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from expert_work.persistence import (
    InMemoryUserWorkspaceStore,
    WorkspaceNotFoundError,
    workspace_volume_name,
)


@pytest.mark.asyncio
async def test_resolve_creates_then_returns_same_row() -> None:
    store = InMemoryUserWorkspaceStore()
    tenant_id, user_id = uuid4(), uuid4()

    first = await store.resolve(tenant_id=tenant_id, user_id=user_id)
    assert first.tenant_id == tenant_id
    assert first.user_id == user_id
    assert first.created_at is not None

    again = await store.resolve(tenant_id=tenant_id, user_id=user_id)
    # Idempotent: same (tenant, user) resolves to the same workspace.
    assert again.id == first.id
    assert again.created_at == first.created_at
    assert again.volume_name == first.volume_name


@pytest.mark.asyncio
async def test_get_returns_none_without_provisioning() -> None:
    # Read-only get — never creates a row (Playground-Uplift D4: a null result
    # truthfully means "no workspace has ever started for this user").
    store = InMemoryUserWorkspaceStore()
    tenant_id, user_id = uuid4(), uuid4()

    assert await store.get(tenant_id=tenant_id, user_id=user_id) is None
    created = await store.resolve(tenant_id=tenant_id, user_id=user_id)
    fetched = await store.get(tenant_id=tenant_id, user_id=user_id)
    assert fetched is not None
    assert fetched.id == created.id


@pytest.mark.asyncio
async def test_resolve_volume_name_is_deterministic() -> None:
    store = InMemoryUserWorkspaceStore()
    tenant_id, user_id = uuid4(), uuid4()
    workspace = await store.resolve(tenant_id=tenant_id, user_id=user_id)
    assert workspace.volume_name == workspace_volume_name(tenant_id, user_id)


@pytest.mark.asyncio
async def test_resolve_bumps_last_accessed_at() -> None:
    store = InMemoryUserWorkspaceStore()
    tenant_id, user_id = uuid4(), uuid4()
    first = await store.resolve(tenant_id=tenant_id, user_id=user_id)
    again = await store.resolve(tenant_id=tenant_id, user_id=user_id)
    assert first.last_accessed_at is not None
    assert again.last_accessed_at is not None
    assert again.last_accessed_at >= first.last_accessed_at


@pytest.mark.asyncio
async def test_resolve_distinguishes_tenant_and_user() -> None:
    store = InMemoryUserWorkspaceStore()
    tenant_a, tenant_b = uuid4(), uuid4()
    user_x, user_y = uuid4(), uuid4()

    w1 = await store.resolve(tenant_id=tenant_a, user_id=user_x)
    # Same tenant, different user → different workspace.
    w2 = await store.resolve(tenant_id=tenant_a, user_id=user_y)
    # Different tenant, same user → different workspace.
    w3 = await store.resolve(tenant_id=tenant_b, user_id=user_x)

    assert len({w1.id, w2.id, w3.id}) == 3
    assert len({w1.volume_name, w2.volume_name, w3.volume_name}) == 3


# --- J.15-补强-1 (Mini-ADR J-29 + J-36) -----------------------------------


@pytest.mark.asyncio
async def test_resolve_defaults_size_limit_bytes_to_10_gib() -> None:
    store = InMemoryUserWorkspaceStore()
    workspace = await store.resolve(tenant_id=uuid4(), user_id=uuid4())
    # Matches migration 0026 server_default + SandboxSupervisorSettings default.
    assert workspace.size_limit_bytes == 10 * 1024 * 1024 * 1024


@pytest.mark.asyncio
async def test_update_size_persists_latest_measurement() -> None:
    store = InMemoryUserWorkspaceStore()
    workspace = await store.resolve(tenant_id=uuid4(), user_id=uuid4())
    assert workspace.size_bytes == 0

    await store.update_size(workspace_id=workspace.id, size_bytes=12345)

    refreshed = await store.resolve(tenant_id=workspace.tenant_id, user_id=workspace.user_id)
    assert refreshed.size_bytes == 12345


@pytest.mark.asyncio
async def test_update_size_raises_when_workspace_missing() -> None:
    store = InMemoryUserWorkspaceStore()
    with pytest.raises(WorkspaceNotFoundError):
        await store.update_size(workspace_id=uuid4(), size_bytes=42)


@pytest.mark.asyncio
async def test_add_size_accumulates() -> None:
    store = InMemoryUserWorkspaceStore()
    tenant_id, user_id = uuid4(), uuid4()
    workspace = await store.resolve(tenant_id=tenant_id, user_id=user_id)

    await store.add_size(workspace_id=workspace.id, delta_bytes=100)
    await store.add_size(workspace_id=workspace.id, delta_bytes=50)

    got = await store.get(tenant_id=tenant_id, user_id=user_id)
    assert got is not None and got.size_bytes == 150


@pytest.mark.asyncio
async def test_add_size_floors_at_zero() -> None:
    store = InMemoryUserWorkspaceStore()
    tenant_id, user_id = uuid4(), uuid4()
    workspace = await store.resolve(tenant_id=tenant_id, user_id=user_id)

    await store.add_size(workspace_id=workspace.id, delta_bytes=-999)

    got = await store.get(tenant_id=tenant_id, user_id=user_id)
    assert got is not None and got.size_bytes == 0


@pytest.mark.asyncio
async def test_soft_delete_sets_deleted_at_and_is_idempotent() -> None:
    store = InMemoryUserWorkspaceStore()
    workspace = await store.resolve(tenant_id=uuid4(), user_id=uuid4())
    assert workspace.deleted_at is None

    first_ts = datetime.now(UTC)
    await store.soft_delete(workspace_id=workspace.id, now=first_ts)

    after_first = await store.resolve(tenant_id=workspace.tenant_id, user_id=workspace.user_id)
    assert after_first.deleted_at == first_ts

    # Second soft_delete keeps the original timestamp.
    second_ts = datetime.now(UTC)
    await store.soft_delete(workspace_id=workspace.id, now=second_ts)
    after_second = await store.resolve(tenant_id=workspace.tenant_id, user_id=workspace.user_id)
    assert after_second.deleted_at == first_ts


@pytest.mark.asyncio
async def test_resolve_returns_soft_deleted_row_without_bumping_last_accessed() -> None:
    store = InMemoryUserWorkspaceStore()
    workspace = await store.resolve(tenant_id=uuid4(), user_id=uuid4())
    await store.soft_delete(workspace_id=workspace.id, now=datetime.now(UTC))
    after_delete = await store.resolve(tenant_id=workspace.tenant_id, user_id=workspace.user_id)

    # Caller can still inspect the row, but last_accessed_at is frozen
    # (soft-deleted rows are read-only on the resolve path; Mini-ADR J-36).
    later = await store.resolve(tenant_id=workspace.tenant_id, user_id=workspace.user_id)
    assert later.last_accessed_at == after_delete.last_accessed_at
    assert later.deleted_at is not None


@pytest.mark.asyncio
async def test_mark_archived_requires_soft_delete_first() -> None:
    store = InMemoryUserWorkspaceStore()
    workspace = await store.resolve(tenant_id=uuid4(), user_id=uuid4())

    with pytest.raises(ValueError, match="soft-deleted"):
        await store.mark_archived(workspace_id=workspace.id, archived_object_key="key")


@pytest.mark.asyncio
async def test_mark_archived_records_object_key() -> None:
    store = InMemoryUserWorkspaceStore()
    workspace = await store.resolve(tenant_id=uuid4(), user_id=uuid4())
    await store.soft_delete(workspace_id=workspace.id, now=datetime.now(UTC))

    await store.mark_archived(
        workspace_id=workspace.id,
        archived_object_key="volume-archive/t/u/v.tar.zst",
    )

    refreshed = await store.resolve(tenant_id=workspace.tenant_id, user_id=workspace.user_id)
    assert refreshed.archived_object_key == "volume-archive/t/u/v.tar.zst"


@pytest.mark.asyncio
async def test_list_pending_archive_filters_correctly() -> None:
    store = InMemoryUserWorkspaceStore()
    tenant = uuid4()
    active = await store.resolve(tenant_id=tenant, user_id=uuid4())
    pending = await store.resolve(tenant_id=tenant, user_id=uuid4())
    archived = await store.resolve(tenant_id=tenant, user_id=uuid4())

    await store.soft_delete(workspace_id=pending.id, now=datetime.now(UTC))
    await store.soft_delete(workspace_id=archived.id, now=datetime.now(UTC))
    await store.mark_archived(workspace_id=archived.id, archived_object_key="k")

    rows = await store.list_pending_archive()
    ids = {r.id for r in rows}
    # Only pending: not active (no deleted_at) and not archived (has archived_object_key).
    assert ids == {pending.id}
    assert active.id not in ids
    assert archived.id not in ids


@pytest.mark.asyncio
async def test_list_active_returns_only_undeleted_rows() -> None:
    """Stream J.15-补强-2 — daily backup sweep reads this list."""
    store = InMemoryUserWorkspaceStore()
    tenant = uuid4()
    active = await store.resolve(tenant_id=tenant, user_id=uuid4())
    soft_deleted = await store.resolve(tenant_id=tenant, user_id=uuid4())
    await store.soft_delete(workspace_id=soft_deleted.id, now=datetime.now(UTC))

    rows = await store.list_active()
    ids = {r.id for r in rows}
    assert ids == {active.id}
    assert soft_deleted.id not in ids


# --- Phase 3b 地基 (list_archived_expired + hard_delete) -------------------


@pytest.mark.asyncio
async def test_list_archived_expired_filters_correctly() -> None:
    store = InMemoryUserWorkspaceStore()
    tenant = uuid4()
    now = datetime.now(UTC)
    before = now - timedelta(days=90)

    active = await store.resolve(tenant_id=tenant, user_id=uuid4())

    hit = await store.resolve(tenant_id=tenant, user_id=uuid4())
    await store.soft_delete(workspace_id=hit.id, now=now - timedelta(days=100))
    await store.mark_archived(workspace_id=hit.id, archived_object_key="k-hit")

    stuck = await store.resolve(tenant_id=tenant, user_id=uuid4())
    await store.soft_delete(workspace_id=stuck.id, now=now - timedelta(days=100))
    # Archive job hasn't run yet — archived_object_key still None.

    recent = await store.resolve(tenant_id=tenant, user_id=uuid4())
    await store.soft_delete(workspace_id=recent.id, now=now - timedelta(days=10))
    await store.mark_archived(workspace_id=recent.id, archived_object_key="k-recent")

    rows = await store.list_archived_expired(before=before)
    ids = {r.id for r in rows}

    assert ids == {hit.id}
    assert active.id not in ids
    assert stuck.id not in ids
    assert recent.id not in ids


@pytest.mark.asyncio
async def test_list_archived_expired_orders_by_deleted_at_ascending() -> None:
    store = InMemoryUserWorkspaceStore()
    tenant = uuid4()
    now = datetime.now(UTC)

    newer = await store.resolve(tenant_id=tenant, user_id=uuid4())
    await store.soft_delete(workspace_id=newer.id, now=now - timedelta(days=100))
    await store.mark_archived(workspace_id=newer.id, archived_object_key="k1")

    older = await store.resolve(tenant_id=tenant, user_id=uuid4())
    await store.soft_delete(workspace_id=older.id, now=now - timedelta(days=200))
    await store.mark_archived(workspace_id=older.id, archived_object_key="k2")

    rows = await store.list_archived_expired(before=now - timedelta(days=90))
    assert [r.id for r in rows] == [older.id, newer.id]


@pytest.mark.asyncio
async def test_list_archived_expired_respects_limit() -> None:
    store = InMemoryUserWorkspaceStore()
    tenant = uuid4()
    now = datetime.now(UTC)

    ids_oldest_first = []
    for days_ago in (300, 200, 100):
        workspace = await store.resolve(tenant_id=tenant, user_id=uuid4())
        await store.soft_delete(workspace_id=workspace.id, now=now - timedelta(days=days_ago))
        await store.mark_archived(workspace_id=workspace.id, archived_object_key="k")
        ids_oldest_first.append(workspace.id)

    rows = await store.list_archived_expired(before=now - timedelta(days=90), limit=2)
    assert [r.id for r in rows] == ids_oldest_first[:2]


@pytest.mark.asyncio
async def test_hard_delete_removes_row_and_is_idempotent() -> None:
    store = InMemoryUserWorkspaceStore()
    workspace = await store.resolve(tenant_id=uuid4(), user_id=uuid4())

    assert await store.hard_delete(workspace_id=workspace.id) is True
    assert await store.get(tenant_id=workspace.tenant_id, user_id=workspace.user_id) is None

    # Row is truly gone — a second delete is a no-op, not an error.
    assert await store.hard_delete(workspace_id=workspace.id) is False


@pytest.mark.asyncio
async def test_hard_delete_returns_false_for_unknown_id() -> None:
    store = InMemoryUserWorkspaceStore()
    assert await store.hard_delete(workspace_id=uuid4()) is False
