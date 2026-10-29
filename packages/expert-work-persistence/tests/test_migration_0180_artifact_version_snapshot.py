"""Migration 0180 — B-235: ``artifact_version.snapshot_key`` (nullable text) + digest CHECK.

Expand-only: existing rows keep ``snapshot_key = NULL`` (= registered before
B-235; content is read from the workspace path as before). A row that names a
snapshot must carry its size and sha256 — they are computed when the copy is
taken, never lazily. Runs in its own database inside the container.
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
_DB = "migration_0180_test"
_REVISION = "0180_artifact_version_snapshot"
# Mirrors the migration's ``down_revision``.
_BEFORE = "0179_kb_ingest_estimate"


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
            "WHERE table_name = 'artifact_version' AND column_name = 'snapshot_key'"
        ).fetchone()
    return None if row is None else (str(row[0]), str(row[1]))


def _insert_version(
    dsn: str,
    *,
    snapshot_key: str | None = None,
    size_bytes: int | None = None,
    sha256: str | None = None,
    with_snapshot_column: bool = True,
) -> str:
    version_id = uuid4()
    now = datetime.now(UTC)
    base = [version_id, uuid4(), uuid4(), uuid4(), 1, "a.md", "t", now, size_bytes, sha256]
    with psycopg.connect(dsn, autocommit=True) as conn:
        if with_snapshot_column:
            conn.execute(
                "INSERT INTO artifact_version (id, artifact_id, tenant_id, user_id, version, "
                "path_in_workspace, created_in_thread, created_at, size_bytes, sha256, "
                "snapshot_key) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                [*base, snapshot_key],
            )
        else:
            conn.execute(
                "INSERT INTO artifact_version (id, artifact_id, tenant_id, user_id, version, "
                "path_in_workspace, created_in_thread, created_at, size_bytes, sha256) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                base,
            )
    return str(version_id)


def test_revision_id_fits_alembic_version_column() -> None:
    assert len(_REVISION) <= 32


def test_upgrade_adds_a_nullable_text_column_and_leaves_old_rows_null(fresh_db: str) -> None:
    assert _column(fresh_db) is None
    old = _insert_version(fresh_db, with_snapshot_column=False)

    command.upgrade(_alembic(fresh_db), _REVISION)

    assert _column(fresh_db) == ("text", "YES")
    with psycopg.connect(fresh_db) as conn:
        row = conn.execute(
            "SELECT snapshot_key FROM artifact_version WHERE id = %s", (old,)
        ).fetchone()
    assert row == (None,)


def test_a_snapshot_row_must_carry_its_digest(fresh_db: str) -> None:
    command.upgrade(_alembic(fresh_db), _REVISION)

    _insert_version(fresh_db, snapshot_key="k1", size_bytes=3, sha256="abc")
    with pytest.raises(psycopg.errors.CheckViolation):
        _insert_version(fresh_db, snapshot_key="k2")
    with pytest.raises(psycopg.errors.CheckViolation):
        _insert_version(fresh_db, snapshot_key="k3", size_bytes=3)


def test_downgrade_drops_the_column(fresh_db: str) -> None:
    command.upgrade(_alembic(fresh_db), _REVISION)
    command.downgrade(_alembic(fresh_db), _BEFORE)

    assert _column(fresh_db) is None


_INDEX = "artifact_version_path_idx"


def _index(dsn: str) -> str | None:
    with psycopg.connect(dsn) as conn:
        row = conn.execute(
            "SELECT indexdef FROM pg_indexes WHERE tablename = 'artifact_version' "
            "AND indexname = %s",
            (_INDEX,),
        ).fetchone()
    return None if row is None else str(row[0])


def test_the_path_lookup_has_an_index_and_downgrade_drops_it(fresh_db: str) -> None:
    """留存作业删一个版本前问「这个路径还有没有别的版本在用」,按
    ``(tenant_id, user_id, path_in_workspace)`` 查 —— 没有索引就是全表扫。"""
    assert _index(fresh_db) is None

    command.upgrade(_alembic(fresh_db), _REVISION)
    definition = _index(fresh_db)
    assert definition is not None
    assert "(tenant_id, user_id, path_in_workspace)" in definition

    command.downgrade(_alembic(fresh_db), _BEFORE)
    assert _index(fresh_db) is None
