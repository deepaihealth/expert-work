"""B-300 重做 PR R —— 来源层级(设计 §6.2 / §6.3)与确认日期在存储层的规矩。

同一组用例跑两遍:内存实现(单元)与 SQL 实现(集成,迁移到 head)。

* 层级:用户更正 3 > 用户说的 2 > 用户确认 1 > 来自助手 = 未记录 0;汇总行取成员(一层层往下)
  里最高的层级(第 9 版起不带置信度;「谁能退谁」在 ``test_memory_write_retire_store.py``);
* 整理:用户一方层级不同的成员同簇也算混合,不汇总;
* 确认日期只在用户一方的行上:刷新只改用户一方的、只往后挪;取代时来自助手的新行不带;
  别人在控制台改写(来源置空)时清掉;
* 用户又说过一遍的记忆不算「从没用过的噪音」,不进清理候选。
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config

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
    is_mixed_origin,
    origin_standing,
    origin_tier,
)
from expert_work.persistence.memory.hash import hash_content
from expert_work.protocol import MemoryItem

ALEMBIC_INI = Path(__file__).resolve().parent.parent / "alembic.ini"
_T0 = datetime(2026, 11, 1, tzinfo=UTC)
_ORIGINS = ("corrected", "user_stated", "user_confirmed", "assistant_derived", None)


def _vec(*head: float) -> tuple[float, ...]:
    return tuple(head) + (0.0,) * (EMBEDDING_DIM - len(head))


def _item(
    tenant: UUID,
    user: UUID,
    content: str,
    *,
    origin: str | None = None,
    confidence: float = 0.5,
    created_at: datetime | None = _T0,
    **extra: Any,
) -> MemoryItem:
    """``origin="corrected"`` = 用户更正(来源列不动、置信度 1.0)。"""
    if origin == "corrected":
        origin, confidence = None, 1.0
    return MemoryItem.model_validate(
        {
            "id": uuid4(),
            "tenant_id": tenant,
            "user_id": user,
            "kind": "fact",
            "content": content,
            "embedding": _vec(1.0),
            "content_hash": hash_content(content),
            "origin": origin,
            "confidence": confidence,
            "created_at": created_at,
            "last_used_at": created_at,
            **extra,
        }
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


async def _assert_untouched(store: MemoryStore, t: UUID, u: UUID, target: MemoryItem) -> None:
    got = await _row(store, t, u, target.id)
    assert (got.invalid_at, got.expired_at, got.superseded_by) == (None, None, None)


# ─── 层级与比较(不碰存储)────────────────────────────────────────────────


def test_origin_tier_of_each_effective_origin() -> None:
    t, u = uuid4(), uuid4()
    tiers = [origin_tier(_item(t, u, "x", origin=o)) for o in _ORIGINS]
    assert tiers == [3, 2, 1, 0, 0]


@pytest.mark.asyncio
async def test_summary_standing_is_its_members_top_tier() -> None:
    t, u = uuid4(), uuid4()
    members = [
        _item(t, u, "a", origin="user_confirmed", confidence=0.8),
        _item(t, u, "b", origin="user_stated", confidence=0.4),
        _item(t, u, "c", origin="user_stated", confidence=0.7),
        _item(t, u, "d", origin="assistant_derived", confidence=0.6),
    ]
    rows = {m.id: m for m in members}
    summary = _item(t, u, "summary", confidence=0.4).model_copy(
        update={"consolidated_from": [m.id for m in members]}
    )

    async def load(ids: Sequence[UUID]) -> list[MemoryItem]:
        return [rows[i] for i in ids if i in rows]

    assert await origin_standing(summary, load) == 2
    plain = _item(t, u, "plain", origin="user_confirmed", confidence=0.8)
    assert await origin_standing(plain, load) == 1


@pytest.mark.asyncio
async def test_summary_standing_fails_closed_past_the_depth_bound() -> None:
    """嵌套汇总查到 ``MAX_SUMMARY_DEPTH`` 层还有没查的成员 → 按层级 3。"""
    t, u = uuid4(), uuid4()
    chain = [_item(t, u, f"level {i}") for i in range(MAX_SUMMARY_DEPTH + 2)]
    rows = {
        row.id: row.model_copy(update={"consolidated_from": [chain[i + 1].id]})
        for i, row in enumerate(chain[:-1])
    }
    rows[chain[-1].id] = chain[-1]

    async def load(ids: Sequence[UUID]) -> list[MemoryItem]:
        return [rows[i] for i in ids if i in rows]

    assert await origin_standing(rows[chain[0].id], load) == 3


def test_user_side_members_of_different_tiers_are_mixed() -> None:
    """设计 §6.3:用户一方层级不同的成员同簇也算混合;来源未记录的不算哪一边。"""
    t, u = uuid4(), uuid4()

    def group(*origins: str | None) -> list[MemoryItem]:
        return [_item(t, u, f"m{i}", origin=o) for i, o in enumerate(origins)]

    assert is_mixed_origin(group("user_stated", "user_confirmed"))
    assert is_mixed_origin(group("corrected", "user_stated"))
    assert is_mixed_origin(group("corrected", "user_confirmed", None))
    assert is_mixed_origin(group("assistant_derived", "user_confirmed"))
    assert not is_mixed_origin(group("user_confirmed", "user_confirmed", None))
    assert not is_mixed_origin(group("user_stated", "user_stated"))
    assert not is_mixed_origin(group("assistant_derived", None))


# ─── 存储按成员算层级 ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_store_origin_standing_reads_a_summary_through_its_members(
    store: MemoryStore,
) -> None:
    t, u = uuid4(), uuid4()
    members = [
        _item(t, u, f"m{i}", origin="user_confirmed", confidence=c)
        for i, c in enumerate((0.8, 0.6, 0.7))
    ]
    await store.write(members)
    summary = await store.write_consolidated(
        tenant_id=t, user_id=u, content="summary", embedding=_vec(1.0),
        source_ids=[m.id for m in members],
        source_hashes={m.id: hash_content(m.content) for m in members},
    )  # fmt: skip
    assert await store.origin_standing(tenant_id=t, user_id=u, memory_id=summary.id) == 1
    assert await store.origin_standing(tenant_id=t, user_id=u, memory_id=uuid4()) is None


@pytest.mark.asyncio
async def test_user_side_members_of_different_tiers_are_not_summarised(
    store: MemoryStore,
) -> None:
    t, u = uuid4(), uuid4()
    members = [
        _item(t, u, "a", origin="user_stated"),
        _item(t, u, "b", origin="user_confirmed"),
        _item(t, u, "c"),
    ]
    await store.write(members)
    with pytest.raises(ConsolidationSourcesChangedError):
        await store.write_consolidated(
            tenant_id=t, user_id=u, content="summary", embedding=_vec(1.0),
            source_ids=[m.id for m in members],
            source_hashes={m.id: hash_content(m.content) for m in members},
        )  # fmt: skip


# ─── 确认日期只在用户一方 ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_refresh_confirmed_at_only_moves_a_user_side_row_forward(store: MemoryStore) -> None:
    t, u = uuid4(), uuid4()
    later, earlier = _T0 + timedelta(days=10), _T0 + timedelta(days=3)
    rows = {o: _item(t, u, f"row {o}", origin=o) for o in _ORIGINS}
    await store.write(list(rows.values()))
    for origin, row in rows.items():
        refreshed = await store.refresh_confirmed_at(
            tenant_id=t, user_id=u, memory_id=row.id, confirmed_at=later
        )
        user_side = origin in ("corrected", "user_stated", "user_confirmed")
        assert refreshed is user_side, origin
        assert (await _row(store, t, u, row.id)).confirmed_at == (later if user_side else None)
    # 只往后挪:更早的时间不覆盖。
    stated = rows["user_stated"].id
    assert await store.refresh_confirmed_at(
        tenant_id=t, user_id=u, memory_id=stated, confirmed_at=earlier
    )
    assert (await _row(store, t, u, stated)).confirmed_at == later
    # 别的用户、不存在的 id、已删的行都不动。
    assert not await store.refresh_confirmed_at(
        tenant_id=t, user_id=uuid4(), memory_id=stated, confirmed_at=later
    )
    assert not await store.refresh_confirmed_at(
        tenant_id=t, user_id=u, memory_id=uuid4(), confirmed_at=later
    )
    confirmed = rows["user_confirmed"].id
    assert await store.soft_delete(tenant_id=t, user_id=u, memory_id=confirmed)
    assert not await store.refresh_confirmed_at(
        tenant_id=t, user_id=u, memory_id=confirmed, confirmed_at=later + timedelta(days=1)
    )


@pytest.mark.asyncio
async def test_refresh_confirmed_at_leaves_superseded_and_expired_rows_alone(
    store: MemoryStore,
) -> None:
    """已被取代(``invalid_at``)、已作废(``expired_at``)的行不再是有效记忆,不刷新。"""
    t, u = uuid4(), uuid4()
    replaced, retracted = (
        _item(t, u, "old", origin="user_stated"),
        _item(t, u, "gone", origin="user_stated"),
    )
    await store.write([replaced, retracted])
    new = _item(t, u, "new", origin="user_stated")
    got = await store.supersede(tenant_id=t, user_id=u, old_ids=[replaced.id], new_item=new)
    assert got.retired == (replaced.id,)
    assert await store.expire(tenant_id=t, user_id=u, memory_id=retracted.id)
    for row in (replaced, retracted):
        assert not await store.refresh_confirmed_at(
            tenant_id=t, user_id=u, memory_id=row.id, confirmed_at=_T0
        )
        assert (await _row(store, t, u, row.id)).confirmed_at is None


@pytest.mark.asyncio
async def test_supersede_drops_a_confirmed_date_on_a_non_user_side_new_row(
    store: MemoryStore,
) -> None:
    t, u = uuid4(), uuid4()
    for origin in ("assistant_derived", None):
        old = _item(t, u, f"old {origin}", origin="assistant_derived")
        await store.write([old])
        new = _item(t, u, f"new {origin}", origin=origin, confirmed_at=_T0)
        got = await store.supersede(tenant_id=t, user_id=u, old_ids=[old.id], new_item=new)
        assert got.retired == (old.id,)
        assert (await _row(store, t, u, new.id)).confirmed_at is None


@pytest.mark.asyncio
async def test_console_edit_by_someone_else_clears_the_confirmed_date_only(
    store: MemoryStore,
) -> None:
    """别人在控制台改写:来源置空(不再是用户一方)→ 确认日期清掉;证据指针与事件日期留着
    (它们说的是这条从哪条消息来、关于哪一天,改写措辞不改变这两样)。本人改写、更正都留着。"""
    t, u = uuid4(), uuid4()
    extra: dict[str, Any] = {
        "confirmed_at": _T0,
        "evidence_message_id": "msg-7",
        "evidence_message_index": 7,
        "occurred_on": date(2026, 11, 1),
    }
    other = _item(t, u, "other", origin="user_confirmed", **extra)
    own = _item(t, u, "own", origin="user_confirmed", **extra)
    corrected = _item(t, u, "corrected", origin="user_stated", **extra)
    await store.write([other, own, corrected])
    await store.update_content(
        tenant_id=t, user_id=u, memory_id=other.id, content="other2", embedding=_vec(1.0),
        clear_origin=True, max_confidence=0.99,
    )  # fmt: skip
    await store.update_content(
        tenant_id=t, user_id=u, memory_id=own.id, content="own2", embedding=_vec(1.0),
        origin="user_stated",
    )  # fmt: skip
    await store.update_content(
        tenant_id=t, user_id=u, memory_id=corrected.id, content="corrected2",
        embedding=_vec(1.0), confidence=1.0,
    )  # fmt: skip
    got = await _row(store, t, u, other.id)
    assert got.confirmed_at is None
    assert (got.evidence_message_id, got.evidence_message_index, got.occurred_on) == (
        "msg-7",
        7,
        date(2026, 11, 1),
    )
    assert (await _row(store, t, u, own.id)).confirmed_at == _T0
    assert (await _row(store, t, u, corrected.id)).confirmed_at == _T0


@pytest.mark.asyncio
async def test_a_memory_the_user_restated_is_not_a_purge_candidate(store: MemoryStore) -> None:
    """清理候选 = 从没被召回、从没复核过的老记忆;用户又说过一遍的(有确认日期)不是噪音。"""
    t, u = uuid4(), uuid4()
    old = datetime.now(UTC) - timedelta(days=60)
    plain = _item(t, u, "plain", origin="user_stated", created_at=old)
    restated = _item(t, u, "restated", origin="user_stated", created_at=old, confirmed_at=old)
    await store.write([plain, restated])
    got = await store.list_purge_candidates(tenant_id=t, user_id=u, min_age_days=30, limit=10)
    assert [m.content for m in got] == ["plain"]
