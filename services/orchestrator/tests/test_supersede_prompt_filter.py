"""P-1 —— agent_node 的 prompt 视图整轮剔除被取代消息(真 ReAct 图 + 内存 checkpointer)。

验收判据 = **送进 LLM 的 messages 列表**里没有被取代轮的任何文本 / 工具调用 / 计划;
这是 spec §7 PR2 要求的「探针」形态:``_ScriptedLLM`` 就是能复述上下文的探针。
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import pytest
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.runnables import RunnableConfig

from expert_work.common.message_stamp import stamp_message
from expert_work.common.supersede import mark_superseded
from expert_work.protocol.plan import Plan, PlanStep
from expert_work.runtime.checkpointer import make_checkpointer
from orchestrator import (
    GraphRunner,
    ToolContext,
    ToolRegistry,
    ToolResult,
    ToolSpec,
    build_react_graph,
)
from orchestrator.context import WorkingWindow

GOAL_TWO = "PLAN-GOAL-TWO"


@dataclass
class _ScriptedLLM:
    script: list[AIMessage]
    prompts: list[list[BaseMessage]] = field(default_factory=list)

    async def __call__(
        self, *, messages: Sequence[BaseMessage], tools: Sequence[ToolSpec], **_: Any
    ) -> AIMessage:
        del tools
        self.prompts.append(list(messages))
        return self.script.pop(0)


@dataclass
class _PlanTool:
    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="set_plan",
            description="plan",
            parameters={"type": "object", "properties": {"goal": {"type": "string"}}},
        )

    async def call(self, args: Mapping[str, Any], *, ctx: ToolContext) -> ToolResult:
        del ctx
        return ToolResult(
            content="ok",
            state_updates={
                "plan": Plan(goal=str(args["goal"]), steps=(PlanStep(id="1", description="s"),))
            },
        )


def _cfg(run_id: str) -> RunnableConfig:
    return {"configurable": {"thread_id": "t", "tenant_id": "tenant", "run_id": run_id}}


def _turn_input(text: str, run_id: str) -> dict[str, Any]:
    human = stamp_message(HumanMessage(content=text), run_id=run_id, now=datetime.now(UTC))
    return {"messages": [SystemMessage(content="sys"), human], "step_count": 0, "max_steps": 6}


def _dump(msgs: Sequence[BaseMessage]) -> str:
    return json.dumps([m.model_dump() for m in msgs], ensure_ascii=False, default=str)


def _script() -> list[AIMessage]:
    """两轮 + 第三轮:第二轮走一次工具(制造 AI(tool_calls) + ToolMessage 这一对)。"""
    return [
        AIMessage(content="A1-final"),
        AIMessage(
            content="",
            tool_calls=[
                {"name": "set_plan", "args": {"goal": GOAL_TWO}, "id": "tc2", "type": "tool_call"}
            ],
        ),
        AIMessage(content="A2-final"),
        AIMessage(content="A3-final"),
    ]


async def _two_turns_then_supersede_second(
    llm: _ScriptedLLM, cp: Any, *, working_window: WorkingWindow | None = None
) -> Any:
    registry = ToolRegistry()
    registry.register(_PlanTool())
    compiled = GraphRunner(checkpointer=cp).compile(
        build_react_graph(llm_caller=llm, tool_registry=registry, working_window=working_window)
    )
    await compiled.ainvoke(_turn_input("U1", "r1"), config=_cfg("r1"))
    await compiled.ainvoke(_turn_input("U2", "r2"), config=_cfg("r2"))
    base: RunnableConfig = {"configurable": {"thread_id": "t", "tenant_id": "tenant"}}
    snap = await compiled.aget_state(base)
    msgs = list(snap.values["messages"])
    # 第一轮 3 条(System/Human/AI),第二轮 5 条(含 tool_calls + Tool)。
    turn2 = msgs[3:]
    copies = [mark_superseded(m, new_run_id="r3", now=datetime.now(UTC)) for m in turn2]
    await compiled.aupdate_state(base, {"messages": copies, "plan": None}, as_node="agent")
    return compiled


@pytest.mark.asyncio
async def test_superseded_turn_never_reaches_the_llm() -> None:
    llm = _ScriptedLLM(script=_script())
    async with make_checkpointer("memory") as cp:
        compiled = await _two_turns_then_supersede_second(llm, cp)
        await compiled.ainvoke(_turn_input("U3", "r3"), config=_cfg("r3"))
    prompt = llm.prompts[-1]
    dumped = _dump(prompt)
    assert "U1" in dumped and "A1-final" in dumped and "U3" in dumped
    assert "U2" not in dumped
    assert "A2-final" not in dumped
    assert GOAL_TWO not in dumped
    assert "tc2" not in dumped
    # 工具对成对消失,没有孤儿 tool_call(否则厂商 400)。
    assert not any(isinstance(m, ToolMessage) for m in prompt)
    assert not any(getattr(m, "tool_calls", None) for m in prompt)
    # plan 已回退到 None。
    assert "## Execution plan" not in dumped


@pytest.mark.asyncio
async def test_negative_control_marks_alone_do_not_hide_anything() -> None:
    """反证:不是「标记本身」让 LangGraph 跳过什么 —— 拿掉过滤器,被标消息**会**进 prompt。

    这条测试在实现里把 filter 行注释掉时必须变红 —— 它就是变异自证的镜像。
    """
    from expert_work.common.conversation_channel import SUPERSEDED_BY

    llm = _ScriptedLLM(script=_script())
    async with make_checkpointer("memory") as cp:
        compiled = await _two_turns_then_supersede_second(llm, cp)
        base: RunnableConfig = {"configurable": {"thread_id": "t", "tenant_id": "tenant"}}
        persisted = list((await compiled.aget_state(base)).values["messages"])
        # 标记确实在检查点里(读面照常看得到这 5 条)。
        assert sum(1 for m in persisted if m.additional_kwargs.get(SUPERSEDED_BY)) == 5
        await compiled.ainvoke(_turn_input("U3", "r3"), config=_cfg("r3"))
    # 但一条都没进 prompt。
    assert not any(m.additional_kwargs.get(SUPERSEDED_BY) for m in llm.prompts[-1])


@pytest.mark.asyncio
async def test_filter_runs_before_the_working_window() -> None:
    """位置判据:过滤必须在 ``working_window`` 之前(spec §3.2),测试**能**测这个位置。

    计划原写「位置由 code review 把关,测试不能测位置」—— 不成立。窗口按
    ``HumanMessage`` 数轮:窗口开在过滤**之后**,被取代的那一轮会占掉一格窗口
    预算,把仍然有效的第一轮挤出去;开在过滤**之前**,窗口只看到两轮、原样放行。
    所以「第一轮还在 prompt 里」就是位置的判据。

    参数:``threshold_pct=0.0`` 让窗口每次必触发;``max_recent_turns=2`` /
    ``keep_first_turn=False`` 让「三轮」与「两轮」的结果不同 —— 三轮时丢掉第一轮。
    """
    window = WorkingWindow(
        context_window=1, threshold_pct=0.0, max_recent_turns=2, keep_first_turn=False
    )
    llm = _ScriptedLLM(script=_script())
    async with make_checkpointer("memory") as cp:
        compiled = await _two_turns_then_supersede_second(llm, cp, working_window=window)
        await compiled.ainvoke(_turn_input("U3", "r3"), config=_cfg("r3"))
    dumped = _dump(llm.prompts[-1])
    # 过滤在窗口之前 → 窗口只数到「第一轮 + 第三轮」两轮,不裁;第一轮活着。
    assert "U1" in dumped and "A1-final" in dumped and "U3" in dumped
    # 过滤本身照常生效。
    assert "U2" not in dumped and "A2-final" not in dumped
