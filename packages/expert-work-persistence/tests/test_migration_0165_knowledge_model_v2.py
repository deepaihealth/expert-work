"""迁移 0165 —— 知识库数据模型 v2(B-175 L2-PR3)。

在 0164 上灌「迁移前形状」的数据再升到 head,看存量处理真的发生了;在 head 上灌数据
测不出 UPDATE / DELETE 有没有跑。

* ``doc_seq`` 按租户、上传顺序回填,两个租户各自从 1 编;计数表播种成每个租户的最大号。
* 有重复分段的文档只留最新那一整套(``created_at`` 大的)—— 旧那套更长时多出来的段也删,
  别的文档不受影响。
* 新表都有租户策略(全局覆盖由 ``test_rls_policy_coverage`` 管,这里只钉本迁移的五张);
  ``knowledge_task`` 给清扫角色授了 SELECT / DELETE。
* downgrade 回 0164 再 upgrade 能来回走;downgrade 把本版写的分段改回旧形状(标题前缀
  写回正文),旧镜像读到的仍是「带前缀的整段」。

用容器里另建一个库:共享的默认库会被别的测试升到 head,再 ``upgrade 0164`` 就是降级。

本地跑前: export DOCKER_HOST=unix:///Users/mac/.docker/run/docker.sock
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from testcontainers.postgres import PostgresContainer

from expert_work.persistence.embedding import EMBEDDING_DIM

pytestmark = pytest.mark.integration

ALEMBIC_INI = Path(__file__).resolve().parent.parent / "alembic.ini"
_DB = "knowledge_model_v2_test"
_BEFORE = "0164_knowledge_ingest_generation"
_NEW_TABLES = (
    "knowledge_doc_seq",
    "knowledge_task",
    "knowledge_chunk_revision",
    "knowledge_tag",
    "knowledge_document_tag",
)
_VECTOR = "[" + ",".join(["0.1"] * EMBEDDING_DIM) + "]"


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
    """A database of its own, migrated only as far as 0164."""
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


def _seed_base(conn: psycopg.Connection, tenant: UUID) -> UUID:
    base_id = uuid4()
    conn.execute(
        "INSERT INTO knowledge_base (id, tenant_id, name) VALUES (%s, %s, %s)",
        (base_id, tenant, f"kb-{base_id.hex[:6]}"),
    )
    return base_id


def _seed_document(
    conn: psycopg.Connection, *, tenant: UUID, kb_id: UUID, filename: str, created_at: datetime
) -> UUID:
    document_id = uuid4()
    conn.execute(
        """
        INSERT INTO knowledge_document (id, tenant_id, kb_id, filename, status, created_at)
        VALUES (%s, %s, %s, %s, 'ready', %s)
        """,
        (document_id, tenant, kb_id, filename, created_at),
    )
    return document_id


def _seed_chunk(
    conn: psycopg.Connection,
    *,
    tenant: UUID,
    kb_id: UUID,
    document_id: UUID,
    index: int,
    content: str,
    created_at: datetime,
) -> None:
    conn.execute(
        """
        INSERT INTO knowledge_chunk (tenant_id, kb_id, document_id, chunk_index, content,
                                     embedding, created_at)
        VALUES (%s, %s, %s, %s, %s, %s::vector, %s)
        """,
        (tenant, kb_id, document_id, index, content, _VECTOR, created_at),
    )


def test_doc_seq_backfilled_per_tenant_and_counter_seeded(fresh_db: str) -> None:
    alpha, beta = uuid4(), uuid4()
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    with psycopg.connect(fresh_db, autocommit=True) as conn:
        kb_alpha = _seed_base(conn, alpha)
        kb_beta = _seed_base(conn, beta)
        # Inserted out of upload order: numbering must follow created_at.
        second = _seed_document(
            conn, tenant=alpha, kb_id=kb_alpha, filename="b.md", created_at=t0 + timedelta(hours=1)
        )
        first = _seed_document(conn, tenant=alpha, kb_id=kb_alpha, filename="a.md", created_at=t0)
        only_beta = _seed_document(conn, tenant=beta, kb_id=kb_beta, filename="c.md", created_at=t0)

    command.upgrade(_alembic(fresh_db), "head")

    with psycopg.connect(fresh_db, autocommit=True) as conn:
        seqs: dict[UUID, int] = dict(
            conn.execute("SELECT id, doc_seq FROM knowledge_document").fetchall()
        )
        counters: dict[UUID, int] = dict(
            conn.execute("SELECT tenant_id, last_seq FROM knowledge_doc_seq").fetchall()
        )
    assert seqs == {first: 1, second: 2, only_beta: 1}
    assert counters == {alpha: 2, beta: 1}


def test_duplicate_chunks_keep_the_newest_set(fresh_db: str) -> None:
    tenant = uuid4()
    old, new = datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 1, 2, tzinfo=UTC)
    with psycopg.connect(fresh_db, autocommit=True) as conn:
        kb_id = _seed_base(conn, tenant)
        doubled = _seed_document(conn, tenant=tenant, kb_id=kb_id, filename="d.md", created_at=old)
        clean = _seed_document(conn, tenant=tenant, kb_id=kb_id, filename="e.md", created_at=old)
        # The stale set is longer than the newer one: its extra chunk (index 2)
        # must go too, not survive next to the new set.
        for index in range(3):
            _seed_chunk(
                conn,
                tenant=tenant,
                kb_id=kb_id,
                document_id=doubled,
                index=index,
                content=f"old-{index}",
                created_at=old,
            )
        for index in range(2):
            _seed_chunk(
                conn,
                tenant=tenant,
                kb_id=kb_id,
                document_id=doubled,
                index=index,
                content=f"new-{index}",
                created_at=new,
            )
            _seed_chunk(
                conn,
                tenant=tenant,
                kb_id=kb_id,
                document_id=clean,
                index=index,
                content=f"clean-{index}",
                created_at=old,
            )

    command.upgrade(_alembic(fresh_db), "head")

    with psycopg.connect(fresh_db, autocommit=True) as conn:
        rows = conn.execute(
            "SELECT document_id, chunk_index, content FROM knowledge_chunk "
            "ORDER BY document_id, chunk_index"
        ).fetchall()
    by_doc: dict[UUID, list[str]] = {}
    for document_id, _index, content in rows:
        by_doc.setdefault(document_id, []).append(content)
    assert by_doc[doubled] == ["new-0", "new-1"]
    assert by_doc[clean] == ["clean-0", "clean-1"]


def test_new_tables_have_tenant_policies_and_task_grant(fresh_db: str) -> None:
    command.upgrade(_alembic(fresh_db), "head")
    with psycopg.connect(fresh_db, autocommit=True) as conn:
        forced: dict[str, bool] = dict(
            conn.execute(
                "SELECT relname, relforcerowsecurity FROM pg_class WHERE relname = ANY(%s)",
                (list(_NEW_TABLES),),
            ).fetchall()
        )
        policies = {
            row[0]
            for row in conn.execute(
                "SELECT c.relname FROM pg_policy p JOIN pg_class c ON c.oid = p.polrelid "
                "WHERE c.relname = ANY(%s)",
                (list(_NEW_TABLES),),
            ).fetchall()
        }
        grants = conn.execute(
            "SELECT has_table_privilege('retention_cleanup_worker', 'knowledge_task', 'SELECT'), "
            "has_table_privilege('retention_cleanup_worker', 'knowledge_task', 'DELETE')"
        ).fetchone()
    assert forced == dict.fromkeys(_NEW_TABLES, True)
    assert policies == set(_NEW_TABLES)
    assert grants == (True, True)


def test_downgrade_round_trip(fresh_db: str) -> None:
    cfg = _alembic(fresh_db)
    command.upgrade(cfg, "head")
    tenant = uuid4()
    with psycopg.connect(fresh_db, autocommit=True) as conn:
        kb_id = _seed_base(conn, tenant)
        document_id = _seed_document(
            conn, tenant=tenant, kb_id=kb_id, filename="d.md", created_at=datetime.now(UTC)
        )
        # One chunk in the new shape (clean body + path), one legacy-shaped
        # with SQL NULL, one with a JSON ``null`` scalar, one with an empty
        # array — the shapes a store can leave behind.
        for index, content, path in (
            (0, "免赔额是 500 元。", '["健康保险", "免赔额"]'),
            (1, "[Section: Old]\n\nlegacy body", None),
            (2, "no heading", "null"),
            (3, "empty path", "[]"),
        ):
            conn.execute(
                """
                INSERT INTO knowledge_chunk (tenant_id, kb_id, document_id, chunk_index,
                                             content, embedding, heading_path)
                VALUES (%s, %s, %s, %s, %s, %s::vector, %s::jsonb)
                """,
                (tenant, kb_id, document_id, index, content, _VECTOR, path),
            )

    command.downgrade(cfg, _BEFORE)
    with psycopg.connect(fresh_db, autocommit=True) as conn:
        contents = [
            row[0]
            for row in conn.execute(
                "SELECT content FROM knowledge_chunk WHERE document_id = %s ORDER BY chunk_index",
                (document_id,),
            ).fetchall()
        ]
    assert contents == [
        "[Section: 健康保险 > 免赔额]\n\n免赔额是 500 元。",
        "[Section: Old]\n\nlegacy body",
        "no heading",
        "empty path",
    ]
    with psycopg.connect(fresh_db, autocommit=True) as conn:
        leftover = conn.execute(
            "SELECT count(*) FROM pg_class WHERE relname = ANY(%s)", (list(_NEW_TABLES),)
        ).fetchone()
        doc_seq_column = conn.execute(
            "SELECT count(*) FROM information_schema.columns "
            "WHERE table_name = 'knowledge_document' AND column_name = 'doc_seq'"
        ).fetchone()
    assert leftover == (0,)
    assert doc_seq_column == (0,)
    command.upgrade(cfg, "head")
