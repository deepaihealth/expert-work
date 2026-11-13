"""迁移 0188 —— 记忆整理的两张小表(B-300 PR A):按租户的用户游标、被拒的簇。

在 0187 上建库再升到 0188:两张表都在、都开了 RLS 且有策略(``FORCE``,只按租户);
降回 0187 两张表都没了。用容器里另建的库(共享库会被别的测试升 / 降级)。
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from testcontainers.postgres import PostgresContainer

pytestmark = pytest.mark.integration

ALEMBIC_INI = Path(__file__).resolve().parent.parent / "alembic.ini"
_DB = "migration_0188_test"
_BEFORE = "0187_rate_card_dated_ids"
_REVISION = "0188_b300_consolidation_state"
_TABLES = ("memory_consolidation_cursor", "memory_cluster_rejection")


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


def _table_state(dsn: str, table: str) -> tuple[bool, bool, int] | None:
    """``(rls enabled, rls forced, policy count)``;表不存在为 ``None``。"""
    with psycopg.connect(dsn) as conn:
        row = conn.execute(
            "SELECT c.relrowsecurity, c.relforcerowsecurity, "
            "(SELECT count(*) FROM pg_policies p WHERE p.tablename = c.relname) "
            "FROM pg_class c WHERE c.relname = %s AND c.relkind = 'r'",
            (table,),
        ).fetchone()
    return None if row is None else (bool(row[0]), bool(row[1]), int(row[2]))


def test_upgrade_creates_both_tables_with_tenant_rls(fresh_db: str) -> None:
    assert all(_table_state(fresh_db, t) is None for t in _TABLES)
    command.upgrade(_alembic(fresh_db), _REVISION)
    for table in _TABLES:
        assert _table_state(fresh_db, table) == (True, True, 1), table


def test_downgrade_drops_both_tables(fresh_db: str) -> None:
    command.upgrade(_alembic(fresh_db), _REVISION)
    command.downgrade(_alembic(fresh_db), _BEFORE)
    assert all(_table_state(fresh_db, t) is None for t in _TABLES)
