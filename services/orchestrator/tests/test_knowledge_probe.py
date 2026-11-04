"""B-267 step 3 — the per-turn knowledge probe.

* Golden platform lines (English, handles and counts only).
* Hit / miss per base: keyword coverage of the message's terms; vector only
  above the base's own calibration, under the model it was made with; no
  calibration → keyword only and no query embedded.
* Skips: small talk, not a fresh user turn, timeout, error, no bound base.
* Wiring: the line rides this turn's ``<platform-context>`` on the first step
  only; switch off / opted out (``knowledge.probe: false``) builds no probe.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from pydantic import ValidationError

from expert_work.common.conversation_channel import HIDE_FROM_UI
from expert_work.common.spotlight import spotlight_untrusted
from expert_work.persistence import InMemoryKnowledgeStore
from expert_work.persistence.embedding import EMBEDDING_DIM
from expert_work.persistence.knowledge.profiles import PROFILE_DEBOUNCE
from expert_work.protocol import (
    AgentSpec,
    DocumentStatus,
    KnowledgeChunk,
    KnowledgeSpec,
    ScoredChunk,
)
from expert_work.protocol.knowledge_profile import ProbeCalibration
from orchestrator import ToolEnv
from orchestrator.agent_factory import _knowledge_probe
from orchestrator.llm import FakeEmbedder
from orchestrator.tools import KnowledgeRetriever
from orchestrator.tools.knowledge_probe import (
    MIN_SHARED_TERMS,
    MIN_TERM_CHARS,
    PROBE_CONCURRENCY,
    PROBE_MAX_BASES,
    KnowledgeProbe,
    ProbeOutcome,
    is_small_talk,
    probe_line,
    record_follow_up,
    turn_query,
)
from orchestrator.tools.registry import ToolContext, ToolRegistry, ToolResult, ToolSpec

from .test_figure_block_injection import _graph, _RecordingLLM, _tool_turn

_NOW = datetime(2026, 11, 4, tzinfo=UTC)
_AXIS = (1.0,) + (0.0,) * (EMBEDDING_DIM - 1)


# --------------------------------------------------------------------------- goldens


def test_the_single_base_line_golden() -> None:
    assert probe_line([("K1", 3)]) == (
        "[knowledge probe] Knowledge base K1 has 3 passages related to this message; "
        "search it with knowledge_search before answering."
    )
    assert probe_line([("K2", 1)]) == (
        "[knowledge probe] Knowledge base K2 has 1 passage related to this message; "
        "search it with knowledge_search before answering."
    )


def test_the_several_bases_line_golden() -> None:
    assert probe_line([("K1", 2), ("K3", 1)]) == (
        "[knowledge probe] Knowledge bases K1 (2 passages), K3 (1 passage) have passages "
        "related to this message; search them with knowledge_search before answering."
    )


def test_no_hit_no_line() -> None:
    assert probe_line([]) is None


# --------------------------------------------------------------------------- turn query


def test_a_fresh_turn_probes_the_latest_user_message() -> None:
    messages = [
        HumanMessage(content="大国食养这本书讲了什么内容呢请详细说"),
        AIMessage(content="…"),
        HumanMessage(content="那么作者在书中是如何具体描述二十四节气与日常饮食之间的关系的"),
        HumanMessage(content="[本轮输入] x", additional_kwargs={HIDE_FROM_UI: True}),
    ]
    assert turn_query(messages) == "那么作者在书中是如何具体描述二十四节气与日常饮食之间的关系的"


def test_a_short_follow_up_carries_the_previous_message() -> None:
    messages = [
        HumanMessage(content="大国食养谁写的"),
        AIMessage(content="…"),
        HumanMessage(content="那他还写过什么"),
    ]
    assert turn_query(messages) == "大国食养谁写的\n那他还写过什么"


@pytest.mark.parametrize(
    "after",
    [
        [AIMessage(content="", tool_calls=[{"name": "x", "args": {}, "id": "1"}])],
        [
            AIMessage(content="", tool_calls=[{"name": "x", "args": {}, "id": "1"}]),
            ToolMessage(content="ok", tool_call_id="1"),
        ],
        [AIMessage(content="done")],
    ],
    ids=["after-a-call", "approval-resume", "answered"],
)
def test_not_a_fresh_turn_is_not_probed(after: list[BaseMessage]) -> None:
    assert turn_query([HumanMessage(content="大国食养谁写的"), *after]) is None


def test_the_spotlight_fence_and_platform_section_are_stripped() -> None:
    wrapped = spotlight_untrusted("大国食养谁写的", nonce="abc123def456")
    text = turn_query([HumanMessage(content=wrapped)])
    assert text == "大国食养谁写的"


@pytest.mark.parametrize("text", ["你好", "谢谢!", "OK", "  hi  ", "Thank you.", "嗯", "?"])
def test_small_talk(text: str) -> None:
    assert is_small_talk(text)


@pytest.mark.parametrize("text", ["大国食养谁写的", "QXA-N7 的价格", "what is plan 3"])
def test_not_small_talk(text: str) -> None:
    assert not is_small_talk(text)


# --------------------------------------------------------------------------- probing


class _Embedder(FakeEmbedder):
    """Every query embeds to ``vector``; counts the calls."""

    def __init__(self, vector: tuple[float, ...] = _AXIS) -> None:
        super().__init__()
        self.vector = vector
        self.calls = 0

    async def embed_query(self, query: str, *, tenant_id: UUID) -> tuple[float, ...]:
        self.calls += 1
        return self.vector


def _cos(value: float) -> tuple[float, ...]:
    return (value, (1 - value * value) ** 0.5) + (0.0,) * (EMBEDDING_DIM - 2)


async def _base(
    store: InMemoryKnowledgeStore,
    tenant: UUID,
    name: str,
    texts: Sequence[str],
    *,
    threshold: float | None = None,
    calibrated_model: str = "acme/embed-1",
) -> UUID:
    base = await store.create_base(
        tenant_id=tenant, name=name, embedding_provider="acme", embedding_model="embed-1"
    )
    document = await store.upsert_document(
        tenant_id=tenant, kb_id=base.id, filename="d.md", content=b"x"
    )
    chunks = [
        KnowledgeChunk(
            id=uuid4(),
            tenant_id=tenant,
            kb_id=base.id,
            document_id=document.id,
            chunk_index=index,
            content=text,
            embedding=_AXIS,
        )
        for index, text in enumerate(texts)
    ]
    await store.replace_chunks(tenant_id=tenant, document_id=document.id, chunks=chunks)
    await store.set_document_status(
        tenant_id=tenant, document_id=document.id, status=DocumentStatus.READY, chunk_count=1
    )
    if threshold is not None:
        await store.mark_profile_refresh(tenant_id=tenant, kb_id=base.id, now=_NOW)
        claims = await store.claim_profile_refreshes(now=_NOW + PROFILE_DEBOUNCE, limit=50)
        [claim] = [c for c in claims if c.kb_id == base.id]
        calibration = ProbeCalibration(
            embedding_model=calibrated_model,
            vector_threshold=threshold,
            queries=16,
            computed_at=_NOW,
        )
        await store.finish_profile_refresh(
            tenant_id=tenant,
            kb_id=base.id,
            holder=claim.holder,
            now=_NOW,
            marks=claim.marks,
            profile=None,
            calibration=calibration,
        )
    return base.id


async def _live() -> str | None:
    return "acme/embed-1"


def _probe(
    store: InMemoryKnowledgeStore,
    refs: tuple[str, ...],
    embedder: FakeEmbedder | None = None,
    live: Callable[[], Awaitable[str | None]] | None = _live,
) -> KnowledgeProbe:
    return KnowledgeProbe(
        retriever=KnowledgeRetriever(store=store, embedder=embedder or _Embedder()),
        refs=refs,
        live_model=live,
    )


def _turn(text: str) -> list[BaseMessage]:
    return [HumanMessage(content=text)]


async def test_a_keyword_hit_on_a_rare_term() -> None:
    store, tenant = InMemoryKnowledgeStore(), uuid4()
    await _base(store, tenant, "books", ["《大国食养》作者是李明", "节气饮食"])
    embedder = _Embedder()
    outcome = await _probe(store, ("books",), embedder).run(
        tenant_id=tenant, messages=_turn("大国食养谁写的")
    )
    assert outcome.reason == "hit"
    assert outcome.hits == (("K1", 1),)
    assert outcome.line == probe_line([("K1", 1)])
    assert embedder.calls == 0  # no calibration: keyword only, nothing embedded


async def test_one_shared_word_is_not_a_hit() -> None:
    store, tenant = InMemoryKnowledgeStore(), uuid4()
    await _base(store, tenant, "notes", ["今天的会议记录", "会议纪要模板"])
    outcome = await _probe(store, ("notes",)).run(
        tenant_id=tenant,
        messages=_turn("今天下雨吗"),  # shares only 今天
    )
    assert outcome.reason == "miss"
    assert outcome.line is None


async def test_an_identifier_alone_is_a_hit() -> None:
    store, tenant = InMemoryKnowledgeStore(), uuid4()
    await _base(store, tenant, "specs", ["型号 QXA-N7 的额定功率为 2kW"])
    outcome = await _probe(store, ("specs",)).run(tenant_id=tenant, messages=_turn("QXA-N7 多少钱"))
    assert outcome.hits == (("K1", 1),)


async def test_english_function_words_do_not_count() -> None:
    store, tenant = InMemoryKnowledgeStore(), uuid4()
    await _base(store, tenant, "docs", ["What is the time limit for this form"])
    miss = await _probe(store, ("docs",)).run(
        tenant_id=tenant, messages=_turn("what is the weather like")
    )
    hit = await _probe(store, ("docs",)).run(
        tenant_id=tenant, messages=_turn("time limit of the form")
    )
    assert (miss.reason, hit.reason) == ("miss", "hit")


def test_the_keyword_rule_constants() -> None:
    """Pinned so a change is a reviewed decision."""
    assert (MIN_SHARED_TERMS, MIN_TERM_CHARS) == (2, 2)


@pytest.mark.parametrize(("score", "hit"), [(0.9, True), (0.5, False)], ids=["above", "below"])
async def test_the_vector_side_uses_the_bases_own_calibration(score: float, hit: bool) -> None:
    store, tenant = InMemoryKnowledgeStore(), uuid4()
    await _base(store, tenant, "books", ["unrelated words only"], threshold=0.7)
    embedder = _Embedder(_cos(score))
    outcome = await _probe(store, ("books",), embedder).run(
        tenant_id=tenant, messages=_turn("zzz qqq vvv")
    )
    assert embedder.calls == 1
    assert (outcome.reason == "hit") is hit


async def test_a_calibration_under_another_model_is_not_used() -> None:
    store, tenant = InMemoryKnowledgeStore(), uuid4()
    await _base(
        store, tenant, "books", ["unrelated"], threshold=0.1, calibrated_model="acme/embed-0"
    )
    embedder = _Embedder(_cos(0.99))
    outcome = await _probe(store, ("books",), embedder).run(
        tenant_id=tenant, messages=_turn("zzz qqq vvv")
    )
    assert embedder.calls == 0
    assert outcome.reason == "miss"


async def _other() -> str | None:
    return "acme/embed-2"


@pytest.mark.parametrize("live", [_other, None], ids=["platform-switched", "unknown"])
async def test_the_query_model_must_be_the_calibrated_one(
    live: Callable[[], Awaitable[str | None]] | None,
) -> None:
    """The platform switched models and the base is not re-indexed yet (or the
    current model is unknown): a query vector now would not compare."""
    store, tenant = InMemoryKnowledgeStore(), uuid4()
    await _base(store, tenant, "books", ["unrelated"], threshold=0.1)
    embedder = _Embedder(_cos(0.99))
    outcome = await _probe(store, ("books",), embedder, live).run(
        tenant_id=tenant, messages=_turn("zzz qqq vvv")
    )
    assert embedder.calls == 0
    assert outcome.reason == "miss"


class _Counting(InMemoryKnowledgeStore):
    """Counts keyword searches and the most running at once."""

    def __init__(self) -> None:
        super().__init__()
        self.running = self.peak = self.calls = 0

    async def keyword_search_scored(self, **kwargs: Any) -> list[ScoredChunk]:
        self.calls += 1
        self.running += 1
        self.peak = max(self.peak, self.running)
        await asyncio.sleep(0.01)
        self.running -= 1
        return []


async def test_bases_are_probed_a_few_at_a_time_and_at_most_the_catalogs() -> None:
    store, tenant = _Counting(), uuid4()
    names = [f"kb{i}" for i in range(PROBE_MAX_BASES + 5)]
    for name in names:
        await store.create_base(tenant_id=tenant, name=name)
    outcome = await _probe(store, tuple(names)).run(
        tenant_id=tenant, messages=_turn("大国食养谁写的")
    )
    assert outcome.reason == "miss"
    assert store.calls == PROBE_MAX_BASES == 20
    assert store.peak == PROBE_CONCURRENCY == 4


async def test_thresholds_are_per_base() -> None:
    """The same score is related in one base and not in another."""
    store, tenant = InMemoryKnowledgeStore(), uuid4()
    await _base(store, tenant, "low", ["x"], threshold=0.5)
    await _base(store, tenant, "high", ["x"], threshold=0.95)
    outcome = await _probe(store, ("high", "low"), _Embedder(_cos(0.8))).run(
        tenant_id=tenant, messages=_turn("zzz qqq vvv")
    )
    assert outcome.hits == (("K2", 1),)


async def test_small_talk_is_not_probed() -> None:
    store, tenant = InMemoryKnowledgeStore(), uuid4()
    await _base(store, tenant, "books", ["你好 你好 你好"])
    outcome = await _probe(store, ("books",)).run(tenant_id=tenant, messages=_turn("你好"))
    assert outcome == ProbeOutcome(reason="small_talk")


async def test_small_talk_after_a_question_is_not_probed() -> None:
    """The follow-up context is never what makes a greeting probe-worthy."""
    store, tenant = InMemoryKnowledgeStore(), uuid4()
    await _base(store, tenant, "books", ["《大国食养》作者是李明"])
    messages = [
        HumanMessage(content="大国食养谁写的"),
        AIMessage(content="李明"),
        HumanMessage(content="谢谢"),
    ]
    outcome = await _probe(store, ("books",)).run(tenant_id=tenant, messages=messages)
    assert outcome == ProbeOutcome(reason="small_talk")


async def test_no_bound_base_exists() -> None:
    outcome = await _probe(InMemoryKnowledgeStore(), ("gone",)).run(
        tenant_id=uuid4(), messages=_turn("大国食养谁写的")
    )
    assert outcome.reason == "no_bases" and outcome.line is None


class _SlowStore(InMemoryKnowledgeStore):
    async def keyword_search_scored(self, **kwargs: Any) -> list[ScoredChunk]:
        await asyncio.sleep(5)
        return []


class _BrokenStore(InMemoryKnowledgeStore):
    async def keyword_search_scored(self, **kwargs: Any) -> list[ScoredChunk]:
        raise RuntimeError("down")


async def test_a_slow_probe_is_dropped_at_its_budget() -> None:
    store, tenant = _SlowStore(), uuid4()
    await _base(store, tenant, "books", ["大国食养"])
    probe = KnowledgeProbe(
        retriever=KnowledgeRetriever(store=store, embedder=_Embedder()),
        refs=("books",),
        timeout_s=0.05,
    )
    outcome = await probe.run(tenant_id=tenant, messages=_turn("大国食养谁写的"))
    assert outcome.reason == "timeout" and outcome.line is None


async def test_a_failing_probe_adds_nothing() -> None:
    store, tenant = _BrokenStore(), uuid4()
    await _base(store, tenant, "books", ["大国食养"])
    outcome = await _probe(store, ("books",)).run(
        tenant_id=tenant, messages=_turn("大国食养谁写的")
    )
    assert outcome.reason == "error" and outcome.line is None


def test_follow_up_is_whether_the_model_searched() -> None:
    hit = ProbeOutcome(reason="hit", line="x", hits=(("K1", 1),))
    assert record_follow_up(hit, _tool_turn("knowledge_search")) is True
    assert record_follow_up(hit, _tool_turn("web_search")) is False
    assert record_follow_up(hit, AIMessage(content="answer")) is False


# --------------------------------------------------------------------------- opt-out


_MANIFEST: dict[str, Any] = {
    "apiVersion": "expert_work.io/v1",
    "kind": "Agent",
    "metadata": {"name": "kb-agent", "version": "1.0.0", "tenant": "t"},
    "spec": {
        "tenant_config": {},
        "model": {"provider": "anthropic", "name": "claude-sonnet-4-6"},
        "system_prompt": {"template": "x"},
        "sandbox": {
            "resources": {"cpu": "1.0", "memory": "1Gi"},
            "network": {"egress": "none", "allowlist": []},
            "filesystem": {"readonly_root": True, "writable": []},
        },
    },
}


def _spec(knowledge: dict[str, Any] | None) -> AgentSpec:
    doc = deepcopy(_MANIFEST)
    if knowledge is not None:
        doc["spec"]["knowledge"] = knowledge
    return AgentSpec.model_validate(doc)


def _env(*, switch: bool) -> ToolEnv:
    return ToolEnv(
        knowledge_retriever=KnowledgeRetriever(
            store=InMemoryKnowledgeStore(), embedder=_Embedder()
        ),
        kb_first_routing=switch,
    )


def test_the_probe_is_on_by_default_with_the_switch() -> None:
    probe = _knowledge_probe(_spec({"knowledge_base_refs": ["a", "b"]}), _env(switch=True))
    assert probe is not None and probe.refs == ("a", "b")


@pytest.mark.parametrize(
    ("knowledge", "switch"),
    [
        ({"knowledge_base_refs": ["a"], "probe": False}, True),
        ({"knowledge_base_refs": ["a"]}, False),
        (None, True),
    ],
    ids=["opted-out", "switch-off", "no-knowledge"],
)
def test_no_probe(knowledge: dict[str, Any] | None, switch: bool) -> None:
    assert _knowledge_probe(_spec(knowledge), _env(switch=switch)) is None


def test_the_opt_out_is_validated() -> None:
    assert KnowledgeSpec(knowledge_base_refs=["a"]).probe is True
    with pytest.raises(ValidationError):
        KnowledgeSpec.model_validate({"knowledge_base_refs": ["a"], "probe": "sometimes"})
    with pytest.raises(ValidationError):
        KnowledgeSpec.model_validate({"knowledge_base_refs": ["a"], "probes": False})


# --------------------------------------------------------------------------- wiring


@dataclass
class _Echo:
    name: str = "echo"

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(name=self.name, description="echo", is_read_only=True)

    async def call(self, args: Mapping[str, Any], *, ctx: ToolContext) -> ToolResult:
        del args, ctx
        return ToolResult(content="ok")


async def _prompts(
    probe: KnowledgeProbe | None, tenant: UUID, text: str
) -> list[list[BaseMessage]]:
    registry = ToolRegistry()
    registry.register(_Echo())
    llm = _RecordingLLM(responses=[_tool_turn("echo")])
    config: RunnableConfig = {"configurable": {"thread_id": str(uuid4()), "tenant_id": str(tenant)}}
    async with _graph(llm, registry=registry, knowledge_probe=probe) as compiled:
        await compiled.ainvoke(
            {"messages": [HumanMessage(content=text)], "step_count": 0, "max_steps": 5},
            config=config,
        )
    return llm.seen_prompts


async def test_the_line_rides_the_first_steps_platform_section_only() -> None:
    store, tenant = InMemoryKnowledgeStore(), uuid4()
    await _base(store, tenant, "books", ["《大国食养》作者是李明"])
    first, second = await _prompts(_probe(store, ("books",)), tenant, "大国食养谁写的")
    line = probe_line([("K1", 1)])
    assert line is not None
    last = str(first[-1].content)
    assert last.startswith("大国食养谁写的\n\n<platform-context>")
    assert line in last
    assert all(line not in str(m.content) for m in second)


async def test_without_a_probe_the_prompt_is_unchanged() -> None:
    store, tenant = InMemoryKnowledgeStore(), uuid4()
    await _base(store, tenant, "books", ["无关内容"])
    with_miss = await _prompts(_probe(store, ("books",)), tenant, "大国食养谁写的")
    without = await _prompts(None, tenant, "大国食养谁写的")
    assert [[m.content for m in p] for p in with_miss] == [[m.content for m in p] for p in without]
