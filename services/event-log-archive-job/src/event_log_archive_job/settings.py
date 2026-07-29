"""``EventLogArchiveSettings`` — env-driven knobs for the G.8 archive job.

Defaults aim at the local docker-compose stack; the DB DSN connects
directly to Postgres (not PgBouncer) — the sweep is cross-tenant and
relies on the connecting role bypassing RLS.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class EventLogArchiveSettings(BaseSettings):
    """Resolved runtime settings."""

    model_config = SettingsConfigDict(
        env_prefix="EXPERT_WORK_EVENT_LOG_ARCHIVE_",
        case_sensitive=False,
        extra="ignore",
    )

    service_name: str = "event_log_archive_job"
    log_level: str = "INFO"

    # ------------------------------------------------------------------ db
    db_dsn: str = "postgresql+asyncpg://expert_work:expert_work_dev@localhost:5432/expert_work_dev"
    db_echo: bool = False

    # -------------------------------------------------------- object store
    object_store_backend: Literal["memory", "s3-compatible"] = "s3-compatible"
    s3_endpoint_url: str = "http://localhost:9000"
    s3_region: str = "us-east-1"
    s3_bucket: str = "expert-work-event-log-archive"
    s3_access_key: str = "expert_work"
    s3_secret_key: str = "expert_work_dev_minio"  # noqa: S105 — dev placeholder
    #: Deprecated — superseded by ``s3_addressing_style``. Kept for env
    #: back-compat: existing deployments setting this bool keep getting
    #: the pre-migration ``"path" if true else "auto"`` behavior (see
    #: ``effective_s3_addressing_style``).
    s3_use_path_style: bool = True
    #: S3 addressing style — set explicitly to override the legacy bool
    #: above. ``"path"`` for MinIO local dev, ``"virtual"`` for Aliyun OSS
    #: prod (W0 real-bucket finding: OSS rejects path-style addressing).
    #: ``None`` (default) falls back to the legacy bool for back-compat.
    s3_addressing_style: Literal["path", "virtual", "auto"] | None = None

    # ------------------------------------------------------------------ tuning
    #: Rows older than this many days are archived. 180d ≈ subsystems/20's
    #: "半年后冷归档" default.
    archive_age_days: int = Field(default=180, gt=0, le=3650)
    #: Max ``(tenant, thread, month)`` groups processed per sweep — bounds
    #: how long one cron invocation runs.
    batch_size: int = Field(default=500, gt=0, le=100000)

    @property
    def effective_s3_addressing_style(self) -> Literal["path", "virtual", "auto"]:
        """Explicit ``s3_addressing_style`` wins; otherwise derived from
        the legacy ``s3_use_path_style`` bool (``True``→``"path"``,
        ``False``→``"auto"`` — the pre-migration ternary in
        ``factory.py``) so existing deployments keep working unchanged."""
        if self.s3_addressing_style is not None:
            return self.s3_addressing_style
        return "path" if self.s3_use_path_style else "auto"
