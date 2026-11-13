"""迁移 0189 —— ``memory_item`` 加两个可空列(B-300 PR 2):来源、向量模型名。

在 0188 上建库、先写一条旧行,再升到 0189:两列都在、可空、没有默认值、没有检查约束和
索引,旧行两列都是空;降回 0188 两列都没了、旧行还在。用容器里另建的库(共享库会被
别的测试升 / 降级)。
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

from expert_work.persistence.embedding import EMBEDDING_DIM

pytestmark = pytest.mark.integration

ALEMBIC_INI = Path(__file__).resolve().parent.parent / "alembic.ini"
_DB = "migration_0189_test"
_BEFORE = "0188_b300_consolidation_state"
_REVISION = "0189_b300_memory_origin"
_COLUMNS = ("origin", "embedding_model")


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


def _columns(dsn: str) -> dict[str, tuple[str, str, str | None]]:
    """``{name: (data_type, is_nullable, column_default)}`` for the two new columns."""
    with psycopg.connect(dsn) as conn:
        rows = conn.execute(
            "SELECT column_name, data_type, is_nullable, column_default "
            "FROM information_schema.columns "
            "WHERE table_name = 'memory_item' AND column_name = ANY(%s)",
            (list(_COLUMNS),),
        ).fetchall()
    return {r[0]: (r[1], r[2], r[3]) for r in rows}


def _seed_legacy_row(dsn: str) -> str:
    row_id = str(uuid4())
    vector = "[" + ",".join(["0"] * EMBEDDING_DIM) + "]"
    with psycopg.connect(dsn) as conn:
        conn.execute(
            "INSERT INTO memory_item (id, tenant_id, user_id, kind, content, embedding, "
            "content_hash) VALUES (%s, %s, %s, 'fact', 'legacy', %s::vector, %s)",
            (row_id, str(uuid4()), str(uuid4()), vector, "0" * 64),
        )
    return row_id


def test_upgrade_adds_two_nullable_text_columns_without_default(fresh_db: str) -> None:
    row_id = _seed_legacy_row(fresh_db)
    assert _columns(fresh_db) == {}
    command.upgrade(_alembic(fresh_db), _REVISION)
    assert _columns(fresh_db) == dict.fromkeys(_COLUMNS, ("text", "YES", None))
    with psycopg.connect(fresh_db) as conn:
        legacy = conn.execute(
            "SELECT origin, embedding_model FROM memory_item WHERE id = %s", (row_id,)
        ).fetchone()
        # 不回填、不加检查约束、不加索引(设计 §6.3)。
        checks = conn.execute(
            "SELECT count(*) FROM pg_constraint WHERE conrelid = 'memory_item'::regclass "
            "AND contype = 'c' AND pg_get_constraintdef(oid) ~ '(origin|embedding_model)'"
        ).fetchone()
        indexes = conn.execute(
            "SELECT count(*) FROM pg_indexes WHERE tablename = 'memory_item' "
            "AND indexdef ~ '(origin|embedding_model)'"
        ).fetchone()
    assert legacy == (None, None)
    assert checks == (0,)
    assert indexes == (0,)


def test_downgrade_drops_both_columns_and_keeps_rows(fresh_db: str) -> None:
    row_id = _seed_legacy_row(fresh_db)
    command.upgrade(_alembic(fresh_db), _REVISION)
    command.downgrade(_alembic(fresh_db), _BEFORE)
    assert _columns(fresh_db) == {}
    with psycopg.connect(fresh_db) as conn:
        kept = conn.execute("SELECT count(*) FROM memory_item WHERE id = %s", (row_id,)).fetchone()
    assert kept == (1,)
