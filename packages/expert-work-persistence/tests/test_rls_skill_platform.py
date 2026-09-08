"""Integration: platform (NULL-tenant) skill RLS + migration safety — Stream X.

Migration 0057 relaxes ``skill`` / ``skill_version`` to allow NULL-tenant
(platform) rows and swaps the strict-equality RLS policies for
``IS NOT DISTINCT FROM`` (mirroring ``encrypted_secret`` / ``mcp_connector_
catalog``). This test pins the X-1 isolation properties on a real Postgres:

* A platform skill (``tenant_id IS NULL``) created under an UNSCOPED session
  is visible to an unscoped session and INVISIBLE to a tenant-scoped session
  (the X-8 / W-8 trap).
* Two tenants still cannot see each other's skills (regression on the RLS swap
  — non-NULL ``tenant_id`` behaves identically to the old strict policy).
* The ``COALESCE(tenant_id, zero-uuid)`` unique index blocks a second platform
  skill with the same name.

A separate test proves the migration is safe: a pre-existing tenant skill row
created before 0057 survives, gets ``required_tier='free'``, and stays readable
by its tenant.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from urllib.parse import urlparse, urlunparse
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import AsyncEngine
from testcontainers.postgres import PostgresContainer

from expert_work.persistence import (
    DatabaseConfig,
    SqlSkillStore,
    create_async_engine_from_config,
    create_async_session_factory,
)
from expert_work.persistence.rls import build_rls_sessionmaker, current_tenant_id_var
from expert_work.persistence.skill.base import DuplicateSkillError
from expert_work.protocol import SkillStatus
from expert_work.protocol.tenant_config import TenantPlan

pytestmark = pytest.mark.integration

ALEMBIC_INI = Path(__file__).resolve().parent.parent / "alembic.ini"

APP_ROLE = "expert_work_app"
APP_PASSWORD = "expert_work_app_test_pw"  # test-only fixture password


def _sync_dsn(container: PostgresContainer) -> str:
    url = str(container.get_connection_url())
    return url.replace("+psycopg2", "+psycopg").replace("postgresql://", "postgresql+psycopg://", 1)


def _async_dsn(container: PostgresContainer) -> str:
    url = str(container.get_connection_url())
    return url.replace("+psycopg2", "+asyncpg").replace("postgresql://", "postgresql+asyncpg://", 1)


def _rewrite_credentials(dsn: str, user: str, password: str) -> str:
    parsed = urlparse(dsn)
    new_netloc = f"{user}:{password}@{parsed.hostname}"
    if parsed.port is not None:
        new_netloc = f"{new_netloc}:{parsed.port}"
    return urlunparse(parsed._replace(netloc=new_netloc))


def _provision_app_role(sync_dsn: str) -> None:
    admin_engine = create_engine(sync_dsn, isolation_level="AUTOCOMMIT")
    try:
        with admin_engine.connect() as conn:
            exists = conn.execute(
                text("SELECT 1 FROM pg_roles WHERE rolname = :role"),
                {"role": APP_ROLE},
            ).first()
            if exists is None:
                conn.execute(text(f"CREATE ROLE {APP_ROLE} LOGIN PASSWORD '{APP_PASSWORD}'"))
            conn.execute(text(f"GRANT USAGE ON SCHEMA public TO {APP_ROLE}"))
            conn.execute(
                text(
                    f"GRANT SELECT, INSERT, UPDATE, DELETE "
                    f"ON ALL TABLES IN SCHEMA public TO {APP_ROLE}"
                )
            )
            conn.execute(
                text(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {APP_ROLE}")
            )
    finally:
        admin_engine.dispose()


@pytest.fixture
def skill_rls(
    postgres_container: PostgresContainer,
) -> Iterator[tuple[SqlSkillStore, AsyncEngine]]:
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", _sync_dsn(postgres_container))
    command.upgrade(cfg, "head")
    _provision_app_role(_sync_dsn(postgres_container))

    app_dsn = _rewrite_credentials(_async_dsn(postgres_container), APP_ROLE, APP_PASSWORD)
    engine = create_async_engine_from_config(DatabaseConfig(dsn=app_dsn))
    sf = build_rls_sessionmaker(create_async_session_factory(engine))
    yield SqlSkillStore(sf), engine


@pytest.fixture(autouse=True)
def reset_rls() -> Iterator[None]:
    tok = current_tenant_id_var.set(None)
    try:
        yield
    finally:
        current_tenant_id_var.reset(tok)


@pytest.mark.asyncio
async def test_x1_tenant_scoped_session_cannot_see_platform_skill(
    skill_rls: tuple[SqlSkillStore, AsyncEngine],
) -> None:
    store, engine = skill_rls
    try:
        # Unscoped (platform/bypass) session inserts a NULL-tenant skill.
        current_tenant_id_var.set(None)
        name = f"plat-{uuid4().hex[:12]}"
        created = await store.create_platform_skill(
            skill_id=uuid4(), name=name, description="platform skill"
        )
        assert created.tenant_id is None

        # Unscoped session sees it (the bypass path).
        fetched = await store.get_platform_skill(skill_id=created.id)
        assert fetched is not None
        platform_rows, _ = await store.list_platform_skills()
        assert name in {s.name for s in platform_rows}

        # X-8 / W-8 trap: a TENANT-scoped session sees ZERO platform rows.
        current_tenant_id_var.set(uuid4())
        assert await store.get_platform_skill(skill_id=created.id) is None
        tenant_view, _ = await store.list_platform_skills()
        assert tenant_view == []
    finally:
        current_tenant_id_var.set(None)
        await engine.dispose()


@pytest.mark.asyncio
async def test_x1_two_tenants_cannot_see_each_others_skills(
    skill_rls: tuple[SqlSkillStore, AsyncEngine],
) -> None:
    store, engine = skill_rls
    tenant_a = uuid4()
    tenant_b = uuid4()
    try:
        current_tenant_id_var.set(tenant_a)
        skill_a = await store.create_skill(skill_id=uuid4(), tenant_id=tenant_a, name="a-skill")

        current_tenant_id_var.set(tenant_b)
        # Tenant B cannot read tenant A's skill (RLS swap regression check).
        assert await store.get_skill(skill_id=skill_a.id, tenant_id=tenant_a) is None
        b_rows, _ = await store.list_skills(tenant_id=tenant_b)
        assert b_rows == []

        # Tenant A still sees its own.
        current_tenant_id_var.set(tenant_a)
        a_rows, _ = await store.list_skills(tenant_id=tenant_a)
        assert {s.id for s in a_rows} == {skill_a.id}
    finally:
        current_tenant_id_var.set(None)
        await engine.dispose()


@pytest.mark.asyncio
async def test_x1_coalesce_unique_index_blocks_duplicate_platform_name(
    skill_rls: tuple[SqlSkillStore, AsyncEngine],
) -> None:
    store, engine = skill_rls
    try:
        current_tenant_id_var.set(None)
        name = f"dup-{uuid4().hex[:12]}"
        await store.create_platform_skill(skill_id=uuid4(), name=name)
        with pytest.raises(DuplicateSkillError):
            await store.create_platform_skill(skill_id=uuid4(), name=name)
    finally:
        current_tenant_id_var.set(None)
        await engine.dispose()


@pytest.mark.asyncio
async def test_x2_migration_safe_preexisting_tenant_skill(
    postgres_container: PostgresContainer,
) -> None:
    """A tenant skill row inserted at 0056 survives 0057 with required_tier='free'."""
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", _sync_dsn(postgres_container))

    # Migrate up to 0056 (pre-platform schema), then insert a tenant skill as
    # the superuser (RLS does not apply to the table owner).
    command.upgrade(cfg, "0056_mcp_catalog_columns")
    tenant = uuid4()
    skill_id = uuid4()
    name = f"legacy-{uuid4().hex[:12]}"
    sync_engine = create_engine(_sync_dsn(postgres_container), isolation_level="AUTOCOMMIT")
    try:
        with sync_engine.connect() as conn:
            conn.execute(
                text(
                    "INSERT INTO skill (id, tenant_id, name, status, latest_version, "
                    "description) VALUES (:id, :tid, :name, 'active', 0, 'legacy')"
                ),
                {"id": skill_id, "tid": tenant, "name": name},
            )
    finally:
        sync_engine.dispose()

    # Apply 0057.
    command.upgrade(cfg, "head")
    _provision_app_role(_sync_dsn(postgres_container))

    app_dsn = _rewrite_credentials(_async_dsn(postgres_container), APP_ROLE, APP_PASSWORD)
    engine = create_async_engine_from_config(DatabaseConfig(dsn=app_dsn))
    sf = build_rls_sessionmaker(create_async_session_factory(engine))
    store = SqlSkillStore(sf)
    try:
        current_tenant_id_var.set(tenant)
        survived = await store.get_skill(skill_id=skill_id, tenant_id=tenant)
        assert survived is not None
        assert survived.name == name
        assert survived.required_tier == TenantPlan.FREE
        assert survived.tenant_id == tenant
    finally:
        current_tenant_id_var.set(None)
        await engine.dispose()


@pytest.mark.asyncio
async def test_b23_platform_keyset_paging_walks_library_and_respects_rls(
    skill_rls: tuple[SqlSkillStore, AsyncEngine],
) -> None:
    """``list_platform_skills_keyset`` (feeds the merged ``GET /v1/skills``)
    pages the NULL-tenant library without gaps or repeats, in the same order
    as the offset listing, and stays behind the same RLS trap."""
    store, engine = skill_rls
    try:
        current_tenant_id_var.set(None)
        prefix = f"page-{uuid4().hex[:8]}"
        seeded: list[UUID] = []
        for i in range(5):
            created = await store.create_platform_skill(skill_id=uuid4(), name=f"{prefix}-{i}")
            seeded.append(created.id)
        await store.set_platform_status(skill_id=seeded[0], status=SkillStatus.ACTIVE)

        walked: list[UUID] = []
        cursor: UUID | None = None
        for _ in range(50):
            rows, cursor = await store.list_platform_skills_keyset(cursor=cursor, limit=2)
            walked.extend(s.id for s in rows)
            if cursor is None:
                break
        assert cursor is None
        assert len(walked) == len(set(walked))  # no row served twice
        assert set(seeded) <= set(walked)  # container is shared across tests: ⊆ not ==
        offset_rows, _ = await store.list_platform_skills(limit=len(walked))
        assert walked == [s.id for s in offset_rows]

        active_rows, _ = await store.list_platform_skills_keyset(
            status=SkillStatus.ACTIVE, limit=50
        )
        active_ids = {s.id for s in active_rows}
        assert seeded[0] in active_ids
        assert not (set(seeded[1:]) & active_ids)

        # X-8 / W-8 trap: a TENANT-scoped session sees ZERO platform rows.
        current_tenant_id_var.set(uuid4())
        tenant_view, tenant_cursor = await store.list_platform_skills_keyset(limit=2)
        assert tenant_view == []
        assert tenant_cursor is None
    finally:
        current_tenant_id_var.set(None)
        await engine.dispose()
