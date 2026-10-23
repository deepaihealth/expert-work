"""迁移 0172 —— B-209 PR-2:文档行上的图片计数(``figure_stats``)与像素哈希(``figure_hashes``)。

在升级前的版本(0171)上灌一份文档,再升到 head:旧行两列为空(改前入库的文档不报图片数),
新值能写能读;降级把两列删掉、再升回来。用容器里另建的库(共享库会被别的测试升 / 降级)。
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from testcontainers.postgres import PostgresContainer

pytestmark = pytest.mark.integration

ALEMBIC_INI = Path(__file__).resolve().parent.parent / "alembic.ini"
_DB = "migration_0172_test"
_BEFORE = "0171_kb_rerank_threshold_model"


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


def _columns(dsn: str) -> dict[str, str]:
    with psycopg.connect(dsn, autocommit=True) as conn:
        rows = conn.execute(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_name = 'knowledge_document' "
            "AND column_name IN ('figure_stats', 'figure_hashes')"
        ).fetchall()
    return {row[0]: row[1] for row in rows}


def test_existing_documents_get_empty_figure_columns(fresh_db: str) -> None:
    tenant, base, document = uuid4(), uuid4(), uuid4()
    with psycopg.connect(fresh_db, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO knowledge_base (id, tenant_id, name) VALUES (%s, %s, 'kb')",
            (base, tenant),
        )
        conn.execute(
            "INSERT INTO knowledge_document (id, tenant_id, kb_id, filename, status) "
            "VALUES (%s, %s, %s, 'old.docx', 'ready')",
            (document, tenant, base),
        )
    command.upgrade(_alembic(fresh_db), "head")
    assert _columns(fresh_db) == {"figure_stats": "jsonb", "figure_hashes": "ARRAY"}
    with psycopg.connect(fresh_db, autocommit=True) as conn:
        row = conn.execute(
            "SELECT figure_stats, figure_hashes FROM knowledge_document WHERE id = %s",
            (document,),
        ).fetchone()
        assert row == (None, None)
        conn.execute(
            "UPDATE knowledge_document SET figure_hashes = %s WHERE id = %s",
            (["a", "b"], document),
        )
        hit = conn.execute(
            "SELECT count(*) FROM knowledge_document WHERE figure_hashes && %s",
            (["b", "c"],),
        ).fetchone()
    assert hit == (1,)


def test_downgrade_round_trip(fresh_db: str) -> None:
    cfg = _alembic(fresh_db)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, _BEFORE)
    assert _columns(fresh_db) == {}
    command.upgrade(cfg, "head")
    assert set(_columns(fresh_db)) == {"figure_stats", "figure_hashes"}
