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
    assert report.memory_hard_deleted == 0
    assert report.workspaces_hard_deleted == 0
    assert report.workspaces_pending_archive == 0
    assert report.artifact_versions_deleted == 0
    assert report.artifact_files_removed == 0
    assert report.artifacts_expired == 0
    assert report.uploads_expired == 0
    assert report.upload_files_removed == 0
    assert report.thread_dirs_removed == 0
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
# X-15 ① —— 审批超时只有 control-plane 一套内核
# ---------------------------------------------------------------------------


def test_job_has_no_approval_timeout_path() -> None:
    """X-15 ① —— 这个 job 曾有第二套审批超时实现:直接 ``mark_decided(TIMEOUT)``,
    不走 ``resolve_approval_decision``(不写 checkpoint、不 spawn 续跑),与
    control-plane 的 ``ApprovalTimeoutSweep`` 抢同一个 CAS,抢赢就让「超时保守
    继续」静默不发生。这里钉死:构造函数不再收 ``ApprovalStore``,报告里没有
    审批计数,模块源码不再碰 ``mark_decided`` / ``ApprovalStatus``。"""
    import inspect

    from retention_cleanup_job import job as job_module

    assert "approval_store" not in inspect.signature(RetentionCleanupJob.__init__).parameters
    assert "approvals_timed_out" not in CleanupReport.__dataclass_fields__
    source = inspect.getsource(job_module)
    assert "mark_decided" not in source
    assert "ApprovalStatus" not in source


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
# X-4 ② —— 已删工作区 90 天销账(行 + 从属行;不碰 OSS 归档对象)
# ---------------------------------------------------------------------------


async def _archived_workspace(
    store: InMemoryUserWorkspaceStore, *, tenant: object, user: object, days_ago: int
) -> object:
    ws = await store.resolve(tenant_id=tenant, user_id=user)  # type: ignore[arg-type]
    await store.soft_delete(workspace_id=ws.id, now=datetime.now(UTC) - timedelta(days=days_ago))
    await store.mark_archived(workspace_id=ws.id, archived_object_key=f"ws-archives/{ws.id}.tar.gz")
    refreshed = await store.get(tenant_id=tenant, user_id=user)  # type: ignore[arg-type]
    assert refreshed is not None
    return refreshed


@pytest.mark.asyncio
async def test_sweep_workspaces_hard_deletes_row_and_dependents_without_touching_object_store() -> (
    None
):
    """软删 + 已归档满 90 天:该用户的 artifact(+版本)/ user_upload 行连同工作区行
    一起硬删;OSS 归档对象**不删**(桶生命周期);写一条 WORKSPACE_HARD_DELETE 审计。"""
    from expert_work.persistence import InMemoryAuditLogStore, InMemoryUserUploadStore
    from expert_work.protocol import AuditAction, AuditQuery
    from expert_work.runtime.audit import (
        AuditLogger,
        DefaultSecretRedactor,
        InMemoryAuditFallbackQueue,
    )

    store = InMemoryUserWorkspaceStore()
    artifacts = InMemoryArtifactStore()
    uploads = InMemoryUserUploadStore()
    object_store = InMemoryObjectStore()
    audit_store = InMemoryAuditLogStore()
    tenant, user, other = uuid4(), uuid4(), uuid4()

    ws = await _archived_workspace(store, tenant=tenant, user=user, days_ago=100)
    await object_store.put(ws.archived_object_key, b"ARCHIVE")  # type: ignore[attr-defined]
    for owner in (user, other):
        await artifacts.save_version(
            tenant_id=tenant,
            user_id=owner,
            name="r.md",
            kind="document",
            path_in_workspace="r.md",
            created_in_thread="t",
        )
        await uploads.insert(
            upload_id=uuid4(),
            tenant_id=tenant,
            user_id=owner,
            thread_id=uuid4(),
            kind="document",
            ref="uploads/a.pdf",
            mime_type="application/pdf",
            size_bytes=1,
            filename="a.pdf",
        )

    job = RetentionCleanupJob(
        db_session_factory=lambda: None,  # type: ignore[arg-type]
        workspace_store=store,
        artifact_store=artifacts,
        user_upload_store=uploads,
        object_store=object_store,
        workspace_archive_retention_days=90,
        audit_logger=AuditLogger(
            store=audit_store,
            redactor=DefaultSecretRedactor(),
            fallback=InMemoryAuditFallbackQueue(),
        ),
    )
    assert await job._sweep_workspaces() == (1, 0)

    assert await store.get(tenant_id=tenant, user_id=user) is None
    assert await artifacts.list_for_user(tenant_id=tenant, user_id=user, include_deleted=True) == []
    assert await uploads.delete_all_for_user(tenant_id=tenant, user_id=user) == 0
    # The other user's rows are untouched (tenant AND user scoped cascade).
    assert len(await artifacts.list_for_user(tenant_id=tenant, user_id=other)) == 1
    assert await uploads.delete_all_for_user(tenant_id=tenant, user_id=other) == 1
    # The archive object is still there — bucket lifecycle owns it, not this job.
    assert await object_store.get(ws.archived_object_key) == b"ARCHIVE"  # type: ignore[attr-defined]

    page = await audit_store.query(AuditQuery(tenant_id=tenant))
    rows = [r for r in page.entries if r.action is AuditAction.WORKSPACE_HARD_DELETE]
    assert len(rows) == 1
    assert rows[0].resource_type == "user_workspace"
    assert rows[0].resource_id == str(ws.id)  # type: ignore[attr-defined]
    assert rows[0].actor_id == "retention_cleanup_job"
    assert rows[0].details["artifacts_deleted"] == 1
    assert rows[0].details["uploads_deleted"] == 1
    assert rows[0].details["archived_object_key"] == ws.archived_object_key  # type: ignore[attr-defined]

    # Idempotent: a second sweep finds nothing and writes no second audit row.
    assert await job._sweep_workspaces() == (0, 0)
    page = await audit_store.query(AuditQuery(tenant_id=tenant))
    assert len([r for r in page.entries if r.action is AuditAction.WORKSPACE_HARD_DELETE]) == 1


@pytest.mark.asyncio
async def test_sweep_workspaces_skips_recent_and_counts_pending_archive() -> None:
    """软删不满 90 天的行不动;满 90 天但 janitor 还没归档的只计入 pending。"""
    store = InMemoryUserWorkspaceStore()
    tenant = uuid4()
    recent = await _archived_workspace(store, tenant=tenant, user=uuid4(), days_ago=10)
    stuck = await store.resolve(tenant_id=tenant, user_id=uuid4())
    await store.soft_delete(workspace_id=stuck.id, now=datetime.now(UTC) - timedelta(days=100))
    # No mark_archived() call — the archive job hasn't run yet.

    job = RetentionCleanupJob(
        db_session_factory=lambda: None,  # type: ignore[arg-type]
        workspace_store=store,
        workspace_archive_retention_days=90,
    )
    assert await job._sweep_workspaces() == (0, 1)
    assert await store.get(tenant_id=tenant, user_id=recent.user_id) is not None  # type: ignore[attr-defined]
    assert await store.get(tenant_id=tenant, user_id=stuck.user_id) is not None


@pytest.mark.asyncio
async def test_sweep_workspaces_noop_without_workspace_store() -> None:
    job = RetentionCleanupJob(
        db_session_factory=lambda: None,  # type: ignore[arg-type]
    )
    assert await job._sweep_workspaces() == (0, 0)


class _StuckWorkspaceStore(InMemoryUserWorkspaceStore):
    """``hard_delete`` never lands — raises or returns False, configurable."""

    def __init__(self, *, raise_on_hard_delete: bool) -> None:
        super().__init__()
        self.raise_on_hard_delete = raise_on_hard_delete

    async def hard_delete(self, *, workspace_id: object) -> bool:
        if self.raise_on_hard_delete:
            raise RuntimeError("db down")
        return False


@pytest.mark.parametrize("raises", [True, False])
@pytest.mark.asyncio
async def test_sweep_workspaces_hard_delete_failure_leaves_dependents_untouched(
    raises: bool,
) -> None:
    """审查 High:硬删工作区行失败(抛错或返回 False)时,该用户的 artifact /
    user_upload 行必须原样还在 —— 行是明天重试的唯一索引,先删从属行再硬删失败
    就是孤儿。顺序钉成:先硬删工作区行,成功了再删从属行。"""
    from expert_work.persistence import InMemoryAuditLogStore, InMemoryUserUploadStore
    from expert_work.protocol import AuditQuery
    from expert_work.runtime.audit import (
        AuditLogger,
        DefaultSecretRedactor,
        InMemoryAuditFallbackQueue,
    )

    store = _StuckWorkspaceStore(raise_on_hard_delete=raises)
    artifacts = InMemoryArtifactStore()
    uploads = InMemoryUserUploadStore()
    audit_store = InMemoryAuditLogStore()
    tenant, user = uuid4(), uuid4()
    await _archived_workspace(store, tenant=tenant, user=user, days_ago=100)
    await artifacts.save_version(
        tenant_id=tenant,
        user_id=user,
        name="r.md",
        kind="document",
        path_in_workspace="r.md",
        created_in_thread="t",
    )
    await uploads.insert(
        upload_id=uuid4(),
        tenant_id=tenant,
        user_id=user,
        thread_id=uuid4(),
        kind="document",
        ref="uploads/a.pdf",
        mime_type="application/pdf",
        size_bytes=1,
        filename="a.pdf",
    )
    job = RetentionCleanupJob(
        db_session_factory=lambda: None,  # type: ignore[arg-type]
        workspace_store=store,
        artifact_store=artifacts,
        user_upload_store=uploads,
        workspace_archive_retention_days=90,
        audit_logger=AuditLogger(
            store=audit_store,
            redactor=DefaultSecretRedactor(),
            fallback=InMemoryAuditFallbackQueue(),
        ),
    )

    if raises:
        with pytest.raises(RuntimeError):
            await job._sweep_workspaces()
    else:
        assert await job._sweep_workspaces() == (0, 0)

    assert len(await artifacts.list_for_user(tenant_id=tenant, user_id=user)) == 1
    assert await uploads.delete_all_for_user(tenant_id=tenant, user_id=user) == 1
    assert (await audit_store.query(AuditQuery(tenant_id=tenant))).entries == []
