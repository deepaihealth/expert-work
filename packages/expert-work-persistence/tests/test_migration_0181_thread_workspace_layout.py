"""Migration 0181 — B-236: ``thread_meta.workspace_layout``.

Conversations that already exist at deploy keep today's view (``agent_root``: the
sandbox ``/workspace`` is the agent root); every thread created afterwards, by any
create path, gets ``conversation`` from the column default — no code path sets it.
Runs in its own database inside the container (the shared one is upgraded /
downgraded by other tests).
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
_DB = "migration_0181_test"
_REVISION = "0181_thread_workspace_layout"
# Mirrors the migration's ``down_revision``.
_BEFORE = "0180_artifact_version_snapshot"


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


def _insert_thread(dsn: str) -> str:
    thread_id = uuid4()
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO thread_meta (thread_id, tenant_id, created_by) VALUES (%s, %s, 'u')",
            (thread_id, uuid4()),
        )
    return str(thread_id)


def _layout(dsn: str, thread_id: str) -> str:
    with psycopg.connect(dsn) as conn:
        row = conn.execute(
            "SELECT workspace_layout FROM thread_meta WHERE thread_id = %s", (thread_id,)
        ).fetchone()
    assert row is not None
    return str(row[0])


def _has_column(dsn: str) -> bool:
    with psycopg.connect(dsn) as conn:
        row = conn.execute(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name = 'thread_meta' AND column_name = 'workspace_layout'"
        ).fetchone()
    return row is not None


def test_revision_id_fits_alembic_version_column() -> None:
    assert len(_REVISION) <= 32


def test_existing_threads_keep_the_agent_root_view_new_ones_get_conversation(
    fresh_db: str,
) -> None:
    before = _insert_thread(fresh_db)

    command.upgrade(_alembic(fresh_db), _REVISION)

    assert _layout(fresh_db, before) == "agent_root"
    after = _insert_thread(fresh_db)  # an insert that does not name the column
    assert _layout(fresh_db, after) == "conversation"


def test_layout_rejects_unknown_values(fresh_db: str) -> None:
    command.upgrade(_alembic(fresh_db), _REVISION)
    with (
        psycopg.connect(fresh_db, autocommit=True) as conn,
        pytest.raises(psycopg.errors.CheckViolation),
    ):
        conn.execute(
            "INSERT INTO thread_meta (thread_id, tenant_id, created_by, workspace_layout) "
            "VALUES (%s, %s, 'u', 'per-thread')",
            (uuid4(), uuid4()),
        )


def test_downgrade_drops_the_column(fresh_db: str) -> None:
    command.upgrade(_alembic(fresh_db), _REVISION)
    command.downgrade(_alembic(fresh_db), _BEFORE)
    assert not _has_column(fresh_db)
    command.upgrade(_alembic(fresh_db), _REVISION)
    assert _has_column(fresh_db)
