"""入口链 span 契约测试 —— 一期 Task 1。

``TRACED_SPANS`` 是非 LLM 追踪 span 名的单源。这里断言每个名字都真的
被发射(对着 InMemorySpanExporter),以及集合本身没有漏项。姊妹契约
``LLM_SPAN_PURPOSES`` 由 ``test_aux_llm_spans.py`` 守。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from expert_work.common.observability import TRACED_SPANS, init_tracing
from expert_work.persistence import InMemoryMemoryStore
from expert_work.protocol import MemoryItem, Plan, PlanStep
from expert_work.runtime.checkpointer import make_checkpointer
from orchestrator import (
    GraphRunner,
    ToolRegistry,
    build_react_graph,
    make_memory_recall_node,
)
from orchestrator.context import render_plan_md
from orchestrator.graph_builder import make_workspace_ingest_node
from orchestrator.llm import FakeEmbedder
from orchestrator.tools.registry import ToolSpec
from orchestrator.tools.sandbox import RecordingSupervisorClient, SandboxOutcome

_DIM = 32

EXPECTED = {
    "expert_work.memory.recall",
    "expert_work.memory.resolve_mode",
    "expert_work.memory.embed",
    "expert_work.memory.retrieve",
    "expert_work.memory.rerank",
    "expert_work.memory.bump_access",
    "expert_work.orchestrator.workspace_ingest",
    "expert_work.orchestrator.context_gates",
}


def test_traced_spans_covers_every_entry_chain_span() -> None:
    assert TRACED_SPANS == EXPECTED


# ---------------------------------------------------------------------------
# In-memory exporter fixture — copied from test_aux_llm_spans.py.
# ---------------------------------------------------------------------------


@pytest.fixture
def exporter() -> Iterator[InMemorySpanExporter]:
    exp = InMemorySpanExporter()
    init_tracing(
        service_name="test-entry-chain-spans",
        env="test",
        span_processor=SimpleSpanProcessor(exp),
    )
    exp.clear()
    yield exp
    exp.clear()


def _state(task: str) -> dict[str, object]:
    return {
        "messages": [SystemMessage(content="help"), HumanMessage(content=task)],
        "step_count": 0,
        "max_steps": 5,
    }


async def _seed(store: InMemoryMemoryStore, *, tenant: object, user: object, content: str) -> None:
    [vec] = await FakeEmbedder(dim=_DIM).embed([content], tenant_id=tenant)  # type: ignore[arg-type]
    await store.write(
        [
            MemoryItem(
                id=uuid4(),
                tenant_id=tenant,  # type: ignore[arg-type]
                user_id=user,  # type: ignore[arg-type]
                kind="fact",
                content=content,
                embedding=vec,
            )
        ]
    )


# ---------------------------------------------------------------------------
# memory_recall — parent span + its children
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_memory_recall_emits_the_full_entry_chain(exporter: InMemorySpanExporter) -> None:
    """一次命中的召回应发射 recall 父 span + resolve_mode/embed/retrieve/
    bump_access 四个子 span。rerank / verify 依配置可选,这里不装配它们。"""
    store = InMemoryMemoryStore()
    tenant, user = uuid4(), uuid4()
    await _seed(store, tenant=tenant, user=user, content="user prefers metric units")
    node = make_memory_recall_node(memory_store=store, embedder=FakeEmbedder(dim=_DIM), top_k=5)

    await node(  # type: ignore[arg-type]
        _state("what's the distance"),
        {"configurable": {"tenant_id": str(tenant), "user_id": str(user)}},
    )

    # 二期 P1.1 —— bump_access 是 fire-and-forget 后台任务,span 在任务内
    # 打;读 finished spans 前先 drain。
    from orchestrator.graph_builder.memory import _BACKGROUND_BUMP_TASKS

    await asyncio.gather(*list(_BACKGROUND_BUMP_TASKS))

    names = {s.name for s in exporter.get_finished_spans()}
    assert "expert_work.memory.recall" in names
    assert "expert_work.memory.resolve_mode" in names
    assert "expert_work.memory.embed" in names
    assert "expert_work.memory.retrieve" in names
    assert "expert_work.memory.bump_access" in names


@pytest.mark.asyncio
async def test_recall_children_nest_under_the_recall_span(
    exporter: InMemorySpanExporter,
) -> None:
    """子 span 必须真的挂在 recall 父 span 下 —— 只断言名字出现的话,把 8 个
    span 全平铺发射也能过测,但瀑布图上会是 8 条并列的根,读不出层次。"""
    store = InMemoryMemoryStore()
    tenant, user = uuid4(), uuid4()
    await _seed(store, tenant=tenant, user=user, content="user prefers metric units")
    node = make_memory_recall_node(memory_store=store, embedder=FakeEmbedder(dim=_DIM), top_k=5)

    await node(  # type: ignore[arg-type]
        _state("what's the distance"),
        {"configurable": {"tenant_id": str(tenant), "user_id": str(user)}},
    )

    # 二期 P1.1 —— 同上,drain 后台 bump_access 任务再读 spans。
    from orchestrator.graph_builder.memory import _BACKGROUND_BUMP_TASKS

    await asyncio.gather(*list(_BACKGROUND_BUMP_TASKS))

    spans = {s.name: s for s in exporter.get_finished_spans()}
    parent = spans["expert_work.memory.recall"]
    for child_name in (
        "expert_work.memory.resolve_mode",
        "expert_work.memory.embed",
        "expert_work.memory.retrieve",
        "expert_work.memory.bump_access",
    ):
        child = spans[child_name]
        assert child.parent is not None, f"{child_name} has no parent span"
        assert child.parent.span_id == parent.context.span_id, (
            f"{child_name} is not nested under expert_work.memory.recall"
        )


# ---------------------------------------------------------------------------
# workspace_ingest — standalone entry-chain node
# ---------------------------------------------------------------------------


def _plan() -> Plan:
    return Plan(
        goal="ship the feature",
        steps=(PlanStep(id="1", description="write tests", status="completed"),),
    )


def _read_envelope(content: str) -> SandboxOutcome:
    return SandboxOutcome(
        stdout=json.dumps(
            {"ok": True, "content": content, "content_hash": "h", "size": len(content)}
        ),
        stderr="",
        exit_code=0,
        timed_out=False,
    )


@pytest.mark.asyncio
async def test_workspace_ingest_emits_named_span(exporter: InMemorySpanExporter) -> None:
    plan = _plan()
    # A genuine edit (goal line changed) so the ingester actually applies it —
    # the span should fire either way, but this also exercises the real path.
    edited = render_plan_md(plan).replace("ship the feature", "ship the feature now")
    client = RecordingSupervisorClient(outcome=_read_envelope(edited))
    node = make_workspace_ingest_node(client=client)

    await node(  # type: ignore[arg-type]
        {"messages": [HumanMessage(content="go")], "step_count": 0, "max_steps": 5, "plan": plan},
        {
            "configurable": {
                "tenant_id": str(uuid4()),
                "user_id": str(uuid4()),
                "run_id": str(uuid4()),
            }
        },
    )

    names = {s.name for s in exporter.get_finished_spans()}
    assert "expert_work.orchestrator.workspace_ingest" in names


# ---------------------------------------------------------------------------
# context_gates — inline in agent_node, driven through the compiled graph
# ---------------------------------------------------------------------------


@dataclass
class _NoToolCallLLM:
    """Replies with no tool calls so the graph ends after one agent step."""

    async def __call__(
        self, *, messages: Sequence[BaseMessage], tools: Sequence[ToolSpec]
    ) -> AIMessage:
        del messages, tools
        return AIMessage(content="done")


@pytest.mark.asyncio
async def test_context_gates_emits_named_span(exporter: InMemorySpanExporter) -> None:
    """``context_gates`` wraps the prune/window checks unconditionally — it
    should fire even with neither gate configured (both are no-ops)."""
    async with make_checkpointer("memory") as cp:
        compiled = GraphRunner(checkpointer=cp).compile(
            build_react_graph(llm_caller=_NoToolCallLLM(), tool_registry=ToolRegistry())
        )
        await compiled.ainvoke(
            {"messages": [HumanMessage(content="hi")], "step_count": 0, "max_steps": 5},
            config={"configurable": {"thread_id": str(uuid4())}},
        )

    names = {s.name for s in exporter.get_finished_spans()}
    assert "expert_work.orchestrator.context_gates" in names
