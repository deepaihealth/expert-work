"""迁移 0130 —— agent_trigger user 维度唯一 + 投递路由列。"""

from __future__ import annotations

from pathlib import Path
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.exc import IntegrityError
from testcontainers.postgres import PostgresContainer

ALEMBIC_INI = Path(__file__).resolve().parent.parent / "alembic.ini"
_MIGRATION = "0130_trigger_user_scope"


def _sync_dsn(url: str) -> str:
    return url.replace("postgresql+psycopg2://", "postgresql+psycopg://")


@pytest.mark.integration
def test_migration_0130_schema() -> None:
    with PostgresContainer("ghcr.io/deepaihealth/mirror/pgvector:pg16") as container:
        dsn = _sync_dsn(str(container.get_connection_url()))
        cfg = Config(str(ALEMBIC_INI))
        cfg.set_main_option("sqlalchemy.url", dsn)
        command.upgrade(cfg, _MIGRATION)

        engine = sa.create_engine(dsn)
        with engine.connect() as conn:
            cols = {
                r[0]
                for r in conn.execute(
                    sa.text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_name = 'agent_trigger'"
                    )
                )
            }
            assert "originating_thread_id" in cols
            assert "context_mode" in cols

            indexes = {
                r[0]
                for r in conn.execute(
                    sa.text("SELECT indexname FROM pg_indexes WHERE tablename = 'agent_trigger'")
                )
            }
            assert "ix_agent_trigger_user_name_uniq" in indexes
            assert "ix_agent_trigger_null_user_name_uniq" in indexes

            constraints = {
                r[0]
                for r in conn.execute(
                    sa.text(
                        "SELECT conname FROM pg_constraint "
                        "WHERE conrelid = 'agent_trigger'::regclass"
                    )
                )
            }
            assert "agent_trigger_name_uniq" not in constraints  # 旧唯一约束已删
        engine.dispose()


def _insert_trigger(
    engine: sa.Engine,
    *,
    tenant_id: UUID,
    agent_name: str,
    name: str,
    user_id: UUID | None,
) -> None:
    """Raw-SQL insert of one ``agent_trigger`` row in its own transaction.

    Each call opens a fresh connection/transaction via ``engine.begin()`` —
    committed on success, rolled back on failure — so a caller can wrap an
    expected-failure call in ``pytest.raises`` without the aborted-transaction
    state from that failure blocking later inserts.
    """
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO agent_trigger ("
                "    id, tenant_id, user_id, agent_name, agent_version, name, kind,"
                "    config, enabled, source, context_mode, created_at, updated_at"
                ") VALUES ("
                "    :id, :tenant_id, :user_id, :agent_name, '1.0.0', :name, 'cron',"
                "    '{}'::jsonb, true, 'api', 'fresh_thread_per_run', now(), now()"
                ")"
            ),
            {
                "id": uuid4(),
                "tenant_id": tenant_id,
                "user_id": user_id,
                "agent_name": agent_name,
                "name": name,
            },
        )


@pytest.mark.integration
def test_migration_0130_partial_unique_indexes_enforce_scope() -> None:
    """The two partial unique indexes must actually ENFORCE their scoped
    uniqueness, not just exist by name (``test_migration_0130_schema`` only
    checks names)."""
    with PostgresContainer("ghcr.io/deepaihealth/mirror/pgvector:pg16") as container:
        dsn = _sync_dsn(str(container.get_connection_url()))
        cfg = Config(str(ALEMBIC_INI))
        cfg.set_main_option("sqlalchemy.url", dsn)
        command.upgrade(cfg, _MIGRATION)

        engine = sa.create_engine(dsn)
        try:
            # Same (tenant_id, agent_name, name), different user_id -> both
            # succeed: different users may reuse the same task name.
            tenant_a = uuid4()
            _insert_trigger(
                engine, tenant_id=tenant_a, agent_name="digest", name="daily", user_id=uuid4()
            )
            _insert_trigger(
                engine, tenant_id=tenant_a, agent_name="digest", name="daily", user_id=uuid4()
            )

            # Same (tenant_id, agent_name, name), user_id IS NULL both times ->
            # ix_agent_trigger_null_user_name_uniq rejects the second insert.
            tenant_b = uuid4()
            _insert_trigger(
                engine, tenant_id=tenant_b, agent_name="digest", name="daily", user_id=None
            )
            with pytest.raises(IntegrityError):
                _insert_trigger(
                    engine, tenant_id=tenant_b, agent_name="digest", name="daily", user_id=None
                )

            # Identical (tenant_id, agent_name, user_id, name) with the SAME
            # non-null user_id -> ix_agent_trigger_user_name_uniq rejects the
            # second insert.
            tenant_c = uuid4()
            user_c = uuid4()
            _insert_trigger(
                engine, tenant_id=tenant_c, agent_name="digest", name="daily", user_id=user_c
            )
            with pytest.raises(IntegrityError):
                _insert_trigger(
                    engine, tenant_id=tenant_c, agent_name="digest", name="daily", user_id=user_c
                )
        finally:
            engine.dispose()
