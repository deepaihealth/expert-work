"""B-63 —— uploads expire by LAST USE; per-tenant file retention days.

* ``user_upload.last_used_at`` — stamped (at most once a day) when an upload is
  re-attached to a run or downloaded through the external API. The retention job
  expires an upload when ``greatest(created_at, last_used_at, last run of its
  conversation)`` is older than the retention window.
* ``tenant_config.upload_retention_days`` / ``artifact_retention_days`` — per-tenant
  windows (7-365 days). NULL → the retention job's platform default (90).

**Expand-only.** All three columns are nullable with no default and no backfill;
old code never names them. An image rollback needs no downgrade (the downgrade
drops them).

Revision ID: 0183_b63_upload_last_used
Revises: 0182_batch1103_columns
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0183_b63_upload_last_used"
down_revision: str | Sequence[str] | None = "0182_batch1103_columns"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

__all__ = ["branch_labels", "depends_on", "down_revision", "downgrade", "revision", "upgrade"]

_RANGES = (
    ("upload_retention_days", "tenant_config_upload_retention_range_ck"),
    ("artifact_retention_days", "tenant_config_artifact_retention_range_ck"),
)


def upgrade() -> None:
    op.add_column(
        "user_upload", sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True)
    )
    for column, constraint in _RANGES:
        op.add_column("tenant_config", sa.Column(column, sa.Integer(), nullable=True))
        op.create_check_constraint(
            constraint, "tenant_config", f"{column} IS NULL OR {column} BETWEEN 7 AND 365"
        )


def downgrade() -> None:
    for column, constraint in reversed(_RANGES):
        op.drop_constraint(constraint, "tenant_config", type_="check")
        op.drop_column("tenant_config", column)
    op.drop_column("user_upload", "last_used_at")
