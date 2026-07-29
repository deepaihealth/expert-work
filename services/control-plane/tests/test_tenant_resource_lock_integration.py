"""Integration test for :func:`control_plane._tenant_resource_lock.tenant_resource_lock`.

Boots a real Postgres (testcontainers) and drives the lock from two
concurrent coroutines on independent sessions/connections to prove the
cross-replica contract the unit tests (``test_tenant_resource_lock.py``,
fake session factory) can only approximate:

- two callers contending the same ``(tenant_id, resource_kind)`` key
  genuinely serialise at the Postgres level — the second either waits out
  its retry and proceeds, or times out and gets ``429``;
- different keys never contend;
- ``session_factory=None`` is a no-op (no DB touched).

No schema is needed — ``pg_try_advisory_xact_lock`` is a built-in, same as
``test_workspace_lock_integration.py``.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncEngine
from testcontainers.postgres import PostgresContainer

from control_plane._tenant_resource_lock import tenant_resource_lock
from expert_work.persistence.database import (
    DatabaseConfig,
    create_async_engine_from_config,
    create_async_session_factory,
)

pytestmark = pytest.mark.integration


def _async_dsn(container: PostgresContainer) -> str:
    url = str(container.get_connection_url())
    return url.replace("+psycopg2", "+asyncpg").replace("postgresql://", "postgresql+asyncpg://", 1)


@pytest.fixture
async def engine(postgres_container: PostgresContainer) -> AsyncIterator[AsyncEngine]:
    eng = create_async_engine_from_config(
        DatabaseConfig(dsn=_async_dsn(postgres_container), pgbouncer_mode=False)
    )
    try:
        yield eng
    finally:
        await eng.dispose()


async def test_second_racer_gets_429_when_held_past_retry_budget(engine: AsyncEngine) -> None:
    session_factory = create_async_session_factory(engine)
    tenant_id = uuid4()
    order: list[str] = []

    async def holder() -> None:
        async with tenant_resource_lock(session_factory, tenant_id, "eval_dataset"):
            order.append("holder-enter")
            await asyncio.sleep(0.3)  # well past 1 try + 1 retry (~50ms)
            order.append("holder-exit")

    async def racer() -> HTTPException:
        await asyncio.sleep(0.02)  # let the holder acquire first
        with pytest.raises(HTTPException) as exc_info:
            async with tenant_resource_lock(session_factory, tenant_id, "eval_dataset"):
                pass  # pragma: no cover - never reached
        return exc_info.value

    _, exc = await asyncio.gather(holder(), racer())
    assert exc.status_code == 429
    assert "eval_dataset" in str(exc.detail)
    assert order == ["holder-enter", "holder-exit"]


async def test_second_racer_succeeds_when_first_releases_quickly(engine: AsyncEngine) -> None:
    session_factory = create_async_session_factory(engine)
    tenant_id = uuid4()
    order: list[str] = []

    async def holder() -> None:
        async with tenant_resource_lock(session_factory, tenant_id, "cron_trigger"):
            order.append("holder-enter")
            await asyncio.sleep(0.01)  # released before the 50ms retry fires
            order.append("holder-exit")

    async def racer() -> None:
        await asyncio.sleep(0.005)
        async with tenant_resource_lock(session_factory, tenant_id, "cron_trigger"):
            order.append("racer-enter")

    await asyncio.gather(holder(), racer())
    assert order == ["holder-enter", "holder-exit", "racer-enter"]


async def test_different_resource_kinds_run_concurrently(engine: AsyncEngine) -> None:
    session_factory = create_async_session_factory(engine)
    tenant_id = uuid4()
    order: list[str] = []

    async def worker(name: str, resource_kind: str) -> None:
        async with tenant_resource_lock(session_factory, tenant_id, resource_kind):
            order.append(f"{name}-enter")
            await asyncio.sleep(0.1)
            order.append(f"{name}-exit")

    await asyncio.gather(worker("A", "eval_dataset"), worker("B", "webhook_endpoint"))
    # Different keys don't contend — both enter before either exits.
    assert order[0].endswith("-enter")
    assert order[1].endswith("-enter")


async def test_different_tenants_same_resource_kind_run_concurrently(engine: AsyncEngine) -> None:
    session_factory = create_async_session_factory(engine)
    order: list[str] = []

    async def worker(name: str, tenant_id: object) -> None:
        async with tenant_resource_lock(session_factory, tenant_id, "cron_trigger"):  # type: ignore[arg-type]
            order.append(f"{name}-enter")
            await asyncio.sleep(0.1)
            order.append(f"{name}-exit")

    await asyncio.gather(worker("A", uuid4()), worker("B", uuid4()))
    assert order[0].endswith("-enter")
    assert order[1].endswith("-enter")


async def test_none_session_factory_touches_no_db_both_proceed() -> None:
    """Sanity check the no-op path doesn't need ``postgres_container`` at
    all — asserts the contract without requiring the real engine fixture."""
    tenant_id = uuid4()
    order: list[str] = []

    async def worker(name: str) -> None:
        async with tenant_resource_lock(None, tenant_id, "eval_dataset"):
            order.append(f"{name}-enter")
            await asyncio.sleep(0.05)
            order.append(f"{name}-exit")

    await asyncio.gather(worker("A"), worker("B"))
    assert order[0].endswith("-enter")
    assert order[1].endswith("-enter")
