"""Integration: 0136 knowledge_chunk 存量孤儿清理 + document FK CASCADE.

删除接口卫生修复第 3 批 Task 9。``knowledge_chunk.document_id``(0021)
历史上是裸 UUID 列、无 FK —— knowledge 文档删除与在途向量化任务竞态:
删除提交后,在途 ingest 把 chunk 重插到已删 ``document_id``,孤儿向量
永久残留(检索还能命中已删文档)。0136 一次性清理存量孤儿 chunk,再补
``ON DELETE CASCADE`` FK 作 DB 级兜底 —— 竞态下的在途写回被 FK 拒绝,
app 层 ingest 守卫把"文档已删"当正常终止(见 ``ingestion._run``)。

测试策略说明(同 ``test_curation_candidate_fk.py``):
``postgres_container`` 是 session 级共享容器,schema 早已在空数据上迁到
head —— 0136 的数据清理对后插入的数据不会再跑,且 head 状态下 FK 已生效、
孤儿 chunk 根本插不进去。故数据清理测试在**一个最终回滚的事务**里:先 DROP
该 FK、手工插入孤儿数据、经 alembic ``ScriptDirectory`` 加载 0136 模块直接
执行其模块级 DELETE 常量(单一事实源,迁移文本变异测试必然看得见),断言后
ROLLBACK —— 共享容器的约束与数据原样恢复。

变异自验(手工执行,未固化进 CI):把 0136 里 DELETE 的 ``NOT EXISTS``
改成 ``EXISTS`` 重跑 —— 测试变红(有主 chunk 被误删,``parented_chunk``
的保留断言失败);改回后复绿。
"""

from __future__ import annotations

from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import Connection, create_engine, text
from sqlalchemy.exc import IntegrityError
from testcontainers.postgres import PostgresContainer

from expert_work.persistence import (
    DatabaseConfig,
    SqlKnowledgeStore,
    create_async_engine_from_config,
    create_async_session_factory,
)
from expert_work.persistence.embedding import EMBEDDING_DIM
from expert_work.protocol import KnowledgeChunk

pytestmark = pytest.mark.integration

ALEMBIC_INI = Path(__file__).resolve().parent.parent / "alembic.ini"

_REVISION = "0136_knowledge_chunk_fk"
_FK_NAME = "fk_knowledge_chunk_document"

#: ``knowledge_chunk.embedding`` is ``vector(EMBEDDING_DIM)`` NOT NULL — raw
#: inserts need a full-width literal.
_ZERO_VEC = "[" + ",".join("0" for _ in range(EMBEDDING_DIM)) + "]"


def _sync_dsn(container: PostgresContainer) -> str:
    url = str(container.get_connection_url())
    return url.replace("+psycopg2", "+psycopg").replace("postgresql://", "postgresql+psycopg://", 1)


def _async_dsn(container: PostgresContainer) -> str:
    url = str(container.get_connection_url())
    return url.replace("+psycopg2", "+asyncpg").replace("postgresql://", "postgresql+asyncpg://", 1)


def _upgraded_config(container: PostgresContainer) -> Config:
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", _sync_dsn(container))
    command.upgrade(cfg, "head")
    return cfg


def _insert_document(conn: Connection, *, document_id: UUID, tenant_id: UUID, kb_id: UUID) -> None:
    conn.execute(
        text(
            "INSERT INTO knowledge_document "
            "(id, tenant_id, kb_id, filename, status, created_at, updated_at) "
            "VALUES (:id, :tid, :kb, 'probe.md', 'ready', now(), now())"
        ),
        {"id": document_id, "tid": tenant_id, "kb": kb_id},
    )


def _insert_chunk(
    conn: Connection,
    *,
    chunk_id: UUID,
    tenant_id: UUID,
    kb_id: UUID,
    document_id: UUID,
) -> None:
    conn.execute(
        text(
            "INSERT INTO knowledge_chunk "
            "(id, tenant_id, kb_id, document_id, chunk_index, content, embedding, created_at) "
            "VALUES (:id, :tid, :kb, :doc, 0, 'chunk text', CAST(:emb AS vector), now())"
        ),
        {
            "id": chunk_id,
            "tid": tenant_id,
            "kb": kb_id,
            "doc": document_id,
            "emb": _ZERO_VEC,
        },
    )


def test_orphan_chunks_removed_parented_kept(postgres_container: PostgresContainer) -> None:
    """存量孤儿 chunk 被删;有主 chunk 保留."""
    cfg = _upgraded_config(postgres_container)

    # RED gate: raises if migration 0136 does not exist yet.
    migration = ScriptDirectory.from_config(cfg).get_revision(_REVISION).module

    tenant_id = uuid4()
    kb_id = uuid4()
    document_id = uuid4()
    parented_chunk = uuid4()
    orphan_chunk = uuid4()

    engine = create_engine(_sync_dsn(postgres_container))
    try:
        # Single transaction, rolled back at the end — the shared session
        # container gets its FK (and pristine data) back no matter what.
        with engine.connect() as conn:
            conn.execute(text(f"ALTER TABLE knowledge_chunk DROP CONSTRAINT {_FK_NAME}"))
            _insert_document(conn, document_id=document_id, tenant_id=tenant_id, kb_id=kb_id)
            # ① Parented chunk — its document exists; must survive.
            _insert_chunk(
                conn,
                chunk_id=parented_chunk,
                tenant_id=tenant_id,
                kb_id=kb_id,
                document_id=document_id,
            )
            # ② Orphan chunk — points at a document that never existed.
            _insert_chunk(
                conn,
                chunk_id=orphan_chunk,
                tenant_id=tenant_id,
                kb_id=kb_id,
                document_id=uuid4(),
            )

            # Execute the migration's DELETE body — single source of truth.
            conn.execute(text(migration._DELETE_ORPHAN_CHUNK_SQL))

            remaining = {
                row[0]
                for row in conn.execute(
                    text("SELECT id FROM knowledge_chunk WHERE id = ANY(:ids)"),
                    {"ids": [parented_chunk, orphan_chunk]},
                )
            }
            conn.rollback()
    finally:
        engine.dispose()

    assert orphan_chunk not in remaining, "orphan knowledge_chunk survived cleanup"
    assert parented_chunk in remaining, "parented knowledge_chunk was wrongly deleted"


def test_fk_cascades_direct_sql_delete_of_document(
    postgres_container: PostgresContainer,
) -> None:
    """FK 生效:绕过 app 层直接 SQL 删 document 行 → chunk 随删."""
    _upgraded_config(postgres_container)

    tenant_id = uuid4()
    kb_id = uuid4()
    document_id = uuid4()
    chunk_id = uuid4()

    engine = create_engine(_sync_dsn(postgres_container))
    try:
        with engine.connect() as conn:
            _insert_document(conn, document_id=document_id, tenant_id=tenant_id, kb_id=kb_id)
            _insert_chunk(
                conn,
                chunk_id=chunk_id,
                tenant_id=tenant_id,
                kb_id=kb_id,
                document_id=document_id,
            )
            conn.execute(text("DELETE FROM knowledge_document WHERE id = :id"), {"id": document_id})
            survivors = conn.execute(
                text("SELECT id FROM knowledge_chunk WHERE id = :id"), {"id": chunk_id}
            ).all()
            conn.rollback()
    finally:
        engine.dispose()

    assert survivors == [], "knowledge_chunk did not cascade with its document"


@pytest.mark.asyncio
async def test_replace_chunks_after_delete_raises_and_leaves_no_orphans(
    postgres_container: PostgresContainer,
) -> None:
    """删除竞态:已删文档的 ``replace_chunks`` 写回被 FK 拒绝,无孤儿行.

    与 ``test_in_memory_knowledge_store.py`` 的
    ``test_replace_chunks_after_document_delete_raises_and_leaves_no_orphans``
    同型 —— in-memory 侧抛 ``KeyError`` 镜像本侧的 FK ``IntegrityError``。
    """
    _upgraded_config(postgres_container)

    engine = create_async_engine_from_config(DatabaseConfig(dsn=_async_dsn(postgres_container)))
    store = SqlKnowledgeStore(create_async_session_factory(engine))
    try:
        tenant_id = uuid4()
        base = await store.create_base(tenant_id=tenant_id, name=f"kb-{uuid4().hex[:8]}")
        doc = await store.upsert_document(tenant_id=tenant_id, kb_id=base.id, filename="d.md")
        assert await store.delete_document(tenant_id=tenant_id, document_id=doc.id) is True

        # The in-flight ingest write-back races the committed delete — the
        # FK must reject it instead of recreating orphan vectors.
        with pytest.raises(IntegrityError):
            await store.replace_chunks(
                tenant_id=tenant_id,
                document_id=doc.id,
                chunks=[
                    KnowledgeChunk(
                        id=uuid4(),
                        tenant_id=tenant_id,
                        kb_id=base.id,
                        document_id=doc.id,
                        chunk_index=0,
                        content="stale write-back",
                        embedding=(0.0,) * EMBEDDING_DIM,
                    )
                ],
            )
        _, total = await store.list_chunks(tenant_id=tenant_id, document_id=doc.id)
        assert total == 0, "orphan chunk row was recreated after document delete"

        # Shared session-scoped container — remove this test's base.
        assert await store.delete_base(tenant_id=tenant_id, kb_id=base.id) is True
    finally:
        await engine.dispose()
