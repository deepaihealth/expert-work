"""B-63 —— artifact versions expire by LAST USE.

* ``artifact_version.last_used_at`` — stamped (at most once a day) when a version's
  bytes are downloaded through any of the three download endpoints. The retention
  job counts a version's last use as ``greatest(created_at, last_used_at, last run
  of the conversation that registered it)``; the artifact row (and its latest
  version) lives as long as its most recently used version.

**Expand-only.** One nullable column, no default, no backfill; old code never
names it. An image rollback needs no downgrade (the downgrade drops it).

Revision ID: 0184_b63_artifact_last_used
Revises: 0183_b63_upload_last_used
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0184_b63_artifact_last_used"
down_revision: str | Sequence[str] | None = "0183_b63_upload_last_used"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

__all__ = ["branch_labels", "depends_on", "down_revision", "downgrade", "revision", "upgrade"]


def upgrade() -> None:
    op.add_column(
        "artifact_version", sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("artifact_version", "last_used_at")
