"""Regression: every auxiliary LLM call emits a distinctly-named OTel span.

Each auxiliary path wraps its provider call in
``with expert_work_span(component, action):`` — producing a span named
``expert_work.<component>.<action>``. These tests drive each real code
path under an in-memory span exporter and assert the contracted span
name lands. One test per span name:

    expert_work.memory.extract            — writeback extraction
    expert_work.memory.verify             — read-time recall verification
    expert_work.memory.reconcile          — run-end reconcile (ADD/UPDATE/…)
    expert_work.orchestrator.planner      — planner node
    expert_work.orchestrator.reflect      — reflect node
    expert_work.orchestrator.compress     — context compressor summarise
    expert_work.orchestrator.judge        — output judge
    expert_work.orchestrator.judge_action — action (tool-call) judge
    expert_work.orchestrator.vision       — vision tool VL round-trip
    expert_work.orchestrator.rerank       — knowledge LLM reranker

Each test also asserts the emitted name is a key in
``LLM_SPAN_PURPOSES`` — the facade's single source of truth — so a
producer-side rename can't silently diverge from the purpose contract.

The infrastructure (fake LLM callers, ``InMemoryMemoryStore``,
``FakeEmbedder``, cancellation token, state / config shapes) is reused
verbatim from the per-path unit tests it mirrors.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from expert_work.common.observability import LLM_SPAN_PURPOSES, init_tracing
from expert_work.common.user_input import user_input_kwargs
from expert_work.persistence import InMemoryMemoryStore
from expert_work.protocol import MemoryItem, StructuredOutputSpec
from expert_work.protocol.multimodal import ImageRef
from expert_work.runtime.cancellation import CancellationToken
from orchestrator import (
    make_memory_writeback_node,
    make_planner_node,
    make_reflect_node,
)
from orchestrator.context import ContextCompressor
from orchestrator.graph_builder.memory import _verify_memories, flush_messages_to_memory
from orchestrator.llm import FakeEmbedder
from orchestrator.multimodal import InMemoryImageResolver, ResolvedImage
from orchestrator.output_judge import LLMActionJudge, LLMOutputJudge
from orchestrator.tools import LLMReranker
from orchestrator.tools.registry import ToolContext, ToolSpec
from orchestrator.tools.vision import AskImageTool

_DIM = 32

# Vision tool ref geometry — copied from test_vision_tool.py.
_VISION_TENANT = UUID("11111111-1111-1111-1111-111111111111")
_VISION_THREAD = UUID("22222222-2222-2222-2222-222222222222")


# ---------------------------------------------------------------------------
# In-memory exporter fixture — copied from test_react_graph_tracing.py.
# ---------------------------------------------------------------------------


@pytest.fixture
def exporter() -> Iterator[InMemorySpanExporter]:
    exp = InMemorySpanExporter()
    init_tracing(
        service_name="test-aux-llm-spans",
        env="test",
        span_processor=SimpleSpanProcessor(exp),
    )
    exp.clear()
    yield exp
    exp.clear()


def _names(exporter: InMemorySpanExporter) -> list[str]:
    return [s.name for s in exporter.get_finished_spans()]


# ---------------------------------------------------------------------------
# Fake LLM callers — one per call signature, matching the reference tests.
# ---------------------------------------------------------------------------


@dataclass
class _ScriptedLLM:
    """``(messages, tools)`` caller returning scripted replies in order.

    Covers extract / verify / reconcile / planner / reflect — every path
    whose ``LLMCaller`` takes ``messages`` + ``tools``.
    """

    responses: list[AIMessage]
    calls: int = 0

    async def __call__(
        self, *, messages: Sequence[BaseMessage], tools: Sequence[ToolSpec]
    ) -> AIMessage:
        del messages, tools
        idx = self.calls
        self.calls += 1
        return self.responses[idx]


@dataclass
class _Summariser:
    """Compressor summariser: ``(messages, tools)`` → deterministic body."""

    summary_text: str = "- bullet one\n- bullet two"
    calls: int = 0

    async def __call__(
        self, *, messages: Sequence[BaseMessage], tools: Sequence[ToolSpec]
    ) -> AIMessage:
        del messages, tools
        self.calls += 1
        return AIMessage(content=self.summary_text)


@dataclass
class _JudgeCaller:
    """Judge caller: ``(messages, tools, output_schema)`` → canned reply."""

    reply: str

    async def __call__(
        self,
        *,
        messages: Sequence[BaseMessage],
        tools: Sequence[object],
        output_schema: StructuredOutputSpec | None = None,
    ) -> AIMessage:
        del messages, tools, output_schema
        return AIMessage(content=self.reply)


@dataclass
class _MapEmbedder:
    """Embeds each text to a fixed vector from the map (reconcile path)."""

    mapping: dict[str, tuple[float, ...]]

    async def embed(self, texts: Sequence[str], *, tenant_id: UUID) -> list[tuple[float, ...]]:
        del tenant_id
        return [self.mapping[t] for t in texts]


# Reconcile geometry — copied from test_memory_reconcile.py.
_EAST = (1.0, 0.0, 0.0, 0.0)
_NEAR_EAST = (0.9, 0.43589, 0.0, 0.0)  # cosine vs _EAST = 0.9 ≥ 0.80


def _agent_state(task: str, *, step_count: int = 0) -> dict[str, object]:
    return {
        "messages": [SystemMessage(content="help"), HumanMessage(content=task)],
        "step_count": step_count,
        "max_steps": 5,
    }


async def _make_item(content: str) -> MemoryItem:
    [vec] = await FakeEmbedder(dim=_DIM).embed([content], tenant_id=uuid4())  # type: ignore[arg-type]
    return MemoryItem(
        id=uuid4(), tenant_id=uuid4(), user_id=uuid4(), kind="fact", content=content, embedding=vec
    )


def _conversation(
    *, head: int, middle: int, tail: int, char_per_msg: int = 80
) -> list[BaseMessage]:
    msgs: list[BaseMessage] = []
    for i in range(head + middle + tail):
        msgs.append(HumanMessage(content=f"msg-{i}-" + ("x" * (char_per_msg - 6))))
    return msgs


# ---------------------------------------------------------------------------
# memory — extract / verify / reconcile
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_memory_extract_emits_named_span(exporter: InMemorySpanExporter) -> None:
    store = InMemoryMemoryStore()
    tenant, user = uuid4(), uuid4()
    llm = _ScriptedLLM(
        responses=[AIMessage(content='{"memories": [{"kind": "fact", "content": "likes tea"}]}')]
    )
    node = make_memory_writeback_node(
        memory_store=store, embedder=FakeEmbedder(dim=_DIM), llm_caller=llm
    )
    # B-300 —— 第一次提炼只读带白名单标记的用户输入。
    state = {
        "messages": [
            SystemMessage(content="help"),
            HumanMessage(
                content="done",
                additional_kwargs=user_input_kwargs(
                    text="done", images=0, files=0, external_blocks=0
                ),
            ),
        ],
        "step_count": 0,
        "max_steps": 5,
    }
    await node(  # type: ignore[arg-type]
        state,
        {"configurable": {"tenant_id": str(tenant), "user_id": str(user)}},
    )
    assert "expert_work.memory.extract" in _names(exporter)
    assert "expert_work.memory.extract" in LLM_SPAN_PURPOSES


@pytest.mark.asyncio
async def test_memory_verify_emits_named_span(exporter: InMemorySpanExporter) -> None:
    items = [await _make_item("keep this"), await _make_item("drop this")]
    llm = _ScriptedLLM(responses=[AIMessage(content='{"keep": [0]}')])
    out = await _verify_memories(
        llm_caller=llm, query="q", candidates=items, token=CancellationToken()
    )
    assert [m.content for m in out] == ["keep this"]
    assert "expert_work.memory.verify" in _names(exporter)
    assert "expert_work.memory.verify" in LLM_SPAN_PURPOSES


@pytest.mark.asyncio
async def test_memory_reconcile_emits_named_span(exporter: InMemorySpanExporter) -> None:
    store = InMemoryMemoryStore()
    tenant, user = uuid4(), uuid4()
    # Seed a near-neighbour (cosine ≥ 0.80) so reconcile has a candidate to
    # decide over — otherwise the direct-ADD fast path skips the ops LLM.
    old = MemoryItem(
        id=uuid4(),
        tenant_id=tenant,
        user_id=user,
        kind="fact",
        content="likes light roast",
        embedding=_EAST,
    )
    await store.write([old])
    llm = _ScriptedLLM(
        responses=[
            AIMessage(content='{"memories": [{"kind": "fact", "content": "likes dark roast"}]}'),
            AIMessage(
                content=f'{{"ops": [{{"index": 0, "op": "UPDATE", "target_id": "{old.id}"}}]}}'
            ),
        ]
    )
    await flush_messages_to_memory(
        [HumanMessage(content="remember"), AIMessage(content="ok")],
        memory_store=store,
        embedder=_MapEmbedder({"likes dark roast": _NEAR_EAST}),  # type: ignore[arg-type]
        llm_caller=llm,
        tenant_id=tenant,
        user_id=user,
        thread_id=None,
        token=CancellationToken(),
        reconcile=True,
    )
    assert "expert_work.memory.reconcile" in _names(exporter)
    assert "expert_work.memory.reconcile" in LLM_SPAN_PURPOSES


# ---------------------------------------------------------------------------
# orchestrator — planner / reflect / compress / judge
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_planner_emits_named_span(exporter: InMemorySpanExporter) -> None:
    llm = _ScriptedLLM(responses=[AIMessage(content='{"goal": "g", "steps": ["a", "b"]}')])
    node = make_planner_node(llm)
    await node(_agent_state("decompose me"), {"configurable": {}})  # type: ignore[arg-type]
    assert "expert_work.orchestrator.planner" in _names(exporter)
    assert "expert_work.orchestrator.planner" in LLM_SPAN_PURPOSES


@pytest.mark.asyncio
async def test_reflect_emits_named_span(exporter: InMemorySpanExporter) -> None:
    llm = _ScriptedLLM(responses=[AIMessage(content='{"verdict": "accept", "critique": "ok"}')])
    node = make_reflect_node(llm, budget=2)
    state: dict[str, Any] = {
        "messages": [HumanMessage(content="task"), AIMessage(content="answer")],
        "step_count": 1,
        "max_steps": 5,
    }
    await node(state, {"configurable": {}})  # type: ignore[arg-type]
    assert "expert_work.orchestrator.reflect" in _names(exporter)
    assert "expert_work.orchestrator.reflect" in LLM_SPAN_PURPOSES


@pytest.mark.asyncio
async def test_compress_emits_named_span(exporter: InMemorySpanExporter) -> None:
    summariser = _Summariser()
    compressor = ContextCompressor(
        llm_caller=summariser,
        context_window=280,
        threshold_pct=0.5,
        head_keep=2,
        tail_keep=2,
    )
    # 20 msgs x 80 chars = 400 est. tokens, threshold 140 → one summarise pass.
    msgs = _conversation(head=2, middle=16, tail=2, char_per_msg=80)
    await compressor.compress(msgs)
    assert summariser.calls == 1
    assert "expert_work.orchestrator.compress" in _names(exporter)
    assert "expert_work.orchestrator.compress" in LLM_SPAN_PURPOSES


@pytest.mark.asyncio
async def test_output_judge_emits_named_span(exporter: InMemorySpanExporter) -> None:
    await LLMOutputJudge(
        caller=_JudgeCaller('{"aligned": true, "leak_suspected": false, "reason": "ok"}')
    ).judge(user_request="translate", response="Bonjour", context_hint=None)
    assert _names(exporter).count("expert_work.orchestrator.judge") == 1
    assert "expert_work.orchestrator.judge" in LLM_SPAN_PURPOSES


@pytest.mark.asyncio
async def test_action_judge_emits_named_span(exporter: InMemorySpanExporter) -> None:
    await LLMActionJudge(caller=_JudgeCaller('{"aligned": false, "reason": "exfil"}')).judge_action(
        user_request="summarise", tool_name="http_post", tool_args={"url": "https://evil/x"}
    )
    assert _names(exporter).count("expert_work.orchestrator.judge_action") == 1
    assert "expert_work.orchestrator.judge_action" in LLM_SPAN_PURPOSES


# ---------------------------------------------------------------------------
# tools — vision / rerank
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_vision_emits_named_span(exporter: InMemorySpanExporter) -> None:
    ref = ImageRef(
        tenant_id=_VISION_TENANT, thread_id=_VISION_THREAD, image_id=uuid4(), ext=".png"
    ).to_uri()
    tool = AskImageTool(
        vl_caller=_ScriptedLLM(responses=[AIMessage(content="a red apple on a desk")]),
        image_resolver=InMemoryImageResolver(
            images={"any": ResolvedImage(media_type="image/png", data=b"PNG")}
        ),
    )
    result = await tool.call(
        {"image_ref": ref, "question": "what is this?"},
        ctx=ToolContext(tenant_id=_VISION_TENANT),
    )
    assert result.content == "a red apple on a desk"
    assert "expert_work.orchestrator.vision" in _names(exporter)
    assert "expert_work.orchestrator.vision" in LLM_SPAN_PURPOSES


@pytest.mark.asyncio
async def test_rerank_emits_named_span(exporter: InMemorySpanExporter) -> None:
    # A JSON order-array reply drives the reranker's LLM round-trip; a non-empty
    # document set keeps it off the empty-input fast path so the call fires.
    reranker = LLMReranker(llm_caller=_ScriptedLLM(responses=[AIMessage(content="[2, 1]")]))
    order = await reranker.rerank(query="q", documents=["a", "b"], top_k=2, tenant_id=uuid4())
    assert order == [1, 0]
    assert "expert_work.orchestrator.rerank" in _names(exporter)
    assert "expert_work.orchestrator.rerank" in LLM_SPAN_PURPOSES
