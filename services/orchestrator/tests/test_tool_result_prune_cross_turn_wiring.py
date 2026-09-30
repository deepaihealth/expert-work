"""B-126 —— 跨轮清理经编译图生效:第 2 轮起旧结果收起;同一轮内各次调用前缀逐字
相同;检查点历史完整;两段对话互不引用对方路径。

``test_current_turn_tool_result_survives_the_max_steps_wrapup_tail_injection``
额外钉住一条 review round-1 裁定:CM-12 的跨轮清理必须在
``builder.agent_node`` 里所有「尾部追加真实 HumanMessage」的注入点
(``_append_tail_human_message`` —— 最大步数/token 预算收尾、token 预算提醒等)
**之前**运行。那些注入不是隐藏消息,一旦跨轮清理挪到它们之后,
``current_turn_start`` 会把注入的尾部消息误判成新一轮边界,把本轮自己的
工具结果当成「旧轮次」一并收起 —— 本测试通过强制触发 max-steps 收尾分支
复现这一条件,并在报告里用手工移动清理调用位置做了 mutation 自证。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.runnables import RunnableConfig

from expert_work.runtime.checkpointer import make_checkpointer
from orchestrator import (
    GraphRunner,
    ToolContext,
    ToolRegistry,
    ToolResult,
    ToolSpec,
    build_react_graph,
)
from orchestrator.context import ToolResultPruner
from orchestrator.sse import PROMPT_INPUTS_KEY
from orchestrator.tools.arg_bindings import (
    BOUND_ARGS_FINGERPRINT_ARTIFACT_KEY,
    ToolArgBindings,
    bound_values_fingerprint,
)
from orchestrator.tools.overflow import TOOL_RESULT_PATH_ARTIFACT_KEY

_BIG = "q" * 8000


@dataclass
class _RecordingLLM:
    seen: list[list[BaseMessage]] = field(default_factory=list)

    async def __call__(
        self, *, messages: Sequence[BaseMessage], tools: Sequence[ToolSpec]
    ) -> AIMessage:
        del tools
        self.seen.append(list(messages))
        return AIMessage(content="done")


def _prior(tag: str, n: int) -> list[BaseMessage]:
    out: list[BaseMessage] = [HumanMessage(content=f"user {tag}")]
    for i in range(n):
        cid = f"{tag}-{i}"
        out.append(
            AIMessage(content="", tool_calls=[{"id": cid, "name": "mcp_x", "args": {"k": cid}}])
        )
        out.append(
            ToolMessage(
                content=f"{_BIG}#{tag}{i}",
                tool_call_id=cid,
                name="mcp_x",
                artifact={TOOL_RESULT_PATH_ARTIFACT_KEY: f".tool_results/{tag}/{cid}.txt"},
            )
        )
    out.append(AIMessage(content=f"answer {tag}"))
    return out


def _pruner() -> ToolResultPruner:
    return ToolResultPruner(
        context_window=10**9,
        recent_tool_results_kept=1,
        min_context_tokens=100,
        min_reclaim_tokens=100,
    )


async def _invoke(llm: _RecordingLLM, history: list[BaseMessage], thread: str, **state: object):
    return await _invoke_with(llm, history, thread, _pruner(), **state)


async def _invoke_with(
    llm: _RecordingLLM,
    history: list[BaseMessage],
    thread: str,
    pruner: ToolResultPruner,
    **state: object,
):
    async with make_checkpointer("memory") as cp:
        compiled = GraphRunner(checkpointer=cp).compile(
            build_react_graph(
                llm_caller=llm, tool_registry=ToolRegistry(), tool_result_pruner=pruner
            )
        )
        cfg: RunnableConfig = {"configurable": {"thread_id": thread}}
        payload: dict[str, object] = {"messages": history, "step_count": 0, "max_steps": 5}
        payload.update(state)
        return await compiled.ainvoke(payload, config=cfg)


@pytest.mark.asyncio
async def test_prior_turn_collapsed_in_prompt_checkpoint_intact() -> None:
    history = [*_prior("a", 3), HumanMessage(content="now")]
    llm = _RecordingLLM()
    state = await _invoke(llm, history, str(uuid4()))
    prompt_tools = [m for m in llm.seen[0] if isinstance(m, ToolMessage)]
    pruned_stubs = sum(1 for m in prompt_tools if str(m.content).startswith("<tool-result-pruned>"))
    assert pruned_stubs == 2
    saved_tools = [m for m in state["messages"] if isinstance(m, ToolMessage)]
    assert all(str(m.content).startswith(_BIG) for m in saved_tools)


@pytest.mark.asyncio
async def test_two_threads_never_reference_each_other() -> None:
    llm_a, llm_b = _RecordingLLM(), _RecordingLLM()
    await _invoke(llm_a, [*_prior("custA", 3), HumanMessage(content="now")], "thread-A")
    await _invoke(llm_b, [*_prior("custB", 3), HumanMessage(content="now")], "thread-B")
    text_b = "\n".join(str(m.content) for m in llm_b.seen[0])
    assert "custA" not in text_b


# --------------------------------------------------------- review round-1: ordering pin


@pytest.mark.asyncio
async def test_current_turn_tool_result_survives_the_max_steps_wrapup_tail_injection() -> None:
    """跨轮清理必须在 max-steps 收尾追加的尾部 ``HumanMessage``**之前**算边界。

    构造:``step_count == max_steps`` 让 ``budget_exhausted`` 在这唯一一次
    ``agent_node`` 调用里立刻为真 —— 收尾分支会往 ``messages`` 尾部追加一条
    **非隐藏** ``HumanMessage``(``_MAX_STEPS_WRAPUP_INSTRUCTION``)。本轮自己在
    这条收尾消息之前已经有一条大工具结果(``now-0``,带持久化路径,构造上与
    旧轮次的三条同样「可无损找回」)。``recent_tool_results_kept=0`` 关掉最近
    窗保护,让这条断言只能靠「它在 current_turn_start 的边界之后」这一条结构
    性质活下来 —— 换句话说,如果清理挪到收尾注入之后跑,``current_turn_start``
    会把新追加的收尾消息当成边界,``now-0`` 连同旧轮次一起被收起,断言就会红。
    """
    history = [
        *_prior("old", 3),
        HumanMessage(content="now"),
        AIMessage(
            content="", tool_calls=[{"id": "now-0", "name": "mcp_x", "args": {"k": "now-0"}}]
        ),
        ToolMessage(
            content=f"{_BIG}#now0",
            tool_call_id="now-0",
            name="mcp_x",
            artifact={TOOL_RESULT_PATH_ARTIFACT_KEY: ".tool_results/now/now-0.txt"},
        ),
    ]
    llm = _RecordingLLM()
    pruner = ToolResultPruner(
        context_window=10**9,
        recent_tool_results_kept=0,
        min_context_tokens=100,
        min_reclaim_tokens=100,
    )
    async with make_checkpointer("memory") as cp:
        compiled = GraphRunner(checkpointer=cp).compile(
            build_react_graph(
                llm_caller=llm, tool_registry=ToolRegistry(), tool_result_pruner=pruner
            )
        )
        cfg: RunnableConfig = {"configurable": {"thread_id": str(uuid4())}}
        # step_count already at max_steps -> budget_exhausted fires on this one call.
        await compiled.ainvoke({"messages": history, "step_count": 1, "max_steps": 1}, config=cfg)

    prompt = llm.seen[0]
    # The wrap-up tail landed — sanity check the scenario actually exercises it.
    assert isinstance(prompt[-1], HumanMessage)
    assert "step budget" in str(prompt[-1].content).lower()
    tool_msgs = [m for m in prompt if isinstance(m, ToolMessage)]
    now_result = next(m for m in tool_msgs if m.tool_call_id == "now-0")
    # This turn's own tool result must reach the LLM byte-identical —
    # never collapsed into a cross-turn stub.
    assert str(now_result.content) == f"{_BIG}#now0"
    # The prior turn's results, in contrast, ARE eligible and get collapsed.
    old_results = [m for m in tool_msgs if m.tool_call_id != "now-0"]
    assert old_results and all(
        str(m.content).startswith("<tool-result-pruned>") for m in old_results
    )


# --------------------------------------------------------------------- B-129


@pytest.mark.asyncio
async def test_requery_stub_reaches_the_llm_and_checkpoint_is_intact() -> None:
    history: list[BaseMessage] = [HumanMessage(content="user a")]
    for i in range(3):
        cid = f"a-{i}"
        history += [
            AIMessage(content="", tool_calls=[{"id": cid, "name": "mcp_x", "args": {"k": cid}}]),
            ToolMessage(content="r" * 8000 + f"#{i}", tool_call_id=cid, name="mcp_x"),
        ]
    history += [AIMessage(content="answer a"), HumanMessage(content="now")]
    llm = _RecordingLLM()
    pruner = ToolResultPruner(
        context_window=10**9,
        recent_tool_results_kept=1,
        min_context_tokens=100,
        min_reclaim_tokens=100,
        requery_tools=frozenset({"mcp_x"}),
    )
    state = await _invoke_with(llm, history, str(uuid4()), pruner)
    prompt_tools = [m for m in llm.seen[0] if isinstance(m, ToolMessage)]
    assert sum("call the same tool again" in str(m.content) for m in prompt_tools) == 2
    saved = [m for m in state["messages"] if isinstance(m, ToolMessage)]
    assert all(str(m.content).startswith("r" * 100) for m in saved)


# ------------------------------------------- B-129 变量绑定的工具:派发记指纹,再查按指纹


_BOUND = ToolArgBindings(args={"project_code": "project"}, fixed={"scope": "all"})


@dataclass
class _RecordingWriter:
    writes: dict[str, str] = field(default_factory=dict)

    async def write(self, *, rel: str, content: str) -> None:
        self.writes[rel] = content


@dataclass
class _LookupTool:
    """只读查询工具;正文落在持久化区间(4k-12k),派发时会写盘并把路径记进 artifact。"""

    name: str = "lookup"

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(name=self.name, description="lookup", is_read_only=True)

    async def call(self, args: Mapping[str, Any], *, ctx: ToolContext) -> ToolResult:
        del args, ctx
        return ToolResult(content="v" * 6_000, meta={"source": "unit"})


@dataclass
class _OneCallLLM:
    calls: int = 0

    async def __call__(
        self, *, messages: Sequence[BaseMessage], tools: Sequence[ToolSpec]
    ) -> AIMessage:
        del messages, tools
        self.calls += 1
        if self.calls == 1:
            return AIMessage(
                content="",
                tool_calls=[{"name": "lookup", "args": {}, "id": "tc-1", "type": "tool_call"}],
            )
        return AIMessage(content="done")


@pytest.mark.asyncio
async def test_dispatch_records_bound_values_fingerprint_and_keeps_the_copy_path() -> None:
    registry = ToolRegistry()
    registry.register(_LookupTool())
    registry.bind_tool_args("lookup", _BOUND)
    writer = _RecordingWriter()
    inputs = {"project": "P-1"}
    async with make_checkpointer("memory") as cp:
        compiled = GraphRunner(checkpointer=cp).compile(
            build_react_graph(
                llm_caller=_OneCallLLM(),
                tool_registry=registry,
                workspace_writer_factory=lambda _ctx: writer,
            )
        )
        cfg: RunnableConfig = {
            "configurable": {"thread_id": str(uuid4()), PROMPT_INPUTS_KEY: inputs}
        }
        state = await compiled.ainvoke(
            {"messages": [HumanMessage(content="go")], "step_count": 0, "max_steps": 5},
            config=cfg,
        )
    (message,) = [m for m in state["messages"] if isinstance(m, ToolMessage)]
    artifact = message.artifact
    assert isinstance(artifact, dict)
    assert artifact[BOUND_ARGS_FINGERPRINT_ARTIFACT_KEY] == bound_values_fingerprint(_BOUND, inputs)
    assert artifact[TOOL_RESULT_PATH_ARTIFACT_KEY] == ".tool_results/adhoc/tc-1-lookup.txt"
    assert artifact["source"] == "unit"
    assert "P-1" not in str(artifact)


def _bound_history(fp: str | None) -> list[BaseMessage]:
    history: list[BaseMessage] = [HumanMessage(content="user a")]
    for i in range(3):
        cid = f"a-{i}"
        history += [
            AIMessage(content="", tool_calls=[{"id": cid, "name": "lookup", "args": {}}]),
            ToolMessage(
                content="r" * 8000 + f"#{i}",
                tool_call_id=cid,
                name="lookup",
                artifact={BOUND_ARGS_FINGERPRINT_ARTIFACT_KEY: fp} if fp else None,
            ),
        ]
    history += [AIMessage(content="answer a"), HumanMessage(content="now")]
    return history


async def _requery_prompt(history: list[BaseMessage], inputs: Mapping[str, Any]) -> list[str]:
    registry = ToolRegistry()
    registry.register(_LookupTool())
    registry.bind_tool_args("lookup", _BOUND)
    llm = _RecordingLLM()
    pruner = ToolResultPruner(
        context_window=10**9,
        recent_tool_results_kept=1,
        min_context_tokens=100,
        min_reclaim_tokens=100,
        requery_tools=frozenset({"lookup"}),
    )
    async with make_checkpointer("memory") as cp:
        compiled = GraphRunner(checkpointer=cp).compile(
            build_react_graph(llm_caller=llm, tool_registry=registry, tool_result_pruner=pruner)
        )
        cfg: RunnableConfig = {
            "configurable": {"thread_id": str(uuid4()), PROMPT_INPUTS_KEY: dict(inputs)}
        }
        await compiled.ainvoke({"messages": history, "step_count": 0, "max_steps": 5}, config=cfg)
    return [str(m.content) for m in llm.seen[0] if isinstance(m, ToolMessage)]


@pytest.mark.asyncio
async def test_agent_node_requery_stubs_only_when_this_turns_bound_values_match() -> None:
    same = bound_values_fingerprint(_BOUND, {"project": "P-1"})
    stubbed = await _requery_prompt(_bound_history(same), {"project": "P-1"})
    assert sum("call the same tool again" in c for c in stubbed) == 2
    kept = await _requery_prompt(_bound_history(same), {"project": "P-2"})
    assert sum("call the same tool again" in c for c in kept) == 0
    assert all(c.startswith("r" * 100) for c in kept)
