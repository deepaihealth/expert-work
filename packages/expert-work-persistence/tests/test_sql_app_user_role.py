"""Integration: the ``app_user`` role exists with the right attributes/grants.

RLS enforcement Task 1 (Phase 0): pins that migration 0121 provisions a
non-superuser, non-BYPASSRLS, NOLOGIN role with DML on tenant tables and
membership in the pre-existing ``audit_reader``/``audit_writer`` BYPASSRLS
roles (0005/0008). The role is created but UNUSED here — Task 6 wires
``SET LOCAL ROLE app_user`` into the runtime session path.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Connection, create_engine, text
from testcontainers.postgres import PostgresContainer

from expert_work.persistence.embedding import EMBEDDING_DIM

pytestmark = pytest.mark.integration
ALEMBIC_INI = Path(__file__).resolve().parent.parent / "alembic.ini"


def _sync_dsn(c: PostgresContainer) -> str:
    url = str(c.get_connection_url())
    return url.replace("+psycopg2", "+psycopg").replace("postgresql://", "postgresql+psycopg://", 1)


def _seed_retired_and_live_same_text(conn: Connection) -> None:
    """A retired memory row and the live row with the same text: allowed at head (since 0127
    the dedup index skips rows that are no longer current) and what memory tests leave behind."""
    tenant_id, user_id = uuid4(), uuid4()
    for invalid_at in (datetime.now(UTC), None):
        conn.execute(
            text(
                "INSERT INTO memory_item (tenant_id, user_id, kind, content, content_hash, "
                "embedding, invalid_at) VALUES (:t, :u, 'fact', 'probe', repeat('a', 64), "
                "array_fill(0::real, ARRAY[CAST(:dim AS int)])::vector, "
                "CAST(:invalid_at AS timestamptz))"
            ),
            {"t": tenant_id, "u": user_id, "dim": EMBEDDING_DIM, "invalid_at": invalid_at},
        )


def _delete_memory_rows_not_current(conn: Connection) -> None:
    """0127's downgrade puts back the older dedup index, which counts rows that are no longer
    current: mark those deleted so leftovers of other tests cannot block the rewind."""
    conn.execute(
        text(
            "UPDATE memory_item SET deleted_at = now() WHERE deleted_at IS NULL "
            "AND (invalid_at IS NOT NULL OR expired_at IS NOT NULL)"
        )
    )


def test_app_user_role_provisioned(postgres_container: PostgresContainer) -> None:
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", _sync_dsn(postgres_container))
    command.upgrade(cfg, "head")
    engine = create_engine(_sync_dsn(postgres_container), isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as conn:
            attrs = conn.execute(
                text(
                    "SELECT rolsuper, rolbypassrls, rolcanlogin "
                    "FROM pg_roles WHERE rolname='app_user'"
                )
            ).one_or_none()
            assert attrs is not None, "app_user role missing"
            assert attrs == (False, False, False)  # NOSUPERUSER, NOBYPASSRLS, NOLOGIN
            # DML granted on a representative tenant table.
            has_select = conn.execute(
                text("SELECT has_table_privilege('app_user', 'tenant_config', 'SELECT')")
            ).scalar()
            assert has_select is True
            # Member of audit_reader (so audit paths' SET ROLE works from app_user).
            is_member = conn.execute(
                text("SELECT pg_has_role('app_user', 'audit_reader', 'MEMBER')")
            ).scalar()
            assert is_member is True
    finally:
        engine.dispose()


def test_app_user_role_downgrade_drops_role(postgres_container: PostgresContainer) -> None:
    """Downgrading past 0121 revokes grants first, then drops the role cleanly.

    The rewind runs on the shared session database, so it meets whatever earlier tests wrote;
    a retired row next to the live row with the same text is seeded here so the outcome does
    not depend on which tests ran first (CI run 43911461697)."""
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", _sync_dsn(postgres_container))
    command.upgrade(cfg, "head")
    engine = create_engine(_sync_dsn(postgres_container), isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as conn:
            _seed_retired_and_live_same_text(conn)
            _delete_memory_rows_not_current(conn)
        command.downgrade(cfg, "0120_supporting_files_cap")
        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT 1 FROM pg_roles WHERE rolname='app_user'")
            ).one_or_none()
            assert row is None, "app_user role still present after downgrade"
    finally:
        command.upgrade(cfg, "head")  # restore shared session-scoped container
        engine.dispose()
