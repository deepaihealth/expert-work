"""Integration test for :class:`SqlTenantConfigStore` — Stream C.7."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from urllib.parse import urlparse, urlunparse
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import AsyncEngine
from testcontainers.postgres import PostgresContainer

from expert_work.persistence import (
    DatabaseConfig,
    SqlTenantConfigStore,
    create_async_engine_from_config,
    create_async_session_factory,
)
from expert_work.persistence.rls import build_rls_sessionmaker, current_tenant_id_var
from expert_work.persistence.tenant_config.base import TenantConfigAlreadyExistsError
from expert_work.protocol import TenantConfigPatch, TenantPlan

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
def tenant_config_store(
    postgres_container: PostgresContainer,
) -> Iterator[tuple[SqlTenantConfigStore, AsyncEngine]]:
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", _sync_dsn(postgres_container))
    command.upgrade(cfg, "head")
    _provision_app_role(_sync_dsn(postgres_container))

    app_dsn = _rewrite_credentials(_async_dsn(postgres_container), APP_ROLE, APP_PASSWORD)
    engine = create_async_engine_from_config(DatabaseConfig(dsn=app_dsn))
    sf = build_rls_sessionmaker(create_async_session_factory(engine))
    yield SqlTenantConfigStore(sf), engine


@pytest.fixture(autouse=True)
def reset_rls() -> Iterator[None]:
    tok = current_tenant_id_var.set(None)
    try:
        yield
    finally:
        current_tenant_id_var.reset(tok)


@pytest.mark.asyncio
async def test_first_upsert_then_get_round_trip(
    tenant_config_store: tuple[SqlTenantConfigStore, AsyncEngine],
) -> None:
    store, engine = tenant_config_store
    try:
        tenant = uuid4()
        current_tenant_id_var.set(tenant)
        created = await store.upsert(
            tenant_id=tenant,
            patch=TenantConfigPatch(
                display_name="ACME Inc",
                plan=TenantPlan.PRO,
                mcp_allowlist=["github-mcp"],
                pii_fields=["email"],
                http_tool_allowlist=["https://api.github.com/*"],
                http_tool_denylist=["internal.acme.com"],
            ),
            actor_id="admin@acme",
        )
        assert created.display_name == "ACME Inc"
        assert created.plan is TenantPlan.PRO
        assert created.mcp_allowlist == ["github-mcp"]
        assert created.pii_fields == ["email"]
        assert created.http_tool_allowlist == ["https://api.github.com/*"]
        assert created.http_tool_denylist == ["internal.acme.com"]

        fetched = await store.get(tenant_id=tenant)
        assert fetched is not None
        assert fetched.display_name == "ACME Inc"
        assert fetched.http_tool_denylist == ["internal.acme.com"]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_partial_update_preserves_unset_fields(
    tenant_config_store: tuple[SqlTenantConfigStore, AsyncEngine],
) -> None:
    store, engine = tenant_config_store
    try:
        tenant = uuid4()
        current_tenant_id_var.set(tenant)
        await store.upsert(
            tenant_id=tenant,
            patch=TenantConfigPatch(
                display_name="initial",
                mcp_allowlist=["github-mcp"],
                pii_fields=["ssn"],
                http_tool_denylist=["blocked.example.com"],
            ),
            actor_id="admin",
        )
        await store.upsert(
            tenant_id=tenant,
            patch=TenantConfigPatch(plan=TenantPlan.ENTERPRISE),
            actor_id="admin",
        )
        final = await store.get(tenant_id=tenant)
        assert final is not None
        assert final.plan is TenantPlan.ENTERPRISE
        assert final.mcp_allowlist == ["github-mcp"]
        assert final.pii_fields == ["ssn"]
        assert final.display_name == "initial"
        # A plan-only patch leaves the denylist untouched.
        assert final.http_tool_denylist == ["blocked.example.com"]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_rls_blocks_cross_tenant_read(
    tenant_config_store: tuple[SqlTenantConfigStore, AsyncEngine],
) -> None:
    """A row owned by tenant A is invisible when the session is scoped to B."""
    store, engine = tenant_config_store
    try:
        tenant_a, tenant_b = uuid4(), uuid4()
        current_tenant_id_var.set(tenant_a)
        await store.upsert(
            tenant_id=tenant_a,
            patch=TenantConfigPatch(display_name="A"),
            actor_id="admin",
        )
        # Verify same-tenant read works.
        current_tenant_id_var.set(tenant_a)
        assert await store.get(tenant_id=tenant_a) is not None

        # Cross-tenant read: scope to B, try to look up A's row.
        current_tenant_id_var.set(tenant_b)
        assert await store.get(tenant_id=tenant_a) is None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_create_provisions_new_tenant_with_defaults(
    tenant_config_store: tuple[SqlTenantConfigStore, AsyncEngine],
) -> None:
    """``create`` writes the first row; every unset field takes its default — Stream P."""
    store, engine = tenant_config_store
    try:
        tenant = uuid4()
        current_tenant_id_var.set(tenant)
        created = await store.create(
            tenant_id=tenant,
            display_name="Fresh Tenant",
            actor_id="bootstrap",
        )
        assert created.tenant_id == tenant
        assert created.display_name == "Fresh Tenant"
        assert created.plan is TenantPlan.FREE
        assert created.model_credentials_ref == {}
        assert created.credentials_mode == "platform"
        assert (await store.get(tenant_id=tenant)) is not None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_create_rejects_duplicate_tenant(
    tenant_config_store: tuple[SqlTenantConfigStore, AsyncEngine],
) -> None:
    """A second ``create`` for the same tenant raises, not silently overwrites — Stream P."""
    store, engine = tenant_config_store
    try:
        tenant = uuid4()
        current_tenant_id_var.set(tenant)
        await store.create(tenant_id=tenant, display_name="First", actor_id="a")
        with pytest.raises(TenantConfigAlreadyExistsError) as exc:
            await store.create(tenant_id=tenant, display_name="Second", actor_id="b")
        assert exc.value.tenant_id == tenant
        fetched = await store.get(tenant_id=tenant)
        assert fetched is not None
        assert fetched.display_name == "First"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_allow_custom_mcp_servers_default_and_patch(
    tenant_config_store: tuple[SqlTenantConfigStore, AsyncEngine],
) -> None:
    """``allow_custom_mcp_servers`` defaults True and round-trips False — Stream W."""
    store, engine = tenant_config_store
    try:
        tenant = uuid4()
        current_tenant_id_var.set(tenant)
        created = await store.create(tenant_id=tenant, display_name="Acme", actor_id="bootstrap")
        assert created.allow_custom_mcp_servers is True

        updated = await store.upsert(
            tenant_id=tenant,
            patch=TenantConfigPatch(allow_custom_mcp_servers=False),
            actor_id="ops",
        )
        assert updated.allow_custom_mcp_servers is False
        fetched = await store.get(tenant_id=tenant)
        assert fetched is not None and fetched.allow_custom_mcp_servers is False
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_skill_evolution_judge_sample_pct_default_and_patch(
    tenant_config_store: tuple[SqlTenantConfigStore, AsyncEngine],
) -> None:
    """SE-16 (SE-A45) — implicit judge sample rate defaults 5 and round-trips."""
    store, engine = tenant_config_store
    try:
        tenant = uuid4()
        current_tenant_id_var.set(tenant)
        created = await store.create(tenant_id=tenant, display_name="Acme", actor_id="bootstrap")
        assert created.skill_evolution_judge_sample_pct == 5

        updated = await store.upsert(
            tenant_id=tenant,
            patch=TenantConfigPatch(skill_evolution_judge_sample_pct=50),
            actor_id="ops",
        )
        assert updated.skill_evolution_judge_sample_pct == 50
        fetched = await store.get(tenant_id=tenant)
        assert fetched is not None and fetched.skill_evolution_judge_sample_pct == 50
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_memory_predictive_review_enabled_default_and_patch(
    tenant_config_store: tuple[SqlTenantConfigStore, AsyncEngine],
) -> None:
    """P5b-2b ⑦ — predictive review opt-in defaults False and round-trips True.

    Closes the activation-gap Critical finding on 7a8e9155 (P5b-2b T4):
    proves the full ``TenantConfigPatch`` → SQL store → ``TenantConfigRecord``
    path actually turns MemoryConsolidator's SUB-PASS 3 on for a tenant,
    not just the Pydantic model in isolation.
    """
    store, engine = tenant_config_store
    try:
        tenant = uuid4()
        current_tenant_id_var.set(tenant)
        created = await store.create(tenant_id=tenant, display_name="Acme", actor_id="bootstrap")
        assert created.memory_predictive_review_enabled is False

        updated = await store.upsert(
            tenant_id=tenant,
            patch=TenantConfigPatch(memory_predictive_review_enabled=True),
            actor_id="ops",
        )
        assert updated.memory_predictive_review_enabled is True
        fetched = await store.get(tenant_id=tenant)
        assert fetched is not None and fetched.memory_predictive_review_enabled is True
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# BUG-1(2026-08-24)mcp_allowlist 原子 add/remove(语义见 base.py;in-memory
# 对应用例在 test_in_memory_tenant_config_store.py,两后端行为必须同义)。


@pytest.mark.asyncio
async def test_add_remove_mcp_allowlist_name_atomic_semantics(
    tenant_config_store: tuple[SqlTenantConfigStore, AsyncEngine],
) -> None:
    store, engine = tenant_config_store
    try:
        tenant = uuid4()
        current_tenant_id_var.set(tenant)
        await store.upsert(
            tenant_id=tenant,
            patch=TenantConfigPatch(display_name="ACME"),
            actor_id="admin",
        )

        record, changed = await store.add_mcp_allowlist_name(
            tenant_id=tenant, name="deep", actor_id="op"
        )
        assert (changed, record.mcp_allowlist) == (True, ["deep"])

        # 后一个 add 不得抹掉先前的名字(丢失更新回归)。
        record, changed = await store.add_mcp_allowlist_name(
            tenant_id=tenant, name="amap", actor_id="op"
        )
        assert (changed, record.mcp_allowlist) == (True, ["deep", "amap"])
        assert record.updated_by == "op"

        # 幂等:重复 add 不变。
        record, changed = await store.add_mcp_allowlist_name(
            tenant_id=tenant, name="amap", actor_id="op2"
        )
        assert (changed, record.mcp_allowlist) == (False, ["deep", "amap"])

        # remove + 幂等。
        record, changed = await store.remove_mcp_allowlist_name(
            tenant_id=tenant, name="deep", actor_id="op"
        )
        assert (changed, record.mcp_allowlist) == (True, ["amap"])
        record, changed = await store.remove_mcp_allowlist_name(
            tenant_id=tenant, name="deep", actor_id="op"
        )
        assert (changed, record.mcp_allowlist) == (False, ["amap"])

        # 落库为准。
        fetched = await store.get(tenant_id=tenant)
        assert fetched is not None and fetched.mcp_allowlist == ["amap"]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_add_mcp_allowlist_missing_tenant_raises(
    tenant_config_store: tuple[SqlTenantConfigStore, AsyncEngine],
) -> None:
    from expert_work.persistence.tenant_config.base import TenantConfigNotFoundError

    store, engine = tenant_config_store
    try:
        tenant = uuid4()
        current_tenant_id_var.set(tenant)
        with pytest.raises(TenantConfigNotFoundError):
            await store.add_mcp_allowlist_name(tenant_id=tenant, name="x", actor_id="op")
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_add_blocks_on_row_lock_then_merges(
    tenant_config_store: tuple[SqlTenantConfigStore, AsyncEngine],
) -> None:
    """确定性行锁证明(终审第二轮:gather 版是重言式,删 FOR UPDATE 照样绿)。

    旁路会话先 FOR UPDATE 持锁并写入 "deep"(不提交),此时 store 的 add
    必须阻塞 —— 没有 FOR UPDATE 它会立刻读到旧快照写回,wait_for 就不会
    超时,第一道断言先红;提交放锁后 add 继续,合并结果两个名字都在。"""
    import asyncio

    from sqlalchemy import select

    from expert_work.persistence.models import TenantConfigRow

    store, engine = tenant_config_store
    try:
        tenant = uuid4()
        current_tenant_id_var.set(tenant)
        await store.upsert(
            tenant_id=tenant,
            patch=TenantConfigPatch(display_name="ACME"),
            actor_id="admin",
        )
        sf = build_rls_sessionmaker(create_async_session_factory(engine))
        async with sf() as blocker:
            row = (
                await blocker.execute(
                    select(TenantConfigRow)
                    .where(TenantConfigRow.tenant_id == tenant)
                    .with_for_update()
                )
            ).scalar_one()
            row.mcp_allowlist = ["deep"]
            await blocker.flush()  # 持锁未提交
            task = asyncio.create_task(
                store.add_mcp_allowlist_name(tenant_id=tenant, name="amap", actor_id="b")
            )
            with pytest.raises(TimeoutError):
                # 没有行锁时 add 不会阻塞 → 这里先红(杀掉删 FOR UPDATE 的变异)。
                await asyncio.wait_for(asyncio.shield(task), timeout=1.0)
            await blocker.commit()
            _, changed = await task
            assert changed is True
        fetched = await store.get(tenant_id=tenant)
        assert fetched is not None
        assert sorted(fetched.mcp_allowlist) == ["amap", "deep"]
    finally:
        await engine.dispose()
