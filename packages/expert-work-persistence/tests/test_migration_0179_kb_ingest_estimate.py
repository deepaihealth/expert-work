"""Migration 0179 — B-222 PR-2: estimate columns, indexes and ``knowledge_parse_snapshot``.

After upgrade: the three nullable document columns exist; the claim index covers
``estimating`` and the expiry index ``awaiting_confirmation``; the snapshot
table is tenant-isolated (ENABLE + FORCE + the ``app.tenant_id`` policy, which a
non-bypass role really is held to) and goes with its document. Downgrade turns
documents waiting for an estimate or a confirmation into ``failed`` (an older
image cannot drive them), drops it all, and upgrade runs again.

A database of its own in the container (the shared one is migrated by others).

Before running locally: export DOCKER_HOST=unix:///Users/mac/.docker/run/docker.sock
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
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
_DB = "migration_0179_test"
_REVISION = "0179_kb_ingest_estimate"
_SNAPSHOT = "knowledge_parse_snapshot"


def _before() -> str:
    """The revision 0179 sits on (read from the script, not repeated here)."""
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


def _document(conn: psycopg.Connection, tenant: UUID, status: str) -> UUID:
    row = conn.execute(
        "INSERT INTO knowledge_document (tenant_id, kb_id, filename, status) "
        "VALUES (%s, %s, %s, %s) RETURNING id",
        (tenant, uuid4(), f"{status}-{uuid4().hex[:6]}.md", status),
    ).fetchone()
    assert row is not None
    return UUID(str(row[0]))


def _snapshot(conn: psycopg.Connection, tenant: UUID, document_id: UUID) -> None:
    conn.execute(
        "INSERT INTO knowledge_parse_snapshot (tenant_id, document_id, estimate_id, format, "
        "payload, size_bytes, expires_at) VALUES (%s, %s, %s, 'text/1', %s, 3, %s)",
        (tenant, document_id, uuid4(), b"abc", datetime.now(UTC) + timedelta(days=7)),
    )


def _indexes(conn: psycopg.Connection, table: str) -> dict[str, str]:
    return {
        row[0]: row[1]
        for row in conn.execute(
            "SELECT indexname, indexdef FROM pg_indexes WHERE tablename = %s", (table,)
        )
    }


def test_columns_indexes_and_an_isolated_snapshot_table(fresh_db: str) -> None:
    command.upgrade(_alembic(fresh_db), _REVISION)
    with psycopg.connect(fresh_db, autocommit=True) as conn:
        columns = {
            row[0]: row[1]
            for row in conn.execute(
                "SELECT column_name, is_nullable FROM information_schema.columns "
                "WHERE table_name = 'knowledge_document' "
                "AND column_name IN ('ingest_estimate', 'ingest_actual', 'confirm_by')"
            )
        }
        documents = _indexes(conn, "knowledge_document")
        snapshots = _indexes(conn, _SNAPSHOT)
        flags = conn.execute(
            "SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname = %s",
            (_SNAPSHOT,),
        ).fetchone()
        policies = [
            row[0]
            for row in conn.execute(
                "SELECT polname FROM pg_policy WHERE polrelid = %s::regclass", (_SNAPSHOT,)
            )
        ]
    assert columns == {"ingest_estimate": "YES", "ingest_actual": "YES", "confirm_by": "YES"}
    assert "'estimating'" in documents["knowledge_document_recovery_idx"]
    assert "'awaiting_confirmation'" in documents["knowledge_document_confirm_by_idx"]
    assert "(expires_at)" in snapshots["knowledge_parse_snapshot_expires_idx"]
    assert flags == (True, True)
    assert policies == [f"{_SNAPSHOT}_tenant_isolation"]


def test_a_snapshot_goes_with_its_document(fresh_db: str) -> None:
    command.upgrade(_alembic(fresh_db), _REVISION)
    tenant = uuid4()
    with psycopg.connect(fresh_db, autocommit=True) as conn:
        document_id = _document(conn, tenant, "awaiting_confirmation")
        _snapshot(conn, tenant, document_id)
        with pytest.raises(errors.UniqueViolation):  # one per document
            _snapshot(conn, tenant, document_id)
        conn.execute("DELETE FROM knowledge_document WHERE id = %s", (document_id,))
        left = conn.execute("SELECT count(*) FROM knowledge_parse_snapshot").fetchone()
    assert left == (0,)


def test_a_non_bypass_role_sees_only_its_tenants_snapshots(fresh_db: str) -> None:
    command.upgrade(_alembic(fresh_db), _REVISION)
    mine, theirs = uuid4(), uuid4()
    role = f"kb_snapshot_probe_{uuid4().hex[:8]}"
    with psycopg.connect(fresh_db, autocommit=True) as conn:
        _snapshot(conn, mine, _document(conn, mine, "awaiting_confirmation"))
        their_document = _document(conn, theirs, "awaiting_confirmation")
        _snapshot(conn, theirs, their_document)
        conn.execute(f"CREATE ROLE {role} NOLOGIN")
        conn.execute(f"GRANT SELECT, INSERT ON {_SNAPSHOT} TO {role}")
        conn.execute(f"GRANT SELECT ON knowledge_document TO {role}")
    try:
        with psycopg.connect(fresh_db) as conn:
            conn.execute(f"SET LOCAL ROLE {role}")
            conn.execute("SELECT set_config('app.tenant_id', %s, true)", (str(mine),))
            seen = [
                row[0] for row in conn.execute("SELECT tenant_id FROM knowledge_parse_snapshot")
            ]
            assert seen == [mine]
            with pytest.raises(errors.InsufficientPrivilege):
                conn.execute(
                    "INSERT INTO knowledge_parse_snapshot (tenant_id, document_id, estimate_id, "
                    "format, payload, size_bytes, expires_at) "
                    "VALUES (%s, %s, %s, 'text/1', 'x', 1, now())",
                    (theirs, uuid4(), uuid4()),
                )
    finally:
        with psycopg.connect(fresh_db, autocommit=True) as conn:
            conn.execute(f"REVOKE ALL ON {_SNAPSHOT} FROM {role}")
            conn.execute(f"REVOKE ALL ON knowledge_document FROM {role}")
            conn.execute(f"DROP ROLE {role}")


def test_downgrade_fails_waiting_documents_and_upgrade_runs_again(fresh_db: str) -> None:
    cfg = _alembic(fresh_db)
    command.upgrade(cfg, _REVISION)
    tenant = uuid4()
    with psycopg.connect(fresh_db, autocommit=True) as conn:
        waiting = [_document(conn, tenant, s) for s in ("estimating", "awaiting_confirmation")]
        ready = _document(conn, tenant, "ready")
    command.downgrade(cfg, _before())
    with psycopg.connect(fresh_db, autocommit=True) as conn:
        statuses: dict[UUID, str] = {
            UUID(str(row[0])): str(row[1])
            for row in conn.execute("SELECT id, status FROM knowledge_document")
        }
        errors_written = {
            row[0]
            for row in conn.execute(
                "SELECT error FROM knowledge_document WHERE id = ANY(%s)", (waiting,)
            )
        }
        recovery = _indexes(conn, "knowledge_document")["knowledge_document_recovery_idx"]
        gone = conn.execute("SELECT to_regclass(%s) IS NULL", (_SNAPSHOT,)).fetchone()
    assert {statuses[d] for d in waiting} == {"failed"}
    assert statuses[ready] == "ready"
    assert errors_written == {"ingest estimate rolled back; re-ingest to run it"}
    assert "'estimating'" not in recovery
    assert gone == (True,)
    command.upgrade(cfg, _REVISION)
