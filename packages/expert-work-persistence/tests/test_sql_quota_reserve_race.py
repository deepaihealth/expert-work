"""W1-PR2 Task 3 — ``reserve()`` monthly-budget TOCTOU regression (real Postgres).

Before the fix, ``reserve_tokens()`` did a ``get_budget()`` read followed by a
separate ``reserve()`` write — two round trips that let N concurrent replicas
each pass the check before any of them bumped ``reserved_total``, overspending
the monthly budget. The fix folds the check into the same row-locked
transaction as the bump (see ``SqlTokenReservationStore._lock_budget_row`` /
``reserve``), so concurrent reserves on one ``(tenant, month)`` ledger row
serialise at the lock and the total granted never exceeds the budget.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime
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
    create_async_engine_from_config,
    create_async_session_factory,
)
from expert_work.persistence.quota import (
    BudgetExceededError,
    SqlTokenReservationStore,
)
from expert_work.persistence.rls import build_rls_sessionmaker, current_tenant_id_var

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
def reservation_store(
    postgres_container: PostgresContainer,
) -> Iterator[tuple[SqlTokenReservationStore, AsyncEngine]]:
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", _sync_dsn(postgres_container))
    command.upgrade(cfg, "head")
    _provision_app_role(_sync_dsn(postgres_container))

    app_dsn = _rewrite_credentials(_async_dsn(postgres_container), APP_ROLE, APP_PASSWORD)
    engine = create_async_engine_from_config(DatabaseConfig(dsn=app_dsn))
    session_factory = build_rls_sessionmaker(create_async_session_factory(engine))
    yield SqlTokenReservationStore(session_factory), engine


@pytest.fixture(autouse=True)
def reset_rls() -> Iterator[None]:
    token = current_tenant_id_var.set(None)
    try:
        yield
    finally:
        current_tenant_id_var.reset(token)


@pytest.mark.asyncio
async def test_concurrent_reserve_never_exceeds_monthly_budget(
    reservation_store: tuple[SqlTokenReservationStore, AsyncEngine],
) -> None:
    """10 concurrent reserves of 100 tokens against a 500 budget must grant
    exactly 5 (500 / 100) and reject the other 5 with ``BudgetExceededError``
    — never over-granting, and never under-granting (no lost admits)."""
    store, engine = reservation_store
    try:
        tenant = uuid4()
        current_tenant_id_var.set(tenant)
        month = datetime.now(tz=UTC).date().replace(day=1)
        await store.set_budget_total(tenant_id=tenant, month=month, budget_total=500)

        async def _reserve() -> bool:
            try:
                await store.reserve(
                    tenant_id=tenant,
                    agent_name="alpha",
                    thread_id=uuid4(),
                    estimated=100,
                )
            except BudgetExceededError:
                return False
            return True

        results = await asyncio.gather(*[_reserve() for _ in range(10)])
        granted = sum(1 for ok in results if ok)
        assert granted == 5

        budget = await store.get_budget(tenant_id=tenant, month=month)
        assert budget is not None
        assert budget.reserved_total == 500  # exactly the cap — no overspend, no lost admit
    finally:
        await engine.dispose()
