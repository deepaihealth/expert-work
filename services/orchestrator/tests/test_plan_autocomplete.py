"""B-131 —— 本轮正常答完时,平台把计划里仍「进行中」的步骤标为完成。

模型不用为了打勾单独花一次调用:更新计划要跟下一步动作放在同一次回复里,
最终回复前也不必再专门标一次完成。只在自然结束(``text_response``)时生效;
步数 / 预算耗尽这类被迫结束不动计划 —— 那些步骤并没有做完。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.runnables import RunnableConfig

from expert_work.protocol import Plan
from expert_work.runtime.checkpointer import make_checkpointer
from expert_work.runtime.middleware import LoopDetectionMiddleware, MiddlewareChain
from orchestrator import GraphRunner, ToolRegistry, ToolSpec, build_react_graph
from orchestrator.graph_builder import render_plan
from orchestrator.graph_builder.planner import complete_in_progress_steps
from orchestrator.tools.update_plan import UpdatePlanTool


def _plan(*statuses: str) -> Plan:
    return Plan.model_validate(
        {
            "goal": "g",
            "steps": [
                {"id": str(i), "description": f"s{i}", "status": s}
                for i, s in enumerate(statuses, start=1)
            ],
        }
    )


def _statuses(plan: Plan) -> list[str]:
    return [s.status for s in plan.steps]


def test_in_progress_becomes_completed_pending_untouched() -> None:
    out = complete_in_progress_steps(_plan("completed", "in_progress", "pending"))
    assert out is not None
    assert _statuses(out) == ["completed", "completed", "pending"]


def test_no_in_progress_step_returns_none() -> None:
    assert complete_in_progress_steps(_plan("completed", "pending")) is None
    assert complete_in_progress_steps(None) is None


def test_recitation_tells_model_not_to_spend_a_step_on_ticking() -> None:
    text = render_plan(_plan("pending"))
    assert "same response as your next action" in text
    assert "marked completed automatically" in text


def test_update_plan_description_says_the_same() -> None:
    desc = UpdatePlanTool().spec.description
    assert "same response" in desc
    assert "marked completed automatically" in desc


@dataclass
class _LLM:
    reply: AIMessage
    seen: list[list[BaseMessage]] = field(default_factory=list)

    async def __call__(
        self, *, messages: Sequence[BaseMessage], tools: Sequence[ToolSpec]
    ) -> AIMessage:
        del tools
        self.seen.append(list(messages))
        return self.reply


async def _run(
    reply: AIMessage, *, step_count: int = 0, max_steps: int = 5, with_middleware: bool = False
) -> dict:
    # ``with_middleware`` drives agent_node's middleware exit (``update_mw``) —
    # the path production agents take — instead of the plain exit.
    chain = (
        MiddlewareChain.from_middlewares("after_llm_call", [LoopDetectionMiddleware()])
        if with_middleware
        else None
    )
    async with make_checkpointer("memory") as cp:
        compiled = GraphRunner(checkpointer=cp).compile(
            build_react_graph(
                llm_caller=_LLM(reply), tool_registry=ToolRegistry(), after_llm_chain=chain
            )
        )
        cfg: RunnableConfig = {"configurable": {"thread_id": str(uuid4())}}
        return await compiled.ainvoke(
            {
                "messages": [HumanMessage(content="do it")],
                "step_count": step_count,
                "max_steps": max_steps,
                "plan": _plan("completed", "in_progress", "pending"),
            },
            config=cfg,
        )


@pytest.mark.asyncio
async def test_final_text_reply_completes_in_progress_steps() -> None:
    state = await _run(AIMessage(content="all done"))
    assert _statuses(state["plan"]) == ["completed", "completed", "pending"]


@pytest.mark.asyncio
async def test_budget_exit_leaves_plan_untouched() -> None:
    state = await _run(AIMessage(content="wrapping up"), step_count=5, max_steps=5)
    assert _statuses(state["plan"]) == ["completed", "in_progress", "pending"]


@pytest.mark.asyncio
async def test_final_text_reply_completes_in_progress_steps_on_middleware_path() -> None:
    state = await _run(AIMessage(content="all done"), with_middleware=True)
    assert _statuses(state["plan"]) == ["completed", "completed", "pending"]


@pytest.mark.asyncio
async def test_budget_exit_leaves_plan_untouched_on_middleware_path() -> None:
    state = await _run(
        AIMessage(content="wrapping up"), step_count=5, max_steps=5, with_middleware=True
    )
    assert _statuses(state["plan"]) == ["completed", "in_progress", "pending"]
