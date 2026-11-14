"""B-300 用户 11-14 定的三条(测试验收 118.5 红:第一次提炼把一个孤零零的短回答读成了「更正」,
去重合并拿它取代了用户自己说的那条)。

* ① 第一次提炼的指令:只有一个值、没说是什么的那句,永远不是更正,也不是对旧记忆的更新;
  ``is_correction`` 只在同一句话自己说清改的是什么、新值是什么时才为真。
* ② 程序的拦截:「用户消息里说的」候选,置信度**低于**目标(用户一方的记忆,含成员里有用户一方的
  汇总行;汇总行按成员里最高的用户一方置信度比,查不完按 1.0)时,不得更新 / 删除它 ——
  与来自助手的拦截同一套处理:
  被拦的删除什么都不做;被拦的更新带 ``is_correction`` 按新增存下,不带就丢弃。预判一次,
  ``supersede`` / ``expire`` 的行锁里按此刻的行再判一次。计数用单独的标签。
* ③ 去重合并的指令补上另一个方向:来自助手的候选对上用户说的 / 用户更正的已有记忆 → NOOP。
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from prometheus_client import REGISTRY

from expert_work.common.user_input import user_input_kwargs
from expert_work.persistence import InMemoryMemoryStore
from expert_work.protocol import MemoryItem
from expert_work.protocol.memory_item import MemoryOrigin
from expert_work.runtime.cancellation import CancellationToken
from orchestrator.graph_builder.memory import flush_messages_with_outcome

_TENANT = UUID("00000000-0000-4000-8000-0000000000a3")
_USER = UUID("00000000-0000-4000-8000-0000000000b3")
_EAST = (1.0, 0.0, 0.0, 0.0)
_NEAR_EAST = (0.9, 0.43589, 0.0, 0.0)  # cosine vs _EAST = 0.9 ≥ 0.80
_NORTH = (0.0, 1.0, 0.0, 0.0)

PASS1_MARK = "The other side of the conversation is not shown."
PASS2_MARK = "Every line starts with a tag:"
_EMPTY = '{"memories": []}'


def _reply(*memories: dict[str, Any]) -> str:
    return json.dumps({"memories": list(memories)}, ensure_ascii=False)


def _ops(op: str, target: UUID) -> str:
    return json.dumps({"ops": [{"index": 0, "op": op, "target_id": str(target)}]})


@dataclass
class _LLM:
    user_reply: str = _EMPTY
    conversation_reply: str = _EMPTY
    ops_reply: str = '{"ops": []}'
    while_deciding: Callable[[], Awaitable[object]] | None = None
    calls: list[list[BaseMessage]] = field(default_factory=list)

    def system_of(self, mark: str | None) -> str:
        """``mark`` = 哪一次提炼;``None`` = 去重合并那次。"""
        marks = (PASS1_MARK, PASS2_MARK)
        for call in self.calls:
            system = str(call[0].content)
            hit = mark in system if mark is not None else not any(m in system for m in marks)
            if hit:
                return system
        raise AssertionError(f"no call for {mark!r}")

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
class _Embedder:
    mapping: dict[str, tuple[float, ...]] = field(default_factory=dict)

    async def embed(self, texts: Sequence[str], *, tenant_id: UUID) -> list[tuple[float, ...]]:
        del tenant_id
        return [self.mapping.get(t, _NORTH) for t in texts]


class _CountingStore(InMemoryMemoryStore):
    """数一数改库的调用:预判拦下的,根本走不到 ``supersede`` / ``expire``。"""

    def __init__(self) -> None:
        super().__init__()
        self.writes_to_targets = 0

    async def supersede(self, **kwargs: Any) -> MemoryItem | None:
        self.writes_to_targets += 1
        return await super().supersede(**kwargs)

    async def expire(self, **kwargs: Any) -> bool:
        self.writes_to_targets += 1
        return await super().expire(**kwargs)


def _turn() -> list[BaseMessage]:
    text = "my words"
    user = HumanMessage(
        content=text,
        additional_kwargs=user_input_kwargs(text=text, images=0, files=0, external_blocks=0),
    )
    return [user, AIMessage(content="the answer")]


async def _flush(llm: _LLM, store: InMemoryMemoryStore) -> int:
    outcome = await flush_messages_with_outcome(
        _turn(),
        memory_store=store,
        embedder=_Embedder({"cand": _NEAR_EAST}),
        llm_caller=llm,  # type: ignore[arg-type]
        tenant_id=_TENANT,
        user_id=_USER,
        thread_id=uuid4(),
        token=CancellationToken(),
        reconcile=True,
    )
    return outcome.written


async def _seed(
    store: InMemoryMemoryStore, *, origin: MemoryOrigin | None, confidence: float
) -> MemoryItem:
    item = MemoryItem(
        id=uuid4(),
        tenant_id=_TENANT,
        user_id=_USER,
        kind="fact",
        content="user said",
        embedding=_EAST,
        origin=origin,
        confidence=confidence,
    )
    _ = await store.write([item])
    return item


async def _live(store: InMemoryMemoryStore) -> dict[str, MemoryItem]:
    rows = await store.retrieve(tenant_id=_TENANT, user_id=_USER, query_embedding=_EAST, limit=50)
    return {r.content: r for r in rows}


def _count(op: str) -> float:
    value = REGISTRY.get_sample_value("expert_work_cm_memory_reconcile_total", {"op": op})
    return value or 0.0


def _stated(confidence: float, *, is_correction: bool = False) -> str:
    return _reply(
        {
            "kind": "fact",
            "content": "cand",
            "confidence": confidence,
            "is_correction": is_correction,
        }
    )


# ---------------------------------------------------------------------------
# ① 第一次提炼的指令
# ---------------------------------------------------------------------------


async def test_user_text_pass_says_a_bare_value_is_never_a_correction() -> None:
    llm = _LLM()
    _ = await _flush(llm, InMemoryMemoryStore())
    system = llm.system_of(PASS1_MARK)
    assert (
        "A value or name on its own, with nothing in the same message saying what it is, is "
        "never a correction and never an update of an earlier memory, even when it differs from "
        "something said before." in " ".join(system.split())
    )
    assert (
        '"is_correction": true ONLY when the same message itself says what is being corrected '
        "and gives the new value" in " ".join(system.split())
    )
    assert "explicitly corrects or updates something stated earlier" not in system


# ---------------------------------------------------------------------------
# ③ 去重合并的指令:另一个方向
# ---------------------------------------------------------------------------


async def test_reconcile_prompt_says_an_assistant_copy_of_a_user_memory_is_a_noop() -> None:
    store = InMemoryMemoryStore()
    _ = await _seed(store, origin="user_stated", confidence=0.9)
    llm = _LLM(conversation_reply=_reply({"kind": "fact", "content": "cand"}), ops_reply="{}")
    _ = await _flush(llm, store)
    system = llm.system_of(None)
    assert (
        "A candidate from the assistant that matches an existing memory stated in user "
        "messages or corrected by the user is a NOOP, not an ADD." in system
    )
    # 原来那个方向的一句还在。
    assert "is an UPDATE, not a NOOP." in system


# ---------------------------------------------------------------------------
# ② 置信度更低的用户候选
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("op", "is_correction", "expected_live", "written", "label"),
    [
        ("UPDATE", True, {"user said", "cand"}, 1, "lower_confidence_update_added"),
        ("UPDATE", False, {"user said"}, 0, "lower_confidence_update_dropped"),
        ("DELETE", True, {"user said"}, 0, "lower_confidence_delete"),
    ],
    ids=["update-correction-added", "update-dropped", "delete-noop"],
)
@pytest.mark.parametrize(
    ("origin", "confidence"),
    [("user_stated", 0.9), (None, 1.0)],
    ids=["user_stated", "user_corrected"],
)
async def test_lower_confidence_user_candidate_cannot_change_a_user_memory(
    op: str,
    is_correction: bool,
    expected_live: set[str],
    written: int,
    label: str,
    origin: MemoryOrigin | None,
    confidence: float,
) -> None:
    store = _CountingStore()
    target = await _seed(store, origin=origin, confidence=confidence)
    llm = _LLM(user_reply=_stated(0.5, is_correction=is_correction), ops_reply=_ops(op, target.id))
    before, before_assistant = _count(label), _count("blocked_update_added")
    assert await _flush(llm, store) == written
    rows = await _live(store)
    assert set(rows) == expected_live
    assert rows["user said"].confidence == confidence
    assert all(r.invalid_at is None and r.expired_at is None for r in store._rows)
    if "cand" in rows:
        assert rows["cand"].supersedes is None
        assert rows["cand"].origin == "user_stated"
    assert _count(label) == before + 1
    assert _count("blocked_update_added") == before_assistant  # 与来自助手的拦截分开计
    # 预判就拦下了,没有走到改库那一步(锁里的那道是兜底)。
    assert store.writes_to_targets == 0


@pytest.mark.parametrize("op", ["UPDATE", "DELETE"])
async def test_equal_or_higher_confidence_user_candidate_still_applies(op: str) -> None:
    store = InMemoryMemoryStore()
    target = await _seed(store, origin="user_stated", confidence=0.7)
    llm = _LLM(user_reply=_stated(0.7), ops_reply=_ops(op, target.id))
    _ = await _flush(llm, store)
    assert set(await _live(store)) == ({"cand"} if op == "UPDATE" else set())


async def test_lower_confidence_user_candidate_may_change_an_unrecorded_memory() -> None:
    store = InMemoryMemoryStore()
    target = await _seed(store, origin=None, confidence=0.9)
    llm = _LLM(user_reply=_stated(0.5), ops_reply=_ops("UPDATE", target.id))
    _ = await _flush(llm, store)
    assert set(await _live(store)) == {"cand"}


async def _seed_summary(
    store: InMemoryMemoryStore, members_spec: Sequence[tuple[MemoryOrigin | None, float]]
) -> MemoryItem:
    members = [
        MemoryItem(
            id=uuid4(),
            tenant_id=_TENANT,
            user_id=_USER,
            kind="fact",
            content=f"member {i}",
            embedding=_NORTH,
            origin=o,
            confidence=c,
        )
        for i, (o, c) in enumerate(members_spec)
    ]
    _ = await store.write(members)
    stored = {r.id: r for r in store._rows}
    return await store.write_consolidated(
        tenant_id=_TENANT,
        user_id=_USER,
        content="summary",
        embedding=_EAST,
        source_ids=[m.id for m in members],
        source_hashes={m.id: str(stored[m.id].content_hash) for m in members},
    )


_STATED_06 = (("user_stated", 0.6), (None, 0.6), (None, 0.6))
#: 一条用户更正(1.0)+ 两条来源未记录(0.5):汇总行自己的置信度是成员最小值 0.5。
_CORRECTED_AND_UNRECORDED = ((None, 1.0), (None, 0.5), (None, 0.5))


@pytest.mark.parametrize(
    ("members", "summary_confidence", "candidate", "changed"),
    [
        (_STATED_06, 0.6, 0.5, False),
        (_STATED_06, 0.6, 0.6, True),
        (_CORRECTED_AND_UNRECORDED, 0.5, 0.6, False),
        (_CORRECTED_AND_UNRECORDED, 0.5, 0.99, False),
    ],
    ids=["stated-lower", "stated-equal", "correction-vs-0.6", "correction-vs-0.99"],
)
async def test_summary_target_is_compared_by_its_most_certain_user_side_member(
    members: Sequence[tuple[MemoryOrigin | None, float]],
    summary_confidence: float,
    candidate: float,
    changed: bool,
) -> None:
    """审查(11-14)—— 汇总行按它成员里**最高**的用户一方置信度比,不按自己那一行(成员最小值):
    否则 {用户更正 1.0, 未记录 0.5} 汇成的 0.5 汇总行会被 0.6 的用户候选取代,更正就没了。"""
    store = _CountingStore()
    summary = await _seed_summary(store, members)
    assert (summary.origin, summary.confidence) == (None, summary_confidence)
    llm = _LLM(user_reply=_stated(candidate), ops_reply=_ops("UPDATE", summary.id))
    _ = await _flush(llm, store)
    assert set(await _live(store)) == ({"cand"} if changed else {"summary"})
    # 拦下的那几条在预判就拦了,没走到改库(锁里那道另由存储测试覆盖)。
    assert store.writes_to_targets == (1 if changed else 0)


def _raise_confidence_while_deciding(
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
        ("UPDATE", True, {"corrected by the user", "cand"}, "lower_confidence_update_added"),
        ("UPDATE", False, {"corrected by the user"}, "lower_confidence_update_dropped"),
        ("DELETE", True, {"corrected by the user"}, "lower_confidence_delete"),
    ],
    ids=["update-added", "update-dropped", "delete-noop"],
)
async def test_target_made_more_certain_while_deciding_is_rechecked_under_the_lock(
    op: str, is_correction: bool, expected_live: set[str], label: str
) -> None:
    """读邻居时目标是 0.5(候选 0.6 预判放过),判的那几秒里用户「更正」成 1.0:锁里重判,拦下。"""
    store = InMemoryMemoryStore()
    target = await _seed(store, origin="user_stated", confidence=0.5)
    llm = _LLM(
        user_reply=_stated(0.6, is_correction=is_correction),
        ops_reply=_ops(op, target.id),
        while_deciding=_raise_confidence_while_deciding(store, target),
    )
    before = _count(label)
    _ = await _flush(llm, store)
    rows = await _live(store)
    assert set(rows) == expected_live
    assert rows["corrected by the user"].confidence == 1.0
    assert _count(label) == before + 1
