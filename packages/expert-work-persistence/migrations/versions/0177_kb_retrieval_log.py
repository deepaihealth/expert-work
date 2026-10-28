"""0177 — the knowledge retrieval log table (B-175 P2-PR4).

``knowledge_retrieval_log``: one row per retrieval — an agent's
``knowledge_search``, a console retrieval test, or the open API (``source``,
CHECKed). It keeps the bases searched (``kb_ids``), the query (at most 2,000
characters, CHECKed; the store cuts longer ones), parameters, how the search
ended, each result's ids and scores (``results`` — never document text), the
degradation flags, latency, and the run, its conversation (``thread_id``) and
the console actor behind it.

* Indexes: ``(tenant_id, created_at DESC)`` for the newest-first lists,
  ``created_at`` for the retention sweep (age alone, every tenant), GIN on
  ``kb_ids`` (a row spanning several bases is listed and deleted under each),
  ``run_id`` and ``thread_id`` where set (user / conversation purge delete by
  them — a conversation's rows by thread, whoever's run wrote them).
* No foreign keys: ``kb_ids`` is an array, and ``agent_run`` / ``thread_meta``
  rows are deleted / anonymised by the purges — the application deletes the log
  rows first (base delete, conversation and user purge, retention).
* Tenant isolation as every knowledge table (ENABLE + FORCE + the
  ``app.tenant_id`` policy, ``test_rls_policy_coverage``).
* ``retention_cleanup_worker`` gets SELECT / DELETE: the retention-cleanup job
  deletes rows past ``knowledge_retrieval_log_retention_days`` (default 30).

expand-only: a new table no older image reads. **Rollback**: swapping the image
back needs nothing (the old image never touches the table); ``downgrade`` drops
it with its rows.

Revision ID: 0177_kb_retrieval_log
Revises: 0176_kb_revision_doc_fk
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID

revision: str = "0177_kb_retrieval_log"
down_revision: str | Sequence[str] | None = "0176_kb_revision_doc_fk"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

__all__ = ["branch_labels", "depends_on", "down_revision", "downgrade", "revision", "upgrade"]

_TABLE = "knowledge_retrieval_log"
_RETENTION_ROLE = "retention_cleanup_worker"


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column(
            "id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")
        ),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("kb_ids", ARRAY(UUID(as_uuid=True)), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("query", sa.Text(), nullable=False),
        sa.Column("params", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("result_count", sa.Integer(), nullable=False),
        sa.Column("results", JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("flags", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.Column("run_id", UUID(as_uuid=True), nullable=True),
        sa.Column("thread_id", UUID(as_uuid=True), nullable=True),
        sa.Column("actor_id", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "source IN ('agent', 'console_test', 'open_api')",
            name="knowledge_retrieval_log_source_enum",
        ),
        sa.CheckConstraint("char_length(query) <= 2000", name="knowledge_retrieval_log_query_len"),
    )
    op.create_index(
        "knowledge_retrieval_log_tenant_time_idx",
        _TABLE,
        ["tenant_id", sa.text("created_at DESC")],
    )
    op.create_index("knowledge_retrieval_log_created_idx", _TABLE, ["created_at"])
    op.create_index(
        "knowledge_retrieval_log_kb_ids_idx", _TABLE, ["kb_ids"], postgresql_using="gin"
    )
    for column in ("run_id", "thread_id"):
        op.create_index(
            f"knowledge_retrieval_log_{column.removesuffix('_id')}_idx",
            _TABLE,
            [column],
            postgresql_where=sa.text(f"{column} IS NOT NULL"),
        )
    op.execute(f"ALTER TABLE {_TABLE} ENABLE ROW LEVEL SECURITY;")
    op.execute(f"ALTER TABLE {_TABLE} FORCE ROW LEVEL SECURITY;")
    op.execute(
        f"""
        CREATE POLICY {_TABLE}_tenant_isolation ON {_TABLE}
            USING      (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
            WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);
        """
    )
    op.execute(f"GRANT SELECT, DELETE ON TABLE {_TABLE} TO {_RETENTION_ROLE};")


def downgrade() -> None:
    op.execute(f"REVOKE SELECT, DELETE ON TABLE {_TABLE} FROM {_RETENTION_ROLE};")
    op.execute(f"DROP POLICY IF EXISTS {_TABLE}_tenant_isolation ON {_TABLE};")
    op.drop_table(_TABLE)
