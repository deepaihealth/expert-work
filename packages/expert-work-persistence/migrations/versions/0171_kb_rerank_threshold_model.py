"""0171 — ``knowledge_base.rerank_threshold_model``:重排分数下限在哪个重排模型下标定(B-206 K-7b)。

各家重排分数的含义不同(请求内相对分 / 按域校准的 0~1 / 可为负的 logit),同一个下限换了重排
模型就不是同一回事。记下设下限时的平台重排模型(``provider/model``);检索时打分的模型不是它,
这次检索就不用这个下限,并在设置页 / 检索测试提示重新标定。不清零:换回原模型,它照旧生效。

回填:已设下限的库记成迁移时平台配置行(``platform_embedding_config``,单行)里的重排模型 ——
改前检索用的就是它,下限只可能是在它下面标定的。没有配置行(只靠环境变量配置)或配置行没开重排
的部署不回填:这些下限的标定模型未知,检索时一律按「换了模型」暂停,提示重新标定(宁可这次不过滤,
也不拿可能过期的下限过滤)。

expand-only:新列可空,旧镜像不读;回滚不需要 downgrade。

Revision ID: 0171_kb_rerank_threshold_model
Revises: 0169_kb_scan_recognition
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0171_kb_rerank_threshold_model"
down_revision: str | Sequence[str] | None = "0169_kb_scan_recognition"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

__all__ = ["branch_labels", "depends_on", "down_revision", "downgrade", "revision", "upgrade"]

_TABLE = "knowledge_base"
_COLUMN = "rerank_threshold_model"


def upgrade() -> None:
    op.add_column(_TABLE, sa.Column(_COLUMN, sa.Text(), nullable=True))
    # A row with rerank off has NULL (or empty) provider / model: the concatenation is NULL.
    op.execute(
        """
        UPDATE knowledge_base kb
           SET rerank_threshold_model =
               NULLIF(c.rerank_provider, '') || '/' || NULLIF(c.rerank_model, '')
          FROM platform_embedding_config c
         WHERE c.id = 'singleton'
           AND kb.rerank_score_threshold IS NOT NULL
        """
    )


def downgrade() -> None:
    op.drop_column(_TABLE, _COLUMN)
