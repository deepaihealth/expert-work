"""0175 — 父段行:``knowledge_chunk`` 存不嵌入的父段(B-206 K-1)。

父段(``chunk_type='parent'``)是相邻子段合成的一段原文,检索命中子段后交给模型的是它;
它只存来读,不嵌入、不进关键词索引、不参与召回(召回 SQL 按可检索类型白名单过滤)。

* ``knowledge_chunk.embedding`` 去掉 NOT NULL(0021 起非空)。HNSW 索引(0021)不收
  向量为 NULL 的行,父段不进向量图。
* CHECK ``knowledge_chunk_embedding_iff_parent``:没有向量 ⇔ 是父段。别的类型漏了向量、
  父段带了向量,都在写入时报错,不会静默进召回。
* 唯一键 ``(document_id, chunk_index)`` 换成 ``(document_id, chunk_type, chunk_index)``:
  父段在自己的编号空间(0..m-1),与子段同号。新键先建、旧键后删,中间不会没有唯一键。

expand-only:旧镜像写的行都带向量、都不是父段,照样满足新约束。

**回滚**:``downgrade`` 先清掉指向父段的 ``parent_chunk_id``、删父段,再换回旧唯一键、恢复
NOT NULL。只换回旧镜像、不跑 downgrade 时,只要新代码写过父段就先手工执行同样两条语句
(``_DROP_PARENTS_SQL``):旧镜像不认父段,小表上的向量检索走顺序扫描,会把没有向量的父段
排在最后读出来,读向量时报错。

Revision ID: 0175_kb_k1_parent_chunks
Revises: 0174_kb_k3_chunking
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0175_kb_k1_parent_chunks"
down_revision: str | Sequence[str] | None = "0174_kb_k3_chunking"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

__all__ = ["branch_labels", "depends_on", "down_revision", "downgrade", "revision", "upgrade"]

_TABLE = "knowledge_chunk"
_OLD_UNIQUE = "knowledge_chunk_document_index_uniq"
_NEW_UNIQUE = "knowledge_chunk_document_type_index_uniq"
_CHECK = "knowledge_chunk_embedding_iff_parent"

#: Children first lose their pointer to a parent row, then the parent rows go.
_DROP_PARENTS_SQL = (
    "UPDATE knowledge_chunk SET parent_chunk_id = NULL WHERE parent_chunk_id IN "
    "(SELECT id FROM knowledge_chunk WHERE chunk_type = 'parent')",
    "DELETE FROM knowledge_chunk WHERE chunk_type = 'parent'",
)


def upgrade() -> None:
    op.alter_column(_TABLE, "embedding", nullable=True)
    op.create_check_constraint(_CHECK, _TABLE, "(chunk_type = 'parent') = (embedding IS NULL)")
    op.create_unique_constraint(_NEW_UNIQUE, _TABLE, ["document_id", "chunk_type", "chunk_index"])
    op.drop_constraint(_OLD_UNIQUE, _TABLE, type_="unique")


def downgrade() -> None:
    for statement in _DROP_PARENTS_SQL:
        op.execute(statement)
    op.create_unique_constraint(_OLD_UNIQUE, _TABLE, ["document_id", "chunk_index"])
    op.drop_constraint(_NEW_UNIQUE, _TABLE, type_="unique")
    op.drop_constraint(_CHECK, _TABLE, type_="check")
    op.alter_column(_TABLE, "embedding", nullable=False)
