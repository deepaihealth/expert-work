"""B-300 测试验收(测试环境,main ``5b8588d0e``)查出的三个问题,按那几次运行的形状用假模型复现。

1. 上下文行被重新提炼(run ``1fec5018`` / ``8051ac10``):两次提炼的产出 ``turn`` 指向
   ``=== new ===`` 之上的行,程序照写(没有证据指针),去重合并在类型不同时判新增 → 用户一方出现
   内容相同的两行。改:两次提炼里 ``turn`` 落在上下文行的一律丢弃、计数 ``context_line_dropped``
   (设计 §4、§5.4、§8.2:上下文行只帮助理解,由之前的任务提炼过)。``turn`` 缺失 / 越界的照旧写、
   指针留空(第一次仍是 ``user_stated``,第二次仍是 ``assistant_derived``)。唯一的例外(用户 11-16
   「这种情况照旧保存」):用户在新段第一行回答 / 同意上下文最后一行的助手提议、``turn`` 被指在
   助手那一行 → 照旧存成来自助手,计数 ``context_line_downgraded``。
2. 同一条用户消息被存两遍(run ``7ee5144b``):第一次存成 ``user_stated``,第二次又把它标成用户
   回答 / 同意;去重合并判「更新」,却被「候选那一行早于目标的记录时间」改成新增 —— 目标几秒前才在
   本任务第一段写下。改:① 先后按目标的**证据消息**的发送时间比(这条消息在本对话里、知道时间时;
   否则按记录时间);② 第二次的 ``user_confirmed`` 候选被模型判成「重复」一条证据消息相同、事件
   日期不冲突的用户一方记忆 → 就是同一句话的重述:NOOP,不刷新确认日期,计数
   ``noop_same_evidence``。判「更新」的照常走(用户 11-16「只拦真重复」,第 4 节)。
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

from expert_work.common.conversation_channel import SUPERSEDED_BY
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


@pytest.mark.asyncio
async def test_a_confirmed_restatement_of_the_same_message_is_a_noop() -> None:
    """run 7ee5144b:第一次把用户整句存成 ``user_stated``(证据 u2);第二次又把同一行标成用户同意。
    去重合并判「重复」第一段刚写的那条 → NOOP,只留一条,不刷新确认日期。判「更新」的不在这条捷径里
    (用户 11-16「只拦真重复」),见 ``test_a_same_message_update_*``。"""
    c = _Ctx()
    store = InMemoryMemoryStore()
    llm = _RoutingLLM(
        user_reply=_reply({"kind": "fact", "content": "用户的猫叫蛋黄", "turn": 2}),
        conversation_reply=_reply(
            {"kind": "fact", "content": "用户确认猫的名字叫蛋黄", "basis": "user_agreement",
             "turn": 3},
        ),
        ops=[_ops((0, "NOOP", "e0"))],
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
        message_times=message_times,
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


# ─── 4. 独立审查(11-16)与用户拍板「只拦真重复」「这种情况照旧保存」────────────────────


@pytest.mark.parametrize(
    ("op", "counter"), [("UPDATE", "refused_event_date"), ("NOOP", "noop_other_event_date")]
)
@pytest.mark.asyncio
async def test_same_evidence_with_different_event_dates_is_added(op: str, counter: str) -> None:
    """M-1:用户一句话里说了「昨天 130,今天 125」;第一次记下「昨天 130」(带日期),第二次给出
    「今天 125」,证据是同一条消息、事件日期不同 —— 是两次读数,不是重述:模型判「更新」被事件日期
    规则改为新增,判「重复」按 ``noop_other_event_date`` 新增。"""
    c = _Ctx()
    store = InMemoryMemoryStore()
    target = c.mem(
        "昨天测得读数 130", _at(0.95), origin="user_stated", confidence=0.9,
        evidence_message_id="u2", occurred_on=_TODAY - timedelta(days=1),
    )  # fmt: skip
    await store.write([target])
    cand = c.mem(
        "今天测得读数 125", origin="user_confirmed", confidence=0.8,
        evidence_message_id="u2", occurred_on=_TODAY,
    )  # fmt: skip
    same, before = _count("noop_same_evidence"), _count(counter)
    result = await _reconcile(
        store,
        _FixedLLM(_ops((0, op, "e0"))),
        _Candidate(cand, line_at=_T0, in_new_segment=True),
        {"u2": _T0},
    )
    assert ([m.id for m in result.direct], result.superseded) == ([cand.id], 0)
    assert (_count("noop_same_evidence"), _count(counter)) == (same, before + 1)


def _picked_the_class_time() -> list[BaseMessage]:
    return [
        _user("帮我排下周的课", _T0, "u1"),
        _assistant("上午还是下午上课?", _T0 + timedelta(seconds=5), "a1"),
        _user("上午吧,下午我要接孩子", _T0 + timedelta(minutes=1), "u2"),
        _assistant("好的,都排在上午。", _T0 + timedelta(minutes=1, seconds=5), "a2"),
    ]


_PICKUP = "用户下午要接孩子"
_MORNING = "用户喜欢上午上课,因为下午要接孩子"


@pytest.mark.asyncio
async def test_a_same_message_update_blocked_by_tier_is_added_alongside() -> None:
    """M-2(用户 11-16「只拦真重复」):同一条消息,第一次记下「下午要接孩子」(``user_stated``),
    第二次给出「喜欢上午上课,因为下午接孩子」(``user_confirmed``),模型判「更新」那条 —— 不再当成
    重述吞掉:层级拦下 → 与旧的并存(``blocked_update_added_confirmed``),「喜欢上午」不丢。"""
    c = _Ctx()
    store = InMemoryMemoryStore()
    llm = _RoutingLLM(
        user_reply=_reply({"kind": "fact", "content": _PICKUP, "turn": 2}),
        conversation_reply=_reply(
            {"kind": "fact", "content": _MORNING, "basis": "user_answer", "turn": 3}
        ),
        ops=[_ops((0, "UPDATE", "e0"))],
    )
    embeddings = {_PICKUP: _E, _MORNING: _at(0.9)}
    same, added = _count("noop_same_evidence"), _count("blocked_update_added_confirmed")
    outcome = await _flush(c, store, _picked_the_class_time(), llm, embeddings)
    rows = {r.content: r for r in _live(store)}
    assert sorted(rows) == sorted([_PICKUP, _MORNING])
    assert (rows[_PICKUP].origin, rows[_MORNING].origin) == ("user_stated", "user_confirmed")
    assert rows[_MORNING].evidence_message_id == "u2"
    assert outcome.written == 2
    assert _count("noop_same_evidence") == same
    assert _count("blocked_update_added_confirmed") == added + 1

    # 任务重试再跑一遍:内容相同 → 确定性「重复」,不再多出行。
    again = await _flush(c, store, _picked_the_class_time(), _RoutingLLM(
        user_reply=llm.user_reply, conversation_reply=llm.conversation_reply
    ), embeddings)  # fmt: skip
    assert sorted(r.content for r in _live(store)) == sorted([_PICKUP, _MORNING])
    assert again.written == 0

    # 重试换了说法、模型判「更新」并存的那条(同层级、同一条证据):原地更新,仍是两行。
    reworded = "用户偏好上午上课,下午要接孩子"
    retry = _RoutingLLM(
        user_reply=llm.user_reply,
        conversation_reply=_reply(
            {"kind": "fact", "content": reworded, "basis": "user_answer", "turn": 3}
        ),
        ops=[_ops((0, "UPDATE", "e0"))],
    )
    await _flush(c, store, _picked_the_class_time(), retry, {**embeddings, reworded: _at(0.9)})
    assert sorted(r.content for r in _live(store)) == sorted([_PICKUP, reworded])


@pytest.mark.asyncio
async def test_a_same_message_update_of_the_same_tier_is_applied() -> None:
    """M-2:证据相同、同层级(用户确认改用户确认),模型判「更新」→ 照常更新(同一条消息不算更旧)。"""
    c = _Ctx()
    store = InMemoryMemoryStore()
    target = c.mem(
        "用户喜欢上午上课", _at(0.95), origin="user_confirmed", confidence=0.8,
        evidence_message_id="u2", created_at=_T0 + timedelta(minutes=2),
    )  # fmt: skip
    await store.write([target])
    cand = c.mem(_MORNING, origin="user_confirmed", confidence=0.8, evidence_message_id="u2")
    same = _count("noop_same_evidence")
    result = await _reconcile(
        store,
        _FixedLLM(_ops((0, "UPDATE", "e0"))),
        _Candidate(cand, line_at=_T0 + timedelta(minutes=1), in_new_segment=True),
        {"u2": _T0 + timedelta(minutes=1)},
    )
    assert (result.superseded, result.direct) == (1, [])
    assert [r.content for r in _live(store)] == [_MORNING]
    assert _count("noop_same_evidence") == same


def _pending(cand: MemoryItem, target: MemoryItem) -> memory_module._Pending:
    return memory_module._Pending(_Candidate(cand), (target,), frozenset())


@pytest.mark.parametrize(
    ("op", "cand_day", "writes"),
    [
        ("NOOP", None, False),  # 同一条消息的重述:失败收尾也不写
        ("NOOP", _TODAY, True),  # 事件日期不同:另一次,收尾时按新增写
        ("UPDATE", None, True),  # 判「更新」的照常(还没执行的更新按新增写)
    ],
    ids=["noop", "noop-other-date", "update"],
)
def test_would_write_follows_the_same_evidence_rule(
    op: str, cand_day: object, writes: bool
) -> None:
    """L-1:去重合并出错时,还没落完的候选要不要写与落库时同一条规矩 —— 不进 DLQ 写成重复。"""
    c = _Ctx()
    target = c.mem(
        "昨天测得读数 130", origin="user_stated", confidence=0.9, evidence_message_id="u2",
        occurred_on=_TODAY - timedelta(days=1),
    )  # fmt: skip
    cand = c.mem(
        "读数 130", origin="user_confirmed", confidence=0.8, evidence_message_id="u2",
        occurred_on=cand_day or (_TODAY - timedelta(days=1)),
    )  # fmt: skip
    assert memory_module._would_write(_pending(cand, target), op, "e0", {"e0": target.id}) is writes


def test_message_times_include_superseded_messages() -> None:
    """L-2:重新生成 / 编辑重发之后,旧那轮的消息被取代、不进提炼 —— 但已有记忆的证据可能指着它,
    发送时间仍要查得到。"""
    old = _user("我的猫叫蛋黄", _T0, "u-old")
    old.additional_kwargs[SUPERSEDED_BY] = "run-2"
    old_reply = _assistant("好名字", _T0 + timedelta(seconds=5), "a-old")
    old_reply.additional_kwargs[SUPERSEDED_BY] = "run-2"
    messages = [
        old,
        old_reply,
        _user("我的猫叫蛋黄", _T0 + timedelta(minutes=1), "u-new"),
        _assistant("好的", _T0 + timedelta(minutes=1, seconds=5), "a-new"),
    ]
    times = memory_module._message_times(messages, ())
    assert times["u-old"] == _T0
    assert times["a-old"] == _T0 + timedelta(seconds=5)
    assert times["u-new"] == _T0 + timedelta(minutes=1)


@dataclass
class _CapturingLLM:
    reply: str
    seen: list[dict[str, Any]] = field(default_factory=list)

    async def __call__(self, *, messages: Sequence[BaseMessage], tools: Sequence[Any]) -> AIMessage:
        del tools
        self.seen.append(json.loads(str(messages[1].content)))
        return AIMessage(content=self.reply)


@pytest.mark.asyncio
async def test_the_targets_date_shown_to_the_model_is_its_evidence_time() -> None:
    """L-3:给模型看的目标日期与程序比先后用同一个时间(证据消息的发送时间),不是记录时间。"""
    c = _Ctx()
    store = InMemoryMemoryStore()
    target = c.mem(
        "the car is a hatchback", _at(0.95), origin="user_confirmed", confidence=0.8,
        created_at=_T0 + timedelta(days=5), evidence_message_id="m1",
    )  # fmt: skip
    await store.write([target])
    cand = c.mem("the car is a red hatchback", origin="user_stated", confidence=0.9)
    llm = _CapturingLLM(_ops((0, "ADD", None)))
    await _reconcile(store, llm, _Candidate(cand, line_at=_T0 + timedelta(days=1)), {"m1": _T0})
    [existing] = llm.seen[0]["candidates"][0]["existing"]
    assert existing["date"] == _T0.date().isoformat()


@pytest.mark.asyncio
async def test_unknown_conversations_are_not_the_same_conversation() -> None:
    """L-4:两边都没有 ``source_thread_id`` 不算同一对话 —— 证据消息的时间不能拿来比先后(按记录
    时间:更旧,改为新增),同一条证据也不算重述。"""
    c = _Ctx()
    store = InMemoryMemoryStore()
    target = c.mem(
        "the car is a hatchback", _at(0.95), origin="user_confirmed", confidence=0.8,
        created_at=_T0 + timedelta(days=5), evidence_message_id="m1", source_thread_id=None,
    )  # fmt: skip
    await store.write([target])
    cand = c.mem(
        "the car is a red hatchback", origin="user_confirmed", confidence=0.8,
        evidence_message_id="m1", source_thread_id=None,
    )  # fmt: skip
    result = await _reconcile(
        store,
        _FixedLLM(_ops((0, "UPDATE", "e0"))),
        _Candidate(cand, line_at=_T0 + timedelta(days=1), in_new_segment=True),
        {"m1": _T0},
    )
    assert (result.superseded, [m.id for m in result.direct]) == (0, [cand.id])

    same = _count("noop_same_evidence")
    await _reconcile(store, _FixedLLM(_ops((0, "NOOP", "e0"))), _Candidate(cand), {"m1": _T0})
    assert _count("noop_same_evidence") == same


def _named_across_the_mark() -> list[ExtractionLine]:
    return [
        ExtractionLine(text="帮我想个猫名", tag="[user]", context=True, message_id="u1"),
        ExtractionLine(text="建议叫它蛋黄", tag="[assistant]", context=True, message_id="a1"),
        ExtractionLine(text="好", tag="[user]", message_id="u2"),
        ExtractionLine(text="好的", tag="[assistant]", message_id="a2"),
    ]


@pytest.mark.parametrize("basis", ["user_answer", "user_agreement"])
def test_an_answer_put_on_the_last_context_assistant_line_is_kept_as_assistant_derived(
    basis: str,
) -> None:
    """M-3(用户 11-16「这种情况照旧保存」):上一轮以「[assistant] 建议叫它蛋黄」结束,新段第一行是
    「[user] 好」,模型把 ``turn`` 指在助手那一行 → 照旧存成来自助手(≤ 0.6)、指针留空,计数
    ``context_line_downgraded``。"""
    settled = memory_lines.settle_conversation(
        basis, 2, None, _named_across_the_mark(), today=_TODAY
    )
    assert settled is not None
    assert (settled.origin, settled.max_confidence) == ("assistant_derived", 0.6)
    assert (settled.evidence_message_id, settled.in_new_segment) == (None, False)
    assert settled.checks == ("context_line_downgraded",)


def test_near_misses_of_the_answer_shape_are_still_dropped() -> None:
    lines = _named_across_the_mark()
    # 不是用户回答 / 同意
    assert memory_lines.settle_conversation("assistant", 2, None, lines, today=_TODAY) is None
    # 上下文行不是最后一行
    not_last = [
        ExtractionLine(text="建议叫它蛋黄", tag="[assistant]", context=True, message_id="a0"),
        *lines,
    ]
    assert (
        memory_lines.settle_conversation("user_agreement", 1, None, not_last, today=_TODAY) is None
    )
    # 上下文最后一行不是助手
    user_last = [lines[1], lines[0], *lines[2:]]
    assert (
        memory_lines.settle_conversation("user_agreement", 2, None, user_last, today=_TODAY) is None
    )
    # 新段第一行不是用户
    assistant_next = [*lines[:2], ExtractionLine(text="继续", tag="[assistant]", message_id="a2")]
    assert (
        memory_lines.settle_conversation("user_agreement", 2, None, assistant_next, today=_TODAY)
        is None
    )
    # 第一次提炼不受影响:上下文行一律丢
    first = [ExtractionLine(text="帮我想个猫名", context=True), ExtractionLine(text="好")]
    assert memory_lines.settle_user_text(1, None, first, today=_TODAY) is None


@pytest.mark.asyncio
async def test_the_answer_shape_is_stored_through_the_write_back(
    checks: list[tuple[str, str]],
) -> None:
    c = _Ctx()
    store = InMemoryMemoryStore()
    messages = [
        _user("帮我想个猫名", _T0, "u1"),
        _assistant("建议叫它蛋黄", _T0 + timedelta(seconds=5), "a1"),
        _user("好", _T0 + timedelta(minutes=1), "u2"),
        _assistant("好的,就叫蛋黄。", _T0 + timedelta(minutes=1, seconds=5), "a2"),
    ]
    llm = _RoutingLLM(
        conversation_reply=_reply(
            {"kind": "fact", "content": "用户的猫叫蛋黄", "basis": "user_agreement", "turn": 2,
             "confidence": 0.9},
        ),
    )  # fmt: skip
    outcome = await _flush(
        c, store, messages, llm, {"用户的猫叫蛋黄": _E}, SegmentStart(message_index=2)
    )
    [row] = _live(store)
    assert (row.content, row.origin, row.evidence_message_id) == (
        "用户的猫叫蛋黄",
        "assistant_derived",
        None,
    )
    assert row.confidence <= 0.6
    assert outcome.written == 1
    assert checks == [("conversation", "context_line_downgraded")]
