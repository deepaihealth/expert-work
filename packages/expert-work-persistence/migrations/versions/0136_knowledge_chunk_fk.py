"""knowledge_chunk 存量孤儿清理 + document FK CASCADE —— 删除竞态 DB 级兜底.

Revision ID: 0136_knowledge_chunk_fk
Revises: 0135_curation_candidate_fk
Create Date: 2026-07-24

删除接口卫生修复第 3 批 Task 9。``knowledge_chunk.document_id``(0021)
历史上是裸 UUID 列、无 FK —— knowledge 文档删除与在途向量化任务竞态:
删除提交后,在途 ingest 把 chunk 重插到已删 ``document_id``,孤儿向量
永久残留(检索还能命中已删文档)。本迁移做两件事:

1. 存量孤儿 chunk(指向不存在的 document)一次性 DELETE;
2. 补 ``ON DELETE CASCADE`` FK 作 DB 级兜底 —— 未来的竞态写回被 FK 拒绝,
   app 层 ingest 守卫把"文档已删"当正常终止(``ingestion._run``),
   ``delete_document`` 的显式 chunk DELETE 保留,FK 只是兜底。

SQL 提为模块级常量:``tests/test_knowledge_chunk_fk.py`` 经 alembic
``ScriptDirectory`` 加载本模块直接执行同一份文本 —— 单一事实源,不存在
测试副本漂移。
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0136_knowledge_chunk_fk"
down_revision: str | Sequence[str] | None = "0135_curation_candidate_fk"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

__all__ = ["branch_labels", "depends_on", "down_revision", "downgrade", "revision", "upgrade"]

_FK_NAME = "fk_knowledge_chunk_document"

_DELETE_ORPHAN_CHUNK_SQL = """
    DELETE FROM knowledge_chunk kc
    WHERE NOT EXISTS (SELECT 1 FROM knowledge_document kd WHERE kd.id = kc.document_id)
"""


def upgrade() -> None:
    op.execute(_DELETE_ORPHAN_CHUNK_SQL)
    op.create_foreign_key(
        _FK_NAME,
        "knowledge_chunk",
        "knowledge_document",
        ["document_id"],
        ["id"],
        ondelete="CASCADE",
    )


def downgrade() -> None:
    op.drop_constraint(_FK_NAME, "knowledge_chunk", type_="foreignkey")
    # The data fix is irreversible by design (same posture as 0134/0135):
    # the deleted chunks pointed at documents that no longer exist —
    # there is no orphan state worth restoring.
