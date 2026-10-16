"""0167 — ``knowledge_chunk.doc_title``:分段记下入库时的文档名(B-175 波 2 PR2b)。

检索文本(嵌入 / 关键词 / 重排用的那段字)前面加上清洗后的文档名
(``[Document: …]``,见 ``chunk_retrieval_text``):说明书里约一半的段正文不写自己是哪个
产品,问句点名产品时向量和关键词都对不上。关键词索引里文档名单独一段、权重 A,正文仍是 D。

存在段上而不是检索时去查文档表:嵌入和关键词索引都是入库时算好的,文档名要和它们同一时刻定下。
旧段为 NULL,检索文本与改前一字不差;重新入库后才带上。文档改名后要重新入库才会更新。

expand-only:新列可空,旧镜像不读;回滚不需要 downgrade。

Revision ID: 0167_knowledge_chunk_doc_title
Revises: 0166_rate_card_deepseek_flash
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0167_knowledge_chunk_doc_title"
down_revision: str | Sequence[str] | None = "0166_rate_card_deepseek_flash"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

__all__ = ["branch_labels", "depends_on", "down_revision", "downgrade", "revision", "upgrade"]


def upgrade() -> None:
    op.add_column("knowledge_chunk", sa.Column("doc_title", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("knowledge_chunk", "doc_title")
