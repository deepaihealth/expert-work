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

from collections.abc import Iterator, Sequence
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
from expert_work.persistence.memory.base import (
    MAX_SUMMARY_DEPTH,
    ConsolidationSourcesChangedError,
    MemoryStore,
    UserSideHigherConfidenceError,
    UserSideTargetError,
    user_side_confidence,
)
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
async def test_update_content_sets_origin_and_caps_confidence_on_request(
    store: MemoryStore,
) -> None:
    """审查 MEDIUM-2(用户 11-13 定「按编辑人区分」)—— 本人改自己的:来源记「用户消息里说的」、
    置信度不动;改别人的:来源置空、置信度压到 0.99 以内(在存储里按行上的值压)。"""
    t, u = uuid4(), uuid4()
    own = _item(t, u, "own", origin="assistant_derived", confidence=1.0)
    other = _item(t, u, "other", origin="user_stated", confidence=1.0)
    await store.write([own, other])
    await store.update_content(
        tenant_id=t, user_id=u, memory_id=own.id, content="own2", embedding=_vec(1.0),
        origin="user_stated",
    )  # fmt: skip
    await store.update_content(
        tenant_id=t, user_id=u, memory_id=other.id, content="other2", embedding=_vec(1.0),
        clear_origin=True, max_confidence=0.99,
    )  # fmt: skip
    got_own, got_other = await _row(store, t, u, own.id), await _row(store, t, u, other.id)
    assert (got_own.origin, got_own.confidence) == ("user_stated", 1.0)
    assert got_other.origin is None
    assert got_other.confidence == pytest.approx(0.99)


# ─── 审查 MEDIUM-1 / MEDIUM-3:取代 / 作废前在锁里重判目标的来源 ─────────────────


async def _assert_untouched(store: MemoryStore, t: UUID, u: UUID, target: MemoryItem) -> None:
    rows = await store.list_for_user(tenant_id=t, user_id=u, limit=50)
    [row] = [r for r in rows if r.id == target.id]
    assert (row.invalid_at, row.expired_at, row.superseded_by) == (None, None, None)
    assert not [r for r in rows if r.supersedes == target.id]


@pytest.mark.asyncio
async def test_guarded_supersede_refuses_a_target_corrected_after_it_was_read(
    store: MemoryStore,
) -> None:
    """读邻居时是「来自助手、0.5」,判的那几秒里用户点了「更正」(原地改成 1.0):带保护的
    取代与作废都在锁里重判、抛专门的错误、什么都不改。"""
    t, u = uuid4(), uuid4()
    target = _item(t, u, "read as derived", origin="assistant_derived", confidence=0.5)
    await store.write([target])
    neighbour = await _row(store, t, u, target.id)  # the reconcile read
    assert neighbour.confidence == 0.5
    await store.update_content(
        tenant_id=t, user_id=u, memory_id=target.id, content="corrected",
        embedding=_vec(1.0), confidence=1.0,
    )  # fmt: skip
    with pytest.raises(UserSideTargetError):
        await store.supersede(
            tenant_id=t, user_id=u, old_id=target.id,
            new_item=_item(t, u, "assistant rewrite", origin="assistant_derived"),
            refuse_user_side=True,
        )  # fmt: skip
    with pytest.raises(UserSideTargetError):
        await store.expire(tenant_id=t, user_id=u, memory_id=target.id, refuse_user_side=True)
    await _assert_untouched(store, t, u, target)
    assert (await _row(store, t, u, target.id)).content == "corrected"


@pytest.mark.asyncio
async def test_guard_lets_assistant_and_unrecorded_targets_through(store: MemoryStore) -> None:
    t, u = uuid4(), uuid4()
    derived, unrecorded = _item(t, u, "d", origin="assistant_derived"), _item(t, u, "n")
    await store.write([derived, unrecorded])
    new = _item(t, u, "d2", origin="assistant_derived")
    written = await store.supersede(
        tenant_id=t, user_id=u, old_id=derived.id, new_item=new, refuse_user_side=True
    )
    assert written is not None
    assert await store.expire(
        tenant_id=t, user_id=u, memory_id=unrecorded.id, refuse_user_side=True
    )
    # 不带保护(用户说的候选)照旧能取代用户那一条。
    stated = _item(t, u, "s", origin="user_stated")
    await store.write([stated])
    assert await store.supersede(
        tenant_id=t, user_id=u, old_id=stated.id, new_item=_item(t, u, "s2", origin="user_stated")
    )


async def _summary(store: MemoryStore, t: UUID, u: UUID, origins: list[str | None]) -> MemoryItem:
    members = [
        _item(t, u, f"member {uuid4()}", origin=o, created_at=_T0)  # type: ignore[arg-type]
        if o != "corrected"
        else _item(t, u, f"member {uuid4()}", confidence=1.0, created_at=_T0)
        for o in origins
    ]
    return await _consolidate(store, members)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("origins", "protected"),
    [
        (["user_stated", None, None], True),
        (["corrected", None, None], True),
        (["assistant_derived", "assistant_derived", None], False),
        ([None, None, None], False),
    ],
    ids=["stated-member", "corrected-member", "assistant-and-unrecorded", "unrecorded"],
)
async def test_summary_is_protected_by_its_user_side_members(
    store: MemoryStore, origins: list[str | None], protected: bool
) -> None:
    """MEDIUM-3(用户 11-13 定「补上保护」)—— 汇总行本身来源为空,但成员里有用户说的 /
    用户更正的,就和用户那一条一样受保护;成员已被汇总(不再当前有效)也照样查得到。"""
    t, u = uuid4(), uuid4()
    summary = await _summary(store, t, u, origins)
    assert summary.origin is None
    assert await store.user_side_protected(tenant_id=t, user_id=u, memory_id=summary.id) is (
        protected
    )
    if protected:
        with pytest.raises(UserSideTargetError):
            await store.expire(tenant_id=t, user_id=u, memory_id=summary.id, refuse_user_side=True)
        await _assert_untouched(store, t, u, summary)
    else:
        assert await store.expire(
            tenant_id=t, user_id=u, memory_id=summary.id, refuse_user_side=True
        )


@pytest.mark.asyncio
async def test_nested_summary_inherits_protection(store: MemoryStore) -> None:
    t, u = uuid4(), uuid4()
    inner = await _summary(store, t, u, ["user_stated", None, None])
    outer = await _consolidate(
        store, [inner, _item(t, u, "o1", created_at=_T0), _item(t, u, "o2", created_at=_T0)]
    )
    assert inner.id in outer.consolidated_from
    assert await store.user_side_protected(tenant_id=t, user_id=u, memory_id=outer.id)
    with pytest.raises(UserSideTargetError):
        await store.supersede(
            tenant_id=t, user_id=u, old_id=outer.id,
            new_item=_item(t, u, "rewrite", origin="assistant_derived"),
            refuse_user_side=True,
        )  # fmt: skip
    await _assert_untouched(store, t, u, outer)
    plain = await _summary(store, t, u, [None, None, None])
    outer2 = await _consolidate(
        store, [plain, _item(t, u, "p1", created_at=_T0), _item(t, u, "p2", created_at=_T0)]
    )
    assert not await store.user_side_protected(tenant_id=t, user_id=u, memory_id=outer2.id)


# ─── 用户 11-14:置信度更低的「用户消息里说的」候选不得改用户一方的记忆(锁里重判)──────


async def _refuse_both(
    store: MemoryStore, t: UUID, u: UUID, target: MemoryItem, above: float
) -> None:
    with pytest.raises(UserSideHigherConfidenceError):
        _ = await store.supersede(
            tenant_id=t, user_id=u, old_id=target.id,
            new_item=_item(t, u, f"rewrite {uuid4()}", origin="user_stated", confidence=above),
            refuse_user_side_above=above,
        )  # fmt: skip
    with pytest.raises(UserSideHigherConfidenceError):
        _ = await store.expire(
            tenant_id=t, user_id=u, memory_id=target.id, refuse_user_side_above=above
        )
    await _assert_untouched(store, t, u, target)


@pytest.mark.asyncio
async def test_lower_confidence_guard_refuses_a_more_certain_user_side_target(
    store: MemoryStore,
) -> None:
    t, u = uuid4(), uuid4()
    stated = _item(t, u, "stated", origin="user_stated", confidence=0.9)
    _ = await store.write([stated])
    await _refuse_both(store, t, u, stated, above=0.5)


@pytest.mark.asyncio
async def test_lower_confidence_guard_judges_the_targets_current_confidence(
    store: MemoryStore,
) -> None:
    """存储按目标**此刻**的置信度判(这里写入时 0.5、调用前已被更正成 1.0),不按调用方早先
    读到的值;「判的那几秒里被更正」的竞争由 orchestrator 的
    ``test_target_made_more_certain_while_deciding_is_rechecked_under_the_lock`` 覆盖。"""
    t, u = uuid4(), uuid4()
    target = _item(t, u, "stated", origin="user_stated", confidence=0.5)
    _ = await store.write([target])
    _ = await store.update_content(
        tenant_id=t, user_id=u, memory_id=target.id, content="corrected",
        embedding=_vec(1.0), confidence=1.0,
    )  # fmt: skip
    await _refuse_both(store, t, u, target, above=0.6)


@pytest.mark.asyncio
async def test_lower_confidence_guard_lets_equal_and_non_user_targets_through(
    store: MemoryStore,
) -> None:
    t, u = uuid4(), uuid4()
    equal = _item(t, u, "equal", origin="user_stated", confidence=0.5)
    derived = _item(t, u, "derived", origin="assistant_derived", confidence=0.6)
    unrecorded = _item(t, u, "unrecorded", confidence=0.9)
    _ = await store.write([equal, derived, unrecorded])
    for target in (equal, derived):
        assert await store.supersede(
            tenant_id=t, user_id=u, old_id=target.id,
            new_item=_item(t, u, f"new {target.content}", origin="user_stated"),
            refuse_user_side_above=0.5,
        )  # fmt: skip
    assert await store.expire(
        tenant_id=t, user_id=u, memory_id=unrecorded.id, refuse_user_side_above=0.5
    )


@pytest.mark.asyncio
async def test_lower_confidence_guard_on_a_summary_uses_its_user_side_members(
    store: MemoryStore,
) -> None:
    """成员里有用户说的汇总行算用户一方;比的是成员里最高的用户一方置信度(这里 0.5)。"""
    t, u = uuid4(), uuid4()
    summary = await _summary(store, t, u, ["user_stated", None, None])
    assert summary.confidence == 0.5
    await _refuse_both(store, t, u, summary, above=0.4)
    assert await store.expire(
        tenant_id=t, user_id=u, memory_id=summary.id, refuse_user_side_above=0.5
    )


@pytest.mark.asyncio
async def test_lower_confidence_guard_keeps_a_correction_folded_into_a_summary(
    store: MemoryStore,
) -> None:
    """审查(11-14)—— {用户更正 1.0, 未记录 0.5, 未记录 0.5} 汇成的汇总行自己是 0.5;按成员里最高的
    用户一方置信度(1.0)比,0.6 乃至 0.99 的用户候选都改不动它,更正不会被抹掉。"""
    t, u = uuid4(), uuid4()
    summary = await _summary(store, t, u, ["corrected", None, None])
    assert summary.confidence == 0.5
    # 写回预判用的同一个口径(存储方法,读邻居之后、改库之前)。
    assert await store.user_side_confidence(tenant_id=t, user_id=u, memory_id=summary.id) == 1.0
    await _refuse_both(store, t, u, summary, above=0.6)
    await _refuse_both(store, t, u, summary, above=0.99)


@pytest.mark.asyncio
async def test_user_side_confidence_fails_closed_past_the_depth_bound() -> None:
    """嵌套汇总查到 ``MAX_SUMMARY_DEPTH`` 层还有没查的成员 → 按 1.0(与受保护判断同一个上限)。"""
    t, u = uuid4(), uuid4()
    chain = [_item(t, u, f"level {i}") for i in range(MAX_SUMMARY_DEPTH + 2)]
    rows = {
        row.id: row.model_copy(update={"consolidated_from": [chain[i + 1].id]})
        for i, row in enumerate(chain[:-1])
    }
    rows[chain[-1].id] = chain[-1].model_copy(update={"origin": "user_stated"})

    async def load(ids: Sequence[UUID]) -> list[MemoryItem]:
        return [rows[i] for i in ids if i in rows]

    top = rows[chain[0].id]
    assert await user_side_confidence(top, load) == 1.0
    assert await user_side_confidence(chain[-1].model_copy(update={"origin": None}), load) is None


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
