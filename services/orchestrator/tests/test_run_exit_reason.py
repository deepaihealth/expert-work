"""B-85 ③ —— run 的退出原因(``exit_reason``)与「最后一批工具失败」通道。

**判据纪律**(spec `2026-09-20-run-completion-signal-design.md` §4):只用客观事实,
不推断模型意图。这里钉住的是「最后一批工具调用里还有没有未解决的非 transient 失败」,
**不是**「模型是不是放弃了」—— 后者猜不准(模型合理地换个方法也长这样)。

测试照 ``test_recovery_advisory.py`` 的做法驱动**真图**:退出原因这件事的全部意义
就在于「从哪条路出去的」,用桩替掉图就等于替掉了被测对象。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
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
from orchestrator.graph_builder.builder import budget_exit_reason

# ---------------------------------------------------------------------------
# 桩
# ---------------------------------------------------------------------------


@dataclass
class _ScriptedLLM:
    responses: list[AIMessage]
    calls: int = 0

    async def __call__(
        self, *, messages: Sequence[BaseMessage], tools: Sequence[ToolSpec]
    ) -> AIMessage:
        del messages, tools
        idx = self.calls
        self.calls += 1
        # 撞预算的用例会比脚本多跑一轮(收尾轮),最后一条兜底重放。
        return self.responses[min(idx, len(self.responses) - 1)]


@dataclass
class _ScriptedTool:
    """``fail_on`` 里的第几次调用抛异常;``error`` 决定分类器判 transient 与否。"""

    name: str = "save_artifact"
    fail_on: frozenset[int] = frozenset()
    error: str = "disk full"
    calls: int = 0
    _seen: list[int] = field(default_factory=list)

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name=self.name,
            description="scripted tool",
            parameters={
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
        )

    async def call(self, args: Mapping[str, Any], *, ctx: ToolContext) -> ToolResult:
        del ctx
        idx = self.calls
        self.calls += 1
        self._seen.append(idx)
        if idx in self.fail_on:
            raise OSError(self.error)
        return ToolResult(content=f"Saved {args.get('name')!r}.")


def _tc(name: str, call_id: str) -> dict[str, Any]:
    return {"name": name, "args": {"name": "x.md"}, "id": call_id, "type": "tool_call"}


async def _run(llm: _ScriptedLLM, registry: ToolRegistry, *, max_steps: int = 5) -> AgentState:
    async with make_checkpointer("memory") as cp:
        runner = GraphRunner(checkpointer=cp)
        compiled = runner.compile(build_react_graph(llm_caller=llm, tool_registry=registry))
        cfg: RunnableConfig = {"configurable": {"thread_id": str(uuid4())}}
        return await compiled.ainvoke(
            {"messages": [HumanMessage(content="start")], "step_count": 0, "max_steps": max_steps},
            config=cfg,
        )


def _failure_classes(state: AgentState) -> list[str]:
    return [f.error_class for f in state.get("last_batch_failures", [])]


# ---------------------------------------------------------------------------
# Task 1 —— last_batch_failures 通道
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_final_batch_failure_survives_to_the_end() -> None:
    """B-85 那次的形状:工具失败 → 模型不再调工具 → 图正常收尾。

    ``tool_failures`` 到这时已经被 ``agent_node`` 清空了(builder 里发完
    ``<recovery-advisory>`` 就重置),所以终局读它永远是空的 —— 这正是
    本通道存在的理由。
    """
    llm = _ScriptedLLM(
        responses=[
            AIMessage(content="", tool_calls=[_tc("save_artifact", "tc-1")]),
            AIMessage(content="我放弃了"),
        ]
    )
    registry = ToolRegistry()
    registry.register(_ScriptedTool(fail_on=frozenset({0})))

    state = await _run(llm, registry)

    assert state.get("tool_failures", []) == [], "前提:tool_failures 按轮重置,终局拿不到"
    assert _failure_classes(state), "last_batch_failures 必须留到终局"


@pytest.mark.asyncio
async def test_a_clean_follow_up_batch_clears_the_channel() -> None:
    """批 1 失败 → 批 2 全成功 → 结束。通道必须被**清空**。

    ``tools`` 节点只在「有失败」时才写的话,通道里会一直留着批 1 的失败,
    于是一个已经自我恢复的 run 被判成没做成 —— **误报**。这是 spec §6.2 那条防线。
    """
    llm = _ScriptedLLM(
        responses=[
            AIMessage(content="", tool_calls=[_tc("save_artifact", "tc-1")]),
            AIMessage(content="", tool_calls=[_tc("save_artifact", "tc-2")]),
            AIMessage(content="done"),
        ]
    )
    registry = ToolRegistry()
    registry.register(_ScriptedTool(fail_on=frozenset({0})))  # 只有第一次失败

    state = await _run(llm, registry)

    assert "last_batch_failures" in state, "跑过工具就必须写,哪怕是空的"
    assert _failure_classes(state) == []


@pytest.mark.asyncio
async def test_transient_failures_are_not_recorded() -> None:
    """transient 是可重试的抖动,不是「没做成」的证据。

    与 ``error_signal``(builder.py 的动态 effort 触发器)同一条谓词:
    ``error_class != "transient"``。

    **用只读工具**(``web_search``)而不是 ``save_artifact``:写类工具失败会被
    L-4 的 mutation 分类器先折成 ``mutation_not_landed``,那条路压根到不了
    transient 判定 —— 第一版拿 ``save_artifact`` 写,红在这里。
    """
    llm = _ScriptedLLM(
        responses=[
            AIMessage(content="", tool_calls=[_tc("web_search", "tc-1")]),
            AIMessage(content="done"),
        ]
    )
    registry = ToolRegistry()
    registry.register(
        _ScriptedTool(name="web_search", fail_on=frozenset({0}), error="connection reset by peer")
    )

    state = await _run(llm, registry)

    assert _failure_classes(state) == []


# ---------------------------------------------------------------------------
# Task 2 —— exit_reason
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_plain_stop_stamps_text_response() -> None:
    llm = _ScriptedLLM(responses=[AIMessage(content="done")])
    state = await _run(llm, ToolRegistry())
    assert state.get("exit_reason") == "text_response"


@pytest.mark.asyncio
async def test_max_steps_does_not_stamp_text_response() -> None:
    """撞预算之后走的是「一次无工具的收尾轮」,响应天然没有 tool_calls ——
    不显式排除就会被 ``text_response`` 盖掉,而那正好把「平台主动中止」
    伪装成「模型自然说完了」。"""
    llm = _ScriptedLLM(responses=[AIMessage(content="", tool_calls=[_tc("save_artifact", "tc-1")])])
    registry = ToolRegistry()
    registry.register(_ScriptedTool())

    state = await _run(llm, registry, max_steps=1)

    assert state.get("exit_reason") == "max_steps"


def test_budget_reason_precedence_is_pinned() -> None:
    """三个同时为真时的取值顺序**钉死**,否则它随分支书写顺序隐式漂移。"""
    assert (
        budget_exit_reason(max_steps=3, step_count=3, stuck=True, token_tripped=True) == "max_steps"
    )
    assert (
        budget_exit_reason(max_steps=0, step_count=0, stuck=True, token_tripped=True)
        == "no_progress"
    )
    assert (
        budget_exit_reason(max_steps=0, step_count=0, stuck=False, token_tripped=True)
        == "token_budget"
    )
    assert budget_exit_reason(max_steps=3, step_count=1, stuck=False, token_tripped=False) is None


def test_max_steps_zero_means_no_budget() -> None:
    """``max_steps=0`` 是「不设预算」,不是「预算为零、立刻用尽」。"""
    assert budget_exit_reason(max_steps=0, step_count=7, stuck=False, token_tripped=False) is None
