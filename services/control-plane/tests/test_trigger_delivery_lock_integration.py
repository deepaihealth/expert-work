"""Integration test for :func:`control_plane.trigger_delivery.delivery_thread_lock`.

PROD-9(多副本)—— 真 Postgres 上证明投递关窗锁的跨副本契约(单测的假
session factory 只能近似):

- 同一 originating thread 的两个投递方**阻塞**串行(不是 429 —— 投递是
  后台/管理路径,等一小段比失败语义正确);
- 不同 thread 互不阻塞;
- ``session_factory=None`` 是 no-op(单进程栈,不碰 DB)。

无需建表 —— ``pg_advisory_xact_lock`` 是内建,与
``test_tenant_resource_lock_integration.py`` 同款基座。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine
from testcontainers.postgres import PostgresContainer

from control_plane.trigger_delivery import delivery_thread_lock
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


async def test_same_thread_contenders_serialise_blocking(engine: AsyncEngine) -> None:
    session_factory = create_async_session_factory(engine)
    thread_id = uuid4()
    order: list[str] = []
    holder_entered = asyncio.Event()
    release_holder = asyncio.Event()

    async def holder() -> None:
        async with delivery_thread_lock(session_factory, thread_id):
            order.append("holder_in")
            holder_entered.set()
            await release_holder.wait()
        order.append("holder_out")

    async def racer() -> None:
        await holder_entered.wait()
        async with delivery_thread_lock(session_factory, thread_id):
            order.append("racer_in")

    holder_task = asyncio.create_task(holder())
    racer_task = asyncio.create_task(racer())
    await asyncio.wait_for(holder_entered.wait(), 10)
    # 锁被持有期间,racer 必须还没进临界区(阻塞在 pg_advisory_xact_lock 上)。
    await asyncio.sleep(0.3)
    assert "racer_in" not in order, "同 thread 的第二个投递方没有被锁住"
    release_holder.set()
    await asyncio.wait_for(asyncio.gather(holder_task, racer_task), 10)
    # 释放后两个收尾动作的先后不定(racer 的 execute 返回与 holder 协程恢复
    # 是并发的);关键不变式在上面那条「持锁期间 racer 未进入」。
    assert order[0] == "holder_in"
    assert sorted(order[1:]) == ["holder_out", "racer_in"]


async def test_different_threads_do_not_contend(engine: AsyncEngine) -> None:
    session_factory = create_async_session_factory(engine)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def holder() -> None:
        async with delivery_thread_lock(session_factory, uuid4()):
            entered.set()
            await release.wait()

    holder_task = asyncio.create_task(holder())
    await asyncio.wait_for(entered.wait(), 10)
    # 另一个 thread 的锁必须立即可得。
    async with asyncio.timeout(2):
        async with delivery_thread_lock(session_factory, uuid4()):
            pass
    release.set()
    await asyncio.wait_for(holder_task, 10)


async def test_none_session_factory_is_noop() -> None:
    async with delivery_thread_lock(None, uuid4()):
        pass
