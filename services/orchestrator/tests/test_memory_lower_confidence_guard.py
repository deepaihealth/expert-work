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
* ④(11-14 重跑 118.5)内容与一条同 kind、同 agent_name 的有效邻居相同(按
  ``_normalized_text``:只忽略宽度、大小写、空白多少、末尾句末标点)、邻居的来源不弱于候选、候选
  不是更正 → 不问模型,直接 NOOP;指令里也写明「已有同样内容的用户说的 / 用户更正的 → NOOP」优先,
  带更正的例外(删掉与它矛盾的那条)。
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal
from uuid import UUID, uuid4

import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from prometheus_client import REGISTRY

from expert_work.common.user_input import user_input_kwargs
from expert_work.persistence import InMemoryMemoryStore
from expert_work.protocol import MemoryItem
from expert_work.protocol.memory_item import MemoryOrigin
from expert_work.runtime.cancellation import CancellationToken
from orchestrator.graph_builder.memory import _normalized_text, flush_messages_with_outcome

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

    def systems_of(self, mark: str | None) -> list[str]:
        """``mark`` = 哪一次提炼;``None`` = 去重合并那几次。"""
        marks = (PASS1_MARK, PASS2_MARK)
        systems = [str(call[0].content) for call in self.calls]
        if mark is not None:
            return [s for s in systems if mark in s]
        return [s for s in systems if not any(m in s for m in marks)]

    def system_of(self, mark: str | None) -> str:
        systems = self.systems_of(mark)
        if not systems:
            raise AssertionError(f"no call for {mark!r}")
        return systems[0]

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


async def _flush(
    llm: _LLM, store: InMemoryMemoryStore, near: str | tuple[str, ...] = "cand"
) -> int:
    """``near`` = 哪(几)段候选文字的向量贴近种子(``_EAST``),进去重合并。"""
    texts = (near,) if isinstance(near, str) else near
    outcome = await flush_messages_with_outcome(
        _turn(),
        memory_store=store,
        embedder=_Embedder(dict.fromkeys(texts, _NEAR_EAST)),
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
        ("DELETE", False, {"user said"}, 0, "lower_confidence_delete"),
        ("DELETE", True, {"user said", "cand"}, 1, "lower_confidence_delete_added"),
    ],
    ids=["update-correction-added", "update-dropped", "delete-noop", "delete-correction-added"],
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
        ("DELETE", False, {"corrected by the user"}, "lower_confidence_delete"),
        ("DELETE", True, {"corrected by the user", "cand"}, "lower_confidence_delete_added"),
    ],
    ids=["update-added", "update-dropped", "delete-noop", "delete-correction-added"],
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


# ---------------------------------------------------------------------------
# ④ 逐字相同的邻居(11-14 重跑 118.5:用户候选 X 同时对上一条相同的用户 X 与一条来自助手的
#    X+Y,模型照「用户说的对上来自助手的 → UPDATE」取代了后者,多出一条重复的用户 X)
# ---------------------------------------------------------------------------


async def _seed_row(
    store: InMemoryMemoryStore,
    content: str,
    *,
    origin: MemoryOrigin | None,
    confidence: float = 0.8,
    kind: Literal["fact", "episodic"] = "fact",
    agent_name: str | None = None,
) -> MemoryItem:
    item = MemoryItem(
        id=uuid4(),
        tenant_id=_TENANT,
        user_id=_USER,
        kind=kind,
        agent_name=agent_name,
        content=content,
        embedding=_EAST,
        origin=origin,
        confidence=confidence,
    )
    _ = await store.write([item])
    return item


def _reconcile_calls(llm: _LLM) -> int:
    return len(llm.systems_of(None))


async def test_user_candidate_identical_to_a_user_memory_is_a_noop_without_asking() -> None:
    """118.5 重跑的第 4 轮原样:邻居里有相同的用户 X 和来自助手的 X+Y,模型若被问会去取代后者。"""
    store = InMemoryMemoryStore()
    stated = await _seed_row(store, "cand", origin="user_stated", confidence=0.9)
    derived = await _seed_row(store, "cand and more", origin="assistant_derived", confidence=0.6)
    llm = _LLM(user_reply=_stated(0.9), ops_reply=_ops("UPDATE", derived.id))
    before = _count("noop")
    assert await _flush(llm, store) == 0
    assert _reconcile_calls(llm) == 0
    rows = await _live(store)
    assert set(rows) == {"cand", "cand and more"}
    assert rows["cand"].id == stated.id
    assert len(store._rows) == 2
    assert _count("noop") == before + 1


@pytest.mark.parametrize(
    ("origin", "confidence"),
    [("user_stated", 0.9), (None, 1.0), ("assistant_derived", 0.6), (None, 0.5)],
    ids=["user_stated", "user_corrected", "assistant_derived", "unrecorded"],
)
async def test_assistant_candidate_identical_to_any_memory_is_a_noop(
    origin: MemoryOrigin | None, confidence: float
) -> None:
    store = InMemoryMemoryStore()
    _ = await _seed_row(store, "  cand \n", origin=origin, confidence=confidence)
    other = await _seed_row(store, "something else", origin="assistant_derived", confidence=0.6)
    llm = _LLM(
        conversation_reply=_reply({"kind": "fact", "content": "cand"}),
        ops_reply=_ops("UPDATE", other.id),
    )
    assert await _flush(llm, store) == 0
    assert _reconcile_calls(llm) == 0
    assert len(store._rows) == 2


@pytest.mark.parametrize(
    ("origin", "confidence"),
    [("assistant_derived", 0.6), (None, 0.5)],
    ids=["assistant_derived", "unrecorded"],
)
async def test_user_candidate_identical_to_a_weaker_memory_still_asks(
    origin: MemoryOrigin | None, confidence: float
) -> None:
    """用户说的候选对上相同内容、但来源更弱的那条:照旧问模型(判「更新」就把它升成用户说的)。"""
    store = InMemoryMemoryStore()
    weaker = await _seed_row(store, "cand", origin=origin, confidence=confidence)
    llm = _LLM(user_reply=_stated(0.9), ops_reply=_ops("UPDATE", weaker.id))
    _ = await _flush(llm, store)
    assert _reconcile_calls(llm) == 1
    rows = await _live(store)
    assert set(rows) == {"cand"}
    assert (rows["cand"].origin, rows["cand"].supersedes) == ("user_stated", weaker.id)


@pytest.mark.parametrize("existing", ["can d", "cand 2", "cand x"])
async def test_non_identical_content_still_asks(existing: str) -> None:
    store = InMemoryMemoryStore()
    _ = await _seed_row(store, existing, origin="user_stated", confidence=0.9)
    llm = _LLM(user_reply=_stated(0.9), ops_reply='{"ops": [{"index": 0, "op": "NOOP"}]}')
    _ = await _flush(llm, store)
    assert _reconcile_calls(llm) == 1


async def test_reconcile_prompt_puts_an_identical_user_memory_first() -> None:
    store = InMemoryMemoryStore()
    _ = await _seed_row(store, "cand x", origin="user_stated", confidence=0.9)
    llm = _LLM(user_reply=_stated(0.9), ops_reply="{}")
    _ = await _flush(llm, store)
    system = llm.system_of(None)
    rule = (
        "When an existing memory stated in user messages or corrected by the user already "
        "says the same thing as the candidate, the candidate is a NOOP, even if an existing "
        "memory from the assistant also matches it; this takes precedence over the rule below "
        "about memories from the assistant."
    )
    assert rule in system
    assert system.index(rule) < system.index("is an UPDATE, not a NOOP.")
    # 审查 MEDIUM-2 —— 与代码里「带更正的不走确定性 NOOP」一致。
    exception = (
        'In that case, a candidate with "is_correction": true is not a NOOP for this reason; '
        "it DELETEs the existing memory it contradicts, if there is one; otherwise it is a NOOP."
    )
    assert exception in system
    assert system.index(rule) < system.index(exception)
    # 审查 LOW-4 —— 「优先于」指的是下面那句「用户说的对上来自助手的 → UPDATE」。
    assert system.index(exception) < system.index("is an UPDATE, not a NOOP.")


async def test_identical_correction_still_asks_so_a_conflicting_memory_can_be_retracted() -> None:
    """审查 MEDIUM(11-14)—— 用户说「不,是 X」:候选 X 带 ``is_correction``,和已有的 X
    内容相同(归一后),但旁边还有一条冲突的用户记忆 Y(把握度更低、经第二条拦截规则并存下来的)。
    确定性 NOOP 会让模型永远没机会删掉 Y,所以带更正的候选照旧问模型。"""
    store = InMemoryMemoryStore()
    stated = await _seed_row(store, "cand", origin="user_stated", confidence=0.9)
    conflicting = await _seed_row(store, "cand other", origin="user_stated", confidence=0.5)
    llm = _LLM(
        user_reply=_stated(0.9, is_correction=True), ops_reply=_ops("DELETE", conflicting.id)
    )
    _ = await _flush(llm, store)
    assert _reconcile_calls(llm) == 1
    rows = await _live(store)
    assert set(rows) == {"cand"}
    assert rows["cand"].id == stated.id
    assert next(r for r in store._rows if r.id == conflicting.id).expired_at is not None


def _turn_six_llm(target: UUID) -> _LLM:
    reply = _reply({"kind": "fact", "content": "用户的取件码是7190", "confidence": 0.9})
    return _LLM(user_reply=reply, ops_reply=_ops("UPDATE", target))


async def test_turn_six_candidate_differing_only_in_punctuation_is_a_noop() -> None:
    """测试环境实测(8ef6b0a21)第 6 轮:已有用户说的 X(带句号「。」)和来自助手的 X+Y;用户候选 X
    只差末尾标点。原来交给模型 → 「更新」X+Y → 多出一条只差标点的用户 X(content_hash 不同,
    唯一索引挡不住)。现在按归一后的文字判相同,直接 NOOP。"""
    store = InMemoryMemoryStore()
    stated = await _seed_row(store, "用户的取件码是7190。", origin="user_stated", confidence=0.9)
    derived = await _seed_row(
        store, "用户的取件码是7190,助手提醒按时取件", origin="assistant_derived", confidence=0.6
    )
    llm = _turn_six_llm(derived.id)
    assert await _flush(llm, store, near="用户的取件码是7190") == 0
    assert _reconcile_calls(llm) == 0
    assert {r.id for r in store._rows} == {stated.id, derived.id}
    assert all(r.invalid_at is None for r in store._rows)


@pytest.mark.parametrize(
    ("kind", "agent_name"),
    [("episodic", None), ("fact", "another-agent")],
    ids=["other-kind", "other-agent"],
)
async def test_identical_text_in_another_kind_or_agent_slot_still_asks(
    kind: Literal["fact", "episodic"], agent_name: str | None
) -> None:
    """审查 LOW-1 —— 只和同一 kind、同一 agent_name 的邻居比(写入去重也按这一格)。"""
    store = InMemoryMemoryStore()
    _ = await _seed_row(
        store, "cand", origin="user_stated", confidence=0.9, kind=kind, agent_name=agent_name
    )
    llm = _LLM(user_reply=_stated(0.9), ops_reply='{"ops": [{"index": 0, "op": "NOOP"}]}')
    _ = await _flush(llm, store)
    assert _reconcile_calls(llm) == 1


# ---------------------------------------------------------------------------
# 审查 MEDIUM-1(11-14 终审)—— 带更正、又与已有用户记忆内容相同的候选:模型可以删掉别的冲突
# 记忆(判「更新」别的那条按删除处理),但这条候选本身不再落成新行
# ---------------------------------------------------------------------------

_X = "\u7528\u6237\u7684\u53d6\u4ef6\u7801\u662f7190"  # 用户的取件码是7190
_X_STOP = _X + "\u3002"


async def _x_and_derived(
    store: InMemoryMemoryStore, x_confidence: float = 0.95
) -> tuple[MemoryItem, MemoryItem]:
    stated = await _seed_row(store, _X_STOP, origin="user_stated", confidence=x_confidence)
    derived = await _seed_row(
        store, _X + ",\u52a9\u624b\u63d0\u9192\u6309\u65f6\u53d6\u4ef6",
        origin="assistant_derived", confidence=0.6,
    )  # fmt: skip
    return stated, derived


def _valid(store: InMemoryMemoryStore) -> list[MemoryItem]:
    return [r for r in store._rows if r.invalid_at is None and r.expired_at is None]


def _user_rows_with_x(store: InMemoryMemoryStore) -> list[MemoryItem]:
    return [
        r
        for r in _valid(store)
        if r.origin == "user_stated" and _normalized_text(r.content) == _normalized_text(_X)
    ]


def _correction_x() -> str:
    return _reply({"kind": "fact", "content": _X, "confidence": 0.9, "is_correction": True})


#: 已有用户 X 的把握度:0.95 高于候选(0.9),对 X 的改动先被第二条拦截规则拦下;0.5 低于候选,
#: 拦截规则放行,只剩 ``_redundant_correction_op`` 把它改成 NOOP(复审 LOW-1:否则把「删掉相同
#: 那条」的变异也放过了)。
_X_CONFIDENCES = pytest.mark.parametrize("x_confidence", [0.95, 0.5], ids=["x-more", "x-less"])


@_X_CONFIDENCES
@pytest.mark.parametrize("target", ["identical", "derived"])
async def test_identical_correction_updating_anything_leaves_one_user_row(
    target: str, x_confidence: float
) -> None:
    """终审复现:模型判「更新」那条相同的 X(原来:被第二条规则拦下、按新增存下)或「更新」来自助手的
    X+Y(原来:取代出第二条用户 X)。现在:前者 NOOP;后者按删除那条 X+Y 处理。"""
    store = InMemoryMemoryStore()
    stated, derived = await _x_and_derived(store, x_confidence)
    tid = stated.id if target == "identical" else derived.id
    llm = _LLM(user_reply=_correction_x(), ops_reply=_ops("UPDATE", tid))
    assert await _flush(llm, store, near=_X) == 0
    assert _reconcile_calls(llm) == 1
    assert [r.id for r in _user_rows_with_x(store)] == [stated.id]
    derived_now = next(r for r in store._rows if r.id == derived.id)
    assert (derived_now.expired_at is not None) is (target == "derived")
    assert derived_now.invalid_at is None


@pytest.mark.parametrize(
    "ops_reply",
    ['{"ops": [{"index": 0, "op": "ADD"}]}', '{"ops": [{"index": 0, "op": "NOOP"}]}', "{}"],
    ids=["add", "noop", "missing-decision"],
)
async def test_identical_correction_never_adds_a_row(ops_reply: str) -> None:
    store = InMemoryMemoryStore()
    stated, derived = await _x_and_derived(store)
    llm = _LLM(user_reply=_correction_x(), ops_reply=ops_reply)
    assert await _flush(llm, store, near=_X) == 0
    assert [r.id for r in _user_rows_with_x(store)] == [stated.id]
    assert {r.id for r in _valid(store)} == {stated.id, derived.id}


@_X_CONFIDENCES
async def test_identical_correction_deleting_the_identical_memory_is_a_noop(
    x_confidence: float,
) -> None:
    store = InMemoryMemoryStore()
    stated, _derived = await _x_and_derived(store, x_confidence)
    llm = _LLM(user_reply=_correction_x(), ops_reply=_ops("DELETE", stated.id))
    _ = await _flush(llm, store, near=_X)
    assert [r.id for r in _user_rows_with_x(store)] == [stated.id]
    assert next(r for r in store._rows if r.id == stated.id).expired_at is None


@pytest.mark.parametrize("op", ["UPDATE", "DELETE"])
async def test_converted_update_of_a_more_certain_user_memory_is_refused(op: str) -> None:
    """复审 LOW-2 —— 模型判「更新」(改写成删除)或「删除」一条更有把握的冲突用户记忆 → 第二条
    拦截规则拒绝:什么都没改,计在 ``lower_confidence_delete``。评测 A 组 P9(11-14)后被拦的删除
    带更正会按新增存下,但与已有用户记忆内容相同的这种除外 —— 仍不落新行。"""
    store = InMemoryMemoryStore()
    stated = await _seed_row(store, _X_STOP, origin="user_stated", confidence=0.5)
    other = await _seed_row(store, _X + "?", origin="user_stated", confidence=0.95)
    before_delete = _count("lower_confidence_delete")
    before_added = _count("lower_confidence_update_added")
    llm = _LLM(user_reply=_correction_x(), ops_reply=_ops(op, other.id))
    assert await _flush(llm, store, near=_X) == 0
    assert {r.id for r in _valid(store)} == {stated.id, other.id}
    assert _count("lower_confidence_delete") == before_delete + 1
    assert _count("lower_confidence_update_added") == before_added


async def test_identical_correction_without_a_similar_neighbour_adds_nothing() -> None:
    """向量不够近、没进模型的材料时,带更正的相同候选同样不落新行(原来会直接新增)。"""
    store = InMemoryMemoryStore()
    stated, _derived = await _x_and_derived(store)
    llm = _LLM(user_reply=_correction_x())
    assert await _flush(llm, store, near=()) == 0
    assert _reconcile_calls(llm) == 0
    assert [r.id for r in _user_rows_with_x(store)] == [stated.id]


async def test_user_candidate_identical_to_a_user_correction_is_a_noop() -> None:
    store = InMemoryMemoryStore()
    corrected = await _seed_row(store, "cand", origin=None, confidence=1.0)
    llm = _LLM(user_reply=_stated(0.9), ops_reply=_ops("UPDATE", corrected.id))
    assert await _flush(llm, store) == 0
    assert _reconcile_calls(llm) == 0
    assert [r.id for r in _valid(store)] == [corrected.id]


async def test_decisions_stay_aligned_when_an_earlier_candidate_is_dropped() -> None:
    """第一条候选被确定性 NOOP 掉,第二条进模型:模型的 index 0 指的是第二条。"""
    store = InMemoryMemoryStore()
    _ = await _seed_row(store, "cand", origin="user_stated", confidence=0.9)
    other = await _seed_row(store, "old other", origin="assistant_derived", confidence=0.6)
    llm = _LLM(
        user_reply=_reply(
            {"kind": "fact", "content": "cand", "confidence": 0.9},
            {"kind": "fact", "content": "new other", "confidence": 0.9},
        ),
        ops_reply=_ops("UPDATE", other.id),
    )
    assert await _flush(llm, store, near=("cand", "new other")) == 1
    assert _reconcile_calls(llm) == 1
    payload = json.loads(str(llm.calls[-1][1].content))  # 去重合并在两次提炼之后
    assert [c["content"] for c in payload["candidates"]] == ["new other"]
    rows = await _live(store)
    assert set(rows) == {"cand", "new other"}
    assert rows["new other"].supersedes == other.id


# ---------------------------------------------------------------------------
# 审查(11-14,8ddd32c0f)MEDIUM-3 / MEDIUM-4 —— 更正型候选的新值既不重复落行,也不跟着删除丢掉
# ---------------------------------------------------------------------------


async def test_blocked_correction_restating_an_existing_memory_is_not_added_again() -> None:
    """来自助手的更正 Y 每轮都被重抽;库里已有一条只差标点的来自助手的 Y。模型判「删掉用户的 X」被拦:
    带更正也不再按新增存下 —— 几轮下来有效的 Y 只有一条。"""
    store = InMemoryMemoryStore()
    x = await _seed_row(store, "cand", origin="user_stated", confidence=0.9)
    y = await _seed_row(store, "cand y\u3002", origin="assistant_derived", confidence=0.6)
    for _turn_no in range(3):
        llm = _LLM(
            conversation_reply=_reply({"kind": "fact", "content": "cand y", "is_correction": True}),
            ops_reply=_ops("DELETE", x.id),
        )
        assert await _flush(llm, store, near="cand y") == 0
    valid = {r.id for r in _valid(store)}
    assert valid == {x.id, y.id}


@pytest.mark.parametrize(
    ("candidate", "target_origin"),
    [("user", "assistant_derived"), ("assistant", "assistant_derived"), ("user", None)],
    ids=["user-vs-assistant", "assistant-vs-assistant", "user-vs-unrecorded"],
)
async def test_unblocked_delete_by_a_correction_keeps_the_new_value(
    candidate: str, target_origin: MemoryOrigin | None
) -> None:
    """更正被判成「删除」而且没被拦(目标不受保护):旧的作废,新值也要存下,不能两样都没了。"""
    store = InMemoryMemoryStore()
    old = await _seed_row(store, "old value", origin=target_origin, confidence=0.6)
    reply = _reply({"kind": "fact", "content": "cand", "confidence": 0.9, "is_correction": True})
    llm = (
        _LLM(user_reply=reply, ops_reply=_ops("DELETE", old.id))
        if candidate == "user"
        else _LLM(conversation_reply=reply, ops_reply=_ops("DELETE", old.id))
    )
    before = _count("delete_added")
    assert await _flush(llm, store) == 1
    rows = await _live(store)
    assert set(rows) == {"cand"}
    assert next(r for r in store._rows if r.id == old.id).expired_at is not None
    assert _count("delete_added") == before + 1


async def test_unblocked_delete_without_a_correction_still_stores_nothing() -> None:
    store = InMemoryMemoryStore()
    old = await _seed_row(store, "old value", origin="assistant_derived", confidence=0.6)
    llm = _LLM(user_reply=_stated(0.9), ops_reply=_ops("DELETE", old.id))
    assert await _flush(llm, store) == 0
    assert await _live(store) == {}


async def test_reconcile_prompt_keeps_new_values_and_dated_readings() -> None:
    store = InMemoryMemoryStore()
    _ = await _seed_row(store, "cand x", origin="user_stated", confidence=0.9)
    llm = _LLM(user_reply=_stated(0.9), ops_reply="{}")
    _ = await _flush(llm, store)
    system = " ".join(llm.system_of(None).split())
    assert (
        "A correction that carries a new value is an UPDATE of the memory it replaces, not a "
        "DELETE; DELETE only when the candidate retracts an existing memory without giving a "
        "new value." in system
    )
    assert (
        "A reading, measurement or status that refers to a different date or time than an "
        "existing one is a new memory (ADD), not an UPDATE of the earlier one." in system
    )
