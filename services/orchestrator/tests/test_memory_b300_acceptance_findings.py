"""B-300 测试验收(测试环境,main ``5b8588d0e``)查出的三个问题,按那几次运行的形状用假模型复现。

1. 上下文行被重新提炼(run ``1fec5018`` / ``8051ac10``):两次提炼的产出 ``turn`` 指向
   ``=== new ===`` 之上的行,程序照写(没有证据指针),去重合并在类型不同时判新增 → 用户一方出现
   内容相同的两行。改:两次提炼里 ``turn`` 落在上下文行的一律丢弃、计数 ``context_line_dropped``
   (设计 §4、§5.4、§8.2:上下文行只帮助理解,由之前的任务提炼过)。``turn`` 缺失 / 越界的照旧写、
   指针留空(第一次仍是 ``user_stated``,第二次仍是 ``assistant_derived``)。唯一的例外(用户 11-16
   「这种情况照旧保存」「按用户确认重新核对」):用户在新段第一行回答 / 同意上下文最后一行的助手
   提议、``turn`` 被指在助手那一行 → 改挂到用户那一行照常核对,存成用户确认,计数
   ``turn_reanchored_context``(第 4 节);同样的产出落在同一段里也同样改挂(第 6 节)。
2. 同一条用户消息被存两遍(run ``7ee5144b``):第一次存成 ``user_stated``,第二次又把它标成用户
   回答 / 同意;去重合并判「更新」,却被「候选那一行早于目标的记录时间」改成新增 —— 目标几秒前才在
   本任务第一段写下。改:① 先后按目标的**证据消息**的发送时间比(这条消息在本对话里、知道时间时;
   否则按记录时间);② 第二次的 ``user_confirmed`` 候选被模型判成「重复」一条证据消息相同、事件
   日期不冲突的用户一方记忆 → 就是同一句话的重述:NOOP,不刷新确认日期,计数
   ``noop_same_evidence``。判「更新」的照常走(用户 11-16「只拦真重复」,第 4 节)。
3. run ``8e5a1f0d``:用户确认的候选要改用户说的那条 —— 最早被丢弃(猫名「蛋黄」丢了,「还没起名字」
   留着),第 8 版改成并存。第 9 版「写和退分开」(设计 §7.3)之后不再有「被拦」:候选一定写下,旧行
   退不退另判 —— 用户确认默认不退用户说的(两条都有效、新行连着旧行,计数
   ``retire_refused_confirmed_vs_stated``);用户确认之间、用户说的之间按先后退,不看置信度;来自
   助手的候选也写下(标「来自助手」),只是退不掉用户一方的。
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
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
from orchestrator.graph_builder.memory import _Candidate, _Decision, _reconcile_and_apply
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


def _lists(**lists: list[str]) -> str:
    """一条候选:写下(``ADD``),并给出两张清单(``replaces`` / ``restates``)。"""
    return json.dumps({"ops": [{"index": 0, "op": "ADD", **lists}]})


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


def _row(store: InMemoryMemoryStore, memory_id: UUID) -> MemoryItem:
    return next(r for r in store._rows if r.id == memory_id)


def _coexist_linked(store: InMemoryMemoryStore, old: MemoryItem, new: MemoryItem) -> bool:
    """两条都有效,新行的 ``supersedes`` 连着旧行(旧行没作废)。"""
    live = {r.id for r in _live(store)}
    return {old.id, new.id} <= live and _row(store, new.id).supersedes == old.id


def _retired_into(store: InMemoryMemoryStore, old: MemoryItem, new: MemoryItem) -> bool:
    row = _row(store, old.id)
    return row.invalid_at is not None and row.superseded_by == new.id


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
    """同一任务里:第二次的来自助手的候选要替掉第一段几秒前写下的用户说的。那一行(助手回复)晚于
    目标的证据消息,不算更旧;退不掉是因为来源(助手一方退不掉用户一方),候选自己照写。"""
    c = _Ctx()
    store = InMemoryMemoryStore()
    llm = _RoutingLLM(
        user_reply=_reply({"kind": "fact", "content": "用户的猫叫蛋黄", "turn": 2}),
        conversation_reply=_reply(
            {"kind": "fact", "content": "助手记下用户的猫叫蛋黄", "basis": "assistant", "turn": 4},
        ),
        ops=[_lists(restates=["e0"])],
    )  # fmt: skip
    embeddings = {"用户的猫叫蛋黄": _E, "助手记下用户的猫叫蛋黄": _at(0.95)}
    older, refused = _count("retire_refused_older_line"), _count("retire_refused_assistant")
    await _flush(c, store, _named_the_cat(), llm, embeddings)
    rows = {r.content: r.origin for r in _live(store)}
    assert rows == {"用户的猫叫蛋黄": "user_stated", "助手记下用户的猫叫蛋黄": "assistant_derived"}
    assert (_count("retire_refused_older_line"), _count("retire_refused_assistant")) == (
        older,
        refused + 1,
    )


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
    """候选更旧 → 目标不退;候选照写,两条并存并连线(这里倒过来「用户确认退用户说的」过不了,
    所以不是历史行)。"""
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
        _FixedLLM(_lists(restates=["e0"])),
        _Candidate(cand, line_at=_T0 + timedelta(days=1), in_new_segment=True),
        times,
    )
    assert (result.superseded, result.direct) == (1, [])
    if applied:
        assert _retired_into(store, target, cand)
    else:
        assert _coexist_linked(store, target, cand)


@pytest.mark.asyncio
async def test_a_later_restatement_still_counts_against_an_older_line() -> None:
    """证据消息早,但用户之后又说过一遍(确认日期晚于候选那一行):仍算更旧,目标不退。"""
    c = _Ctx()
    store = InMemoryMemoryStore()
    target = c.mem(
        "the car is a hatchback", _at(0.95), origin="user_confirmed", confidence=0.8,
        evidence_message_id="m1", confirmed_at=_T0 + timedelta(days=3),
    )  # fmt: skip
    await store.write([target])
    cand = c.mem("the car is a red hatchback", origin="user_stated", confidence=0.9)
    before = _count("retire_refused_older_line")
    await _reconcile(
        store,
        _FixedLLM(_lists(restates=["e0"])),
        _Candidate(cand, line_at=_T0 + timedelta(days=1), in_new_segment=True),
        {"m1": _T0},
    )
    assert _coexist_linked(store, target, cand)
    assert _count("retire_refused_older_line") == before + 1


# ─── 3. 用户确认碰上已有记忆(run 8e5a1f0d;第 9 版:写和退分开)──────────────────────


@pytest.mark.asyncio
async def test_a_user_confirmed_candidate_coexists_with_what_the_user_stated() -> None:
    """用户先说「猫还没起名字」(``user_stated``);后来同意了助手提的「蛋黄」(``user_confirmed``,
    不是更正)。最早「蛋黄」被丢;现在一定写下,用户说的那条默认不退,两条都有效并连线。"""
    c = _Ctx()
    store = InMemoryMemoryStore()
    old = c.mem("用户的猫还没起名字", _at(0.9), origin="user_stated", confidence=0.9)
    await store.write([old])
    cand = c.mem("用户的猫叫蛋黄", origin="user_confirmed", confidence=0.8)
    before = _count("retire_refused_confirmed_vs_stated")
    result = await _reconcile(
        store,
        _FixedLLM(_lists(replaces=["e0"])),
        _Candidate(cand, line_at=_T0 + timedelta(days=1), in_new_segment=True),
    )
    assert (result.superseded, result.direct) == (1, [])
    assert _coexist_linked(store, old, cand)
    assert _count("retire_refused_confirmed_vs_stated") == before + 1


@pytest.mark.asyncio
async def test_an_assistant_candidate_is_written_and_does_not_retire_what_the_user_stated() -> None:
    """用户 2026-11-16(§15 第 11 条 ①):助手的不同说法写下、标「来自助手」,退不掉用户说的。"""
    c = _Ctx()
    store = InMemoryMemoryStore()
    old = c.mem("用户的猫还没起名字", _at(0.9), origin="user_stated", confidence=0.9)
    await store.write([old])
    cand = c.mem("助手建议猫叫蛋黄", origin="assistant_derived", confidence=0.6)
    before = _count("retire_refused_assistant")
    result = await _reconcile(
        store,
        _FixedLLM(_lists(replaces=["e0"])),
        _Candidate(cand, line_at=_T0 + timedelta(days=1), in_new_segment=True),
    )
    assert [m.id for m in result.direct] == [cand.id]  # 助手一方、什么都不退 → 走普通写入
    assert [r.id for r in _live(store)] == [old.id]
    assert _count("retire_refused_assistant") == before + 1


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
        return AIMessage(content=_lists(replaces=["e0"]))


@pytest.mark.asyncio
async def test_a_target_corrected_meanwhile_is_kept_and_the_candidate_still_written() -> None:
    c = _Ctx()
    store = InMemoryMemoryStore()
    target = c.mem("猫的名字待定", _at(0.9), origin="assistant_derived", confidence=0.5)
    await store.write([target])
    cand = c.mem("用户的猫叫蛋黄", origin="user_confirmed", confidence=0.8)
    before = _count("retire_refused_corrected")
    result = await _reconcile(
        store,
        _CorrectingLLM(store, target),
        _Candidate(cand, line_at=_T0 + timedelta(days=1), in_new_segment=True),
    )
    assert (result.superseded, result.direct) == (1, [])
    assert sorted(r.content for r in _live(store)) == ["corrected by the user", "用户的猫叫蛋黄"]
    assert _count("retire_refused_corrected") == before + 1


@pytest.mark.asyncio
async def test_a_later_user_confirmed_retires_the_earlier_one_whatever_the_confidence() -> None:
    """用户确认过猫叫蛋黄(0.8);后来又同意了助手提的「布丁」,这次没那么确定(0.7)。第 8 版是
    两条并存(``lower_confidence_update_added_confirmed``);第 9 版同为用户确认按先后:「布丁」
    写下,「蛋黄」被退并连到它,置信度不参与。重试时内容相同的不再多一行。"""
    c = _Ctx()
    store = InMemoryMemoryStore()
    old = c.mem("用户的猫叫蛋黄", _at(0.9), origin="user_confirmed", confidence=0.8)
    await store.write([old])
    cand = c.mem("用户的猫叫布丁", origin="user_confirmed", confidence=0.7)
    retired = _count("retired")
    result = await _reconcile(
        store,
        _FixedLLM(_lists(replaces=["e0"])),
        _Candidate(cand, line_at=_T0 + timedelta(days=1), in_new_segment=True),
    )
    assert (result.superseded, result.direct) == (1, [])
    assert _retired_into(store, old, cand)
    assert [r.content for r in _live(store)] == ["用户的猫叫布丁"]
    assert _count("retired") == retired + 1

    again = c.mem("用户的猫叫布丁", origin="user_confirmed", confidence=0.7)
    retry = await _reconcile(
        store,
        _FixedLLM(_lists(replaces=["e0"])),
        _Candidate(again, line_at=_T0 + timedelta(days=1), in_new_segment=True),
    )
    assert (retry.superseded, retry.direct) == (0, [])
    assert [r.content for r in _live(store)] == ["用户的猫叫布丁"]


@pytest.mark.parametrize(
    ("cand_origin", "target_origin", "retired"),
    [
        # 用户说的候选、用户确认的目标:照常退(「用户后来自己说全了」),不算重述。
        ("user_stated", "user_confirmed", True),
        # 来自助手的目标不是用户一方:用户确认照常退它。
        ("user_confirmed", "assistant_derived", True),
        # 来自助手的候选:退不掉用户说的(来源),自己照写,不是重述。
        ("assistant_derived", "user_stated", False),
    ],
    ids=["stated-on-confirmed", "confirmed-on-assistant", "assistant-on-stated"],
)
@pytest.mark.asyncio
async def test_only_a_user_confirmed_on_a_user_side_row_is_a_same_message_restatement(
    cand_origin: str, target_origin: str, retired: bool
) -> None:
    c = _Ctx()
    store = InMemoryMemoryStore()
    target = c.mem(
        "用户的猫叫蛋黄", _at(0.95), origin=target_origin, confidence=0.6, evidence_message_id="u2"
    )
    await store.write([target])
    cand = c.mem("用户的猫名叫蛋黄", origin=cand_origin, confidence=0.6, evidence_message_id="u2")
    before = _count("noop_same_evidence")
    result = await _reconcile(
        store,
        _FixedLLM(_lists(restates=["e0"])),
        _Candidate(cand, line_at=_T0, in_new_segment=True),
        {"u2": _T0},
    )
    await store.write(result.direct)
    assert cand.id in {r.id for r in _live(store)}
    assert _retired_into(store, target, cand) is retired
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
    assert (result.superseded, result.direct) == (1, [])
    assert {r.id for r in _live(store)} == {target.id, cand.id}


@pytest.mark.asyncio
async def test_a_user_confirmed_denial_is_written_and_keeps_what_the_user_stated() -> None:
    """模型仍回旧的「删除」(按 ``ADD`` + ``replaces`` 读):那句话本身写下;用户说的那条默认不退。
    以前被拦的删除什么都不做、候选也不存。"""
    c = _Ctx()
    store = InMemoryMemoryStore()
    old = c.mem("用户的猫还没起名字", _at(0.9), origin="user_stated", confidence=0.9)
    await store.write([old])
    cand = c.mem("用户的猫不是没名字", origin="user_confirmed", confidence=0.8)
    legacy, refused = _count("legacy_op"), _count("retire_refused_confirmed_vs_stated")
    await _reconcile(
        store,
        _FixedLLM(_ops((0, "DELETE", "e0"))),
        _Candidate(cand, line_at=_T0 + timedelta(days=1), in_new_segment=True),
    )
    assert _coexist_linked(store, old, cand)
    assert _row(store, old.id).expired_at is None
    assert (_count("legacy_op"), _count("retire_refused_confirmed_vs_stated")) == (
        legacy + 1,
        refused + 1,
    )


# ─── 4. 独立审查(11-16)与用户拍板「只拦真重复」「这种情况照旧保存」────────────────────


@pytest.mark.parametrize(
    ("reply", "counter"),
    [
        (_lists(replaces=["e0"]), "retire_refused_event_date"),
        (_ops((0, "NOOP", "e0")), "noop_other_event_date"),
    ],
    ids=["replaces", "noop"],
)
@pytest.mark.asyncio
async def test_same_evidence_with_different_event_dates_is_added(reply: str, counter: str) -> None:
    """M-1:用户一句话里说了「昨天 130,今天 125」;第一次记下「昨天 130」(带日期),第二次给出
    「今天 125」,证据是同一条消息、事件日期不同 —— 是两次读数,不是重述:模型要它替掉那条 → 事件
    日期规则不退,两条都在;判「重复」按 ``noop_other_event_date`` 写下。"""
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
        _FixedLLM(reply),
        _Candidate(cand, line_at=_T0, in_new_segment=True),
        {"u2": _T0},
    )
    assert (result.superseded, result.direct) == (1, [])
    assert {r.id for r in _live(store)} == {target.id, cand.id}
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
async def test_a_second_pass_answer_is_kept_next_to_the_first_pass_statement() -> None:
    """M-2(用户 11-16「只拦真重复」):同一条消息,第一次记下「下午要接孩子」(``user_stated``),
    第二次给出「喜欢上午上课,因为下午接孩子」(``user_confirmed``),模型要它替掉那条 —— 不当成
    重述吞掉,也不退用户说的:两条都在、连线(``retire_refused_confirmed_vs_stated``),「喜欢上午」
    不丢。"""
    c = _Ctx()
    store = InMemoryMemoryStore()
    llm = _RoutingLLM(
        user_reply=_reply({"kind": "fact", "content": _PICKUP, "turn": 2}),
        conversation_reply=_reply(
            {"kind": "fact", "content": _MORNING, "basis": "user_answer", "turn": 3}
        ),
        ops=[_lists(restates=["e0"])],
    )
    embeddings = {_PICKUP: _E, _MORNING: _at(0.9)}
    same, refused = _count("noop_same_evidence"), _count("retire_refused_confirmed_vs_stated")
    outcome = await _flush(c, store, _picked_the_class_time(), llm, embeddings)
    rows = {r.content: r for r in _live(store)}
    assert sorted(rows) == sorted([_PICKUP, _MORNING])
    assert (rows[_PICKUP].origin, rows[_MORNING].origin) == ("user_stated", "user_confirmed")
    assert rows[_MORNING].evidence_message_id == "u2"
    assert rows[_MORNING].supersedes == rows[_PICKUP].id
    assert outcome.written == 2
    assert _count("noop_same_evidence") == same
    assert _count("retire_refused_confirmed_vs_stated") == refused + 1

    # 任务重试再跑一遍:内容相同 → 确定性「重复」,不再多出行,也不再问模型。
    retried = _RoutingLLM(user_reply=llm.user_reply, conversation_reply=llm.conversation_reply)
    again = await _flush(c, store, _picked_the_class_time(), retried, embeddings)
    assert sorted(r.content for r in _live(store)) == sorted([_PICKUP, _MORNING])
    assert again.written == 0
    assert retried.reconcile_calls == []

    # 重试换了说法、模型要它替掉上一次那条(同为第二次的产出、同一条证据):退掉、换成新说法,仍是两行。
    reworded = "用户偏好上午上课,下午要接孩子"
    retry = _RoutingLLM(
        user_reply=llm.user_reply,
        conversation_reply=_reply(
            {"kind": "fact", "content": reworded, "basis": "user_answer", "turn": 3}
        ),
        ops=[_lists(restates=["e0"])],
    )
    await _flush(c, store, _picked_the_class_time(), retry, {**embeddings, reworded: _at(0.9)})
    assert sorted(r.content for r in _live(store)) == sorted([_PICKUP, reworded])


@pytest.mark.asyncio
async def test_a_same_message_update_of_the_same_tier_is_applied() -> None:
    """M-2:证据相同、同为用户确认,模型说候选把它说全了 → 照常退掉旧的(同一条消息不算更旧)。"""
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
        _FixedLLM(_lists(restates=["e0"])),
        _Candidate(cand, line_at=_T0 + timedelta(minutes=1), in_new_segment=True),
        {"u2": _T0 + timedelta(minutes=1)},
    )
    assert (result.superseded, result.direct) == (1, [])
    assert [r.content for r in _live(store)] == [_MORNING]
    assert _count("noop_same_evidence") == same


@pytest.mark.parametrize(
    ("decision", "cand_day", "writes"),
    [
        (_Decision("NOOP", "e0"), None, False),  # 同一条消息的重述:不写
        (_Decision("NOOP", "e0"), _TODAY, True),  # 事件日期不同:另一次,写下
        (_Decision("ADD", None, (), ("e0",)), None, True),  # 判写下并替掉它:旧的不退,候选照写
    ],
    ids=["noop", "noop-other-date", "add"],
)
@pytest.mark.asyncio
async def test_a_failed_reconcile_writes_what_applying_would_write_for_the_same_message(
    decision: _Decision, cand_day: object, writes: bool
) -> None:
    """L-1:去重合并中途出错时,还没落完的候选按 ``_would_write`` 收尾(写或进失败队列)—— 它必须与
    真正落库时对同一条消息的判法一致:落库不写的(重述)收尾也不写,落库会写的收尾也写。"""
    c = _Ctx()
    store = InMemoryMemoryStore()
    target = c.mem(
        "昨天测得读数 130", _at(0.95), origin="user_stated", confidence=0.9,
        evidence_message_id="u2", occurred_on=_TODAY - timedelta(days=1),
    )  # fmt: skip
    await store.write([target])
    cand = c.mem(
        "读数 130", origin="user_confirmed", confidence=0.8, evidence_message_id="u2",
        occurred_on=cand_day or (_TODAY - timedelta(days=1)),
    )  # fmt: skip
    reply = json.dumps(
        {
            "ops": [
                {
                    "index": 0,
                    "op": decision.op,
                    "target_id": decision.target,
                    "restates": list(decision.restates),
                }
            ]
        }
    )
    await _reconcile(store, _FixedLLM(reply), _Candidate(cand))
    pending = memory_module._Pending(_Candidate(cand), (target,), {target.id: 2})
    on_failure = memory_module._would_write(pending, decision, {"e0": target.id})
    assert (cand.id in {r.id for r in _live(store)}, on_failure) == (writes, writes)


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
    时间:更旧,目标不退、两条并存),同一条证据也不算重述。"""
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
    older = _count("retire_refused_older_line")
    await _reconcile(
        store,
        _FixedLLM(_lists(restates=["e0"])),
        _Candidate(cand, line_at=_T0 + timedelta(days=1), in_new_segment=True),
        {"m1": _T0},
    )
    assert _coexist_linked(store, target, cand)
    assert _count("retire_refused_older_line") == older + 1

    same = _count("noop_same_evidence")
    again = c.mem(
        "the car is a hatchback, a red one", _at(0.99), origin="user_confirmed",
        evidence_message_id="m1", source_thread_id=None,
    )  # fmt: skip
    await _reconcile(store, _FixedLLM(_ops((0, "NOOP", "e0"))), _Candidate(again), {"m1": _T0})
    assert _count("noop_same_evidence") == same


def _named_across_the_mark(next_line: ExtractionLine | None = None) -> list[ExtractionLine]:
    """上一轮 23:50 以助手的提议结束;新段第一行是用户 00:10(第二天)的回答。"""
    return [
        ExtractionLine(text="帮我想个猫名", tag="[user]", context=True, message_id="u1",
                       message_index=0, sent_at=_LATE, sent_day=_LATE.date()),
        ExtractionLine(text="建议叫它蛋黄", tag="[assistant]", context=True, message_id="a1",
                       message_index=1, sent_at=_LATE, sent_day=_LATE.date()),
        next_line or ExtractionLine(text="好,就叫这个", tag="[user]", message_id="u2",
                                    message_index=2, sent_at=_AFTER, sent_day=_AFTER.date()),
        ExtractionLine(text="好的", tag="[assistant]", message_id="a2", message_index=3,
                       sent_at=_AFTER, sent_day=_AFTER.date()),
    ]  # fmt: skip


_LATE = datetime(2026, 3, 1, 23, 50, tzinfo=UTC)
_AFTER = datetime(2026, 3, 2, 0, 10, tzinfo=UTC)


@pytest.mark.parametrize("basis", ["user_answer", "user_agreement"])
def test_an_answer_put_on_the_last_context_assistant_line_is_reanchored_to_the_user_line(
    basis: str,
) -> None:
    """M-3 → 审查 M-1(用户 11-16「按用户确认重新核对」):上一轮以「[assistant] 建议叫它蛋黄」结束,
    新段第一行是「[user] 好,就叫这个」,模型把 ``turn`` 指在助手那一行 → 程序改挂到那一行用户行、
    照常核对:过了 → 用户确认(≤ 0.8)、证据指针 = 用户那条消息、算新段,计数
    ``turn_reanchored_context``。时间与事件日期按用户那一行(00:10,第二天),不按助手那一行
    (23:50):「今天」的事件日期不被当成「晚于那一行」清掉(审查 LOW-2)。"""
    settled = memory_lines.settle_conversation(
        basis, 2, _AFTER.date().isoformat(), _named_across_the_mark(), today=_TODAY
    )
    assert settled is not None
    assert (settled.origin, settled.max_confidence) == ("user_confirmed", 0.8)
    assert (settled.evidence_message_id, settled.evidence_message_index) == ("u2", 2)
    assert (settled.in_new_segment, settled.line_at) == (True, _AFTER)
    assert settled.event_date == _AFTER.date()
    assert settled.checks == ("turn_reanchored_context",)


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
    # 新段第一行不是用户(另一条助手消息):不改挂,丢弃
    assistant_next = _named_across_the_mark(
        ExtractionLine(text="提醒:该喂猫了", tag="[assistant]", message_id="a1b", message_index=2,
                       sent_at=_AFTER, sent_day=_AFTER.date()),
    )  # fmt: skip
    assert (
        memory_lines.settle_conversation("user_agreement", 2, None, assistant_next, today=_TODAY)
        is None
    )
    # 新段里没有行
    assert (
        memory_lines.settle_conversation("user_agreement", 2, None, lines[:2], today=_TODAY) is None
    )
    # 第一次提炼不受影响:上下文行一律丢
    first = [ExtractionLine(text="帮我想个猫名", context=True), ExtractionLine(text="好")]
    assert memory_lines.settle_user_text(1, None, first, today=_TODAY) is None


@pytest.mark.asyncio
async def test_the_reanchored_answer_is_kept_alongside_what_the_user_said_before(
    checks: list[tuple[str, str]],
) -> None:
    """端到端:用户先前说过「猫还没起名字」(``user_stated``);上一轮以「建议叫它蛋黄」结束,新段
    「好,就叫这个」;模型把 ``turn`` 指在助手那一行,去重合并要它替掉那条用户说的 → 改挂成用户
    确认;用户确认默认不退用户说的 → 两条并存并连线:名字存下了,不丢。"""
    c = _Ctx()
    store = InMemoryMemoryStore()
    old = c.mem(
        "用户的猫还没起名字", _at(0.9), origin="user_stated", confidence=0.9,
        created_at=_LATE - timedelta(days=1),
    )  # fmt: skip
    await store.write([old])
    messages = [
        _user("帮我想个猫名", _LATE - timedelta(minutes=1), "u1"),
        _assistant("建议叫它蛋黄", _LATE, "a1"),
        _user("好,就叫这个", _AFTER, "u2"),
        _assistant("好的,就叫蛋黄。", _AFTER + timedelta(seconds=5), "a2"),
    ]
    llm = _RoutingLLM(
        conversation_reply=_reply(
            {"kind": "fact", "content": "用户的猫叫蛋黄", "basis": "user_agreement", "turn": 2,
             "confidence": 0.9},
        ),
        ops=[_lists(replaces=["e0"])],
    )  # fmt: skip
    added = _count("retire_refused_confirmed_vs_stated")
    outcome = await _flush(
        c, store, messages, llm, {"用户的猫叫蛋黄": _E}, SegmentStart(message_index=2)
    )
    rows = {r.content: r for r in _live(store)}
    assert sorted(rows) == ["用户的猫叫蛋黄", "用户的猫还没起名字"]
    named = rows["用户的猫叫蛋黄"]
    assert (named.origin, named.evidence_message_id, named.evidence_message_index) == (
        "user_confirmed",
        "u2",
        2,
    )
    assert named.confidence <= 0.8
    assert named.supersedes == old.id
    assert outcome.written == 1
    assert _count("retire_refused_confirmed_vs_stated") == added + 1
    assert checks == [("conversation", "turn_reanchored_context")]


# ─── 5. 第二轮审查(11-16):逐字重述 ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_identical_content_from_the_same_message_counts_as_same_evidence() -> None:
    """审查 LOW-4:第二次把同一条消息逐字又给了一遍(内容相同 → 不问模型的确定性「重复」)—— 也是
    同一句话的重述:计 ``noop_same_evidence``,不刷新确认日期。"""
    c = _Ctx()
    store = InMemoryMemoryStore()
    stated = c.mem("用户的猫叫蛋黄", origin="user_stated", confidence=0.9, evidence_message_id="u2")
    await store.write([stated])
    cand = c.mem(
        "用户的猫叫蛋黄", origin="user_confirmed", confidence=0.8, evidence_message_id="u2"
    )
    same, refreshed = _count("noop_same_evidence"), _count("noop_refreshed")
    result = await _reconcile(
        store,
        _FixedLLM(_ops()),
        _Candidate(cand, line_at=_T0 + timedelta(minutes=1), in_new_segment=True),
        {"u2": _T0 + timedelta(minutes=1)},
    )
    assert result.direct == []
    assert (_count("noop_same_evidence"), _count("noop_refreshed")) == (same + 1, refreshed)
    [row] = _live(store)
    assert row.confirmed_at is None


# ─── 6. 第三轮审查(11-16):改挂与切片无关(用户「同一批里也改指」)──────────────────────


def _named_in_one_segment(next_line: ExtractionLine | None = None) -> list[ExtractionLine]:
    """同样四行,都在新段里(一个写回任务覆盖了几轮,例如对话的第一个任务按 30 天窗起算)。"""
    return [replace(line, context=False) for line in _named_across_the_mark(next_line)]


@pytest.mark.parametrize("basis", ["user_answer", "user_agreement"])
def test_an_answer_put_on_the_assistant_line_inside_the_segment_is_reanchored(basis: str) -> None:
    """审查 M-1:助手提议与用户回答都在新段里,模型把 ``turn`` 指在助手那一行、下一行是用户行 ——
    与跨分界时一样改挂到用户行:用户确认、证据指针取用户行、时间与事件日期按用户行,计数
    ``turn_reanchored_in_segment``。以前降为来自助手,被层级拦下就丢了。"""
    settled = memory_lines.settle_conversation(
        basis, 2, _AFTER.date().isoformat(), _named_in_one_segment(), today=_TODAY
    )
    assert settled is not None
    assert (settled.origin, settled.max_confidence) == ("user_confirmed", 0.8)
    assert (settled.evidence_message_id, settled.evidence_message_index) == ("u2", 2)
    assert (settled.in_new_segment, settled.line_at) == (True, _AFTER)
    assert settled.event_date == _AFTER.date()
    assert settled.checks == ("turn_reanchored_in_segment",)


def test_in_segment_near_misses_keep_todays_downgrade() -> None:
    """同一段里别的形状照旧:助手行的下一行不是用户行、助手行是最后一行、``basis`` 不是回答 / 同意
    → 来自助手,指针取助手那一行。"""
    task_next = _named_in_one_segment(
        ExtractionLine(text="定时提醒", tag="[task]", message_id="t1", message_index=2),
    )
    cases = [
        ("user_agreement", 2, task_next, ("downgraded_not_user_line",)),
        ("user_agreement", 4, _named_in_one_segment(), ("downgraded_not_user_line",)),
        ("assistant", 2, _named_in_one_segment(), ()),
    ]
    for basis, turn, lines, checks in cases:
        settled = memory_lines.settle_conversation(basis, turn, None, lines, today=_TODAY)
        assert settled is not None
        assert (settled.origin, settled.evidence_message_id) == (
            "assistant_derived",
            lines[turn - 1].message_id,
        )
        assert settled.checks == checks


@pytest.mark.asyncio
async def test_the_same_output_settles_the_same_however_the_write_back_is_sliced(
    checks: list[tuple[str, str]],
) -> None:
    """用户 11-16「同一批里也改指」:同一段对话、同一份模型产出(``turn`` 指在助手提议那一行),
    一次按「上一轮是上下文」切,一次整段都是新段 —— 存下的来源、置信度、证据指针完全一样。"""
    messages = [
        _user("帮我想个猫名", _LATE - timedelta(minutes=1), "u1"),
        _assistant("建议叫它蛋黄", _LATE, "a1"),
        _user("好,就叫这个", _AFTER, "u2"),
        _assistant("好的,就叫蛋黄。", _AFTER + timedelta(seconds=5), "a2"),
    ]
    reply = _reply(
        {"kind": "fact", "content": "用户的猫叫蛋黄", "basis": "user_agreement", "turn": 2,
         "confidence": 0.9},
    )  # fmt: skip
    stored = []
    for segment in (SegmentStart(message_index=2), None):
        c = _Ctx()
        store = InMemoryMemoryStore()
        llm = _RoutingLLM(conversation_reply=reply)
        await _flush(c, store, messages, llm, {"用户的猫叫蛋黄": _E}, segment)
        [row] = _live(store)
        stored.append(
            (row.origin, row.confidence, row.evidence_message_id, row.evidence_message_index)
        )
    assert stored[0] == stored[1] == ("user_confirmed", 0.8, "u2", 2)
    assert checks == [
        ("conversation", "turn_reanchored_context"),
        ("conversation", "turn_reanchored_in_segment"),
    ]


# ─── 7. 第 10 版审查 MEDIUM-2:早说的后到,看本任务里先写的那条带没带更正(用户 11-17 选 (b))────


def _agreed_then_renamed(later: str) -> list[BaseMessage]:
    """场景 j:第 1 轮同意助手提的名字(第二次提炼,用户确认),第 2 轮整句改口(第一次提炼,用户
    说的)。一个任务覆盖两轮:两段合并先写第 2 轮那句,第 1 轮那句后到。"""
    return [
        _user("帮我想想猫的名字", _T0, "u1"),
        _assistant("叫「南枝」怎么样?", _T0 + timedelta(seconds=5), "a1"),
        _user("好", _T0 + timedelta(minutes=1), "u2"),
        _assistant("好的,就叫南枝。", _T0 + timedelta(minutes=1, seconds=5), "a2"),
        _user(later, _T0 + timedelta(minutes=5), "u3"),
        _assistant("好的。", _T0 + timedelta(minutes=5, seconds=5), "a3"),
    ]


def _first_and_second(later: str, *, correction: bool) -> _RoutingLLM:
    """第一次:第 2 轮那句(用户行编号 3);第二次:第 1 轮的同意(整段编号 3,上一行是助手的提议);
    去重合并:模型把先写下的那句列进后到那句的 ``replaces``(配成了一对)。"""
    return _RoutingLLM(
        user_reply=_reply(
            {"kind": "fact", "content": later, "turn": 3, "is_correction": correction}
        ),
        conversation_reply=_reply(
            {"kind": "fact", "content": "用户的猫叫南枝", "basis": "user_agreement", "turn": 3}
        ),
        ops=[_lists(replaces=["e0"])],
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("correction", [True, False])
async def test_an_agreement_that_reaches_the_store_after_the_change_follows_the_change_mark(
    correction: bool,
) -> None:
    """场景 j(用户 11-16「记成历史」;11-17 选 (b)):「算了,还是叫松风」按 C3 是更正 → 本任务
    先写下它时带着更正标记,倒过来「它退南枝」过得了 → 南枝写成历史行、连到松风。同一句若没带
    更正标记(提炼没认出改口),倒过来过不了 → 两条并存、南枝连着松风。"""
    c = _Ctx()
    store = InMemoryMemoryStore()
    llm = _first_and_second("用户的猫叫松风", correction=correction)
    embeddings = {"用户的猫叫松风": _E, "用户的猫叫南枝": _at(0.9)}
    history, coexists = _count("written_as_history"), _count("older_coexists")
    await _flush(c, store, _agreed_then_renamed("算了,还是叫松风"), llm, embeddings)
    later = next(r for r in store._rows if r.content == "用户的猫叫松风")
    earlier = next(r for r in store._rows if r.content == "用户的猫叫南枝")
    assert (later.origin, earlier.origin) == ("user_stated", "user_confirmed")
    assert later.invalid_at is None and later.superseded_by is None
    if correction:
        assert earlier.invalid_at is not None and earlier.superseded_by == later.id
    else:
        assert _coexist_linked(store, later, earlier)
    assert (_count("written_as_history") - history, _count("older_coexists") - coexists) == (
        int(correction),
        int(not correction),
    )


@pytest.mark.asyncio
async def test_a_quiz_answer_written_first_in_one_task_does_not_make_the_agreement_history() -> (
    None
):
    """倒过来的 T39(审查 MEDIUM-2 选 (b) 时要挡住的那一半):一个任务里,用户先同意了 Y(第二次,
    用户确认),后来被考时答了一个错值 W(第一次,用户说的、不带更正标记)。W 先写下,Y 后到;模型
    把 W 列进 Y 的 ``replaces`` → Y 不写成历史行:两条并存、Y 连着 W。"""
    c = _Ctx()
    store = InMemoryMemoryStore()
    messages = [
        _user("帮我查一下我的工号", _T0, "u1"),
        _assistant("是 A1407 吗?", _T0 + timedelta(seconds=5), "a1"),
        _user("对", _T0 + timedelta(minutes=1), "u2"),
        _assistant("好的。", _T0 + timedelta(minutes=1, seconds=5), "a2"),
        _user("A1074", _T0 + timedelta(minutes=5), "u3"),
        _assistant("不对,是 A1407。", _T0 + timedelta(minutes=5, seconds=5), "a3"),
    ]
    llm = _RoutingLLM(
        user_reply=_reply(
            {"kind": "fact", "content": "用户的工号是 A1074", "turn": 3, "is_correction": False}
        ),
        conversation_reply=_reply(
            {"kind": "fact", "content": "用户的工号是 A1407", "basis": "user_agreement", "turn": 3}
        ),
        ops=[_lists(replaces=["e0"])],
    )
    embeddings = {"用户的工号是 A1074": _E, "用户的工号是 A1407": _at(0.95)}
    history = _count("written_as_history")
    await _flush(c, store, messages, llm, embeddings)
    w = next(r for r in store._rows if r.content == "用户的工号是 A1074")
    y = next(r for r in store._rows if r.content == "用户的工号是 A1407")
    assert y.invalid_at is None and w.invalid_at is None
    assert _coexist_linked(store, w, y)
    assert _count("written_as_history") == history
