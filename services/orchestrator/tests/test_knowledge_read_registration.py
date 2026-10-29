"""B-206 K-2 — ``knowledge_read`` comes with a ``knowledge:`` block, never without one.

It is registered beside ``knowledge_search``, over the same bases and the same
store, so an agent with no knowledge base has neither the tool nor any text
about it; a dynamic worker (``worker_policy``) keeps it — it only reads.
"""

from __future__ import annotations

import pytest

from expert_work.persistence import InMemoryKnowledgeStore
from expert_work.protocol import BuiltinToolSpec, KnowledgeSpec
from orchestrator.llm import FakeEmbedder
from orchestrator.tools import (
    KnowledgeReadTool,
    KnowledgeRetriever,
    ToolEnv,
    build_tool_registry,
)


def _env(*, worker_policy: bool = False) -> ToolEnv:
    retriever = KnowledgeRetriever(store=InMemoryKnowledgeStore(), embedder=FakeEmbedder())
    return ToolEnv(knowledge_retriever=retriever, worker_policy=worker_policy)


@pytest.mark.parametrize("worker_policy", [False, True])
async def test_a_knowledge_block_brings_knowledge_read_over_its_bases(worker_policy: bool) -> None:
    env = _env(worker_policy=worker_policy)
    registry = await build_tool_registry(
        [], tool_env=env, knowledge=KnowledgeSpec(knowledge_base_refs=["hr", "eng"])
    )

    tool = registry.get("knowledge_read")
    assert isinstance(tool, KnowledgeReadTool)
    assert tool.knowledge_base_refs == ("hr", "eng")
    assert env.knowledge_retriever is not None
    assert tool.store is env.knowledge_retriever.store
    assert "knowledge_read" in {entry.name for entry in registry.catalog()}


async def test_without_a_knowledge_block_there_is_no_knowledge_read_nor_text_about_it() -> None:
    tools = [BuiltinToolSpec(name="ask_for_approval")]
    registry = await build_tool_registry(tools, tool_env=_env())

    assert registry.get("knowledge_read") is None
    assert "knowledge_read" not in {entry.name for entry in registry.catalog()}
    specs = registry.all_specs()
    assert specs  # the agent has tools, so the check below is not vacuous
    assert all("knowledge_read" not in spec.description for spec in specs)
