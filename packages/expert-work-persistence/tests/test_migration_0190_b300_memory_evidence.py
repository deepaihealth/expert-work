"""迁移 0190 —— ``memory_item`` 加四个可空列(B-300 重做 PR M,设计 §9.1):证据指针
(``evidence_message_id`` / ``evidence_message_index``)、事件日期 ``occurred_on``、确认日期
``confirmed_at``。

在 0189 上建库、先写一条旧行,再升到 0190:四列都在、类型对、可空、没有默认值、没有检查约束和
索引,旧行四列都是空;降回 0189 四列都没了、旧行还在。用容器里另建的库(共享库会被别的测试升 /
降级)。
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
_DB = "migration_0190_test"
_BEFORE = "0189_b300_memory_origin"
_REVISION = "0190_b300_memory_evidence"
_EXPECTED = {
    "evidence_message_id": ("text", "YES", None),
    "evidence_message_index": ("integer", "YES", None),
    "occurred_on": ("date", "YES", None),
    "confirmed_at": ("timestamp with time zone", "YES", None),
}
_PATTERN = "(evidence_message_id|evidence_message_index|occurred_on|confirmed_at)"


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
    """``{name: (data_type, is_nullable, column_default)}`` for the four new columns."""
    with psycopg.connect(dsn) as conn:
        rows = conn.execute(
            "SELECT column_name, data_type, is_nullable, column_default "
            "FROM information_schema.columns "
            "WHERE table_name = 'memory_item' AND column_name = ANY(%s)",
            (list(_EXPECTED),),
        ).fetchall()
    return {r[0]: (r[1], r[2], r[3]) for r in rows}


def _seed_legacy_row(dsn: str) -> str:
    row_id = str(uuid4())
    vector = "[" + ",".join(["0"] * EMBEDDING_DIM) + "]"
    with psycopg.connect(dsn) as conn:
        conn.execute(
            "INSERT INTO memory_item (id, tenant_id, user_id, kind, content, embedding, "
            "content_hash, origin) VALUES (%s, %s, %s, 'fact', 'legacy', %s::vector, %s, "
            "'user_stated')",
            (row_id, str(uuid4()), str(uuid4()), vector, "0" * 64),
        )
    return row_id


def test_upgrade_adds_four_nullable_columns_without_default(fresh_db: str) -> None:
    row_id = _seed_legacy_row(fresh_db)
    assert _columns(fresh_db) == {}
    command.upgrade(_alembic(fresh_db), _REVISION)
    assert _columns(fresh_db) == _EXPECTED
    with psycopg.connect(fresh_db) as conn:
        legacy = conn.execute(
            "SELECT evidence_message_id, evidence_message_index, occurred_on, confirmed_at, "
            "origin FROM memory_item WHERE id = %s",
            (row_id,),
        ).fetchone()
        # 不回填、不加检查约束、不加索引(设计 §9.1)。
        checks = conn.execute(
            "SELECT count(*) FROM pg_constraint WHERE conrelid = 'memory_item'::regclass "
            "AND contype = 'c' AND pg_get_constraintdef(oid) ~ %s",
            (_PATTERN,),
        ).fetchone()
        indexes = conn.execute(
            "SELECT count(*) FROM pg_indexes WHERE tablename = 'memory_item' AND indexdef ~ %s",
            (_PATTERN,),
        ).fetchone()
    assert legacy == (None, None, None, None, "user_stated")
    assert checks == (0,)
    assert indexes == (0,)


def test_downgrade_drops_the_four_columns_and_keeps_rows(fresh_db: str) -> None:
    row_id = _seed_legacy_row(fresh_db)
    command.upgrade(_alembic(fresh_db), _REVISION)
    command.downgrade(_alembic(fresh_db), _BEFORE)
    assert _columns(fresh_db) == {}
    with psycopg.connect(fresh_db) as conn:
        kept = conn.execute(
            "SELECT count(*), max(origin) FROM memory_item WHERE id = %s", (row_id,)
        ).fetchone()
    assert kept == (1, "user_stated")
