"""Stream CM-3 — pre-compaction flush wiring (graph-level).

Drives the flush through the compiled graph: when ``agent_node``'s
compressor preflight fires, the bound ``pre_compaction_flush`` extracts the
about-to-be-discarded middle into long-term memory before the summariser
runs. A graph without the callback compresses but flushes nothing.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig

from expert_work.persistence import InMemoryMemoryStore
from expert_work.runtime.checkpointer import make_checkpointer
from orchestrator import GraphRunner, ToolRegistry, build_react_graph
from orchestrator.context import ContextCompressor
from orchestrator.graph_builder import make_pre_compaction_flush
from orchestrator.llm import FakeEmbedder
from orchestrator.tools.registry import ToolSpec

_DIM = 32


@dataclass
class _FixedLLM:
    """Replies with one fixed message and counts calls."""

    reply: AIMessage
    calls: int = 0

    async def __call__(
        self, *, messages: Sequence[BaseMessage], tools: Sequence[ToolSpec]
    ) -> AIMessage:
        del messages, tools
        self.calls += 1
        return self.reply


def _long_history() -> list[BaseMessage]:
    msgs: list[BaseMessage] = [SystemMessage(content="sys")]
    for i in range(12):
        msgs.append(HumanMessage(content=f"user-{i} " + "w" * 400))
        msgs.append(AIMessage(content=f"assistant-{i} " + "w" * 400))
    return msgs


def _compressor() -> ContextCompressor:
    # Small window so the long history is over threshold and compaction fires.
    return ContextCompressor(
        llm_caller=_FixedLLM(reply=AIMessage(content="- summary bullet")),
        context_window=1000,
        threshold_pct=0.7,
        head_keep=2,
        tail_keep=2,
        max_passes=3,
    )


async def _run(graph: object, *, tenant: object, user: object) -> None:
    async with make_checkpointer("memory") as cp:
        compiled = GraphRunner(checkpointer=cp).compile(graph)  # type: ignore[arg-type]
        cfg: RunnableConfig = {
            "configurable": {
                "thread_id": str(uuid4()),
                "tenant_id": str(tenant),
                "user_id": str(user),
            }
        }
        await compiled.ainvoke(
            {"messages": _long_history(), "step_count": 0, "max_steps": 5},
            config=cfg,
        )


@pytest.mark.asyncio
async def test_compaction_flushes_middle_to_memory() -> None:
    store = InMemoryMemoryStore()
    tenant, user = uuid4(), uuid4()
    embedder = FakeEmbedder(dim=_DIM)
    # Separate LLM for the flush extraction so its JSON reply is unambiguous.
    flush_llm = _FixedLLM(
        reply=AIMessage(content='{"memories": [{"kind": "fact", "content": "decided to use RRF"}]}')
    )
    agent_llm = _FixedLLM(reply=AIMessage(content="done"))
    graph = build_react_graph(
        llm_caller=agent_llm,
        tool_registry=ToolRegistry(),
        context_compressor=_compressor(),
        pre_compaction_flush=make_pre_compaction_flush(
            memory_store=store, embedder=embedder, llm_caller=flush_llm
        ),
    )

    await _run(graph, tenant=tenant, user=user)

    # The discarded middle was flushed to long-term memory before the summary.
    assert flush_llm.calls == 1
    stored = await store.retrieve(
        tenant_id=tenant, user_id=user, query_embedding=(0.0,) * _DIM, limit=10
    )
    assert [m.content for m in stored] == ["decided to use RRF"]


@pytest.mark.asyncio
async def test_compaction_without_flush_persists_nothing() -> None:
    store = InMemoryMemoryStore()
    tenant, user = uuid4(), uuid4()
    agent_llm = _FixedLLM(reply=AIMessage(content="done"))
    graph = build_react_graph(
        llm_caller=agent_llm,
        tool_registry=ToolRegistry(),
        context_compressor=_compressor(),
        # pre_compaction_flush=None → compaction still happens, no flush.
    )

    await _run(graph, tenant=tenant, user=user)

    stored = await store.retrieve(
        tenant_id=tenant, user_id=user, query_embedding=(0.0,) * _DIM, limit=10
    )
    assert stored == []


async def _run_with_sink(graph: object, *, sink: object, messages: list[BaseMessage]) -> None:
    from orchestrator.graph_builder._config import COMPACTION_SINK_KEY

    async with make_checkpointer("memory") as cp:
        compiled = GraphRunner(checkpointer=cp).compile(graph)  # type: ignore[arg-type]
        cfg: RunnableConfig = {
            "configurable": {"thread_id": str(uuid4()), COMPACTION_SINK_KEY: sink}
        }
        await compiled.ainvoke({"messages": messages, "step_count": 0, "max_steps": 5}, config=cfg)


@pytest.mark.asyncio
async def test_compaction_fires_event_sink_from_config() -> None:
    """RT-2 PR-4 — agent_node reads the COMPACTION sink from config and fires
    it once when the compressor produces a summary, with the pass count +
    before/after token estimates + summary length."""
    seen: list[dict[str, Any]] = []

    async def _sink(payload: dict[str, Any]) -> None:
        seen.append(payload)

    graph = build_react_graph(
        llm_caller=_FixedLLM(reply=AIMessage(content="done")),
        tool_registry=ToolRegistry(),
        context_compressor=_compressor(),
    )

    await _run_with_sink(graph, sink=_sink, messages=_long_history())

    assert len(seen) == 1
    payload = seen[0]
    assert payload["passes"] >= 1
    assert payload["tokens_before"] > payload["tokens_after"]
    assert payload["summary_chars"] > 0
    # B-141 —— 摘要模型实际读到的字符数也随事件发出
    assert payload["summary_input_chars"] > 0


@pytest.mark.asyncio
async def test_no_compaction_does_not_fire_event_sink() -> None:
    """A prompt that stays under threshold never compresses, so the sink is
    never called."""
    seen: list[dict[str, Any]] = []

    async def _sink(payload: dict[str, Any]) -> None:
        seen.append(payload)

    graph = build_react_graph(
        llm_caller=_FixedLLM(reply=AIMessage(content="done")),
        tool_registry=ToolRegistry(),
        context_compressor=_compressor(),
    )

    await _run_with_sink(graph, sink=_sink, messages=[HumanMessage(content="hi")])

    assert seen == []
