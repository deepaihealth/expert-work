"""迁移 0176 —— B-175 P3-PR2:分段修订表挂到文档上(删文档连带删修订)。

升级:先删掉指向不存在文档的修订行,再建 ``knowledge_chunk_revision.document_id →
knowledge_document.id`` 外键(ON DELETE CASCADE)。删文档 → 它的修订行跟着没;
指向不存在文档的修订行写不进去。降级:外键没了,再升回来。
用容器里另建的库(共享库会被别的测试升 / 降级)。
"""

from __future__ import annotations

import io
from collections.abc import Iterator
from pathlib import Path
from uuid import UUID, uuid4

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from psycopg import errors
from testcontainers.postgres import PostgresContainer

pytestmark = pytest.mark.integration

ALEMBIC_INI = Path(__file__).resolve().parent.parent / "alembic.ini"
_DB = "migration_0176_test"
_BEFORE = "0175_kb_k1_parent_chunks"
_AFTER = "0176_kb_revision_doc_fk"
_FK = "fk_knowledge_chunk_revision_document"


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
    """One base + one document; writes revision rows by hand."""

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

    def revision(self, document_id: UUID | None = None) -> UUID:
        chunk_id = uuid4()
        with psycopg.connect(self.dsn, autocommit=True) as conn:
            conn.execute(
                "INSERT INTO knowledge_chunk_revision "
                "(tenant_id, kb_id, document_id, chunk_id, revision, content) "
                "VALUES (%s, %s, %s, %s, 1, 'text')",
                (self.tenant, self.kb, document_id or self.id, chunk_id),
            )
        return chunk_id

    def revisions(self) -> set[UUID]:
        with psycopg.connect(self.dsn, autocommit=True) as conn:
            return {row[0] for row in conn.execute("SELECT chunk_id FROM knowledge_chunk_revision")}

    def delete(self) -> None:
        with psycopg.connect(self.dsn, autocommit=True) as conn:
            conn.execute("DELETE FROM knowledge_document WHERE id = %s", (self.id,))


def _has_fk(dsn: str) -> bool:
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "SELECT confdeltype FROM pg_constraint WHERE conname = %s AND contype = 'f' "
            "AND conrelid = 'knowledge_chunk_revision'::regclass "
            "AND confrelid = 'knowledge_document'::regclass",
            (_FK,),
        ).fetchone()
    return row is not None and row[0] == "c"  # ON DELETE CASCADE


def test_revisions_go_with_their_document(fresh_db: str) -> None:
    doc = _Doc(fresh_db)
    kept = doc.revision()
    orphan = doc.revision(document_id=uuid4())  # possible before: no FK
    assert not _has_fk(fresh_db)

    command.upgrade(_alembic(fresh_db), _AFTER)
    assert _has_fk(fresh_db)
    assert doc.revisions() == {kept}  # the orphan was cleared first
    with pytest.raises(errors.ForeignKeyViolation):
        doc.revision(document_id=uuid4())
    assert orphan not in doc.revisions()

    doc.delete()
    assert doc.revisions() == set()


def _columns(dsn: str) -> set[tuple[str, str]]:
    with psycopg.connect(dsn, autocommit=True) as conn:
        return {
            (row[0], row[1])
            for row in conn.execute(
                "SELECT table_name, column_name FROM information_schema.columns "
                "WHERE table_name IN ('knowledge_chunk', 'knowledge_chunk_revision')"
            )
        }


def _retention_may_delete(dsn: str) -> bool:
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "SELECT has_table_privilege('retention_cleanup_worker', "
            "'knowledge_chunk_revision', 'SELECT') AND "
            "has_table_privilege('retention_cleanup_worker', 'knowledge_chunk_revision', 'DELETE')"
        ).fetchone()
    return bool(row and row[0])


_NEW_COLUMNS = {
    ("knowledge_chunk", "disabled_reason"),
    ("knowledge_chunk_revision", "orphaned_at"),
    ("knowledge_chunk_revision", "switch_off_lost"),
}


def _reason_check(dsn: str) -> tuple[bool, ...] | None:
    """``(convalidated,)`` of the ``disabled_reason`` CHECK, ``None`` if absent."""
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "SELECT convalidated FROM pg_constraint "
            "WHERE conname = 'knowledge_chunk_disabled_reason_enum' AND contype = 'c' "
            "AND conrelid = 'knowledge_chunk'::regclass"
        ).fetchone()
    return None if row is None else (bool(row[0]),)


def test_the_reason_check_is_never_validated_and_nothing_commits_midway() -> None:
    """The CHECK goes on ``knowledge_chunk`` NOT VALID and is not validated:
    a validation scan inside the release's single ``upgrade head``
    transaction would hold the ALTERs' ACCESS EXCLUSIVE lock (blocking
    recall), and committing midway to avoid that would leave a half-applied
    run that a retry cannot redo. The whole migration stays in one
    transaction: one COMMIT, at the end."""
    out = io.StringIO()
    cfg = Config(str(ALEMBIC_INI), output_buffer=out)
    cfg.set_main_option("sqlalchemy.url", "postgresql+psycopg://offline/none")
    command.upgrade(cfg, f"{_BEFORE}:{_AFTER}", sql=True)
    sql = out.getvalue()
    added = sql.index("ADD CONSTRAINT knowledge_chunk_disabled_reason_enum")
    assert "NOT VALID" in sql[added : sql.index(";", added)]
    assert "VALIDATE CONSTRAINT" not in sql
    assert sql.count("COMMIT") == 1
    assert sql.rindex("COMMIT") > sql.rindex("alembic_version")


def test_disabled_reason_and_orphaned_at_columns(fresh_db: str) -> None:
    doc = _Doc(fresh_db)
    with psycopg.connect(fresh_db, autocommit=True) as conn:
        conn.execute(  # a chunk from before: the CHECK is validated over it
            "INSERT INTO knowledge_chunk (id, tenant_id, kb_id, document_id, chunk_index, "
            "content, chunk_type) VALUES (gen_random_uuid(), %s, %s, %s, 9, 'p', 'parent')",
            (doc.tenant, doc.kb, doc.id),
        )
    assert not _NEW_COLUMNS & _columns(fresh_db)
    assert not _retention_may_delete(fresh_db)
    command.upgrade(_alembic(fresh_db), _AFTER)
    assert _NEW_COLUMNS <= _columns(fresh_db)
    # Added NOT VALID and left so (no full scan under the lock that blocks
    # recall); new writes are still checked (below).
    assert _reason_check(fresh_db) == (False,)
    assert _retention_may_delete(fresh_db)  # the 30-day sweep of orphaned histories
    with psycopg.connect(fresh_db, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO knowledge_chunk (id, tenant_id, kb_id, document_id, chunk_index, "
            "content, chunk_type, enabled, disabled_reason) "
            "VALUES (gen_random_uuid(), %s, %s, %s, 0, 'p', 'parent', false, 'inherited')",
            (doc.tenant, doc.kb, doc.id),
        )
        with pytest.raises(errors.CheckViolation):
            conn.execute(
                "INSERT INTO knowledge_chunk (id, tenant_id, kb_id, document_id, chunk_index, "
                "content, chunk_type, disabled_reason) "
                "VALUES (gen_random_uuid(), %s, %s, %s, 1, 'p', 'parent', 'whatever')",
                (doc.tenant, doc.kb, doc.id),
            )


def test_downgrade_drops_what_it_added(fresh_db: str) -> None:
    command.upgrade(_alembic(fresh_db), _AFTER)
    command.downgrade(_alembic(fresh_db), _BEFORE)
    assert not _has_fk(fresh_db)
    assert not _NEW_COLUMNS & _columns(fresh_db)
    assert _reason_check(fresh_db) is None
    assert not _retention_may_delete(fresh_db)
    doc = _Doc(fresh_db)
    mine = doc.revision()
    doc.delete()
    assert mine in doc.revisions()  # no cascade any more

    with psycopg.connect(fresh_db, autocommit=True) as conn:
        conn.execute("DELETE FROM knowledge_chunk_revision")
    command.upgrade(_alembic(fresh_db), _AFTER)
    assert _has_fk(fresh_db)
