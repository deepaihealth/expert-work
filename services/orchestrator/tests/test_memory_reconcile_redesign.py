"""B-300 重做 PR R —— 去重合并(设计 §7、附录 B、§11.6 去重合并 / 失败语义 / 层级拦截)。

* 没有相似度门槛:相似度 0.3 的邻居也交给模型;每条候选按向量取召回范围内(带智能体名)最近 5 条;
* 材料带类型、来源、日期、事件日期,已有记忆用本次调用内的短编号 ``e0, e1, …``,程序映射回去,
  不认识的编号按没给决定;
* 更新 / 删除:跨格、不是同一格里按余弦(不是检索顺序)最近的前 2 条 → 新增;事件日期相同且候选
  带更正才可以,相同不是更正、不同、一边没有 → 新增;候选那一行早于目标的记录时间的「更新」→ 新增;
* 「重复」要带目标、目标要在候选的召回范围内;只在用户一方对用户一方、候选那一行在新段里时把目标
  的确认日期刷新为那一行的发送时间,确定性「重复」同样;
* 层级拦截:用户确认改不动用户说的;用户说的可以改用户确认的;
* 两段合并:第二次提炼的候选能取到第一段刚写的行;
* 去重合并内部出错一律抛给调用方,不再退回直接写入。
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage

from expert_work.common.user_input import user_input_kwargs
from expert_work.persistence import InMemoryMemoryStore
from expert_work.protocol import MemoryItem
from expert_work.runtime.cancellation import CancellationToken
from orchestrator.graph_builder import memory as memory_module
from orchestrator.graph_builder.memory import (
    _RECONCILE_SYSTEM,
    ExtractedMemory,
    ReconcileReplyError,
    _Candidate,
    _reconcile_and_apply,
    flush_messages_with_outcome,
)

_APPENDIX_B = """\
You compare newly extracted memories ("candidates") with existing memories about the same
user and decide what to store. Each candidate comes with up to five existing memories that
are closest to it in meaning. Every existing memory has a short id (e0, e1, ...), its kind,
its source, its date, and "event_date" when it is about something that happened on one day.
Each candidate also has the date of the line it was taken from.
For each candidate choose one operation:
- "NOOP": an existing memory already says the same thing; the candidate only rephrases it
  or says less. Set "target_id" to that memory.
- "UPDATE": the candidate is about the same subject or the same occasion as one existing
  memory and brings newer or fuller information. The candidate replaces that memory's text,
  so choose UPDATE only when the candidate still says everything in it that remains true;
  otherwise choose ADD. Set "target_id".
- "DELETE": the candidate says that one existing memory is no longer true and gives
  nothing to put in its place. Set "target_id".
- "ADD": anything else. A memory that is only related — the same topic but another subject,
  person, item or occasion — is not a target; the candidate is an ADD.
UPDATE and DELETE apply only to an existing memory of the same kind.
Two memories with different event dates are different occasions: ADD. A memory with an
"event_date" is changed only by a candidate with "is_correction": true about the same day;
any other candidate about that day is a NOOP when it says the same thing and an ADD
otherwise. A reading, count or status that refers to a different date or time than an
existing memory is a new memory (ADD), not an UPDATE of the earlier one.
A candidate with "is_correction": true changes something said earlier: when an existing
memory still says the earlier value, the candidate is an UPDATE of that memory.
A candidate's "is_correction" says what the candidate does, not where it comes from; its
source is given in "source".
Sources, from strongest to weakest: "corrected by the user", "stated in user messages",
"confirmed by the user", then "from the assistant" and "source not recorded" (equal).
- When an existing memory whose source is at least as strong as the candidate's says the
  same thing, the candidate is a NOOP of that memory, even if a memory with a weaker source
  also matches.
- When the candidate says the same thing as an existing memory with a weaker source, it is
  an UPDATE of that memory.
- When a candidate with "is_correction": true says the same thing as an existing memory
  whose source is at least as strong, and another existing memory still says the earlier
  value, DELETE that other memory; otherwise the candidate is a NOOP.
Respond with ONLY a JSON object, no prose and no code fences:
{"ops": [{"index": <candidate index>, "op": "ADD" | "UPDATE" | "DELETE" | "NOOP",
"target_id": "<short id of the existing memory; omit for ADD>"}]}"""

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
        return json.loads(str(self.calls[call][1].content))


def _ops(*rows: tuple[int, str, str | None]) -> str:
    return json.dumps(
        {
            "ops": [
                {"index": i, "op": op, **({"target_id": t} if t is not None else {})}
                for i, op, t in rows
            ]
        }
    )


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


def test_reconcile_prompt_is_appendix_b_verbatim() -> None:
    assert _RECONCILE_SYSTEM == _APPENDIX_B


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
    llm = _LLM([_ops((0, "UPDATE", "e1"))])
    cand = c.mem("likes green tea, no sugar")
    result = await _reconcile(store, llm, _Candidate(cand))
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


# ─── 更新 / 删除的程序拒绝 ──────────────────────────────────────────────


@pytest.mark.parametrize("op", ["UPDATE", "DELETE"])
@pytest.mark.asyncio
async def test_update_or_delete_across_cells_becomes_add(op: str) -> None:
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
    llm = _LLM([_ops((0, "UPDATE", "e2"), (1, "UPDATE", "e0"))])
    refused, allowed = c.mem("moved to district 9"), c.mem("moved to district 8")
    result = await _reconcile(store, llm, _Candidate(refused), _Candidate(allowed))
    existing = [e["content"] for e in llm.payload()["candidates"][0]["existing"]]
    assert existing == [near.content, mid.content, far.content]
    assert [m.id for m in result.direct] == [refused.id]
    assert _untouched(store, far)
    assert not _untouched(store, near)


@pytest.mark.parametrize(
    ("cand_day", "target_day", "correction", "applied"),
    [
        (1, 1, True, True),
        (1, 1, False, False),
        (1, 2, True, False),
        (1, None, True, False),
        (None, 1, True, False),
        (None, None, False, True),
    ],
    ids=[
        "same-day-correction",
        "same-day-again",
        "other-day",
        "candidate-only",
        "target-only",
        "no-event-date",
    ],
)
@pytest.mark.parametrize("op", ["UPDATE", "DELETE"])
@pytest.mark.asyncio
async def test_event_dates_gate_update_and_delete(
    op: str, cand_day: int | None, target_day: int | None, correction: bool, applied: bool
) -> None:
    c = _Ctx()
    store = InMemoryMemoryStore()

    def day(n: int | None) -> date | None:
        return None if n is None else date(2026, 11, n)

    target = c.mem("the meter read 310", _at(0.95), occurred_on=day(target_day))
    await store.write([target])
    cand = c.mem("the meter read 301", occurred_on=day(cand_day))
    result = await _reconcile(
        store, _LLM([_ops((0, op, "e0"))]), _Candidate(cand, is_correction=correction)
    )
    assert _untouched(store, target) is not applied
    written = [m.id for m in result.direct]
    if applied:
        # 更新落成新行;带更正的删除照旧把候选存下,不带的不存。
        assert written == ([cand.id] if op == "DELETE" and correction else [])
    else:
        assert written == [cand.id]


@pytest.mark.parametrize(
    ("line_offset", "applied"),
    [(timedelta(days=-1), False), (timedelta(days=1), True), (None, True)],
    ids=["older-line", "newer-line", "unknown-line"],
)
@pytest.mark.asyncio
async def test_an_update_from_a_line_older_than_the_target_becomes_add(
    line_offset: timedelta | None, applied: bool
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
        _LLM([_ops((0, "UPDATE", "e0"))]),
        _Candidate(cand, line_at=line_at, in_new_segment=True),
    )
    assert _untouched(store, target) is not applied
    assert [m.id for m in result.direct] == ([] if applied else [cand.id])


@pytest.mark.asyncio
async def test_a_later_restatement_counts_as_the_targets_record_time() -> None:
    """目标是三天前记下、昨天用户又说了一遍:前天那一行的「更新」也算更旧,改为新增。"""
    c = _Ctx()
    store = InMemoryMemoryStore()
    target = c.mem(
        "lives in the north", _at(0.95), origin="user_stated", created_at=_T0,
        confirmed_at=_T0 + timedelta(days=2),
    )  # fmt: skip
    await store.write([target])
    cand = c.mem("lives in the south", origin="user_stated", confidence=0.9)
    result = await _reconcile(
        store, _LLM([_ops((0, "UPDATE", "e0"))]),
        _Candidate(cand, line_at=_T0 + timedelta(days=1), in_new_segment=True),
    )  # fmt: skip
    assert _untouched(store, target)
    assert [m.id for m in result.direct] == [cand.id]


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
        ("user_stated", "user_confirmed", True, _LINE, True),
        ("user_confirmed", "user_stated", True, _LINE, True),
        ("assistant_derived", "user_stated", True, _LINE, False),
        ("user_stated", "assistant_derived", True, _LINE, False),
        ("user_stated", None, True, _LINE, False),
        ("user_stated", "user_stated", False, _LINE, False),
        ("user_stated", "user_stated", True, None, False),
    ],
    ids=[
        "stated-on-confirmed",
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


# ─── 层级拦截 ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(("correction", "kept"), [(False, False), (True, True)])
@pytest.mark.asyncio
async def test_user_confirmed_cannot_update_a_less_certain_user_stated(
    correction: bool, kept: bool
) -> None:
    """审查 H2:0.8 的用户确认改不动 0.5 的用户说的;带更正的按新增并存,不带的丢弃。"""
    c = _Ctx()
    store = InMemoryMemoryStore()
    target = c.mem("the car is a hatchback", _at(0.95), origin="user_stated", confidence=0.5)
    await store.write([target])
    cand = c.mem("the car is a sedan", origin="user_confirmed", confidence=0.8)
    result = await _reconcile(
        store, _LLM([_ops((0, "UPDATE", "e0"))]), _Candidate(cand, is_correction=correction)
    )
    assert _untouched(store, target)
    assert [m.id for m in result.direct] == ([cand.id] if kept else [])


@pytest.mark.asyncio
async def test_user_stated_updates_a_more_certain_user_confirmed() -> None:
    c = _Ctx()
    store = InMemoryMemoryStore()
    target = c.mem("the car is a hatchback", _at(0.95), origin="user_confirmed", confidence=0.8)
    await store.write([target])
    cand = c.mem("the car is a red hatchback", origin="user_stated", confidence=0.3)
    result = await _reconcile(store, _LLM([_ops((0, "UPDATE", "e0"))]), _Candidate(cand))
    assert result.superseded == 1
    assert not _untouched(store, target)


@pytest.mark.asyncio
async def test_identical_text_noop_follows_the_tiers() -> None:
    """确定性「重复」:用户确认候选碰到相同的用户说的 → 不问模型;反过来交给模型(应升级)。"""
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


# ─── 失败语义:一律抛错,不退回直接写入 ─────────────────────────────────


class _FailingStore(InMemoryMemoryStore):
    def __init__(self, fail: str) -> None:
        super().__init__()
        self.fail = fail

    async def retrieve(self, **kwargs: Any) -> list[MemoryItem]:
        if self.fail == "retrieve":
            raise ConnectionError("db down")
        return await super().retrieve(**kwargs)

    async def supersede(self, **kwargs: Any) -> MemoryItem | None:
        if self.fail == "supersede":
            raise ConnectionError("db down")
        return await super().supersede(**kwargs)

    async def expire(self, **kwargs: Any) -> bool:
        if self.fail == "expire":
            raise ConnectionError("db down")
        return await super().expire(**kwargs)

    async def origin_standing(self, **kwargs: Any) -> Any:
        if self.fail == "origin_standing":
            raise ConnectionError("db down")
        return await super().origin_standing(**kwargs)

    async def refresh_confirmed_at(self, **kwargs: Any) -> bool:
        if self.fail == "refresh":
            raise ConnectionError("db down")
        return await super().refresh_confirmed_at(**kwargs)


@pytest.mark.parametrize(
    ("fail", "op"),
    [("retrieve", "ADD"), ("supersede", "UPDATE"), ("expire", "DELETE"), ("refresh", "NOOP")],
)
@pytest.mark.asyncio
async def test_store_errors_inside_reconcile_raise(fail: str, op: str) -> None:
    c = _Ctx()
    store = _FailingStore(fail)
    target = c.mem("speaks French", _at(0.95), origin="user_stated")
    await store.write([target])
    cand = c.mem("speaks French well", origin="user_stated", confidence=0.9)
    with pytest.raises(ConnectionError):
        await _reconcile(
            store, _LLM([_ops((0, op, "e0"))]),
            _Candidate(cand, line_at=_LINE, in_new_segment=True),
        )  # fmt: skip
    assert len(store._rows) == 1


@pytest.mark.asyncio
async def test_a_failed_standing_lookup_for_a_summary_target_raises() -> None:
    c = _Ctx()
    store = _FailingStore("origin_standing")
    summary = c.mem("summary", _at(0.95)).model_copy(update={"consolidated_from": [uuid4()]})
    await store.write([summary])
    with pytest.raises(ConnectionError):
        await _reconcile(store, _LLM([_ops((0, "UPDATE", "e0"))]), _Candidate(c.mem("x")))


class _BrokenLLM:
    async def __call__(self, **kwargs: Any) -> AIMessage:
        raise TimeoutError("model timed out")


@pytest.mark.asyncio
async def test_a_failed_model_call_raises() -> None:
    c = _Ctx()
    store = InMemoryMemoryStore()
    await store.write([c.mem("likes tea", _at(0.95))])
    with pytest.raises(TimeoutError):
        await _reconcile_and_apply(
            [_Candidate(c.mem("likes coffee"))], agent_name=_AGENT, positions={},
            memory_store=store, llm_caller=_BrokenLLM(),  # type: ignore[arg-type]
            token=CancellationToken(), log_label="test",
        )  # fmt: skip


@pytest.mark.parametrize(("reply", "truncated"), [("not json at all", False), (_ops(), True)])
@pytest.mark.asyncio
async def test_an_unusable_or_truncated_reply_raises(reply: str, truncated: bool) -> None:
    c = _Ctx()
    store = InMemoryMemoryStore()
    await store.write([c.mem("likes tea", _at(0.95))])
    with pytest.raises(ReconcileReplyError):
        await _reconcile(
            store, _LLM([reply], truncated=truncated), _Candidate(c.mem("likes coffee"))
        )


# ─── 写回整体:两段合并、出错不直接写 ───────────────────────────────────

PASS1_MARK = "The other side of the conversation is not shown."
PASS2_MARK = "Every line starts with a tag:"


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


async def _flush(store: InMemoryMemoryStore, llm: _RoutingLLM, c: _Ctx) -> Any:
    text = "I like tea"
    user = HumanMessage(
        content=text,
        additional_kwargs=user_input_kwargs(text=text, images=0, files=0, external_blocks=0),
    )
    return await flush_messages_with_outcome(
        [user, AIMessage(content="noted")],
        memory_store=store,
        embedder=_Embedder({"likes tea": _E, "the user likes tea": _at(0.97)}),  # type: ignore[arg-type]
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


@pytest.mark.asyncio
async def test_a_reconcile_failure_writes_nothing_directly() -> None:
    """去重合并的模型调用失败:不再退回直接写入(第二段那条不落库)。"""
    c = _Ctx()
    store = InMemoryMemoryStore()
    llm = _RoutingLLM(_memories("likes tea"), _memories("the user likes tea"), [])
    outcome = await _flush(store, llm, c)
    assert outcome.failed
    assert sorted(r.content for r in _live(store)) == ["likes tea"]


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
        embedder=embedder,  # type: ignore[arg-type]
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
