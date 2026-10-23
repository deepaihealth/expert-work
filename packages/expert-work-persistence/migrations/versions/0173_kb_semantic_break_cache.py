"""0173 — 识别缓存多存一种:切块的主题断点判断(B-215)。

``knowledge_parse_cache.kind`` 的 CHECK 加上 ``semantic_break``:语义切块对相邻两段「要不要在
这里断开」的判断,每份文档每个嵌入模型一行(键是两段文字 + 嵌入模型 + 阈值的哈希,不存原文)。
重新摄取 / 补识别时文字没变的那几对直接复用,不再把每一段重新嵌入一遍(10-23 测试:一份 250 万字
的 PDF 补识别时一页没识别,嵌入照样又记了 68 万 token)。与识别结果一样按文档存,删文档即删。

expand-only:只放宽 CHECK,旧镜像不写也不读这种行。downgrade 先删掉这种行再收紧 CHECK。

Revision ID: 0173_kb_semantic_break_cache
Revises: 0172_kb_document_figures
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0173_kb_semantic_break_cache"
down_revision: str | Sequence[str] | None = "0172_kb_document_figures"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

__all__ = ["branch_labels", "depends_on", "down_revision", "downgrade", "revision", "upgrade"]

_TABLE = "knowledge_parse_cache"
_CHECK = "knowledge_parse_cache_kind_enum"


def upgrade() -> None:
    op.drop_constraint(_CHECK, _TABLE, type_="check")
    op.create_check_constraint(
        _CHECK, _TABLE, "kind IN ('page_ocr', 'figure_desc', 'figure_ocr', 'semantic_break')"
    )


def downgrade() -> None:
    op.execute(f"DELETE FROM {_TABLE} WHERE kind = 'semantic_break'")  # noqa: S608 — constants
    op.drop_constraint(_CHECK, _TABLE, type_="check")
    op.create_check_constraint(_CHECK, _TABLE, "kind IN ('page_ocr', 'figure_desc', 'figure_ocr')")
