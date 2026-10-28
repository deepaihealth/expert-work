"""Migration 0177 — B-175 P2-PR4: the ``knowledge_retrieval_log`` table.

After upgrade: the table is tenant-isolated like every knowledge table (RLS
ENABLE + FORCE + the ``app.tenant_id`` policy, which a non-bypass role really
is held to), the retention-cleanup role may read and delete it, the three
indexes exist, and the CHECKs refuse an unknown source and a query over 2,000
characters. Downgrade drops it all and upgrade runs again.

A database of its own in the container (the shared one is migrated by others).

Before running locally: export DOCKER_HOST=unix:///Users/mac/.docker/run/docker.sock
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from uuid import UUID, uuid4

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from psycopg import errors
from testcontainers.postgres import PostgresContainer

pytestmark = pytest.mark.integration

ALEMBIC_INI = Path(__file__).resolve().parent.parent / "alembic.ini"
_DB = "migration_0177_test"
_REVISION = "0177_kb_retrieval_log"
_TABLE = "knowledge_retrieval_log"
_INDEXES = {
    "knowledge_retrieval_log_tenant_time_idx",
    "knowledge_retrieval_log_created_idx",
    "knowledge_retrieval_log_kb_ids_idx",
    "knowledge_retrieval_log_run_idx",
    "knowledge_retrieval_log_thread_idx",
}


def _before() -> str:
    """The revision 0177 sits on (read from the script, not repeated here)."""
    down = ScriptDirectory.from_config(Config(str(ALEMBIC_INI))).get_revision(_REVISION)
    assert down is not None
    assert isinstance(down.down_revision, str)
    return down.down_revision


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
        conn.execute(f'DROP DATABASE IF EXISTS "{_DB}" WITH (FORCE)')
        conn.execute(f'CREATE DATABASE "{_DB}"')
    dsn = _sync_dsn(postgres_container, _DB)
    command.upgrade(_alembic(dsn), _before())
    try:
        yield dsn
    finally:
        with psycopg.connect(admin, autocommit=True) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{_DB}" WITH (FORCE)')


def _insert(
    conn: psycopg.Connection, tenant: UUID, *, source: str = "agent", query: str = "q"
) -> None:
    conn.execute(
        "INSERT INTO knowledge_retrieval_log (tenant_id, kb_ids, source, query, status, "
        "result_count, latency_ms) VALUES (%s, %s, %s, %s, 'ok', 0, 5)",
        (tenant, [uuid4()], source, query),
    )


def _table_exists(dsn: str) -> bool:
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute("SELECT to_regclass(%s) IS NOT NULL", (_TABLE,)).fetchone()
    return bool(row and row[0])


def test_the_table_is_tenant_isolated_indexed_and_swept(fresh_db: str) -> None:
    assert not _table_exists(fresh_db)
    command.upgrade(_alembic(fresh_db), "head")
    with psycopg.connect(fresh_db, autocommit=True) as conn:
        flags = conn.execute(
            "SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname = %s",
            (_TABLE,),
        ).fetchone()
        policies = [
            row[0]
            for row in conn.execute(
                "SELECT polname FROM pg_policy WHERE polrelid = %s::regclass", (_TABLE,)
            )
        ]
        grants = conn.execute(
            "SELECT has_table_privilege('retention_cleanup_worker', %s, 'SELECT'), "
            "has_table_privilege('retention_cleanup_worker', %s, 'DELETE')",
            (_TABLE, _TABLE),
        ).fetchone()
        indexes = {
            row[0]: row[1]
            for row in conn.execute(
                "SELECT indexname, indexdef FROM pg_indexes WHERE tablename = %s", (_TABLE,)
            )
        }
    assert flags == (True, True)
    assert policies == [f"{_TABLE}_tenant_isolation"]
    assert grants == (True, True)
    assert _INDEXES <= set(indexes)
    assert "USING gin (kb_ids)" in indexes["knowledge_retrieval_log_kb_ids_idx"]
    assert "WHERE (run_id IS NOT NULL)" in indexes["knowledge_retrieval_log_run_idx"]
    # Conversation purge deletes by thread; retention by age alone.
    assert "WHERE (thread_id IS NOT NULL)" in indexes["knowledge_retrieval_log_thread_idx"]
    assert "(created_at)" in indexes["knowledge_retrieval_log_created_idx"]


def test_a_non_bypass_role_sees_and_writes_only_its_tenant(fresh_db: str) -> None:
    command.upgrade(_alembic(fresh_db), "head")
    mine, theirs = uuid4(), uuid4()
    role = f"kb_log_probe_{uuid4().hex[:8]}"
    with psycopg.connect(fresh_db, autocommit=True) as conn:
        _insert(conn, mine)
        _insert(conn, theirs)
        conn.execute(f"CREATE ROLE {role} NOLOGIN")
        conn.execute(f"GRANT SELECT, INSERT ON {_TABLE} TO {role}")
    try:
        with psycopg.connect(fresh_db) as conn:
            conn.execute(f"SET LOCAL ROLE {role}")
            conn.execute("SELECT set_config('app.tenant_id', %s, true)", (str(mine),))
            seen = [row[0] for row in conn.execute("SELECT tenant_id FROM knowledge_retrieval_log")]
            assert seen == [mine]
            with pytest.raises(errors.InsufficientPrivilege):
                _insert(conn, theirs)
    finally:
        with psycopg.connect(fresh_db, autocommit=True) as conn:
            conn.execute(f"REVOKE ALL ON {_TABLE} FROM {role}")
            conn.execute(f"DROP ROLE {role}")


def test_checks_refuse_an_unknown_source_and_an_overlong_query(fresh_db: str) -> None:
    command.upgrade(_alembic(fresh_db), "head")
    with psycopg.connect(fresh_db, autocommit=True) as conn:
        for source in ("agent", "console_test", "open_api"):
            _insert(conn, uuid4(), source=source)
        _insert(conn, uuid4(), query="x" * 2000)
        with pytest.raises(errors.CheckViolation):
            _insert(conn, uuid4(), source="webhook")
        with pytest.raises(errors.CheckViolation):
            _insert(conn, uuid4(), query="x" * 2001)


def test_downgrade_drops_the_table_and_upgrade_runs_again(fresh_db: str) -> None:
    cfg = _alembic(fresh_db)
    command.upgrade(cfg, "head")
    with psycopg.connect(fresh_db, autocommit=True) as conn:
        _insert(conn, uuid4())
    command.downgrade(cfg, _before())
    assert not _table_exists(fresh_db)
    command.upgrade(cfg, "head")
    assert _table_exists(fresh_db)
