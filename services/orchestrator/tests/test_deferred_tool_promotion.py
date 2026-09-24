"""End-to-end: deferred tool promotion via ``find_tools`` — Stream TE-6.

Verifies the full tool-RAG loop through the real ReAct graph:

1. A tool is registered ``deferred=True`` so it is absent from the LLM bind.
2. The LLM calls ``find_tools`` to retrieve it → ``promoted_tools`` is written
   to ``AgentState`` (per-thread, checkpointed).
3. On the next turn ``agent_node`` adds the promoted spec to the bind and the
   LLM's call to the (now-loaded) tool dispatches successfully.

The cross-turn carry proves promotion rides the AgentState channel, not a
ContextVar / mutated registry (per-run isolation).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.runnables import RunnableConfig

from expert_work.runtime.checkpointer import make_checkpointer
from orchestrator import (
    AgentState,
    FindToolsTool,
    GraphRunner,
    ToolContext,
    ToolRegistry,
    ToolResult,
    ToolSpec,
    build_react_graph,
)


@dataclass
class _ScriptedLLM:
    """LLMCaller stub recording the tool names bound on each call."""

    responses: list[AIMessage]
    calls: int = 0
    bound_tool_names: list[list[str]] = field(default_factory=list)

    async def __call__(
        self,
        *,
        messages: Sequence[BaseMessage],
        tools: Sequence[ToolSpec],
    ) -> AIMessage:
        self.bound_tool_names.append([t.name for t in tools])
        idx = self.calls
        self.calls += 1
        return self.responses[idx]


@dataclass
class _ScriptedTool:
    name: str
    result: str = "ok"

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(name=self.name, description=f"scripted {self.name}")

    async def call(self, args: Mapping[str, Any], *, ctx: ToolContext) -> ToolResult:
        del args, ctx
        return ToolResult(content=self.result)


def _tool_call(name: str, args: dict[str, Any], call_id: str) -> dict[str, Any]:
    return {"name": name, "args": args, "id": call_id, "type": "tool_call"}


@pytest.mark.asyncio
async def test_find_tools_promotes_deferred_tool_across_turns() -> None:
    registry = ToolRegistry()
    registry.register(FindToolsTool(registry=registry))
    registry.register(_ScriptedTool(name="github_issue", result="issue#42"), deferred=True)

    llm = _ScriptedLLM(
        responses=[
            # Turn 1 — retrieve the deferred tool.
            AIMessage(
                content="",
                tool_calls=[_tool_call("find_tools", {"query": "github"}, "tc-find")],
            ),
            # Turn 2 — call the now-promoted tool.
            AIMessage(
                content="",
                tool_calls=[_tool_call("github_issue", {"title": "bug"}, "tc-gh")],
            ),
            # Turn 3 — finalise.
            AIMessage(content="done"),
        ]
    )

    async with make_checkpointer("memory") as cp:
        runner = GraphRunner(checkpointer=cp)
        compiled = runner.compile(build_react_graph(llm_caller=llm, tool_registry=registry))
        cfg: RunnableConfig = {"configurable": {"thread_id": "te6-thread"}}
        state: AgentState = await compiled.ainvoke(
            {
                "messages": [HumanMessage(content="open a github issue")],
                "step_count": 0,
                "max_steps": 10,
            },
            config=cfg,
        )

    # Turn 1 bind: find_tools present, deferred github_issue absent.
    assert "find_tools" in llm.bound_tool_names[0]
    assert "github_issue" not in llm.bound_tool_names[0]
    # Turn 2 bind: github_issue now promoted into the bind.
    assert "github_issue" in llm.bound_tool_names[1]

    # promoted_tools carried on AgentState; the deferred tool dispatched.
    assert state["promoted_tools"] == ["github_issue"]
    tool_msgs = [m for m in state["messages"] if isinstance(m, ToolMessage)]
    assert any(m.content == "issue#42" for m in tool_msgs)
    assert state["messages"][-1].content == "done"


@pytest.mark.asyncio
async def test_two_find_tools_in_one_turn_promote_the_union() -> None:
    """Stream TE-6 — two ``find_tools`` calls in a single turn must promote
    BOTH discoveries. The tools_node batch accumulates list-valued state
    channels (a plain overwrite would drop all but the last call's list)."""
    registry = ToolRegistry()
    registry.register(FindToolsTool(registry=registry))
    registry.register(_ScriptedTool(name="github_issue"), deferred=True)
    registry.register(_ScriptedTool(name="postgres_query"), deferred=True)

    llm = _ScriptedLLM(
        responses=[
            # Turn 1 — two parallel find_tools, one per deferred tool.
            AIMessage(
                content="",
                tool_calls=[
                    _tool_call("find_tools", {"query": "select:github_issue"}, "tc-a"),
                    _tool_call("find_tools", {"query": "select:postgres_query"}, "tc-b"),
                ],
            ),
            # Turn 2 — finalise.
            AIMessage(content="done"),
        ]
    )

    async with make_checkpointer("memory") as cp:
        runner = GraphRunner(checkpointer=cp)
        compiled = runner.compile(build_react_graph(llm_caller=llm, tool_registry=registry))
        cfg: RunnableConfig = {"configurable": {"thread_id": "te6-parallel"}}
        state: AgentState = await compiled.ainvoke(
            {
                "messages": [HumanMessage(content="find both")],
                "step_count": 0,
                "max_steps": 10,
            },
            config=cfg,
        )

    # BOTH discoveries survive the batch (union, no silent loss).
    assert set(state["promoted_tools"]) == {"github_issue", "postgres_query"}
    # Turn 2 bind includes both promoted tools.
    assert "github_issue" in llm.bound_tool_names[1]
    assert "postgres_query" in llm.bound_tool_names[1]


@pytest.mark.asyncio
async def test_no_deferral_keeps_bind_identical() -> None:
    """Zero-behaviour-change guard: with no deferred tools the bind is unchanged."""
    registry = ToolRegistry()
    registry.register(FindToolsTool(registry=registry))
    registry.register(_ScriptedTool(name="search", result="r"))

    llm = _ScriptedLLM(responses=[AIMessage(content="done")])
    async with make_checkpointer("memory") as cp:
        runner = GraphRunner(checkpointer=cp)
        compiled = runner.compile(build_react_graph(llm_caller=llm, tool_registry=registry))
        cfg: RunnableConfig = {"configurable": {"thread_id": "te6-nodefer"}}
        await compiled.ainvoke(
            {
                "messages": [HumanMessage(content="hi")],
                "step_count": 0,
                "max_steps": 5,
            },
            config=cfg,
        )

    assert sorted(llm.bound_tool_names[0]) == ["find_tools", "search"]


# --- Stream HX-12 — call-through + ranked unknown-name suggestions ----------


@pytest.mark.asyncio
async def test_direct_call_to_deferred_name_executes_and_promotes() -> None:
    """HX-12 call-through: the model calls a deferred name WITHOUT a
    find_tools round-trip. Dispatch routes (TE-6 keeps deferred tools in
    the lookup table) and the name is promoted so the schema enters the
    next turn's bind."""
    registry = ToolRegistry()
    registry.register(FindToolsTool(registry=registry))
    registry.register(_ScriptedTool(name="github_issue", result="issue#7"), deferred=True)

    llm = _ScriptedLLM(
        responses=[
            # Turn 1 — call the deferred tool DIRECTLY (no find_tools).
            AIMessage(
                content="",
                tool_calls=[_tool_call("github_issue", {"title": "bug"}, "tc-direct")],
            ),
            # Turn 2 — finalise.
            AIMessage(content="done"),
        ]
    )

    async with make_checkpointer("memory") as cp:
        runner = GraphRunner(checkpointer=cp)
        compiled = runner.compile(build_react_graph(llm_caller=llm, tool_registry=registry))
        cfg: RunnableConfig = {"configurable": {"thread_id": "hx12-callthrough"}}
        state: AgentState = await compiled.ainvoke(
            {
                "messages": [HumanMessage(content="open a github issue")],
                "step_count": 0,
                "max_steps": 10,
            },
            config=cfg,
        )

    # The direct call executed (no unknown-tool error)...
    tool_msgs = [m for m in state["messages"] if isinstance(m, ToolMessage)]
    assert any(m.content == "issue#7" for m in tool_msgs)
    # ...and the name was promoted into AgentState + the next bind.
    assert state["promoted_tools"] == ["github_issue"]
    assert "github_issue" in llm.bound_tool_names[1]


@pytest.mark.asyncio
async def test_unknown_name_error_carries_ranked_suggestions() -> None:
    """HX-12 — a truly unknown name (typo/hallucination) errors with
    ranked suggestions from the deferred pool instead of a dead end."""
    registry = ToolRegistry()
    registry.register(FindToolsTool(registry=registry))
    registry.register(
        _ScriptedTool(name="github_create_issue", result="x"),
        deferred=True,
    )

    llm = _ScriptedLLM(
        responses=[
            AIMessage(
                content="",
                tool_calls=[_tool_call("github_issue_create", {}, "tc-typo")],
            ),
            AIMessage(content="done"),
        ]
    )

    async with make_checkpointer("memory") as cp:
        runner = GraphRunner(checkpointer=cp)
        compiled = runner.compile(build_react_graph(llm_caller=llm, tool_registry=registry))
        cfg: RunnableConfig = {"configurable": {"thread_id": "hx12-typo"}}
        state: AgentState = await compiled.ainvoke(
            {
                "messages": [HumanMessage(content="open an issue")],
                "step_count": 0,
                "max_steps": 10,
            },
            config=cfg,
        )

    tool_msgs = [m for m in state["messages"] if isinstance(m, ToolMessage)]
    error_msg = next(m for m in tool_msgs if m.status == "error")
    assert "Did you mean" in str(error_msg.content)
    assert "github_create_issue" in str(error_msg.content)
    # Nothing was promoted — the call never routed.
    assert not state.get("promoted_tools")


# --- Stream HX-12 — promotion demotion (PR4) --------------------------------


def test_merge_promoted_list_keeps_add_only_semantics() -> None:
    from orchestrator.state import _merge_promoted

    assert _merge_promoted(["a"], ["b", "a"]) == ["a", "b"]
    assert _merge_promoted(None, ["x"]) == ["x"]


def test_merge_promoted_dict_removes_and_adds() -> None:
    from orchestrator.state import _merge_promoted

    assert _merge_promoted(["a", "b", "c"], {"remove": ["b"]}) == ["a", "c"]
    assert _merge_promoted(["a"], {"add": ["b"], "remove": ["a"]}) == ["b"]
    # Removing an absent name is a no-op; add wins dedupe.
    assert _merge_promoted(["a"], {"add": ["a"], "remove": ["zz"]}) == ["a"]


def test_merge_last_used_takes_per_key_max() -> None:
    from orchestrator.state import _merge_last_used

    assert _merge_last_used({"a": 3, "b": 5}, {"a": 7, "c": 1}) == {"a": 7, "b": 5, "c": 1}
    assert _merge_last_used(None, {"a": 2}) == {"a": 2}
    # A stale (lower) stamp never regresses an entry.
    assert _merge_last_used({"a": 9}, {"a": 4}) == {"a": 9}


@dataclass
class _AlwaysCompress:
    """Duck-typed stand-in for ContextCompressor: always fires, identity."""

    async def compress(self, messages: Sequence[BaseMessage], **kwargs: Any) -> list[BaseMessage]:
        return list(messages)

    def should_compress(
        self, messages: Sequence[BaseMessage], *, reserved: Sequence[BaseMessage] = ()
    ) -> bool:
        del messages, reserved
        return True


@pytest.mark.asyncio
async def test_stale_promotion_demoted_on_compress_and_repromotable() -> None:
    """HX-12 PR4: a promoted tool unused past the stale window leaves the
    bind when the compressor fires; it stays in the deferred pool and a
    later find_tools call re-promotes it."""
    registry = ToolRegistry()
    registry.register(FindToolsTool(registry=registry))
    registry.register(_ScriptedTool(name="github_issue", result="x"), deferred=True)

    llm = _ScriptedLLM(
        responses=[
            AIMessage(content="done"),
        ]
    )

    async with make_checkpointer("memory") as cp:
        runner = GraphRunner(checkpointer=cp)
        compiled = runner.compile(
            build_react_graph(
                llm_caller=llm,
                tool_registry=registry,
                context_compressor=_AlwaysCompress(),  # type: ignore[arg-type]
            )
        )
        cfg: RunnableConfig = {"configurable": {"thread_id": "hx12-demote"}}
        # Seed: tool was promoted at step 0 and never used since; the run
        # is now at step 20 — far past the stale window.
        state: AgentState = await compiled.ainvoke(
            {
                "messages": [HumanMessage(content="hi")],
                "step_count": 20,
                "max_steps": 40,
                "promoted_tools": ["github_issue"],
                "promoted_tool_last_used": {"github_issue": 0},
            },
            config=cfg,
        )

    # Demoted: out of promoted_tools, and the (single-turn) bind that the
    # LLM saw still carried it this turn — demotion applies to the NEXT bind.
    assert state.get("promoted_tools") == []
    # Still in the deferred pool — re-promotable any time.
    assert "github_issue" in {s.name for s in registry.all_specs()}
    found = registry.search("github issue")
    assert [s.name for s in found] == ["github_issue"]


@pytest.mark.asyncio
async def test_fresh_promotion_survives_compress() -> None:
    """A tool promoted/used within the stale window is NOT demoted."""
    registry = ToolRegistry()
    registry.register(FindToolsTool(registry=registry))
    registry.register(_ScriptedTool(name="github_issue", result="x"), deferred=True)

    llm = _ScriptedLLM(responses=[AIMessage(content="done")])

    async with make_checkpointer("memory") as cp:
        runner = GraphRunner(checkpointer=cp)
        compiled = runner.compile(
            build_react_graph(
                llm_caller=llm,
                tool_registry=registry,
                context_compressor=_AlwaysCompress(),  # type: ignore[arg-type]
            )
        )
        cfg: RunnableConfig = {"configurable": {"thread_id": "hx12-fresh"}}
        state: AgentState = await compiled.ainvoke(
            {
                "messages": [HumanMessage(content="hi")],
                "step_count": 20,
                "max_steps": 40,
                "promoted_tools": ["github_issue"],
                "promoted_tool_last_used": {"github_issue": 15},  # 5 steps ago
            },
            config=cfg,
        )

    assert state.get("promoted_tools") == ["github_issue"]


@pytest.mark.asyncio
async def test_dispatch_refreshes_last_used_stamp() -> None:
    """tools_node stamps promoted tools on dispatch (and fresh promotions
    get a baseline), feeding the demotion gate."""
    registry = ToolRegistry()
    registry.register(FindToolsTool(registry=registry))
    registry.register(_ScriptedTool(name="github_issue", result="ok"), deferred=True)

    llm = _ScriptedLLM(
        responses=[
            AIMessage(
                content="",
                tool_calls=[_tool_call("github_issue", {}, "tc-1")],
            ),
            AIMessage(content="done"),
        ]
    )

    async with make_checkpointer("memory") as cp:
        runner = GraphRunner(checkpointer=cp)
        compiled = runner.compile(build_react_graph(llm_caller=llm, tool_registry=registry))
        cfg: RunnableConfig = {"configurable": {"thread_id": "hx12-stamp"}}
        state: AgentState = await compiled.ainvoke(
            {
                "messages": [HumanMessage(content="hi")],
                "step_count": 7,
                "max_steps": 20,
            },
            config=cfg,
        )

    # Call-through promoted the tool and stamped its baseline at the
    # dispatching step (step_count was 8 after the agent bump).
    stamps = state.get("promoted_tool_last_used") or {}
    assert "github_issue" in stamps
    assert stamps["github_issue"] >= 7


# --- Stream HX-13 — vendor-native disclosure tiers (bind shapes) ------------


def _registry_for_tiers() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(FindToolsTool(registry=registry))
    registry.register(_ScriptedTool(name="active_tool"))
    registry.register(_ScriptedTool(name="mcp__gh__issue"), deferred=True)
    registry.register(_ScriptedTool(name="mcp__gh__pr"), deferred=True)
    return registry


async def _bound_specs(
    tool_disclosure: str | None, *, promoted: list[str] | None = None
) -> list[ToolSpec]:
    registry = _registry_for_tiers()
    llm = _ScriptedLLM(responses=[AIMessage(content="done")])
    bound: list[list[ToolSpec]] = []

    async def _capture(*, messages: Any, tools: Any) -> AIMessage:
        bound.append(list(tools))
        return await llm(messages=messages, tools=tools)

    async with make_checkpointer("memory") as cp:
        runner = GraphRunner(checkpointer=cp)
        compiled = runner.compile(
            build_react_graph(
                llm_caller=_capture,
                tool_registry=registry,
                tool_disclosure=tool_disclosure,  # type: ignore[arg-type]
            )
        )
        cfg: RunnableConfig = {"configurable": {"thread_id": f"hx13-{tool_disclosure}"}}
        state: dict[str, Any] = {
            "messages": [HumanMessage(content="hi")],
            "step_count": 0,
            "max_steps": 5,
        }
        if promoted:
            state["promoted_tools"] = promoted
        await compiled.ainvoke(state, config=cfg)
    return bound[0]


@pytest.mark.asyncio
async def test_native_search_tier_marks_deferred_and_drops_find_tools() -> None:
    specs = await _bound_specs("native_search")
    by_name = {s.name: s for s in specs}
    assert "find_tools" not in by_name  # HX-J3: one retrieval channel
    assert by_name["active_tool"].defer_loading is False
    assert by_name["mcp__gh__issue"].defer_loading is True
    assert by_name["mcp__gh__pr"].defer_loading is True


@pytest.mark.asyncio
async def test_native_search_promoted_tool_loses_marker() -> None:
    specs = await _bound_specs("native_search", promoted=["mcp__gh__issue"])
    by_name = {s.name: s for s in specs}
    assert by_name["mcp__gh__issue"].defer_loading is False  # promoted = active-shaped
    assert by_name["mcp__gh__pr"].defer_loading is True


@pytest.mark.asyncio
async def test_allowed_tools_tier_keeps_find_tools_and_full_set() -> None:
    specs = await _bound_specs("allowed_tools")
    by_name = {s.name: s for s in specs}
    assert "find_tools" in by_name  # HX-J3: the only promotion entry point
    assert by_name["mcp__gh__issue"].defer_loading is True
    assert by_name["find_tools"].defer_loading is False


@pytest.mark.asyncio
async def test_none_tier_bind_unchanged() -> None:
    specs = await _bound_specs(None)
    names = {s.name for s in specs}
    assert names == {"find_tools", "active_tool"}  # HX-12 shape, byte-identical
    assert all(s.defer_loading is False for s in specs)
