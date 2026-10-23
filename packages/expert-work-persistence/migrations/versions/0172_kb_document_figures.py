"""0172 — 知识库文档内图片(B-209 PR-2):图片计数与像素哈希。

* ``knowledge_document.figure_stats``(JSONB,可空):这一份文档里的图片抽出 / 过滤 / 截掉 /
  无法解码 / 已描述 / 失败 / 待描述各多少张(列表与详情的「已描述 N 张图」)。``None`` = 改前
  入库,或入库时没有图片描述器。
* ``knowledge_document.figure_hashes``(``text[]``,可空):这份文档里过了单文档过滤的图片
  的像素哈希。设计 §5.4 的「同一租户 ≥ 3 份文档出现(按像素哈希)」过滤读它 —— 别的文档
  里出现过几次只按同租户、本文档以外数(``tenant_id`` 已在 RLS 之内)。放在文档行上:删文档
  即删(与 0169 的识别缓存同理),不需要另一张带 RLS 的表。一份文档最多百来个哈希。
* 图片原图在对象存储(``{tenant}/knowledge/{kb}/{document}/figures/{id}.jpg``),不进库。
  识别 / 描述结果复用 0169 的 ``knowledge_parse_cache``(``figure_desc`` / ``figure_ocr`` 两种
  kind 在 0169 的 CHECK 里已预留)。

expand-only:新列可空、旧镜像不读;回滚不需要 downgrade。

Revision ID: 0172_kb_document_figures
Revises: 0171_kb_rerank_threshold_model
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ARRAY, JSONB

revision: str = "0172_kb_document_figures"
down_revision: str | Sequence[str] | None = "0171_kb_rerank_threshold_model"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

__all__ = ["branch_labels", "depends_on", "down_revision", "downgrade", "revision", "upgrade"]


def upgrade() -> None:
    op.add_column("knowledge_document", sa.Column("figure_stats", JSONB(), nullable=True))
    op.add_column("knowledge_document", sa.Column("figure_hashes", ARRAY(sa.Text()), nullable=True))


def downgrade() -> None:
    op.drop_column("knowledge_document", "figure_hashes")
    op.drop_column("knowledge_document", "figure_stats")
