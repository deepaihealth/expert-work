"""B-302 PR 1 守卫(spec §4.1.6 T1-9 后两项、T1-10)—— 新旧代码都绿,咬住写明的改坏。

这里故意不 import 任何 PR 1 新增的符号:运行配置的三个键写成字面量、时间用真时钟加偏移
(本段起跑时刻往前拨),所以旧代码上也能跑,证明这些行为在改动前后一致。
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
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
from orchestrator.tools._guards import GUARD_SINK_KEY


@dataclass
class _LLM:
    script: list[AIMessage]
    prompts: list[list[BaseMessage]] = field(default_factory=list)

    async def __call__(
        self, *, messages: Sequence[BaseMessage], tools: Sequence[ToolSpec], **_: Any
    ) -> AIMessage:
        del tools
        self.prompts.append(list(messages))
        return self.script.pop(0) if self.script else AIMessage(content="done")


@dataclass
class _Probe:
    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(name="probe", description="probe")

    async def call(self, args: Any, *, ctx: ToolContext) -> ToolResult:
        del args, ctx
        return ToolResult(content="ok")


def _call(call_id: str) -> AIMessage:
    return AIMessage(
        content="", tool_calls=[{"name": "probe", "args": {}, "id": call_id, "type": "tool_call"}]
    )


async def _run(
    llm: _LLM, *, limit_s: int, already_s: float, step_count: int, max_steps: int
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    registry = ToolRegistry()
    registry.register(_Probe())
    frames: list[dict[str, Any]] = []

    async def _publish(frame: dict[str, Any]) -> None:
        frames.append(frame)

    async with make_checkpointer("memory") as cp:
        compiled = GraphRunner(checkpointer=cp).compile(
            build_react_graph(llm_caller=llm, tool_registry=registry)
        )
        cfg: RunnableConfig = {
            "configurable": {
                "thread_id": str(uuid4()),
                GUARD_SINK_KEY: _publish,
                # B-302 的运行配置三件套(字面量;旧代码不认识,原样忽略)。
                "run_limit_s": limit_s,
                "run_segment": "seg-guard",
                "run_segment_started_at": time.monotonic() - already_s,
            }
        }
        state = await compiled.ainvoke(
            {
                "messages": [HumanMessage(content="go")],
                "step_count": step_count,
                "max_steps": max_steps,
            },
            config=cfg,
        )
    return dict(state), frames


async def test_t1_9_a_limit_of_86400_does_not_notice_at_90_minutes() -> None:
    """守卫(T1-9):智能体填 86400 = 基本关掉;已经跑了 90 分钟也不提醒、不收尾。

    咬住的改坏:取值规则被改成按平台默认封顶(例如 ``min(limit, 5400)``)—— 那样 90 分钟
    已经越过 100%,这里会收尾、出提醒并发帧。
    """
    llm = _LLM([_call("a"), AIMessage(content="done")])
    state, frames = await _run(llm, limit_s=86_400, already_s=5_400, step_count=0, max_steps=20)

    assert state["exit_reason"] == "text_response"
    assert all("[time limit notice]" not in str(p[-1].content) for p in llm.prompts)
    assert [f for f in frames if f.get("guard") == "deadline"] == []


async def test_t1_10_steps_and_time_spent_together_report_max_steps() -> None:
    """守卫(T1-10):步数与时限同一步用尽,``exit_reason`` 取 ``max_steps``(取值顺序
    步数 → 打转 → token → 时限)。

    咬住的改坏:把时限排到步数前面(``budget_exit_reason`` 里先判 deadline)。
    """
    llm = _LLM([AIMessage(content="wrapped up")])
    state, _ = await _run(llm, limit_s=60, already_s=10_000, step_count=3, max_steps=3)

    assert state["exit_reason"] == "max_steps"
    assert "step budget (3 steps)" in str(llm.prompts[0][-1].content)
