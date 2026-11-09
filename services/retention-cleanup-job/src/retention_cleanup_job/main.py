"""Entrypoint: ``uv run python -m retention_cleanup_job``.

Runs one cleanup sweep and exits — cron / Kubernetes CronJob handles
scheduling. The job is idempotent; running it twice in a row is fine
(the second pass usually deletes nothing).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from collections.abc import Mapping
from typing import IO

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from expert_work.common.observability import init_logging
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
from expert_work.persistence.knowledge import SqlKnowledgeRetrievalLogStore
from expert_work.persistence.rls import build_rls_sessionmaker
from expert_work.runtime.audit import (
    AuditLogger,
    DefaultSecretRedactor,
    InMemoryAuditFallbackQueue,
)
from expert_work.runtime.runs.store import SqlRunStore
from retention_cleanup_job.job import RetentionCleanupJob
from retention_cleanup_job.object_store import (
    enter_object_store,
    log_plan,
    plan_object_store,
)
from retention_cleanup_job.report import CleanupReport
from retention_cleanup_job.settings import RetentionCleanupSettings
from retention_cleanup_job.workspace_files import validate_workspace_root

logger = logging.getLogger(__name__)


#: control-plane 自己的 NAS 根键 —— CronJob 的 envFrom 把它带进本进程;job 的
#: ``EXPERT_WORK_RETENTION_WORKSPACE_ROOT`` 在 manifest 里就是从这个键取值的,
#: 这里再校验一遍是防 manifest 被人改成两个值。
_CONTROL_PLANE_NAS_ROOT_KEY = "EXPERT_WORK_WORKSPACE_NAS_ROOT"


def resolve_workspace_root(
    settings: RetentionCleanupSettings, *, environ: Mapping[str, str]
) -> str | None:
    """启动时决定 ``workspace_root``,不合格就**整个 job 拒跑**(审查项 3 / 4)。

    * 未配 → ``None`` + warning:三条碰文件的规则整体跳过,其余 pass 照跑。
    * 与 control-plane 的 ``EXPERT_WORK_WORKSPACE_NAS_ROOT``(envFrom 带进来)不一致
      → error + ``SystemExit(2)``:两边看的不是同一棵树,删的就不是登记的那份文件。
    * 目录不存在 / 不是目录 / 是 symlink → error + ``SystemExit(2)``:NAS 没挂上时
      每个登记文件都「看起来不在」,行删了、字节永远留在卷里;拒跑比跳过安全。
    """
    root = settings.workspace_root
    if root is None:
        logger.warning(
            "retention.workspace_root_unset — artifact-version / upload-file / "
            "orphan-thread-dir rules are skipped (set EXPERT_WORK_RETENTION_WORKSPACE_ROOT)"
        )
        return None
    control_plane_root = environ.get(_CONTROL_PLANE_NAS_ROOT_KEY)
    if control_plane_root is not None and control_plane_root != root:
        logger.error(
            "retention.workspace_root_mismatch job=%r control_plane=%r — refusing to run",
            root,
            control_plane_root,
        )
        raise SystemExit(2)
    try:
        resolved = validate_workspace_root(root)
    except ValueError as exc:
        logger.error("retention.workspace_root_invalid %s — refusing to run", exc)
        raise SystemExit(2) from exc
    return str(resolved)


def build_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """B-45 —— 与 control-plane / billing-rollup-job 同款:session factory 必须
    经过 ``build_rls_sessionmaker``,``after_begin`` listener 才在本进程存在。
    此前这个 job 从不调它,RLS Detect 信号在这里根本不会出现,生产 enforce
    之前也就没人知道它哪条路径会 fail-closed。sweep 本身在
    :func:`retention_cleanup_job.job._bypass_rls` 里显式声明跨租户 bypass。"""
    return build_rls_sessionmaker(create_async_session_factory(engine))


def configure_logging(settings: RetentionCleanupSettings, *, stream: IO[str] | None = None) -> None:
    """B-45 —— 装平台的 JSON formatter,而不是 ``logging.basicConfig``。

    Detect 信号的归因(``rls_caller`` / ``rls_caller_outer`` / ``rls_suppressed``)
    是 ``extra=`` 结构化字段。``basicConfig`` 的默认 format 只渲染 message,这些
    字段一个都不落盘 —— 于是「信号打了」而「归因看不见」。#1493 在另外两个进程
    修的是同一处失效;这个 job 是三个里**唯一真部署的**(test overlay CronJob
    已起),所以在它修好之前,今天落 Loki 的就是无归因那条。
    """
    init_logging(
        service=settings.service_name, env=settings.env, level=settings.log_level, stream=stream
    )


def warn_retired_image_retention(settings: RetentionCleanupSettings) -> None:
    """B-283 —— ``EXPERT_WORK_RETENTION_IMAGE_RETENTION_DAYS`` 不再生效:图片与附件同一个天数
    (``upload_retention_days``)。还配着就说一声,免得以为它管用。"""
    if settings.image_retention_days is not None:
        logger.warning(
            "retention.image_retention_days_ignored configured=%d used=upload_retention_days=%d",
            settings.image_retention_days,
            settings.upload_retention_days,
        )


def warn_clamped_retention(settings: RetentionCleanupSettings) -> None:
    """B-63 —— 产物 / 上传保留期配得超出 7-365 的,settings 已夹回范围;这里在日志装好之后
    逐条打 warning,让人知道实际用的不是配的那个数。"""
    for name, configured in settings.clamped_retention.items():
        logger.warning(
            "retention.retention_days_clamped field=%s configured=%d used=%d "
            "— the allowed range is 7-365 days",
            name,
            configured,
            getattr(settings, name),
        )


#: B-271 —— 有 pass 失败(吞掉异常、其余 pass 照跑完)时进程的退出码,让 CronJob 显示失败。
#: 与启动校验拒跑的 ``2`` 区分开。``backoffLimit: 1`` 会再起一个 pod 重跑一次:每个 pass 都
#: 幂等,重跑只是多一份日志,不会多删东西。
EXIT_PASS_FAILED = 3


def exit_code(report: CleanupReport) -> int:
    """``run_once`` 跑完之后进程该以什么退出:有 pass 失败 → :data:`EXIT_PASS_FAILED`。"""
    return EXIT_PASS_FAILED if report.failed_passes else 0


def log_done(report: CleanupReport) -> None:
    """The one ``retention_cleanup_job.done`` line ops (and the runbooks) read."""
    logger.info(
        "retention_cleanup_job.done audit=%d audit_skipped_unacked=%d "
        "event=%d jwt=%d image_rows=%d image_keys_ok=%d image_keys_failed=%d "
        "artifact_soft=%d artifact_hard=%d "
        "artifact_versions_deleted=%d artifact_files_removed=%d artifacts_expired=%d "
        "orphan_artifact_copies_removed=%d "
        "purged_agent_dirs_removed=%d purged_agent_dirs_failed=%d "
        "trajectories_expired=%d trajectory_orphans_removed=%d trajectories_failed=%d "
        "trajectories_scanned=%d trajectories_capped=%s trajectories_skipped=%s "
        "uploads_expired=%d upload_files_removed=%d upload_files_unsafe=%d "
        "upload_rows_failed=%d upload_rows_abandoned=%d upload_rows_held=%d "
        "upload_owners_unavailable=%d upload_rows_failing_overdue=%d "
        "conversation_dirs_removed=%d conversation_tool_results_removed=%d "
        "conversation_dirs_skipped_active=%d conversation_dirs_skipped_artifact=%d "
        "conversation_dirs_skipped_unsafe=%d conversation_dirs_skipped_owner_deleted=%d "
        "conversation_dirs_failed=%d conversation_tool_results_skipped_artifact=%d "
        "conversation_dirs_raced=%d "
        "agent_root_files_removed=%d agent_root_dirs_removed=%d shared_dirs_removed=%d "
        "agent_root_failed=%d agent_root_skipped_unknown=%d agent_root_skipped_symlinks=%d "
        "agent_root_skipped_changed=%d agent_file_version_dirs_dropped=%d "
        "agent_file_versions_capped=%d agent_file_versions_failed=%d "
        "thread_dirs_removed=%d "
        "memory_hard_deleted=%d workspaces_hard_deleted=%d "
        "workspaces_pending_archive=%d tenant_users_hard_deleted=%d "
        "sandbox_egress_audit=%d memory_writeback_jobs=%d knowledge_tasks=%d "
        "knowledge_revisions=%d knowledge_retrieval_logs=%d failed_passes=%s duration=%.2fs",
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
        report.orphan_artifact_copies_removed,
        report.purged_agent_dirs_removed,
        report.purged_agent_dirs_failed,
        report.trajectories_expired,
        report.trajectory_orphans_removed,
        report.trajectories_failed,
        report.trajectories_scanned,
        report.trajectories_capped,
        report.trajectories_skipped,
        report.uploads_expired,
        report.upload_files_removed,
        report.upload_files_unsafe,
        report.upload_rows_failed,
        report.upload_rows_abandoned,
        report.upload_rows_held,
        report.upload_owners_unavailable,
        report.upload_rows_failing_overdue,
        report.conversation_dirs_removed,
        report.conversation_tool_results_removed,
        report.conversation_dirs_skipped_active,
        report.conversation_dirs_skipped_artifact,
        report.conversation_dirs_skipped_unsafe,
        report.conversation_dirs_skipped_owner_deleted,
        report.conversation_dirs_failed,
        report.conversation_tool_results_skipped_artifact,
        report.conversation_dirs_raced,
        report.agent_root_files_removed,
        report.agent_root_dirs_removed,
        report.shared_dirs_removed,
        report.agent_root_failed,
        report.agent_root_skipped_unknown,
        report.agent_root_skipped_symlinks,
        report.agent_root_skipped_changed,
        report.agent_file_version_dirs_dropped,
        report.agent_file_versions_capped,
        report.agent_file_versions_failed,
        report.thread_dirs_removed,
        report.memory_hard_deleted,
        report.workspaces_hard_deleted,
        report.workspaces_pending_archive,
        report.tenant_users_hard_deleted,
        report.sandbox_egress_audit_deleted,
        report.memory_writeback_jobs_deleted,
        report.knowledge_tasks_deleted,
        report.knowledge_revisions_deleted,
        report.knowledge_retrieval_logs_deleted,
        ",".join(report.failed_passes) or "-",
        report.duration_seconds,
    )


async def _amain() -> int:
    settings = RetentionCleanupSettings()
    configure_logging(settings)
    warn_clamped_retention(settings)
    warn_retired_image_retention(settings)
    # 连库之前先定工作区根:不合格就退出,一条 DELETE 都不发。
    workspace_root = resolve_workspace_root(settings, environ=os.environ)

    engine = create_async_engine_from_config(DatabaseConfig(dsn=settings.db_dsn))
    session_factory = build_session_factory(engine)

    async with contextlib.AsyncExitStack() as stack:
        # Mini-ADR J-32 (J.6.补强-3b) — the image pass needs both a
        # registry + the object store. ``memory`` backend skips the
        # pass (in-memory bytes vanish per process anyway).
        #
        # B-286 —— 与 control-plane 同一个对象存储(``EXPERT_WORK_OBJECT_STORE_*`` + 同一个
        # SecretStore 解 ``secret://`` 引用)。解不出来不崩:图片 pass 记跳过、进失败清单。
        plan = await plan_object_store(settings, session_factory)
        object_store, plan = await enter_object_store(stack, plan)
        log_plan(plan)
        image_store = (
            SqlImageUploadStore(session_factory) if plan.backend == "s3-compatible" else None
        )

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
        run_store = SqlRunStore(session_factory)
        audit_log_store = SqlAuditLogStore(session_factory)
        audit_logger = AuditLogger(
            store=audit_log_store,
            redactor=DefaultSecretRedactor(),
            fallback=InMemoryAuditFallbackQueue(),
        )
        job = RetentionCleanupJob(
            db_session_factory=session_factory,
            batch_size=settings.batch_size,
            image_upload_store=image_store,
            object_store=object_store,
            object_store_unavailable=(plan.error or "unavailable") if plan.unavailable else None,
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
            memory_writeback_job_retention_days=settings.memory_writeback_job_retention_days,
            knowledge_task_retention_days=settings.knowledge_task_retention_days,
            knowledge_revision_orphan_days=settings.knowledge_revision_orphan_days,
            # B-175 P2-PR4 — metadata-only like memory_store: always wired.
            knowledge_retrieval_log_store=SqlKnowledgeRetrievalLogStore(session_factory),
            knowledge_retrieval_log_retention_days=settings.knowledge_retrieval_log_retention_days,
            user_upload_store=user_upload_store,
            upload_retention_days=settings.upload_retention_days,
            conversation_workspace_retention_days=(settings.conversation_workspace_retention_days),
            conversation_workspace_batch_size=settings.conversation_workspace_batch_size,
            shared_delete_after=settings.shared_delete_after,
            agent_root_cutover=settings.agent_root_cutover,
            thread_store=thread_store,
            run_store=run_store,
            workspace_root=workspace_root,
            audit_logger=audit_logger,
            audit_log_store=audit_log_store,
        )
        logger.info("retention_cleanup_job.start batch=%d", settings.batch_size)
        report = await job.run_once()
        log_done(report)
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
        if report.upload_rows_failed > 0:
            logger.warning(
                "retention.upload_rows_failed count=%d — retried tomorrow; abandoned after 7 days",
                report.upload_rows_failed,
            )
        if report.failed_passes:
            logger.error("retention.passes_failed passes=%s", ",".join(report.failed_passes))

    await engine.dispose()
    return exit_code(report)


def main() -> None:
    code = asyncio.run(_amain())
    if code:
        raise SystemExit(code)


if __name__ == "__main__":
    main()
