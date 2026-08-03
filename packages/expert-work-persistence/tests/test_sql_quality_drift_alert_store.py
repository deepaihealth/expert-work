"""Integration test for :class:`SqlQualityDriftAlertStore` — RT-5 (RT-ADR-24).

Exercises the 0118_quality_drift_alert migration + the store (insert /
latest_alert_at / list / RLS isolation) against a real Postgres.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse, urlunparse
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text, update
from sqlalchemy.ext.asyncio import AsyncEngine
from testcontainers.postgres import PostgresContainer

from expert_work.persistence import (
    DatabaseConfig,
    SqlQualityDriftAlertStore,
    create_async_engine_from_config,
    create_async_session_factory,
)
from expert_work.persistence.models import QualityDriftAlertRow
from expert_work.persistence.rls import build_rls_sessionmaker, current_tenant_id_var
from expert_work.protocol import QualityDriftAlertRecord

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
def alert_store(
    postgres_container: PostgresContainer,
) -> Iterator[tuple[SqlQualityDriftAlertStore, AsyncEngine]]:
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", _sync_dsn(postgres_container))
    command.upgrade(cfg, "head")
    _provision_app_role(_sync_dsn(postgres_container))

    app_dsn = _rewrite_credentials(_async_dsn(postgres_container), APP_ROLE, APP_PASSWORD)
    engine = create_async_engine_from_config(DatabaseConfig(dsn=app_dsn))
    sf = build_rls_sessionmaker(create_async_session_factory(engine))
    yield SqlQualityDriftAlertStore(sf), engine


@pytest.fixture(autouse=True)
def reset_rls() -> Iterator[None]:
    tok = current_tenant_id_var.set(None)
    try:
        yield
    finally:
        current_tenant_id_var.reset(tok)


def _alert(*, tenant: UUID, agent: str = "support-bot") -> QualityDriftAlertRecord:
    return QualityDriftAlertRecord(
        tenant_id=tenant,
        agent_name=agent,
        recent_mean=3.1,
        baseline_mean=4.2,
        drift_pct=0.26,
        recent_count=12,
        baseline_count=80,
    )


@pytest.mark.asyncio
async def test_insert_and_latest_and_list(
    alert_store: tuple[SqlQualityDriftAlertStore, AsyncEngine],
) -> None:
    store, engine = alert_store
    try:
        tenant = uuid4()
        current_tenant_id_var.set(tenant)
        assert await store.latest_alert_at(tenant_id=tenant, agent_name="support-bot") is None
        first = await store.insert(_alert(tenant=tenant))
        assert first.id is not None
        assert first.detected_at is not None
        second = await store.insert(_alert(tenant=tenant))
        latest = await store.latest_alert_at(tenant_id=tenant, agent_name="support-bot")
        assert latest == second.detected_at

        rows = await store.list_alerts(tenant_id=tenant)
        assert len(rows) == 2
        assert rows[0].detected_at is not None
        assert rows[1].detected_at is not None
        assert rows[0].detected_at >= rows[1].detected_at  # newest first
        assert rows[0].drift_pct == 0.26
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_list_alerts_all_tenants_spans_tenants(
    alert_store: tuple[SqlQualityDriftAlertStore, AsyncEngine],
    postgres_container: PostgresContainer,
) -> None:
    """W4 — the aggregate reader spans tenants. Runs on the container's
    superuser connection (production posture: the app's superuser connection
    with the RLS GUC skipped via ``bypass_rls_session``); the app-role RLS
    store below proves per-tenant reads stay scoped."""
    store, engine = alert_store
    su_engine = create_async_engine_from_config(DatabaseConfig(dsn=_async_dsn(postgres_container)))
    su_store = SqlQualityDriftAlertStore(create_async_session_factory(su_engine))
    try:
        tenant_a, tenant_b = uuid4(), uuid4()
        current_tenant_id_var.set(tenant_a)
        await store.insert(_alert(tenant=tenant_a, agent="agg-a"))
        current_tenant_id_var.set(tenant_b)
        await store.insert(_alert(tenant=tenant_b, agent="agg-b"))

        rows = await su_store.list_alerts_all_tenants()
        mine = {(r.tenant_id, r.agent_name) for r in rows if r.tenant_id in {tenant_a, tenant_b}}
        assert mine == {(tenant_a, "agg-a"), (tenant_b, "agg-b")}
        # agent_name narrows the aggregate the same way as the sibling.
        only_b = await su_store.list_alerts_all_tenants(agent_name="agg-b")
        assert {r.tenant_id for r in only_b if r.tenant_id in {tenant_a, tenant_b}} == {tenant_b}
    finally:
        await su_engine.dispose()
        await engine.dispose()


@pytest.mark.asyncio
async def test_list_alerts_all_tenants_id_tiebreak_on_equal_detected_at(
    alert_store: tuple[SqlQualityDriftAlertStore, AsyncEngine],
    postgres_container: PostgresContainer,
) -> None:
    """W4 review C-3 — ``detected_at`` ties order by ``id`` ASC. The two
    rows' ``detected_at`` are equalized via raw UPDATEs issued higher-id
    first, so the live heap tuples end up in *descending* id order — a
    dropped tiebreak degrades to heap-scan order and the exact-order
    assertion goes red."""
    store, engine = alert_store
    su_engine = create_async_engine_from_config(DatabaseConfig(dsn=_async_dsn(postgres_container)))
    su_store = SqlQualityDriftAlertStore(create_async_session_factory(su_engine))
    try:
        tenant_a, tenant_b = uuid4(), uuid4()
        agent = f"tie-{uuid4().hex[:8]}"
        current_tenant_id_var.set(tenant_a)
        first = await store.insert(_alert(tenant=tenant_a, agent=agent))
        current_tenant_id_var.set(tenant_b)
        second = await store.insert(_alert(tenant=tenant_b, agent=agent))
        assert first.id is not None and second.id is not None and first.id < second.id

        ts = datetime(2035, 1, 1, 12, 0, tzinfo=UTC)
        async with su_engine.begin() as conn:
            for row_id in (second.id, first.id):  # hi first → live tuples id-descending
                await conn.execute(
                    update(QualityDriftAlertRow)
                    .where(QualityDriftAlertRow.id == row_id)
                    .values(detected_at=ts)
                )

        rows = await su_store.list_alerts_all_tenants(agent_name=agent)
        assert [r.id for r in rows] == [first.id, second.id]
    finally:
        await su_engine.dispose()
        await engine.dispose()


@pytest.mark.asyncio
async def test_list_alerts_id_tiebreak_on_equal_detected_at(
    alert_store: tuple[SqlQualityDriftAlertStore, AsyncEngine],
    postgres_container: PostgresContainer,
) -> None:
    """W4 review #5 — the per-tenant sibling (``list_alerts``) shares the
    ``id`` tiebreak contract. Its query is served off the
    ``(tenant_id, agent_name, detected_at)`` index scanned backward, whose
    duplicate-key order is heap-TID *descending* — so the UPDATEs equalizing
    ``detected_at`` are issued lowest-id first (ascending TIDs track ids):
    without the tiebreak the backward scan yields ids descending and the
    ascending assertion goes red."""
    store, engine = alert_store
    su_engine = create_async_engine_from_config(DatabaseConfig(dsn=_async_dsn(postgres_container)))
    su_store = SqlQualityDriftAlertStore(create_async_session_factory(su_engine))
    try:
        tenant = uuid4()
        agent = f"tie-sib-{uuid4().hex[:8]}"
        current_tenant_id_var.set(tenant)
        mine = [await store.insert(_alert(tenant=tenant, agent=agent)) for _ in range(6)]
        ids = [r.id for r in mine]
        assert all(i is not None for i in ids)
        assert ids == sorted(ids, key=lambda i: i or 0)  # serial ids follow insertion order

        ts = datetime(2035, 2, 1, 12, 0, tzinfo=UTC)
        async with su_engine.begin() as conn:
            for row_id in ids:  # lowest id first → new index TIDs ascend with id
                await conn.execute(
                    update(QualityDriftAlertRow)
                    .where(QualityDriftAlertRow.id == row_id)
                    .values(detected_at=ts)
                )

        rows = await su_store.list_alerts(tenant_id=tenant, agent_name=agent)
        assert [r.id for r in rows] == ids
    finally:
        await su_engine.dispose()
        await engine.dispose()


@pytest.mark.asyncio
async def test_rls_blocks_cross_tenant_read(
    alert_store: tuple[SqlQualityDriftAlertStore, AsyncEngine],
) -> None:
    store, engine = alert_store
    try:
        tenant_a, tenant_b = uuid4(), uuid4()
        current_tenant_id_var.set(tenant_a)
        await store.insert(_alert(tenant=tenant_a))
        assert len(await store.list_alerts(tenant_id=tenant_a)) == 1
        # Scope to B: A's alert is invisible under RLS.
        current_tenant_id_var.set(tenant_b)
        assert await store.list_alerts(tenant_id=tenant_a) == []
        assert await store.latest_alert_at(tenant_id=tenant_a, agent_name="support-bot") is None
    finally:
        await engine.dispose()
