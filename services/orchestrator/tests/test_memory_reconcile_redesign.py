"""B-300 重做 PR R —— 去重合并(设计 §7、附录 B、§11.6 去重合并 / 失败语义)。

第 9 版「写和退分开」之后,「谁能退谁」、两张清单的解析、程序改判、事件日期的严规则、历史行、
汇总行在 ``test_memory_write_retire_split.py``;这里留的是第 9 版没有改的那些:

* 没有相似度门槛:相似度 0.3 的邻居也交给模型;每条候选按向量取召回范围内(带智能体名)最近 5 条;
* 材料带类型、来源、日期、事件日期,已有记忆用本次调用内的短编号 ``e0, e1, …``,程序映射回去,
  不认识的编号按没给决定;
* 要退的目标:跨格、不是同一格里按余弦(不是检索顺序)最近的前 2 条 → 不退,候选照写;候选那一行
  早于目标的时间、或不知道那一行的时间 → 不退;
* 「重复」要带目标、目标要在候选的召回范围内;只在用户一方对用户一方、候选那一行在新段里时把目标
  的确认日期刷新为那一行的发送时间,确定性「重复」同样;
* 两段合并:第二次提炼的候选能取到第一段刚写的行;
* 去重合并内部出错抛 ``ReconcileFailedError``,只带「还没写的」;写回不直接写入,抛给任务重试,
  只有最后一次尝试才把「还没写的」交给失败队列。
"""

from __future__ import annotations

import json
import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from prometheus_client import REGISTRY

from expert_work.common.user_input import user_input_kwargs
from expert_work.persistence import InMemoryMemoryStore
from expert_work.protocol import MemoryItem
from expert_work.runtime.cancellation import CancellationToken
from orchestrator.graph_builder import memory as memory_module
from orchestrator.graph_builder.memory import (
    _RECONCILE_SYSTEM,
    ExtractedMemory,
    ReconcileFailedError,
    ReconcileReplyError,
    _Candidate,
    _reconcile_and_apply,
    flush_messages_with_outcome,
)

_T0 = datetime(2026, 11, 1, 9, 0, tzinfo=UTC)
_AGENT = "agent-a"
_E = (1.0, 0.0, 0.0, 0.0)


def _at(cosine: float) -> tuple[float, ...]:
    """与 ``_E`` 余弦为 ``cosine`` 的单位向量。"""
    return (cosine, (1.0 - cosine * cosine) ** 0.5, 0.0, 0.0)


@dataclass
class _Ctx:
    tenant: UUID = field(default_factory=uuid4)
    user: UUID = field(default_factory=uuid4)

    def mem(
        self,
        content: str,
        embedding: tuple[float, ...] = _E,
        *,
        origin: str | None = None,
        confidence: float = 0.5,
        kind: str = "fact",
        agent: str | None = None,
        created_at: datetime | None = _T0,
        **extra: Any,
    ) -> MemoryItem:
        return MemoryItem.model_validate(
            {
                "id": uuid4(),
                "tenant_id": self.tenant,
                "user_id": self.user,
                "kind": kind,
                "agent_name": agent,
                "content": content,
                "embedding": embedding,
                "origin": origin,
                "confidence": confidence,
                "created_at": created_at,
                "last_used_at": created_at,
                **extra,
            }
        )


@dataclass
class _LLM:
    replies: list[str]
    calls: list[list[BaseMessage]] = field(default_factory=list)
    truncated: bool = False

    async def __call__(self, *, messages: Sequence[BaseMessage], tools: Sequence[Any]) -> AIMessage:
        del tools
        self.calls.append(list(messages))
        reply = AIMessage(content=self.replies[len(self.calls) - 1])
        if self.truncated:
            reply.response_metadata = {"finish_reason": "length"}
        return reply

    def payload(self, call: int = 0) -> dict[str, Any]:
        payload: dict[str, Any] = json.loads(str(self.calls[call][1].content))
        return payload


def _ops(*rows: tuple[int, str, str | None]) -> str:
    return json.dumps(
        {
            "ops": [
                {"index": i, "op": op, **({"target_id": t} if t is not None else {})}
                for i, op, t in rows
            ]
        }
    )


def _retiring(*rows: tuple[int, str]) -> str:
    """每条候选写下(``ADD``),并说一条已有记忆被它说全了(``restates``)。"""
    return json.dumps({"ops": [{"index": i, "op": "ADD", "restates": [t]} for i, t in rows]})


#: 晚于用例里种下的记忆的记录时间(``_T0``)。不知道那一行时间的候选退不掉任何旧记忆。
_SAID_AT = _T0 + timedelta(days=30)


def _said(item: MemoryItem) -> _Candidate:
    return _Candidate(item, line_at=_SAID_AT, in_new_segment=True)


async def _reconcile(
    store: InMemoryMemoryStore, llm: _LLM, *candidates: _Candidate, agent: str | None = _AGENT
) -> Any:
    return await _reconcile_and_apply(
        list(candidates),
        agent_name=agent,
        positions={c.item.id: i for i, c in enumerate(candidates)},
        memory_store=store,
        llm_caller=llm,  # type: ignore[arg-type]
        token=CancellationToken(),
        log_label="test",
    )


def _live(store: InMemoryMemoryStore) -> list[MemoryItem]:
    return [
        r for r in store._rows if r.invalid_at is None and r.expired_at is None and not r.deleted_at
    ]


def _untouched(store: InMemoryMemoryStore, target: MemoryItem) -> bool:
    row = next(r for r in store._rows if r.id == target.id)
    return (row.invalid_at, row.expired_at) == (None, None)


# ─── 指令 ──────────────────────────────────────────────────────────────


_DOMAIN_WORDS = (
    "health", "medical", "patient", "blood", "glucose", "pressure", "weight", "doctor",
    "clinic", "coach", "customer", "client", "sales", "legal", "contract", "lawyer",
    "insurance", "健康", "医", "患者", "病", "血", "体重", "教练", "客户", "销售", "合同",
    "律师", "保险",
)  # fmt: skip


def test_reconcile_prompt_has_no_industry_words() -> None:
    lowered = _RECONCILE_SYSTEM.lower()
    assert [w for w in _DOMAIN_WORDS if w in lowered] == []


# ─── 取邻居:不设门槛、5 条、召回范围 ───────────────────────────────────


@pytest.mark.asyncio
async def test_a_distant_neighbour_still_goes_to_the_model() -> None:
    """余弦 0.3 的邻居(旧代码在 0.80 门槛下直接新增、不问模型)也交给模型。"""
    c = _Ctx()
    store = InMemoryMemoryStore()
    old = c.mem("prefers window seats", _at(0.3))
    await store.write([old])
    llm = _LLM([_ops((0, "ADD", None))])
    cand = c.mem("prefers aisle seats on long flights")
    result = await _reconcile(store, llm, _Candidate(cand))
    assert len(llm.calls) == 1
    assert [e["content"] for e in llm.payload()["candidates"][0]["existing"]] == [old.content]
    assert [m.id for m in result.direct] == [cand.id]


class _SpyStore(InMemoryMemoryStore):
    def __init__(self) -> None:
        super().__init__()
        self.retrieve_kwargs: list[dict[str, Any]] = []
        self.reverse = False

    async def retrieve(self, **kwargs: Any) -> list[MemoryItem]:
        self.retrieve_kwargs.append(kwargs)
        rows = await super().retrieve(**kwargs)
        return list(reversed(rows)) if self.reverse else rows


@pytest.mark.asyncio
async def test_neighbours_are_the_five_nearest_within_recall_scope() -> None:
    c = _Ctx()
    store = _SpyStore()
    other_agent = c.mem("other agent's event", _at(0.99), kind="episodic", agent="agent-b")
    own_event = c.mem("own event", _at(0.98), kind="episodic", agent=_AGENT)
    facts = [c.mem(f"fact {i}", _at(0.9 - i / 20)) for i in range(6)]
    await store.write([other_agent, own_event, *facts])
    llm = _LLM([_ops()])
    await _reconcile(store, llm, _Candidate(c.mem("candidate")))
    [kwargs] = store.retrieve_kwargs
    assert (kwargs["agent_name"], kwargs["limit"]) == (_AGENT, 5)
    existing = [e["content"] for e in llm.payload()["candidates"][0]["existing"]]
    assert existing == ["own event", "fact 0", "fact 1", "fact 2", "fact 3"]


# ─── 材料与短编号 ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_material_carries_short_ids_kinds_sources_and_dates() -> None:
    c = _Ctx()
    store = InMemoryMemoryStore()
    stated = c.mem(
        "reading was 41", _at(0.95), origin="user_stated", occurred_on=date(2026, 10, 30),
        created_at=_T0, confirmed_at=_T0 + timedelta(days=3),
    )  # fmt: skip
    derived = c.mem("assistant guess", _at(0.9), origin="assistant_derived", created_at=_T0)
    await store.write([stated, derived])
    llm = _LLM([_ops()])
    first = c.mem("reading was 42", origin="user_confirmed", occurred_on=date(2026, 11, 2))
    second = c.mem("another", _at(0.99), kind="episodic", agent=_AGENT)
    await _reconcile(
        store, llm,
        _Candidate(first, is_correction=True, line_at=_T0 + timedelta(days=1)),
        _Candidate(second),
    )  # fmt: skip
    [cand0, cand1] = llm.payload()["candidates"]
    assert {k: cand0[k] for k in ("index", "kind", "content", "source", "date")} == {
        "index": 0, "kind": "fact", "content": "reading was 42",
        "source": "confirmed by the user", "date": "2026-11-02",
    }  # fmt: skip
    assert (cand0["is_correction"], cand0["event_date"]) == (True, "2026-11-02")
    assert cand0["existing"] == [
        {"id": "e0", "kind": "fact", "content": "reading was 41",
         "source": "stated in user messages", "date": "2026-11-04", "event_date": "2026-10-30"},
        {"id": "e1", "kind": "fact", "content": "assistant guess",
         "source": "from the assistant", "date": "2026-11-01", "event_date": None},
    ]  # fmt: skip
    # 同一条已有记忆在两条候选下用同一个编号;没有那一行的日期 → null。
    assert [e["id"] for e in cand1["existing"]] == ["e0", "e1"]
    assert (cand1["date"], cand1["event_date"]) == (None, None)
    assert str(stated.id) not in json.dumps(llm.payload())


@pytest.mark.asyncio
async def test_a_short_id_maps_back_to_its_memory() -> None:
    c = _Ctx()
    store = InMemoryMemoryStore()
    nearest, second = c.mem("likes tea", _at(0.95)), c.mem("likes green tea", _at(0.9))
    await store.write([nearest, second])
    llm = _LLM([_retiring((0, "e1"))])
    cand = c.mem("likes green tea, no sugar")
    result = await _reconcile(store, llm, _said(cand))
    assert result.superseded == 1
    assert _untouched(store, nearest)
    assert not _untouched(store, second)


@pytest.mark.parametrize("target", ["e7", "uuid", None])
@pytest.mark.asyncio
async def test_an_unknown_or_missing_id_is_no_decision(target: str | None) -> None:
    """不认识的短编号、原始 UUID、「重复」不带目标 → 按没给决定:写候选。"""
    c = _Ctx()
    store = InMemoryMemoryStore()
    old = c.mem("likes tea", _at(0.95))
    await store.write([old])
    raw = str(old.id) if target == "uuid" else target
    op = "NOOP" if target is None else "UPDATE"
    cand = c.mem("likes coffee")
    result = await _reconcile(store, _LLM([_ops((0, op, raw))]), _Candidate(cand))
    assert [m.id for m in result.direct] == [cand.id]
    assert _untouched(store, old)


# ─── 要退的目标:程序的限制 ──────────────────────────────────────────────


@pytest.mark.parametrize("op", ["UPDATE", "DELETE"])
@pytest.mark.asyncio
async def test_a_target_in_another_cell_is_not_retired(op: str) -> None:
    c = _Ctx()
    store = InMemoryMemoryStore()
    event = c.mem("went hiking on saturday", _at(0.95), kind="episodic", agent=_AGENT)
    await store.write([event])
    cand = c.mem("likes hiking")
    result = await _reconcile(store, _LLM([_ops((0, op, "e0"))]), _Candidate(cand))
    assert [m.id for m in result.direct] == [cand.id]
    assert _untouched(store, event)


@pytest.mark.asyncio
async def test_targets_are_ranked_by_cosine_not_by_retrieval_order() -> None:
    """检索返回的顺序被换成最远的在前:只认按余弦重排后的前 2 名。"""
    c = _Ctx()
    store = _SpyStore()
    store.reverse = True
    near, mid, far = (
        c.mem(f"lives in district {i}", _at(s)) for i, s in enumerate((0.95, 0.9, 0.85))
    )
    await store.write([near, mid, far])
    llm = _LLM([_retiring((0, "e2"), (1, "e0"))])
    refused, allowed = c.mem("moved to district 9"), c.mem("moved to district 8")
    result = await _reconcile(store, llm, _said(refused), _said(allowed))
    existing = [e["content"] for e in llm.payload()["candidates"][0]["existing"]]
    assert existing == [near.content, mid.content, far.content]
    # 名次不够的不退,候选照写(助手一方、什么都不退 → 交回调用方直写)。
    assert [m.id for m in result.direct] == [refused.id]
    assert _untouched(store, far)
    assert not _untouched(store, near)


def _day(n: int | None) -> date | None:
    return None if n is None else date(2026, 11, n)


@pytest.mark.parametrize(
    ("cand_day", "target_day", "added"),
    [(1, 2, True), (1, 1, False), (1, None, False), (None, 1, False), (None, None, False)],
    ids=["other-day", "same-day", "candidate-only", "target-only", "no-event-date"],
)
@pytest.mark.asyncio
async def test_a_noop_across_different_event_dates_is_an_add(
    monkeypatch: pytest.MonkeyPatch, cand_day: int | None, target_day: int | None, added: bool
) -> None:
    """复审 MEDIUM(11-16,「方案三」在「重复」这条路上的落法):两边都有事件日期且不同 = 不同的
    两次,模型判「重复」也改为新增并计数;其余照旧是重复。"""
    c = _Ctx()
    store = InMemoryMemoryStore()
    counted: list[str] = []
    monkeypatch.setattr(memory_module, "record_memory_reconcile", lambda *, op: counted.append(op))
    target = c.mem("the meter read 310", _at(0.95), occurred_on=_day(target_day))
    await store.write([target])
    cand = c.mem("the meter reading was 310", occurred_on=_day(cand_day))
    result = await _reconcile(store, _LLM([_ops((0, "NOOP", "e0"))]), _Candidate(cand))
    assert _untouched(store, target)
    assert [m.id for m in result.direct] == ([cand.id] if added else [])
    assert ("noop_other_event_date" in counted) is added


@pytest.mark.parametrize(
    ("target_day", "deterministic"),
    [(2, False), (1, True), (None, True)],
    ids=["other-day", "same-day", "target-undated"],
)
@pytest.mark.asyncio
async def test_identical_text_on_another_event_date_is_not_a_deterministic_noop(
    target_day: int | None, deterministic: bool
) -> None:
    """内容相同、事件日期不同 = 不同的两次:不走确定性「重复」,交给模型(判「重复」也改为新增)。"""
    c = _Ctx()
    store = InMemoryMemoryStore()
    target = c.mem("the meter read 310", occurred_on=_day(target_day))
    await store.write([target])
    cand = c.mem("the meter read 310", occurred_on=_day(1))
    llm = _LLM([_ops((0, "NOOP", "e0"))])
    result = await _reconcile(store, llm, _Candidate(cand))
    assert (llm.calls == []) is deterministic
    assert [m.id for m in result.direct] == ([] if deterministic else [cand.id])


def test_an_unapplied_noop_across_different_event_dates_is_still_written() -> None:
    """收尾时(去重合并出错)还没落的「重复」:日期不同的写下,同一天的不写。"""
    c = _Ctx()
    target = c.mem("the meter read 310", occurred_on=_day(2))
    by_short_id = {"e0": target.id}
    tiers = {target.id: 0}
    other = memory_module._Pending(
        _Candidate(c.mem("read 310", occurred_on=_day(1))), (target,), tiers
    )
    same = memory_module._Pending(
        _Candidate(c.mem("read 310", occurred_on=_day(2))), (target,), tiers
    )
    noop = memory_module._Decision("NOOP", "e0")
    assert memory_module._would_write(other, noop, by_short_id) is True
    assert memory_module._would_write(same, noop, by_short_id) is False


@pytest.mark.parametrize(("cand_day", "target_day"), [(1, None)])
@pytest.mark.asyncio
async def test_the_one_sided_count_needs_the_retirement_to_happen(
    monkeypatch: pytest.MonkeyPatch, cand_day: int | None, target_day: int | None
) -> None:
    """复审 LOW(11-16):只有候选一边有事件日期的计数只在旧行真的被退了之后才记;因来源没退的不记。"""
    c = _Ctx()
    store = InMemoryMemoryStore()
    counted: list[str] = []
    monkeypatch.setattr(memory_module, "record_memory_reconcile", lambda *, op: counted.append(op))
    target = c.mem(
        "the meter read 310", _at(0.95), origin="user_stated", confidence=0.9,
        occurred_on=_day(target_day),
    )  # fmt: skip
    await store.write([target])
    cand = c.mem(
        "the meter read 301", origin="assistant_derived", confidence=0.5, occurred_on=_day(cand_day)
    )
    await _reconcile(store, _LLM([_retiring((0, "e0"))]), _said(cand))
    assert _untouched(store, target)
    assert [o for o in counted if o.startswith("retire_refused_")] == ["retire_refused_assistant"]
    assert [o for o in counted if o.startswith("event_date_one_sided")] == []


@pytest.mark.parametrize(
    ("line_offset", "retired"),
    [(timedelta(days=-1), False), (timedelta(days=1), True), (None, False)],
    ids=["older-line", "newer-line", "unknown-line"],
)
@pytest.mark.asyncio
async def test_a_candidate_from_an_older_or_unknown_line_does_not_retire_the_target(
    line_offset: timedelta | None, retired: bool
) -> None:
    c = _Ctx()
    store = InMemoryMemoryStore()
    recorded = _T0 + timedelta(days=5)
    target = c.mem("lives in the north", _at(0.95), origin="user_stated", created_at=recorded)
    await store.write([target])
    line_at = None if line_offset is None else recorded + line_offset
    cand = c.mem("lives in the south", origin="user_stated", confidence=0.9)
    result = await _reconcile(
        store,
        _LLM([_retiring((0, "e0"))]),
        _Candidate(cand, line_at=line_at, in_new_segment=True),
    )
    assert _untouched(store, target) is not retired
    # 退不退都不影响写:候选一定落库。
    assert (result.superseded, result.direct) == (1, [])
    assert cand.id in {r.id for r in _live(store)}


@pytest.mark.asyncio
async def test_a_later_restatement_counts_as_the_targets_record_time() -> None:
    """目标是三天前记下、昨天用户又说了一遍:前天那一行的候选也算更旧,目标不退。"""
    c = _Ctx()
    store = InMemoryMemoryStore()
    target = c.mem(
        "lives in the north", _at(0.95), origin="user_stated", created_at=_T0,
        confirmed_at=_T0 + timedelta(days=2),
    )  # fmt: skip
    await store.write([target])
    cand = c.mem("lives in the south", origin="user_stated", confidence=0.9)
    await _reconcile(
        store, _LLM([_retiring((0, "e0"))]),
        _Candidate(cand, line_at=_T0 + timedelta(days=1), in_new_segment=True),
    )  # fmt: skip
    assert _untouched(store, target)
    assert cand.id in {r.id for r in _live(store)}


# ─── 「重复」:召回范围与刷新确认日期 ────────────────────────────────────


@pytest.mark.asyncio
async def test_a_fact_is_not_a_duplicate_of_an_event() -> None:
    c = _Ctx()
    store = InMemoryMemoryStore()
    event = c.mem("ran 5 km today", _at(0.95), kind="episodic", agent=_AGENT)
    await store.write([event])
    cand = c.mem("runs 5 km")
    result = await _reconcile(store, _LLM([_ops((0, "NOOP", "e0"))]), _Candidate(cand))
    assert [m.id for m in result.direct] == [cand.id]


@pytest.mark.asyncio
async def test_an_event_can_be_a_duplicate_of_a_fact() -> None:
    c = _Ctx()
    store = InMemoryMemoryStore()
    fact = c.mem("runs 5 km every morning", _at(0.95))
    await store.write([fact])
    cand = c.mem("ran 5 km this morning", kind="episodic", agent=_AGENT)
    result = await _reconcile(store, _LLM([_ops((0, "NOOP", "e0"))]), _Candidate(cand))
    assert result.direct == []


_LINE = _T0 + timedelta(days=4, hours=3)


@pytest.mark.parametrize(
    ("cand_origin", "target_origin", "in_new_segment", "line_at", "refreshed"),
    [
        ("user_stated", "user_stated", True, _LINE, True),
        ("user_confirmed", "user_stated", True, _LINE, True),
        ("assistant_derived", "user_stated", True, _LINE, False),
        ("user_stated", "assistant_derived", True, _LINE, False),
        ("user_stated", None, True, _LINE, False),
        ("user_stated", "user_stated", False, _LINE, False),
        ("user_stated", "user_stated", True, None, False),
    ],
    ids=[
        "stated-on-stated",
        "confirmed-on-stated",
        "assistant-candidate",
        "assistant-target",
        "unrecorded-target",
        "context-line",
        "no-line-date",
    ],
)
@pytest.mark.asyncio
async def test_a_duplicate_refreshes_the_confirmed_date_only_user_side_on_user_side(
    cand_origin: str,
    target_origin: str | None,
    in_new_segment: bool,
    line_at: datetime | None,
    refreshed: bool,
) -> None:
    c = _Ctx()
    store = _RefreshCountingStore()
    target = c.mem("speaks French at home", _at(0.95), origin=target_origin, confidence=0.6)
    await store.write([target])
    cand = c.mem("the user speaks French at home", origin=cand_origin, confidence=0.6)
    result = await _reconcile(
        store, _LLM([_ops((0, "NOOP", "e0"))]),
        _Candidate(cand, line_at=line_at, in_new_segment=in_new_segment),
    )  # fmt: skip
    assert result.direct == []
    got = next(r for r in store._rows if r.id == target.id)
    assert got.confirmed_at == (_LINE if refreshed else None)
    # 存储自己也只改用户一方的行;写回这边不该为不刷新的情形去碰库。
    assert store.refreshes == (1 if refreshed else 0)


class _RefreshCountingStore(InMemoryMemoryStore):
    def __init__(self) -> None:
        super().__init__()
        self.refreshes = 0

    async def refresh_confirmed_at(self, **kwargs: Any) -> bool:
        self.refreshes += 1
        return await super().refresh_confirmed_at(**kwargs)


@pytest.mark.parametrize(("in_new_segment", "refreshed"), [(True, True), (False, False)])
@pytest.mark.asyncio
async def test_an_identical_restatement_refreshes_without_asking_the_model(
    in_new_segment: bool, refreshed: bool
) -> None:
    c = _Ctx()
    store = InMemoryMemoryStore()
    target = c.mem("Speaks French at home.", origin="user_stated")
    await store.write([target])
    llm = _LLM([])
    cand = c.mem("speaks french at home", origin="user_confirmed")
    result = await _reconcile(
        store, llm, _Candidate(cand, line_at=_LINE, in_new_segment=in_new_segment)
    )
    assert (result.direct, llm.calls) == ([], [])
    got = next(r for r in store._rows if r.id == target.id)
    assert got.confirmed_at == (_LINE if refreshed else None)


# ─── 确定性「重复」看来源强弱 ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_identical_text_noop_follows_the_tiers() -> None:
    """确定性「重复」:用户确认候选碰到相同的用户说的 → 不问模型;反过来(已有的那条来源更弱)
    候选写下、那条被退,由程序定(``test_memory_write_retire_split``),模型调用照常发生。"""
    c = _Ctx()
    store = InMemoryMemoryStore()
    await store.write([c.mem("works nights", origin="user_stated")])
    llm = _LLM([])
    await _reconcile(store, llm, _Candidate(c.mem("works nights", origin="user_confirmed")))
    assert llm.calls == []

    c2 = _Ctx()
    store2 = InMemoryMemoryStore()
    await store2.write([c2.mem("works nights", origin="user_confirmed")])
    llm2 = _LLM([_ops((0, "UPDATE", "e0"))])
    await _reconcile(store2, llm2, _Candidate(c2.mem("works nights", origin="user_stated")))
    assert len(llm2.calls) == 1


# ─── 失败语义:去重合并内部出错 → 抛 ReconcileFailedError,带着「还没写的」──────────
#
# ``.unwritten`` 里是要写但还没写的;已落库的、已判重复的不在里面(第 9 版起没有「被拦下丢弃」)。


class _FailingStore(InMemoryMemoryStore):
    def __init__(self, *fails: str, fail_on: UUID | None = None) -> None:
        super().__init__()
        self.fails = set(fails)
        #: ``supersede`` 只在要退这一条时出错(``None`` = 每次都出错)。
        self.fail_on = fail_on

    def _boom(self, what: str, target: UUID | None = None) -> None:
        if what in self.fails and (self.fail_on is None or target in (None, self.fail_on)):
            raise ConnectionError("db down")

    async def retrieve(self, **kwargs: Any) -> list[MemoryItem]:
        self._boom("retrieve")
        return await super().retrieve(**kwargs)

    async def supersede(self, **kwargs: Any) -> Any:
        targets = kwargs.get("old_ids") or [None]
        self._boom("supersede", self.fail_on if self.fail_on in targets else targets[0])
        return await super().supersede(**kwargs)

    async def retire(self, **kwargs: Any) -> Any:
        self._boom("retire")
        return await super().retire(**kwargs)

    async def get_live(self, **kwargs: Any) -> MemoryItem | None:
        self._boom("get_live")
        return await super().get_live(**kwargs)

    async def origin_standing(self, **kwargs: Any) -> Any:
        self._boom("origin_standing")
        return await super().origin_standing(**kwargs)

    async def refresh_confirmed_at(self, **kwargs: Any) -> bool:
        self._boom("refresh")
        return await super().refresh_confirmed_at(**kwargs)

    async def write(self, items: Sequence[MemoryItem]) -> Any:
        self._boom("write")
        return await super().write(items)


async def _failed(store: InMemoryMemoryStore, llm: Any, *candidates: _Candidate) -> Any:
    with pytest.raises(ReconcileFailedError) as raised:
        await _reconcile(store, llm, *candidates)
    return raised.value


@pytest.mark.parametrize(
    ("fail", "reply", "kept"),
    [
        ("retrieve", _ops((0, "ADD", None)), True),
        ("supersede", _retiring((0, "e0")), True),
        ("supersede", _ops((0, "ADD", None)), True),
        # 判成重复的候选本来就不写:核对那条还在不在、刷新确认日期、只退不写时出错都一样。
        ("get_live", _ops((0, "NOOP", "e0")), False),
        ("refresh", _ops((0, "NOOP", "e0")), False),
        ("retire", json.dumps({"ops": [{"index": 0, "op": "NOOP", "target_id": "e0",
                                        "replaces": ["e1"]}]}), False),
    ],
    ids=["retrieve", "supersede", "plain-write", "still-current", "refresh", "retire"],
)  # fmt: skip
@pytest.mark.asyncio
async def test_store_errors_inside_reconcile_raise_with_what_is_unwritten(
    fail: str, reply: str, kept: bool
) -> None:
    c = _Ctx()
    store = _FailingStore(fail)
    target = c.mem("speaks French", _at(0.95), origin="user_stated")
    other = c.mem("speaks German", _at(0.9), origin="user_stated")
    await store.write([target, other])
    cand = c.mem("speaks French well", origin="user_stated", confidence=0.9)
    # 带更正标记:「只退不写」那一项要过预判才会去调存储(第 10 版起第一次的候选退用户说的要带)。
    candidate = _Candidate(cand, is_correction=True, line_at=_LINE, in_new_segment=True)
    error = await _failed(store, _LLM([reply]), candidate)
    assert isinstance(error.__cause__, ConnectionError)
    assert [m.id for m in error.unwritten] == ([cand.id] if kept else [])
    assert len(store._rows) == 2


@pytest.mark.asyncio
async def test_a_failed_standing_lookup_for_a_summary_target_raises() -> None:
    c = _Ctx()
    store = _FailingStore("origin_standing")
    summary = c.mem("summary", _at(0.95)).model_copy(update={"consolidated_from": [uuid4()]})
    await store.write([summary])
    cand = c.mem("x")
    error = await _failed(store, _LLM([_ops((0, "UPDATE", "e0"))]), _Candidate(cand))
    assert isinstance(error.__cause__, ConnectionError)
    assert [m.id for m in error.unwritten] == [cand.id]


class _BrokenLLM:
    async def __call__(self, **kwargs: Any) -> AIMessage:
        raise TimeoutError("model timed out")


@pytest.mark.asyncio
async def test_a_failed_model_call_raises_with_the_candidate_unwritten() -> None:
    c = _Ctx()
    store = InMemoryMemoryStore()
    await store.write([c.mem("likes tea", _at(0.95))])
    cand = c.mem("likes coffee")
    error = await _failed(store, _BrokenLLM(), _Candidate(cand))
    assert isinstance(error.__cause__, TimeoutError)
    assert [m.id for m in error.unwritten] == [cand.id]


@pytest.mark.parametrize(("reply", "truncated"), [("not json at all", False), (_ops(), True)])
@pytest.mark.asyncio
async def test_an_unusable_or_truncated_reply_raises(reply: str, truncated: bool) -> None:
    c = _Ctx()
    store = InMemoryMemoryStore()
    await store.write([c.mem("likes tea", _at(0.95))])
    cand = c.mem("likes coffee")
    error = await _failed(store, _LLM([reply], truncated=truncated), _Candidate(cand))
    assert isinstance(error.__cause__, ReconcileReplyError)
    assert [m.id for m in error.unwritten] == [cand.id]


def _deg(deg: float) -> tuple[float, ...]:
    return (math.cos(math.radians(deg)), math.sin(math.radians(deg)), 0.0, 0.0)


@pytest.mark.asyncio
async def test_a_mid_batch_failure_hands_back_only_the_unwritten() -> None:
    """审查 MEDIUM-2:a1 已写下并退掉 r0、b(来自助手)退不掉用户说的那条但照写(还在等批量写入)、
    a2 写下时库出错、a3 判重复(还没轮到)、d 判写下(还没轮到)—— 交回的是 b、a2 与 d。已落库的、
    重复的不在里面。"""
    c = _Ctx()
    r0, r1 = c.mem("lives north", _deg(0), origin="user_stated"), c.mem("lives east", _deg(40))
    user_row = c.mem("works nights", _deg(80), origin="user_stated", confidence=0.9)
    store = _FailingStore("supersede", fail_on=r1.id)
    await store.write([r0, r1, user_row])
    a1 = c.mem("a1", _deg(0), origin="user_stated", confidence=0.9)
    b = c.mem("b", _deg(80), origin="assistant_derived", confidence=0.6)
    a2 = c.mem("a2", _deg(40), origin="user_stated", confidence=0.9)
    a3 = c.mem("a3", _deg(0), origin="user_stated", confidence=0.9)
    d = c.mem("d", _deg(20), origin="user_stated", confidence=0.9)
    ops = _ops(
        (0, "UPDATE", "e0"), (1, "UPDATE", "e2"), (2, "UPDATE", "e1"), (3, "NOOP", "e0"),
        (4, "ADD", None),
    )  # fmt: skip
    error = await _failed(store, _LLM([ops]), *(_said(m) for m in (a1, b, a2, a3, d)))
    assert [m.id for m in error.unwritten] == [b.id, a2.id, d.id]
    assert error.superseded == 1
    assert not _untouched(store, r0)
    assert _untouched(store, r1) and _untouched(store, user_row)

    # PR S(§7.6)—— 写回不直接写下 a2:没有任务重试的这一次(本轮内写回)等于最后一次尝试,
    # 失败队列只收 a2(已落库的 a1、重复的 a3 不重建);出错走失败分支,条数是 0、failed 为真。
    c2 = _Ctx()
    s0 = c2.mem("lives north", _deg(0), origin="user_stated")
    s1 = c2.mem("lives east", _deg(40))
    store2 = _FailingStore("supersede", fail_on=s1.id)
    await store2.write([s0, s1])
    llm = _RoutingLLM(
        _user_memories(("a1", 0.9), ("a2", 0.9), ("a3", 0.9)),
        '{"memories": []}',
        [_ops((0, "UPDATE", "e0"), (1, "UPDATE", "e1"), (2, "NOOP", "e0"))],
    )
    dlq = _RecordingDLQ()
    embeddings = {"a1": _deg(0), "a2": _deg(40), "a3": _deg(0)}
    outcome = await _flush_with_dlq(store2, llm, c2, embeddings, dlq)
    assert (outcome.written, outcome.failed, dlq.enqueued) == (0, True, [[("fact", "a2")]])
    assert sorted(r.content for r in _live(store2)) == ["a1", "lives east"]


@pytest.mark.asyncio
async def test_a_short_id_of_another_candidates_neighbour_is_no_decision() -> None:
    """短编号在一次调用内全局编,但每条候选只能指向自己的邻居;指向别人的 → 按没给决定写候选。"""
    c = _Ctx()
    store = InMemoryMemoryStore()

    rows = [c.mem(f"row {d}", _deg(d)) for d in (0, 10, 20, 30, 40, 50)]
    await store.write(rows)
    first, second = c.mem("first", _deg(-5)), c.mem("second", _deg(55))
    llm = _LLM([_ops((0, "UPDATE", "e5"), (1, "ADD", None))])
    result = await _reconcile(store, llm, _Candidate(first), _Candidate(second))
    [cand0, cand1] = llm.payload()["candidates"]
    assert "e5" not in {e["id"] for e in cand0["existing"]}
    assert {e["id"]: e["content"] for e in cand1["existing"]}["e5"] == "row 50"
    assert {m.id for m in result.direct} == {first.id, second.id}
    assert all(_untouched(store, r) for r in rows)


@pytest.mark.asyncio
async def test_a_naive_line_time_is_read_as_utc() -> None:
    """时间比较不因有没有时区报错:没有时区的按 UTC 读。"""
    c = _Ctx()
    store = InMemoryMemoryStore()
    recorded = _T0 + timedelta(days=5)
    target = c.mem("lives north", _at(0.95), origin="user_stated", created_at=recorded)
    await store.write([target])
    cand = c.mem("lives south", origin="user_stated", confidence=0.9)
    naive_older = (recorded - timedelta(hours=1)).replace(tzinfo=None)
    await _reconcile(store, _LLM([_retiring((0, "e0"))]), _Candidate(cand, line_at=naive_older))
    assert _untouched(store, target)
    assert cand.id in {r.id for r in _live(store)}


# ─── 写回整体:两段合并、出错时失败队列只收还没写的 ─────────────────────────────────

PASS1_MARK = "The other side of the conversation is not shown."
PASS2_MARK = "Each line starts with its number in brackets and a tag:"


@dataclass
class _RoutingLLM:
    user_reply: str
    conversation_reply: str
    ops_replies: list[str] = field(default_factory=list)
    reconcile_calls: list[list[BaseMessage]] = field(default_factory=list)

    async def __call__(self, *, messages: Sequence[BaseMessage], tools: Sequence[Any]) -> AIMessage:
        del tools
        system = str(messages[0].content)
        if PASS1_MARK in system:
            return AIMessage(content=self.user_reply)
        if PASS2_MARK in system:
            return AIMessage(content=self.conversation_reply)
        self.reconcile_calls.append(list(messages))
        if len(self.reconcile_calls) > len(self.ops_replies):
            raise TimeoutError("reconcile model timed out")
        return AIMessage(content=self.ops_replies[len(self.reconcile_calls) - 1])


@dataclass
class _Embedder:
    mapping: dict[str, tuple[float, ...]]

    async def embed(self, texts: Sequence[str], *, tenant_id: UUID) -> list[tuple[float, ...]]:
        del tenant_id
        return [self.mapping[t] for t in texts]


def _memories(content: str) -> str:
    return json.dumps({"memories": [{"kind": "fact", "content": content}]})


def _user_message(text: str) -> HumanMessage:
    return HumanMessage(
        content=text,
        additional_kwargs=user_input_kwargs(
            text=text, images=0, files=0, external_blocks=0, sent_at=_SAID_AT.isoformat()
        ),
    )


async def _flush(store: InMemoryMemoryStore, llm: _RoutingLLM, c: _Ctx) -> Any:
    return await flush_messages_with_outcome(
        [_user_message("I like tea"), AIMessage(content="noted")],
        memory_store=store,
        embedder=_Embedder({"likes tea": _E, "the user likes tea": _at(0.97)}),
        llm_caller=llm,  # type: ignore[arg-type]
        tenant_id=c.tenant,
        user_id=c.user,
        thread_id=uuid4(),
        token=CancellationToken(),
        reconcile=True,
        agent_name=_AGENT,
    )


@pytest.mark.asyncio
async def test_the_second_pass_is_reconciled_against_what_the_first_just_wrote() -> None:
    c = _Ctx()
    store = InMemoryMemoryStore()
    llm = _RoutingLLM(
        _memories("likes tea"), _memories("the user likes tea"), [_ops((0, "NOOP", "e0"))]
    )
    outcome = await _flush(store, llm, c)
    # 第一段没有邻居、直接写;第二段把第一段刚写的那条当邻居交给模型,判成重复。
    assert len(llm.reconcile_calls) == 1
    payload = json.loads(str(llm.reconcile_calls[0][1].content))
    assert [e["content"] for e in payload["candidates"][0]["existing"]] == ["likes tea"]
    assert payload["candidates"][0]["content"] == "the user likes tea"
    assert sorted(r.content for r in _live(store)) == ["likes tea"]
    assert (outcome.written, outcome.failed) == (1, False)


@dataclass
class _RecordingDLQ:
    enqueued: list[list[tuple[str, str]]] = field(default_factory=list)

    async def enqueue(self, **kwargs: Any) -> None:
        self.enqueued.append(list(kwargs["extracted"]))


def _reconcile_count(op: str) -> float:
    value = REGISTRY.get_sample_value("expert_work_cm_memory_reconcile_total", {"op": op})
    return value or 0.0


async def _flush_with_dlq(
    store: InMemoryMemoryStore,
    llm: _RoutingLLM,
    c: _Ctx,
    embeddings: dict[str, tuple[float, ...]],
    dlq: _RecordingDLQ,
) -> Any:
    return await flush_messages_with_outcome(
        [_user_message("my words"), AIMessage(content="noted")],
        memory_store=store,
        embedder=_Embedder(embeddings),
        llm_caller=llm,  # type: ignore[arg-type]
        tenant_id=c.tenant,
        user_id=c.user,
        thread_id=uuid4(),
        token=CancellationToken(),
        reconcile=True,
        agent_name=_AGENT,
        dlq=dlq,  # type: ignore[arg-type]
    )


def _user_memories(*rows: tuple[str, float]) -> str:
    return json.dumps(
        {
            "memories": [
                {"kind": "fact", "content": t, "confidence": conf, "turn": 1} for t, conf in rows
            ]
        }
    )


@pytest.mark.asyncio
async def test_a_reconcile_failure_raises_to_the_retry_and_dlqs_only_on_the_last_attempt() -> None:
    """PR S(§7.6,替换 R 的过渡做法)—— 去重合并的模型超时:不再直接写入。后台任务的非最后一次
    尝试抛给任务重试(什么都不写、不进失败队列);最后一次尝试(给了失败队列)把还没写的交给失败
    队列,``failed`` 为真。旧行一直不动。(过渡做法下,一条还没执行的纯「删除」会被当成新增写下;
    抛错之后这个问题也随之消失。)"""
    c = _Ctx()
    store = InMemoryMemoryStore()
    old = c.mem("lives in the north", _E)
    await store.write([old])
    embeddings = {"lives in the south": _at(0.9)}
    before = _reconcile_count("reconcile_failed")

    async def flush(dlq: _RecordingDLQ | None) -> Any:
        llm = _RoutingLLM(_user_memories(("lives in the south", 0.9)), '{"memories": []}', [])
        return await flush_messages_with_outcome(
            [_user_message("my words"), AIMessage(content="noted")],
            memory_store=store,
            embedder=_Embedder(embeddings),
            llm_caller=llm,  # type: ignore[arg-type]
            tenant_id=c.tenant,
            user_id=c.user,
            thread_id=uuid4(),
            token=CancellationToken(),
            reconcile=True,
            agent_name=_AGENT,
            dlq=dlq,  # type: ignore[arg-type]
            raise_on_failure=True,
        )

    with pytest.raises(ReconcileFailedError):
        await flush(None)
    assert [r.content for r in _live(store)] == ["lives in the north"]
    dlq = _RecordingDLQ()
    outcome = await flush(dlq)
    assert (outcome.written, outcome.failed) == (0, True)
    assert dlq.enqueued == [[("fact", "lives in the south")]]
    assert [r.content for r in _live(store)] == ["lives in the north"]
    assert _untouched(store, old)
    assert _reconcile_count("reconcile_failed") == before + 2


@pytest.mark.asyncio
async def test_after_a_reconcile_failure_the_dlq_gets_only_the_unwritten() -> None:
    """审查 MEDIUM-2,失败队列接上:a1 已写下并退掉旧行、a2 写下时出错、a3 判重复 ——
    失败队列只收到 a2(已落库的 a1、重复的 a3 不会被重建成新行)。"""
    c = _Ctx()
    r0, r1 = c.mem("lives north", _deg(0), origin="user_stated"), c.mem("lives east", _deg(40))
    store = _FailingStore(fail_on=r1.id)
    await store.write([r0, r1])
    store.fails = {"supersede", "write"}
    ops = _ops((0, "UPDATE", "e0"), (1, "UPDATE", "e1"), (2, "NOOP", "e0"))
    llm = _RoutingLLM(
        _user_memories(("a1", 0.9), ("a2", 0.9), ("a3", 0.9)), '{"memories": []}', [ops]
    )
    dlq = _RecordingDLQ()
    outcome = await _flush_with_dlq(
        store, llm, c, {"a1": _deg(0), "a2": _deg(40), "a3": _deg(0)}, dlq
    )
    assert outcome.failed
    assert dlq.enqueued == [[("fact", "a2")]]
    assert not _untouched(store, r0)


@pytest.mark.asyncio
async def test_extraction_hooks_reach_the_reconcile_and_the_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PR X 要填的三样(事件日期、那一行的发送时间、在不在新段)从提炼结果一路带到去重合并的
    材料、确认日期的刷新和落库的事件日期;X 之前它们是默认值(不知道)。"""
    c = _Ctx()
    store = InMemoryMemoryStore()
    restated = c.mem("likes tea", origin="user_stated")
    await store.write([restated])
    line_at = _T0 + timedelta(days=9)

    async def extracted(*_args: Any, **_kwargs: Any) -> tuple[list[ExtractedMemory], bool]:
        hooks: dict[str, Any] = {"line_at": line_at, "in_new_segment": True}
        return [
            ExtractedMemory(
                kind="fact", content="the user likes tea", origin="user_stated",
                event_date=date(2026, 11, 9), **hooks,
            ),
            ExtractedMemory(
                kind="fact", content="meter read 310", origin="user_stated",
                event_date=date(2026, 11, 8), **hooks,
            ),
        ], False  # fmt: skip

    monkeypatch.setattr(memory_module, "_extract_two_passes", extracted)
    llm = _RoutingLLM("", "", [_ops((0, "NOOP", "e0"), (1, "ADD", None))])
    embedder = _Embedder({"the user likes tea": _at(0.97), "meter read 310": _at(0.9)})
    outcome = await flush_messages_with_outcome(
        [HumanMessage(content="x"), AIMessage(content="y")],
        memory_store=store,
        embedder=embedder,
        llm_caller=llm,  # type: ignore[arg-type]
        tenant_id=c.tenant,
        user_id=c.user,
        thread_id=uuid4(),
        token=CancellationToken(),
        reconcile=True,
        agent_name=_AGENT,
    )
    assert (outcome.written, outcome.failed) == (1, False)
    [first, second] = json.loads(str(llm.reconcile_calls[0][1].content))["candidates"]
    assert (first["date"], first["event_date"]) == ("2026-11-10", "2026-11-09")
    assert second["event_date"] == "2026-11-08"
    assert next(r for r in store._rows if r.id == restated.id).confirmed_at == line_at
    [reading] = [r for r in store._rows if r.content == "meter read 310"]
    assert reading.occurred_on == date(2026, 11, 8)
