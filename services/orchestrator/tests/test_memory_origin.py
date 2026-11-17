"""B-300 ② —— 来源字段在写入一侧(设计 §6.1、§6.2、§8.4、§13.3)。

* 来源只由「是哪一次提炼出来的」决定:第一次 → ``user_stated``,第二次 → ``assistant_derived``,
  无论模型回什么;压缩前抢存同样两次、不走去重合并;
* 每条写下的记忆带这次实际用的向量模型名(嵌入器报得出来时);
* 来源管的一条(重做第 9 版 §6.3):来自助手的候选**退不掉**有效来源是用户一方的记忆 —— 旧行
  不动,候选自己照写(标「来自助手」),带不带 ``is_correction`` 都一样;控制台更正过的谁也退不掉;
  写回不直接退汇总行;其余按先后退;
* 去重合并的材料带双方的来源,指令里有「同样的内容、已有的来源更弱 → 写下并列进 restates」那一句。
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.runnables import RunnableConfig
from prometheus_client import REGISTRY

from expert_work.common.message_stamp import STAMP_CREATED_AT
from expert_work.common.user_input import user_input_kwargs
from expert_work.persistence import InMemoryMemoryStore
from expert_work.protocol import MemoryItem
from expert_work.protocol.memory_item import MemoryOrigin
from expert_work.runtime.cancellation import CancellationToken
from orchestrator.graph_builder import memory as memory_module
from orchestrator.graph_builder.memory import (
    flush_messages_with_outcome,
    make_pre_compaction_flush,
)

_TENANT = UUID("00000000-0000-4000-8000-0000000000a2")
_USER = UUID("00000000-0000-4000-8000-0000000000b2")
_EAST = (1.0, 0.0, 0.0, 0.0)
_NEAR_EAST = (0.9, 0.43589, 0.0, 0.0)  # cosine vs _EAST = 0.9 ≥ 0.80
_NORTH = (0.0, 1.0, 0.0, 0.0)
#: 这一轮两条消息的发送时间:晚于用例里种下的记忆(它们的记录时间是「此刻」)。不知道那一行的
#: 时间的候选退不掉任何旧记忆(重做 §7.1 第 11 行)。
_SENT = datetime.now(UTC) + timedelta(hours=1)


@pytest.fixture(autouse=True)
def _ids_as_short_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    """重做 §7.1 第 3 行起,材料里的已有记忆用本次调用内的短编号(``e0, e1, …``)。本文件的用例
    按记忆 id 写模型的决定,这里让「短编号」就是 id 本身;短编号的生成与映射由
    ``test_memory_reconcile_redesign`` 钉住。"""
    monkeypatch.setattr(
        memory_module,
        "_short_ids",
        lambda pending: {n.id: str(n.id) for entry in pending for n in entry.neighbors},
    )


_MODEL = "vendor-x/embed-1"

PASS1_MARK = "The other side of the conversation is not shown."
PASS2_MARK = "Each line starts with its number in brackets and a tag:"
_EMPTY = '{"memories": []}'


def _reply(*memories: dict[str, Any]) -> str:
    return json.dumps({"memories": list(memories)}, ensure_ascii=False)


def _ops(op: str, target: UUID | None = None) -> str:
    row: dict[str, Any] = {"index": 0, "op": op}
    if target is not None:
        row["target_id"] = str(target)
    return json.dumps({"ops": [row]})


def _replaces(*targets: UUID) -> str:
    """写下候选(``ADD``),并说这几条已有记忆不再成立。"""
    return json.dumps({"ops": [{"index": 0, "op": "ADD", "replaces": [str(t) for t in targets]}]})


@dataclass
class _LLM:
    """两次提炼按系统提示词认(可并发);其余的调用是去重合并。"""

    user_reply: str = _EMPTY
    conversation_reply: str = _EMPTY
    ops_reply: str = '{"ops": []}'
    calls: list[list[BaseMessage]] = field(default_factory=list)
    #: 在去重合并的模型「判的那几秒」里做的事(模拟用户这时点了更正)。
    while_deciding: Callable[[], Awaitable[object]] | None = None

    def reconcile_calls(self) -> list[list[BaseMessage]]:
        return [
            c
            for c in self.calls
            if PASS1_MARK not in str(c[0].content) and PASS2_MARK not in str(c[0].content)
        ]

    async def __call__(self, *, messages: Sequence[BaseMessage], tools: Sequence[Any]) -> AIMessage:
        del tools
        self.calls.append(list(messages))
        system = str(messages[0].content)
        if PASS1_MARK in system:
            return AIMessage(content=self.user_reply)
        if PASS2_MARK in system:
            return AIMessage(content=self.conversation_reply)
        if self.while_deciding is not None:
            _ = await self.while_deciding()
        return AIMessage(content=self.ops_reply)


@dataclass
class _ReportingEmbedder:
    """按内容给固定向量;能报出模型名(同 ``DynamicResolvingEmbedder``)。"""

    mapping: dict[str, tuple[float, ...]] = field(default_factory=dict)

    def _vec(self, text: str) -> tuple[float, ...]:
        return self.mapping.get(text, _NORTH)

    async def embed(self, texts: Sequence[str], *, tenant_id: UUID) -> list[tuple[float, ...]]:
        del tenant_id
        return [self._vec(t) for t in texts]

    async def embed_reporting_model(
        self, texts: Sequence[str], *, tenant_id: UUID
    ) -> tuple[list[tuple[float, ...]], str]:
        return await self.embed(texts, tenant_id=tenant_id), _MODEL


@dataclass
class _PlainEmbedder:
    async def embed(self, texts: Sequence[str], *, tenant_id: UUID) -> list[tuple[float, ...]]:
        del tenant_id
        return [_NORTH for _ in texts]


def _user(text: str) -> HumanMessage:
    return HumanMessage(
        content=text,
        additional_kwargs=user_input_kwargs(
            text=text, images=0, files=0, external_blocks=0, sent_at=_SENT.isoformat()
        ),
    )


def _turn(text: str = "my words", answer: str = "the answer") -> list[BaseMessage]:
    reply = AIMessage(content=answer, additional_kwargs={STAMP_CREATED_AT: _SENT.isoformat()})
    return [_user(text), reply]


async def _flush(
    llm: _LLM,
    store: InMemoryMemoryStore,
    embedder: Any,
    *,
    reconcile: bool = False,
    messages: Sequence[BaseMessage] | None = None,
) -> tuple[int, bool]:
    outcome = await flush_messages_with_outcome(
        messages if messages is not None else _turn(),
        memory_store=store,
        embedder=embedder,
        llm_caller=llm,
        tenant_id=_TENANT,
        user_id=_USER,
        thread_id=uuid4(),
        token=CancellationToken(),
        reconcile=reconcile,
    )
    return outcome.written, outcome.failed


async def _live(store: InMemoryMemoryStore) -> dict[str, MemoryItem]:
    rows = await store.retrieve(tenant_id=_TENANT, user_id=_USER, query_embedding=_EAST, limit=50)
    return {r.content: r for r in rows}


async def _seed(
    store: InMemoryMemoryStore,
    content: str,
    *,
    origin: MemoryOrigin | None,
    confidence: float = 0.8,
) -> MemoryItem:
    item = MemoryItem(
        id=uuid4(),
        tenant_id=_TENANT,
        user_id=_USER,
        kind="fact",
        content=content,
        embedding=_EAST,
        origin=origin,
        confidence=confidence,
    )
    await store.write([item])
    return item


# ---------------------------------------------------------------------------
# 来源由提炼次序决定;向量模型名
# ---------------------------------------------------------------------------


async def test_origin_follows_the_pass_whatever_the_model_says() -> None:
    llm = _LLM(
        # 模型在回复里自己写了来源字段:程序不看,只按是哪一次提炼。
        user_reply=_reply({"kind": "fact", "content": "u1", "origin": "assistant_derived"}),
        conversation_reply=_reply({"kind": "fact", "content": "a1", "origin": "user_stated"}),
    )
    store = InMemoryMemoryStore()
    assert await _flush(llm, store, _ReportingEmbedder()) == (2, False)
    rows = await _live(store)
    assert rows["u1"].origin == "user_stated"
    assert rows["a1"].origin == "assistant_derived"


async def test_same_content_from_both_passes_is_kept_as_user_stated() -> None:
    llm = _LLM(
        user_reply=_reply({"kind": "fact", "content": "same"}),
        conversation_reply=_reply({"kind": "fact", "content": "same"}),
    )
    store = InMemoryMemoryStore()
    assert await _flush(llm, store, _ReportingEmbedder()) == (1, False)
    assert (await _live(store))["same"].origin == "user_stated"


async def test_written_rows_carry_the_reported_embedding_model() -> None:
    llm = _LLM(user_reply=_reply({"kind": "fact", "content": "u1"}))
    reporting, plain = InMemoryMemoryStore(), InMemoryMemoryStore()
    await _flush(llm, reporting, _ReportingEmbedder())
    await _flush(_LLM(user_reply=llm.user_reply), plain, _PlainEmbedder())
    assert (await _live(reporting))["u1"].embedding_model == _MODEL
    assert (await _live(plain))["u1"].embedding_model is None


async def test_pre_compaction_flush_no_longer_extracts() -> None:
    """B-300 重做 PR S(§8.4)—— 抢存不再提炼(它直写、不走去重合并,切片后只会添重复):不写、
    已有的记忆不动。"""
    store = InMemoryMemoryStore()
    await _seed(store, "kept", origin="user_stated")
    flush = make_pre_compaction_flush()
    config: RunnableConfig = {
        "configurable": {"tenant_id": str(_TENANT), "user_id": str(_USER), "thread_id": None}
    }
    assert await flush(_turn(), config, CancellationToken()) == 0
    assert list(await _live(store)) == ["kept"]


# ---------------------------------------------------------------------------
# 来源管的那一条:助手一方的候选退不掉用户一方的记忆,它自己照写
# ---------------------------------------------------------------------------


def _assistant_candidate(content: str = "cand", *, is_correction: bool = False) -> str:
    """第二次提炼的一条候选,出自助手那一行(第 2 行)。"""
    return _reply({"kind": "fact", "content": content, "is_correction": is_correction, "turn": 2})


def _user_candidate(content: str) -> str:
    """第一次提炼的一条候选,出自用户那一行(第 1 行)。"""
    return _reply({"kind": "fact", "content": content, "confidence": 0.8, "turn": 1})


def _reconcile_count(op: str) -> float:
    value = REGISTRY.get_sample_value("expert_work_cm_memory_reconcile_total", {"op": op})
    return value or 0.0


@pytest.mark.parametrize("is_correction", [False, True], ids=["plain", "correction"])
@pytest.mark.parametrize(
    ("origin", "confidence", "label"),
    [("user_stated", 0.9, "retire_refused_assistant"), (None, 1.0, "retire_refused_corrected")],
    ids=["user_stated", "user_corrected"],
)
async def test_an_assistant_candidate_never_retires_a_user_memory_and_is_still_written(
    origin: MemoryOrigin | None, confidence: float, label: str, is_correction: bool
) -> None:
    store = InMemoryMemoryStore()
    target = await _seed(store, "user said", origin=origin, confidence=confidence)
    llm = _LLM(
        conversation_reply=_assistant_candidate(is_correction=is_correction),
        ops_reply=_replaces(target.id),
    )
    before = _reconcile_count(label)
    written, _ = await _flush(llm, store, _ReportingEmbedder({"cand": _NEAR_EAST}), reconcile=True)
    assert written == 1
    rows = await _live(store)
    assert set(rows) == {"user said", "cand"}
    assert (rows["cand"].origin, rows["cand"].supersedes) == ("assistant_derived", None)
    assert all(
        (r.invalid_at, r.expired_at) == (None, None) for r in store._rows if r.id == target.id
    )
    assert _reconcile_count(label) == before + 1


@pytest.mark.parametrize("op", ["UPDATE", "DELETE"])
async def test_a_legacy_op_by_the_assistant_never_removes_a_user_memory(op: str) -> None:
    """模型仍回旧的操作:照 ``ADD`` + 清单读,用户那条同样不动,候选照写。"""
    store = InMemoryMemoryStore()
    target = await _seed(store, "user said", origin="user_stated", confidence=0.9)
    llm = _LLM(conversation_reply=_assistant_candidate(), ops_reply=_ops(op, target.id))
    before = _reconcile_count("legacy_op")
    written, _ = await _flush(llm, store, _ReportingEmbedder({"cand": _NEAR_EAST}), reconcile=True)
    assert written == 1
    assert set(await _live(store)) == {"user said", "cand"}
    assert all(r.expired_at is None for r in store._rows if r.id == target.id)
    assert _reconcile_count("legacy_op") == before + 1


@pytest.mark.parametrize("origin", ["assistant_derived", None])
async def test_an_assistant_candidate_retires_assistant_side_memories(
    origin: MemoryOrigin | None,
) -> None:
    store = InMemoryMemoryStore()
    target = await _seed(store, "old", origin=origin)
    llm = _LLM(conversation_reply=_assistant_candidate(), ops_reply=_replaces(target.id))
    await _flush(llm, store, _ReportingEmbedder({"cand": _NEAR_EAST}), reconcile=True)
    rows = await _live(store)
    assert set(rows) == {"cand"}
    # 新行带候选的来源和向量模型名,旧行连到它。
    assert (rows["cand"].origin, rows["cand"].embedding_model) == ("assistant_derived", _MODEL)
    assert rows["cand"].supersedes == target.id


async def test_a_denial_is_itself_stored_and_the_old_memory_links_to_it() -> None:
    """以前的「删除」:旧行标过期、这句话本身不存。现在把这句话写下,旧行作废并连到它(§7.3)。"""
    store = InMemoryMemoryStore()
    target = await _seed(store, "old", origin="assistant_derived")
    llm = _LLM(conversation_reply=_assistant_candidate(), ops_reply=_ops("DELETE", target.id))
    await _flush(llm, store, _ReportingEmbedder({"cand": _NEAR_EAST}), reconcile=True)
    rows = await _live(store)
    assert set(rows) == {"cand"}
    [old] = [r for r in store._rows if r.id == target.id]
    assert (old.expired_at, old.superseded_by) == (None, rows["cand"].id)


@pytest.mark.parametrize("is_correction", [True, False], ids=["correction", "plain"])
async def test_user_stated_candidate_retires_a_user_memory(is_correction: bool) -> None:
    store = InMemoryMemoryStore()
    target = await _seed(store, "old words", origin="user_stated", confidence=0.95)
    user_reply = _reply(
        {
            "kind": "fact",
            "content": "new words",
            "confidence": 0.8,
            "turn": 1,
            "is_correction": is_correction,
        }
    )
    llm = _LLM(user_reply=user_reply, ops_reply=_replaces(target.id))
    await _flush(llm, store, _ReportingEmbedder({"new words": _NEAR_EAST}), reconcile=True)
    rows = await _live(store)
    # 置信度(0.8 对 0.95)不参与:同是用户说的,按先后。第 10 版(用户 2026-11-17,测试 118.5
    # T39):第一次的候选拿 replaces 退用户一方要带更正标记,不带的两条并存、新行连着旧行。
    if is_correction:
        assert set(rows) == {"new words"}
    else:
        assert set(rows) == {"old words", "new words"}
        assert rows["new words"].supersedes == target.id
    assert rows["new words"].origin == "user_stated"


async def test_a_candidate_whose_line_time_is_unknown_retires_nothing() -> None:
    store = InMemoryMemoryStore()
    target = await _seed(store, "old words", origin="user_stated")
    llm = _LLM(
        user_reply=_reply({"kind": "fact", "content": "new words"}),  # 没给 turn
        ops_reply=_replaces(target.id),
    )
    before = _reconcile_count("retire_refused_unknown_time")
    await _flush(llm, store, _ReportingEmbedder({"new words": _NEAR_EAST}), reconcile=True)
    assert set(await _live(store)) == {"old words", "new words"}
    assert _reconcile_count("retire_refused_unknown_time") == before + 1


# ---------------------------------------------------------------------------
# 去重合并的材料
# ---------------------------------------------------------------------------


async def test_reconcile_material_carries_both_sides_sources_and_the_rule() -> None:
    store = InMemoryMemoryStore()
    await _seed(store, "derived before", origin="assistant_derived")
    await _seed(store, "corrected before", origin=None, confidence=1.0)
    llm = _LLM(user_reply=_reply({"kind": "fact", "content": "said now"}), ops_reply=_ops("NOOP"))
    await _flush(llm, store, _ReportingEmbedder({"said now": _NEAR_EAST}), reconcile=True)
    [call] = llm.reconcile_calls()
    # 重做附录 B:同样内容、已有的来源更弱 → 写下,并把那条列进 restates(用户后来自己说全了)。
    assert (
        "When the candidate says the same thing as an existing memory with a weaker source, it is\n"
        '  an ADD that lists that memory in "restates".' in str(call[0].content)
    )
    payload = json.loads(str(call[1].content))
    [candidate] = payload["candidates"]
    assert candidate["source"] == "stated in user messages"
    existing = {e["content"]: e["source"] for e in candidate["existing"]}
    assert existing == {
        "derived before": "from the assistant",
        "corrected before": "corrected by the user",
    }


# ---------------------------------------------------------------------------
# 审查 MEDIUM-1:读邻居之后、改库之前,目标被用户更正
# ---------------------------------------------------------------------------


def _correct_while_deciding(
    store: InMemoryMemoryStore, target: MemoryItem
) -> Callable[[], Awaitable[object]]:
    async def correct() -> object:
        return await store.update_content(
            tenant_id=_TENANT,
            user_id=_USER,
            memory_id=target.id,
            content="corrected by the user",
            embedding=_EAST,
            confidence=1.0,
        )

    return correct


@pytest.mark.parametrize("is_correction", [False, True], ids=["plain", "correction"])
@pytest.mark.parametrize("op", ["ADD", "UPDATE", "DELETE"])
async def test_target_corrected_while_the_model_decides_is_not_retired(
    op: str, is_correction: bool
) -> None:
    """预判时目标还是「来自助手」,判的那几秒里用户点了更正:存储在行锁里重判 → 不退,候选照写。"""
    store = InMemoryMemoryStore()
    target = await _seed(store, "derived", origin="assistant_derived", confidence=0.5)
    llm = _LLM(
        conversation_reply=_assistant_candidate(is_correction=is_correction),
        ops_reply=_replaces(target.id) if op == "ADD" else _ops(op, target.id),
        while_deciding=_correct_while_deciding(store, target),
    )
    before = _reconcile_count("retire_refused_corrected")
    await _flush(llm, store, _ReportingEmbedder({"cand": _NEAR_EAST}), reconcile=True)
    rows = await _live(store)
    assert set(rows) == {"corrected by the user", "cand"}
    assert rows["corrected by the user"].supersedes is None
    assert rows["cand"].supersedes is None
    assert _reconcile_count("retire_refused_corrected") == before + 1


# ---------------------------------------------------------------------------
# 审查 MEDIUM-3 / 重做第 9 版审查 H-3:写回不直接退汇总行
# ---------------------------------------------------------------------------


async def _seed_summary(
    store: InMemoryMemoryStore, member_origins: Sequence[MemoryOrigin | None]
) -> MemoryItem:
    members = [
        MemoryItem(
            id=uuid4(),
            tenant_id=_TENANT,
            user_id=_USER,
            kind="fact",
            content=f"member {i}",
            embedding=_NORTH,
            origin=origin,
        )
        for i, origin in enumerate(member_origins)
    ]
    await store.write(members)
    stored = {r.id: r for r in store._rows}
    return await store.write_consolidated(
        tenant_id=_TENANT,
        user_id=_USER,
        content="summary",
        embedding=_EAST,
        source_ids=[m.id for m in members],
        source_hashes={m.id: str(stored[m.id].content_hash) for m in members},
    )


@pytest.mark.parametrize(
    "members",
    [["user_stated", None, None], [None, None, None]],
    ids=["user-side-member", "unrecorded-members"],
)
async def test_an_assistant_candidate_neither_retires_nor_flags_a_summary(
    members: list[MemoryOrigin | None],
) -> None:
    """汇总行是几条记忆合成的一句,候选通常只否认其中一件:写回一律不退(不分成员来源,第 9 版
    §14 第 30 行);重写标记只有用户一方的候选打得上。候选照写。"""
    store = InMemoryMemoryStore()
    summary = await _seed_summary(store, members)
    assert summary.origin is None
    llm = _LLM(conversation_reply=_assistant_candidate(), ops_reply=_replaces(summary.id))
    before = _reconcile_count("retire_refused_summary")
    written, _ = await _flush(llm, store, _ReportingEmbedder({"cand": _NEAR_EAST}), reconcile=True)
    assert written == 1
    rows = await _live(store)
    assert set(rows) == {"summary", "cand"}
    assert rows["summary"].review_flagged_at is None
    assert _reconcile_count("retire_refused_summary") == before + 1


# ---------------------------------------------------------------------------
# 审查 LOW-1:写入失败时进失败队列的,只是本来要写的那几条
# ---------------------------------------------------------------------------


class _FailingWriteStore(InMemoryMemoryStore):
    async def write(self, items: Sequence[MemoryItem]) -> Any:
        del items
        raise RuntimeError("database down")


@dataclass
class _RecordingDLQ:
    enqueued: list[list[tuple[str, str]]] = field(default_factory=list)

    async def enqueue(self, *, extracted: Sequence[tuple[str, str]], **_: Any) -> None:
        self.enqueued.append(list(extracted))


async def test_dlq_gets_only_what_was_not_written_yet() -> None:
    store = _FailingWriteStore()
    user_target = await _seed_in(store, "user said", origin="user_stated")
    derived_target = await _seed_in(store, "derived", origin="assistant_derived")
    llm = _LLM(
        conversation_reply=_reply(
            {"kind": "fact", "content": "keep", "turn": 2},
            {"kind": "fact", "content": "beside", "turn": 2},
            {"kind": "fact", "content": "landed", "turn": 2},
        ),
        # 重做 §7.1 第 2 行:没有相似度门槛,三条都交给模型(「keep」的邻居再远也算)。
        ops_reply=json.dumps(
            {
                "ops": [
                    {"index": 0, "op": "ADD"},
                    {"index": 1, "op": "ADD", "replaces": [str(user_target.id)]},
                    {"index": 2, "op": "ADD", "replaces": [str(derived_target.id)]},
                ]
            }
        ),
    )
    dlq = _RecordingDLQ()
    outcome = await flush_messages_with_outcome(
        _turn(),
        memory_store=store,
        embedder=_ReportingEmbedder({"beside": _NEAR_EAST, "landed": _NEAR_EAST}),
        llm_caller=llm,
        tenant_id=_TENANT,
        user_id=_USER,
        thread_id=uuid4(),
        token=CancellationToken(),
        reconcile=True,
        dlq=dlq,  # type: ignore[arg-type]
    )
    assert outcome.failed
    # 第三条连同它退掉的旧行已经在去重合并里落库(``supersede``),不该在重试时再被重建一遍;
    # 前两条(什么都不退的、退不掉用户那条的)还在等批量写入,写入失败 → 进失败队列。
    assert dlq.enqueued == [[("fact", "keep"), ("fact", "beside")]]
    assert [r.content for r in store._rows if r.supersedes == derived_target.id] == ["landed"]


async def _seed_in(store: InMemoryMemoryStore, content: str, *, origin: MemoryOrigin) -> MemoryItem:
    """绕过 ``write``(这个存储的写入会失败)直接放一行。"""
    item = MemoryItem(
        id=uuid4(),
        tenant_id=_TENANT,
        user_id=_USER,
        kind="fact",
        content=content,
        embedding=_EAST,
        origin=origin,
    )
    store._rows.append(item)
    return item


# ---------------------------------------------------------------------------
# D12 —— 「写了几条」含去重合并里自己落库的新行(真栈验收 §117.9:库里多了 3 行、计数报 1)
# ---------------------------------------------------------------------------


async def test_written_counts_reconciled_adds() -> None:
    store = InMemoryMemoryStore()
    _ = await _seed(store, "old", origin=None)
    llm = _LLM(user_reply=_user_candidate("cand"), ops_reply=_ops("ADD"))
    written, _ = await _flush(llm, store, _ReportingEmbedder({"cand": _NEAR_EAST}), reconcile=True)
    assert written == 1
    assert set(await _live(store)) == {"old", "cand"}


async def test_written_counts_the_row_that_retired_another() -> None:
    store = InMemoryMemoryStore()
    target = await _seed(store, "old", origin=None)
    llm = _LLM(user_reply=_user_candidate("cand"), ops_reply=_replaces(target.id))
    written, _ = await _flush(llm, store, _ReportingEmbedder({"cand": _NEAR_EAST}), reconcile=True)
    assert written == 1
    rows = await _live(store)
    assert set(rows) == {"cand"}
    assert rows["cand"].supersedes == target.id


async def test_a_target_that_is_gone_does_not_stop_the_write() -> None:
    """要退的那条在判的那几秒里被用户删了:跳过它,候选照写,只算那一行(以前是「取代没写成 →
    退回直接写入」)。"""
    store = InMemoryMemoryStore()
    target = await _seed(store, "old", origin=None)

    async def forget() -> object:
        return await store.soft_delete(tenant_id=_TENANT, user_id=_USER, memory_id=target.id)

    llm = _LLM(
        user_reply=_user_candidate("cand"), ops_reply=_replaces(target.id), while_deciding=forget
    )
    before = _reconcile_count("retire_target_gone")
    written, _ = await _flush(llm, store, _ReportingEmbedder({"cand": _NEAR_EAST}), reconcile=True)
    assert written == 1
    assert len(store._rows) == 2
    rows = await _live(store)
    assert set(rows) == {"cand"}
    assert rows["cand"].supersedes is None
    assert _reconcile_count("retire_target_gone") == before + 1


async def test_written_counts_every_row_that_landed() -> None:
    store = InMemoryMemoryStore()
    old1 = await _seed(store, "old1", origin=None)
    old2 = await _seed(store, "old2", origin=None)
    llm = _LLM(
        user_reply=_reply(
            {"kind": "fact", "content": "replaces old1", "turn": 1},
            {"kind": "fact", "content": "denies old2", "turn": 1},
            {"kind": "fact", "content": "unrelated", "turn": 1},
        ),
        ops_reply=json.dumps(
            {
                "ops": [
                    {"index": 0, "op": "ADD", "restates": [str(old1.id)]},
                    {"index": 1, "op": "ADD", "replaces": [str(old2.id)]},
                ]
            }
        ),
    )
    rows_before = len(store._rows)
    written, _ = await _flush(
        llm,
        store,
        _ReportingEmbedder({"replaces old1": _NEAR_EAST, "denies old2": _NEAR_EAST}),
        reconcile=True,
    )
    # 每条候选都写下(否认旧记忆的那句话本身也是一行);两条旧行各被退一次。
    assert set(await _live(store)) == {"replaces old1", "denies old2", "unrelated"}
    assert written == len(store._rows) - rows_before == 3
