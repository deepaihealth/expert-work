"""``AuditBackupSettings`` — env-driven knobs for the WORM backup worker.

Per Stream D.1c. Defaults aimed at local docker-compose:

* DB DSN points at the bouncer-less Postgres direct port so
  ``SET LOCAL ROLE`` persists across the read + UPDATE in one txn.
* MinIO matches the existing dev fixture (port 9000, root user).
* ``audit_retention_days_default`` is a global fallback until D.3
  introduces ``tenant_config.audit_retention_days``.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class AuditBackupSettings(BaseSettings):
    """Resolved runtime settings; cheap to construct in tests."""

    model_config = SettingsConfigDict(
        env_prefix="EXPERT_WORK_AUDIT_BACKUP_",
        case_sensitive=False,
        extra="ignore",
    )

    service_name: str = "audit_backup_worker"
    log_level: str = "INFO"

    # ------------------------------------------------------------------ db
    # Bypass PgBouncer's transaction-pooling so ``SET LOCAL ROLE`` holds.
    db_dsn: str = "postgresql+asyncpg://expert_work:expert_work_dev@localhost:5432/expert_work_dev"
    db_echo: bool = False

    # ------------------------------------------------------------------ object store
    object_store_backend: Literal["memory", "s3-compatible"] = "s3-compatible"
    s3_endpoint_url: str = "http://localhost:9000"
    s3_region: str = "us-east-1"
    s3_bucket: str = "expert-work-audit-worm"
    s3_access_key: str = "expert_work"
    s3_secret_key: str = "expert_work_dev_minio"  # noqa: S105 — dev fixture default; prod via env
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

    # ------------------------------------------------------------------ worker tuning
    batch_size: int = Field(default=100, gt=0, le=10000)
    poll_interval_s: float = Field(default=2.0, gt=0, le=60.0)
    max_retries_per_row: int = Field(default=10, gt=0)
    audit_retention_days_default: int = Field(default=90, gt=0, le=3650)

    @property
    def effective_s3_addressing_style(self) -> Literal["path", "virtual", "auto"]:
        """Explicit ``s3_addressing_style`` wins; otherwise derived from
        the legacy ``s3_use_path_style`` bool (``True``→``"path"``,
        ``False``→``"auto"`` — the pre-migration ternary in
        ``factory.py``) so existing deployments keep working unchanged."""
        if self.s3_addressing_style is not None:
            return self.s3_addressing_style
        return "path" if self.s3_use_path_style else "auto"
