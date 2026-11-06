"""B-278 review —— one purge per agent name at a time; a second one answers 409 AGENT_BUSY.

* The in-process fallback (no database) is exercised through the real endpoint:
  a second ``POST …/purge`` fired while the first is mid-way is refused, the first
  one finishes.
* The Postgres advisory lock is checked against a real server: a second holder on
  another connection is refused while the first holds it, gets it once released,
  and other names / tenants are independent.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from testcontainers.postgres import PostgresContainer

from control_plane.purge.agent_purge_lock import agent_purge_lock
from expert_work.persistence.database import (
    DatabaseConfig,
    create_async_engine_from_config,
    create_async_session_factory,
)
from tests.test_agent_lifecycle_api import _create_and_delete, _Ctx, _purge
from tests.test_agent_lifecycle_api import ctx as ctx


@pytest.mark.asyncio
async def test_a_second_purge_of_the_same_name_is_409_busy(ctx: _Ctx) -> None:
    await _create_and_delete(ctx.client)
    jobs = ctx.app.state.memory_writeback_job_store  # type: ignore[attr-defined]
    original = jobs.delete_for_agent
    seen: dict[str, object] = {}

    async def _meanwhile(*, tenant_id: UUID, agent_name: str) -> int:
        second = await _purge(ctx.client)
        seen["status"] = second.status_code  # type: ignore[attr-defined]
        seen["code"] = second.json()["error"]["code"]  # type: ignore[attr-defined]
        return int(await original(tenant_id=tenant_id, agent_name=agent_name))

    jobs.delete_for_agent = _meanwhile
    first = await _purge(ctx.client)

    assert seen == {"status": 409, "code": "AGENT_BUSY"}
    assert first.status_code == 200, first.text  # type: ignore[attr-defined]
    again = await _purge(ctx.client)  # lock released and the name gone: 404, not busy
    assert again.status_code == 404  # type: ignore[attr-defined]


def _async_dsn(container: PostgresContainer) -> str:
    url = str(container.get_connection_url())
    return url.replace("+psycopg2", "+asyncpg").replace("postgresql://", "postgresql+asyncpg://", 1)


@pytest.fixture
async def pg_sf(
    postgres_container: PostgresContainer,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine_from_config(DatabaseConfig(dsn=_async_dsn(postgres_container)))
    try:
        yield create_async_session_factory(engine)
    finally:
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_postgres_lock_admits_one_holder_per_name(
    pg_sf: async_sessionmaker[AsyncSession],
) -> None:
    tenant, other_tenant = uuid4(), uuid4()
    async with agent_purge_lock(pg_sf, tenant, "doomed") as first:
        assert first is True
        async with agent_purge_lock(pg_sf, tenant, "doomed") as second:
            assert second is False
        async with agent_purge_lock(pg_sf, tenant, "other-name") as other_name:
            assert other_name is True
        async with agent_purge_lock(pg_sf, other_tenant, "doomed") as other:
            assert other is True
    async with agent_purge_lock(pg_sf, tenant, "doomed") as after_release:
        assert after_release is True
