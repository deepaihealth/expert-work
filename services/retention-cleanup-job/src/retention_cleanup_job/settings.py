"""``RetentionCleanupSettings`` — env-driven knobs for the cleanup job.

Defaults aimed at local docker-compose; connect direct to Postgres
(not via PgBouncer) so transactional ``SET LOCAL ROLE`` survives.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, PrivateAttr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from expert_work.protocol import FILE_RETENTION_MAX_DAYS, FILE_RETENTION_MIN_DAYS

#: B-63 —— 这两个窗口与租户可设的范围一致:7-365 天(上限从 3650 降到一年)。
_FILE_RETENTION_FIELDS = (
    "artifact_retention_days",
    "upload_retention_days",
    "conversation_workspace_retention_days",
)


class RetentionCleanupSettings(BaseSettings):
    """Resolved runtime settings."""

    model_config = SettingsConfigDict(
        env_prefix="EXPERT_WORK_RETENTION_",
        case_sensitive=False,
        extra="ignore",
    )

    service_name: str = "retention_cleanup_job"
    log_level: str = "INFO"
    #: B-45 —— 平台 JSON formatter 的 ``env`` 标签(control-plane 同名字段)。
    env: Literal["dev", "staging", "prod"] = "dev"

    # ------------------------------------------------------------------ db
    db_dsn: str = "postgresql+asyncpg://expert_work:expert_work_dev@localhost:5432/expert_work_dev"
    db_echo: bool = False

    # ------------------------------------------------------------------ tuning
    # Per-table per-sweep batch ceiling. Bounded so a single sweep
    # doesn't take an autovacuum-blocking lock for too long; with
    # ``WHERE`` predicates each DELETE is plan-time pruned.
    batch_size: int = Field(default=10000, gt=0, le=1000000)

    # --------------------------------------------------- image retention (Mini-ADR J-32)
    # (Was: a global ``created_at``-based hard-delete window for ``image_upload``.)
    #
    # B-283: RETIRED. Images expire by LAST USE on the same clock and the same days
    # as uploads (``upload_retention_days`` / the tenant's
    # ``tenant_config.upload_retention_days``), so an image's registry row and its
    # bytes can never expire on different days. Still parsed only so ``main`` can
    # warn that a configured value is ignored.
    image_retention_days: int | None = None

    # --------------------------------------------------- artifact retention (Mini-ADR J-25)
    # Active artifact rows with ``updated_at < now() - artifact_retention_days``
    # are soft-deleted; soft-deleted rows past
    # ``artifact_hard_delete_grace_days`` are hard-deleted entirely.
    # M0 defaults match the J-25 spec (90 days active → soft → 60 days
    # → hard). Workspace files are *not* removed here; J.15 volume
    # lifecycle owns the bytes.
    #
    # B-63: platform default for tenants whose ``tenant_config.artifact_retention_days``
    # is NULL. Range 7-365 (outside it is clamped with a warning, see
    # ``_clamp_file_retention``). MUST equal ``expert_work.protocol.FILE_RETENTION_DEFAULT_DAYS``
    # (the control plane's "is this a decrease" baseline) — both are 90.
    artifact_retention_days: int = Field(default=90, ge=1)
    artifact_hard_delete_grace_days: int = Field(default=60, ge=1, le=3650)

    # --------------------------------------------------- deletion hygiene PR1 (Task 7)
    # Three 90-day physical hard-delete sweeps, each gated on its own
    # soft-delete / deactivation timestamp already reached by the upstream
    # (K.K6 memory forget, J.15 workspace archive, Phase 3a purge_user)
    # flows. No per-tenant override in M0 — same rationale as the image
    # retention knob above.
    #
    # NOTE: these knobs also bound the ``purge_user`` "recoverable for 90
    # days" window (spec 2026-07-24 D1/D4) — purged users' memory and the
    # tenant_user row are recoverable only until the respective sweep
    # fires. Do not lower below the recovery SLA promised to operators.
    memory_hard_delete_grace_days: int = Field(default=90, ge=1, le=3650)
    workspace_archive_retention_days: int = Field(default=90, ge=1, le=3650)
    tenant_user_hard_delete_grace_days: int = Field(default=90, ge=1, le=3650)

    # --------------------------------------------------- 沙箱出网审计 (波 1 PR-E)
    # ``sandbox_egress_audit`` 每次沙箱出网写一行(``allowed`` 占绝大多数),
    # 此前没有任何清理路径。与上面几个窗口同样是 M0 全局旋钮、无 per-tenant
    # 覆盖。这张表是运维/安全遥测,不是 audit_log 那种合规 WORM 表,所以没有
    # ``backup_acked`` 之类的前置闸。
    sandbox_egress_audit_retention_days: int = Field(default=90, ge=1, le=3650)

    # --------------------------------------------------- 记忆后台写回任务(B-168)
    # ``memory_writeback_job`` 每轮一行(只存指针)。收尾(``done`` / ``failed``)满这个
    # 天数就删;``pending`` / ``running`` 不按时间删(还没写完的记忆)。30 天内控制台
    # 的 run 详情还看得到「后台记忆写回」那一行。
    memory_writeback_job_retention_days: int = Field(default=30, ge=1, le=3650)

    # --------------------------------------------------- 知识库任务(B-175 L2-PR3)
    # ``knowledge_task`` 一次导入 / 重解析 / 重建索引一行。收尾(``succeeded`` /
    # ``failed`` / ``cancelled``)满这个天数就删;``queued`` / ``running`` 不按时间删。
    # 全局旋钮(与上面几个窗口一样),没有按租户覆盖。
    knowledge_task_retention_days: int = Field(default=90, ge=1, le=3650)

    # --------------------------------------------------- 知识库分段修订(B-175 P3-PR2)
    # 重新入库后整块找不到了、人工修改没能套回去的修订(``orphaned_at`` 有值),满这个
    # 天数就删(修订正文可能含健康数据)。整块还在的修订不按时间删,随文档删除。用户
    # 给检索记录定的期限是 30 天,这里同一期限。
    knowledge_revision_orphan_days: int = Field(default=30, ge=1, le=3650)

    # --------------------------------------------------- knowledge retrieval log (B-175 P2-PR4)
    # ``knowledge_retrieval_log``: one row per knowledge search (agent + console
    # test), the question asked included. User ruling (2026-10-27): kept 30 days.
    # Global knob like the windows above, no per-tenant override.
    knowledge_retrieval_log_retention_days: int = Field(default=30, ge=1, le=3650)

    # --------------------------------------------------- 留存链 PR2(波 3 线 R)
    # 用户拍板(2026-09-09):产物 90 天、上传 90 天、已删工作区库行 90 天销账。
    # 产物版本复用上面的 ``artifact_retention_days``,已删工作区复用
    # ``workspace_archive_retention_days``;上传是新旋钮。三个都是 job 级默认,
    # test / prod overlay 不另设。
    #
    # B-63:按**最后使用**算(created_at / last_used_at / 会话最后一次 run 最晚的那个),
    # 这是 ``tenant_config.upload_retention_days`` 为空时的平台默认。范围 7-365,超出的
    # 夹回范围并打 warning(``_clamp_file_retention``)。**必须与协议里的
    # ``FILE_RETENTION_DEFAULT_DAYS`` 一致**(控制面判「改小」的基准),两边都是 90。
    upload_retention_days: int = Field(default=90, ge=1)
    # B-282 —— 闲置会话的工作目录(``agents/<key>/threads/<会话>/`` 与各 run 的
    # ``.tool_results/<run>/``)在会话最后一次 run 之后保留多少天;这是
    # ``tenant_config.conversation_workspace_retention_days`` 为空时的平台默认。范围 7-365
    # (同上,夹回并 warning),**必须与协议的 ``FILE_RETENTION_DEFAULT_DAYS`` 一致**。
    conversation_workspace_retention_days: int = Field(default=90, ge=1)
    # 一轮最多处理多少段闲置会话(每段几次 NAS 删除 + 两三条查询)。
    conversation_workspace_batch_size: int = Field(default=500, gt=0, le=100000)
    # B-284 —— 冻结的 ``shared/``(B-50 搬迁时归属不明的 legacy)从这个时刻起整个删掉。
    # 运维在发布那天设成「发布 + 30 天」;不设 / 空 = 永不删。只认 ``YYYY-MM-DD``(UTC 零点),
    # 别的写法 / 数字 / 早于功能上线的日期 → 记错误、只跳过 shared/ 这一步(作业照跑);
    # 原样存字符串,解析在 ``agent_roots.parse_shared_delete_after``。
    shared_delete_after: str | None = None
    # B-284 —— agent 根存量的切换时刻 T:这个环境 B-236 上线的时刻,带时区的 ISO 时间
    # (测试 ``2026-10-30T23:48:00+08:00``,生产 ``2026-11-04T19:10:00+08:00``)。不设 = 那条
    # 规则不跑;写错 → 记错误、那条规则不跑。解析在 ``agent_roots.parse_cutover``。值钉在
    # ``infra/k8s/overlays/{test,prod}/retention-cleanup-patch.yaml``。
    agent_root_cutover: str | None = None
    # NAS 工作区根 —— 与 control-plane 的 EXPERT_WORK_WORKSPACE_NAS_ROOT 同一个挂载
    # 点(CronJob 挂同一个 PVC)。``None`` → 三条碰文件的规则(产物版本 / 上传文件 /
    # 孤儿 threads 目录)整体跳过并打 warning:只删行不删文件会留下永远没人认领
    # 的字节,所以没有「半开」这一档。
    workspace_root: str | None = None

    # Object-store backend that owns the uploaded image bytes. ``memory``
    # (default) skips the image pass — useful for unit-tested local cron
    # ticks and for envs that haven't deployed J.6 yet. ``s3-compatible``
    # points at the same MinIO / OSS / S3 bucket the control-plane writes.
    object_store_backend: Literal["memory", "s3-compatible"] = "memory"
    object_store_endpoint_url: str | None = None
    object_store_region: str = "us-east-1"
    object_store_bucket: str = "expert-work"
    object_store_access_key: str | None = None
    object_store_secret_key: str | None = None
    #: S3 addressing style. ``"path"`` (default) matches MinIO local dev.
    #: 阿里云 OSS prod MUST use ``"virtual"`` — W0 real-bucket testing found
    #: OSS rejects path-style addressing (``SecondLevelDomainForbidden``)
    #: and does not reliably resolve ``"auto"`` to virtual-hosted style.
    object_store_addressing_style: Literal["path", "virtual", "auto"] = "path"

    #: B-63 —— 被夹回 7-365 的配置 ``{字段: 原值}``;``main`` 装好日志之后逐条打 warning
    #: (构造 settings 时日志还没装上)。
    _clamped: dict[str, int] = PrivateAttr(default_factory=dict)

    @model_validator(mode="after")
    def _clamp_file_retention(self) -> RetentionCleanupSettings:
        """B-63 —— 产物 / 上传保留期 7-365 天。超出范围的配置**不拒跑**(那会让每晚的清理整个
        停掉):夹回范围并记下来,由 ``main`` 打 warning。仓库里的 overlay 都没设这两个键(用默认
        90)。"""
        for name in _FILE_RETENTION_FIELDS:
            configured = getattr(self, name)
            clamped = min(max(configured, FILE_RETENTION_MIN_DAYS), FILE_RETENTION_MAX_DAYS)
            if clamped != configured:
                self._clamped[name] = configured
                setattr(self, name, clamped)
        return self

    @property
    def clamped_retention(self) -> dict[str, int]:
        """被夹回 7-365 的字段与它们原来配的值(没有就是空)。"""
        return dict(self._clamped)
