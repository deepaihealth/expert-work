"""``RetentionCleanupJob`` — the D.3 nightly sweep.

Per STREAM-D-DESIGN § 2.6 + Mini-ADR D-5: M0 walks the rows with
``DELETE ... WHERE ctid IN (SELECT ... LIMIT N)`` rather than partition
drops. Simple, no schema churn, and the per-tenant retention shapes
fit in a single SQL statement using a JOIN with ``tenant_config``.

Three independent passes per ``run_once``:

1.  ``audit_log`` — only ``backup_acked = true`` rows past
    ``audit_retention_days``. Unacked candidates are counted + logged
    so SRE notices when the D.1c worker is lagging; the rows
    themselves are **never** deleted while unacked.
2.  ``event_log`` — past ``event_log_retention_days``. No WORM gate
    in M0 (cold archive to S3 is a Stream G item).
3.  ``jwt_blacklist`` — past ``expires_at``. Global, not tenant-scoped.

The whole sweep runs as ``retention_cleanup_worker`` (migration 0010,
NOLOGIN BYPASSRLS with the minimum delete grants).

留存链 PR2(波 3 线 R,用户 2026-09-09 拍板:产物 90 天、上传 90 天、已删工作区
库行 90 天销账)在这之上加了三条碰 NAS 工作区的规则 + 一条不变式:

* B-28 ``_sweep_artifact_versions`` —— ``artifact_version.created_at`` 满 90 天:
  删 NAS 文件 + 删版本行;版本清空的 ``artifact`` 行标过期(复用 ``deleted_at``)。
* 上传 ``_sweep_uploads`` —— ``user_upload.created_at`` 满 90 天:删 ``uploads/<file>``
  + 标 ``deleted_at``。
* B-27 ``_sweep_orphan_thread_dirs`` —— ``threads/<thread_id>/`` 的 thread 行不存在
  → 删目录。**不按时间删活会话的目录**。
* X-4 ② ``_sweep_workspaces`` —— 软删且已归档满 90 天的 ``user_workspace`` 行连
  从属行硬删;OSS 归档对象**不主动删**(桶生命周期到期)。

不变式(``tests/test_workspace_invariant.py``):对活着的工作区,job 只删 (a) 登记
过的产物文件 (b) ``uploads/`` 下登记过的文件 (c) 孤儿 ``threads/<id>/``;根目录
其它任何文件/目录(``style/``、MEMORY.md、未登记文件)永不触碰。碰文件的入口
只有 :mod:`retention_cleanup_job.workspace_files` 那两个函数。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from expert_work.persistence.artifact import ArtifactStore
from expert_work.persistence.image_upload import ImageUploadStore
from expert_work.persistence.memory import MemoryStore
from expert_work.persistence.rls import bypass_rls_var, current_tenant_id_var
from expert_work.persistence.tenant_user import TenantUserStore
from expert_work.persistence.thread_meta import ThreadMetaStore
from expert_work.persistence.user_upload import UserUploadStore
from expert_work.persistence.workspace import UserWorkspaceStore
from expert_work.persistence.workspace.layout import WORKSPACE_UPLOADS_DIR
from expert_work.protocol import (
    ArtifactVersion,
    AuditAction,
    AuditEntry,
    AuditResult,
)
from expert_work.runtime.audit import AuditLogger
from expert_work.runtime.storage import ObjectStore
from retention_cleanup_job.orphan_threads import sweep_orphan_thread_dirs
from retention_cleanup_job.report import CleanupReport
from retention_cleanup_job.workspace_files import (
    UnsafeWorkspacePathError,
    unlink_registered_file,
)

logger = logging.getLogger(__name__)

#: 审计行的 actor —— 与 control-plane 的 sweep 同款写法(``actor_type="system"``)。
_ACTOR_ID = "retention_cleanup_job"


# The cleanup runs against a DB connection that's already authenticated
# as a role with DELETE privilege on the target tables — typically
# ``retention_cleanup_worker`` (NOLOGIN role from migration 0010;
# operators ``ALTER ROLE ... WITH LOGIN`` for the cron user, or assign
# the role to a separate LOGIN account that's a member). We deliberately
# do NOT issue ``SET LOCAL ROLE`` in this code path: under asyncpg +
# SQLAlchemy 2.0, a ``SET LOCAL ROLE`` followed by a DELETE that
# actually matches rows intermittently returns "permission denied"
# even when ``has_table_privilege`` confirms the GRANT. Connecting
# directly as the worker role sidesteps the issue entirely.


@contextmanager
def _bypass_rls() -> Iterator[None]:
    """B-45 —— 整个 sweep 都是跨租户扫描,显式声明 RLS bypass。

    main.py 把 session factory 包进 ``build_rls_sessionmaker``,``after_begin``
    listener 从此在本进程存在;没有 tenant 上下文又没声明 bypass 的会话会被
    listener 记成 ``rls.would_fail_closed``(Detect 信号),将来 enforce 时会
    直接 fail-closed 拿到 0 行。这个 job 本来就以 BYPASSRLS 角色跨租户删行,
    所以在这里把意图写明,与 billing-rollup-job / control-plane 的各 sweep
    同一个形状(``bypass_rls_var=True`` + ``current_tenant_id_var=None``)。
    """
    bypass = bypass_rls_var.set(True)
    tenant = current_tenant_id_var.set(None)
    try:
        yield
    finally:
        current_tenant_id_var.reset(tenant)
        bypass_rls_var.reset(bypass)


class RetentionCleanupJob:
    """One-shot retention sweep driven by ``tenant_config`` per-tenant TTLs."""

    def __init__(
        self,
        *,
        db_session_factory: async_sessionmaker[AsyncSession],
        batch_size: int = 10000,
        image_upload_store: ImageUploadStore | None = None,
        object_store: ObjectStore | None = None,
        image_retention_days: int = 90,
        artifact_store: ArtifactStore | None = None,
        artifact_retention_days: int = 90,
        artifact_hard_delete_grace_days: int = 60,
        memory_store: MemoryStore | None = None,
        memory_hard_delete_grace_days: int = 90,
        workspace_store: UserWorkspaceStore | None = None,
        workspace_archive_retention_days: int = 90,
        tenant_user_store: TenantUserStore | None = None,
        tenant_user_hard_delete_grace_days: int = 90,
        sandbox_egress_audit_retention_days: int = 90,
        user_upload_store: UserUploadStore | None = None,
        upload_retention_days: int = 90,
        thread_store: ThreadMetaStore | None = None,
        workspace_root: str | None = None,
        audit_logger: AuditLogger | None = None,
    ) -> None:
        if batch_size <= 0:
            msg = "batch_size must be positive"
            raise ValueError(msg)
        if image_retention_days < 1:
            msg = "image_retention_days must be >= 1"
            raise ValueError(msg)
        if artifact_retention_days < 1:
            msg = "artifact_retention_days must be >= 1"
            raise ValueError(msg)
        if artifact_hard_delete_grace_days < 1:
            msg = "artifact_hard_delete_grace_days must be >= 1"
            raise ValueError(msg)
        if memory_hard_delete_grace_days < 1:
            msg = "memory_hard_delete_grace_days must be >= 1"
            raise ValueError(msg)
        if workspace_archive_retention_days < 1:
            msg = "workspace_archive_retention_days must be >= 1"
            raise ValueError(msg)
        if tenant_user_hard_delete_grace_days < 1:
            msg = "tenant_user_hard_delete_grace_days must be >= 1"
            raise ValueError(msg)
        if sandbox_egress_audit_retention_days < 1:
            msg = "sandbox_egress_audit_retention_days must be >= 1"
            raise ValueError(msg)
        if upload_retention_days < 1:
            msg = "upload_retention_days must be >= 1"
            raise ValueError(msg)
        self._sf = db_session_factory
        self._batch_size = batch_size
        self._image_upload_store = image_upload_store
        self._object_store = object_store
        self._image_retention_days = image_retention_days
        self._artifact_store = artifact_store
        self._artifact_retention_days = artifact_retention_days
        self._artifact_hard_delete_grace_days = artifact_hard_delete_grace_days
        self._memory_store = memory_store
        self._memory_grace_days = memory_hard_delete_grace_days
        self._workspace_store = workspace_store
        self._workspace_retention_days = workspace_archive_retention_days
        self._tenant_user_store = tenant_user_store
        self._tenant_user_grace_days = tenant_user_hard_delete_grace_days
        self._sandbox_egress_audit_retention_days = sandbox_egress_audit_retention_days
        self._user_upload_store = user_upload_store
        self._upload_retention_days = upload_retention_days
        self._thread_store = thread_store
        self._workspace_root = workspace_root
        self._audit_logger = audit_logger

    async def run_once(self) -> CleanupReport:
        """Run the retention passes once and return a tally.

        Each pass owns its own session + transaction so the
        ``SET LOCAL ROLE`` is re-issued cleanly per pass. Sharing one
        session across all three passes triggered intermittent
        ``permission denied`` failures on later DELETEs in CI even
        though the role had the grants — the per-pass isolation
        avoids whatever cross-statement state interaction caused that.

        Mini-ADR J-32 (J.6.补强-3b) — the image-upload pass runs when
        both an :class:`ImageUploadStore` and an :class:`ObjectStore`
        are wired; otherwise it's a no-op (the audit / event / jwt
        passes still run, so unit tests that don't care about images
        keep working).
        """
        started = time.monotonic()
        with _bypass_rls():
            audit_deleted, audit_by_tenant = await self._delete_audit_log()
            audit_skipped = await self._count_unacked_past_retention()
            event_deleted = await self._delete_event_log()
            sandbox_egress_audit_deleted = await self._delete_sandbox_egress_audit()
            jwt_deleted = await self._delete_expired_jwt_blacklist()
            image_rows, image_keys_ok, image_keys_failed = await self._delete_expired_images()
            # 版本 pass 先于逻辑产物 pass:文件先按版本清掉,后面的 hard-delete
            # 才不会把还带着文件的版本行连根拔掉。
            (
                artifact_versions_deleted,
                artifact_files_removed,
                artifacts_expired,
            ) = await self._sweep_artifact_versions()
            artifact_soft, artifact_hard = await self._sweep_artifacts()
            uploads_expired, upload_files_removed = await self._sweep_uploads()
            thread_dirs_removed = await self._sweep_orphan_thread_dirs()
            memory_hard_deleted = await self._sweep_memory()
            tenant_users_hard_deleted = await self._sweep_tenant_users()
            workspaces_hard_deleted, workspaces_pending_archive = await self._sweep_workspaces()

        return CleanupReport(
            audit_deleted=audit_deleted,
            audit_skipped_unacked=audit_skipped,
            audit_deleted_by_tenant=audit_by_tenant,
            event_deleted=event_deleted,
            jwt_blacklist_deleted=jwt_deleted,
            image_uploads_hard_deleted=image_rows,
            image_object_keys_removed=image_keys_ok,
            image_object_keys_failed=image_keys_failed,
            artifacts_soft_deleted=artifact_soft,
            artifacts_hard_deleted=artifact_hard,
            artifact_versions_deleted=artifact_versions_deleted,
            artifact_files_removed=artifact_files_removed,
            artifacts_expired=artifacts_expired,
            uploads_expired=uploads_expired,
            upload_files_removed=upload_files_removed,
            thread_dirs_removed=thread_dirs_removed,
            memory_hard_deleted=memory_hard_deleted,
            workspaces_hard_deleted=workspaces_hard_deleted,
            workspaces_pending_archive=workspaces_pending_archive,
            tenant_users_hard_deleted=tenant_users_hard_deleted,
            sandbox_egress_audit_deleted=sandbox_egress_audit_deleted,
            duration_seconds=time.monotonic() - started,
        )

    async def _sweep_memory(self) -> int:
        """Deletion hygiene PR1 (Task 7) — physically remove memory rows
        soft-deleted (K.K6 forget) past ``memory_hard_delete_grace_days``.

        No-op when no :class:`MemoryStore` is wired (unit-test path /
        deployments not running this pass).
        """
        if self._memory_store is None:
            return 0
        cutoff = datetime.now(UTC) - timedelta(days=self._memory_grace_days)
        return await self._memory_store.hard_delete_expired(before=cutoff, limit=self._batch_size)

    async def _sweep_tenant_users(self) -> int:
        """Deletion hygiene PR1 (Task 7) — physically remove tenant_user rows
        deactivated (Phase 3a purge_user) past
        ``tenant_user_hard_delete_grace_days``.

        No-op when no :class:`TenantUserStore` is wired (unit-test path /
        deployments not running this pass).
        """
        if self._tenant_user_store is None:
            return 0
        cutoff = datetime.now(UTC) - timedelta(days=self._tenant_user_grace_days)
        return await self._tenant_user_store.hard_delete_deactivated(
            before=cutoff, limit=self._batch_size
        )

    async def _sweep_workspaces(self) -> tuple[int, int]:
        """X-4 ② —— 已删工作区 90 天销账。

        软删(``deleted_at``)且已归档(``archived_object_key``)满
        ``workspace_archive_retention_days`` 的 ``user_workspace`` 行:**先硬删
        工作区行**,成功了再删从属行(该用户的 artifact / artifact_version /
        user_upload —— 与 ``purge_user`` 的级联清单同一批「字节住在工作区里」的
        表),写一条 ``WORKSPACE_HARD_DELETE`` 审计。

        顺序是审查(High)钉下来的:三个 store 各开各的 session,做不成一个事务;
        先删从属行再硬删工作区行,硬删一失败从属行就成了永远没人认领的孤儿
        (工作区行是明天重试的唯一索引)。反过来,工作区行删掉后从属行删失败,
        行本身仍按 ``(tenant, user)`` 可查,log + 审计 ``dependents_failed`` 记下,
        由人处理 —— 这条路径不会静默。

        **OSS 归档对象不在这里删**:桶生命周期到期自然清掉。此前这条 pass 会先
        ``ObjectStore.delete(archived_object_key)`` 再删行,PR2 起 job 不持有 OSS
        凭据、也不再需要 —— 审计行记下 key,要追溯还有据可查。

        Returns ``(rows_hard_deleted, pending_archive)``。仍在等 janitor 归档的行
        (``archived_object_key IS NULL``)只计数不动。No-op when
        :class:`UserWorkspaceStore` is not wired.
        """
        if self._workspace_store is None:
            return 0, 0
        cutoff = datetime.now(UTC) - timedelta(days=self._workspace_retention_days)
        pending = [
            w
            for w in await self._workspace_store.list_pending_archive()
            if w.deleted_at is not None and w.deleted_at < cutoff
        ]
        rows = await self._workspace_store.list_archived_expired(
            before=cutoff, limit=self._batch_size
        )
        hard = 0
        for ws in rows:
            if not await self._workspace_store.hard_delete(workspace_id=ws.id):
                continue
            hard += 1
            artifacts_deleted = uploads_deleted = 0
            dependents_failed: list[str] = []
            try:
                if self._artifact_store is not None:
                    artifacts_deleted = await self._artifact_store.delete_all_for_user(
                        tenant_id=ws.tenant_id, user_id=ws.user_id
                    )
            except Exception:
                dependents_failed.append("artifact")
                logger.exception("retention.workspace_dependents_failed store=artifact")
            try:
                if self._user_upload_store is not None:
                    uploads_deleted = await self._user_upload_store.delete_all_for_user(
                        tenant_id=ws.tenant_id, user_id=ws.user_id
                    )
            except Exception:
                dependents_failed.append("user_upload")
                logger.exception("retention.workspace_dependents_failed store=user_upload")
            await self._audit(
                tenant_id=ws.tenant_id,
                action=AuditAction.WORKSPACE_HARD_DELETE,
                resource_type="user_workspace",
                resource_id=str(ws.id),
                details={
                    "user_id": str(ws.user_id),
                    "deleted_at": ws.deleted_at.isoformat() if ws.deleted_at else None,
                    "archived_object_key": ws.archived_object_key,
                    "archive_object": "left to bucket lifecycle",
                    "artifacts_deleted": artifacts_deleted,
                    "uploads_deleted": uploads_deleted,
                    "dependents_failed": dependents_failed,
                },
            )
        return hard, len(pending)

    async def _sweep_artifact_versions(self) -> tuple[int, int, int]:
        """留存链 B-28 —— 产物按**版本** 90 天。

        ``artifact_version.created_at < now - artifact_retention_days`` 的版本:
        删 NAS 文件(``path_in_workspace``,只 unlink 那一个普通文件)+ 删版本行;
        同名产物有多版本时逐版本判,新版本留着。一个逻辑产物的版本全部清完 →
        ``artifact`` 行标过期(复用 ``deleted_at``,之后走既有的 hard-delete
        宽限)。每个逻辑产物一次 sweep 写一条 ``ARTIFACT_EXPIRED`` 审计。

        文件删不掉(权限 / 不是普通文件 / 路径逃逸)→ **保留版本行**,明天再试:
        登记行是那个文件唯一的索引,删行不删文件就是永久孤儿。文件本来就不在
        (用户手删、工作区已归档 rmtree)→ 正常删行。

        Returns ``(versions_deleted, files_removed, artifacts_expired)``。
        No-op when :class:`ArtifactStore` or ``workspace_root`` is missing.
        """
        if self._artifact_store is None or self._workspace_root is None:
            return 0, 0, 0
        now = datetime.now(UTC)
        cutoff = now - timedelta(days=self._artifact_retention_days)
        expired = await self._artifact_store.list_versions_expired(
            before=cutoff, limit=self._batch_size
        )
        by_artifact: dict[UUID, list[ArtifactVersion]] = {}
        for version in expired:
            by_artifact.setdefault(version.artifact_id, []).append(version)
        versions_deleted = files_removed = artifacts_expired = 0
        for artifact_id, versions in by_artifact.items():
            deletable: list[UUID] = []
            removed_here = 0
            for version in versions:
                removed = await self._unlink(version)
                if removed is None:
                    continue
                removed_here += int(removed)
                deletable.append(version.id)
            if not deletable:
                continue
            deleted = await self._artifact_store.delete_versions(version_ids=deletable)
            versions_deleted += deleted
            files_removed += removed_here
            expired_now = await self._artifact_store.mark_expired_if_versionless(
                artifact_id=artifact_id, now=now
            )
            artifacts_expired += int(expired_now)
            details: dict[str, object] = {
                "user_id": str(versions[0].user_id),
                "versions_deleted": sorted(v.version for v in versions if v.id in deletable),
                "files_removed": removed_here,
                "artifact_expired": expired_now,
                "retention_days": self._artifact_retention_days,
            }
            if not expired_now:
                # 审计要能自解释:没标过期是因为还有版本(正常,含本轮删不掉
                # 而保留的),还是行早已软删 / 不存在(mark 的 CAS 谓词没命中)。
                remaining = await self._artifact_store.list_versions_by_artifact(
                    artifact_id=artifact_id
                )
                details["versions_remaining"] = len(remaining)
                details["reason"] = (
                    "versions_remaining" if remaining else "artifact_already_deleted_or_missing"
                )
                if len(deletable) < len(versions):
                    details["versions_kept_undeletable"] = sorted(
                        v.version for v in versions if v.id not in deletable
                    )
            await self._audit(
                tenant_id=versions[0].tenant_id,
                action=AuditAction.ARTIFACT_EXPIRED,
                resource_type="artifact",
                resource_id=str(artifact_id),
                details=details,
            )
        return versions_deleted, files_removed, artifacts_expired

    async def _sweep_artifacts(self) -> tuple[int, int]:
        """Mini-ADR J-25 (J.9-step1) — two-stage artifact lifecycle sweep.

        Stage 1: active rows past ``artifact_retention_days`` → soft-delete
        (sets ``deleted_at``). Stage 2: soft-deleted rows past
        ``artifact_hard_delete_grace_days`` → hard-delete (row +
        version rows).

        留存链 PR2:stage 2 在删行前先 unlink 该产物剩余版本的文件(用户手动软删
        的产物,其版本可能还不到 90 天,:meth:`_sweep_artifact_versions` 没碰过);
        某个文件删不掉就保留整条产物行,明天再试 —— 与版本 pass 同一取舍。
        ``workspace_root`` 未配时退回原来的只删元数据。

        No-op when :class:`ArtifactStore` is not wired (unit-test path
        + deployments not running J.9).
        """
        if self._artifact_store is None:
            return 0, 0
        now = datetime.now(UTC)
        # Stage 1 — soft-delete active rows past retention.
        soft_cutoff = now - timedelta(days=self._artifact_retention_days)
        active = await self._artifact_store.list_active_past_retention(
            before=soft_cutoff, limit=self._batch_size
        )
        soft_count = 0
        for row in active:
            ok = await self._artifact_store.soft_delete(
                tenant_id=row.tenant_id,
                user_id=row.user_id,
                name=row.name,
                now=now,
            )
            if ok:
                soft_count += 1
        # Stage 2 — hard-delete soft-deleted rows past the grace window.
        hard_cutoff = now - timedelta(days=self._artifact_hard_delete_grace_days)
        expired = await self._artifact_store.list_expired(
            before=hard_cutoff, limit=self._batch_size
        )
        if not expired:
            return soft_count, 0
        hard_ids: list[UUID] = []
        for artifact in expired:
            if self._workspace_root is not None:
                remaining = await self._artifact_store.list_versions_by_artifact(
                    artifact_id=artifact.id
                )
                outcomes = [await self._unlink(v) for v in remaining]
                if None in outcomes:
                    continue  # 某个文件删不掉 → 行留着,明天再试
            hard_ids.append(artifact.id)
        if not hard_ids:
            return soft_count, 0
        hard_count = await self._artifact_store.hard_delete(artifact_ids=hard_ids)
        return soft_count, hard_count

    async def _sweep_uploads(self) -> tuple[int, int]:
        """留存链 —— 上传 90 天。

        ``user_upload.created_at < now - upload_retention_days`` 且未软删的行:
        ``ref`` 是工作区相对路径(``uploads/<file>``,文档类)→ unlink 那个文件;
        图片类的 ``ref`` 是 ``expert_work://image/…`` 对象存储 URI,字节归
        ``image_upload`` 自己的 90 天 pass,这里不碰。两类都标 ``deleted_at``
        (对外 GET 从此 404,复用既有语义)。文件删不掉 → 行不标,明天再试。

        Returns ``(rows_marked, files_removed)``。No-op when
        :class:`UserUploadStore` or ``workspace_root`` is missing.
        """
        if self._user_upload_store is None or self._workspace_root is None:
            return 0, 0
        now = datetime.now(UTC)
        cutoff = now - timedelta(days=self._upload_retention_days)
        expired = await self._user_upload_store.list_expired(before=cutoff, limit=self._batch_size)
        marked = files_removed = 0
        for row in expired:
            # 两种 ``ref`` 形态,两种字节归属(见 protocol.UserUpload.ref):
            # * 文档类:工作区相对路径 ``uploads/<file>`` —— 字节在 NAS,这里
            #   unlink;删不掉就整行跳过(不标),明天再试,绝不留孤儿文件。
            # * 图片类:``expert_work://image/…`` 对象存储 URI —— 字节在 OSS,
            #   归 ``image_upload`` 自己的 90 天 pass(需要 OSS 凭据),这里不碰;
            #   行照样标 ``deleted_at``,对外 GET 与文档类同一天起 404。
            if row.ref.startswith(f"{WORKSPACE_UPLOADS_DIR}/"):
                try:
                    removed = await asyncio.to_thread(
                        unlink_registered_file,
                        self._workspace_root,
                        row.tenant_id,
                        row.user_id,
                        row.ref,
                    )
                except (OSError, UnsafeWorkspacePathError):
                    logger.exception("retention.upload_file_unlink_failed upload_id=%s", row.id)
                    continue
                files_removed += int(removed)
            if await self._user_upload_store.soft_delete(
                upload_id=row.id, tenant_id=row.tenant_id, now=now
            ):
                marked += 1
        return marked, files_removed

    async def _sweep_orphan_thread_dirs(self) -> int:
        """留存链 B-27 —— 孤儿 ``threads/<thread_id>/`` 目录;规则本体在
        :func:`retention_cleanup_job.orphan_threads.sweep_orphan_thread_dirs`。
        No-op when :class:`ThreadMetaStore` or ``workspace_root`` is missing."""
        if self._thread_store is None or self._workspace_root is None:
            return 0
        return await sweep_orphan_thread_dirs(self._workspace_root, self._thread_store)

    async def _unlink(self, version: ArtifactVersion) -> bool | None:
        """Unlink one registered artifact file. ``True`` removed, ``False`` was
        already gone, ``None`` could not be removed (logged; caller keeps the row)."""
        assert self._workspace_root is not None  # noqa: S101 - callers gate on it
        try:
            return await asyncio.to_thread(
                unlink_registered_file,
                self._workspace_root,
                version.tenant_id,
                version.user_id,
                version.path_in_workspace,
            )
        except (OSError, UnsafeWorkspacePathError):
            logger.exception("retention.artifact_file_unlink_failed version_id=%s", version.id)
            return None

    async def _audit(
        self,
        *,
        tenant_id: UUID,
        action: AuditAction,
        resource_type: str,
        resource_id: str,
        details: dict[str, object],
    ) -> None:
        """写一条 system 审计;没接 :class:`AuditLogger` 时(单测 / 老部署)跳过。
        ``AuditLogger.write`` 自身 best-effort,不会把 sweep 打断。"""
        if self._audit_logger is None:
            return
        await self._audit_logger.write(
            AuditEntry(
                tenant_id=tenant_id,
                actor_type="system",
                actor_id=_ACTOR_ID,
                action=action,
                resource_type=resource_type,  # type: ignore[arg-type]
                resource_id=resource_id,
                result=AuditResult.SUCCESS,
                details=details,
            )
        )

    async def _delete_expired_images(self) -> tuple[int, int, int]:
        """Remove image rows past their retention window + their bytes.

        Mini-ADR J-32 — finds the reapable ``image_upload`` rows: those
        whose ``created_at`` is older than ``now - image_retention_days``,
        plus **any already-soft-deleted row regardless of age** (a user
        delete means the blob should leave at the next sweep, not at the
        original retention horizon). Removes the object-store key for
        each, then hard-deletes the row.

        Object-store failures don't block row hard-delete: an orphaned
        key is a far smaller correctness problem than a stuck row
        whose key never goes away (the bytes are billed against
        ``IMAGE_STORAGE_BYTES``). The failure count lands in the report
        so SRE can investigate without the sweep stalling.

        Returns ``(rows_deleted, keys_removed, keys_failed)``. Returns
        ``(0, 0, 0)`` when either store is missing (unit-test path).
        """
        if self._image_upload_store is None or self._object_store is None:
            return 0, 0, 0
        cutoff = datetime.now(UTC) - timedelta(days=self._image_retention_days)
        expired = await self._image_upload_store.list_reapable(
            before=cutoff,
            limit=self._batch_size,
        )
        if not expired:
            return 0, 0, 0
        keys_ok = 0
        keys_failed = 0
        for row in expired:
            try:
                await self._object_store.delete(row.object_key)
                keys_ok += 1
            except Exception:
                keys_failed += 1
                logger.exception(
                    "retention.image_object_delete_failed key=%s",
                    row.object_key,
                )
        rows = await self._image_upload_store.hard_delete(
            image_ids=[r.id for r in expired],
        )
        return rows, keys_ok, keys_failed

    # ------------------------------------------------------------------
    # Per-table helpers (private). Each opens its own session + txn,
    # SETs LOCAL ROLE retention_cleanup_worker, runs one statement,
    # commits.
    # ------------------------------------------------------------------

    async def _delete_audit_log(self) -> tuple[int, dict[str, int]]:
        """Delete acked audit rows past their tenant's retention window.

        Uses ``ctid`` subquery to apply LIMIT to a DELETE (Postgres
        doesn't support ``DELETE ... LIMIT`` directly). RETURNING
        ``tenant_id`` lets us tally per-tenant deletes for the report.

        The ``backup_acked = true`` predicate is the WORM safety
        gate: unacked rows are skipped here and counted separately
        by ``_count_unacked_past_retention``.
        """
        async with self._sf() as session:
            result = await session.execute(
                text(
                    """
                    DELETE FROM audit_log
                    WHERE ctid IN (
                        SELECT a.ctid
                        FROM audit_log a
                        JOIN tenant_config c ON c.tenant_id = a.tenant_id
                        WHERE a.backup_acked = true
                          AND a.occurred_at < now() - (c.audit_retention_days || ' days')::interval
                        LIMIT :batch
                    )
                    RETURNING tenant_id
                    """
                ),
                {"batch": self._batch_size},
            )
            rows = result.fetchall()
            await session.commit()
        per_tenant: dict[str, int] = {}
        for row in rows:
            tid = str(row[0])
            per_tenant[tid] = per_tenant.get(tid, 0) + 1
        return len(rows), per_tenant

    async def _count_unacked_past_retention(self) -> int:
        """How many audit rows are *past* retention but still unacked.

        Steady-state value is 0. A growing number means the D.1c
        WORM backup worker is falling behind and needs investigation;
        we surface it on the report but never delete those rows.
        """
        async with self._sf() as session:
            result = await session.execute(
                text(
                    """
                    SELECT count(*)
                    FROM audit_log a
                    JOIN tenant_config c ON c.tenant_id = a.tenant_id
                    WHERE a.backup_acked = false
                      AND a.occurred_at < now() - (c.audit_retention_days || ' days')::interval
                    """
                )
            )
            count = int(result.scalar() or 0)
            await session.commit()
        return count

    async def _delete_event_log(self) -> int:
        """Two-step: read retentions, then per-tenant flat DELETE.

        Investigation in CI showed that ``DELETE FROM event_log WHERE
        ctid IN (SELECT … LIMIT N)`` consistently raises ``permission
        denied for table event_log`` even though ``has_table_privilege``
        + a trivial probe ``DELETE FROM event_log WHERE id = -999999``
        both succeed for the same role in the same session. The
        ``ctid``-subquery + ``LIMIT`` form is the only thing that
        differs from the audit_log path that *does* work — and rather
        than chase the asyncpg/SQLAlchemy quirk further, the flat
        ``DELETE … WHERE tenant_id = :t AND created_at < :cutoff``
        form is plenty for M0 retention volumes. M1 can add ``LIMIT``
        back if the table grows large enough to need batching, by
        which time we'll have partitioning anyway.
        """
        retentions = await self._read_event_retentions()
        total = 0
        for tenant_id, days in retentions:
            async with self._sf() as session:
                result = await session.execute(
                    text(
                        "DELETE FROM event_log "
                        "WHERE tenant_id = :t "
                        "  AND created_at < now() - make_interval(days => :d) "
                        "RETURNING id"
                    ),
                    {"t": tenant_id, "d": days},
                )
                total += len(result.fetchall())
                await session.commit()
        return total

    async def _delete_sandbox_egress_audit(self) -> int:
        """删掉过了保留期的沙箱出网审计行。

        ``ctid`` 子查询给 DELETE 限批(Postgres 不支持 ``DELETE ... LIMIT``),
        与本文件其他几个 pass 同款。全局窗口、无 per-tenant 覆盖 —— 同
        ``image_retention_days`` 的理由。表级 GRANT 见迁移 0143:这个 pass 以
        ``retention_cleanup_worker`` 角色执行,少了那条授权就是 permission denied。

        Uses ``make_interval(days => :days)`` rather than
        ``(:days || ' days')::interval`` — the latter made Postgres infer
        the bind parameter's type as ``text`` (from the ``||`` operator),
        and asyncpg then refused to bind the Python ``int`` into a
        text-typed parameter (``DataError: invalid input for query
        argument $1: 90 (expected str, got int)``). ``make_interval``
        takes an integer directly, sidestepping the type-inference gap;
        it's also what ``_delete_event_log`` already uses for the same
        reason.
        """
        async with self._sf() as session:
            result = await session.execute(
                text(
                    """
                    DELETE FROM sandbox_egress_audit
                    WHERE ctid IN (
                        SELECT a.ctid
                        FROM sandbox_egress_audit a
                        WHERE a.occurred_at < now() - make_interval(days => :days)
                        LIMIT :batch
                    )
                    """
                ),
                {
                    "days": self._sandbox_egress_audit_retention_days,
                    "batch": self._batch_size,
                },
            )
            await session.commit()
        # ``Result.rowcount`` is only typed on ``CursorResult``; the base
        # ``Result`` mypy sees from ``session.execute(text(...))`` exposes
        # it at runtime but not at the type level (same as
        # ``expert_work.persistence.workspace.sql`` / ``skill.sql``).
        return result.rowcount or 0  # type: ignore[attr-defined]

    async def _read_event_retentions(self) -> list[tuple[str, int]]:
        """Return ``(tenant_id, event_log_retention_days)`` for every tenant."""
        async with self._sf() as session:
            result = await session.execute(
                text("SELECT tenant_id::text, event_log_retention_days FROM tenant_config")
            )
            rows = [(str(r[0]), int(r[1])) for r in result.fetchall()]
            await session.commit()
        return rows

    async def _delete_expired_jwt_blacklist(self) -> int:
        """``jwt_blacklist`` is global — no tenant_id, expire_at-driven."""
        async with self._sf() as session:
            result = await session.execute(
                text("DELETE FROM jwt_blacklist WHERE expires_at < now() RETURNING jti")
            )
            rows = result.fetchall()
            await session.commit()
        return len(rows)


__all__ = ["CleanupReport", "RetentionCleanupJob"]
