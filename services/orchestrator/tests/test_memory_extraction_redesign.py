"""B-300 重做 PR X —— 提炼(设计 §4、§5、§7.4、§11.6、附录 A、附录 C)。

* 两次提炼的输入每行带编号 ``[n]``,``=== new ===`` 一行之上是上下文(切片是 PR S 的事;切片之前
  上下文为空,标记在最上面,整段对话都在它下面 —— 与今天一样整段重抽);
* ``[user]`` / ``[assistant]`` 行在标签后带发送日期;
* 第二次:``basis`` + ``turn`` 由程序核对 —— 挂在新段里的 ``[user]`` 行、上一行是 ``[assistant]``
  → ``user_confirmed``(置信度 ≤ 0.8);否则降为 ``assistant_derived``(≤ 0.6)并按原因计数;
* 第一次的产出永远是 ``user_stated``,第二次的永远不是;
* 证据指针 = ``turn`` 那一行对应的消息 id + 序号;越界、缺失 → 留空、计数,记忆照写;指向上下文行
  → 整条丢弃、计数 ``context_line_dropped``(测试验收 11-16);
* 事件日期:解析不了、晚于那一行的日期 → 置空、计数;与类型无关(读数是「事实」,各智能体共享;
  「去掉自检,读数共享」,用户 2026-11-16);
* ``line_at`` = 那一行的发送时间;``in_new_segment`` = 那一行属于本次写回负责的轮次(切片之前 =
  最后一轮,即落这个写回任务的那一轮)。
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage

from expert_work.common.message_stamp import STAMP_CREATED_AT
from expert_work.common.user_input import user_input_kwargs
from expert_work.persistence import InMemoryMemoryStore
from expert_work.protocol import MemoryItem
from expert_work.runtime.cancellation import CancellationToken
from orchestrator.graph_builder import memory as memory_module
from orchestrator.graph_builder import memory_lines
from orchestrator.graph_builder.memory import (
    _CONVERSATION_EXTRACTION_SYSTEM,
    _USER_TEXT_EXTRACTION_SYSTEM,
    ExtractedMemory,
    flush_messages_with_outcome,
)
from orchestrator.graph_builder.memory_lines import ExtractionLine, evidence_pointer
from orchestrator.graph_builder.memory_slice import SegmentStart, Turn, plan_slice
from orchestrator.llm import FakeEmbedder

_DIM = 32
_TENANT = UUID("00000000-0000-4000-8000-0000000000a7")
_USER = UUID("00000000-0000-4000-8000-0000000000b7")

_TODAY = date(2026, 3, 2)

PASS1_MARK = "The other side of the conversation is not shown."
PASS2_MARK = "Each line starts with its number in brackets and a tag:"

_APPENDIX_A = """\
You are a memory extraction module. The input is part of a conversation, one message per
line. Each line starts with its number in brackets and a tag:
- [user] — text the user typed, followed by the date it was sent.
- [task] — text that started a turn but was not typed by the user in this conversation
  (for example a scheduled or delegated task).
- [attached data] — a platform note that the message before it also carried images, files
  or external data. Their content is not shown.
- [assistant] — the assistant's final reply of a turn, followed by the date it was sent. It
  may rest on tool results, attachments or the assistant's own inference, which you cannot
  see and any of which can be wrong.
- [activity] — a platform note listing the tools called in that turn.
Lines above the line "=== new ===" were handled earlier; they are there so you can
understand the lines below it. Take every memory from the lines below "=== new ===".

Record two sorts of durable, reusable memories:
1. What the assistant did, produced, found, concluded or proposed. Set "basis" to
   "assistant" and "turn" to the number of the [assistant] line that says it. In the
   content, say that the assistant found, judged or proposed it, and keep the assistant's
   own hedging (such as "about", "looks like", "probably").
2. What the user settled by replying to the assistant's line just above: an answer to its
   question ("basis": "user_answer"), or agreeing to or rejecting what it proposed or
   reported ("basis": "user_agreement"). Set "turn" to the number of that [user] line. Read
   the two lines together and write the whole fact they settle, the way the user would
   state it, keeping any doubt the user expressed. A line that answers two questions gives
   two memories. When the user agrees to one proposal in a reply that also states other
   findings, only that proposal is the user's agreement; the findings stay the assistant's.
When a reply changes or rejects something said earlier, set "is_correction" to true and
write both what it was and what it is now.
A value that happened or was observed on one day — a reading, a count, a payment made —
gets "event_date": the day it refers to, as YYYY-MM-DD, counted from the date on its line
when the line says "today", "this morning", "yesterday" and the like. Plans, arrangements,
preferences and states get "event_date": null. A date alone does not make something an
event, and neither does the day something was said or asked about: a current status is
written with "(as of <date>)" in the content, and that date is not an "event_date". For
example:
- "The kitchen renovation is waiting for the tiles (as of 2026-04-03)." — a state:
  "event_date": null.
- "On 2026-04-03 the tiles for the kitchen were delivered." — it happened that day:
  "event_date": "2026-04-03".
Passwords, one-time codes, bank card or bank account numbers and other secrets are never
recorded, even when the user gives one in reply to the assistant.
Write each memory as one sentence that can be understood on its own, in the language of
the conversation, with names, codes, numbers and units exactly as written.
For each memory also give "kind" ("fact" for something that stays true, "episodic" for
something done or decided), "importance" (0-1: how reusable it is in future sessions),
"confidence" (0-1: how well the lines support it) and "expected_valid_days" (a rough number
of days it should stay valid, or null).

Examples (lines, then the output, then why):

Example 1
[1] [user] [2026-03-02] 帮我看看下周去成都的机票
[2] [assistant] [2026-03-02] 3 月 9 日上午有两班直飞,我先按这两班比价。回程想订哪天?
=== new ===
[3] [user] [2026-03-02] 18号
{"memories": [{"kind": "fact", "content": "用户的回程日期定在 2026 年 3 月 18 日",
"basis": "user_answer", "turn": 3, "event_date": null, "importance": 0.5,
"confidence": 0.9, "expected_valid_days": 30, "is_correction": false}]}
Why: line 3 says nothing alone; read with line 2 it answers the assistant's question. A
planned date is not something that happened, so event_date is null.

Example 2
=== new ===
[1] [user] [2026-04-11] The release keeps slipping. Any idea how to fix the review step?
[2] [activity] tools called this turn: team_calendar ×1
[3] [assistant] [2026-04-11] Two of the reviewers are on leave this month. I suggest Priya Nair reviews every release branch until they are back.
[4] [user] [2026-04-11] ok, do that
{"memories": [{"kind": "fact", "content": "The assistant found that two of the reviewers
are on leave this month.", "basis": "assistant", "turn": 3, "event_date": null,
"importance": 0.4, "confidence": 0.7, "expected_valid_days": 30, "is_correction": false},
{"kind": "episodic", "content": "Priya Nair reviews every release branch until the
reviewers on leave are back.", "basis": "user_agreement", "turn": 4, "event_date": null,
"importance": 0.7, "confidence": 0.9, "expected_valid_days": 30, "is_correction": false}]}
Why: line 4 agrees to the proposal in line 3, so the agreed plan is recorded on line 4;
the finding in the same reply stays the assistant's.

Example 3
=== new ===
[1] [user] [2026-05-20] 我上周买的台灯还没到
[2] [activity] tools called this turn: order_lookup ×1
[3] [assistant] [2026-05-20] 查到一笔台灯订单,订单号 RX-4471-B,目前在中转站。是这一笔吗?
[4] [user] [2026-05-20] 对
{"memories": [{"kind": "fact", "content": "用户上周买的台灯订单号是 RX-4471-B",
"basis": "user_agreement", "turn": 4, "event_date": null, "importance": 0.6,
"confidence": 0.9, "expected_valid_days": 60, "is_correction": false},
{"kind": "episodic", "content": "助手查到这笔台灯订单在 2026-05-20 时还在中转站",
"basis": "assistant", "turn": 3, "event_date": null, "importance": 0.3,
"confidence": 0.6, "expected_valid_days": 7, "is_correction": false}]}
Why: line 4 confirms the order number the assistant looked up; where the parcel is stays
the assistant's finding, and it is a state that can change, so event_date is null.

Example 4
[1] [user] [2026-06-03] Necesito cambiar las pastillas de freno.
[2] [assistant] [2026-06-03] Para tu coche, el modelo T-200 de 2019, las pastillas delanteras cuestan unos 80 euros. ¿Es ese tu modelo?
=== new ===
[3] [user] [2026-06-03] no, es el T-250
{"memories": [{"kind": "fact", "content": "El coche del usuario es el modelo T-250, no el
T-200 de 2019 que suponía el asistente.", "basis": "user_agreement", "turn": 3,
"event_date": null, "importance": 0.7, "confidence": 0.9, "expected_valid_days": 1095,
"is_correction": true}]}
Why: line 3 rejects what line 2 assumed and gives the new value, so it is a correction and
both values are written.

Example 5
=== new ===
[1] [user] [2026-07-08] Our hall holds 120 people. Can you check the booking page to be sure?
[2] [activity] tools called this turn: web_fetch ×1
[3] [assistant] [2026-07-08] The booking page lists a capacity of 120 for the main hall.
{"memories": [{"kind": "fact", "content": "The assistant found that the booking page lists
a capacity of 120 for the main hall.", "basis": "assistant", "turn": 3, "event_date": null,
"importance": 0.5, "confidence": 0.6, "expected_valid_days": 365, "is_correction": false}]}
Why: record what the assistant found even when it matches what the user said; matching
memories are merged in a later step.

Example 6
=== new ===
[1] [user] [2026-08-15] 发你一张月季叶子的照片
[2] [attached data] images: 1, files: 0, external blocks: 0
[3] [assistant] [2026-08-15] 从照片看,叶片上的黑斑像是真菌引起的,不过只凭一张照片不好确定。
[4] [user] [2026-08-15] 那浇水要怎么调整?
{"memories": [{"kind": "fact", "content": "助手根据一张照片判断,用户月季叶片上的黑斑可能是真菌引起的,尚未确定",
"basis": "assistant", "turn": 3, "event_date": null, "importance": 0.4,
"confidence": 0.4, "expected_valid_days": 30, "is_correction": false}]}
Why: line 4 moves on to another question without agreeing, so the judgement stays the
assistant's, with its doubt kept.

Example 7
[1] [assistant] [2026-09-01] Sure. What did the electricity meter show this morning?
=== new ===
[2] [user] [2026-09-01] 18342
{"memories": [{"kind": "fact", "content": "The electricity meter read 18342 on the morning
of 2026-09-01.", "basis": "user_answer", "turn": 2, "event_date": "2026-09-01",
"importance": 0.5, "confidence": 0.9, "expected_valid_days": null, "is_correction": false}]}
Why: a reading taken on one day gets that day as event_date, so a reading on another day
is kept next to it.

Example 8
=== new ===
[1] [user] [2026-10-12] 我想报个西班牙语课
[2] [assistant] [2026-10-12] 好的。你现在大概是什么水平?初级、中级还是高级?
[3] [user] [2026-10-12] 应该算中级吧,不太确定
{"memories": [{"kind": "fact", "content": "用户认为自己的西班牙语大概是中级水平,但不太确定",
"basis": "user_answer", "turn": 3, "event_date": null, "importance": 0.6,
"confidence": 0.6, "expected_valid_days": 180, "is_correction": false}]}
Why: the user's own doubt stays in the content and in the confidence.

Example 9
[1] [user] [2026-11-02] I can't sign in to my account.
[2] [assistant] [2026-11-02] I've sent a six-digit code to your phone. What is it?
=== new ===
[3] [user] [2026-11-02] 482913
{"memories": []}
Why: a one-time code is a secret; it is never recorded, even as an answer.

Respond with ONLY a JSON object, no prose and no code fences:
{"memories": [{"kind": "fact" | "episodic", "content": "<one sentence>",
"basis": "assistant" | "user_answer" | "user_agreement", "turn": <line number>,
"event_date": "<YYYY-MM-DD>" | null, "importance": <0-1>, "confidence": <0-1>,
"expected_valid_days": <int or null>, "is_correction": <bool>}]}
If nothing in the lines below "=== new ===" is worth remembering, return {"memories": []}."""  # noqa: E501, RUF001

_DOMAIN_WORDS = (
    "health",
    "medical",
    "patient",
    "blood",
    "glucose",
    "pressure",
    "weight",
    "doctor",
    "clinic",
    "coach",
    "customer",
    "client",
    "sales",
    "legal",
    "contract",
    "lawyer",
    "insurance",
    "健康",
    "医",
    "患者",
    "病",
    "血",
    "体重",
    "教练",
    "客户",
    "销售",
    "合同",
    "律师",
    "保险",
)


def _flat(text: str) -> str:
    return " ".join(text.split())


# ─── 指令 ──────────────────────────────────────────────────────────────


def test_conversation_prompt_is_appendix_a_verbatim() -> None:
    assert _CONVERSATION_EXTRACTION_SYSTEM == _APPENDIX_A


def test_conversation_prompt_keeps_the_secret_rule_and_drops_the_negative_rules() -> None:
    system = _flat(_CONVERSATION_EXTRACTION_SYSTEM)
    assert (
        "Passwords, one-time codes, bank card or bank account numbers and other secrets are "
        "never recorded, even when the user gives one in reply to the assistant." in system
    )
    for gone in (
        "do not record it again here",
        "only confirms what the user currently says",
        "is not recorded as a conclusion on its own",
        "When the assistant only repeats something",
        "the assistant found ...",
    ):
        assert gone not in system


def test_user_text_prompt_has_the_appendix_c_changes() -> None:
    system = _flat(_USER_TEXT_EXTRACTION_SYSTEM)
    assert system.startswith(
        "You are a memory extraction module. The input is messages one user typed in a "
        "conversation, one per line, oldest first, each starting with its number in brackets. "
        'The other side of the conversation is not shown. Lines above the line "=== new ===" '
        "were handled earlier and are there only so you can understand the lines below it; take "
        "every memory from the lines below it. Extract durable, reusable memories"
    )
    assert (
        'a date in brackets after the line number is the day that line was sent, so turn "this '
        'morning", "yesterday" and the like in that line into a date counted from it; in a line '
        "without a date, leave such words as they are." in system
    )
    assert "a line that starts with a date in brackets was sent on that date" not in system
    assert "The input is every message one user typed" not in system
    # 「方案三:折中」(用户 2026-11-16):写进内容的日期不是事件日期;成对的对照例子。
    assert (
        "- A value the user states about themselves or their own things, with the same message "
        "saying what it is — a reading or measurement, an id or number, a current status — is "
        "recorded even though it may change, except passwords, one-time codes, bank card or bank "
        "account numbers and other secrets, which are never recorded. Write a date in the "
        "content so that it is not later taken for a current value: a current status with "
        '"(as of <date>)", and a reading or count with the day it was measured ("measured on '
        '<date>"); an id or a number that names something gets no date. For that date, a date '
        'in brackets after the line number is the day that line was sent, so turn "this '
        'morning", "yesterday" and the like in that line into a date counted from it; in a line '
        "without a date, leave such words as they are. A date written in the content is not by "
        'itself an "event_date" (see below). - A message that only agrees' in system
    )
    # 复审 MEDIUM(11-16):「(as of <date>)」只给当前状态;读数写测得那天;编号不带日期。
    assert 'such as "(as of <date>)", so it is not later taken' not in system
    assert "Write it with the date or time it refers to, so it is not later taken" not in system
    assert (
        '- "is_correction": true ONLY when the same message itself says what is being corrected '
        "and gives the new value (e.g. the user says they moved, changed jobs, renamed a thing); "
        'otherwise false. - "turn": the number of the line that states it. - "event_date": only '
        "for a value that happened or was observed on one day (a reading, a count, a payment "
        "made), that day as YYYY-MM-DD; null for anything else, including an id, a number that "
        "names something, or a current status, even when its date is written in the content. "
        "A date alone does not make something an event, and neither does the day something was "
        'said or asked about. For example: "鱼缸的水泵在等配件(截至 2026-04-03)" — a state: '
        '"event_date": null. "2026-04-03 测得鱼缸水温 26 度" — measured that day: "event_date": '
        '"2026-04-03". Respond with ONLY a JSON object' in system
    )
    assert "episodic" not in system.split('"event_date": only')[1].split("Respond with")[0]
    assert (
        '"expected_valid_days": <int or null>, "is_correction": <bool>, "turn": <line number>, '
        '"event_date": "<YYYY-MM-DD>" or null}]}' in system
    )
    # 其余一字不动(含秘密值那句)。
    assert (
        "except passwords, one-time codes, bank card or bank account numbers and other secrets, "
        "which are never recorded." in system
    )
    assert "- A question, a guess, a hypothetical, or a repeat of what someone else said" in system


@pytest.mark.parametrize("which", ["user", "conversation"])
def test_extraction_prompts_have_no_industry_words(which: str) -> None:
    system = memory_module._extract_system(which, None).lower()
    assert [w for w in _DOMAIN_WORDS if w in system] == []


# ─── 输入:编号、标记、日期 ─────────────────────────────────────────────


_EMPTY = '{"memories": []}'


def _reply(*memories: dict[str, Any]) -> str:
    return json.dumps({"memories": list(memories)}, ensure_ascii=False)


@dataclass
class _ByPassLLM:
    user_reply: str = _EMPTY
    conversation_reply: str = _EMPTY
    calls: list[list[BaseMessage]] = field(default_factory=list)

    def _pass_of(self, messages: Sequence[BaseMessage]) -> str:
        system = str(messages[0].content)
        if PASS1_MARK in system:
            return "user"
        if PASS2_MARK in system:
            return "conversation"
        return "other"

    def input_of(self, which: str) -> str:
        [call] = [c for c in self.calls if self._pass_of(c) == which]
        return str(call[1].content)

    async def __call__(self, *, messages: Sequence[BaseMessage], tools: Sequence[Any]) -> AIMessage:
        del tools
        self.calls.append(list(messages))
        which = self._pass_of(messages)
        return AIMessage(content=self.user_reply if which == "user" else self.conversation_reply)


def _user(text: str, sent_at: str | None = None, *, msg_id: str | None = None) -> HumanMessage:
    return HumanMessage(
        content=text,
        id=msg_id,
        additional_kwargs=user_input_kwargs(
            text=text, images=0, files=0, external_blocks=0, sent_at=sent_at
        ),
    )


def _assistant(text: str, created_at: str | None = None, *, msg_id: str | None = None) -> AIMessage:
    kwargs = {STAMP_CREATED_AT: created_at} if created_at is not None else {}
    return AIMessage(content=text, id=msg_id, additional_kwargs=kwargs)


def _qa() -> list[BaseMessage]:
    """两轮:用户问 → 助手反问 → 用户只答一个值 → 助手回答。"""
    return [
        _user("帮我看看下周去成都的机票", "2026-03-02T01:00:00+00:00", msg_id="u1"),
        _assistant(
            "3 月 9 日上午有两班直飞。回程想订哪天?", "2026-03-02T01:00:05+00:00", msg_id="a1"
        ),
        _user("18号", "2026-03-02T01:01:00+00:00", msg_id="u2"),
        _assistant("好的,按 18 号回程比价。", "2026-03-02T01:01:05+00:00", msg_id="a2"),
    ]


async def _flush(
    messages: Sequence[BaseMessage], llm: _ByPassLLM, store: InMemoryMemoryStore | None = None
) -> tuple[Any, InMemoryMemoryStore]:
    store = store or InMemoryMemoryStore()
    outcome = await flush_messages_with_outcome(
        messages,
        memory_store=store,
        embedder=FakeEmbedder(dim=_DIM),
        llm_caller=llm,
        tenant_id=_TENANT,
        user_id=_USER,
        thread_id=uuid4(),
        token=CancellationToken(),
    )
    return outcome, store


async def _rows(store: InMemoryMemoryStore) -> dict[str, MemoryItem]:
    rows = await store.list_for_user(tenant_id=_TENANT, user_id=_USER, limit=100)
    return {row.content: row for row in rows}


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


async def test_conversation_lines_are_numbered_below_the_new_mark_with_dates() -> None:
    messages: list[BaseMessage] = [
        HumanMessage(content="SEED 每天提醒一次"),
        _assistant("好的", "2026-03-01T08:00:00+00:00"),
        *_qa()[:2],
        _user("18号", "2026-03-02T01:01:00+00:00"),
        AIMessage(content="", tool_calls=[{"name": "flight_search", "args": {}, "id": "c1"}]),
        ToolMessage(content="TOOLSENTINEL", tool_call_id="c1", name="flight_search"),
        _assistant("已按 18 号比价。"),
    ]
    llm = _ByPassLLM()
    await _flush(messages, llm)
    assert llm.input_of("conversation").splitlines() == [
        "=== new ===",
        "[1] [task] SEED 每天提醒一次",
        "[2] [assistant] [2026-03-01] 好的",
        "[3] [user] [2026-03-02] 帮我看看下周去成都的机票",
        "[4] [assistant] [2026-03-02] 3 月 9 日上午有两班直飞。回程想订哪天?",
        "[5] [user] [2026-03-02] 18号",
        "[6] [activity] tools called this turn: flight_search ×1",  # noqa: RUF001
        "[7] [assistant] 已按 18 号比价。",
    ]


async def test_user_text_lines_are_numbered_with_the_date_after_the_number() -> None:
    llm = _ByPassLLM()
    await _flush([_user("没有日期的一句"), _assistant("好"), *_qa()], llm)
    assert llm.input_of("user").splitlines() == [
        "=== new ===",
        "[1] 没有日期的一句",
        "[2] [2026-03-02] 帮我看看下周去成都的机票",
        "[3] [2026-03-02] 18号",
    ]


async def test_the_total_cap_numbers_the_kept_lines_from_one() -> None:
    """PR S —— 上限按整轮丢最早的;丢掉的整轮里最近的两条用户输入作上下文留在标记之上,编号从 1 起、
    上下文与新行连续。"""
    messages: list[BaseMessage] = [_user(f"M{i:02d}" + "字" * 897) for i in range(50)]
    llm = _ByPassLLM()
    await _flush(messages, llm)
    first = llm.input_of("user")
    assert len(first) <= memory_module._EXTRACT_INPUT_CHAR_CAP
    lines = first.splitlines()
    assert lines[0].startswith("[1] M") and lines[1].startswith("[2] M")
    assert lines[2] == "=== new ==="
    assert lines[3].startswith("[3] M")
    assert lines[-1].startswith(f"[{len(lines) - 1}] M49")
    assert "M00" not in first


# ─── 第二次:basis + turn 的核对 ─────────────────────────────────────────


async def test_an_answer_on_a_user_line_after_the_assistant_is_user_confirmed() -> None:
    llm = _ByPassLLM(
        conversation_reply=_reply(
            {
                "kind": "fact",
                "content": "用户的回程日期定在 3 月 18 日",
                "basis": "user_answer",
                "turn": 3,
                "confidence": 0.95,
            }
        )
    )
    _, store = await _flush(_qa(), llm)
    row = (await _rows(store))["用户的回程日期定在 3 月 18 日"]
    assert row.origin == "user_confirmed"
    assert row.confidence == 0.8
    assert (row.evidence_message_id, row.evidence_message_index) == ("u2", 2)


async def test_an_agreement_is_user_confirmed_too() -> None:
    llm = _ByPassLLM(
        conversation_reply=_reply(
            {
                "kind": "fact",
                "content": "回程 18 号",
                "basis": "user_agreement",
                "turn": 3,
                "confidence": 0.7,
            }
        )
    )
    _, store = await _flush(_qa(), llm)
    row = (await _rows(store))["回程 18 号"]
    assert (row.origin, row.confidence) == ("user_confirmed", 0.7)


@pytest.mark.parametrize(
    ("turn", "check", "pointer"),
    [
        (2, "downgraded_not_user_line", ("a1", 1)),  # 指向助手行
        (1, "downgraded_previous_not_assistant", ("u1", 0)),  # 第一句,上面没有助手行
        (99, "downgraded_out_of_range", (None, None)),
        (0, "downgraded_out_of_range", (None, None)),
        (None, "downgraded_out_of_range", (None, None)),
        ("3", "downgraded_out_of_range", (None, None)),  # 不是整数
    ],
)
async def test_an_answer_that_does_not_anchor_is_downgraded(
    checks: list[tuple[str, str]],
    turn: object,
    check: str,
    pointer: tuple[str | None, int | None],
) -> None:
    llm = _ByPassLLM(
        conversation_reply=_reply(
            {
                "kind": "fact",
                "content": "回程 18 号",
                "basis": "user_answer",
                "turn": turn,
                "confidence": 0.95,
            }
        )
    )
    _, store = await _flush(_qa(), llm)
    row = (await _rows(store))["回程 18 号"]
    assert (row.origin, row.confidence) == ("assistant_derived", 0.6)
    assert (row.evidence_message_id, row.evidence_message_index) == pointer
    assert ("conversation", check) in checks
    if pointer == (None, None):
        assert ("conversation", "pointer_missing") in checks


async def test_a_task_line_is_not_a_user_line(checks: list[tuple[str, str]]) -> None:
    messages: list[BaseMessage] = [
        _user("先问一句"),
        _assistant("请说"),
        HumanMessage(content="SEED 定时任务的种子"),
        _assistant("完成了"),
    ]
    llm = _ByPassLLM(
        conversation_reply=_reply(
            {"kind": "fact", "content": "种子", "basis": "user_answer", "turn": 3}
        )
    )
    _, store = await _flush(messages, llm)
    assert (await _rows(store))["种子"].origin == "assistant_derived"
    assert ("conversation", "downgraded_not_user_line") in checks


@pytest.mark.parametrize("basis", ["assistant", None, "user", "USER_STATED"])
async def test_anything_else_is_assistant_derived_with_the_pointer_kept(basis: object) -> None:
    llm = _ByPassLLM(
        conversation_reply=_reply(
            {
                "kind": "episodic",
                "content": "助手按 18 号比价",
                "basis": basis,
                "turn": 4,
                "confidence": 0.9,
            }
        )
    )
    _, store = await _flush(_qa(), llm)
    row = (await _rows(store))["助手按 18 号比价"]
    assert (row.origin, row.confidence) == ("assistant_derived", 0.6)
    assert (row.evidence_message_id, row.evidence_message_index) == ("a2", 3)


async def test_the_first_pass_is_always_user_stated_whatever_it_claims() -> None:
    llm = _ByPassLLM(
        user_reply=_reply(
            {
                "kind": "fact",
                "content": "用户下周去成都",
                "basis": "user_answer",
                "turn": 1,
                "confidence": 0.95,
            }
        )
    )
    _, store = await _flush(_qa(), llm)
    row = (await _rows(store))["用户下周去成都"]
    assert (row.origin, row.confidence) == ("user_stated", 0.95)
    # 第一次的行号是它自己那份输入里的:第 1 行 = 第一条用户消息。
    assert (row.evidence_message_id, row.evidence_message_index) == ("u1", 0)


async def test_a_first_pass_memory_without_a_line_is_written_without_a_pointer(
    checks: list[tuple[str, str]],
) -> None:
    llm = _ByPassLLM(user_reply=_reply({"kind": "fact", "content": "用户下周去成都", "turn": 7}))
    outcome, store = await _flush(_qa(), llm)
    row = (await _rows(store))["用户下周去成都"]
    assert (row.evidence_message_id, row.evidence_message_index) == (None, None)
    assert ("user", "pointer_missing") in checks
    assert outcome.written == 1


# ─── 事件日期 ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("claimed", "kept"),
    [
        ("2026-03-02", date(2026, 3, 2)),  # 那一行当天
        ("2026-03-01", date(2026, 3, 1)),  # 昨天
        ("2026-03-03", None),  # 晚于那一行的日期
        ("next tuesday", None),
        ("2026-3-2", None),
        (20260302, None),
    ],
)
async def test_event_date_must_parse_and_not_be_after_its_line(
    checks: list[tuple[str, str]], claimed: object, kept: date | None
) -> None:
    llm = _ByPassLLM(
        conversation_reply=_reply(
            {
                "kind": "fact",
                "content": "电表读数 18342",
                "basis": "user_answer",
                "turn": 3,
                "event_date": claimed,
            }
        ),
        user_reply=_reply(
            {"kind": "fact", "content": "用户提到下周", "turn": 2, "event_date": claimed}
        ),
    )
    _, store = await _flush(_qa(), llm)
    rows = await _rows(store)
    assert rows["电表读数 18342"].occurred_on == kept
    assert rows["用户提到下周"].occurred_on == kept
    dropped = [c for c in checks if c[1] == "event_date_dropped"]
    assert len(dropped) == (0 if kept else 2)


async def test_a_null_event_date_is_not_counted(checks: list[tuple[str, str]]) -> None:
    llm = _ByPassLLM(
        conversation_reply=_reply(
            {
                "kind": "fact",
                "content": "回程 18 号",
                "basis": "user_answer",
                "turn": 3,
                "event_date": None,
            }
        )
    )
    _, store = await _flush(_qa(), llm)
    assert (await _rows(store))["回程 18 号"].occurred_on is None
    assert [c for c in checks if c[1] == "event_date_dropped"] == []


async def test_a_line_without_a_date_checks_the_format_and_today(
    checks: list[tuple[str, str]],
) -> None:
    """那一行没有日期:只查格式,以及不晚于智能体时区的「今天」(审查 L3)。"""
    llm = _ByPassLLM(
        conversation_reply=_reply(
            {"kind": "fact", "content": "读数", "basis": "assistant", "turn": 2,
             "event_date": "2026-03-01"},
            {"kind": "fact", "content": "将来的读数", "basis": "assistant", "turn": 2,
             "event_date": "2030-01-01"},
        )
    )  # fmt: skip
    _, store = await _flush([_user("看一下"), _assistant("读数是 5")], llm)
    rows = await _rows(store)
    assert rows["读数"].occurred_on == date(2026, 3, 1)
    assert rows["将来的读数"].occurred_on is None
    assert checks.count(("conversation", "event_date_dropped")) == 1


async def test_an_unresolved_turn_still_checks_today(checks: list[tuple[str, str]]) -> None:
    llm = _ByPassLLM(
        user_reply=_reply(
            {"kind": "fact", "content": "过去的读数", "turn": 99, "event_date": "2026-03-01"},
            {"kind": "fact", "content": "将来的读数", "turn": 99, "event_date": "2030-01-01"},
        )
    )
    _, store = await _flush(_qa(), llm)
    rows = await _rows(store)
    assert rows["过去的读数"].occurred_on == date(2026, 3, 1)
    assert rows["将来的读数"].occurred_on is None
    assert checks.count(("user", "event_date_dropped")) == 1


@pytest.mark.parametrize("kind", ["fact", "episodic"])
async def test_a_dated_memory_keeps_its_date_whatever_its_kind(
    checks: list[tuple[str, str]], kind: str
) -> None:
    """「去掉自检,读数共享」(用户 2026-11-16):读数是「事实」(经历只在本智能体召回),事件日期
    不因类型被清空,也不计数。"""
    llm = _ByPassLLM(
        conversation_reply=_reply(
            {"kind": kind, "content": "电表读数 18342", "basis": "user_answer", "turn": 3,
             "event_date": "2026-03-02"},
        ),
        user_reply=_reply(
            {"kind": kind, "content": "鱼缸水温 26 度", "turn": 2, "event_date": "2026-03-02"},
        ),
    )  # fmt: skip
    _, store = await _flush(_qa(), llm)
    rows = await _rows(store)
    assert rows["电表读数 18342"].occurred_on == date(2026, 3, 2)
    assert rows["鱼缸水温 26 度"].occurred_on == date(2026, 3, 2)
    assert [c for c in checks if c[1].startswith("event_date")] == []


def test_the_today_guard_uses_the_given_day() -> None:
    undated = [ExtractionLine(text="读数是 5")]
    today = date(2026, 3, 2)
    kept = memory_lines.settle_user_text(1, "2026-03-02", undated, today=today)
    late = memory_lines.settle_user_text(1, "2026-03-03", undated, today=today)
    assert kept.event_date == date(2026, 3, 2)
    assert (late.event_date, late.checks) == (None, ("event_date_dropped",))


async def test_the_line_date_boundary_follows_the_agent_timezone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """17:30Z 在上海已是第二天:那一行的日子按智能体时区算(审查 L5)。"""
    monkeypatch.setenv("EXPERT_WORK_TIMEZONE", "Asia/Shanghai")
    llm = _ByPassLLM(
        user_reply=_reply(
            {"kind": "fact", "content": "当天", "turn": 1, "event_date": "2026-03-02"},
            {"kind": "fact", "content": "次日", "turn": 1, "event_date": "2026-03-03"},
        )
    )
    _, store = await _flush([_user("今天的读数", "2026-03-01T17:30:00+00:00")], llm)
    rows = await _rows(store)
    assert rows["当天"].occurred_on == date(2026, 3, 2)
    assert rows["次日"].occurred_on is None


# ─── 交给去重合并的三样:line_at、in_new_segment、事件日期 ─────────────


async def _extracted(
    messages: Sequence[BaseMessage], llm: _ByPassLLM, segment: SegmentStart | None = None
) -> list[ExtractedMemory]:
    out, failed = await memory_module._extract_two_passes(
        messages,
        user_text_messages=(),
        llm_caller=llm,
        token=CancellationToken(),
        log_label="test",
        segment=segment,
    )
    assert not failed
    return out


async def test_line_time_and_new_segment_follow_the_turn_line() -> None:
    """PR S —— 「新段」= 不是上下文行:从第二轮起切片,第一轮的行在标记之上(上下文);落在上下文行
    的产出丢弃(测试验收 11-16),新段里的带那一行的发送时间。"""
    llm = _ByPassLLM(
        user_reply=_reply(
            {"kind": "fact", "content": "早先说的", "turn": 1},
            {"kind": "fact", "content": "本轮说的", "turn": 2},
        ),
        conversation_reply=_reply(
            {"kind": "fact", "content": "上一轮的回答", "basis": "assistant", "turn": 2},
            {"kind": "fact", "content": "本轮的回答", "basis": "user_answer", "turn": 3},
        ),
    )
    by_content = {
        m.content: m for m in await _extracted(_qa(), llm, segment=SegmentStart(message_index=2))
    }
    assert set(by_content) == {"本轮说的", "本轮的回答"}
    assert by_content["本轮说的"].line_at == datetime(2026, 3, 2, 1, 1, tzinfo=UTC)
    assert by_content["本轮说的"].in_new_segment is True
    assert by_content["本轮的回答"].line_at == datetime(2026, 3, 2, 1, 1, tzinfo=UTC)
    assert by_content["本轮的回答"].in_new_segment is True


async def test_the_line_time_is_utc() -> None:
    """``line_at`` 进去重合并、与记录时间比先后:一律是带时区的 UTC(发送时刻带别的偏移也换算)。"""
    llm = _ByPassLLM(user_reply=_reply({"kind": "fact", "content": "我住在成都", "turn": 1}))
    [memory] = await _extracted(
        [_user("我住在成都", "2026-03-02T09:00:00+08:00"), _assistant("好")], llm
    )
    assert memory.line_at == datetime(2026, 3, 2, 1, 0, tzinfo=UTC)
    assert memory.line_at is not None and memory.line_at.tzinfo is UTC


async def test_an_unanchored_memory_knows_no_line() -> None:
    llm = _ByPassLLM(
        conversation_reply=_reply({"kind": "fact", "content": "x", "basis": "assistant"})
    )
    [memory] = await _extracted(_qa(), llm)
    assert (memory.line_at, memory.in_new_segment) == (None, False)


async def test_a_turn_without_send_times_has_no_line_time() -> None:
    llm = _ByPassLLM(
        user_reply=_reply({"kind": "fact", "content": "我住在成都", "turn": 1}),
    )
    [memory] = await _extracted([_user("我住在成都"), _assistant("好")], llm)
    assert (memory.line_at, memory.in_new_segment) == (None, True)


async def test_a_user_confirmed_candidate_is_reconciled_in_the_second_stage() -> None:
    """两段合并按来源分:第一次(``user_stated``)一段,其余(含 ``user_confirmed``)第二段 ——
    第二段能取到第一段刚写的那条。"""
    calls: list[list[BaseMessage]] = []

    async def llm(*, messages: Sequence[BaseMessage], tools: Sequence[Any]) -> AIMessage:
        del tools
        system = str(messages[0].content)
        if PASS1_MARK in system:
            return AIMessage(content=_reply({"kind": "fact", "content": "回程 18 号", "turn": 2}))
        if PASS2_MARK in system:
            return AIMessage(
                content=_reply(
                    {
                        "kind": "fact",
                        "content": "用户回程定在 18 号",
                        "basis": "user_answer",
                        "turn": 3,
                    }
                )
            )
        calls.append(list(messages))
        return AIMessage(content=json.dumps({"ops": [{"index": 0, "op": "ADD"}]}))

    store = InMemoryMemoryStore()
    await flush_messages_with_outcome(
        _qa(),
        memory_store=store,
        embedder=FakeEmbedder(dim=_DIM),
        llm_caller=llm,
        tenant_id=_TENANT,
        user_id=_USER,
        thread_id=uuid4(),
        token=CancellationToken(),
        reconcile=True,
        agent_name="agent-x",
    )
    [call] = calls
    [candidate] = json.loads(str(call[1].content))["candidates"]
    assert (candidate["content"], candidate["source"]) == (
        "用户回程定在 18 号",
        "confirmed by the user",
    )
    assert [e["content"] for e in candidate["existing"]] == ["回程 18 号"]
    assert candidate["date"] == "2026-03-02"


# ─── 证据指针 ───────────────────────────────────────────────────────────


async def test_activity_and_attachment_lines_point_at_their_messages() -> None:
    """``[activity]`` → 该轮第一条调工具的助手消息;``[attached data]`` → 那条用户消息。"""
    asked = HumanMessage(
        content="看看这个文件",
        id="u1",
        additional_kwargs=user_input_kwargs(
            text="看看这个文件", images=0, files=1, external_blocks=0
        ),
    )
    messages: list[BaseMessage] = [
        asked,
        AIMessage(content="", id="t1", tool_calls=[{"name": "read", "args": {}, "id": "c1"}]),
        ToolMessage(content="TOOLSENTINEL", tool_call_id="c1", name="read"),
        AIMessage(content="", id="t2", tool_calls=[{"name": "read", "args": {}, "id": "c2"}]),
        ToolMessage(content="TOOLSENTINEL", tool_call_id="c2", name="read"),
        _assistant("读完了", msg_id="a1"),
    ]
    llm = _ByPassLLM(
        conversation_reply=_reply(
            {"kind": "episodic", "content": "助手读了文件", "basis": "assistant", "turn": 3},
            {"kind": "episodic", "content": "用户发了一个文件", "basis": "assistant", "turn": 2},
        )
    )
    _, store = await _flush(messages, llm)
    assert llm.input_of("conversation").splitlines()[1:4] == [
        "[1] [user] 看看这个文件",
        "[2] [attached data] images: 0, files: 1, external blocks: 0",
        "[3] [activity] tools called this turn: read ×2",  # noqa: RUF001
    ]
    rows = await _rows(store)
    read = rows["助手读了文件"]
    sent = rows["用户发了一个文件"]
    assert (read.evidence_message_id, read.evidence_message_index) == ("t1", 1)
    assert (sent.evidence_message_id, sent.evidence_message_index) == ("u1", 0)


def test_the_pointer_refuses_a_negative_index() -> None:
    line = ExtractionLine(text="x", message_id="m1", message_index=-1)
    assert evidence_pointer(line) == ("m1", None)
    assert evidence_pointer(ExtractionLine(text="x", message_id="m1", message_index=0)) == (
        "m1",
        0,
    )


async def test_after_the_cap_a_turn_maps_to_the_kept_line() -> None:
    """总量上限丢掉最早的整轮、重新从 1 编号之后(上面两行是上下文),``turn`` = 3 指向留下的第一条
    新行那条消息,序号是整段对话里的(审查 L5)。"""
    messages: list[BaseMessage] = [
        _user(f"M{i:02d}" + "字" * 897, msg_id=f"m{i:02d}") for i in range(50)
    ]
    llm = _ByPassLLM(user_reply=_reply({"kind": "fact", "content": "第三行", "turn": 3}))
    _, store = await _flush(messages, llm)
    third = llm.input_of("user").splitlines()[3]
    assert third.startswith("[3] M")
    label = third[len("[3] M") : len("[3] M") + 2]
    assert label != "02"  # 确实丢了前面的轮次
    row = (await _rows(store))["第三行"]
    assert (row.evidence_message_id, row.evidence_message_index) == (f"m{label}", int(label))


def test_the_cap_counts_the_numbers_the_mark_and_the_context() -> None:
    """三轮正文合起来 30 字,远不到上限 55;带上编号、标记、丢掉的轮次作上下文的那几行,三轮
    (56 字)、两轮(56 字)都超了,只留最近的一轮(最近的一轮总是留下)。"""
    turns = [
        Turn(
            user=(),
            conversation=(ExtractionLine(text=f"line-{i}-xxx", message_index=i),),
            has_reply=True,
            first_index=i,
            last_index=i,
            opened_at=None,
        )
        for i in range(3)
    ]  # 每行 10 字
    plan = plan_slice(turns, None, cap=55, halves=False)
    [chunk] = plan.chunks
    assert plan.dropped_over_cap == 2
    assert [(line.text, line.context) for line in chunk.conversation] == [
        ("line-0-xxx", True),
        ("line-1-xxx", True),
        ("line-2-xxx", False),
    ]
    assert len(memory_lines.render_lines(chunk.conversation)) == 56
    assert plan_slice(turns, None, cap=56, halves=False).dropped_over_cap == 0


def test_rendering_puts_context_above_the_mark() -> None:
    """PR S 的上下文行:编号与新行连续,放在 ``=== new ===`` 之上。"""
    lines = [
        ExtractionLine(text="a", tag="[assistant]", context=True),
        ExtractionLine(text="b", tag="[user]"),
    ]
    assert memory_lines.render_lines(lines) == "[1] [assistant] a\n=== new ===\n[2] [user] b"


async def test_a_context_line_is_dropped_but_may_be_the_question(
    checks: list[tuple[str, str]],
) -> None:
    lines = [
        ExtractionLine(text="回程哪天?", tag="[assistant]", context=True, message_index=0),
        ExtractionLine(text="18号", tag="[user]", context=True, message_index=1),
        ExtractionLine(text="好的", tag="[assistant]", message_index=2),
    ]
    # 测试验收 11-16:落在上下文行的不再降级照写,整条丢弃(由调用方计数)。
    assert memory_lines.settle_conversation("user_answer", 2, None, lines, today=_TODAY) is None
    # 上一行可以是上下文行(问题常在上一轮末尾)。
    asked = [lines[0], ExtractionLine(text="18号", tag="[user]", message_id="u", message_index=1)]
    settled = memory_lines.settle_conversation("user_answer", 2, None, asked, today=_TODAY)
    assert settled is not None
    assert (settled.origin, settled.evidence_message_id, settled.checks) == (
        "user_confirmed",
        "u",
        (),
    )


# ─── 去重合并材料里的日期:按智能体时区的日子(审查 L2)───────────────────


async def test_reconcile_material_days_follow_the_agent_timezone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """候选那一行与邻居的日期都按智能体时区显示,与事件日期同一口径;比先后仍在 UTC。"""
    monkeypatch.setenv("EXPERT_WORK_TIMEZONE", "Asia/Shanghai")
    [vec] = await FakeEmbedder(dim=_DIM).embed(["电表读数"], tenant_id=_TENANT)

    def item(content: str, **extra: Any) -> MemoryItem:
        return MemoryItem.model_validate(
            {"id": uuid4(), "tenant_id": _TENANT, "user_id": _USER, "kind": "fact",
             "content": content, "embedding": vec, "origin": "user_stated", **extra}
        )  # fmt: skip

    store = InMemoryMemoryStore()
    await store.write(
        [item("电表读数 18342", created_at=datetime(2026, 11, 14, 18, 0, tzinfo=UTC))]
    )
    candidate = memory_module._Candidate(
        item("电表读数 18350", occurred_on=date(2026, 11, 15)),
        line_at=datetime(2026, 11, 14, 17, 30, tzinfo=UTC),
        in_new_segment=True,
    )
    calls: list[list[BaseMessage]] = []

    async def llm(*, messages: Sequence[BaseMessage], tools: Sequence[Any]) -> AIMessage:
        del tools
        calls.append(list(messages))
        return AIMessage(content=json.dumps({"ops": [{"index": 0, "op": "ADD"}]}))

    await memory_module._reconcile_and_apply(
        [candidate],
        agent_name="agent-x",
        positions={candidate.item.id: 0},
        memory_store=store,
        llm_caller=llm,
        token=CancellationToken(),
        log_label="test",
    )
    [material] = json.loads(str(calls[0][1].content))["candidates"]
    assert (material["date"], material["event_date"]) == ("2026-11-15", "2026-11-15")
    assert material["existing"][0]["date"] == "2026-11-15"
