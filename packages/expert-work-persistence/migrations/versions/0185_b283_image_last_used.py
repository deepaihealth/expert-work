"""B-283 —— uploaded images expire by LAST USE.

* ``image_upload.last_used_at`` — stamped (at most once a day) when the image's
  bytes are downloaded through the external uploads endpoint. The retention job
  counts an image's last use as ``greatest(created_at, last_used_at, last run of
  its conversation)`` plus the tenant's ``upload_retention_days`` (images are
  uploads; NULL = the job's image default, 90).

**Expand-only.** One nullable column, no default, no backfill; old code never
names it. An image rollback needs no downgrade (the downgrade drops it).

Revision ID: 0185_b283_image_last_used
Revises: 0184_b63_artifact_last_used
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0185_b283_image_last_used"
down_revision: str | Sequence[str] | None = "0184_b63_artifact_last_used"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

__all__ = ["branch_labels", "depends_on", "down_revision", "downgrade", "revision", "upgrade"]


def upgrade() -> None:
    op.add_column(
        "image_upload", sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("image_upload", "last_used_at")
