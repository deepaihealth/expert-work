"""0165 — 知识库数据模型 v2:文档 / 分段 / 库的新列 + 任务 / 修订 / 标签表(B-175 L2-PR3)。

一次把 P3 要的列与表铺好(设计 §6.1、波 1 计划 L2-PR3),之后的 PR 只写代码不再动表。

**增列**(全部可空或带默认值,旧镜像不读;回滚不需要 downgrade):

* ``knowledge_base``:``kind``(document / faq / material)、``metadata_schema``、
  ``api_access_enabled``。设计里的 ``retrieval_config jsonb`` 这次**不加**:库上已经有
  ``retrieval_top_k`` 等四列,接口也已用 ``retrieval_config`` 这个名字拼它们,再加一个同名
  jsonb 会有两个来源;等 P2 定下权重 / 校准阈值的形状再加。
* ``knowledge_document``:``doc_seq``(租户内单调,短编号 ``D{doc_seq}``,重传保留)、
  ``title``、``source_type`` / ``source_uri`` / ``external_id``、``metadata``、``enabled``、
  ``stage`` / ``progress`` / ``error_code``、``page_count``、``size_bytes``、``mime``。
  唯一键 ``(tenant_id, doc_seq)``;``(tenant_id, kb_id, external_id) WHERE external_id
  IS NOT NULL``。``doc_seq`` 保持可空:回滚到旧镜像时它插入文档不带这一列,NOT NULL 会
  让旧镜像连上传都失败。
* ``knowledge_chunk``:``chunk_type``、``parent_chunk_id``、``heading_path``、
  ``source_locator``、``enabled`` / ``edited`` / ``revision``、``content_hash``、
  ``embedding_model``(产出这条向量的「供应商/模型」;重传时只复用同一模型的旧向量,
  平台换了嵌入模型不会把两个模型的向量混进同一份文档);补唯一键
  ``(document_id, chunk_index)``。

**存量**:生产 0 个库;测试环境有少量文档。

* ``doc_seq`` 按租户、``created_at, id`` 顺序回填,计数表 ``knowledge_doc_seq`` 按每个
  租户的最大号播种 —— 新文档接着往下编,不撞号。
* 唯一键 ``(document_id, chunk_index)`` 建之前先删重复段:旧的无围栏写回在并发时可能
  把同一份文档的两套分段都留下。一次 ``replace_chunks`` 的整套分段 ``created_at``
  相同(同一事务),所以对有重复的文档只留 ``created_at`` 最新的那一整套 —— 旧那套比
  新那套长时多出来的段也一起删掉;万一两套 ``created_at`` 相同,再按位置留 ``id`` 大的。
* 旧分段的 ``[Section: …]`` 前缀仍在 ``content`` 里、``heading_path`` 为空 ——
  ``chunk_retrieval_text`` 对这种行原样返回,检索行为不变;重新入库后变成新形状。

**回滚**:旧镜像把 ``content`` 当「带前缀的整段」读。``downgrade`` 先把本版写入的分段
改回旧形状(``[Section: a > b]`` 前缀写回 ``content``、清空 ``heading_path``)再删列;
只换回旧镜像、不跑 downgrade 时,先手工执行同一条 UPDATE(``_LEGACY_SHAPE_SQL``),
否则旧镜像给智能体的结果丢标题、旧版重建索引会把标题从向量与关键词里永久去掉。
新代码同样认这种旧形状,再升回来不用处理。

**新表**:``knowledge_doc_seq``(计数)、``knowledge_task``、``knowledge_chunk_revision``、
``knowledge_tag``、``knowledge_document_tag``。都带 ``tenant_id`` + 与 0161 同形的
``app.tenant_id`` 策略(ENABLE + FORCE,``test_rls_policy_coverage``)。标签两张表用外键
级联(删库删标签、删文档 / 标签删关联)。``knowledge_task`` 给 ``retention_cleanup_worker``
补 SELECT / DELETE(收尾满保留期的任务行由 retention-cleanup-job 删,同 0162 的教训)。
修订表的保留策略等 P3-PR2 定下修订怎么写再加。

Revision ID: 0165_knowledge_model_v2
Revises: 0164_knowledge_ingest_generation
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "0165_knowledge_model_v2"
down_revision: str | Sequence[str] | None = "0164_knowledge_ingest_generation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

__all__ = ["branch_labels", "depends_on", "down_revision", "downgrade", "revision", "upgrade"]

_RETENTION_ROLE = "retention_cleanup_worker"

#: Put chunks written by this version back into the pre-0165 shape — the
#: breadcrumb prefixed into ``content`` (``chunk_retrieval_text``'s format),
#: ``heading_path`` cleared. Run by ``downgrade`` and, before a rollback that
#: only swaps the image back, by hand.
_LEGACY_SHAPE_SQL = """
WITH paths AS MATERIALIZED (
    -- Only real arrays: a JSON ``null`` scalar (written before the ORM column
    -- had ``none_as_null``) makes the array functions raise. MATERIALIZED so
    -- the filter runs before jsonb_array_elements_text sees any row.
    SELECT id, heading_path FROM knowledge_chunk WHERE jsonb_typeof(heading_path) = 'array'
), crumbs AS (
    SELECT p.id, string_agg(part.value, ' > ' ORDER BY part.ordinality) AS joined
      FROM paths p
     CROSS JOIN LATERAL jsonb_array_elements_text(p.heading_path)
           WITH ORDINALITY AS part(value, ordinality)
     GROUP BY p.id
)
UPDATE knowledge_chunk c
   SET content = '[Section: ' || crumbs.joined || E']\n\n' || c.content,
       heading_path = NULL
  FROM crumbs
 WHERE c.id = crumbs.id
"""
_NEW_TABLES = (
    "knowledge_doc_seq",
    "knowledge_task",
    "knowledge_chunk_revision",
    "knowledge_tag",
    "knowledge_document_tag",
)


def _uuid_pk() -> sa.Column[Any]:
    return sa.Column(
        "id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")
    )


def _created_at() -> sa.Column[Any]:
    return sa.Column(
        "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
    )


def _tenant_policy(table: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY;")
    op.execute(
        f"""
        CREATE POLICY {table}_tenant_isolation ON {table}
            USING      (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
            WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);
        """
    )


def _upgrade_base() -> None:
    op.add_column(
        "knowledge_base",
        sa.Column("kind", sa.Text(), nullable=False, server_default=sa.text("'document'")),
    )
    op.add_column("knowledge_base", sa.Column("metadata_schema", JSONB(), nullable=True))
    op.add_column(
        "knowledge_base",
        sa.Column(
            "api_access_enabled", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
    )


def _upgrade_document() -> None:
    columns: list[sa.Column[Any]] = [
        sa.Column("doc_seq", sa.BigInteger(), nullable=True),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("source_type", sa.Text(), nullable=False, server_default=sa.text("'file'")),
        sa.Column("source_uri", sa.Text(), nullable=True),
        sa.Column("external_id", sa.Text(), nullable=True),
        sa.Column("metadata", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("stage", sa.Text(), nullable=True),
        sa.Column("progress", sa.SmallInteger(), nullable=True),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column("page_count", sa.Integer(), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("mime", sa.Text(), nullable=True),
    ]
    for column in columns:
        op.add_column("knowledge_document", column)

    op.create_table(
        "knowledge_doc_seq",
        sa.Column("tenant_id", UUID(as_uuid=True), primary_key=True),
        sa.Column("last_seq", sa.BigInteger(), nullable=False),
    )
    _tenant_policy("knowledge_doc_seq")
    # Backfill: number existing documents per tenant in upload order, then
    # seed each tenant's counter with its highest number.
    op.execute(
        """
        UPDATE knowledge_document d
           SET doc_seq = numbered.seq
          FROM (
                SELECT id,
                       row_number() OVER (PARTITION BY tenant_id ORDER BY created_at, id) AS seq
                  FROM knowledge_document
               ) AS numbered
         WHERE d.id = numbered.id
        """
    )
    op.execute(
        """
        INSERT INTO knowledge_doc_seq (tenant_id, last_seq)
        SELECT tenant_id, max(doc_seq) FROM knowledge_document GROUP BY tenant_id
        """
    )
    op.create_unique_constraint(
        "knowledge_document_doc_seq_uniq", "knowledge_document", ["tenant_id", "doc_seq"]
    )
    op.create_index(
        "knowledge_document_external_id_uniq",
        "knowledge_document",
        ["tenant_id", "kb_id", "external_id"],
        unique=True,
        postgresql_where=sa.text("external_id IS NOT NULL"),
    )


def _upgrade_chunk() -> None:
    columns: list[sa.Column[Any]] = [
        sa.Column("chunk_type", sa.Text(), nullable=False, server_default=sa.text("'text'")),
        sa.Column("parent_chunk_id", UUID(as_uuid=True), nullable=True),
        sa.Column("heading_path", JSONB(), nullable=True),
        sa.Column("source_locator", JSONB(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("edited", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("revision", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("content_hash", sa.Text(), nullable=True),
        sa.Column("embedding_model", sa.Text(), nullable=True),
    ]
    for column in columns:
        op.add_column("knowledge_chunk", column)
    # Duplicate (document_id, chunk_index) rows can only come from the old
    # unfenced concurrent write-backs. For each such document keep only its
    # newest set (one replace_chunks = one transaction = one created_at) …
    op.execute(
        """
        DELETE FROM knowledge_chunk c
         USING (
                SELECT document_id, max(created_at) AS newest
                  FROM knowledge_chunk
                 WHERE document_id IN (
                        SELECT document_id FROM knowledge_chunk
                         GROUP BY document_id, chunk_index HAVING count(*) > 1
                       )
                 GROUP BY document_id
               ) AS doubled
         WHERE c.document_id = doubled.document_id
           AND c.created_at < doubled.newest
        """
    )
    # … and should two sets share a created_at, one row per position.
    op.execute(
        """
        DELETE FROM knowledge_chunk c
         USING knowledge_chunk twin
         WHERE c.document_id = twin.document_id
           AND c.chunk_index = twin.chunk_index
           AND c.created_at = twin.created_at
           AND c.id < twin.id
        """
    )
    op.create_unique_constraint(
        "knowledge_chunk_document_index_uniq", "knowledge_chunk", ["document_id", "chunk_index"]
    )


def _create_task_table() -> None:
    op.create_table(
        "knowledge_task",
        _uuid_pk(),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("kb_id", UUID(as_uuid=True), nullable=True),
        sa.Column("task_type", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'queued'")),
        sa.Column("progress", sa.SmallInteger(), nullable=True),
        sa.Column("params", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("stats", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column("error_detail", sa.Text(), nullable=True),
        sa.Column("created_by", sa.Text(), nullable=True),
        _created_at(),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')",
            name="knowledge_task_status_enum",
        ),
    )
    op.create_index("knowledge_task_kb_idx", "knowledge_task", ["tenant_id", "kb_id", "created_at"])
    op.create_index(
        "knowledge_task_active_idx",
        "knowledge_task",
        ["created_at"],
        postgresql_where=sa.text("status IN ('queued', 'running')"),
    )
    _tenant_policy("knowledge_task")
    op.execute(f"GRANT SELECT, DELETE ON TABLE knowledge_task TO {_RETENTION_ROLE};")


def _create_revision_table() -> None:
    op.create_table(
        "knowledge_chunk_revision",
        _uuid_pk(),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("kb_id", UUID(as_uuid=True), nullable=False),
        sa.Column("document_id", UUID(as_uuid=True), nullable=False),
        sa.Column("chunk_id", UUID(as_uuid=True), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("heading_path", JSONB(), nullable=True),
        sa.Column("edited_by", sa.Text(), nullable=True),
        _created_at(),
        sa.UniqueConstraint("chunk_id", "revision", name="knowledge_chunk_revision_uniq"),
    )
    op.create_index(
        "knowledge_chunk_revision_document_idx", "knowledge_chunk_revision", ["document_id"]
    )
    _tenant_policy("knowledge_chunk_revision")


def _create_tag_tables() -> None:
    op.create_table(
        "knowledge_tag",
        _uuid_pk(),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column(
            "kb_id",
            UUID(as_uuid=True),
            sa.ForeignKey("knowledge_base.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("color", sa.Text(), nullable=True),
        _created_at(),
        sa.UniqueConstraint("tenant_id", "kb_id", "name", name="knowledge_tag_name_uniq"),
    )
    _tenant_policy("knowledge_tag")
    op.create_table(
        "knowledge_document_tag",
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column(
            "document_id",
            UUID(as_uuid=True),
            sa.ForeignKey("knowledge_document.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "tag_id",
            UUID(as_uuid=True),
            sa.ForeignKey("knowledge_tag.id", ondelete="CASCADE"),
            nullable=False,
        ),
        _created_at(),
        sa.PrimaryKeyConstraint("document_id", "tag_id", name="knowledge_document_tag_pkey"),
    )
    op.create_index("knowledge_document_tag_tag_idx", "knowledge_document_tag", ["tag_id"])
    _tenant_policy("knowledge_document_tag")


def upgrade() -> None:
    _upgrade_base()
    _upgrade_document()
    _upgrade_chunk()
    _create_task_table()
    _create_revision_table()
    _create_tag_tables()


def downgrade() -> None:
    op.execute(f"REVOKE SELECT, DELETE ON TABLE knowledge_task FROM {_RETENTION_ROLE};")
    for table in reversed(_NEW_TABLES):
        op.execute(f"DROP POLICY IF EXISTS {table}_tenant_isolation ON {table};")
        op.drop_table(table)
    op.drop_constraint("knowledge_chunk_document_index_uniq", "knowledge_chunk", type_="unique")
    op.execute(_LEGACY_SHAPE_SQL)
    for column in (
        "embedding_model",
        "content_hash",
        "revision",
        "edited",
        "enabled",
        "source_locator",
        "heading_path",
        "parent_chunk_id",
        "chunk_type",
    ):
        op.drop_column("knowledge_chunk", column)
    op.drop_index("knowledge_document_external_id_uniq", table_name="knowledge_document")
    op.drop_constraint("knowledge_document_doc_seq_uniq", "knowledge_document", type_="unique")
    for column in (
        "mime",
        "size_bytes",
        "page_count",
        "error_code",
        "progress",
        "stage",
        "enabled",
        "metadata",
        "external_id",
        "source_uri",
        "source_type",
        "title",
        "doc_seq",
    ):
        op.drop_column("knowledge_document", column)
    for column in ("api_access_enabled", "metadata_schema", "kind"):
        op.drop_column("knowledge_base", column)
