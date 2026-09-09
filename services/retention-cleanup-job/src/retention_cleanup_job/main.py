"""Entrypoint: ``uv run python -m retention_cleanup_job``.

Runs one cleanup sweep and exits — cron / Kubernetes CronJob handles
scheduling. The job is idempotent; running it twice in a row is fine
(the second pass usually deletes nothing).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from expert_work.persistence import (
    DatabaseConfig,
    SqlArtifactStore,
    SqlAuditLogStore,
    SqlImageUploadStore,
    SqlMemoryStore,
    SqlTenantUserStore,
    SqlThreadMetaStore,
    SqlUserUploadStore,
    SqlUserWorkspaceStore,
    create_async_engine_from_config,
    create_async_session_factory,
)
from expert_work.persistence.rls import build_rls_sessionmaker
from expert_work.runtime.audit import (
    AuditLogger,
    DefaultSecretRedactor,
    InMemoryAuditFallbackQueue,
)
from expert_work.runtime.storage import make_object_store
from expert_work.runtime.storage.factory import S3CompatibleConfig
from retention_cleanup_job.job import RetentionCleanupJob
from retention_cleanup_job.settings import RetentionCleanupSettings

logger = logging.getLogger(__name__)


def build_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """B-45 —— 与 control-plane / billing-rollup-job 同款:session factory 必须
    经过 ``build_rls_sessionmaker``,``after_begin`` listener 才在本进程存在。
    此前这个 job 从不调它,RLS Detect 信号在这里根本不会出现,生产 enforce
    之前也就没人知道它哪条路径会 fail-closed。sweep 本身在
    :func:`retention_cleanup_job.job._bypass_rls` 里显式声明跨租户 bypass。"""
    return build_rls_sessionmaker(create_async_session_factory(engine))


async def _amain() -> None:
    settings = RetentionCleanupSettings()
    logging.basicConfig(level=settings.log_level)

    engine = create_async_engine_from_config(DatabaseConfig(dsn=settings.db_dsn))
    session_factory = build_session_factory(engine)

    async with contextlib.AsyncExitStack() as stack:
        # Mini-ADR J-32 (J.6.补强-3b) — the image pass needs both a
        # registry + the object store. ``memory`` backend skips the
        # pass (in-memory bytes vanish per process anyway).
        image_store: SqlImageUploadStore | None = None
        object_store = None
        if settings.object_store_backend == "s3-compatible":
            if (
                not settings.object_store_endpoint_url
                or not settings.object_store_access_key
                or not settings.object_store_secret_key
            ):
                msg = (
                    "object_store_backend=s3-compatible requires endpoint_url + "
                    "access_key + secret_key"
                )
                raise ValueError(msg)
            object_store = await stack.enter_async_context(
                make_object_store(
                    settings.object_store_backend,
                    S3CompatibleConfig(
                        endpoint_url=settings.object_store_endpoint_url,
                        region=settings.object_store_region,
                        bucket=settings.object_store_bucket,
                        access_key=settings.object_store_access_key,
                        secret_key=settings.object_store_secret_key,
                        addressing_style=settings.object_store_addressing_style,
                    ),
                )
            )
            image_store = SqlImageUploadStore(session_factory)

        # Mini-ADR J-25 (J.9-step1) — artifact lifecycle sweep wires the
        # SqlArtifactStore in unconditionally; the artifact pass is
        # metadata-only (no object store / supervisor calls), so it is
        # safe to enable even on deployments without J.6 image uploads.
        artifact_store = SqlArtifactStore(session_factory)
        # X-15 ① —— 审批超时不在这里做:control-plane 的 ``ApprovalTimeoutSweep``
        # 是唯一内核(走 ``resolve_approval_decision``,写 checkpoint、spawn 续跑)。

        # Deletion hygiene PR1 (Task 7) — memory / tenant_user hard-delete
        # sweeps are metadata-only (no object store), safe to wire
        # unconditionally like artifact_store above. The
        # workspace-archive sweep is metadata-only too, but its own pass
        # still gates on ``object_store`` being wired (it deletes the
        # archive's ObjectStore key before dropping the row).
        memory_store = SqlMemoryStore(session_factory)
        tenant_user_store = SqlTenantUserStore(session_factory)
        workspace_store = SqlUserWorkspaceStore(session_factory)

        # 留存链 PR2 —— 三条工作区规则的登记表 + 审计。审计走 SqlAuditLogStore
        # (append 时 SET LOCAL ROLE audit_writer —— DSN 用户与 control-plane 同一
        # 个,本来就是 audit_writer 成员);fallback 队列是进程内的,一次性 job
        # 写不进库就只剩 error 日志,与 control-plane 的 best-effort 语义一致。
        user_upload_store = SqlUserUploadStore(session_factory)
        thread_store = SqlThreadMetaStore(session_factory)
        audit_logger = AuditLogger(
            store=SqlAuditLogStore(session_factory),
            redactor=DefaultSecretRedactor(),
            fallback=InMemoryAuditFallbackQueue(),
        )
        if settings.workspace_root is None:
            logger.warning(
                "retention.workspace_root_unset — artifact-version / upload-file / "
                "orphan-thread-dir rules are skipped (set EXPERT_WORK_RETENTION_WORKSPACE_ROOT)"
            )

        job = RetentionCleanupJob(
            db_session_factory=session_factory,
            batch_size=settings.batch_size,
            image_upload_store=image_store,
            object_store=object_store,
            image_retention_days=settings.image_retention_days,
            artifact_store=artifact_store,
            artifact_retention_days=settings.artifact_retention_days,
            artifact_hard_delete_grace_days=settings.artifact_hard_delete_grace_days,
            memory_store=memory_store,
            memory_hard_delete_grace_days=settings.memory_hard_delete_grace_days,
            workspace_store=workspace_store,
            workspace_archive_retention_days=settings.workspace_archive_retention_days,
            tenant_user_store=tenant_user_store,
            tenant_user_hard_delete_grace_days=settings.tenant_user_hard_delete_grace_days,
            sandbox_egress_audit_retention_days=settings.sandbox_egress_audit_retention_days,
            user_upload_store=user_upload_store,
            upload_retention_days=settings.upload_retention_days,
            thread_store=thread_store,
            workspace_root=settings.workspace_root,
            audit_logger=audit_logger,
        )
        logger.info("retention_cleanup_job.start batch=%d", settings.batch_size)
        report = await job.run_once()
        logger.info(
            "retention_cleanup_job.done audit=%d audit_skipped_unacked=%d "
            "event=%d jwt=%d image_rows=%d image_keys_ok=%d image_keys_failed=%d "
            "artifact_soft=%d artifact_hard=%d "
            "artifact_versions_deleted=%d artifact_files_removed=%d artifacts_expired=%d "
            "uploads_expired=%d upload_files_removed=%d thread_dirs_removed=%d "
            "memory_hard_deleted=%d workspaces_hard_deleted=%d "
            "workspaces_pending_archive=%d tenant_users_hard_deleted=%d "
            "sandbox_egress_audit=%d duration=%.2fs",
            report.audit_deleted,
            report.audit_skipped_unacked,
            report.event_deleted,
            report.jwt_blacklist_deleted,
            report.image_uploads_hard_deleted,
            report.image_object_keys_removed,
            report.image_object_keys_failed,
            report.artifacts_soft_deleted,
            report.artifacts_hard_deleted,
            report.artifact_versions_deleted,
            report.artifact_files_removed,
            report.artifacts_expired,
            report.uploads_expired,
            report.upload_files_removed,
            report.thread_dirs_removed,
            report.memory_hard_deleted,
            report.workspaces_hard_deleted,
            report.workspaces_pending_archive,
            report.tenant_users_hard_deleted,
            report.sandbox_egress_audit_deleted,
            report.duration_seconds,
        )
        if report.audit_skipped_unacked > 0:
            logger.warning(
                "retention.skipped_unacked count=%d — D.1c backup worker may be lagging",
                report.audit_skipped_unacked,
            )
        if report.image_object_keys_failed > 0:
            logger.warning(
                "retention.image_object_keys_failed count=%d — object store unhealthy?",
                report.image_object_keys_failed,
            )

    await engine.dispose()


def main() -> None:
    asyncio.run(_amain())


if __name__ == "__main__":
    main()
