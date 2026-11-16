"""B-300 重做 PR M —— 两个存储实现上的新来源值「用户确认」、证据指针、事件日期、确认日期
(设计 §6.2、§6.3、§9.1、§9.2)。

同一组用例跑两遍:内存实现(单元)与 SQL 实现(集成,迁移到 head)。

* 新写入、取代把四个新字段原样落库、读回;老行(不带它们)读回都是空;
* ``user_confirmed`` 原样落库、读回;它算用户一方:来自助手的候选带保护时改不动它,
  与来自助手的成员同簇不汇总;
* 汇总行不继承成员的指针、事件日期、确认日期;
* 到期复核的基准时间取「创建、最后复核、确认」三者里最晚的。
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
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
    ASSISTANT_DERIVED_MAX_CONFIDENCE,
    NON_CORRECTION_MAX_CONFIDENCE,
    USER_CONFIRMED_MAX_CONFIDENCE,
    USER_SIDE_ORIGINS,
    ConsolidationSourcesChangedError,
    MemoryStore,
    is_mixed_origin,
)
from expert_work.persistence.memory.hash import hash_content
from expert_work.protocol import MemoryItem
from expert_work.protocol.memory_item import MemoryOrigin

ALEMBIC_INI = Path(__file__).resolve().parent.parent / "alembic.ini"
_NOW = datetime.now(UTC)
_CONFIRMED = datetime(2026, 11, 15, 8, 30, tzinfo=UTC)


def _vec(*head: float) -> tuple[float, ...]:
    return tuple(head) + (0.0,) * (EMBEDDING_DIM - len(head))


def _item(
    tenant: UUID,
    user: UUID,
    content: str,
    *,
    origin: MemoryOrigin | None = None,
    created_at: datetime | None = None,
    expected_valid_days: int | None = None,
    **extra: Any,
) -> MemoryItem:
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
            "created_at": created_at,
            "last_used_at": created_at,
            "expected_valid_days": expected_valid_days,
            **extra,
        }
    )


def _evidence(index: int) -> dict[str, Any]:
    """四个字段都随 ``index`` 变,取代时分得清新行带的是候选的还是旧行的。"""
    return {
        "evidence_message_id": f"msg-{index}",
        "evidence_message_index": index,
        "occurred_on": date(2026, 11, 1) + timedelta(days=index),
        "confirmed_at": _CONFIRMED + timedelta(hours=index),
    }


def _expected(index: int) -> tuple[object, ...]:
    return (
        f"msg-{index}",
        index,
        date(2026, 11, 1) + timedelta(days=index),
        _CONFIRMED + timedelta(hours=index),
    )


def _fields(item: MemoryItem) -> tuple[object, ...]:
    return (
        item.evidence_message_id,
        item.evidence_message_index,
        item.occurred_on,
        item.confirmed_at,
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


# ─── 常量与判定(不碰存储)────────────────────────────────────────────────


def test_user_confirmed_cap_sits_between_assistant_and_non_correction() -> None:
    """设计 §6.2:用户确认的置信度上限 0.8 —— 高于来自助手(0.6),低于非更正上限(0.99)。"""
    assert USER_CONFIRMED_MAX_CONFIDENCE == 0.8
    assert ASSISTANT_DERIVED_MAX_CONFIDENCE < USER_CONFIRMED_MAX_CONFIDENCE
    assert USER_CONFIRMED_MAX_CONFIDENCE < NON_CORRECTION_MAX_CONFIDENCE


def test_user_confirmed_is_on_the_user_side() -> None:
    assert USER_SIDE_ORIGINS == {"user_corrected", "user_stated", "user_confirmed"}
    t, u = uuid4(), uuid4()
    confirmed = _item(t, u, "c", origin="user_confirmed")
    derived = _item(t, u, "d", origin="assistant_derived")
    assert is_mixed_origin([confirmed, derived])
    assert not is_mixed_origin([confirmed, _item(t, u, "n")])


# ─── 写入、取代、读回 ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_write_keeps_user_confirmed_and_the_four_new_fields(store: MemoryStore) -> None:
    t, u = uuid4(), uuid4()
    confirmed = _item(t, u, "confirmed", origin="user_confirmed", **_evidence(3))
    legacy = _item(t, u, "legacy", origin="user_stated")
    await store.write([confirmed, legacy])
    got = await _row(store, t, u, confirmed.id)
    assert got.origin == "user_confirmed"
    assert _fields(got) == _expected(3)
    assert _fields(await _row(store, t, u, legacy.id)) == (None, None, None, None)


@pytest.mark.asyncio
async def test_supersede_writes_the_candidates_pointer_and_event_date(store: MemoryStore) -> None:
    """设计 §9.2:``supersede`` 的新行带候选的证据指针与事件日期。"""
    t, u = uuid4(), uuid4()
    old = _item(t, u, "old", origin="user_stated", **_evidence(1))
    await store.write([old])
    new = _item(t, u, "new", origin="user_confirmed", **_evidence(9))
    new_stated = new.model_copy(update={"origin": "user_stated"})
    written = await store.supersede(tenant_id=t, user_id=u, old_ids=[old.id], new_item=new_stated)
    assert written.retired == (old.id,)
    got = await _row(store, t, u, new.id)
    assert got.origin == "user_stated"
    assert _fields(got) == _expected(9)


@pytest.mark.asyncio
async def test_an_assistant_candidate_does_not_retire_a_user_confirmed_target(
    store: MemoryStore,
) -> None:
    """来自助手的候选退不掉用户确认的那一条,它自己照写(设计 §6.3 第一行)。"""
    t, u = uuid4(), uuid4()
    target = _item(t, u, "confirmed", origin="user_confirmed", confidence=0.8)
    await store.write([target])
    new = _item(t, u, "assistant rewrite", origin="assistant_derived")
    result = await store.supersede(tenant_id=t, user_id=u, old_ids=[target.id], new_item=new)
    assert result.inserted and dict(result.refused) == {target.id: "assistant"}
    got = await _row(store, t, u, target.id)
    assert (got.invalid_at, got.expired_at, got.superseded_by) == (None, None, None)


# ─── 整理 ────────────────────────────────────────────────────────────────


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
    )


@pytest.mark.asyncio
async def test_user_confirmed_and_assistant_members_are_not_summarised(
    store: MemoryStore,
) -> None:
    t, u = uuid4(), uuid4()
    members = [
        _item(t, u, "a", origin="assistant_derived"),
        _item(t, u, "b", origin="assistant_derived"),
        _item(t, u, "c", origin="user_confirmed"),
    ]
    with pytest.raises(ConsolidationSourcesChangedError):
        await _consolidate(store, members)


@pytest.mark.asyncio
async def test_summary_has_no_pointer_event_date_or_confirmed_date(store: MemoryStore) -> None:
    """设计 §9.2:汇总行的指针、事件日期、确认日期都空(汇总句是模型新写的)。"""
    t, u = uuid4(), uuid4()
    members = [_item(t, u, f"m{i}", origin="user_stated", **_evidence(i)) for i in range(3)]
    summary = await _consolidate(store, members)
    assert _fields(await _row(store, t, u, summary.id)) == (None, None, None, None)


# ─── 到期复核的基准时间 ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def _set_last_reviewed(store: MemoryStore, memory_id: UUID, when: datetime) -> None:
    """``write`` 不落 ``last_reviewed_at``、``mark_reviewed`` 只写「现在」:直接改行。"""
    if isinstance(store, InMemoryMemoryStore):
        store._rows = [
            r.model_copy(update={"last_reviewed_at": when}) if r.id == memory_id else r
            for r in store._rows
        ]
        return
    assert isinstance(store, SqlMemoryStore)
    async with store._sf() as session:
        await session.execute(
            text("UPDATE memory_item SET last_reviewed_at = :when WHERE id = :id"),
            {"when": when, "id": memory_id},
        )
        await session.commit()


@pytest.mark.asyncio
async def test_due_for_review_counts_from_the_latest_of_created_reviewed_confirmed(
    store: MemoryStore,
) -> None:
    """设计 §9.2:基准 = 创建、最后复核、确认三者里最晚的。都是 60 天前创建、有效期 30 天:

    * 没有确认日期 → 到期;
    * 5 天前用户又说了一遍 → 没到期;
    * 40 天前确认、刚复核过 → 没到期(取最晚的,不是「有确认日期就用它」);
    * 40 天前确认 → 仍到期(确认本身也过了 30 天);
    * 50 天前复核过、5 天前确认 → 没到期(取最晚的,不是「有复核时间就用它」)。"""
    t, u = uuid4(), uuid4()
    created = _NOW - timedelta(days=60)

    def aged(content: str, confirmed: datetime | None) -> MemoryItem:
        return _item(
            t, u, content, origin="user_stated", created_at=created, expected_valid_days=30,
            confirmed_at=confirmed,
        )  # fmt: skip

    plain = aged("plain", None)
    restated = aged("restated", _NOW - timedelta(days=5))
    reviewed = aged("reviewed", _NOW - timedelta(days=40))
    stale = aged("stale", _NOW - timedelta(days=40))
    reconfirmed = aged("reconfirmed", _NOW - timedelta(days=5))
    await store.write([plain, restated, reviewed, stale, reconfirmed])
    assert await store.mark_reviewed(tenant_id=t, user_id=u, memory_id=reviewed.id)
    await _set_last_reviewed(store, reconfirmed.id, _NOW - timedelta(days=50))
    due = await store.list_due_for_review(tenant_id=t, user_id=u, limit=10)
    assert {m.content for m in due} == {"plain", "stale"}
