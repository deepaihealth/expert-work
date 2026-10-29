"""0179 — estimate an ingest before it starts, confirm it, keep what it cost (B-222 PR-2).

* ``knowledge_document.ingest_estimate`` (JSONB): the latest estimate — line
  items, totals, the models it was made for — plus, for the store only, how to
  put the document back when the estimate is withdrawn. ``ingest_actual``
  (JSONB): what the latest paid ingest was billed. ``confirm_by``
  (TIMESTAMPTZ): until when an estimate waits for its confirmation (7 days),
  then it is withdrawn. All nullable.
* Two new statuses use the free-text ``status`` column (no CHECK on it):
  ``estimating`` (claimed like ``pending``, but only for the free estimate) and
  ``awaiting_confirmation`` (never claimed). ``knowledge_document_recovery_idx``
  is rebuilt to cover ``estimating``; an older image's claim
  (``status IN ('pending', 'processing')``) still implies the new predicate, so
  it keeps using the index. ``knowledge_document_confirm_by_idx`` backs the
  expiry sweep.
* ``knowledge_parse_snapshot``: the estimate's local parse (gzip JSON), so the
  confirmed ingest does not parse again. One row per document (the latest
  estimate's), ``ON DELETE CASCADE`` from ``knowledge_document`` (deleting a
  document or a base takes it; the stores also delete it explicitly). It can
  hold document text, possibly health data: tenant-isolated like every
  knowledge table (ENABLE + FORCE + the ``app.tenant_id`` policy). Kept until
  ``expires_at`` (the confirmation deadline, one day after a confirmation) or
  until the ingest settles; the recovery worker purges what expired.

expand-only: an older image never writes or reads the new columns / table; one
that reads a document in a new status reports it as failed (B-222 PR-1).
**Rollback**: before swapping only the image back past B-222 PR-1, run the
downgrade's ``UPDATE`` by hand (documents waiting for an estimate or a
confirmation become ``failed``).

Revision ID: 0179_kb_ingest_estimate
Revises: 0178_run_unresolved_failures
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "0179_kb_ingest_estimate"
down_revision: str | Sequence[str] | None = "0178_run_unresolved_failures"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

__all__ = ["branch_labels", "depends_on", "down_revision", "downgrade", "revision", "upgrade"]

_DOCUMENT = "knowledge_document"
_SNAPSHOT = "knowledge_parse_snapshot"
_RECOVERY_IDX = "knowledge_document_recovery_idx"
_CONFIRM_IDX = "knowledge_document_confirm_by_idx"

#: What a document waiting for an estimate or a confirmation becomes when the
#: schema (or the image) goes back to before B-222 PR-2.
ROLLBACK_UPDATE = (
    "UPDATE knowledge_document SET status = 'failed', "
    "error = 'ingest estimate rolled back; re-ingest to run it', "
    "claimed_at = NULL, lease_until = NULL "
    "WHERE status IN ('estimating', 'awaiting_confirmation')"
)


def upgrade() -> None:
    op.add_column(_DOCUMENT, sa.Column("ingest_estimate", JSONB(), nullable=True))
    op.add_column(_DOCUMENT, sa.Column("ingest_actual", JSONB(), nullable=True))
    op.add_column(_DOCUMENT, sa.Column("confirm_by", sa.DateTime(timezone=True), nullable=True))
    op.drop_index(_RECOVERY_IDX, table_name=_DOCUMENT)
    op.create_index(
        _RECOVERY_IDX,
        _DOCUMENT,
        ["lease_until"],
        postgresql_where=sa.text("status IN ('pending', 'processing', 'estimating')"),
    )
    op.create_index(
        _CONFIRM_IDX,
        _DOCUMENT,
        ["confirm_by"],
        postgresql_where=sa.text("status = 'awaiting_confirmation'"),
    )

    op.create_table(
        _SNAPSHOT,
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column(
            "document_id",
            UUID(as_uuid=True),
            sa.ForeignKey(
                f"{_DOCUMENT}.id", ondelete="CASCADE", name="fk_knowledge_parse_snapshot_document"
            ),
            primary_key=True,
        ),
        sa.Column("estimate_id", UUID(as_uuid=True), nullable=False),
        sa.Column("format", sa.Text(), nullable=False),
        sa.Column("payload", sa.LargeBinary(), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("knowledge_parse_snapshot_expires_idx", _SNAPSHOT, ["expires_at"])
    op.execute(f"ALTER TABLE {_SNAPSHOT} ENABLE ROW LEVEL SECURITY;")
    op.execute(f"ALTER TABLE {_SNAPSHOT} FORCE ROW LEVEL SECURITY;")
    op.execute(
        f"""
        CREATE POLICY {_SNAPSHOT}_tenant_isolation ON {_SNAPSHOT}
            USING      (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
            WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);
        """
    )


def downgrade() -> None:
    op.execute(ROLLBACK_UPDATE)
    op.execute(f"DROP POLICY IF EXISTS {_SNAPSHOT}_tenant_isolation ON {_SNAPSHOT};")
    op.drop_table(_SNAPSHOT)
    op.drop_index(_CONFIRM_IDX, table_name=_DOCUMENT)
    op.drop_index(_RECOVERY_IDX, table_name=_DOCUMENT)
    op.create_index(
        _RECOVERY_IDX,
        _DOCUMENT,
        ["lease_until"],
        postgresql_where=sa.text("status IN ('pending', 'processing')"),
    )
    op.drop_column(_DOCUMENT, "confirm_by")
    op.drop_column(_DOCUMENT, "ingest_actual")
    op.drop_column(_DOCUMENT, "ingest_estimate")
