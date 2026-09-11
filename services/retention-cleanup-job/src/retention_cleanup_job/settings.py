"""``RetentionCleanupSettings`` — env-driven knobs for the cleanup job.

Defaults aimed at local docker-compose; connect direct to Postgres
(not via PgBouncer) so transactional ``SET LOCAL ROLE`` survives.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


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
    # Image lifecycle hard-delete window. Rows in ``image_upload`` with
    # ``created_at < now() - image_retention_days`` get their object
    # store key removed + the row hard-deleted. M0 has no per-tenant
    # override; tenants needing longer retention set the env var.
    image_retention_days: int = Field(default=90, ge=1, le=3650)

    # --------------------------------------------------- artifact retention (Mini-ADR J-25)
    # Active artifact rows with ``updated_at < now() - artifact_retention_days``
    # are soft-deleted; soft-deleted rows past
    # ``artifact_hard_delete_grace_days`` are hard-deleted entirely.
    # M0 defaults match the J-25 spec (90 days active → soft → 60 days
    # → hard). Workspace files are *not* removed here; J.15 volume
    # lifecycle owns the bytes.
    artifact_retention_days: int = Field(default=90, ge=1, le=3650)
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

    # --------------------------------------------------- 留存链 PR2(波 3 线 R)
    # 用户拍板(2026-09-09):产物 90 天、上传 90 天、已删工作区库行 90 天销账。
    # 产物版本复用上面的 ``artifact_retention_days``,已删工作区复用
    # ``workspace_archive_retention_days``;上传是新旋钮。三个都是 job 级默认,
    # test / prod overlay 不另设。
    upload_retention_days: int = Field(default=90, ge=1, le=3650)
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
