"""B-131 —— 本轮正常答完时,平台把计划里所有未完成的步骤标为完成。

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
from orchestrator import GraphRunner, ToolRegistry, ToolSpec, build_react_graph, make_reflect_node
from orchestrator.context import plan_md_digest
from orchestrator.graph_builder import render_plan
from orchestrator.graph_builder.planner import complete_open_steps
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


def test_every_open_step_becomes_completed() -> None:
    # 重放实测:模型常常建完计划就不再更新,最后留着 in_progress 和一串 pending。
    out = complete_open_steps(_plan("completed", "in_progress", "pending"))
    assert out is not None
    assert _statuses(out) == ["completed", "completed", "completed"]


def test_pending_only_plan_is_completed_too() -> None:
    out = complete_open_steps(_plan("completed", "pending", "pending"))
    assert out is not None
    assert _statuses(out) == ["completed", "completed", "completed"]


def test_all_completed_or_no_plan_returns_none() -> None:
    assert complete_open_steps(_plan("completed", "completed")) is None
    assert complete_open_steps(None) is None


def test_recitation_tells_model_not_to_spend_a_step_on_ticking() -> None:
    text = render_plan(_plan("pending"))
    assert "same response as that step's first action" in text
    assert "The one update you may skip is the last" in text
    assert "any step not yet completed is marked completed automatically" in text


def test_update_plan_description_says_the_same() -> None:
    desc = UpdatePlanTool().spec.description
    assert "same response as that step's first action" in desc
    assert "The one update you may skip is the last" in desc
    assert "any step not yet completed is marked completed automatically" in desc


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
async def test_final_text_reply_completes_open_steps() -> None:
    state = await _run(AIMessage(content="all done"))
    assert _statuses(state["plan"]) == ["completed", "completed", "completed"]


@pytest.mark.asyncio
async def test_budget_exit_leaves_plan_untouched() -> None:
    state = await _run(AIMessage(content="wrapping up"), step_count=5, max_steps=5)
    assert _statuses(state["plan"]) == ["completed", "in_progress", "pending"]


@pytest.mark.asyncio
async def test_final_text_reply_completes_open_steps_on_middleware_path() -> None:
    state = await _run(AIMessage(content="all done"), with_middleware=True)
    assert _statuses(state["plan"]) == ["completed", "completed", "completed"]


@pytest.mark.asyncio
async def test_budget_exit_leaves_plan_untouched_on_middleware_path() -> None:
    state = await _run(
        AIMessage(content="wrapping up"), step_count=5, max_steps=5, with_middleware=True
    )
    assert _statuses(state["plan"]) == ["completed", "in_progress", "pending"]


# ---------------------------------------------------------------- review round 1


@dataclass
class _Writer:
    writes: dict[str, str] = field(default_factory=dict)

    async def write(self, *, rel: str, content: str) -> None:
        self.writes[rel] = content


_THREAD = "aaaaaaaa-bbbb-cccc-dddd-eeeeffff0131"


@pytest.mark.asyncio
async def test_auto_completed_plan_is_projected_to_plan_md() -> None:
    """标完成之后必须把 PLAN.md 同步一次;否则下一轮 workspace_ingest 读回的是旧文件,
    会当成人工修改把步骤改回「进行中」(review round 1, finding 1)。"""
    writer = _Writer()
    async with make_checkpointer("memory") as cp:
        compiled = GraphRunner(checkpointer=cp).compile(
            build_react_graph(
                llm_caller=_LLM(AIMessage(content="all done")),
                tool_registry=ToolRegistry(),
                workspace_writer_factory=lambda _ctx: writer,
            )
        )
        cfg: RunnableConfig = {"configurable": {"thread_id": _THREAD}}
        state = await compiled.ainvoke(
            {
                "messages": [HumanMessage(content="do it")],
                "step_count": 0,
                "max_steps": 5,
                "plan": _plan("completed", "in_progress", "pending"),
            },
            config=cfg,
        )
    assert _statuses(state["plan"]) == ["completed", "completed", "completed"]
    plan_md = writer.writes[f"threads/{_THREAD}/PLAN.md"]
    assert "[x] 2." in plan_md and "[~] 2." not in plan_md
    assert "[x] 3." in plan_md and "[ ] 3." not in plan_md
    assert state.get("last_projection_hash")
    # 收尾投影写进去的那份 PLAN.md 的摘要也要落检查点,下一轮才认得出它没被人动过。
    assert state.get("last_plan_md_digest") == plan_md_digest(plan_md)


@dataclass
class _SeqLLM:
    replies: list[AIMessage]
    seen: list[list[BaseMessage]] = field(default_factory=list)

    async def __call__(
        self, *, messages: Sequence[BaseMessage], tools: Sequence[ToolSpec]
    ) -> AIMessage:
        del tools
        self.seen.append(list(messages))
        return self.replies[len(self.seen) - 1]


@pytest.mark.asyncio
async def test_reflect_revise_does_not_complete_steps_before_the_real_end() -> None:
    """复查判「要返工」时这一轮没结束:返工那次调用看到的计划必须仍是「进行中」
    (review round 1, finding 3)。复查通过、真正结束时才标完成。"""
    agent_llm = _SeqLLM([AIMessage(content="first answer"), AIMessage(content="second answer")])
    critic = _SeqLLM(
        [
            AIMessage(content='{"verdict": "revise", "critique": "incomplete"}'),
            AIMessage(content='{"verdict": "accept", "critique": "ok"}'),
        ]
    )
    async with make_checkpointer("memory") as cp:
        compiled = GraphRunner(checkpointer=cp).compile(
            build_react_graph(
                llm_caller=agent_llm,
                tool_registry=ToolRegistry(),
                reflect_node=make_reflect_node(critic, budget=2),
            )
        )
        cfg: RunnableConfig = {"configurable": {"thread_id": str(uuid4())}}
        state = await compiled.ainvoke(
            {
                "messages": [HumanMessage(content="do it")],
                "step_count": 0,
                "max_steps": 5,
                "plan": _plan("completed", "in_progress", "pending"),
            },
            config=cfg,
        )
    assert len(agent_llm.seen) == 2
    second_prompt = "\n".join(str(m.content) for m in agent_llm.seen[1])
    assert "[~] 2." in second_prompt
    assert _statuses(state["plan"]) == ["completed", "completed", "completed"]


# ---------------------------------------------------------------- CI round 1


@pytest.mark.asyncio
@pytest.mark.parametrize("plan", [None, _plan("completed", "completed")])
async def test_injected_agent_message_leaves_a_clean_turn_end(plan: Plan | None) -> None:
    """定时任务投递 / 重新生成用 ``aupdate_state(as_node="agent")`` 补一条消息;
    计划没有要收尾的步骤时图必须直接结束,不能停在「plan_close 待跑」。"""
    async with make_checkpointer("memory") as cp:
        compiled = GraphRunner(checkpointer=cp).compile(
            build_react_graph(llm_caller=_LLM(AIMessage(content="x")), tool_registry=ToolRegistry())
        )
        cfg: RunnableConfig = {"configurable": {"thread_id": str(uuid4())}}
        await compiled.aupdate_state(
            cfg,
            {
                "messages": [HumanMessage(content="hi"), AIMessage(content="delivered")],
                "plan": plan,
                "exit_reason": "text_response",
            },
            as_node="agent",
        )
        snap = await compiled.aget_state(cfg)
    assert snap.next == ()
