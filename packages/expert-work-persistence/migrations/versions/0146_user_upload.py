"""对外附件模型统一 —— ``user_upload`` registry table.

Revision ID: 0146_user_upload
Revises: 0145_agent_run_idempotency
Create Date: 2026-08-17

Adds the ``user_upload`` registry: one row per landed third-party
attachment (document or image), keyed by an opaque ``upl_<uuid>`` id that
the external API returns to and accepts from third parties. The row's
``ref`` column carries the underlying storage location, whose shape
differs by ``kind`` — an implementation detail third parties never see.

Tenant RLS uses the standard ``current_setting('app.tenant_id')`` GUC
pattern shared with the rest of the schema (see ``image_upload``,
migration 0028).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0146_user_upload"
down_revision: str | Sequence[str] | None = "0145_agent_run_idempotency"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

__all__ = ["branch_labels", "depends_on", "down_revision", "downgrade", "revision", "upgrade"]


def upgrade() -> None:
    op.create_table(
        "user_upload",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", UUID(as_uuid=True), nullable=False),
        sa.Column("thread_id", UUID(as_uuid=True), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("ref", sa.Text(), nullable=False),
        sa.Column("mime_type", sa.Text(), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("filename", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("kind IN ('image','document')", name="user_upload_kind_enum"),
        sa.CheckConstraint("size_bytes >= 0", name="user_upload_size_nonneg"),
    )
    op.create_index(
        "ix_user_upload_tenant_user",
        "user_upload",
        ["tenant_id", "user_id"],
    )
    op.create_index(
        "ix_user_upload_tenant_thread",
        "user_upload",
        ["tenant_id", "thread_id"],
    )

    # Tenant RLS — same ``app.tenant_id`` GUC pattern as image_upload,
    # audit_log, memory_item, user_workspace.
    op.execute("ALTER TABLE user_upload ENABLE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY user_upload_tenant_isolation ON user_upload "
        "USING (tenant_id = current_setting('app.tenant_id', true)::uuid)"
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS user_upload_tenant_isolation ON user_upload")
    op.drop_index("ix_user_upload_tenant_thread", table_name="user_upload")
    op.drop_index("ix_user_upload_tenant_user", table_name="user_upload")
    op.drop_table("user_upload")
