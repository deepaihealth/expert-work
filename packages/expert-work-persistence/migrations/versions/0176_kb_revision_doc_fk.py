"""0176 — 分段维护:修订挂到文档上、停用原因、修订的孤儿时间(B-175 P3-PR2)。

P3-PR2 起分段编辑 / 新增 / 恢复写 ``knowledge_chunk_revision``(0165 建表时还没人写,保留
策略等这一步定)。定下的是:

* 修订跟着文档走 —— 文档删了,它的修订也删:先删掉 ``document_id`` 指向不存在文档的修订行
  (今天表是空的,防万一),再建外键 ``knowledge_chunk_revision.document_id →
  knowledge_document.id`` ON DELETE CASCADE。``delete_document`` / ``delete_base`` 也显式删
  修订,外键是兜底(与 0136 给分段补的外键同一做法)。
* ``knowledge_chunk_revision.orphaned_at``:重新入库后它的整块找不到了(人工修改没能套回去)
  的时刻。这样的修订还能在「未套用的修改」里看到、可以手动丢弃;满 30 天由 retention-cleanup-job
  删掉(修订正文可能含健康数据,不留到天荒地老)。给 ``retention_cleanup_worker`` 补
  SELECT / DELETE(同 0165 给 ``knowledge_task`` 的授权)。
* ``knowledge_chunk_revision.switch_off_lost``:为真的行不是正文修订,是**停用记录**
  (``revision = 0``):有人停用的整块,重新入库时找不到了 —— 记下它的原文与标题路径。之后
  每次入库都只按这些记录重新算「跟着停用」的整块(跟着停用的不再当来源,不会越停越多)。
  ``orphaned_at`` 对停用记录的意思是「从何时起新文件里没有整块含它的原文」(有就为空):
  这样的记录和找不回的修改一起列出来等人复核,丢弃 / 30 天删除同上。
* ``knowledge_chunk.disabled_reason``:停用的原因,``manual``(有人停的)或 ``inherited``
  (按停用记录跟着停用 —— 用户 10-27 拍板「宁可多停,标出来让人复核」)。启用的行为空。
  CHECK 以 NOT VALID 加上、不做 VALIDATE:校验要扫全表,而发布时整个 ``upgrade head`` 在
  一个事务里,扫描会落在 ALTER 拿着的 ACCESS EXCLUSIVE 锁里挡住检索;新列存量全是 NULL,
  本来没什么可校验,之后每次写入照样检查。

expand-only:新列可空或带默认值;旧镜像不写这张表、不读新列,外键与新列对它没有影响。

**回滚**:``downgrade`` 删外键、三列、CHECK、授权;删掉的孤儿行不恢复(它们指向的文档已经
不在)。

Revision ID: 0176_kb_revision_doc_fk
Revises: 0175_kb_k1_parent_chunks
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0176_kb_revision_doc_fk"
down_revision: str | Sequence[str] | None = "0175_kb_k1_parent_chunks"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

__all__ = ["branch_labels", "depends_on", "down_revision", "downgrade", "revision", "upgrade"]

_FK_NAME = "fk_knowledge_chunk_revision_document"
_REASON_CHECK = "knowledge_chunk_disabled_reason_enum"
_ORPHANED_IDX = "knowledge_chunk_revision_orphaned_idx"
_RETENTION_ROLE = "retention_cleanup_worker"

_DELETE_ORPHAN_REVISIONS_SQL = """
    DELETE FROM knowledge_chunk_revision r
    WHERE NOT EXISTS (SELECT 1 FROM knowledge_document d WHERE d.id = r.document_id)
"""


def upgrade() -> None:
    op.execute(_DELETE_ORPHAN_REVISIONS_SQL)
    op.create_foreign_key(
        _FK_NAME,
        "knowledge_chunk_revision",
        "knowledge_document",
        ["document_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.add_column(
        "knowledge_chunk_revision",
        sa.Column("orphaned_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        _ORPHANED_IDX,
        "knowledge_chunk_revision",
        ["orphaned_at"],
        postgresql_where=sa.text("orphaned_at IS NOT NULL"),
    )
    op.add_column(
        "knowledge_chunk_revision",
        sa.Column("switch_off_lost", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column("knowledge_chunk", sa.Column("disabled_reason", sa.Text(), nullable=True))
    # NOT VALID, and never validated: validating scans knowledge_chunk, and in
    # this migration's transaction (the release runs ``upgrade head`` as one)
    # under the ACCESS EXCLUSIVE lock the ALTERs hold, which blocks recall.
    # Nothing to validate anyway — the column is new, every existing row NULL —
    # and every write from here on is checked.
    op.execute(
        f"ALTER TABLE knowledge_chunk ADD CONSTRAINT {_REASON_CHECK} "
        "CHECK (disabled_reason IS NULL OR disabled_reason IN ('manual', 'inherited')) "
        "NOT VALID"
    )
    op.execute(f"GRANT SELECT, DELETE ON TABLE knowledge_chunk_revision TO {_RETENTION_ROLE};")


def downgrade() -> None:
    op.execute(f"REVOKE SELECT, DELETE ON TABLE knowledge_chunk_revision FROM {_RETENTION_ROLE};")
    op.drop_constraint(_REASON_CHECK, "knowledge_chunk", type_="check")
    op.drop_column("knowledge_chunk", "disabled_reason")
    op.drop_column("knowledge_chunk_revision", "switch_off_lost")
    op.drop_index(_ORPHANED_IDX, table_name="knowledge_chunk_revision")
    op.drop_column("knowledge_chunk_revision", "orphaned_at")
    op.drop_constraint(_FK_NAME, "knowledge_chunk_revision", type_="foreignkey")
