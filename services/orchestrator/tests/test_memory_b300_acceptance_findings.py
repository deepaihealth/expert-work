"""B-300 测试验收(测试环境,main ``5b8588d0e``)查出的三个问题,按那几次运行的形状用假模型复现。

1. 上下文行被重新提炼(run ``1fec5018`` / ``8051ac10``):两次提炼的产出 ``turn`` 指向
   ``=== new ===`` 之上的行,程序照写(没有证据指针),去重合并在类型不同时判新增 → 用户一方出现
   内容相同的两行。改:两次提炼里 ``turn`` 落在上下文行的一律丢弃、计数 ``context_line_dropped``
   (设计 §4、§5.4、§8.2:上下文行只帮助理解,由之前的任务提炼过)。``turn`` 缺失 / 越界的照旧写、
   指针留空(第一次仍是 ``user_stated``,第二次仍是 ``assistant_derived``)。
2. 同一条用户消息被存两遍(run ``7ee5144b``):第一次存成 ``user_stated``,第二次又把它标成用户
   回答 / 同意;去重合并判「更新」,却被「候选那一行早于目标的记录时间」改成新增 —— 目标几秒前才在
   本任务第一段写下。改:① 先后按目标的**证据消息**的发送时间比(这条消息在本对话里、知道时间时;
   否则按记录时间);② 第二次的 ``user_confirmed`` 候选被判成「重复 / 更新」一条证据消息相同的
   用户一方记忆 → 就是同一句话的重述:NOOP,不刷新确认日期,计数 ``noop_same_evidence``。
3. 用户 2026-11-16「被拦时与旧的并存」(run ``8e5a1f0d``):用户确认的候选被层级拦下、又不是更正
   → 以前丢弃(猫名「蛋黄」丢了,「还没起名字」留着);改为按新增与旧的并存,计数
   ``blocked_update_added_confirmed``。来自助手的候选照旧丢弃。
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from prometheus_client import REGISTRY

from expert_work.common.message_stamp import STAMP_CREATED_AT
from expert_work.common.user_input import user_input_kwargs
from expert_work.persistence import InMemoryMemoryStore
from expert_work.protocol import MemoryItem
from expert_work.runtime.cancellation import CancellationToken
from orchestrator.graph_builder import memory as memory_module
from orchestrator.graph_builder import memory_lines
from orchestrator.graph_builder.memory import _Candidate, _reconcile_and_apply
from orchestrator.graph_builder.memory_lines import ExtractionLine
from orchestrator.graph_builder.memory_slice import SegmentStart

PASS1_MARK = "The other side of the conversation is not shown."
PASS2_MARK = "Each line starts with its number in brackets and a tag:"

_TODAY = datetime(2026, 3, 2, tzinfo=UTC).date()
_T0 = datetime(2026, 3, 2, 1, 0, tzinfo=UTC)
_AGENT = "agent-a"
_E = (1.0, 0.0, 0.0, 0.0)


def _at(cosine: float) -> tuple[float, ...]:
    return (cosine, (1.0 - cosine * cosine) ** 0.5, 0.0, 0.0)


@pytest.fixture(autouse=True)
def _utc_agent_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXPERT_WORK_TIMEZONE", "UTC")


@pytest.fixture
def checks(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    seen: list[tuple[str, str]] = []
    monkeypatch.setattr(
        memory_module,
        "record_memory_extraction_check",
        lambda *, pass_name, check: seen.append((pass_name, check)),
    )
    return seen


def _count(op: str) -> float:
    value = REGISTRY.get_sample_value("expert_work_cm_memory_reconcile_total", {"op": op})
    return value or 0.0


@dataclass
class _Ctx:
    tenant: UUID = field(default_factory=uuid4)
    user: UUID = field(default_factory=uuid4)
    thread: UUID = field(default_factory=uuid4)

    def mem(
        self,
        content: str,
        embedding: tuple[float, ...] = _E,
        *,
        origin: str | None = None,
        confidence: float = 0.5,
        created_at: datetime | None = _T0,
        **extra: Any,
    ) -> MemoryItem:
        return MemoryItem.model_validate(
            {
                "id": uuid4(),
                "tenant_id": self.tenant,
                "user_id": self.user,
                "kind": "fact",
                "content": content,
                "embedding": embedding,
                "origin": origin,
                "confidence": confidence,
                "created_at": created_at,
                "last_used_at": created_at,
                "source_thread_id": str(self.thread),
                **extra,
            }
        )


def _user(text: str, sent_at: datetime, msg_id: str) -> HumanMessage:
    return HumanMessage(
        content=text,
        id=msg_id,
        additional_kwargs=user_input_kwargs(
            text=text, images=0, files=0, external_blocks=0, sent_at=sent_at.isoformat()
        ),
    )


def _assistant(text: str, created_at: datetime, msg_id: str) -> AIMessage:
    return AIMessage(
        content=text, id=msg_id, additional_kwargs={STAMP_CREATED_AT: created_at.isoformat()}
    )


def _reply(*memories: dict[str, Any]) -> str:
    return json.dumps({"memories": list(memories)}, ensure_ascii=False)


def _ops(*rows: tuple[int, str, str | None]) -> str:
    return json.dumps(
        {"ops": [{"index": i, "op": op, **({"target_id": t} if t else {})} for i, op, t in rows]}
    )


@dataclass
class _RoutingLLM:
    """第一次 / 第二次提炼按系统提示词分;其余是去重合并,依次给 ``ops``(不够就判全部新增)。"""

    user_reply: str = '{"memories": []}'
    conversation_reply: str = '{"memories": []}'
    ops: list[str] = field(default_factory=list)
    reconcile_calls: list[list[BaseMessage]] = field(default_factory=list)

    async def __call__(self, *, messages: Sequence[BaseMessage], tools: Sequence[Any]) -> AIMessage:
        del tools
        system = str(messages[0].content)
        if PASS1_MARK in system:
            return AIMessage(content=self.user_reply)
        if PASS2_MARK in system:
            return AIMessage(content=self.conversation_reply)
        self.reconcile_calls.append(list(messages))
        if len(self.reconcile_calls) <= len(self.ops):
            return AIMessage(content=self.ops[len(self.reconcile_calls) - 1])
        count = len(json.loads(str(messages[1].content))["candidates"])
        return AIMessage(content=_ops(*((i, "ADD", None) for i in range(count))))


@dataclass
class _Embedder:
    mapping: dict[str, tuple[float, ...]]

    async def embed(self, texts: Sequence[str], *, tenant_id: UUID) -> list[tuple[float, ...]]:
        del tenant_id
        return [self.mapping[t] for t in texts]


async def _flush(
    c: _Ctx,
    store: InMemoryMemoryStore,
    messages: Sequence[BaseMessage],
    llm: _RoutingLLM,
    embeddings: dict[str, tuple[float, ...]],
    segment: SegmentStart | None = None,
) -> Any:
    return await memory_module.flush_messages_with_outcome(
        messages,
        memory_store=store,
        embedder=_Embedder(embeddings),
        llm_caller=llm,  # type: ignore[arg-type]
        tenant_id=c.tenant,
        user_id=c.user,
        thread_id=c.thread,
        token=CancellationToken(),
        reconcile=True,
        agent_name=_AGENT,
        segment=segment,
    )


def _live(store: InMemoryMemoryStore) -> list[MemoryItem]:
    return [
        r for r in store._rows if r.invalid_at is None and r.expired_at is None and not r.deleted_at
    ]


# ─── 1. 上下文行不再被提炼(run 1fec5018 / 8051ac10)──────────────────────────────


def _two_turns() -> list[BaseMessage]:
    """第一轮由前一个任务提炼过;本任务从第二轮(序号 2)起。"""
    return [
        _user("我下周去成都出差", _T0, "u1"),
        _assistant("好的。回程想订哪天?", _T0 + timedelta(seconds=5), "a1"),
        _user("18号", _T0 + timedelta(minutes=1), "u2"),
        _assistant("好的,按 18 号回程比价。", _T0 + timedelta(minutes=1, seconds=5), "a2"),
    ]


@pytest.mark.asyncio
async def test_memories_on_context_lines_are_dropped_in_both_passes(
    checks: list[tuple[str, str]],
) -> None:
    """run 1fec5018 的形状:前一个任务已记下第一轮的「下周去成都出差」(``user_stated`` 事实);
    本任务两次提炼又从上下文行(第一次 [1]、第二次 [1] / [2])把它抽出来、类型不同,去重合并判新增
    → 多出内容相同的用户一方行。改后:落在上下文行的一律不写、计数;新段里的照写。"""
    c = _Ctx()
    store = InMemoryMemoryStore()
    earlier = c.mem(
        "用户下周去成都出差", origin="user_stated", confidence=0.9,
        evidence_message_id="u1", evidence_message_index=0,
    )  # fmt: skip
    await store.write([earlier])
    llm = _RoutingLLM(
        user_reply=_reply({"kind": "episodic", "content": "用户下周去成都出差", "turn": 1}),
        conversation_reply=_reply(
            {"kind": "episodic", "content": "用户下周去成都出差", "basis": "user_answer",
             "turn": 1},
            {"kind": "fact", "content": "助手问了回程日期", "basis": "assistant", "turn": 2},
            {"kind": "fact", "content": "用户的回程定在 18 号", "basis": "user_answer",
             "turn": 3},
        ),
    )  # fmt: skip
    embeddings = {
        "用户下周去成都出差": _E,
        "助手问了回程日期": _at(0.5),
        "用户的回程定在 18 号": _at(0.3),
    }
    outcome = await _flush(c, store, _two_turns(), llm, embeddings, SegmentStart(message_index=2))
    assert sorted(r.content for r in _live(store)) == ["用户下周去成都出差", "用户的回程定在 18 号"]
    assert outcome.written == 1
    assert checks.count(("user", "context_line_dropped")) == 1
    assert checks.count(("conversation", "context_line_dropped")) == 2
    # 丢弃的不再顺带记「指针留空」「降级」。
    assert [c for c in checks if c[1] != "context_line_dropped"] == []


@pytest.mark.parametrize("basis", ["user_answer", "user_agreement", "assistant", None])
def test_settling_a_context_line_drops_the_memory(basis: object) -> None:
    lines = [
        ExtractionLine(text="回程哪天?", tag="[assistant]", context=True, message_id="a1"),
        ExtractionLine(text="18号", tag="[user]", context=True, message_id="u1"),
        ExtractionLine(text="好的", tag="[assistant]", message_id="a2"),
    ]
    for turn in (1, 2):
        assert memory_lines.settle_conversation(basis, turn, None, lines, today=_TODAY) is None
    assert memory_lines.settle_conversation(basis, 3, None, lines, today=_TODAY) is not None


def test_settling_first_pass_context_line_drops_but_an_unknown_line_keeps() -> None:
    """第一次:上下文行 → 丢;``turn`` 缺失、越界 → 照旧写、``user_stated``、指针留空。"""
    lines = [
        ExtractionLine(text="我下周去成都出差", context=True, message_id="u1"),
        ExtractionLine(text="18号", message_id="u2"),
    ]
    assert memory_lines.settle_user_text(1, None, lines, today=_TODAY) is None
    for turn in (None, 0, 9, "2"):
        settled = memory_lines.settle_user_text(turn, None, lines, today=_TODAY)
        assert settled is not None
        assert (settled.origin, settled.evidence_message_id) == ("user_stated", None)
        assert settled.checks == ("pointer_missing",)


# ─── 2. 同一条用户消息不再存两遍(run 7ee5144b)──────────────────────────────────


def _named_the_cat() -> list[BaseMessage]:
    return [
        _user("帮我想想猫的名字", _T0, "u1"),
        _assistant("叫「蛋黄」怎么样?", _T0 + timedelta(seconds=5), "a1"),
        _user("就叫蛋黄,我的猫以后就叫蛋黄了", _T0 + timedelta(minutes=1), "u2"),
        _assistant("好的,记住了。", _T0 + timedelta(minutes=1, seconds=5), "a2"),
    ]


@pytest.mark.parametrize("op", ["UPDATE", "NOOP"])
@pytest.mark.asyncio
async def test_a_confirmed_restatement_of_the_same_message_is_a_noop(op: str) -> None:
    """run 7ee5144b:第一次把用户整句存成 ``user_stated``(证据 u2);第二次又把同一行标成用户同意。
    去重合并判「更新」第一段刚写的那条(或判「重复」)→ NOOP,只留一条,不刷新确认日期。"""
    c = _Ctx()
    store = InMemoryMemoryStore()
    llm = _RoutingLLM(
        user_reply=_reply({"kind": "fact", "content": "用户的猫叫蛋黄", "turn": 2}),
        conversation_reply=_reply(
            {"kind": "fact", "content": "用户确认猫的名字叫蛋黄", "basis": "user_agreement",
             "turn": 3},
        ),
        ops=[_ops((0, op, "e0"))],
    )  # fmt: skip
    embeddings = {"用户的猫叫蛋黄": _E, "用户确认猫的名字叫蛋黄": _at(0.95)}
    before = _count("noop_same_evidence")
    outcome = await _flush(c, store, _named_the_cat(), llm, embeddings)
    [row] = _live(store)
    assert (row.content, row.origin, row.evidence_message_id) == (
        "用户的猫叫蛋黄",
        "user_stated",
        "u2",
    )
    assert row.confirmed_at is None
    assert outcome.written == 1
    assert _count("noop_same_evidence") == before + 1


@pytest.mark.asyncio
async def test_another_fact_from_the_same_message_is_still_added() -> None:
    """同一条消息里另一件事(模型判新增):照写,不因证据相同被吞掉。"""
    c = _Ctx()
    store = InMemoryMemoryStore()
    llm = _RoutingLLM(
        user_reply=_reply({"kind": "fact", "content": "用户的猫叫蛋黄", "turn": 2}),
        conversation_reply=_reply(
            {"kind": "fact", "content": "用户同意下周带蛋黄去打疫苗", "basis": "user_agreement",
             "turn": 3},
        ),
        ops=[_ops((0, "ADD", None))],
    )  # fmt: skip
    embeddings = {"用户的猫叫蛋黄": _E, "用户同意下周带蛋黄去打疫苗": _at(0.6)}
    outcome = await _flush(c, store, _named_the_cat(), llm, embeddings)
    assert sorted(r.content for r in _live(store)) == [
        "用户同意下周带蛋黄去打疫苗",
        "用户的猫叫蛋黄",
    ]
    assert outcome.written == 2


@pytest.mark.asyncio
async def test_an_assistant_restatement_written_seconds_after_is_not_an_older_line() -> None:
    """同一任务里:第二次的来自助手的候选「更新」第一段几秒前写下的用户说的。那一行(助手回复)
    晚于目标的证据消息,不算更旧 → 走层级拦截、丢弃;以前被「更旧」改成新增,多出一条复述。"""
    c = _Ctx()
    store = InMemoryMemoryStore()
    llm = _RoutingLLM(
        user_reply=_reply({"kind": "fact", "content": "用户的猫叫蛋黄", "turn": 2}),
        conversation_reply=_reply(
            {"kind": "fact", "content": "助手记下用户的猫叫蛋黄", "basis": "assistant", "turn": 4},
        ),
        ops=[_ops((0, "UPDATE", "e0"))],
    )  # fmt: skip
    embeddings = {"用户的猫叫蛋黄": _E, "助手记下用户的猫叫蛋黄": _at(0.95)}
    older, blocked = _count("refused_older_line"), _count("blocked_update_dropped")
    await _flush(c, store, _named_the_cat(), llm, embeddings)
    assert [r.content for r in _live(store)] == ["用户的猫叫蛋黄"]
    assert (_count("refused_older_line"), _count("blocked_update_dropped")) == (older, blocked + 1)


async def _reconcile(
    store: InMemoryMemoryStore,
    llm: Any,
    candidate: _Candidate,
    message_times: dict[str, datetime] | None = None,
) -> Any:
    return await _reconcile_and_apply(
        [candidate],
        agent_name=_AGENT,
        positions={candidate.item.id: 0},
        memory_store=store,
        llm_caller=llm,
        token=CancellationToken(),
        log_label="test",
        **({} if message_times is None else {"message_times": message_times}),
    )


@dataclass
class _FixedLLM:
    reply: str

    async def __call__(self, *, messages: Sequence[BaseMessage], tools: Sequence[Any]) -> AIMessage:
        del messages, tools
        return AIMessage(content=self.reply)


@pytest.mark.parametrize(
    ("evidence_at", "same_thread", "applied"),
    [
        (_T0, True, True),  # 证据消息早于候选那一行:不算更旧(记录时间晚是写回排队的事)
        (_T0 + timedelta(days=1), True, True),  # 同一时刻 = 同一条消息,不算更旧
        (_T0 + timedelta(days=2), True, False),  # 证据消息晚于候选那一行:更旧
        (None, True, False),  # 不知道证据消息的时间 → 按记录时间
        (_T0, False, False),  # 证据在别的对话里 → 按记录时间
    ],
    ids=["evidence-earlier", "same-moment", "evidence-later", "time-unknown", "other-thread"],
)
@pytest.mark.asyncio
async def test_the_older_line_rule_reads_the_targets_evidence_time(
    evidence_at: datetime | None, same_thread: bool, applied: bool
) -> None:
    c = _Ctx()
    store = InMemoryMemoryStore()
    target = c.mem(
        "the car is a hatchback", _at(0.95), origin="user_confirmed", confidence=0.8,
        created_at=_T0 + timedelta(days=5), evidence_message_id="m1",
        **({} if same_thread else {"source_thread_id": str(uuid4())}),
    )  # fmt: skip
    await store.write([target])
    cand = c.mem("the car is a red hatchback", origin="user_stated", confidence=0.9)
    times = {} if evidence_at is None else {"m1": evidence_at}
    result = await _reconcile(
        store,
        _FixedLLM(_ops((0, "UPDATE", "e0"))),
        _Candidate(cand, line_at=_T0 + timedelta(days=1), in_new_segment=True),
        times,
    )
    assert result.superseded == (1 if applied else 0)
    assert [m.id for m in result.direct] == ([] if applied else [cand.id])


@pytest.mark.asyncio
async def test_a_later_restatement_still_counts_against_an_older_line() -> None:
    """证据消息早,但用户之后又说过一遍(确认日期晚于候选那一行):仍算更旧,改为新增。"""
    c = _Ctx()
    store = InMemoryMemoryStore()
    target = c.mem(
        "the car is a hatchback", _at(0.95), origin="user_confirmed", confidence=0.8,
        evidence_message_id="m1", confirmed_at=_T0 + timedelta(days=3),
    )  # fmt: skip
    await store.write([target])
    cand = c.mem("the car is a red hatchback", origin="user_stated", confidence=0.9)
    result = await _reconcile(
        store,
        _FixedLLM(_ops((0, "UPDATE", "e0"))),
        _Candidate(cand, line_at=_T0 + timedelta(days=1), in_new_segment=True),
        {"m1": _T0},
    )
    assert (result.superseded, [m.id for m in result.direct]) == (0, [cand.id])


# ─── 3. 被拦的用户确认与旧的并存(run 8e5a1f0d,用户 2026-11-16)────────────────────


@pytest.mark.asyncio
async def test_a_blocked_user_confirmed_update_is_added_alongside() -> None:
    """用户先说「猫还没起名字」(``user_stated``);后来同意了助手提的「蛋黄」(``user_confirmed``,
    不是更正)。去重合并判「更新」,层级拦下 → 以前丢了「蛋黄」;现在与旧的并存。"""
    c = _Ctx()
    store = InMemoryMemoryStore()
    old = c.mem("用户的猫还没起名字", _at(0.9), origin="user_stated", confidence=0.9)
    await store.write([old])
    cand = c.mem("用户的猫叫蛋黄", origin="user_confirmed", confidence=0.8)
    before = _count("blocked_update_added_confirmed")
    dropped = _count("blocked_update_dropped")
    result = await _reconcile(
        store,
        _FixedLLM(_ops((0, "UPDATE", "e0"))),
        _Candidate(cand, line_at=_T0 + timedelta(days=1), in_new_segment=True),
    )
    assert [m.id for m in result.direct] == [cand.id]
    assert [r.id for r in _live(store)] == [old.id]  # 旧的不动(新的由调用方写)
    assert _count("blocked_update_added_confirmed") == before + 1
    assert _count("blocked_update_dropped") == dropped


@pytest.mark.asyncio
async def test_a_blocked_assistant_update_is_still_dropped() -> None:
    c = _Ctx()
    store = InMemoryMemoryStore()
    old = c.mem("用户的猫还没起名字", _at(0.9), origin="user_stated", confidence=0.9)
    await store.write([old])
    cand = c.mem("助手建议猫叫蛋黄", origin="assistant_derived", confidence=0.6)
    before = _count("blocked_update_dropped")
    result = await _reconcile(store, _FixedLLM(_ops((0, "UPDATE", "e0"))), _Candidate(cand))
    assert result.direct == []
    assert _count("blocked_update_dropped") == before + 1


@dataclass
class _CorrectingLLM:
    """判的那几秒里用户在控制台更正了目标(置信度 1.0):锁里重判时被拦。"""

    store: InMemoryMemoryStore
    target: MemoryItem

    async def __call__(self, *, messages: Sequence[BaseMessage], tools: Sequence[Any]) -> AIMessage:
        del messages, tools
        await self.store.update_content(
            tenant_id=self.target.tenant_id,
            user_id=self.target.user_id,
            memory_id=self.target.id,
            content="corrected by the user",
            embedding=self.target.embedding,
            confidence=1.0,
        )
        return AIMessage(content=_ops((0, "UPDATE", "e0")))


@pytest.mark.asyncio
async def test_a_user_confirmed_update_blocked_under_the_lock_is_added_alongside() -> None:
    c = _Ctx()
    store = InMemoryMemoryStore()
    target = c.mem("猫的名字待定", _at(0.9), origin="assistant_derived", confidence=0.5)
    await store.write([target])
    cand = c.mem("用户的猫叫蛋黄", origin="user_confirmed", confidence=0.8)
    before = _count("blocked_update_added_confirmed")
    result = await _reconcile(store, _CorrectingLLM(store, target), _Candidate(cand))
    assert [m.id for m in result.direct] == [cand.id]
    assert [r.content for r in _live(store)] == ["corrected by the user"]
    assert _count("blocked_update_added_confirmed") == before + 1


@pytest.mark.asyncio
async def test_a_less_certain_user_confirmed_of_the_same_tier_is_still_dropped() -> None:
    """同一层级、置信度更低(``lower_confidence``)不在本次决定里,照旧丢弃。"""
    c = _Ctx()
    store = InMemoryMemoryStore()
    old = c.mem("用户的猫叫蛋黄", _at(0.9), origin="user_confirmed", confidence=0.8)
    await store.write([old])
    cand = c.mem("用户的猫好像叫蛋黄", origin="user_confirmed", confidence=0.5)
    before = _count("lower_confidence_update_dropped")
    result = await _reconcile(store, _FixedLLM(_ops((0, "UPDATE", "e0"))), _Candidate(cand))
    assert result.direct == []
    assert _count("lower_confidence_update_dropped") == before + 1


@pytest.mark.parametrize(
    ("cand_origin", "target_origin", "op", "superseded", "direct"),
    [
        # 用户说的候选、用户确认的目标:照常「更新」(升格),不算重述。
        ("user_stated", "user_confirmed", "UPDATE", 1, False),
        # 来自助手的目标不是用户一方:用户确认照常「更新」它。
        ("user_confirmed", "assistant_derived", "UPDATE", 1, False),
        # 来自助手的候选:走层级拦截(丢弃),不是重述。
        ("assistant_derived", "user_stated", "UPDATE", 0, False),
    ],
    ids=["stated-on-confirmed", "confirmed-on-assistant", "assistant-on-stated"],
)
@pytest.mark.asyncio
async def test_only_a_user_confirmed_on_a_user_side_row_is_a_same_message_restatement(
    cand_origin: str, target_origin: str, op: str, superseded: int, direct: bool
) -> None:
    c = _Ctx()
    store = InMemoryMemoryStore()
    target = c.mem(
        "用户的猫叫蛋黄", _at(0.95), origin=target_origin, confidence=0.6, evidence_message_id="u2"
    )
    await store.write([target])
    cand = c.mem("用户的猫名叫蛋黄", origin=cand_origin, confidence=0.6, evidence_message_id="u2")
    before = _count("noop_same_evidence")
    result = await _reconcile(store, _FixedLLM(_ops((0, op, "e0"))), _Candidate(cand))
    assert (result.superseded, bool(result.direct)) == (superseded, direct)
    assert _count("noop_same_evidence") == before


@pytest.mark.asyncio
async def test_a_same_message_noop_outside_recall_scope_is_still_written() -> None:
    """证据相同、但目标是本智能体的经历而候选是事实:事实不能「重复」进经历(别的智能体召回不到),
    照旧写下。"""
    c = _Ctx()
    store = InMemoryMemoryStore()
    target = c.mem(
        "用户给猫起名蛋黄", _at(0.95), origin="user_stated", kind="episodic", agent_name=_AGENT,
        evidence_message_id="u2",
    )  # fmt: skip
    await store.write([target])
    cand = c.mem(
        "用户的猫叫蛋黄", origin="user_confirmed", confidence=0.8, evidence_message_id="u2"
    )
    result = await _reconcile(store, _FixedLLM(_ops((0, "NOOP", "e0"))), _Candidate(cand))
    assert [m.id for m in result.direct] == [cand.id]


@pytest.mark.asyncio
async def test_a_blocked_user_confirmed_delete_still_does_nothing() -> None:
    """只改被拦的「更新」;被拦的删除(不是更正)照旧什么都不做、候选不存。"""
    c = _Ctx()
    store = InMemoryMemoryStore()
    old = c.mem("用户的猫还没起名字", _at(0.9), origin="user_stated", confidence=0.9)
    await store.write([old])
    cand = c.mem("用户的猫不是没名字", origin="user_confirmed", confidence=0.8)
    before = _count("blocked_delete")
    result = await _reconcile(store, _FixedLLM(_ops((0, "DELETE", "e0"))), _Candidate(cand))
    assert result.direct == []
    assert [r.id for r in _live(store)] == [old.id]
    assert _count("blocked_delete") == before + 1
