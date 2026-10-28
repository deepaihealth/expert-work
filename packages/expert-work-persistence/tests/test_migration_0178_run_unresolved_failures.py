"""Migration 0178 — B-226 ⑥: ``agent_run.unresolved_failures`` (nullable JSONB).

Expand-only: upgrading adds the column without touching existing rows (they read
NULL = no record, never ``[]``); downgrading drops it. Runs in its own database
inside the container (the shared one is upgraded / downgraded by other tests).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from testcontainers.postgres import PostgresContainer

pytestmark = pytest.mark.integration

ALEMBIC_INI = Path(__file__).resolve().parent.parent / "alembic.ini"
_DB = "migration_0178_test"
_REVISION = "0178_run_unresolved_failures"
# Mirrors the migration's ``down_revision`` (see the chain note there).
_BEFORE = "0175_kb_k1_parent_chunks"


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


def _column(dsn: str) -> tuple[str, str] | None:
    with psycopg.connect(dsn) as conn:
        row = conn.execute(
            "SELECT data_type, is_nullable FROM information_schema.columns "
            "WHERE table_name = 'agent_run' AND column_name = 'unresolved_failures'"
        ).fetchone()
    return None if row is None else (str(row[0]), str(row[1]))


def _insert_run(dsn: str) -> str:
    run_id = uuid4()
    now = datetime.now(UTC)
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO agent_run (id, tenant_id, thread_id, status, on_disconnect, "
            "created_at, updated_at) VALUES (%s, %s, %s, 'success', 'cancel', %s, %s)",
            (run_id, uuid4(), uuid4(), now, now),
        )
    return str(run_id)


def test_revision_id_fits_alembic_version_column() -> None:
    assert len(_REVISION) <= 32


def test_upgrade_adds_a_nullable_jsonb_column_and_leaves_old_rows_null(fresh_db: str) -> None:
    assert _column(fresh_db) is None
    old_run = _insert_run(fresh_db)

    command.upgrade(_alembic(fresh_db), _REVISION)

    assert _column(fresh_db) == ("jsonb", "YES")
    with psycopg.connect(fresh_db) as conn:
        value = conn.execute(
            "SELECT unresolved_failures FROM agent_run WHERE id = %s", (old_run,)
        ).fetchone()
    assert value is not None and value[0] is None, "no backfill: an old run is no record"


def test_downgrade_drops_the_column(fresh_db: str) -> None:
    command.upgrade(_alembic(fresh_db), _REVISION)
    command.downgrade(_alembic(fresh_db), _BEFORE)
    assert _column(fresh_db) is None
    command.upgrade(_alembic(fresh_db), _REVISION)
    assert _column(fresh_db) == ("jsonb", "YES")
