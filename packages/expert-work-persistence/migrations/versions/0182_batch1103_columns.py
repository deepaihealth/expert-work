"""Three nullable columns for the 11-03 batch (B-267, B-269/B-270, B-271).

* ``agent_run.started_at`` — when the run first entered RUNNING. ``updated_at``
  moves again at every later status change, so it cannot say how long a run has
  been going; queued and in-flight run reads now report it (B-269/B-270).
* ``user_upload.sha256`` — hex sha256 of the stored bytes, so the retention sweep
  can refuse to delete a file that no longer holds what was uploaded (B-271).
* ``knowledge_base.profile`` — the generated base profile and per-base probe
  calibration used for knowledge-first routing (B-267).

**Expand-only.** All three are nullable with no default and no backfill; old code
never names them. An image rollback needs no downgrade (the downgrade drops them).

Revision ID: 0182_batch1103_columns
Revises: 0181_thread_workspace_layout
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0182_batch1103_columns"
down_revision: str | Sequence[str] | None = "0181_thread_workspace_layout"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

__all__ = ["branch_labels", "depends_on", "down_revision", "downgrade", "revision", "upgrade"]


def upgrade() -> None:
    op.add_column("agent_run", sa.Column("started_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("user_upload", sa.Column("sha256", sa.Text(), nullable=True))
    op.add_column("knowledge_base", sa.Column("profile", JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("knowledge_base", "profile")
    op.drop_column("user_upload", "sha256")
    op.drop_column("agent_run", "started_at")
