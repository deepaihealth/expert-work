"""迁移 0175 —— B-206 K-1:父段行(``chunk_type='parent'``,不嵌入)。

升级后:只有父段可以没有向量、父段不能有向量(CHECK);``chunk_index`` 按
``(document_id, chunk_type)`` 唯一,父段与子段可以同号;向量索引(HNSW)不收没有向量的行。
降级:删父段、清掉指向父段的 ``parent_chunk_id``,旧唯一键与 NOT NULL 回来,再升回来。
用容器里另建的库(共享库会被别的测试升 / 降级)。
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from uuid import UUID, uuid4

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from psycopg import errors
from testcontainers.postgres import PostgresContainer

from expert_work.persistence.embedding import EMBEDDING_DIM

pytestmark = pytest.mark.integration

ALEMBIC_INI = Path(__file__).resolve().parent.parent / "alembic.ini"
_DB = "migration_0175_test"
_BEFORE = "0174_kb_k3_chunking"
_OLD_UNIQUE = "knowledge_chunk_document_index_uniq"
_NEW_UNIQUE = "knowledge_chunk_document_type_index_uniq"
_CHECK = "knowledge_chunk_embedding_iff_parent"


def _vector(*head: float) -> str:
    values = list(head) + [0.0] * (EMBEDDING_DIM - len(head))
    return "[" + ",".join(str(value) for value in values) + "]"


def _sync_dsn(container: PostgresContainer, database: str | None = None) -> str:
    url = str(container.get_connection_url()).replace("+psycopg2", "")
    if database is None:
        return url
    base, _, _ = url.rpartition("/")
    return f"{base}/{database}"


def _alembic(dsn: str) -> Config:
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", dsn.replace("postgresql://", "postgresql+psycopg://", 1))
    return cfg


@pytest.fixture
def fresh_db(postgres_container: PostgresContainer) -> Iterator[str]:
    admin = _sync_dsn(postgres_container)
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{_DB}"')
        conn.execute(f'CREATE DATABASE "{_DB}"')
    dsn = _sync_dsn(postgres_container, _DB)
    command.upgrade(_alembic(dsn), _BEFORE)
    try:
        yield dsn
    finally:
        with psycopg.connect(admin, autocommit=True) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{_DB}" WITH (FORCE)')


class _Doc:
    """One base + one document; inserts chunk rows by hand."""

    def __init__(self, dsn: str) -> None:
        self.dsn = dsn
        self.tenant, self.kb, self.id = uuid4(), uuid4(), uuid4()
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(
                "INSERT INTO knowledge_base (id, tenant_id, name) VALUES (%s, %s, 'kb')",
                (self.kb, self.tenant),
            )
            conn.execute(
                "INSERT INTO knowledge_document (id, tenant_id, kb_id, filename, status) "
                "VALUES (%s, %s, %s, 'a.md', 'ready')",
                (self.id, self.tenant, self.kb),
            )

    def chunk(
        self,
        index: int,
        *,
        chunk_type: str = "text",
        embedding: str | None = None,
        parent: UUID | None = None,
    ) -> UUID:
        chunk_id = uuid4()
        with psycopg.connect(self.dsn, autocommit=True) as conn:
            conn.execute(
                """
                INSERT INTO knowledge_chunk (id, tenant_id, kb_id, document_id, chunk_index,
                                             content, embedding, chunk_type, parent_chunk_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s::vector, %s, %s)
                """,
                (
                    chunk_id,
                    self.tenant,
                    self.kb,
                    self.id,
                    index,
                    f"{chunk_type} {index}",
                    embedding,
                    chunk_type,
                    parent,
                ),
            )
        return chunk_id

    def rows(self) -> list[tuple[str, int, UUID | None]]:
        with psycopg.connect(self.dsn, autocommit=True) as conn:
            return [
                (row[0], row[1], row[2])
                for row in conn.execute(
                    "SELECT chunk_type, chunk_index, parent_chunk_id FROM knowledge_chunk "
                    "WHERE document_id = %s ORDER BY chunk_type, chunk_index",
                    (self.id,),
                )
            ]


def _constraints(dsn: str) -> set[str]:
    with psycopg.connect(dsn, autocommit=True) as conn:
        return {
            row[0]
            for row in conn.execute(
                "SELECT conname FROM pg_constraint WHERE conrelid = 'knowledge_chunk'::regclass"
            )
        }


def test_only_a_parent_row_goes_without_an_embedding(fresh_db: str) -> None:
    doc = _Doc(fresh_db)
    doc.chunk(0, embedding=_vector(1.0))
    with pytest.raises(errors.NotNullViolation):
        doc.chunk(0, chunk_type="parent")  # before: every row needs a vector

    command.upgrade(_alembic(fresh_db), "head")
    assert doc.rows() == [("text", 0, None)]  # existing rows pass the new CHECK
    parent = doc.chunk(0, chunk_type="parent")
    doc.chunk(1, embedding=_vector(0.5), parent=parent)
    with pytest.raises(errors.CheckViolation):
        doc.chunk(2)  # a text row with no vector
    with pytest.raises(errors.CheckViolation):
        doc.chunk(1, chunk_type="parent", embedding=_vector(1.0))  # a parent with one
    assert doc.rows() == [("parent", 0, None), ("text", 0, None), ("text", 1, parent)]


def test_chunk_index_is_unique_per_document_and_type(fresh_db: str) -> None:
    command.upgrade(_alembic(fresh_db), "head")
    assert _NEW_UNIQUE in _constraints(fresh_db)
    assert _OLD_UNIQUE not in _constraints(fresh_db)
    doc = _Doc(fresh_db)
    doc.chunk(0, embedding=_vector(1.0))
    doc.chunk(0, chunk_type="parent")  # a parent and a child share index 0
    with pytest.raises(errors.UniqueViolation):
        doc.chunk(0, embedding=_vector(0.5))
    with pytest.raises(errors.UniqueViolation):
        doc.chunk(0, chunk_type="parent")
    _Doc(fresh_db).chunk(0, embedding=_vector(1.0))  # another document: its own numbering


def test_the_vector_index_holds_no_parent_rows(fresh_db: str) -> None:
    """pgvector's HNSW index does not index NULL vectors: an index scan never
    returns a parent row, whatever ``LIMIT``. A sequential scan does (its
    NULL distance sorts last) — why recall still filters by chunk type."""
    command.upgrade(_alembic(fresh_db), "head")
    doc = _Doc(fresh_db)
    for index in range(3):
        doc.chunk(index, embedding=_vector(1.0, 0.1 * index))
    for index in range(2):
        doc.chunk(index, chunk_type="parent")
    query = "SELECT chunk_type FROM knowledge_chunk ORDER BY embedding <=> %s::vector LIMIT 10"
    nearest = (_vector(1.0),)
    with psycopg.connect(fresh_db) as conn:
        for setting in ("enable_seqscan", "enable_bitmapscan", "enable_sort"):
            conn.execute(f"SET LOCAL {setting} = off")
        plan = "\n".join(str(row[0]) for row in conn.execute("EXPLAIN " + query, nearest))
        assert "knowledge_chunk_embedding_idx" in plan
        assert [row[0] for row in conn.execute(query, nearest)] == ["text"] * 3
    with psycopg.connect(fresh_db) as conn:
        conn.execute("SET LOCAL enable_indexscan = off")
        scanned = [row[0] for row in conn.execute(query, nearest)]
    assert scanned == ["text"] * 3 + ["parent"] * 2


def test_the_downgrade_drops_parents_and_restores_the_old_shape(fresh_db: str) -> None:
    command.upgrade(_alembic(fresh_db), "head")
    doc = _Doc(fresh_db)
    parent = doc.chunk(0, chunk_type="parent")
    doc.chunk(0, embedding=_vector(1.0), parent=parent)
    doc.chunk(1, embedding=_vector(0.5), parent=parent)
    unrelated = uuid4()  # a pointer to something that is not a parent row: kept
    doc.chunk(2, embedding=_vector(0.2), parent=unrelated)

    command.downgrade(_alembic(fresh_db), _BEFORE)
    assert doc.rows() == [("text", 0, None), ("text", 1, None), ("text", 2, unrelated)]
    constraints = _constraints(fresh_db)
    assert _OLD_UNIQUE in constraints
    assert not {_NEW_UNIQUE, _CHECK} & constraints
    with pytest.raises(errors.NotNullViolation):
        doc.chunk(3)
    with pytest.raises(errors.UniqueViolation):
        doc.chunk(0, chunk_type="question", embedding=_vector(1.0))  # old key ignores the type

    command.upgrade(_alembic(fresh_db), "head")
    assert {_NEW_UNIQUE, _CHECK} <= _constraints(fresh_db)
