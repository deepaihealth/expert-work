"""Stream CM-1 — recovery-advisory integration tests (generalises L.L4).

Drives the full ``tools_node`` → ``AgentState.tool_failures`` →
``agent_node`` round-trip through the compiled graph: a failing tool
call surfaces a ``<recovery-advisory>`` HumanMessage on the next agent
step, the advisory persists in conversation history, the channel resets
so a second turn doesn't re-inject, and (Mini-ADR CM-B4 / L-1 invariant)
the advisory never lands in a ``SystemMessage``.

CM-1 generalises L-4 beyond file mutations: a failing read-only tool now
also advises (it used to be mutation-only). The two ``save_artifact``
cases exercise the folded-in ``mutation_not_landed`` class; the
``web_search`` case exercises the error-path classifier.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

import pytest
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.runnables import RunnableConfig

from expert_work.runtime.checkpointer import make_checkpointer
from orchestrator import (
    AgentState,
    GraphRunner,
    ToolContext,
    ToolRegistry,
    ToolResult,
    ToolSpec,
    build_react_graph,
)

# ---------------------------------------------------------------------------
# Test stubs
# ---------------------------------------------------------------------------


@dataclass
class _ScriptedLLM:
    responses: list[AIMessage]
    seen_prompts: list[list[BaseMessage]]
    calls: int = 0

    async def __call__(
        self,
        *,
        messages: Sequence[BaseMessage],
        tools: Sequence[ToolSpec],
    ) -> AIMessage:
        del tools
        self.seen_prompts.append(list(messages))
        idx = self.calls
        self.calls += 1
        if idx >= len(self.responses):
            msg = f"scripted LLM ran out at call {idx}"
            raise RuntimeError(msg)
        return self.responses[idx]


@dataclass
class _ScriptedSaveArtifact:
    """``save_artifact`` stub. ``fail`` controls whether the dispatch
    raises (→ ToolMessage(status="error") via the builder's error
    wrapper) or returns success."""

    name: str = "save_artifact"
    fail: bool = False

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name=self.name,
            description="scripted save_artifact",
            parameters={
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
            # L6 path_args lets multiple saves to different paths
            # parallelise; not exercised by these tests but kept for parity.
            path_args=("name",),
        )

    async def call(self, args: Mapping[str, Any], *, ctx: ToolContext) -> ToolResult:
        del ctx
        if self.fail:
            msg = "disk full"
            raise OSError(msg)
        return ToolResult(content=f"Saved {args.get('name')!r}.")


@dataclass
class _ScriptedWorkspaceWrite:
    """``write_file`` / ``edit_file`` stub —— 路径参数叫 ``path``,不是 ``name``。

    B-84 第 3 条把这两个工具加进了 L-4 的 mutation 分类器,所以它们的失败
    现在也走 ``mutation_not_landed`` 并带得出路径。
    """

    name: str = "edit_file"
    fail: bool = False
    error: str = "old_string not found"

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name=self.name,
            description="scripted workspace write",
            parameters={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
            path_args=("path",),
        )

    async def call(self, args: Mapping[str, Any], *, ctx: ToolContext) -> ToolResult:
        del ctx
        if self.fail:
            raise OSError(self.error)
        return ToolResult(content=f"Wrote {args.get('path')!r}.")


def _tc(name: str, args: dict[str, Any], call_id: str) -> dict[str, Any]:
    return {"name": name, "args": args, "id": call_id, "type": "tool_call"}


async def _run(
    llm: _ScriptedLLM,
    registry: ToolRegistry,
    *,
    max_steps: int = 5,
) -> AgentState:
    async with make_checkpointer("memory") as cp:
        runner = GraphRunner(checkpointer=cp)
        compiled = runner.compile(build_react_graph(llm_caller=llm, tool_registry=registry))
        cfg: RunnableConfig = {"configurable": {"thread_id": str(uuid4())}}
        return await compiled.ainvoke(
            {
                "messages": [HumanMessage(content="start")],
                "step_count": 0,
                "max_steps": max_steps,
            },
            config=cfg,
        )


def _find_advisory(messages: Sequence[BaseMessage]) -> HumanMessage | None:
    for msg in messages:
        if isinstance(msg, HumanMessage):
            content = msg.content if isinstance(msg.content, str) else ""
            if "<recovery-advisory>" in content:
                return msg
    return None


# ---------------------------------------------------------------------------
# tools_node → tool_failures channel (mutation_not_landed case)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_failing_save_artifact_populates_tool_failures_channel() -> None:
    """A failing ``save_artifact`` surfaces a ``mutation_not_landed``
    classification in the advisory the next agent step sees; the channel
    is consumed (reset to ``[]``) by the time the run ends."""
    prompts: list[list[BaseMessage]] = []
    llm = _ScriptedLLM(
        responses=[
            AIMessage(
                content="",
                tool_calls=[_tc("save_artifact", {"name": "report.md"}, "tc-1")],
            ),
            AIMessage(content="done"),
        ],
        seen_prompts=prompts,
    )
    registry = ToolRegistry()
    registry.register(_ScriptedSaveArtifact(fail=True))

    state = await _run(llm, registry)

    # Consumed by the agent step → empty in the final state.
    assert state.get("tool_failures", []) == []
    # The second LLM call DID see the advisory, with the path preserved.
    second_prompt = prompts[1]
    advisory = _find_advisory(second_prompt)
    assert advisory is not None
    content = advisory.content if isinstance(advisory.content, str) else ""
    assert "report.md" in content
    assert "mutation_not_landed" in content


@pytest.mark.asyncio
async def test_failing_edit_file_advisory_carries_the_path() -> None:
    """B-84 第 3 条 —— ``edit_file`` 进了 mutation 分类器之后,它的失败也
    **带得出那个路径**。

    这是扩集合带来的行为变化,不是纯加法:扩之前 ``edit_file`` 的失败走
    error_classifier 的通用分类(``unknown`` 之类),advisory 里**没有路径**,
    模型只知道「有个编辑失败了」。路径是 run 级欠账的记账单位,取不到它,
    ``edit_file`` 失败 → ``write_file`` 重写成功这一格就抵消不掉。
    """
    prompts: list[list[BaseMessage]] = []
    llm = _ScriptedLLM(
        responses=[
            AIMessage(content="", tool_calls=[_tc("edit_file", {"path": "style/render.py"}, "t1")]),
            AIMessage(content="done"),
        ],
        seen_prompts=prompts,
    )
    registry = ToolRegistry()
    registry.register(_ScriptedWorkspaceWrite(fail=True))

    await _run(llm, registry)

    advisory = _find_advisory(prompts[1])
    assert advisory is not None
    content = advisory.content if isinstance(advisory.content, str) else ""
    assert "style/render.py" in content, "路径没带出来,欠账就对不上键"
    assert "mutation_not_landed" in content


@pytest.mark.asyncio
async def test_successful_write_file_is_not_judged_a_failure() -> None:
    """扩集合的**反向**防线:成功的写不许被判成没落地。

    ``classify`` 现在对 ``write_file`` 一律返回 ``MutationOutcome``,成功那条
    ``landed=True``,``_classify_tool_failure`` 必须继续返回 ``None``。搞反了
    就是每一次正常写入都发一条 advisory —— 比不发更坏,模型会学会无视它。
    """
    prompts: list[list[BaseMessage]] = []
    llm = _ScriptedLLM(
        responses=[
            AIMessage(content="", tool_calls=[_tc("write_file", {"path": "a.py"}, "t1")]),
            AIMessage(content="done"),
        ],
        seen_prompts=prompts,
    )
    registry = ToolRegistry()
    registry.register(_ScriptedWorkspaceWrite(name="write_file"))

    state = await _run(llm, registry)

    assert _find_advisory(prompts[1]) is None, "成功的写不该有 advisory"
    assert state.get("unresolved_failures", []) == [], "成功的写不该留欠账"


@pytest.mark.asyncio
async def test_successful_save_artifact_does_not_inject_advisory() -> None:
    """Happy path: no failures → no advisory in any future prompt. The
    L1 cache-prefix invariant cares that we don't spuriously inject
    HumanMessages when the agent is healthy."""
    prompts: list[list[BaseMessage]] = []
    llm = _ScriptedLLM(
        responses=[
            AIMessage(
                content="",
                tool_calls=[_tc("save_artifact", {"name": "report.md"}, "tc-1")],
            ),
            AIMessage(content="done"),
        ],
        seen_prompts=prompts,
    )
    registry = ToolRegistry()
    registry.register(_ScriptedSaveArtifact(fail=False))

    await _run(llm, registry)

    second_prompt = prompts[1]
    assert _find_advisory(second_prompt) is None


# ---------------------------------------------------------------------------
# agent_node — invariants
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_advisory_lives_in_human_message_not_system() -> None:
    """Mini-ADR L-1 / CM-B4: the advisory must NOT live in a
    ``SystemMessage`` (would invalidate the prompt-cache prefix)."""
    prompts: list[list[BaseMessage]] = []
    llm = _ScriptedLLM(
        responses=[
            AIMessage(
                content="",
                tool_calls=[_tc("save_artifact", {"name": "x.md"}, "tc-1")],
            ),
            AIMessage(content="done"),
        ],
        seen_prompts=prompts,
    )
    registry = ToolRegistry()
    registry.register(_ScriptedSaveArtifact(fail=True))

    await _run(llm, registry)

    second_prompt = prompts[1]
    for msg in second_prompt:
        if isinstance(msg, SystemMessage):
            content = msg.content if isinstance(msg.content, str) else ""
            assert "<recovery-advisory>" not in content


@pytest.mark.asyncio
async def test_advisory_persists_in_conversation_history() -> None:
    """The advisory is part of the agent step's return dict so it lands
    in ``state["messages"]`` via the ``add_messages`` reducer — the next
    checkpoint resume sees the same history."""
    prompts: list[list[BaseMessage]] = []
    llm = _ScriptedLLM(
        responses=[
            AIMessage(
                content="",
                tool_calls=[_tc("save_artifact", {"name": "x.md"}, "tc-1")],
            ),
            AIMessage(content="done"),
        ],
        seen_prompts=prompts,
    )
    registry = ToolRegistry()
    registry.register(_ScriptedSaveArtifact(fail=True))

    state = await _run(llm, registry)

    assert _find_advisory(state["messages"]) is not None


@pytest.mark.asyncio
async def test_persisted_advisory_is_marked_hidden_from_ui() -> None:
    """RT-2 PR-4 (RT-ADR-9), Option A — the persisted advisory carries
    ``expert_work_hide_from_ui`` so the UI bubble view (``read_turns`` with
    ``include_hidden=False``) keeps the CM-1 leak out of the rendered
    transcript, while the durable record + search/audit mirror stay faithful
    (the default ``include_hidden=True``) and the model sees it in-prompt."""
    prompts: list[list[BaseMessage]] = []
    llm = _ScriptedLLM(
        responses=[
            AIMessage(
                content="",
                tool_calls=[_tc("save_artifact", {"name": "x.md"}, "tc-1")],
            ),
            AIMessage(content="done"),
        ],
        seen_prompts=prompts,
    )
    registry = ToolRegistry()
    registry.register(_ScriptedSaveArtifact(fail=True))

    state = await _run(llm, registry)

    advisory = _find_advisory(state["messages"])
    assert advisory is not None
    assert advisory.additional_kwargs.get("expert_work_hide_from_ui") is True


@pytest.mark.asyncio
async def test_advisory_does_not_double_inject_on_followup_turn() -> None:
    """After consumption ``tool_failures`` resets to ``[]``; a follow-up
    agent step (no further failures) must NOT re-inject the same advisory
    text."""
    prompts: list[list[BaseMessage]] = []
    llm = _ScriptedLLM(
        responses=[
            # Turn 1: fail.
            AIMessage(
                content="",
                tool_calls=[_tc("save_artifact", {"name": "x.md"}, "tc-1")],
            ),
            # Turn 2: ack the advisory, call no tools → run ends.
            AIMessage(content="I understand the save failed"),
        ],
        seen_prompts=prompts,
    )
    registry = ToolRegistry()
    registry.register(_ScriptedSaveArtifact(fail=True))

    state = await _run(llm, registry)

    advisories = [
        m
        for m in state["messages"]
        if isinstance(m, HumanMessage)
        and isinstance(m.content, str)
        and "<recovery-advisory>" in m.content
    ]
    assert len(advisories) == 1


# ---------------------------------------------------------------------------
# Multiple failures aggregate into one advisory
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_multiple_failures_aggregate_into_single_advisory() -> None:
    """Two failing ``save_artifact`` calls in one batch produce a single
    advisory listing both paths — the "footer aggregates cross-tool
    failures" guarantee. (Different paths share one L6 stage.)"""
    prompts: list[list[BaseMessage]] = []
    llm = _ScriptedLLM(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    _tc("save_artifact", {"name": "a.md"}, "tc-1"),
                    _tc("save_artifact", {"name": "b.md"}, "tc-2"),
                ],
            ),
            AIMessage(content="done"),
        ],
        seen_prompts=prompts,
    )
    registry = ToolRegistry()
    registry.register(_ScriptedSaveArtifact(fail=True))

    await _run(llm, registry)

    second_prompt = prompts[1]
    advisory = _find_advisory(second_prompt)
    assert advisory is not None
    content = advisory.content if isinstance(advisory.content, str) else ""
    assert "a.md" in content
    assert "b.md" in content


@pytest.mark.asyncio
async def test_only_failing_tool_appears_in_advisory() -> None:
    """A failing ``save_artifact`` alongside a *successful* read-only tool
    surfaces only the failure — successful calls never appear."""

    @dataclass
    class _ReadTool:
        @property
        def spec(self) -> ToolSpec:
            return ToolSpec(
                name="web_search",
                description="scripted web_search",
                is_read_only=True,
            )

        async def call(self, args: Mapping[str, Any], *, ctx: ToolContext) -> ToolResult:
            del args, ctx
            return ToolResult(content="some search result")

    prompts: list[list[BaseMessage]] = []
    llm = _ScriptedLLM(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    _tc("save_artifact", {"name": "report.md"}, "tc-1"),
                    _tc("web_search", {"q": "x"}, "tc-2"),
                ],
            ),
            AIMessage(content="done"),
        ],
        seen_prompts=prompts,
    )
    registry = ToolRegistry()
    registry.register(_ScriptedSaveArtifact(fail=True))
    registry.register(_ReadTool())

    await _run(llm, registry)

    second_prompt = prompts[1]
    advisory = _find_advisory(second_prompt)
    assert advisory is not None
    content = advisory.content if isinstance(advisory.content, str) else ""
    assert "save_artifact" in content
    # web_search succeeded → not a failure → not in the advisory.
    assert "web_search" not in content


@pytest.mark.asyncio
async def test_failing_read_only_tool_now_advises() -> None:
    """CM-1 generalisation: a failing read-only tool now produces a
    recovery advisory (under L-4 this was mutation-only and stayed
    silent). The transient class on a read-only tool is retryable."""

    @dataclass
    class _TimingOutSearch:
        @property
        def spec(self) -> ToolSpec:
            return ToolSpec(
                name="web_search",
                description="scripted web_search",
                is_read_only=True,
            )

        async def call(self, args: Mapping[str, Any], *, ctx: ToolContext) -> ToolResult:
            del args, ctx
            msg = "upstream timed out"
            raise TimeoutError(msg)

    prompts: list[list[BaseMessage]] = []
    llm = _ScriptedLLM(
        responses=[
            AIMessage(
                content="",
                tool_calls=[_tc("web_search", {"q": "x"}, "tc-1")],
            ),
            AIMessage(content="done"),
        ],
        seen_prompts=prompts,
    )
    registry = ToolRegistry()
    registry.register(_TimingOutSearch())

    state = await _run(llm, registry)

    # The error ToolMessage is still inline in history.
    tool_msgs = [m for m in state["messages"] if isinstance(m, ToolMessage)]
    assert len(tool_msgs) == 1
    assert tool_msgs[0].status == "error"
    # And — the CM-1 delta — a recovery advisory now fires for it.
    advisory = _find_advisory(state["messages"])
    assert advisory is not None
    content = advisory.content if isinstance(advisory.content, str) else ""
    assert "web_search" in content
    assert "transient" in content
