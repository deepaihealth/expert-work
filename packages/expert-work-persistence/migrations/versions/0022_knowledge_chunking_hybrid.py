"""Stream J.5 — per-KB chunking config + hybrid-search full-text column.

Revision ID: 0022_knowledge_chunking_hybrid
Revises: 0021_knowledge_base
Create Date: 2026-05-19

Two J.5 additions on top of ``0021``:

* ``knowledge_base`` gains per-base chunking parameters
  (``chunk_max_tokens`` / ``chunk_overlap_tokens``) so different
  knowledge bases can tune chunk granularity to their content.

* ``knowledge_chunk`` gains a ``content_tsv`` ``tsvector`` column +
  GIN index — the keyword side of hybrid retrieval (vector recall +
  keyword recall, RRF-fused). The column is populated app-side by the
  ``KnowledgeStore`` from jieba-segmented text under the ``simple``
  config: Postgres' built-in text-search configs do not segment CJK,
  so app-side tokenization keeps keyword search correct for Chinese
  without a ``zhparser`` / ``pg_jieba`` extension.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import TSVECTOR

#: The platform defaults when this revision was written. Literals, not the
#: protocol constants: a migration must keep meaning what it meant — the
#: defaults moved to 400 / 0 in 0174 (B-206 K-3).
DEFAULT_CHUNK_MAX_TOKENS = 512
DEFAULT_CHUNK_OVERLAP_TOKENS = 64

revision: str = "0022_knowledge_chunking_hybrid"
down_revision: str | Sequence[str] | None = "0021_knowledge_base"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

__all__ = ["branch_labels", "depends_on", "down_revision", "downgrade", "revision", "upgrade"]


def upgrade() -> None:
    op.add_column(
        "knowledge_base",
        sa.Column(
            "chunk_max_tokens",
            sa.Integer(),
            nullable=False,
            server_default=sa.text(str(DEFAULT_CHUNK_MAX_TOKENS)),
        ),
    )
    op.add_column(
        "knowledge_base",
        sa.Column(
            "chunk_overlap_tokens",
            sa.Integer(),
            nullable=False,
            server_default=sa.text(str(DEFAULT_CHUNK_OVERLAP_TOKENS)),
        ),
    )
    op.add_column(
        "knowledge_chunk",
        sa.Column("content_tsv", TSVECTOR(), nullable=True),
    )
    op.create_index(
        "knowledge_chunk_tsv_idx",
        "knowledge_chunk",
        ["content_tsv"],
        postgresql_using="gin",
    )


def downgrade() -> None:
    op.drop_index("knowledge_chunk_tsv_idx", table_name="knowledge_chunk")
    op.drop_column("knowledge_chunk", "content_tsv")
    op.drop_column("knowledge_base", "chunk_overlap_tokens")
    op.drop_column("knowledge_base", "chunk_max_tokens")
