"""0168 — ``knowledge_base.rerank_score_threshold``:按重排分数拒答的下限(B-175 波 2 PR3)。

重排分数低于它的段丢掉,全部丢掉 = 这次检索什么都不返回(工具告诉模型「库里没找到」)。
原有的 ``retrieval_score_threshold`` 只管向量相似度、关键词命中绕过它,混合检索下永远有结果。

只用来挡明显离题的问题:「问的是库里的东西、但资料没写」这类,重排分数分不开(评测实测,
见波 2 计划 PR3 节),那一类靠工具结果里给模型的提示。分数尺度随重排模型而定,换模型要重新标定;
NULL = 不启用(默认),行为与改前相同。取值约束与 0100 的 ``retrieval_score_threshold`` 同形
(手写 SQL 写进越界值会让读库校验报错、整个租户的检索都失败)。

expand-only:新列可空,旧镜像不读;回滚不需要 downgrade。

Revision ID: 0168_kb_rerank_score_threshold
Revises: 0167_knowledge_chunk_doc_title
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0168_kb_rerank_score_threshold"
down_revision: str | Sequence[str] | None = "0167_knowledge_chunk_doc_title"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

__all__ = ["branch_labels", "depends_on", "down_revision", "downgrade", "revision", "upgrade"]

_TABLE = "knowledge_base"
_CHECK = "knowledge_base_rerank_score_threshold_check"


def upgrade() -> None:
    op.add_column(_TABLE, sa.Column("rerank_score_threshold", sa.Float(), nullable=True))
    op.create_check_constraint(
        _CHECK,
        _TABLE,
        "rerank_score_threshold IS NULL OR rerank_score_threshold BETWEEN 0 AND 1",
    )


def downgrade() -> None:
    op.drop_constraint(_CHECK, _TABLE, type_="check")
    op.drop_column(_TABLE, "rerank_score_threshold")
