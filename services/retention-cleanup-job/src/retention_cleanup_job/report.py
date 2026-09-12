"""``CleanupReport`` —— one ``run_once`` sweep's tally (split out of ``job.py`` to
keep that module under the 800-line ceiling; re-exported from there so
``from retention_cleanup_job.job import CleanupReport`` keeps working)."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class CleanupReport:
    """Tally produced by one ``run_once`` sweep."""

    audit_deleted: int = 0
    audit_skipped_unacked: int = 0
    event_deleted: int = 0
    jwt_blacklist_deleted: int = 0
    # Mini-ADR J-32 (J.6.补强-3b) — image lifecycle hard-delete counts.
    image_uploads_hard_deleted: int = 0
    image_object_keys_removed: int = 0
    image_object_keys_failed: int = 0
    # Mini-ADR J-25 (J.9-step1) — artifact lifecycle counts.
    artifacts_soft_deleted: int = 0
    artifacts_hard_deleted: int = 0
    # 留存链 B-28 —— 产物按版本 90 天。
    artifact_versions_deleted: int = 0
    artifact_files_removed: int = 0
    artifacts_expired: int = 0
    # 留存链 —— 上传 90 天。
    uploads_expired: int = 0
    upload_files_removed: int = 0
    # 留存链 B-27 —— 孤儿 threads/<id>/ 目录。
    thread_dirs_removed: int = 0
    #: B-50 spec §4.2 —— 孤儿 ``.tool_results/<run_id>/``。此前从没被清过
    #: (overflow.py 的注释声称留存机制负责,实际不负责),所以首轮这个数
    #: 会明显偏大,之后趋近 0。
    tool_result_dirs_removed: int = 0
    # Deletion hygiene PR1 (Task 7) — 90-day physical hard-delete sweeps.
    memory_hard_deleted: int = 0
    # X-4 ② —— 已删工作区 90 天销账(行 + 从属行;不碰 OSS 归档对象)。
    workspaces_hard_deleted: int = 0
    workspaces_pending_archive: int = 0
    tenant_users_hard_deleted: int = 0
    # 波 1 PR-E —— 沙箱出网审计的保留期清理。
    sandbox_egress_audit_deleted: int = 0
    duration_seconds: float = 0.0
    # Per-tenant breakdown of audit deletes (for observability).
    audit_deleted_by_tenant: dict[str, int] = field(default_factory=dict)


__all__ = ["CleanupReport"]
