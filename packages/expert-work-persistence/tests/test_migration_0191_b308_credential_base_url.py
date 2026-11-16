"""迁移 0191 —— ``platform_provider_secret`` / ``tenant_provider_secret`` 各加两列(B-308 设计
§4.1):请求地址 ``base_url``(可空)和「未通过测试」标记 ``base_url_unverified``(非空,默认
false)。

在 0190 上建库、每张表先写一条旧行,再升到 0191:每张表多两列、类型对;旧行 ``base_url`` 为空、
``base_url_unverified`` 为 false;检查约束拒 ``http://``、放行 ``https://`` 和空;降回 0190 四列都
没了、旧行还在。用容器里另建的库(共享库会被别的测试升 / 降级)。
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
_DB = "migration_0191_test"
_BEFORE = "0190_b300_memory_evidence"
_REVISION = "0191_b308_credential_base_url"
_TABLES = ("platform_provider_secret", "tenant_provider_secret")
_EXPECTED = {
    "base_url": ("text", "YES", None),
    "base_url_unverified": ("boolean", "NO", "false"),
}


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


def _columns(dsn: str, table: str) -> dict[str, tuple[str, str, str | None]]:
    """``{name: (data_type, is_nullable, column_default)}`` for the two new columns."""
    with psycopg.connect(dsn) as conn:
        rows = conn.execute(
            "SELECT column_name, data_type, is_nullable, column_default "
            "FROM information_schema.columns "
            "WHERE table_name = %s AND column_name = ANY(%s)",
            (table, list(_EXPECTED)),
        ).fetchall()
    return {r[0]: (r[1], r[2], r[3]) for r in rows}


def _seed_legacy_rows(dsn: str) -> str:
    tenant_id = str(uuid4())
    with psycopg.connect(dsn) as conn:
        conn.execute(
            "INSERT INTO platform_provider_secret (provider, key_id, secret_ref, updated_by) "
            "VALUES ('glm', 'default', 'secret://legacy/platform', 'tester')"
        )
        conn.execute(
            "INSERT INTO tenant_provider_secret (tenant_id, provider, secret_ref, updated_by) "
            "VALUES (%s, 'glm', 'secret://legacy/tenant', 'tester')",
            (tenant_id,),
        )
    return tenant_id


def test_upgrade_adds_two_columns_per_table_and_leaves_old_rows_official(fresh_db: str) -> None:
    _seed_legacy_rows(fresh_db)
    for table in _TABLES:
        assert _columns(fresh_db, table) == {}
    command.upgrade(_alembic(fresh_db), _REVISION)
    for table in _TABLES:
        assert _columns(fresh_db, table) == _EXPECTED
    with psycopg.connect(fresh_db) as conn:
        platform = conn.execute(
            "SELECT secret_ref, base_url, base_url_unverified FROM platform_provider_secret"
        ).fetchall()
        tenant = conn.execute(
            "SELECT secret_ref, base_url, base_url_unverified FROM tenant_provider_secret"
        ).fetchall()
    # 不回填:旧行走官方地址、没有「未通过测试」标记。
    assert platform == [("secret://legacy/platform", None, False)]
    assert tenant == [("secret://legacy/tenant", None, False)]


@pytest.mark.parametrize("table", _TABLES)
def test_check_constraint_only_accepts_https_or_null(fresh_db: str, table: str) -> None:
    _seed_legacy_rows(fresh_db)
    command.upgrade(_alembic(fresh_db), _REVISION)
    with psycopg.connect(fresh_db, autocommit=True) as conn:
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(f"UPDATE {table} SET base_url = 'http://proxy.example.com/v1'")  # noqa: S608
        conn.execute(f"UPDATE {table} SET base_url = 'https://proxy.example.com/v1'")  # noqa: S608
        conn.execute(f"UPDATE {table} SET base_url = NULL")  # noqa: S608
        stored = conn.execute(f"SELECT base_url FROM {table}").fetchall()  # noqa: S608
    assert stored == [(None,)]


def test_downgrade_drops_the_four_columns_and_keeps_rows(fresh_db: str) -> None:
    _seed_legacy_rows(fresh_db)
    command.upgrade(_alembic(fresh_db), _REVISION)
    command.downgrade(_alembic(fresh_db), _BEFORE)
    for table in _TABLES:
        assert _columns(fresh_db, table) == {}
    with psycopg.connect(fresh_db) as conn:
        kept = conn.execute(
            "SELECT (SELECT count(*) FROM platform_provider_secret), "
            "(SELECT count(*) FROM tenant_provider_secret)"
        ).fetchone()
    assert kept == (1, 1)
