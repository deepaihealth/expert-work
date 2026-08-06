"""Unit tests for :class:`RetentionCleanupJob` construction + CleanupReport."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from expert_work.persistence import (
    InMemoryArtifactStore,
    InMemoryImageUploadStore,
    InMemoryMemoryStore,
    InMemoryTenantUserStore,
    InMemoryUserWorkspaceStore,
)
from expert_work.protocol import MemoryItem
from expert_work.runtime.storage import InMemoryObjectStore
from retention_cleanup_job.job import CleanupReport, RetentionCleanupJob


def test_cleanup_report_default_is_all_zero() -> None:
    report = CleanupReport()
    assert report.audit_deleted == 0
    assert report.audit_skipped_unacked == 0
    assert report.event_deleted == 0
    assert report.jwt_blacklist_deleted == 0
    assert report.image_uploads_hard_deleted == 0
    assert report.image_object_keys_removed == 0
    assert report.image_object_keys_failed == 0
    assert report.artifacts_soft_deleted == 0
    assert report.artifacts_hard_deleted == 0
    assert report.approvals_timed_out == 0
    assert report.memory_hard_deleted == 0
    assert report.workspaces_hard_deleted == 0
    assert report.workspace_archives_removed == 0
    assert report.workspace_archives_failed == 0
    assert report.workspaces_pending_archive == 0
    assert report.tenant_users_hard_deleted == 0
    assert report.sandbox_egress_audit_deleted == 0
    assert report.duration_seconds == 0.0
    assert report.audit_deleted_by_tenant == {}


def test_job_rejects_non_positive_batch_size() -> None:
    """``batch_size <= 0`` is a programmer error — surface early."""
    with pytest.raises(ValueError, match="batch_size"):
        RetentionCleanupJob(
            db_session_factory=lambda: None,  # type: ignore[arg-type]
            batch_size=0,
        )


def test_job_rejects_non_positive_image_retention_days() -> None:
    with pytest.raises(ValueError, match="image_retention_days"):
        RetentionCleanupJob(
            db_session_factory=lambda: None,  # type: ignore[arg-type]
            image_retention_days=0,
        )


def test_job_rejects_non_positive_artifact_retention_days() -> None:
    with pytest.raises(ValueError, match="artifact_retention_days"):
        RetentionCleanupJob(
            db_session_factory=lambda: None,  # type: ignore[arg-type]
            artifact_retention_days=0,
        )


def test_job_rejects_non_positive_artifact_hard_delete_grace_days() -> None:
    with pytest.raises(ValueError, match="artifact_hard_delete_grace_days"):
        RetentionCleanupJob(
            db_session_factory=lambda: None,  # type: ignore[arg-type]
            artifact_hard_delete_grace_days=0,
        )


def test_job_rejects_non_positive_memory_hard_delete_grace_days() -> None:
    with pytest.raises(ValueError, match="memory_hard_delete_grace_days"):
        RetentionCleanupJob(
            db_session_factory=lambda: None,  # type: ignore[arg-type]
            memory_hard_delete_grace_days=0,
        )


def test_job_rejects_non_positive_workspace_archive_retention_days() -> None:
    with pytest.raises(ValueError, match="workspace_archive_retention_days"):
        RetentionCleanupJob(
            db_session_factory=lambda: None,  # type: ignore[arg-type]
            workspace_archive_retention_days=0,
        )


def test_job_rejects_non_positive_tenant_user_hard_delete_grace_days() -> None:
    with pytest.raises(ValueError, match="tenant_user_hard_delete_grace_days"):
        RetentionCleanupJob(
            db_session_factory=lambda: None,  # type: ignore[arg-type]
            tenant_user_hard_delete_grace_days=0,
        )


# ---------------------------------------------------------------------------
# Mini-ADR J-32 (J.6.补强-3b) — image retention sweep
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_expired_images_purges_old_rows_and_object_keys() -> None:
    """Rows older than ``image_retention_days`` get their object key
    removed + are hard-deleted from the registry."""
    images = InMemoryImageUploadStore()
    object_store = InMemoryObjectStore()
    tenant = uuid4()

    # An old row (created_at well past the cutoff).
    old_id = uuid4()
    old_key = "tenants/x/uploads/old.png"
    await object_store.put(old_key, b"OLD", content_type="image/png")
    await images.insert(
        image_id=old_id,
        tenant_id=tenant,
        thread_id=uuid4(),
        user_id=None,
        object_key=old_key,
        size_bytes=3,
        mime_type="image/png",
        sha256="x",
    )
    # Backdate the row to before the retention horizon.
    images._rows[old_id] = images._rows[old_id].model_copy(
        update={"created_at": datetime.now(UTC) - timedelta(days=200)},
    )

    # A fresh row (must stay).
    fresh_id = uuid4()
    fresh_key = "tenants/x/uploads/fresh.png"
    await object_store.put(fresh_key, b"FRESH", content_type="image/png")
    await images.insert(
        image_id=fresh_id,
        tenant_id=tenant,
        thread_id=uuid4(),
        user_id=None,
        object_key=fresh_key,
        size_bytes=5,
        mime_type="image/png",
        sha256="y",
    )

    job = RetentionCleanupJob(
        db_session_factory=lambda: None,  # type: ignore[arg-type]
        image_upload_store=images,
        object_store=object_store,
        image_retention_days=90,
    )

    rows, keys_ok, keys_failed = await job._delete_expired_images()

    assert rows == 1
    assert keys_ok == 1
    assert keys_failed == 0
    from expert_work.runtime.storage.base import ObjectNotFoundError

    # Old row + key gone.
    assert await images.get(image_id=old_id, tenant_id=tenant) is None
    with pytest.raises(ObjectNotFoundError):
        await object_store.get(old_key)
    # Fresh row + key remain.
    assert await images.get(image_id=fresh_id, tenant_id=tenant) is not None
    assert await object_store.get(fresh_key) == b"FRESH"


@pytest.mark.asyncio
async def test_delete_expired_images_continues_on_object_store_failure() -> None:
    """A failed object-store delete is tallied + logged — the row is
    still hard-deleted (orphaned key < stuck row whose bytes never go away)."""

    images = InMemoryImageUploadStore()
    tenant = uuid4()
    old_id = uuid4()
    await images.insert(
        image_id=old_id,
        tenant_id=tenant,
        thread_id=uuid4(),
        user_id=None,
        object_key="tenants/x/uploads/old.png",
        size_bytes=3,
        mime_type="image/png",
        sha256="x",
    )
    images._rows[old_id] = images._rows[old_id].model_copy(
        update={"created_at": datetime.now(UTC) - timedelta(days=200)},
    )

    class _FailingStore:
        async def delete(self, key: str) -> None:
            raise RuntimeError("boom")

        async def put(self, *args: object, **kwargs: object) -> None:
            return None

        async def get(self, key: str) -> bytes | None:
            return None

        async def list_prefix(self, prefix: str) -> list[str]:
            return []

    job = RetentionCleanupJob(
        db_session_factory=lambda: None,  # type: ignore[arg-type]
        image_upload_store=images,
        object_store=_FailingStore(),  # type: ignore[arg-type]
        image_retention_days=90,
    )

    rows, keys_ok, keys_failed = await job._delete_expired_images()
    assert rows == 1
    assert keys_ok == 0
    assert keys_failed == 1
    assert await images.get(image_id=old_id, tenant_id=tenant) is None


@pytest.mark.asyncio
async def test_delete_expired_images_noop_without_stores() -> None:
    """Job constructed without image stores skips the image pass cleanly."""
    job = RetentionCleanupJob(
        db_session_factory=lambda: None,  # type: ignore[arg-type]
    )
    rows, keys_ok, keys_failed = await job._delete_expired_images()
    assert (rows, keys_ok, keys_failed) == (0, 0, 0)


# ---------------------------------------------------------------------------
# Mini-ADR J-25 (J.9-step1) — artifact lifecycle sweep
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sweep_artifacts_noop_without_store() -> None:
    """Job constructed without ArtifactStore skips the artifact pass cleanly."""
    job = RetentionCleanupJob(
        db_session_factory=lambda: None,  # type: ignore[arg-type]
    )
    soft, hard = await job._sweep_artifacts()
    assert (soft, hard) == (0, 0)


@pytest.mark.asyncio
async def test_sweep_artifacts_soft_deletes_stale_active_rows() -> None:
    """Active rows past ``artifact_retention_days`` get soft-deleted."""
    artifacts = InMemoryArtifactStore()
    tenant, user = uuid4(), uuid4()
    await artifacts.save_version(
        tenant_id=tenant,
        user_id=user,
        name="stale.md",
        kind="document",
        path_in_workspace="stale.md",
        created_in_thread="t-1",
    )
    # Backdate ``updated_at`` to before the retention horizon.
    stale = (await artifacts.list_for_user(tenant_id=tenant, user_id=user))[0]
    artifacts._artifacts[stale.id] = stale.model_copy(
        update={"updated_at": datetime.now(UTC) - timedelta(days=120)}
    )
    # And a fresh row that must survive.
    await artifacts.save_version(
        tenant_id=tenant,
        user_id=user,
        name="fresh.md",
        kind="document",
        path_in_workspace="fresh.md",
        created_in_thread="t-2",
    )

    job = RetentionCleanupJob(
        db_session_factory=lambda: None,  # type: ignore[arg-type]
        artifact_store=artifacts,
        artifact_retention_days=90,
    )
    soft, hard = await job._sweep_artifacts()
    assert (soft, hard) == (1, 0)
    # Only ``fresh.md`` remains in the default (non-deleted) listing.
    active = await artifacts.list_for_user(tenant_id=tenant, user_id=user)
    assert [a.name for a in active] == ["fresh.md"]


@pytest.mark.asyncio
async def test_sweep_artifacts_hard_deletes_expired_soft_deleted_rows() -> None:
    """Soft-deleted rows past the hard-delete grace are removed entirely."""
    artifacts = InMemoryArtifactStore()
    tenant, user = uuid4(), uuid4()
    await artifacts.save_version(
        tenant_id=tenant,
        user_id=user,
        name="old.md",
        kind="document",
        path_in_workspace="old.md",
        created_in_thread="t-1",
    )
    # Soft-delete with a backdated timestamp past the grace window.
    long_ago = datetime.now(UTC) - timedelta(days=120)
    await artifacts.soft_delete(tenant_id=tenant, user_id=user, name="old.md", now=long_ago)

    job = RetentionCleanupJob(
        db_session_factory=lambda: None,  # type: ignore[arg-type]
        artifact_store=artifacts,
        artifact_hard_delete_grace_days=60,
    )
    soft, hard = await job._sweep_artifacts()
    # Active sweep finds nothing; hard sweep clears the soft-deleted row.
    assert (soft, hard) == (0, 1)
    # No row should remain even with include_deleted=True.
    assert await artifacts.list_for_user(tenant_id=tenant, user_id=user, include_deleted=True) == []


@pytest.mark.asyncio
async def test_sweep_artifacts_skips_recent_soft_deleted_rows() -> None:
    """Recent soft-deletes (within the grace window) survive the sweep."""
    artifacts = InMemoryArtifactStore()
    tenant, user = uuid4(), uuid4()
    await artifacts.save_version(
        tenant_id=tenant,
        user_id=user,
        name="recent.md",
        kind="document",
        path_in_workspace="recent.md",
        created_in_thread="t-1",
    )
    # Soft-delete only 10 days ago — must stay.
    recent = datetime.now(UTC) - timedelta(days=10)
    await artifacts.soft_delete(tenant_id=tenant, user_id=user, name="recent.md", now=recent)

    job = RetentionCleanupJob(
        db_session_factory=lambda: None,  # type: ignore[arg-type]
        artifact_store=artifacts,
        artifact_hard_delete_grace_days=60,
    )
    soft, hard = await job._sweep_artifacts()
    assert (soft, hard) == (0, 0)
    # Row still in include_deleted listing.
    deleted = await artifacts.list_for_user(tenant_id=tenant, user_id=user, include_deleted=True)
    assert len(deleted) == 1


# ---------------------------------------------------------------------------
# Mini-ADR J-24 (J.8-step3b) — approval timeout sweep
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sweep_approval_timeouts_noop_without_store() -> None:
    """Job constructed without ApprovalStore skips the approval pass cleanly."""
    job = RetentionCleanupJob(
        db_session_factory=lambda: None,  # type: ignore[arg-type]
    )
    assert await job._sweep_approval_timeouts() == 0


@pytest.mark.asyncio
async def test_sweep_approval_timeouts_auto_rejects_expired_pending() -> None:
    """Pending rows past ``timeout_at`` flip to TIMEOUT; fresh ones survive."""
    from expert_work.persistence import InMemoryApprovalStore
    from expert_work.protocol import ApprovalRecord, ApprovalStatus

    approvals = InMemoryApprovalStore()
    now = datetime.now(UTC)

    def _rec(*, run_id: object, timeout_at: datetime) -> ApprovalRecord:
        return ApprovalRecord(
            id=uuid4(),
            tenant_id=uuid4(),
            run_id=run_id,  # type: ignore[arg-type]
            thread_id=uuid4(),
            request_id="approval:x",
            node="tools",
            reason_kind="policy_gate",
            action_summary="gated tool",
            requested_at=now - timedelta(hours=30),
            timeout_at=timeout_at,
        )

    expired_run = uuid4()
    fresh_run = uuid4()
    await approvals.create(_rec(run_id=expired_run, timeout_at=now - timedelta(hours=6)))
    await approvals.create(_rec(run_id=fresh_run, timeout_at=now + timedelta(hours=6)))

    job = RetentionCleanupJob(
        db_session_factory=lambda: None,  # type: ignore[arg-type]
        approval_store=approvals,
    )
    timed_out = await job._sweep_approval_timeouts()
    assert timed_out == 1

    expired = await approvals.get_by_run(
        run_id=expired_run, tenant_id=(await _tenant_of(approvals, expired_run))
    )
    assert expired is not None
    assert expired.status is ApprovalStatus.TIMEOUT
    assert expired.decided_by == "system"
    fresh = await approvals.get_by_run(
        run_id=fresh_run, tenant_id=(await _tenant_of(approvals, fresh_run))
    )
    assert fresh is not None
    assert fresh.status is ApprovalStatus.PENDING


async def _tenant_of(store: object, run_id: object) -> object:
    """Test helper — pull a seeded record's tenant_id back out."""
    rows = store._rows  # type: ignore[attr-defined]
    return rows[run_id].tenant_id


# ---------------------------------------------------------------------------
# Deletion hygiene PR1 (Task 7) — memory hard-delete sweep
# ---------------------------------------------------------------------------


def _memory_item(*, tenant: object, user: object, content: str = "c") -> MemoryItem:
    return MemoryItem(
        id=uuid4(),
        tenant_id=tenant,  # type: ignore[arg-type]
        user_id=user,  # type: ignore[arg-type]
        kind="fact",
        content=content,
        embedding=(1.0, 0.0),
    )


@pytest.mark.asyncio
async def test_sweep_memory_hard_deletes_only_old_soft_deleted() -> None:
    """A row soft-deleted 100 days ago is reaped; one soft-deleted only
    10 days ago is left alone (still inside the grace window)."""
    store = InMemoryMemoryStore()
    tenant, user = uuid4(), uuid4()
    old = _memory_item(tenant=tenant, user=user, content="old")
    recent = _memory_item(tenant=tenant, user=user, content="recent")
    await store.write([old, recent])
    assert await store.soft_delete(tenant_id=tenant, user_id=user, memory_id=old.id)
    assert await store.soft_delete(tenant_id=tenant, user_id=user, memory_id=recent.id)
    now = datetime.now(UTC)
    for idx, row in enumerate(store._rows):
        if row.id == old.id:
            store._rows[idx] = row.model_copy(update={"deleted_at": now - timedelta(days=100)})
        elif row.id == recent.id:
            store._rows[idx] = row.model_copy(update={"deleted_at": now - timedelta(days=10)})

    job = RetentionCleanupJob(
        db_session_factory=lambda: None,  # type: ignore[arg-type]
        memory_store=store,
        memory_hard_delete_grace_days=90,
    )
    assert await job._sweep_memory() == 1
    remaining_ids = {row.id for row in store._rows}
    assert remaining_ids == {recent.id}


@pytest.mark.asyncio
async def test_sweep_memory_noop_without_store() -> None:
    """Job constructed without a MemoryStore skips the memory pass cleanly."""
    job = RetentionCleanupJob(
        db_session_factory=lambda: None,  # type: ignore[arg-type]
    )
    assert await job._sweep_memory() == 0


# ---------------------------------------------------------------------------
# Deletion hygiene PR1 (Task 7) — tenant_user hard-delete sweep
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sweep_tenant_users_hard_deletes_only_old_deactivated() -> None:
    """A user deactivated 100 days ago is reaped; one deactivated only 10
    days ago is left alone (still inside the grace window)."""
    store = InMemoryTenantUserStore()
    tenant = uuid4()
    old = await store.resolve(tenant_id=tenant, subject_type="user", subject_id="old")
    recent = await store.resolve(tenant_id=tenant, subject_type="user", subject_id="recent")
    now = datetime.now(UTC)
    assert await store.deactivate(old.id, tenant_id=tenant, now=now - timedelta(days=100))
    assert await store.deactivate(recent.id, tenant_id=tenant, now=now - timedelta(days=10))

    job = RetentionCleanupJob(
        db_session_factory=lambda: None,  # type: ignore[arg-type]
        tenant_user_store=store,
        tenant_user_hard_delete_grace_days=90,
    )
    assert await job._sweep_tenant_users() == 1
    assert await store.get(old.id, tenant_id=tenant) is None
    assert await store.get(recent.id, tenant_id=tenant) is not None


@pytest.mark.asyncio
async def test_sweep_tenant_users_noop_without_store() -> None:
    """Job constructed without a TenantUserStore skips the pass cleanly."""
    job = RetentionCleanupJob(
        db_session_factory=lambda: None,  # type: ignore[arg-type]
    )
    assert await job._sweep_tenant_users() == 0


# ---------------------------------------------------------------------------
# Deletion hygiene PR1 (Task 7) — workspace archive hard-delete sweep
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sweep_workspaces_removes_archive_key_and_hard_deletes_row() -> None:
    """An archived row past retention gets its ObjectStore key removed and
    the row physically deleted."""
    store = InMemoryUserWorkspaceStore()
    object_store = InMemoryObjectStore()
    tenant = uuid4()
    now = datetime.now(UTC)

    ws = await store.resolve(tenant_id=tenant, user_id=uuid4())
    await store.soft_delete(workspace_id=ws.id, now=now - timedelta(days=100))
    await store.mark_archived(workspace_id=ws.id, archived_object_key="ws-archives/x.tar.zst")
    await object_store.put("ws-archives/x.tar.zst", b"ARCHIVE")

    job = RetentionCleanupJob(
        db_session_factory=lambda: None,  # type: ignore[arg-type]
        workspace_store=store,
        object_store=object_store,
        workspace_archive_retention_days=90,
    )
    hard, keys_ok, keys_failed, pending = await job._sweep_workspaces()
    assert (hard, keys_ok, keys_failed, pending) == (1, 1, 0, 0)
    assert await store.get(tenant_id=tenant, user_id=ws.user_id) is None
    from expert_work.runtime.storage.base import ObjectNotFoundError

    with pytest.raises(ObjectNotFoundError):
        await object_store.get("ws-archives/x.tar.zst")


@pytest.mark.asyncio
async def test_sweep_workspaces_keeps_row_when_key_delete_fails() -> None:
    """A real object-store failure keeps the row (it's the only lookup
    back to the orphaned key) and is tallied as failed — the opposite
    tradeoff from the image-upload pass."""
    store = InMemoryUserWorkspaceStore()
    tenant = uuid4()
    now = datetime.now(UTC)
    ws = await store.resolve(tenant_id=tenant, user_id=uuid4())
    await store.soft_delete(workspace_id=ws.id, now=now - timedelta(days=100))
    await store.mark_archived(workspace_id=ws.id, archived_object_key="ws-archives/y.tar.zst")

    class _FailingStore:
        async def delete(self, key: str) -> None:
            raise RuntimeError("boom")

        async def put(self, *args: object, **kwargs: object) -> None:
            return None

        async def get(self, key: str) -> bytes | None:
            return None

        async def list_prefix(self, prefix: str) -> list[str]:
            return []

    job = RetentionCleanupJob(
        db_session_factory=lambda: None,  # type: ignore[arg-type]
        workspace_store=store,
        object_store=_FailingStore(),  # type: ignore[arg-type]
        workspace_archive_retention_days=90,
    )
    hard, keys_ok, keys_failed, pending = await job._sweep_workspaces()
    assert (hard, keys_ok, keys_failed, pending) == (0, 0, 1, 0)
    # Row survives — it's the only remaining lookup back to the key.
    assert await store.get(tenant_id=tenant, user_id=ws.user_id) is not None


@pytest.mark.asyncio
async def test_sweep_workspaces_treats_missing_key_as_success() -> None:
    """The archive key is already gone from ObjectStore (e.g. a previous
    sweep removed it but crashed before the row hard-delete committed).
    ``ObjectStore.delete`` is idempotent for a missing key, so this reads
    as success and the row is reaped."""
    store = InMemoryUserWorkspaceStore()
    object_store = InMemoryObjectStore()
    tenant = uuid4()
    now = datetime.now(UTC)
    ws = await store.resolve(tenant_id=tenant, user_id=uuid4())
    await store.soft_delete(workspace_id=ws.id, now=now - timedelta(days=100))
    await store.mark_archived(workspace_id=ws.id, archived_object_key="ws-archives/gone.tar.zst")
    # Deliberately never ``put`` the key — it's already absent.

    job = RetentionCleanupJob(
        db_session_factory=lambda: None,  # type: ignore[arg-type]
        workspace_store=store,
        object_store=object_store,
        workspace_archive_retention_days=90,
    )
    hard, keys_ok, keys_failed, pending = await job._sweep_workspaces()
    assert (hard, keys_ok, keys_failed, pending) == (1, 1, 0, 0)
    assert await store.get(tenant_id=tenant, user_id=ws.user_id) is None


@pytest.mark.asyncio
async def test_sweep_workspaces_counts_pending_archive_without_deleting() -> None:
    """A soft-deleted row whose J.15 archive job hasn't run yet
    (``archived_object_key IS NULL``) is not a hard-delete candidate even
    once its ``deleted_at`` is past the retention cutoff — it only shows
    up in the ``pending_archive`` tally."""
    store = InMemoryUserWorkspaceStore()
    object_store = InMemoryObjectStore()
    tenant = uuid4()
    now = datetime.now(UTC)
    stuck = await store.resolve(tenant_id=tenant, user_id=uuid4())
    await store.soft_delete(workspace_id=stuck.id, now=now - timedelta(days=100))
    # No mark_archived() call — the archive job hasn't run yet.

    job = RetentionCleanupJob(
        db_session_factory=lambda: None,  # type: ignore[arg-type]
        workspace_store=store,
        object_store=object_store,
        workspace_archive_retention_days=90,
    )
    hard, keys_ok, keys_failed, pending = await job._sweep_workspaces()
    assert (hard, keys_ok, keys_failed, pending) == (0, 0, 0, 1)
    assert await store.get(tenant_id=tenant, user_id=stuck.user_id) is not None


@pytest.mark.asyncio
async def test_sweep_workspaces_noop_without_object_store() -> None:
    """Job constructed with a workspace store but no object store skips
    the pass cleanly (the pass has its own object-store gate)."""
    store = InMemoryUserWorkspaceStore()
    job = RetentionCleanupJob(
        db_session_factory=lambda: None,  # type: ignore[arg-type]
        workspace_store=store,
    )
    assert await job._sweep_workspaces() == (0, 0, 0, 0)
