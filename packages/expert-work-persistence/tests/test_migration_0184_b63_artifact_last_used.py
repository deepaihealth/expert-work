"""Migration 0184 — B-63: ``artifact_version.last_used_at``.

Added nullable with no default, so existing rows read NULL (never used since the
column existed) and an old pod's insert (which never names it) still works. Runs
in its own database inside the container (the shared one is upgraded / downgraded
by other tests).
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
_DB = "migration_0184_test"
_REVISION = "0184_b63_artifact_last_used"
# Mirrors the migration's ``down_revision``.
_BEFORE = "0183_b63_upload_last_used"
_COLUMNS = (("artifact_version", "last_used_at"),)


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


def _column(dsn: str, table: str, column: str) -> tuple[str, str | None] | None:
    with psycopg.connect(dsn) as conn:
        row = conn.execute(
            "SELECT is_nullable, column_default FROM information_schema.columns "
            "WHERE table_name = %s AND column_name = %s",
            (table, column),
        ).fetchone()
    return None if row is None else (str(row[0]), row[1])


def test_revision_id_fits_alembic_version_column() -> None:
    assert len(_REVISION) <= 32


def test_columns_are_added_nullable_without_default(fresh_db: str) -> None:
    command.upgrade(_alembic(fresh_db), _REVISION)
    for table, column in _COLUMNS:
        assert _column(fresh_db, table, column) == ("YES", None), (table, column)


def test_downgrade_drops_the_columns(fresh_db: str) -> None:
    command.upgrade(_alembic(fresh_db), _REVISION)
    command.downgrade(_alembic(fresh_db), _BEFORE)
    for table, column in _COLUMNS:
        assert _column(fresh_db, table, column) is None, (table, column)
    command.upgrade(_alembic(fresh_db), _REVISION)
    for table, column in _COLUMNS:
        assert _column(fresh_db, table, column) is not None, (table, column)
