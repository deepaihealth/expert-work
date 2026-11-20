"""迁移 0194 —— 产物分享链接表(B-326)。

在 0193 上建库再升到 0194:表在、开了 RLS(``FORCE``)且有一条租户 + 用户的策略、没有外键
(指向 ``FORCE`` RLS 表的外键是 J-1a 的坑);降回 0193 表没了。用容器里另建的库
(共享库会被别的测试升 / 降级)。
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from testcontainers.postgres import PostgresContainer

pytestmark = pytest.mark.integration

ALEMBIC_INI = Path(__file__).resolve().parent.parent / "alembic.ini"
_DB = "migration_0194_test"
_BEFORE = "0193_rate_card_claude5"
_REVISION = "0194_b326_artifact_share_links"
_TABLE = "artifact_share_links"


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


def _table_state(dsn: str) -> tuple[bool, bool, int] | None:
    """``(rls enabled, rls forced, policy count)``;表不存在为 ``None``。"""
    with psycopg.connect(dsn) as conn:
        row = conn.execute(
            "SELECT c.relrowsecurity, c.relforcerowsecurity, "
            "(SELECT count(*) FROM pg_policies p WHERE p.tablename = c.relname) "
            "FROM pg_class c WHERE c.relname = %s AND c.relkind = 'r'",
            (_TABLE,),
        ).fetchone()
    return None if row is None else (bool(row[0]), bool(row[1]), int(row[2]))


def test_upgrade_creates_table_with_rls_and_no_fk(fresh_db: str) -> None:
    assert _table_state(fresh_db) is None
    command.upgrade(_alembic(fresh_db), _REVISION)
    assert _table_state(fresh_db) == (True, True, 1)
    with psycopg.connect(fresh_db) as conn:
        fk = conn.execute(
            "SELECT confrelid::regclass::text, confdeltype FROM pg_constraint "
            "WHERE conrelid = %s::regclass AND contype = 'f'",
            (_TABLE,),
        ).fetchall()
        policy = conn.execute(
            "SELECT qual FROM pg_policies WHERE tablename = %s", (_TABLE,)
        ).fetchone()
    assert fk == []
    with psycopg.connect(fresh_db) as conn:
        columns: dict[str, str] = dict(
            conn.execute(
                "SELECT column_name, is_nullable FROM information_schema.columns "
                "WHERE table_name = %s",
                (_TABLE,),
            ).fetchall()
        )
        indexes = {
            name
            for (name,) in conn.execute(
                "SELECT indexname FROM pg_indexes WHERE tablename = %s", (_TABLE,)
            ).fetchall()
        }
        retention_can = conn.execute(
            "SELECT has_table_privilege('retention_cleanup_worker', %s, 'SELECT'), "
            "has_table_privilege('retention_cleanup_worker', %s, 'DELETE')",
            (_TABLE, _TABLE),
        ).fetchone()
    assert columns["download_count"] == "NO"
    assert columns["file_name"] == "NO"
    assert indexes == {
        "artifact_share_links_pkey",
        "ix_artifact_share_links_expires_at",
        "ix_artifact_share_links_lookup",
    }
    # H1 —— the retention job deletes expired rows after a grace window; its
    # ``DELETE … WHERE ctid IN (SELECT …)`` needs both privileges.
    assert retention_can == (True, True)
    assert columns["thread_id"] == "NO"
    assert policy is not None
    assert "app.tenant_id" in policy[0]
    assert "app.user_id" in policy[0]


def test_downgrade_drops_table(fresh_db: str) -> None:
    command.upgrade(_alembic(fresh_db), _REVISION)
    command.downgrade(_alembic(fresh_db), _BEFORE)
    assert _table_state(fresh_db) is None


_INSERT = (
    "INSERT INTO artifact_share_links (id, tenant_id, user_id, agent_key, artifact_id, "
    "artifact_version_id, version, thread_id, created_at, expires_at, download_count) "
    "VALUES (%(id)s, %(tenant_id)s, %(user_id)s, 'a', %(artifact_id)s, %(version_id)s, 1, "
    "%(thread_id)s, %(created_at)s, %(expires_at)s, %(download_count)s)"
)


@pytest.mark.parametrize("column", ["thread_id", "download_count", "expires_at"])
def test_constraints_reject_bad_rows(fresh_db: str, column: str) -> None:
    """L7 —— 链接必属于某段对话;下载次数不为负;过期时刻晚于创建时刻。"""
    command.upgrade(_alembic(fresh_db), _REVISION)
    now = datetime.now(UTC)
    good: dict[str, object] = {
        "id": "x000000000000000",
        "tenant_id": uuid4(),
        "user_id": uuid4(),
        "artifact_id": uuid4(),
        "version_id": uuid4(),
        "thread_id": uuid4(),
        "created_at": now,
        "expires_at": now + timedelta(hours=1),
        "download_count": 0,
    }
    bad = {
        **good,
        "thread_id": None if column == "thread_id" else good["thread_id"],
        "download_count": -1 if column == "download_count" else 0,
        "expires_at": now - timedelta(seconds=1) if column == "expires_at" else good["expires_at"],
    }
    with psycopg.connect(fresh_db) as conn:
        conn.execute(_INSERT, good)  # the baseline row is accepted
        conn.rollback()
        with pytest.raises(psycopg.errors.IntegrityError):
            conn.execute(_INSERT, bad)
