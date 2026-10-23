"""迁移 0173 —— B-215:识别缓存多存一种 ``semantic_break``(切块的主题断点判断)。

升级前(0172)CHECK 拒收这种行;升到 head 后能写;降级先删掉这种行再收紧 CHECK(别的缓存行不动),
再升回来。用容器里另建的库(共享库会被别的测试升 / 降级)。
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from uuid import UUID, uuid4

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from testcontainers.postgres import PostgresContainer

pytestmark = pytest.mark.integration

ALEMBIC_INI = Path(__file__).resolve().parent.parent / "alembic.ini"
_DB = "migration_0173_test"
_BEFORE = "0172_kb_document_figures"


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


def _document(dsn: str) -> tuple[UUID, UUID]:
    tenant, base, document = uuid4(), uuid4(), uuid4()
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO knowledge_base (id, tenant_id, name) VALUES (%s, %s, 'kb')",
            (base, tenant),
        )
        conn.execute(
            "INSERT INTO knowledge_document (id, tenant_id, kb_id, filename, status) "
            "VALUES (%s, %s, %s, 'report.pdf', 'ready')",
            (document, tenant, base),
        )
    return tenant, document


def _cache(dsn: str, tenant: UUID, document: UUID, kind: str) -> None:
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO knowledge_parse_cache "
            "(tenant_id, document_id, kind, cache_key, model, result) "
            "VALUES (%s, %s, %s, %s, 'm', '{}')",
            (tenant, document, kind, f"{kind}-key"),
        )


def _kinds(dsn: str) -> list[str]:
    with psycopg.connect(dsn, autocommit=True) as conn:
        rows = conn.execute("SELECT kind FROM knowledge_parse_cache ORDER BY kind").fetchall()
    return [row[0] for row in rows]


def test_the_cache_takes_topic_decisions_only_after_the_upgrade(fresh_db: str) -> None:
    tenant, document = _document(fresh_db)
    _cache(fresh_db, tenant, document, "page_ocr")
    with pytest.raises(psycopg.errors.CheckViolation):
        _cache(fresh_db, tenant, document, "semantic_break")
    command.upgrade(_alembic(fresh_db), "head")
    _cache(fresh_db, tenant, document, "semantic_break")
    assert _kinds(fresh_db) == ["page_ocr", "semantic_break"]


def test_the_downgrade_drops_only_topic_decisions_and_upgrades_again(fresh_db: str) -> None:
    tenant, document = _document(fresh_db)
    command.upgrade(_alembic(fresh_db), "head")
    _cache(fresh_db, tenant, document, "page_ocr")
    _cache(fresh_db, tenant, document, "semantic_break")
    command.downgrade(_alembic(fresh_db), _BEFORE)
    assert _kinds(fresh_db) == ["page_ocr"]
    with pytest.raises(psycopg.errors.CheckViolation):
        _cache(fresh_db, tenant, document, "semantic_break")
    command.upgrade(_alembic(fresh_db), "head")
    _cache(fresh_db, tenant, document, "semantic_break")
