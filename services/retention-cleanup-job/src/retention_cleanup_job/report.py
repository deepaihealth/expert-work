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
    # B-235 —— 找不到版本行的产物副本(登记失败 / 清除用户没删成)满 24 小时清掉。
    orphan_artifact_copies_removed: int = 0
    #: B-278 —— 彻底删除智能体时挪进隔离区(``{tenant}/.purged-agents``)满 7 天删掉的条目,
    #: 与删不掉的条目数(非 0 时 ``failed_passes`` 带 ``purged_agents``)。
    purged_agent_dirs_removed: int = 0
    purged_agent_dirs_failed: int = 0
    #: B-285 —— 轨迹对象:到期删掉的、对话已不在而删掉的、删不掉的(非 0 时
    #: ``failed_passes`` 带 ``trajectories``)。
    trajectories_expired: int = 0
    trajectory_orphans_removed: int = 0
    trajectories_failed: int = 0
    #: 列出来看过的键数;删除预算用完(剩下的明天删);这一轮没跑(没接对象存储 ——
    #: B-286 之前的部署环境)。``skipped`` 与「跑了、没东西可删」靠它区分。
    trajectories_scanned: int = 0
    trajectories_capped: bool = False
    trajectories_skipped: bool = False
    # 留存链 —— 上传 90 天。
    uploads_expired: int = 0
    upload_files_removed: int = 0
    #: B-271 —— 路径形状不安全(某段是软链 / 不是目录、叶子不是普通文件)而没删文件的到期行;
    #: 行照样软删。非 0 就该有人去看那个工作区。
    upload_files_unsafe: int = 0
    #: B-271 —— 暂时性失败、这一轮跳过的上传行(明天再试)。
    upload_rows_failed: int = 0
    #: B-271 —— 过了到期日 7 天还在失败、不再等的上传行:行已软删,文件没动。
    upload_rows_abandoned: int = 0
    #: B-271 —— 要放弃的行涉及的用户太多(像环境问题),熔断拦下、这一轮没标的行。
    upload_rows_held: int = 0
    #: B-271 —— ``{tenant}`` / ``{user}`` 目录打不开的用户数(他们这一轮的上传行跳过、不标)。
    upload_owners_unavailable: int = 0
    #: B-63 —— 过了到期日 7 天还在暂时失败、因租户配置改动推迟放弃而仍在重试的上传行。
    upload_rows_failing_overdue: int = 0
    #: B-282 —— 闲置会话的工作目录:删掉的会话目录、agent 根下的 ``.tool_results/<run>/``,
    #: 这一轮因为有没结束的 run / 产物保护 / 路径有符号链接 / 用户已软删而没删的,与删失败的
    #: (非 0 时 ``failed_passes`` 带 ``conversation_dirs``)。
    conversation_dirs_removed: int = 0
    conversation_tool_results_removed: int = 0
    conversation_dirs_skipped_active: int = 0
    conversation_dirs_skipped_artifact: int = 0
    conversation_dirs_skipped_unsafe: int = 0
    conversation_dirs_skipped_owner_deleted: int = 0
    conversation_dirs_failed: int = 0
    conversation_tool_results_skipped_artifact: int = 0
    conversation_dirs_raced: int = 0
    #: B-284 —— agent 根 B-236 之前的存量(顶层文件 / 模板子目录)、过期的 ``shared/``、
    #: 删失败的项(非 0 时 ``failed_passes`` 带 ``agent_roots``)。
    agent_root_files_removed: int = 0
    agent_root_dirs_removed: int = 0
    shared_dirs_removed: int = 0
    agent_root_failed: int = 0
    agent_root_skipped_unknown: int = 0
    agent_root_skipped_symlinks: int = 0
    agent_root_skipped_changed: int = 0
    #: B-284 —— ``/agent`` 文件历史版本:源文件不见满 30 天整个删掉的版本目录、满 90 天删掉的
    #: 老版本、出错的版本目录(非 0 时 ``failed_passes`` 带 ``agent_file_versions``)。
    agent_file_version_dirs_dropped: int = 0
    agent_file_versions_capped: int = 0
    agent_file_versions_failed: int = 0
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
    # B-168 —— 收尾满保留期的记忆后台写回任务行。
    memory_writeback_jobs_deleted: int = 0
    # B-175 L2-PR3 —— 收尾满保留期的知识库任务行。
    knowledge_tasks_deleted: int = 0
    # B-175 P3-PR2 —— 重新入库没能套回去满 30 天的分段修订行。
    knowledge_revisions_deleted: int = 0
    # B-175 P2-PR4 — knowledge retrieval log rows past their window (30 days).
    knowledge_retrieval_logs_deleted: int = 0
    duration_seconds: float = 0.0
    # Per-tenant breakdown of audit deletes (for observability).
    audit_deleted_by_tenant: dict[str, int] = field(default_factory=dict)
    #: B-271 —— 吞掉异常继续跑的 pass 里失败了的(``"uploads"`` / ``"orphan_artifact_copies"``)。
    #: 非空时进程以 :data:`retention_cleanup_job.main.EXIT_PASS_FAILED` 退出,CronJob 显示失败。
    failed_passes: tuple[str, ...] = ()


__all__ = ["CleanupReport"]
