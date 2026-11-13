"""B-300 PR 2 —— 两个存储实现上的来源列与「向量模型名」列,以及汇总行的出处和分数(D6 前半)。

同一组用例跑两遍:内存实现(单元)与 SQL 实现(集成,迁移到 head)。

* 新写入、取代、改内容、汇总四条路都把这次用的向量模型名写进行里;
* 来源列随新写入、取代原样落库;改内容可以把它置空(管理员 ``PATCH``),默认不动;
* 汇总行:来源会话 / run 取成员里最新那一条的,重要度取最大,置信度取最小且不超过 0.99;
  成员全是来自助手的 → 来源「来自助手」、置信度再压到 0.6 以内;否则来源为空;
* 写汇总前在同一事务里重读成员,成员里既有来自助手、又有用户说的 / 用户更正的 → 不写;
* 库里不认识的来源值读出来是空(只有 SQL 能造出这种行)。
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text

from expert_work.persistence import (
    DatabaseConfig,
    InMemoryMemoryStore,
    SqlMemoryStore,
    create_async_engine_from_config,
    create_async_session_factory,
)
from expert_work.persistence.embedding import EMBEDDING_DIM
from expert_work.persistence.memory.base import ConsolidationSourcesChangedError, MemoryStore
from expert_work.persistence.memory.hash import hash_content
from expert_work.protocol import MemoryItem
from expert_work.protocol.memory_item import MemoryOrigin

ALEMBIC_INI = Path(__file__).resolve().parent.parent / "alembic.ini"
_MODEL = "vendor-a/embed-v2"
_NEW_MODEL = "vendor-b/embed-v3"
_T0 = datetime(2026, 11, 1, tzinfo=UTC)


def _vec(*head: float) -> tuple[float, ...]:
    return tuple(head) + (0.0,) * (EMBEDDING_DIM - len(head))


def _item(
    tenant: UUID,
    user: UUID,
    content: str,
    *,
    origin: MemoryOrigin | None = None,
    confidence: float = 0.5,
    importance: float = 0.5,
    created_at: datetime | None = None,
    thread: str | None = None,
    run: str | None = None,
) -> MemoryItem:
    return MemoryItem(
        id=uuid4(),
        tenant_id=tenant,
        user_id=user,
        kind="fact",
        content=content,
        embedding=_vec(1.0),
        content_hash=hash_content(content),
        origin=origin,
        embedding_model=_MODEL,
        confidence=confidence,
        importance=importance,
        created_at=created_at,
        last_used_at=created_at,
        source_thread_id=thread,
        source_run_id=run,
    )


@pytest.fixture(params=["memory", pytest.param("sql", marks=pytest.mark.integration)])
def store(request: pytest.FixtureRequest) -> Iterator[MemoryStore]:
    if request.param == "memory":
        yield InMemoryMemoryStore()
        return
    container = request.getfixturevalue("postgres_container")
    url = str(container.get_connection_url())
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option(
        "sqlalchemy.url",
        url.replace("+psycopg2", "+psycopg").replace("postgresql://", "postgresql+psycopg://", 1),
    )
    command.upgrade(cfg, "head")
    dsn = url.replace("+psycopg2", "+asyncpg").replace("postgresql://", "postgresql+asyncpg://", 1)
    engine = create_async_engine_from_config(DatabaseConfig(dsn=dsn))
    yield SqlMemoryStore(create_async_session_factory(engine))


async def _row(store: MemoryStore, tenant: UUID, user: UUID, memory_id: UUID) -> MemoryItem:
    rows = await store.list_for_user(tenant_id=tenant, user_id=user, limit=50)
    return next(r for r in rows if r.id == memory_id)


@pytest.mark.asyncio
async def test_write_keeps_origin_and_embedding_model(store: MemoryStore) -> None:
    t, u = uuid4(), uuid4()
    stated = _item(t, u, "stated", origin="user_stated")
    derived = _item(t, u, "derived", origin="assistant_derived")
    legacy = _item(t, u, "legacy").model_copy(update={"embedding_model": None})
    await store.write([stated, derived, legacy])
    assert (await _row(store, t, u, stated.id)).origin == "user_stated"
    assert (await _row(store, t, u, derived.id)).origin == "assistant_derived"
    got = await _row(store, t, u, legacy.id)
    assert (got.origin, got.embedding_model) == (None, None)
    assert (await _row(store, t, u, stated.id)).embedding_model == _MODEL


@pytest.mark.asyncio
async def test_supersede_writes_the_new_rows_origin_and_model(store: MemoryStore) -> None:
    t, u = uuid4(), uuid4()
    old = _item(t, u, "old", origin="user_stated")
    await store.write([old])
    new = _item(t, u, "new", origin="assistant_derived").model_copy(
        update={"embedding_model": _NEW_MODEL}
    )
    written = await store.supersede(tenant_id=t, user_id=u, old_id=old.id, new_item=new)
    assert written is not None
    got = await _row(store, t, u, new.id)
    assert (got.origin, got.embedding_model) == ("assistant_derived", _NEW_MODEL)


@pytest.mark.asyncio
async def test_update_content_records_model_and_clears_origin_on_request(
    store: MemoryStore,
) -> None:
    t, u = uuid4(), uuid4()
    kept, cleared = _item(t, u, "a", origin="user_stated"), _item(t, u, "b", origin="user_stated")
    await store.write([kept, cleared])
    # 用户更正:来源列不动(有效来源由置信度 1.0 表达),向量模型名换成这次的。
    await store.update_content(
        tenant_id=t,
        user_id=u,
        memory_id=kept.id,
        content="a2",
        embedding=_vec(0.5, 0.5),
        confidence=1.0,
        embedding_model=_NEW_MODEL,
    )
    # 管理员改写:来源置空。
    await store.update_content(
        tenant_id=t,
        user_id=u,
        memory_id=cleared.id,
        content="b2",
        embedding=_vec(0.5, 0.5),
        embedding_model=_NEW_MODEL,
        clear_origin=True,
    )
    got_kept, got_cleared = await _row(store, t, u, kept.id), await _row(store, t, u, cleared.id)
    assert (got_kept.origin, got_kept.embedding_model) == ("user_stated", _NEW_MODEL)
    assert (got_cleared.origin, got_cleared.embedding_model) == (None, _NEW_MODEL)


async def _consolidate(store: MemoryStore, members: list[MemoryItem]) -> MemoryItem:
    t, u = members[0].tenant_id, members[0].user_id
    await store.write(members)
    return await store.write_consolidated(
        tenant_id=t,
        user_id=u,
        content=f"summary {uuid4()}",
        embedding=_vec(1.0),
        source_ids=[m.id for m in members],
        source_hashes={m.id: hash_content(m.content) for m in members},
        embedding_model=_NEW_MODEL,
    )


@pytest.mark.asyncio
async def test_summary_takes_newest_source_max_importance_min_confidence(
    store: MemoryStore,
) -> None:
    t, u = uuid4(), uuid4()
    members = [
        _item(t, u, "m0", origin="user_stated", confidence=0.9, importance=0.3,
              created_at=_T0, thread="th-0", run="run-0"),
        _item(t, u, "m1", confidence=0.7, importance=0.8,
              created_at=_T0 + timedelta(days=2), thread="th-new", run="run-new"),
        _item(t, u, "m2", origin="user_stated", confidence=0.95, importance=0.4,
              created_at=_T0 + timedelta(days=1), thread="th-1", run="run-1"),
    ]  # fmt: skip
    summary = await _consolidate(store, members)
    got = await _row(store, t, u, summary.id)
    assert (got.source_thread_id, got.source_run_id) == ("th-new", "run-new")
    assert got.importance == pytest.approx(0.8)
    assert got.confidence == pytest.approx(0.7)
    # 用户说的 + 未记录:汇总句是模型新写的,不给「用户消息里说的」。
    assert got.origin is None
    assert got.embedding_model == _NEW_MODEL


@pytest.mark.asyncio
async def test_summary_confidence_never_reaches_the_correction_sentinel(
    store: MemoryStore,
) -> None:
    t, u = uuid4(), uuid4()
    members = [_item(t, u, f"c{i}", confidence=1.0, created_at=_T0) for i in range(3)]
    summary = await _consolidate(store, members)
    assert (await _row(store, t, u, summary.id)).confidence == pytest.approx(0.99)


@pytest.mark.asyncio
async def test_all_assistant_summary_is_assistant_derived_and_capped(store: MemoryStore) -> None:
    t, u = uuid4(), uuid4()
    members = [
        _item(t, u, f"d{i}", origin="assistant_derived", confidence=0.6, created_at=_T0)
        for i in range(3)
    ]
    # 成员的置信度本来就 ≤ 0.6;再放一条超线的(旧数据可能有),看封顶。
    members[0] = members[0].model_copy(update={"confidence": 0.95})
    members[1] = members[1].model_copy(update={"confidence": 0.9})
    members[2] = members[2].model_copy(update={"confidence": 0.8})
    summary = await _consolidate(store, members)
    got = await _row(store, t, u, summary.id)
    assert got.origin == "assistant_derived"
    assert got.confidence == pytest.approx(0.6)


@pytest.mark.asyncio
async def test_summary_of_assistant_and_unrecorded_has_no_origin(store: MemoryStore) -> None:
    t, u = uuid4(), uuid4()
    members = [
        _item(t, u, "x0", origin="assistant_derived", created_at=_T0),
        _item(t, u, "x1", created_at=_T0),
        _item(t, u, "x2", origin="assistant_derived", created_at=_T0),
    ]
    summary = await _consolidate(store, members)
    assert (await _row(store, t, u, summary.id)).origin is None


@pytest.mark.asyncio
async def test_mixed_assistant_and_user_members_are_refused(store: MemoryStore) -> None:
    """取簇之后成员变成混合来源(例如原样内容被用户更正成置信度 1.0)—— 写汇总前重读,不写。"""
    t, u = uuid4(), uuid4()
    for user_side in ("user_stated", "user_corrected"):
        members = [
            _item(t, u, f"{user_side} a", origin="assistant_derived", created_at=_T0),
            _item(t, u, f"{user_side} b", origin="assistant_derived", created_at=_T0),
            _item(
                t,
                u,
                f"{user_side} c",
                origin="user_stated" if user_side == "user_stated" else None,
                confidence=1.0 if user_side == "user_corrected" else 0.9,
                created_at=_T0,
            ),
        ]
        with pytest.raises(ConsolidationSourcesChangedError):
            await _consolidate(store, members)
        rows = await store.list_for_user(tenant_id=t, user_id=u, limit=50)
        assert not [r for r in rows if r.status == "consolidated"]
        assert all(r.consolidated_into is None for r in rows)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_sql_unknown_origin_value_reads_as_none(
    postgres_container: object,
) -> None:
    url = str(postgres_container.get_connection_url())  # type: ignore[attr-defined]
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option(
        "sqlalchemy.url",
        url.replace("+psycopg2", "+psycopg").replace("postgresql://", "postgresql+psycopg://", 1),
    )
    command.upgrade(cfg, "head")
    dsn = url.replace("+psycopg2", "+asyncpg").replace("postgresql://", "postgresql+asyncpg://", 1)
    engine = create_async_engine_from_config(DatabaseConfig(dsn=dsn))
    sql = SqlMemoryStore(create_async_session_factory(engine))
    t, u = uuid4(), uuid4()
    item = _item(t, u, "odd", origin="user_stated")
    await sql.write([item])
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE memory_item SET origin = 'some_future_value' WHERE id = :id"),
            {"id": item.id},
        )
    assert (await _row(sql, t, u, item.id)).origin is None
    await engine.dispose()
