"""``artifact_version`` gains ``snapshot_key`` — a copy of each version's bytes (B-235).

``save_artifact`` used to record only the workspace path. Two conversations of the
same user and agent that save the same name both point their versions at one file,
so the later save silently replaces the earlier version's content. From B-235 on,
every save copies the file's bytes at that moment to a location the sandbox cannot
reach, named by the version row id; this column records that copy.

A row that names a copy must carry its size and sha256: they are computed while the
copy is taken, never lazily (the CHECK below).

**Expand-only, no backfill**: NULL = registered before B-235 — no copy exists, and
the content can only be read from ``path_in_workspace`` as before. Old code never
writes the column, so its inserts stay NULL and pass the CHECK; an image rollback
needs no downgrade.

Revision ID: 0180_artifact_version_snapshot
Revises: 0179_kb_ingest_estimate
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0180_artifact_version_snapshot"
down_revision: str | Sequence[str] | None = "0179_kb_ingest_estimate"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

__all__ = ["branch_labels", "depends_on", "down_revision", "downgrade", "revision", "upgrade"]

_CHECK = "artifact_version_snapshot_digest"


def upgrade() -> None:
    op.add_column("artifact_version", sa.Column("snapshot_key", sa.Text(), nullable=True))
    op.create_check_constraint(
        _CHECK,
        "artifact_version",
        "snapshot_key IS NULL OR (size_bytes IS NOT NULL AND sha256 IS NOT NULL)",
    )


def downgrade() -> None:
    op.drop_constraint(_CHECK, "artifact_version", type_="check")
    op.drop_column("artifact_version", "snapshot_key")
