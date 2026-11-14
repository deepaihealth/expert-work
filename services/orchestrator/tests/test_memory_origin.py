"""B-300 ② —— 来源字段在写入一侧(设计 §6.1、§6.2、§8.4、§13.3)。

* 来源只由「是哪一次提炼出来的」决定:第一次 → ``user_stated``,第二次 → ``assistant_derived``,
  无论模型回什么;压缩前抢存同样两次、不走去重合并;
* 每条写下的记忆带这次实际用的向量模型名(嵌入器报得出来时);
* 唯一的一条拦截规则:来自助手的候选不得「更新」或「删除」有效来源是用户消息里说的 /
  用户更正的记忆 —— 被拦的删除什么都不做;被拦的更新带 ``is_correction`` 按新增存下,
  不带就丢弃;其余照旧;
* 去重合并的材料带双方的来源,指令里有「用户说全了的候选对上来自助手的旧记忆是更新」那一句。
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.runnables import RunnableConfig
from prometheus_client import REGISTRY

from expert_work.common.user_input import user_input_kwargs
from expert_work.persistence import InMemoryMemoryStore
from expert_work.protocol import MemoryItem
from expert_work.protocol.memory_item import MemoryOrigin
from expert_work.runtime.cancellation import CancellationToken
from orchestrator.graph_builder.memory import (
    flush_messages_with_outcome,
    make_pre_compaction_flush,
)

_TENANT = UUID("00000000-0000-4000-8000-0000000000a2")
_USER = UUID("00000000-0000-4000-8000-0000000000b2")
_EAST = (1.0, 0.0, 0.0, 0.0)
_NEAR_EAST = (0.9, 0.43589, 0.0, 0.0)  # cosine vs _EAST = 0.9 ≥ 0.80
_NORTH = (0.0, 1.0, 0.0, 0.0)
_MODEL = "vendor-x/embed-1"

PASS1_MARK = "The other side of the conversation is not shown."
PASS2_MARK = "Every line starts with a tag:"
_EMPTY = '{"memories": []}'


def _reply(*memories: dict[str, Any]) -> str:
    return json.dumps({"memories": list(memories)}, ensure_ascii=False)


def _ops(op: str, target: UUID | None = None) -> str:
    row: dict[str, Any] = {"index": 0, "op": op}
    if target is not None:
        row["target_id"] = str(target)
    return json.dumps({"ops": [row]})


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
        additional_kwargs=user_input_kwargs(text=text, images=0, files=0, external_blocks=0),
    )


def _turn(text: str = "my words", answer: str = "the answer") -> list[BaseMessage]:
    return [_user(text), AIMessage(content=answer)]


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


async def test_pre_compaction_flush_tags_origin_and_skips_reconcile() -> None:
    store = InMemoryMemoryStore()
    seeded = await _seed(store, "kept", origin="user_stated")
    llm = _LLM(
        user_reply=_reply({"kind": "fact", "content": "u1"}),
        conversation_reply=_reply({"kind": "fact", "content": "a1"}),
        ops_reply=_ops("DELETE", seeded.id),
    )
    flush = make_pre_compaction_flush(
        memory_store=store,
        embedder=_ReportingEmbedder({"u1": _NEAR_EAST, "a1": _NEAR_EAST}),  # type: ignore[arg-type]
        llm_caller=llm,
    )
    config: RunnableConfig = {
        "configurable": {"tenant_id": str(_TENANT), "user_id": str(_USER), "thread_id": None}
    }
    assert await flush(_turn(), config, CancellationToken()) == 2
    rows = await _live(store)
    assert (rows["u1"].origin, rows["a1"].origin) == ("user_stated", "assistant_derived")
    assert "kept" in rows
    assert llm.reconcile_calls() == []


# ---------------------------------------------------------------------------
# 唯一的拦截规则
# ---------------------------------------------------------------------------


def _assistant_candidate(content: str = "cand", *, is_correction: bool = False) -> str:
    return _reply({"kind": "fact", "content": content, "is_correction": is_correction})


def _reconcile_count(op: str) -> float:
    value = REGISTRY.get_sample_value("expert_work_cm_memory_reconcile_total", {"op": op})
    return value or 0.0


@pytest.mark.parametrize(
    ("origin", "confidence"),
    [("user_stated", 0.9), (None, 1.0)],
    ids=["user_stated", "user_corrected"],
)
async def test_assistant_update_without_correction_is_dropped(
    origin: MemoryOrigin | None, confidence: float
) -> None:
    store = InMemoryMemoryStore()
    target = await _seed(store, "user said", origin=origin, confidence=confidence)
    llm = _LLM(conversation_reply=_assistant_candidate(), ops_reply=_ops("UPDATE", target.id))
    before = _reconcile_count("blocked_update_dropped")
    written, _ = await _flush(llm, store, _ReportingEmbedder({"cand": _NEAR_EAST}), reconcile=True)
    assert written == 0
    assert set(await _live(store)) == {"user said"}
    assert all(r.invalid_at is None for r in store._rows if r.id == target.id)
    assert _reconcile_count("blocked_update_dropped") == before + 1


@pytest.mark.parametrize(
    ("origin", "confidence"),
    [("user_stated", 0.9), (None, 1.0)],
    ids=["user_stated", "user_corrected"],
)
async def test_assistant_update_marked_as_correction_is_added_alongside(
    origin: MemoryOrigin | None, confidence: float
) -> None:
    store = InMemoryMemoryStore()
    target = await _seed(store, "user said", origin=origin, confidence=confidence)
    llm = _LLM(
        conversation_reply=_assistant_candidate(is_correction=True),
        ops_reply=_ops("UPDATE", target.id),
    )
    before = _reconcile_count("blocked_update_added")
    written, _ = await _flush(llm, store, _ReportingEmbedder({"cand": _NEAR_EAST}), reconcile=True)
    assert written == 1
    assert _reconcile_count("blocked_update_added") == before + 1
    rows = await _live(store)
    assert set(rows) == {"user said", "cand"}
    assert rows["cand"].origin == "assistant_derived"
    assert rows["cand"].supersedes is None


@pytest.mark.parametrize(
    ("origin", "confidence"),
    [("user_stated", 0.9), (None, 1.0)],
    ids=["user_stated", "user_corrected"],
)
async def test_assistant_delete_of_a_user_memory_does_nothing(
    origin: MemoryOrigin | None, confidence: float
) -> None:
    store = InMemoryMemoryStore()
    target = await _seed(store, "user said", origin=origin, confidence=confidence)
    llm = _LLM(
        conversation_reply=_assistant_candidate(is_correction=True),
        ops_reply=_ops("DELETE", target.id),
    )
    before = _reconcile_count("blocked_delete")
    written, _ = await _flush(llm, store, _ReportingEmbedder({"cand": _NEAR_EAST}), reconcile=True)
    assert written == 0
    assert set(await _live(store)) == {"user said"}
    assert all(r.expired_at is None for r in store._rows if r.id == target.id)
    assert _reconcile_count("blocked_delete") == before + 1


@pytest.mark.parametrize("origin", ["assistant_derived", None])
async def test_assistant_update_of_other_memories_applies(origin: MemoryOrigin | None) -> None:
    store = InMemoryMemoryStore()
    target = await _seed(store, "old", origin=origin)
    llm = _LLM(conversation_reply=_assistant_candidate(), ops_reply=_ops("UPDATE", target.id))
    await _flush(llm, store, _ReportingEmbedder({"cand": _NEAR_EAST}), reconcile=True)
    rows = await _live(store)
    assert set(rows) == {"cand"}
    # 取代出来的新行带候选的来源和向量模型名。
    assert (rows["cand"].origin, rows["cand"].embedding_model) == ("assistant_derived", _MODEL)
    assert rows["cand"].supersedes == target.id


async def test_assistant_delete_of_an_assistant_memory_applies() -> None:
    store = InMemoryMemoryStore()
    target = await _seed(store, "old", origin="assistant_derived")
    llm = _LLM(conversation_reply=_assistant_candidate(), ops_reply=_ops("DELETE", target.id))
    await _flush(llm, store, _ReportingEmbedder({"cand": _NEAR_EAST}), reconcile=True)
    assert await _live(store) == {}


async def test_user_stated_candidate_may_update_a_user_memory() -> None:
    store = InMemoryMemoryStore()
    target = await _seed(store, "old words", origin="user_stated")
    llm = _LLM(
        # 用户 11-14 —— 置信度不低于目标(0.8)才可以;更低的见 test_memory_lower_confidence_guard。
        user_reply=_reply({"kind": "fact", "content": "new words", "confidence": 0.8}),
        ops_reply=_ops("UPDATE", target.id),
    )
    await _flush(llm, store, _ReportingEmbedder({"new words": _NEAR_EAST}), reconcile=True)
    rows = await _live(store)
    assert set(rows) == {"new words"}
    assert rows["new words"].origin == "user_stated"


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
    assert (
        "A candidate stated in user messages that matches an existing memory from the "
        "assistant is an UPDATE, not a NOOP." in str(call[0].content)
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


@pytest.mark.parametrize(
    ("op", "is_correction", "expected_live", "label"),
    [
        ("UPDATE", False, {"corrected by the user"}, "blocked_update_dropped"),
        ("UPDATE", True, {"corrected by the user", "cand"}, "blocked_update_added"),
        ("DELETE", True, {"corrected by the user"}, "blocked_delete"),
    ],
    ids=["update-dropped", "update-added", "delete-noop"],
)
async def test_target_corrected_while_the_model_decides_is_still_protected(
    op: str, is_correction: bool, expected_live: set[str], label: str
) -> None:
    store = InMemoryMemoryStore()
    target = await _seed(store, "derived", origin="assistant_derived", confidence=0.5)
    llm = _LLM(
        conversation_reply=_assistant_candidate(is_correction=is_correction),
        ops_reply=_ops(op, target.id),
        while_deciding=_correct_while_deciding(store, target),
    )
    before = _reconcile_count(label)
    await _flush(llm, store, _ReportingEmbedder({"cand": _NEAR_EAST}), reconcile=True)
    rows = await _live(store)
    assert set(rows) == expected_live
    assert rows["corrected by the user"].supersedes is None
    assert _reconcile_count(label) == before + 1


# ---------------------------------------------------------------------------
# 审查 MEDIUM-3:成员里有用户一方的汇总行同样受保护
# ---------------------------------------------------------------------------


class _CountingStore(InMemoryMemoryStore):
    def __init__(self) -> None:
        super().__init__()
        self.writes_to_targets = 0

    async def supersede(self, **kwargs: Any) -> MemoryItem | None:
        self.writes_to_targets += 1
        return await super().supersede(**kwargs)

    async def expire(self, **kwargs: Any) -> bool:
        self.writes_to_targets += 1
        return await super().expire(**kwargs)


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


@pytest.mark.parametrize("op", ["UPDATE", "DELETE"])
async def test_summary_of_user_side_members_is_protected(op: str) -> None:
    store = _CountingStore()
    summary = await _seed_summary(store, ["user_stated", None, None])
    assert summary.origin is None
    llm = _LLM(conversation_reply=_assistant_candidate(), ops_reply=_ops(op, summary.id))
    written, _ = await _flush(llm, store, _ReportingEmbedder({"cand": _NEAR_EAST}), reconcile=True)
    assert written == 0
    assert set(await _live(store)) == {"summary"}
    # 预判就拦下了,没有走到改库那一步(锁里的那道是兜底)。
    assert store.writes_to_targets == 0


async def test_summary_of_unrecorded_members_is_not_protected() -> None:
    store = InMemoryMemoryStore()
    summary = await _seed_summary(store, [None, None, None])
    llm = _LLM(conversation_reply=_assistant_candidate(), ops_reply=_ops("UPDATE", summary.id))
    await _flush(llm, store, _ReportingEmbedder({"cand": _NEAR_EAST}), reconcile=True)
    assert set(await _live(store)) == {"cand"}


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


async def test_dlq_gets_only_what_reconcile_meant_to_write() -> None:
    store = _FailingWriteStore()
    user_target = await _seed_in(store, "user said", origin="user_stated")
    derived_target = await _seed_in(store, "derived", origin="assistant_derived")
    llm = _LLM(
        conversation_reply=_reply(
            {"kind": "fact", "content": "keep"},
            {"kind": "fact", "content": "drop"},
            {"kind": "fact", "content": "retract"},
        ),
        ops_reply=json.dumps(
            {
                "ops": [
                    {"index": 0, "op": "UPDATE", "target_id": str(user_target.id)},
                    {"index": 1, "op": "DELETE", "target_id": str(derived_target.id)},
                ]
            }
        ),
    )
    dlq = _RecordingDLQ()
    outcome = await flush_messages_with_outcome(
        _turn(),
        memory_store=store,
        embedder=_ReportingEmbedder({"drop": _NEAR_EAST, "retract": _NEAR_EAST}),
        llm_caller=llm,
        tenant_id=_TENANT,
        user_id=_USER,
        thread_id=uuid4(),
        token=CancellationToken(),
        reconcile=True,
        dlq=dlq,  # type: ignore[arg-type]
    )
    assert outcome.failed
    # 被拦下丢弃的更新、删除事件都不该在重试时被重建成新记忆。
    assert dlq.enqueued == [[("fact", "keep")]]


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
# D12 —— 「写了几条」含去重合并里取代出来的新行(真栈验收 §117.9:库里多了 3 行、计数报 1)
# ---------------------------------------------------------------------------


class _GoneTargetStore(InMemoryMemoryStore):
    """``supersede`` 找不到目标(已被删 / 已被取代)时返回 ``None``、什么都不写。"""

    async def supersede(self, **kwargs: Any) -> MemoryItem | None:
        del kwargs
        return None


async def test_written_counts_reconciled_adds() -> None:
    store = InMemoryMemoryStore()
    _ = await _seed(store, "old", origin=None)
    llm = _LLM(user_reply=_reply({"kind": "fact", "content": "cand"}), ops_reply=_ops("ADD"))
    written, _ = await _flush(llm, store, _ReportingEmbedder({"cand": _NEAR_EAST}), reconcile=True)
    assert written == 1
    assert set(await _live(store)) == {"old", "cand"}


async def test_written_counts_the_row_a_supersede_wrote() -> None:
    store = InMemoryMemoryStore()
    target = await _seed(store, "old", origin=None)
    llm = _LLM(
        user_reply=_reply({"kind": "fact", "content": "cand"}), ops_reply=_ops("UPDATE", target.id)
    )
    written, _ = await _flush(llm, store, _ReportingEmbedder({"cand": _NEAR_EAST}), reconcile=True)
    assert written == 1
    rows = await _live(store)
    assert set(rows) == {"cand"}
    assert rows["cand"].supersedes == target.id


async def test_supersede_that_wrote_nothing_counts_only_the_fallback_write() -> None:
    store = _GoneTargetStore()
    target = await _seed(store, "old", origin=None)
    llm = _LLM(
        user_reply=_reply({"kind": "fact", "content": "cand"}), ops_reply=_ops("UPDATE", target.id)
    )
    written, _ = await _flush(llm, store, _ReportingEmbedder({"cand": _NEAR_EAST}), reconcile=True)
    # 取代没写成 → 候选退回直接写入,只算那一行。
    assert written == 1
    assert len(store._rows) == 2
    rows = await _live(store)
    assert set(rows) == {"old", "cand"}
    assert rows["cand"].supersedes is None


async def test_written_counts_superseded_and_added_rows_but_not_retractions() -> None:
    store = InMemoryMemoryStore()
    old1 = await _seed(store, "old1", origin=None)
    old2 = await _seed(store, "old2", origin=None)
    llm = _LLM(
        user_reply=_reply(
            {"kind": "fact", "content": "replaces old1"},
            {"kind": "fact", "content": "retracts old2"},
            {"kind": "fact", "content": "unrelated"},
        ),
        ops_reply=json.dumps(
            {
                "ops": [
                    {"index": 0, "op": "UPDATE", "target_id": str(old1.id)},
                    {"index": 1, "op": "DELETE", "target_id": str(old2.id)},
                ]
            }
        ),
    )
    rows_before = len(store._rows)
    written, _ = await _flush(
        llm,
        store,
        _ReportingEmbedder({"replaces old1": _NEAR_EAST, "retracts old2": _NEAR_EAST}),
        reconcile=True,
    )
    assert set(await _live(store)) == {"replaces old1", "unrelated"}
    # 新增的行 = 取代出来的一行 + 直接写入的一行;作废(DELETE)不新增行。
    assert written == len(store._rows) - rows_before == 2
