"""B-206 11-09 — multi-turn references in arguments that leave the conversation.

C1. ``# Tool arguments`` in the system prompt, only for a build with a tool that
    sends a query or arguments out of the conversation (knowledge_search,
    web_search, http, any MCP tool): such arguments are self-contained, the
    answer names what it took a reference to mean, and with two or more
    candidates of the same kind the agent asks or answers each by name.
C2. The ``query`` parameter of knowledge_search and web_search says the same
    in one line.
C3. A counter of outgoing queries that still carry a reference marker —
    labelled by tool name only (a heuristic signal; behaviour is unchanged).
"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

import pytest
from prometheus_client import REGISTRY

from expert_work.persistence import InMemoryKnowledgeStore
from expert_work.protocol import AgentSpec
from expert_work.runtime.checkpointer import make_checkpointer
from expert_work.runtime.secret_store import LocalDevSecretStore
from expert_work.runtime.tokens import default_estimator
from orchestrator import BuiltAgent, ToolContext, ToolEnv, ToolResult, ToolSpec, build_agent
from orchestrator.agent_factory import _assemble_system_prompt
from orchestrator.graph_builder.builder import _invoke_tool
from orchestrator.graph_builder.outgoing_reference import (
    OUTGOING_ARGUMENTS_CLAUSE,
    note_outgoing_query,
    query_has_reference,
    sends_arguments_out,
)
from orchestrator.llm import FakeEmbedder
from orchestrator.tools import KnowledgeRetriever
from orchestrator.tools.knowledge import KnowledgeSearchTool
from orchestrator.tools.web_search import WebSearchTool

_CLAUSE_MAX_TOKENS = 130
_METRIC = "expert_work_outgoing_query_reference_total"


# ---------------------------------------------------------------------------
# C1. the system prompt clause
# ---------------------------------------------------------------------------


def test_the_clause_is_appended_only_when_asked() -> None:
    with_clause = _assemble_system_prompt(base="BASE", skill_fragments=[], outgoing_arguments=True)
    without = _assemble_system_prompt(base="BASE", skill_fragments=[])
    assert with_clause.startswith("BASE") and OUTGOING_ARGUMENTS_CLAUSE in with_clause
    assert "# Tool arguments" not in without
    assert OUTGOING_ARGUMENTS_CLAUSE not in without


def test_the_clause_sits_before_the_date_so_the_daily_change_stays_after_it() -> None:
    prompt = _assemble_system_prompt(
        base="BASE", skill_fragments=[], current_date="DATELINE", outgoing_arguments=True
    )
    assert prompt.index("# Tool arguments") < prompt.index("# Current date")


def test_the_clause_is_generic_platform_text_within_budget() -> None:
    clause = OUTGOING_ARGUMENTS_CLAUSE
    assert clause.startswith("# Tool arguments\n")
    assert clause.isascii()
    assert default_estimator().count(clause) <= _CLAUSE_MAX_TOKENS
    lowered = clause.lower()
    assert "self-contained" in lowered
    assert "only the arguments" in lowered  # why: the receiver sees no conversation
    assert "pronoun" in lowered
    assert "name" in lowered and "answer" in lowered  # the answer names the referent
    assert "two or more" in lowered and "ask" in lowered  # ambiguity: ask or answer each
    # Generic: no domain words, no tool tied to a tenant scenario.
    for word in ("product", "customer", "patient", "drug", "price", "order"):
        assert word not in lowered, word


@pytest.mark.parametrize(
    ("names", "expected"),
    [
        (["knowledge_search", "knowledge_read"], True),
        (["web_search"], True),
        (["http"], True),
        (["mcp__crm__get_record"], True),
        (["read_file", "write_file", "update_plan", "exec_python"], False),
        ([], False),
    ],
)
def test_which_tools_send_arguments_out(names: list[str], expected: bool) -> None:
    assert sends_arguments_out(names) is expected


_KEY_NAME = "expert-work/dev/llm/anthropic"
_MANIFEST: dict[str, Any] = {
    "apiVersion": "expert_work.io/v1",
    "kind": "Agent",
    "metadata": {"name": "coref-agent", "version": "1.0.0", "tenant": "t"},
    "spec": {
        "tenant_config": {},
        "model": {"provider": "anthropic", "name": "claude-sonnet-4-6"},
        "system_prompt": {"template": "you answer questions"},
        "sandbox": {
            "resources": {"cpu": "1.0", "memory": "1Gi"},
            "network": {"egress": "none", "allowlist": []},
            "filesystem": {"readonly_root": True, "writable": []},
        },
    },
}


async def _key_resolver(provider: str) -> list[str]:
    del provider
    return [f"secret://{_KEY_NAME}"]


async def _build(knowledge: bool) -> BuiltAgent:
    doc = deepcopy(_MANIFEST)
    if knowledge:
        doc["spec"]["knowledge"] = {"knowledge_base_refs": ["kb"]}
    store = InMemoryKnowledgeStore()
    tenant = uuid4()
    await store.create_base(tenant_id=tenant, name="kb")
    env = ToolEnv(knowledge_retriever=KnowledgeRetriever(store=store, embedder=FakeEmbedder()))
    async with make_checkpointer("memory") as checkpointer:
        return await build_agent(
            AgentSpec.model_validate(doc),
            secret_store=LocalDevSecretStore.from_mapping({_KEY_NAME: "sk-ant-test"}),
            checkpointer=checkpointer,
            tool_env=env,
            tenant_id=tenant,
            provider_key_resolver=_key_resolver,
        )


async def test_a_knowledge_agent_gets_the_clause() -> None:
    built = await _build(knowledge=True)
    assert OUTGOING_ARGUMENTS_CLAUSE in built.system_prompt


async def test_an_agent_without_outgoing_tools_does_not() -> None:
    built = await _build(knowledge=False)
    assert not sends_arguments_out(e.name for e in built.tool_catalog)
    assert "# Tool arguments" not in built.system_prompt


# ---------------------------------------------------------------------------
# C2. the query parameter descriptions
# ---------------------------------------------------------------------------


def _query_description(spec: ToolSpec) -> str:
    return str(spec.parameters["properties"]["query"]["description"])


@dataclass
class _NoTavily:
    async def search(self, **kwargs: Any) -> Mapping[str, Any]:
        del kwargs
        return {"results": []}


def test_both_query_parameters_ask_for_a_self_contained_query() -> None:
    retriever = KnowledgeRetriever(store=InMemoryKnowledgeStore(), embedder=FakeEmbedder())
    specs = [
        KnowledgeSearchTool(retriever=retriever, knowledge_base_refs=("kb",)).spec,
        WebSearchTool(client=_NoTavily()).spec,
    ]
    for spec in specs:
        description = _query_description(spec)
        assert "self-contained" in description, spec.name
        assert "pronoun" in description, spec.name
        assert "\n" not in description and default_estimator().count(description) <= 40


# ---------------------------------------------------------------------------
# C3. the reference-marker counter
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "query",
    [
        "它的保质期多久",
        "这款适合孕妇吗",
        "上面第二个的价格",
        "那他们的地址呢",
        "该产品的适用人群",
        "is it safe for children",
        "what about the other one",
        "their opening hours",
    ],
)
def test_a_query_with_a_reference_marker_is_flagged(query: str) -> None:
    assert query_has_reference(query)


@pytest.mark.parametrize(
    "query",
    [
        "维生素D3 软胶囊 保质期",
        "其他门店营业时间",
        "这周的值班安排",
        "应该如何保存 维生素C",
        "60岁以上人群 用量",
        "opening hours of the Shanghai store",
        "items that contain gluten",
    ],
)
def test_a_self_contained_query_is_not_flagged(query: str) -> None:
    assert not query_has_reference(query)


def _count(tool: str) -> float:
    return REGISTRY.get_sample_value(_METRIC, {"tool": tool}) or 0.0


def test_the_counter_counts_flagged_queries_of_query_tools_only() -> None:
    before = {name: _count(name) for name in ("knowledge_search", "web_search", "http")}

    note_outgoing_query("knowledge_search", {"query": "它的保质期"})
    note_outgoing_query("knowledge_search", {"query": "维生素D3 保质期"})
    note_outgoing_query("web_search", {"query": "is it open on Sunday"})
    note_outgoing_query("http", {"query": "它的保质期"})
    note_outgoing_query("knowledge_search", {"query": 3})
    note_outgoing_query("knowledge_search", {})

    assert _count("knowledge_search") == before["knowledge_search"] + 1
    assert _count("web_search") == before["web_search"] + 1
    assert _count("http") == before["http"]


@dataclass
class _QueryTool:
    name: str = "knowledge_search"
    calls: list[Mapping[str, Any]] = field(default_factory=list)

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name=self.name,
            description="q",
            parameters={
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
                "additionalProperties": False,
            },
            is_read_only=True,
        )

    async def call(self, args: Mapping[str, Any], *, ctx: ToolContext) -> ToolResult:
        del ctx
        self.calls.append(args)
        return ToolResult(content="ok")


async def test_dispatch_counts_an_outgoing_query_and_still_runs_it() -> None:
    tool = _QueryTool()
    before = _count("knowledge_search")

    msg, *_ = await _invoke_tool(
        tool, {"query": "这款适合孕妇吗"}, "tc-1", ToolContext(tenant_id=uuid4())
    )

    assert msg.status != "error"
    assert tool.calls == [{"query": "这款适合孕妇吗"}]  # behaviour unchanged
    assert _count("knowledge_search") == before + 1


async def test_dispatch_does_not_count_rejected_arguments() -> None:
    tool = _QueryTool()
    before = _count("knowledge_search")
    msg, *_ = await _invoke_tool(
        tool, {"query": "它的保质期", "extra": 1}, "tc-1", ToolContext(tenant_id=uuid4())
    )
    assert msg.status == "error" and not tool.calls
    assert _count("knowledge_search") == before
