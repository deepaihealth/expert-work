"""B-282 —— conversation work directories expire after N idle days.

* ``tenant_config.conversation_workspace_retention_days`` — per-tenant window
  (7-365 days) a conversation's ``agents/<key>/threads/<conv>/`` directory
  survives after the conversation's last run. NULL → the retention job's platform
  default (90).
* ``thread_meta.workspace_cleaned_at`` — when the retention job last removed the
  conversation's work directory. A later run makes the conversation eligible
  again; until then the job skips it (so the nightly batch is not eaten by
  conversations it already cleaned).
* ``thread_meta.workspace_clean_attempted_at`` — when the job last listed the
  conversation but did not clean it (activity found under the lock, a failure, an
  artifact still reading from it). The next batch puts such conversations after
  the ones never tried, so the same un-cleanable ones cannot fill every batch.

**Expand-only.** All three columns are nullable with no default and no backfill; old
code never names them. An image rollback needs no downgrade (the downgrade
drops them).

Revision ID: 0186_b282_conv_dir_expiry
Revises: 0185_b283_image_last_used
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0186_b282_conv_dir_expiry"
down_revision: str | Sequence[str] | None = "0185_b283_image_last_used"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

__all__ = ["branch_labels", "depends_on", "down_revision", "downgrade", "revision", "upgrade"]

_COLUMN = "conversation_workspace_retention_days"
_CONSTRAINT = "tenant_config_conv_workspace_retention_range_ck"


def upgrade() -> None:
    op.add_column("tenant_config", sa.Column(_COLUMN, sa.Integer(), nullable=True))
    op.create_check_constraint(
        _CONSTRAINT, "tenant_config", f"{_COLUMN} IS NULL OR {_COLUMN} BETWEEN 7 AND 365"
    )
    op.add_column(
        "thread_meta", sa.Column("workspace_cleaned_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "thread_meta",
        sa.Column("workspace_clean_attempted_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("thread_meta", "workspace_clean_attempted_at")
    op.drop_column("thread_meta", "workspace_cleaned_at")
    op.drop_constraint(_CONSTRAINT, "tenant_config", type_="check")
    op.drop_column("tenant_config", _COLUMN)
