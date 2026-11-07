"""B-206 K-9 —— keyword recall ranks by BM25: each chunk's keyword length.

* ``knowledge_chunk.keyword_token_count`` — BM25's document length (dl): how many
  tokens the body part of ``content_tsv`` (breadcrumb + content, label D; the
  document title, label A, is not counted) holds. New rows get it from the write
  path (the searchable tokens it indexes); here existing rows get it from their
  stored keyword vector — the positions of its D lexemes, the same count wherever
  Postgres' parser keeps jieba's tokens whole. Not ``length(tsvector)``: that is
  the number of distinct lexemes. Parent rows (no keyword vector) stay NULL.

**Expand-only.** Nullable, no default; old code never names it. A row an old pod
writes during the rollout has NULL, which keyword search reads as the average
length (no length normalisation for that row) until it is re-ingested. An image
rollback needs no downgrade (the downgrade drops the column).

The backfill is one UPDATE over the chunk rows that have a keyword vector (it
rewrites each of them once; the dev corpus's 45k chunks is the size to expect).

Revision ID: 0187_k9_keyword_token_count
Revises: 0186_b282_conv_dir_expiry
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0187_k9_keyword_token_count"
down_revision: str | Sequence[str] | None = "0186_b282_conv_dir_expiry"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

__all__ = ["branch_labels", "depends_on", "down_revision", "downgrade", "revision", "upgrade"]

_COLUMN = "keyword_token_count"

_BACKFILL = """
UPDATE knowledge_chunk
SET keyword_token_count = (
    SELECT coalesce(sum(cardinality(lexemes.positions)), 0)
    FROM unnest(ts_filter(content_tsv, '{d}')) AS lexemes
)
WHERE content_tsv IS NOT NULL AND keyword_token_count IS NULL
"""


def upgrade() -> None:
    op.add_column("knowledge_chunk", sa.Column(_COLUMN, sa.Integer(), nullable=True))
    op.execute(_BACKFILL)


def downgrade() -> None:
    op.drop_column("knowledge_chunk", _COLUMN)
