"""B-300 重做 PR S —— 增量切片(B-176 M2-1)与写回的失败语义(设计 §7.6、§8、§11.6)。

* 起点之后的整轮是新段;起点之前取几行作上下文(第二次:最近 4 行、至少含一条 ``[assistant]``;
  第一次:最近 2 条用户输入),放在 ``=== new ===`` 之上,编号连续;
* 先按整段对话切轮、编序号,再按起点过滤(证据指针的序号是整段对话里的);起点落在一轮中间,
  那一轮不进新段、计数;没有更早任务时取开轮时间在窗口内的轮次(没有时间戳的算更早的);
* 新段超过 32,000 字(第二次的完整渲染)时从最早的整轮丢起、计数,两次用同一批轮次;
* 第 2 次起的尝试按整轮对半切成至多两块,每块带自己的上下文;
* 语言按整段对话里用户的文字判;新段为空不调模型;
* 后台任务(``raise_on_failure``):模型出错、解析失败、截断、向量化 / 去重合并 / 写库失败一律抛给
  任务重试;只有给了失败队列(最后一次尝试)且提炼已成功时,坏在之后的才进失败队列;
* 压缩前抢存不再提炼;本轮内写回(落任务失败的退路)只做本轮,等于最后一次尝试。
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage

from expert_work.common.memory_language import (
    detect_user_language,
    extraction_language_section,
)
from expert_work.common.message_stamp import STAMP_CREATED_AT
from expert_work.common.supersede import mark_superseded
from expert_work.common.user_input import mark_draft_turn, user_input_kwargs
from expert_work.persistence import InMemoryMemoryStore
from expert_work.persistence.memory import InMemoryMemoryWritebackJobStore
from expert_work.protocol import MemoryItem
from expert_work.runtime.cancellation import CancellationToken
from orchestrator.graph_builder import memory as memory_module
from orchestrator.graph_builder.memory import (
    ExtractionReplyError,
    SegmentStart,
    flush_messages_with_outcome,
    make_memory_writeback_node,
    make_pre_compaction_flush,
)
from orchestrator.llm import FakeEmbedder

_DIM = 16
_TENANT = UUID("00000000-0000-4000-8000-0000000000c1")
_USER = UUID("00000000-0000-4000-8000-0000000000c2")
_NOW = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)

PASS1_MARK = "The other side of the conversation is not shown."
PASS2_MARK = "Each line starts with its number in brackets and a tag:"
_EMPTY = '{"memories": []}'


def _reply(*memories: dict[str, Any]) -> str:
    return json.dumps({"memories": list(memories)}, ensure_ascii=False)


@dataclass
class _LLM:
    user_reply: str = _EMPTY
    conversation_reply: str = _EMPTY
    ops_reply: str | None = None
    fail_extraction: Exception | None = None
    calls: list[list[BaseMessage]] = field(default_factory=list)

    @staticmethod
    def pass_of(messages: Sequence[BaseMessage]) -> str:
        system = str(messages[0].content)
        if PASS1_MARK in system:
            return "user"
        if PASS2_MARK in system:
            return "conversation"
        return "reconcile"

    def inputs(self, which: str) -> list[str]:
        return [str(c[1].content) for c in self.calls if self.pass_of(c) == which]

    def systems(self, which: str) -> list[str]:
        return [str(c[0].content) for c in self.calls if self.pass_of(c) == which]

    async def __call__(self, *, messages: Sequence[BaseMessage], tools: Sequence[Any]) -> AIMessage:
        del tools
        self.calls.append(list(messages))
        which = self.pass_of(messages)
        if which == "reconcile":
            if self.ops_reply is None:
                raise TimeoutError("reconcile model timed out")
            return AIMessage(content=self.ops_reply)
        if self.fail_extraction is not None:
            raise self.fail_extraction
        return AIMessage(content=self.user_reply if which == "user" else self.conversation_reply)


def _stamp(at: datetime | None) -> dict[str, Any]:
    return {STAMP_CREATED_AT: at.isoformat()} if at is not None else {}


def _user(text: str, at: datetime | None = _NOW, *, msg_id: str | None = None) -> HumanMessage:
    return HumanMessage(
        content=text,
        id=msg_id,
        additional_kwargs={
            **user_input_kwargs(text=text, images=0, files=0, external_blocks=0),
            **_stamp(at),
        },
    )


def _assistant(text: str, at: datetime | None = _NOW, *, msg_id: str | None = None) -> AIMessage:
    return AIMessage(content=text, id=msg_id, additional_kwargs=_stamp(at))


def _turns(n: int, *, start: int = 0) -> list[BaseMessage]:
    out: list[BaseMessage] = []
    for i in range(start, start + n):
        out += [_user(f"U{i}", msg_id=f"u{i}"), _assistant(f"A{i}", msg_id=f"a{i}")]
    return out


async def _flush(
    messages: Sequence[BaseMessage],
    llm: _LLM,
    *,
    store: InMemoryMemoryStore | None = None,
    embedder: Any = None,
    **kwargs: Any,
) -> Any:
    return await flush_messages_with_outcome(
        messages,
        memory_store=store or InMemoryMemoryStore(),
        embedder=embedder or FakeEmbedder(dim=_DIM),
        llm_caller=llm,  # type: ignore[arg-type]
        tenant_id=_TENANT,
        user_id=_USER,
        thread_id=uuid4(),
        token=CancellationToken(),
        **kwargs,
    )


@pytest.fixture(autouse=True)
def _utc_agent_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXPERT_WORK_TIMEZONE", "UTC")


@pytest.fixture
def events(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, int]]:
    seen: list[tuple[str, int]] = []
    monkeypatch.setattr(
        memory_module,
        "record_memory_writeback_segment",
        lambda *, event, count=1: seen.append((event, count)),
    )
    return seen


# ─── 起点、上下文、编号 ──────────────────────────────────────────────────


async def test_only_the_turns_after_the_start_are_new_with_context_above() -> None:
    """起点 = 第 3 轮的开轮消息:第二次给最近 4 行作上下文,第一次给最近 2 条用户输入;编号连续。"""
    llm = _LLM()
    await _flush(_turns(3), llm, segment=SegmentStart(message_index=4))
    assert llm.inputs("conversation") == [
        "\n".join(
            [
                "[1] [user] [2026-11-15] U0",
                "[2] [assistant] [2026-11-15] A0",
                "[3] [user] [2026-11-15] U1",
                "[4] [assistant] [2026-11-15] A1",
                "=== new ===",
                "[5] [user] [2026-11-15] U2",
                "[6] [assistant] [2026-11-15] A2",
            ]
        )
    ]
    assert llm.inputs("user") == [
        "[1] [2026-11-15] U0\n[2] [2026-11-15] U1\n=== new ===\n[3] [2026-11-15] U2"
    ]


async def test_the_context_reaches_back_to_an_assistant_line() -> None:
    """最近 4 行里没有 ``[assistant]`` 时往前补到第一条 ``[assistant]``(§8.2 第 3 步)。"""

    def attached(text: str, msg_id: str) -> HumanMessage:
        return HumanMessage(
            content=text,
            id=msg_id,
            additional_kwargs=user_input_kwargs(text=text, images=1, files=0, external_blocks=0),
        )

    tool_call = {"name": "lookup", "args": {}, "id": "c"}
    messages: list[BaseMessage] = [
        _user("U0"),
        _assistant("A0"),
        attached("U1", "u1"),
        AIMessage(content="", tool_calls=[{**tool_call, "id": "c1"}]),
        attached("U2", "u2"),
        AIMessage(content="", tool_calls=[{**tool_call, "id": "c2"}]),
        _user("U3"),
        _assistant("A3"),
    ]
    llm = _LLM()
    await _flush(messages, llm, segment=SegmentStart(message_index=6))
    [rendered] = llm.inputs("conversation")
    above = rendered.split("=== new ===")[0].splitlines()
    assert [line.split("] ", 1)[1] for line in above] == [
        "[assistant] [2026-11-15] A0",
        "[user] U1",
        "[attached data] images: 1, files: 0, external blocks: 0",
        "[activity] tools called this turn: lookup ×1",  # noqa: RUF001
        "[user] U2",
        "[attached data] images: 1, files: 0, external blocks: 0",
        "[activity] tools called this turn: lookup ×1",  # noqa: RUF001
    ]


async def test_evidence_index_is_the_position_in_the_whole_conversation() -> None:
    """先整段编序号、再按起点过滤:证据指针的序号是整段对话里的;上下文行不进新段。"""
    llm = _LLM(
        user_reply=_reply({"kind": "fact", "content": "说过 U2", "turn": 3}),
        conversation_reply=_reply(
            {"kind": "fact", "content": "答过 U2", "basis": "user_answer", "turn": 5},
            {"kind": "fact", "content": "上下文里的", "basis": "assistant", "turn": 4},
        ),
    )
    store = InMemoryMemoryStore()
    messages = [SystemMessage(content="sys"), *_turns(3)]
    await _flush(messages, llm, store=store, segment=SegmentStart(message_index=5))
    rows = {r.content: r for r in await store.list_for_user(tenant_id=_TENANT, user_id=_USER)}
    assert (rows["说过 U2"].evidence_message_id, rows["说过 U2"].evidence_message_index) == (
        "u2",
        5,
    )
    assert (rows["答过 U2"].evidence_message_id, rows["答过 U2"].evidence_message_index) == (
        "u2",
        5,
    )
    assert rows["答过 U2"].origin == "user_confirmed"
    assert rows["上下文里的"].evidence_message_id is None


async def test_lines_in_the_new_segment_count_as_new() -> None:
    """``in_new_segment`` = 不是上下文行(切片之后新段里的每一行都是第一次被提炼)。"""
    llm = _LLM(
        user_reply=_reply(
            {"kind": "fact", "content": "上下文", "turn": 1},
            {"kind": "fact", "content": "新段", "turn": 3},
        )
    )
    out, _failed = await memory_module._extract_two_passes(
        _turns(3),
        user_text_messages=(),
        llm_caller=llm,
        token=CancellationToken(),
        log_label="t",
        segment=SegmentStart(message_index=4),
    )
    by = {m.content: m for m in out}
    assert (by["上下文"].in_new_segment, by["新段"].in_new_segment) == (False, True)


async def test_a_turn_straddling_the_start_is_left_out_and_counted(
    events: list[tuple[str, int]],
) -> None:
    llm = _LLM()
    messages = [
        _user("U0"),
        AIMessage(content="", tool_calls=[{"name": "t", "args": {}, "id": "c"}]),
        _assistant("A0"),
        *_turns(1, start=1),
    ]
    await _flush(messages, llm, segment=SegmentStart(message_index=2))
    [rendered] = llm.inputs("conversation")
    assert rendered.split("=== new ===")[1].splitlines()[1:] == [
        "[4] [user] [2026-11-15] U1",
        "[5] [assistant] [2026-11-15] A1",
    ]
    assert ("straddling_turn_skipped", 1) in events


async def test_without_an_earlier_job_only_recent_turns_are_new() -> None:
    """没有更早的任务:开轮时间在窗口内的轮次是新段;没有时间戳的算更早的。"""
    old = _NOW - timedelta(days=31)
    messages: list[BaseMessage] = [
        _user("OLD", old),
        _assistant("old reply", old),
        _user("NOSTAMP", None),
        _assistant("nostamp reply", None),
        _user("RECENT", _NOW - timedelta(days=2)),
        _assistant("recent reply", _NOW - timedelta(days=2)),
    ]
    llm = _LLM()
    await _flush(messages, llm, segment=SegmentStart(opened_since=_NOW - timedelta(days=30)))
    [rendered] = llm.inputs("user")
    above, below = rendered.split("=== new ===")
    assert "RECENT" in below and "OLD" not in below and "NOSTAMP" not in below
    assert "NOSTAMP" in above


async def test_an_empty_segment_calls_no_model(events: list[tuple[str, int]]) -> None:
    llm = _LLM()
    outcome = await _flush(_turns(2), llm, segment=SegmentStart(message_index=4))
    assert llm.calls == []
    assert (outcome.written, outcome.failed) == (0, False)
    assert ("empty", 1) in events


async def test_a_draft_turn_in_the_segment_is_skipped() -> None:
    draft = [mark_draft_turn(_user("DRAFT")), _assistant("draft reply")]
    llm = _LLM()
    await _flush([*_turns(1), *draft, *_turns(1, start=2)], llm, segment=SegmentStart(2))
    [rendered] = llm.inputs("conversation")
    assert "DRAFT" not in rendered and "U2" in rendered.split("=== new ===")[1]


async def test_a_superseded_turn_in_the_segment_is_skipped() -> None:
    """重新生成 / 编辑重发:旧轮原地打「已取代」标记、序号保留;新轮在后、被提炼,旧轮跳过。"""
    replaced = [
        mark_superseded(m, new_run_id="r2", now=_NOW) for m in (_user("OLDASK"), _assistant("x"))
    ]
    llm = _LLM()
    await _flush(
        [*_turns(1), *replaced, *_turns(1, start=2)], llm, segment=SegmentStart(message_index=2)
    )
    [rendered] = llm.inputs("conversation")
    assert "OLDASK" not in rendered and "U2" in rendered.split("=== new ===")[1]


async def test_a_regenerated_turn_keeps_its_original_send_date() -> None:
    """重新生成:用户消息是新写入的(``expert_work_created_at`` 今天),白名单里的 ``sent_at``
    是原来那天 —— 行里的日期用原来那天。"""
    regenerated = HumanMessage(
        content="U1",
        additional_kwargs={
            **user_input_kwargs(
                text="U1", images=0, files=0, external_blocks=0, sent_at="2026-11-01T08:00:00+00:00"
            ),
            **_stamp(_NOW),
        },
    )
    llm = _LLM()
    await _flush([*_turns(1), regenerated, _assistant("A1")], llm, segment=SegmentStart(2))
    assert llm.inputs("user")[0].endswith("[2] [2026-11-01] U1")


async def test_without_a_segment_the_whole_transcript_is_new() -> None:
    """内联写回模式(逃生口)保留整段重抽:没有上下文行,标记在最上面。"""
    llm = _LLM()
    await _flush(_turns(2), llm)
    [rendered] = llm.inputs("conversation")
    assert rendered.splitlines()[0] == "=== new ==="
    assert "U0" in rendered and "U1" in rendered


# ─── 上限、分块、语言 ───────────────────────────────────────────────────


def _long_turns(n: int) -> list[BaseMessage]:
    out: list[BaseMessage] = []
    for i in range(n):
        out += [_user(f"T{i:02d}" + "字" * 990), _assistant(f"R{i:02d}" + "答" * 990)]
    return out


async def test_an_oversized_segment_keeps_the_newest_whole_turns(
    events: list[tuple[str, int]],
) -> None:
    """30 天、几十轮的段:只提炼最近的整轮(第二次的完整输入 ≤ 32,000 字),更早的整轮丢掉、计数;
    两次用同一批轮次;每次提炼仍只调一次模型。"""
    llm = _LLM()
    await _flush(_long_turns(40), llm, segment=SegmentStart(message_index=0))
    [conversation] = llm.inputs("conversation")
    [user] = llm.inputs("user")
    assert len(conversation) <= memory_module._EXTRACT_INPUT_CHAR_CAP
    new = conversation.split("=== new ===")[1]
    kept = [i for i in range(40) if f"T{i:02d}" in new]
    assert kept == list(range(40 - len(kept), 40)) and 10 <= len(kept) < 40
    assert all(f"R{i:02d}" in new for i in kept)  # whole turns, no cut line
    assert [i for i in range(40) if f"T{i:02d}" in user.split("=== new ===")[1]] == kept
    assert ("turns_over_cap_dropped", 40 - len(kept)) in events
    assert len(llm.calls) == 2


async def test_a_retry_splits_the_segment_into_two_halves_of_whole_turns(
    events: list[tuple[str, int]],
) -> None:
    llm = _LLM()
    await _flush(_turns(4), llm, segment=SegmentStart(message_index=0), halves=True)
    first, second = llm.inputs("conversation")
    assert first.splitlines()[0] == "=== new ==="
    assert "U0" in first and "U1" in first and "U2" not in first
    above, below = second.split("=== new ===")
    assert "U1" in above and "U2" in below and "U3" in below and "U0" not in below
    assert len(llm.inputs("user")) == 2
    assert ("halved", 1) in events


async def test_a_single_turn_is_retried_as_it_is() -> None:
    llm = _LLM()
    await _flush(_turns(2), llm, segment=SegmentStart(message_index=2), halves=True)
    assert len(llm.inputs("conversation")) == 1


async def test_language_comes_from_the_whole_conversation() -> None:
    """新段只有一句「18号」:语言仍按整段对话里用户的文字判出。"""
    earlier = "帮我看看下周去成都的机票,最好是上午出发的直飞航班"
    messages: list[BaseMessage] = [
        _user(earlier),
        _assistant("回程想订哪天?"),
        _user("18号"),
        _assistant("好的"),
    ]
    language = detect_user_language([earlier, "18号"])
    assert language is not None and detect_user_language(["18号"]) is None
    llm = _LLM()
    await _flush(messages, llm, segment=SegmentStart(message_index=2))
    for system in llm.systems("user") + llm.systems("conversation"):
        assert system.endswith(extraction_language_section(language))


# ─── 失败语义(§7.6)────────────────────────────────────────────────────


@dataclass
class _RecordingDLQ:
    enqueued: list[list[tuple[str, str]]] = field(default_factory=list)

    async def enqueue(self, **kwargs: Any) -> None:
        self.enqueued.append(list(kwargs["extracted"]))


async def test_a_model_error_raises_to_the_task_retry() -> None:
    llm = _LLM(fail_extraction=TimeoutError("vendor timed out"))
    dlq = _RecordingDLQ()
    with pytest.raises(TimeoutError):
        await _flush(_turns(1), llm, segment=SegmentStart(0), raise_on_failure=True, dlq=dlq)
    assert dlq.enqueued == []  # extraction never succeeded: nothing for the DLQ


@pytest.mark.parametrize(
    "reply",
    [
        AIMessage(content="no json here"),
        AIMessage(
            content=_reply({"kind": "fact", "content": "x"}),
            response_metadata={"finish_reason": "length"},
        ),
    ],
    ids=["parse_failed", "truncated"],
)
async def test_an_unusable_or_truncated_extraction_raises(reply: AIMessage) -> None:
    async def llm(*, messages: Sequence[BaseMessage], tools: Sequence[Any]) -> AIMessage:
        del messages, tools
        return reply

    store = InMemoryMemoryStore()
    with pytest.raises(ExtractionReplyError):
        await _flush(_turns(1), llm, store=store, segment=SegmentStart(0), raise_on_failure=True)  # type: ignore[arg-type]
    assert await store.list_for_user(tenant_id=_TENANT, user_id=_USER) == []


class _BrokenEmbedder:
    async def embed(self, texts: Sequence[str], *, tenant_id: UUID) -> list[tuple[float, ...]]:
        raise ConnectionError("embedder down")


class _BrokenWriteStore(InMemoryMemoryStore):
    async def write(self, items: Sequence[MemoryItem]) -> Any:
        raise ConnectionError("db down")


_ONE = _reply({"kind": "fact", "content": "likes tea", "turn": 1})


async def test_an_embed_failure_raises_unless_this_is_the_last_attempt() -> None:
    with pytest.raises(ConnectionError):
        await _flush(
            _turns(1),
            _LLM(user_reply=_ONE),
            embedder=_BrokenEmbedder(),
            segment=SegmentStart(0),
            raise_on_failure=True,
        )
    dlq = _RecordingDLQ()
    outcome = await _flush(
        _turns(1),
        _LLM(user_reply=_ONE),
        embedder=_BrokenEmbedder(),
        segment=SegmentStart(0),
        raise_on_failure=True,
        dlq=dlq,
    )
    assert (outcome.written, outcome.failed) == (0, True)
    assert dlq.enqueued == [[("fact", "likes tea")]]


async def test_a_store_failure_raises_to_the_task_retry() -> None:
    with pytest.raises(ConnectionError):
        await _flush(
            _turns(1),
            _LLM(user_reply=_ONE),
            store=_BrokenWriteStore(),
            segment=SegmentStart(0),
            raise_on_failure=True,
        )


async def _seeded_store() -> InMemoryMemoryStore:
    store = InMemoryMemoryStore()
    embedder = FakeEmbedder(dim=_DIM)
    [vector] = await embedder.embed(["likes coffee"], tenant_id=_TENANT)
    await store.write(
        [
            MemoryItem(
                id=uuid4(),
                tenant_id=_TENANT,
                user_id=_USER,
                kind="fact",
                content="likes coffee",
                embedding=vector,
                origin="user_stated",
            )
        ]
    )
    return store


async def test_a_reconcile_failure_raises_to_the_task_retry() -> None:
    """R 的过渡做法(直接写入还没写的)撤掉:去重合并出错抛给任务重试,什么都不直接写。"""
    store = await _seeded_store()
    llm = _LLM(user_reply=_ONE, conversation_reply=_EMPTY)
    with pytest.raises(memory_module.ReconcileFailedError):
        await _flush(
            _turns(1),
            llm,
            store=store,
            segment=SegmentStart(0),
            raise_on_failure=True,
            reconcile=True,
        )
    assert [r.content for r in await store.list_for_user(tenant_id=_TENANT, user_id=_USER)] == [
        "likes coffee"
    ]


async def test_on_the_last_attempt_a_reconcile_failure_hands_the_unwritten_to_the_dlq() -> None:
    store = await _seeded_store()
    llm = _LLM(
        user_reply=_ONE,
        conversation_reply=_reply(
            {"kind": "fact", "content": "assistant found tea", "basis": "assistant", "turn": 2}
        ),
    )
    dlq = _RecordingDLQ()
    outcome = await _flush(
        _turns(1),
        llm,
        store=store,
        segment=SegmentStart(0),
        raise_on_failure=True,
        reconcile=True,
        dlq=dlq,
    )
    assert (outcome.written, outcome.failed) == (0, True)
    assert dlq.enqueued == [[("fact", "likes tea"), ("fact", "assistant found tea")]]
    assert [r.content for r in await store.list_for_user(tenant_id=_TENANT, user_id=_USER)] == [
        "likes coffee"
    ]


# ─── 抢存、本轮内写回 ───────────────────────────────────────────────────


async def test_the_pre_compaction_flush_no_longer_extracts(events: list[tuple[str, int]]) -> None:
    flush = make_pre_compaction_flush()
    written = await flush(
        _turns(2),
        {"configurable": {"tenant_id": str(_TENANT), "user_id": str(_USER)}},
        CancellationToken(),
    )
    assert written == 0
    assert ("precompaction_flush_skipped", 1) in events


class _BrokenJobStore(InMemoryMemoryWritebackJobStore):
    async def enqueue(self, **kwargs: Any) -> UUID:
        del kwargs
        raise RuntimeError("db down")


def _node_config() -> dict[str, Any]:
    return {
        "configurable": {
            "tenant_id": str(_TENANT),
            "user_id": str(_USER),
            "thread_id": str(uuid4()),
            "run_id": str(uuid4()),
            "checkpoint_map": {"": "ckpt-1"},
        }
    }


async def test_the_in_turn_fallback_extracts_only_this_turn() -> None:
    """后台模式下落任务失败:本轮内写回只做本轮(更早的轮次作上下文)。"""
    llm = _LLM()
    node = make_memory_writeback_node(
        memory_store=InMemoryMemoryStore(),
        embedder=FakeEmbedder(dim=_DIM),
        llm_caller=llm,  # type: ignore[arg-type]
        agent_name="a",
        agent_version="1",
        writeback_jobs=_BrokenJobStore(),
    )
    state = {"messages": [SystemMessage(content="s"), *_turns(3)], "step_count": 0, "max_steps": 5}
    await node(state, _node_config())  # type: ignore[arg-type]
    [rendered] = llm.inputs("conversation")
    above, below = rendered.split("=== new ===")
    assert "U2" in below and "U1" not in below and "U1" in above


async def test_the_in_turn_fallback_is_a_last_attempt() -> None:
    """它没有任务、没有重试:提炼成功之后出错 → 进失败队列,不抛。"""
    llm = _LLM(user_reply=_ONE)
    dlq = _RecordingDLQ()
    node = make_memory_writeback_node(
        memory_store=_BrokenWriteStore(),
        embedder=FakeEmbedder(dim=_DIM),
        llm_caller=llm,  # type: ignore[arg-type]
        dlq=dlq,  # type: ignore[arg-type]
        agent_name="a",
        agent_version="1",
        writeback_jobs=_BrokenJobStore(),
    )
    state = {"messages": _turns(1), "step_count": 0, "max_steps": 5}
    out = await node(state, _node_config())  # type: ignore[arg-type]
    assert out["memory_writeback_failed"] is True
    assert dlq.enqueued == [[("fact", "likes tea")]]
