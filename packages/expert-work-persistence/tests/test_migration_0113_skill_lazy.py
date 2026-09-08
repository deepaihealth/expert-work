"""Migration 0113 — ``skill_version.lazy_load`` progressive-disclosure default.

RT-ADR-11 (STREAM-RT-DESIGN §9). Verifies the two data properties the
migration must hold when it runs on a populated database:

  1. curated **platform** skills (``skill.tenant_id IS NULL``) that were eager
     are retro-lazied;
  2. **tenant**-owned eager skills are left untouched (U-15 no-regression).

Uses a dedicated throw-away container so the revision can be rolled to just
before this migration, seeded, then advanced — the session-scoped
``postgres_container`` fixture is shared and always sits at ``head``.
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text

pytestmark = pytest.mark.integration

ALEMBIC_INI = Path(__file__).resolve().parent.parent / "alembic.ini"

_PRE = "0112_webhook_payload_format"
_MIGRATION = "0113_skill_lazy_load_default"


def _sync_dsn(url: str) -> str:
    return url.replace("+psycopg2", "+psycopg").replace("postgresql://", "postgresql+psycopg://", 1)


def _seed_eager_skill(conn: object, *, tenant_id: object) -> object:
    """Insert one eager (lazy_load=false) skill + v1, return the skill id."""
    skill_id = uuid4()
    conn.execute(  # type: ignore[attr-defined]
        text("INSERT INTO skill (id, name, tenant_id) VALUES (:id, :name, :tid)"),
        {"id": skill_id, "name": f"s-{skill_id.hex[:8]}", "tid": tenant_id},
    )
    conn.execute(  # type: ignore[attr-defined]
        text(
            "INSERT INTO skill_version (id, skill_id, version, prompt_fragment, lazy_load) "
            "VALUES (:id, :sid, 1, 'body', false)"
        ),
        {"id": uuid4(), "sid": skill_id},
    )
    return skill_id


def test_migration_0113_retro_lazies_only_curated_skills() -> None:
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("ghcr.io/deepaihealth/mirror/pgvector:pg16") as container:
        dsn = _sync_dsn(str(container.get_connection_url()))
        cfg = Config(str(ALEMBIC_INI))
        cfg.set_main_option("sqlalchemy.url", dsn)

        # Roll to just before this migration and seed two EAGER skills.
        command.upgrade(cfg, _PRE)
        engine = create_engine(dsn)
        try:
            with engine.begin() as conn:
                platform_id = _seed_eager_skill(conn, tenant_id=None)
                tenant_id = _seed_eager_skill(conn, tenant_id=uuid4())

            command.upgrade(cfg, _MIGRATION)

            with engine.connect() as conn:
                platform_lazy = conn.execute(
                    text("SELECT lazy_load FROM skill_version WHERE skill_id = :s"),
                    {"s": platform_id},
                ).scalar_one()
                tenant_lazy = conn.execute(
                    text("SELECT lazy_load FROM skill_version WHERE skill_id = :s"),
                    {"s": tenant_id},
                ).scalar_one()
        finally:
            engine.dispose()

    # Curated platform skill flipped to lazy; tenant skill left eager.
    assert platform_lazy is True
    assert tenant_lazy is False
