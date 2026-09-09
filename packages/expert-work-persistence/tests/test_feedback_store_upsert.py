"""``FeedbackStore.upsert`` / ``list_for_thread_scoped`` / ``down_rated_thread_ids`` — P-2 PR1.

迁移 0152 的形状先在这里钉住(部分唯一索引真的拒绝重复),再对两套实现跑同一场景。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from testcontainers.postgres import PostgresContainer

from expert_work.persistence import (
    DatabaseConfig,
    create_async_engine_from_config,
    create_async_session_factory,
)
from expert_work.persistence.feedback_store import (
    DbFeedbackStore,
    FeedbackRecord,
    FeedbackStore,
    InMemoryFeedbackStore,
)

ALEMBIC_INI = Path(__file__).resolve().parent.parent / "alembic.ini"


def _sync_dsn(container: PostgresContainer) -> str:
    url = str(container.get_connection_url())
    return url.replace("+psycopg2", "+psycopg").replace("postgresql://", "postgresql+psycopg://", 1)


def _async_dsn(container: PostgresContainer) -> str:
    url = str(container.get_connection_url())
    return url.replace("+psycopg2", "+asyncpg").replace("postgresql://", "postgresql+asyncpg://", 1)


@pytest.fixture
async def migrated_engine(postgres_container: PostgresContainer) -> AsyncIterator[AsyncEngine]:
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", _sync_dsn(postgres_container))
    command.upgrade(cfg, "head")
    engine = create_async_engine_from_config(DatabaseConfig(dsn=_async_dsn(postgres_container)))
    try:
        yield engine
    finally:
        await engine.dispose()


_INSERT = text(
    "INSERT INTO feedback (tenant_id, thread_id, run_id, rating, actor_id, source) "
    "VALUES (:t, :th, :r, 'down', :a, :s)"
)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_0152_partial_unique_index_rejects_duplicate_run_actor(
    migrated_engine: AsyncEngine,
) -> None:
    """同 (tenant, run, actor) 第二行被 ``feedback_run_actor_uniq`` 拒绝。

    run_id 为 NULL 的老式行不受约束。
    """
    tenant, thread, run = uuid4(), uuid4(), uuid4()
    async with migrated_engine.begin() as conn:
        await conn.execute(
            _INSERT, {"t": tenant, "th": thread, "r": run, "a": "u-1", "s": "external"}
        )
    with pytest.raises(IntegrityError) as exc_info:
        async with migrated_engine.begin() as conn:
            await conn.execute(
                _INSERT, {"t": tenant, "th": thread, "r": run, "a": "u-1", "s": "external"}
            )
    assert "feedback_run_actor_uniq" in str(exc_info.value)
    # 老式行(run_id NULL)想插几条插几条。
    async with migrated_engine.begin() as conn:
        for _ in range(2):
            await conn.execute(
                _INSERT, {"t": tenant, "th": thread, "r": None, "a": "u-1", "s": "console"}
            )
    # 上面那两条 NULL 行**证不出**部分谓词在不在:Postgres 唯一索引默认
    # NULLS DISTINCT,键里带 NULL 的行本来就互不相等,去掉 WHERE 一样能插。
    # 谓词本身只能直接读 indexdef 钉住(去掉 postgresql_where 这条就红)。
    async with migrated_engine.connect() as conn:
        indexdef = (
            await conn.execute(
                text("SELECT indexdef FROM pg_indexes WHERE indexname = 'feedback_run_actor_uniq'")
            )
        ).scalar_one()
    assert "WHERE (run_id IS NOT NULL)" in indexdef


@pytest.mark.integration
@pytest.mark.asyncio
async def test_0152_source_check_and_candidate_columns(migrated_engine: AsyncEngine) -> None:
    with pytest.raises(IntegrityError) as exc_info:
        async with migrated_engine.begin() as conn:
            await conn.execute(
                _INSERT, {"t": uuid4(), "th": uuid4(), "r": uuid4(), "a": "u", "s": "widget"}
            )
    assert "feedback_source_valid" in str(exc_info.value)
    async with migrated_engine.connect() as conn:
        cols = {
            row[0]
            for row in await conn.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'curation_candidate'"
                )
            )
        }
    assert {
        "feedback_run_id",
        "feedback_comment",
        "feedback_changed_at",
        "feedback_source",
    } <= cols


def _rec(
    *,
    tenant_id: UUID,
    thread_id: UUID,
    run_id: UUID | None,
    actor_id: str = "ext-user-1",
    rating: str = "down",
    comment: str | None = None,
    item_id: str | None = None,
    source: str = "external",
) -> FeedbackRecord:
    return FeedbackRecord(
        tenant_id=tenant_id,
        thread_id=thread_id,
        run_id=run_id,
        rating=rating,
        comment=comment,
        item_id=item_id,
        source=source,
        actor_id=actor_id,
    )


async def _upsert_scenario(store: FeedbackStore) -> None:
    tenant, thread, run = uuid4(), uuid4(), uuid4()
    first, updated = await store.upsert(
        _rec(tenant_id=tenant, thread_id=thread, run_id=run, comment="太慢", item_id="p1")
    )
    assert updated is False
    assert first.id is not None and first.created_at is not None and first.updated_at is None

    second, updated = await store.upsert(
        _rec(tenant_id=tenant, thread_id=thread, run_id=run, rating="up", comment=None)
    )
    assert updated is True
    assert second.id == first.id
    assert second.created_at == first.created_at
    assert second.rating == "up" and second.comment is None and second.item_id is None
    assert second.updated_at is not None

    rows = await store.list_for_thread_scoped(tenant_id=tenant, thread_id=thread)
    assert [r.id for r in rows] == [first.id]  # 一行,不是两行

    # 另一个 actor 同一轮 → 各自一行。
    other, updated = await store.upsert(
        _rec(tenant_id=tenant, thread_id=thread, run_id=run, actor_id="ext-user-2")
    )
    assert updated is False and other.id != first.id
    rows = await store.list_for_thread_scoped(tenant_id=tenant, thread_id=thread)
    assert [r.id for r in rows] == [other.id, first.id]  # id 降序

    # 显式租户谓词:别的租户看不到。
    assert await store.list_for_thread_scoped(tenant_id=uuid4(), thread_id=thread) == []

    with pytest.raises(ValueError, match="run_id"):
        await store.upsert(_rec(tenant_id=tenant, thread_id=thread, run_id=None))


async def _down_rated_scenario(store: FeedbackStore) -> None:
    tenant = uuid4()
    t_down, t_up, t_mixed = uuid4(), uuid4(), uuid4()
    await store.upsert(_rec(tenant_id=tenant, thread_id=t_up, run_id=uuid4(), rating="up"))
    await store.upsert(_rec(tenant_id=tenant, thread_id=t_down, run_id=uuid4(), rating="down"))
    await store.upsert(_rec(tenant_id=tenant, thread_id=t_mixed, run_id=uuid4(), rating="up"))
    await store.upsert(
        _rec(tenant_id=tenant, thread_id=t_mixed, run_id=uuid4(), rating="down", actor_id="b")
    )
    assert await store.down_rated_thread_ids(tenant_id=tenant) == {t_down, t_mixed}
    assert await store.down_rated_thread_ids(tenant_id=tenant, limit=1) == {t_down}
    assert await store.down_rated_thread_ids(tenant_id=uuid4()) == set()


@pytest.mark.asyncio
async def test_in_memory_upsert_overwrites_same_run_actor() -> None:
    await _upsert_scenario(InMemoryFeedbackStore())


@pytest.mark.asyncio
async def test_in_memory_down_rated_thread_ids() -> None:
    await _down_rated_scenario(InMemoryFeedbackStore())


@pytest.mark.integration
@pytest.mark.asyncio
async def test_sql_upsert_overwrites_same_run_actor(migrated_engine: AsyncEngine) -> None:
    await _upsert_scenario(DbFeedbackStore(create_async_session_factory(migrated_engine)))


@pytest.mark.integration
@pytest.mark.asyncio
async def test_sql_down_rated_thread_ids(migrated_engine: AsyncEngine) -> None:
    await _down_rated_scenario(DbFeedbackStore(create_async_session_factory(migrated_engine)))


# --------------------------------------------------------------------------- #
# 定序同义 —— 排序键是「该 thread 最早一条 👎 的 id」,不是最新那条,也不受
# 中间夹着的 👍 影响。同一 thread 的多条 👎 与别的 thread 交错时两套实现必须
# 给出同一个集合。
# --------------------------------------------------------------------------- #
async def _seed_interleaved(
    store: FeedbackStore, *, tenant_id: UUID, a: UUID, b: UUID, c: UUID
) -> None:
    """按 id 顺序落 9 行:A 的 👎 在 1/5/9、B 在 2、C 在 7,其余是 👍。"""

    async def rate(thread: UUID, actor: str, rating: str) -> None:
        await store.upsert(
            _rec(
                tenant_id=tenant_id,
                thread_id=thread,
                run_id=uuid4(),
                rating=rating,
                actor_id=actor,
            )
        )

    await rate(a, "a1", "down")  # 1
    await rate(b, "b1", "down")  # 2
    await rate(a, "a2", "up")  # 3
    await rate(c, "c1", "up")  # 4
    await rate(a, "a3", "down")  # 5
    await rate(b, "b2", "up")  # 6
    await rate(c, "c2", "down")  # 7
    await rate(a, "a4", "up")  # 8
    await rate(a, "a5", "down")  # 9


@pytest.mark.integration
@pytest.mark.asyncio
async def test_down_rated_thread_ids_orders_by_earliest_down_in_both_implementations(
    migrated_engine: AsyncEngine,
) -> None:
    """两套实现在「多条 👎 交错」的形态上返回同一个集合。

    排序键 = 该 thread **最早**一条 👎 的 id → A=1、B=2、C=7;``limit=2`` 取
    前两个 = {A, B}。改成按最晚一条排(``max``)会得到 {B, C} —— 这是这条用例
    真正钉住的东西,``_down_rated_scenario`` 里一 thread 一条 👎 的形态区分不了
    ``min`` 和 ``max``。
    """
    a, b, c = uuid4(), uuid4(), uuid4()
    tenant_mem, tenant_sql = uuid4(), uuid4()

    memory = InMemoryFeedbackStore()
    sql = DbFeedbackStore(create_async_session_factory(migrated_engine))
    await _seed_interleaved(memory, tenant_id=tenant_mem, a=a, b=b, c=c)
    await _seed_interleaved(sql, tenant_id=tenant_sql, a=a, b=b, c=c)

    memory_out = await memory.down_rated_thread_ids(tenant_id=tenant_mem, limit=2)
    sql_out = await sql.down_rated_thread_ids(tenant_id=tenant_sql, limit=2)

    assert memory_out == {a, b}
    assert sql_out == memory_out
    # 不设 limit 时三条 thread 都在(C 只是排第三,不是被过滤掉)。
    assert await memory.down_rated_thread_ids(tenant_id=tenant_mem) == {a, b, c}
    assert await sql.down_rated_thread_ids(tenant_id=tenant_sql) == {a, b, c}


# --------------------------------------------------------------------------- #
# INSERT 撞唯一键后的重试分支 —— 真并发那条在 P-2 PR2 Task 8(两副本同时
# upsert 只成一行),这里用打桩把**分支本身**跑到,并钉住「最多重试一次」。
# --------------------------------------------------------------------------- #
class _FlakyFlushSessionFactory:
    """包一层真 session 工厂,让前 ``fail_times`` 次 ``flush()`` 抛 IntegrityError。

    ``before_raise`` 在抛之前跑,用来模拟「并发写入方在我们 SELECT 之后、
    INSERT 之前抢先落了同一把键的行」—— 没有它,重试那一趟 SELECT 依然扑空,
    走的还是 INSERT 分支,``updated=True`` 就无从谈起。
    """

    def __init__(
        self,
        inner: Any,
        *,
        fail_times: int,
        before_raise: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self._inner = inner
        self.remaining = fail_times
        self._before_raise = before_raise

    def __call__(self) -> Any:
        return _FlakyFlushSession(self._inner(), self)


class _FlakyFlushSession:
    def __init__(self, ctx: Any, owner: _FlakyFlushSessionFactory) -> None:
        self._ctx = ctx
        self._owner = owner

    async def __aenter__(self) -> Any:
        session = await self._ctx.__aenter__()
        real_flush = session.flush

        async def flush(*args: Any, **kwargs: Any) -> Any:
            if self._owner.remaining > 0:
                self._owner.remaining -= 1
                if self._owner._before_raise is not None:
                    await self._owner._before_raise()
                raise IntegrityError(
                    "INSERT INTO feedback",
                    None,
                    Exception('duplicate key value violates "feedback_run_actor_uniq"'),
                )
            return await real_flush(*args, **kwargs)

        session.flush = flush
        return session

    async def __aexit__(self, *exc: Any) -> Any:
        return await self._ctx.__aexit__(*exc)


def _as_session_factory(factory: _FlakyFlushSessionFactory) -> async_sessionmaker[AsyncSession]:
    """测试替身,只需满足 ``DbFeedbackStore`` 实际用到的「调用后当 async 上下文管理器」。"""
    return cast("async_sessionmaker[AsyncSession]", factory)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_sql_upsert_retries_once_and_the_retry_takes_the_update_branch(
    migrated_engine: AsyncEngine,
) -> None:
    """第一趟 INSERT 撞键 → 回滚重试 → 第二趟 SELECT 命中并走 UPDATE,库里仍是一行。"""
    tenant, thread, run = uuid4(), uuid4(), uuid4()
    session_factory = create_async_session_factory(migrated_engine)

    async def competitor_wins() -> None:
        async with migrated_engine.begin() as conn:
            await conn.execute(
                _INSERT,
                {"t": tenant, "th": thread, "r": run, "a": "ext-user-1", "s": "external"},
            )

    store = DbFeedbackStore(
        _as_session_factory(
            _FlakyFlushSessionFactory(session_factory, fail_times=1, before_raise=competitor_wins)
        )
    )
    stored, updated = await store.upsert(
        _rec(tenant_id=tenant, thread_id=thread, run_id=run, rating="up", comment="第二趟")
    )

    assert updated is True  # 走的是 UPDATE 分支,不是又插了一行
    assert stored.rating == "up" and stored.comment == "第二趟"
    plain = DbFeedbackStore(session_factory)
    rows = await plain.list_for_thread_scoped(tenant_id=tenant, thread_id=thread)
    assert len(rows) == 1
    assert rows[0].id == stored.id


@pytest.mark.integration
@pytest.mark.asyncio
async def test_sql_upsert_gives_up_after_one_retry(migrated_engine: AsyncEngine) -> None:
    """连撞两次 → IntegrityError 抛出去,不无界递归。"""
    store = DbFeedbackStore(
        _as_session_factory(
            _FlakyFlushSessionFactory(create_async_session_factory(migrated_engine), fail_times=2)
        )
    )
    with pytest.raises(IntegrityError):
        await store.upsert(_rec(tenant_id=uuid4(), thread_id=uuid4(), run_id=uuid4()))
