"""agent_node 产出的助手消息带 run_id / created_at(P2 Task 4)。

用真 graph 而非手工 fixture:reconcile / 盖戳这类不变式在手工对齐的
fixture 下会假绿(既有教训 —— 见 memory「reconcile 类不变式要驱动真
graph 集成测」)。``orchestrator/tests`` 下没有 ``conftest.py``、也没有
``build_minimal_agent`` 之类的共享 graph 构造 fixture,这里照
``test_output_dlp_wiring.py`` / ``test_no_progress_stop.py`` 里的同款
用法,直接用 ``build_react_graph`` + ``GraphRunner`` 现场搭最小 graph。

``agent_node`` 的盖戳分两条 return 路径(builder.py 的
``update_mw``/``persisted_messages`` 中间件分支,与
``update_plain``/``emit_messages`` 无中间件分支),两条都要覆盖 —— 挂一个
真实的 ``after_llm_call`` 中间件(``LoopDetectionMiddleware``,单条无
tool_calls 的回复下是纯直通)触发中间件分支。

评审 Important-1/2 补的两条:
- ``test_screen_blocked_response_still_stamped`` —— output-screen block 时
  ``_screen_model_response`` 返回的是全新 ``AIMessage``(不走 ``model_copy``,
  kwargs 全空),证明盖戳确实晚于这次重绑才生效,而不是早绑上去被冲掉。
- ``test_recovery_advisory_carries_stamp_and_hide_from_ui`` —— 之前引用的
  31 个回归用例(``test_recovery_advisory.py`` 等)从没往
  ``config["configurable"]`` 塞 ``run_id``,盖戳在那批里全程是 no-op;这里
  带真实 ``run_id`` 跑出一条 CM-1 advisory,坐实 ``expert_work_hide_from_ui``
  与两个 STAMP 键能同时共存(合并逻辑不互相顶替)。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.runnables import RunnableConfig

from expert_work.common.message_stamp import STAMP_CREATED_AT, STAMP_RUN_ID
from expert_work.common.output_screen import REFUSAL_TEXT
from expert_work.runtime.checkpointer import make_checkpointer
from expert_work.runtime.middleware import LoopDetectionMiddleware, MiddlewareChain
from orchestrator import (
    GraphRunner,
    ToolContext,
    ToolRegistry,
    ToolResult,
    ToolSpec,
    build_react_graph,
)


@dataclass
class _ScriptedLLM:
    """Replies without tool calls so the run ends after one agent step."""

    responses: list[AIMessage]
    calls: int = field(default=0)

    async def __call__(
        self, *, messages: Sequence[BaseMessage], tools: Sequence[object]
    ) -> AIMessage:
        del messages, tools
        idx = self.calls
        self.calls += 1
        return self.responses[idx]


async def _run_one_turn(
    *, run_id: str, after_llm_chain: MiddlewareChain | None
) -> list[BaseMessage]:
    llm = _ScriptedLLM(responses=[AIMessage(content="done")])
    async with make_checkpointer("memory") as cp:
        runner = GraphRunner(checkpointer=cp)
        compiled = runner.compile(
            build_react_graph(
                llm_caller=llm,
                tool_registry=ToolRegistry(),
                after_llm_chain=after_llm_chain,
            )
        )
        cfg: RunnableConfig = {"configurable": {"thread_id": str(uuid4()), "run_id": run_id}}
        state = await compiled.ainvoke(
            {"messages": [HumanMessage(content="hi")], "step_count": 0, "max_steps": 5},
            config=cfg,
        )
    return list(state["messages"])


def _assert_ai_messages_stamped(messages: list[BaseMessage], run_id: str) -> None:
    ai = [m for m in messages if m.type == "ai"]
    assert ai, "graph 没产出助手消息,测试本身无效"
    for m in ai:
        assert m.additional_kwargs[STAMP_RUN_ID] == run_id
        assert m.additional_kwargs[STAMP_CREATED_AT]


@pytest.mark.asyncio
async def test_agent_node_stamps_response_no_middleware() -> None:
    """无中间件路径 —— builder.py 的 ``update_plain``/``emit_messages`` 分支。"""
    run_id = "11111111-1111-1111-1111-111111111111"
    messages = await _run_one_turn(run_id=run_id, after_llm_chain=None)
    _assert_ai_messages_stamped(messages, run_id)


@pytest.mark.asyncio
async def test_agent_node_stamps_response_with_middleware() -> None:
    """中间件路径 —— builder.py 的 ``update_mw``/``persisted_messages`` 分支。

    这条是 Task 4 最容易漏写/漏跑到的一半(盖戳必须放在 ``after_llm_chain``
    调用之后、紧挨 return,早放会被中间件重绑掉)。
    """
    run_id = "22222222-2222-2222-2222-222222222222"
    messages = await _run_one_turn(
        run_id=run_id,
        after_llm_chain=MiddlewareChain.from_middlewares(
            "after_llm_call", [LoopDetectionMiddleware()]
        ),
    )
    _assert_ai_messages_stamped(messages, run_id)


@pytest.mark.asyncio
async def test_screen_blocked_response_still_stamped() -> None:
    """Important-1 —— output-screen block rebinds ``response`` to a brand
    new ``AIMessage`` (``_screen_model_response`` doesn't ``model_copy``,
    so kwargs start empty). The stamp must survive that rebind, i.e. it
    has to run AFTER the screen block, not before."""
    run_id = "44444444-4444-4444-4444-444444444444"
    # Split literal so push protection sees no contiguous provider token.
    leak = "Sure, the key is sk-" + "ant-api03-AbCdEf012345678901234567"
    llm = _ScriptedLLM(responses=[AIMessage(content=leak)])
    async with make_checkpointer("memory") as cp:
        runner = GraphRunner(checkpointer=cp)
        compiled = runner.compile(
            build_react_graph(llm_caller=llm, tool_registry=ToolRegistry(), output_screen=True)
        )
        cfg: RunnableConfig = {"configurable": {"thread_id": str(uuid4()), "run_id": run_id}}
        state = await compiled.ainvoke(
            {"messages": [HumanMessage(content="hi")], "step_count": 0, "max_steps": 5},
            config=cfg,
        )
    last = state["messages"][-1]
    assert last.content == REFUSAL_TEXT, "screen block 没有触发,测试本身无效"
    assert last.additional_kwargs[STAMP_RUN_ID] == run_id
    assert last.additional_kwargs[STAMP_CREATED_AT]


@dataclass
class _FailingSaveArtifact:
    """``save_artifact`` stub that always raises — drives a real CM-1
    recovery advisory (照 ``test_recovery_advisory.py`` 同款 stub)."""

    name: str = "save_artifact"

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
        )

    async def call(self, args: Mapping[str, Any], *, ctx: ToolContext) -> ToolResult:
        del args, ctx
        msg = "disk full"
        raise OSError(msg)


@pytest.mark.asyncio
async def test_recovery_advisory_carries_stamp_and_hide_from_ui() -> None:
    """Important-2 —— a real CM-1 recovery advisory, with a real ``run_id``
    threaded through config, must carry ``expert_work_hide_from_ui`` AND
    both STAMP keys at once (``stamp_message`` merges into existing
    ``additional_kwargs`` rather than replacing them)."""
    run_id = "33333333-3333-3333-3333-333333333333"
    registry = ToolRegistry()
    registry.register(_FailingSaveArtifact())
    llm = _ScriptedLLM(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "save_artifact",
                        "args": {"name": "x.md"},
                        "id": "tc-1",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="done"),
        ]
    )
    async with make_checkpointer("memory") as cp:
        runner = GraphRunner(checkpointer=cp)
        compiled = runner.compile(build_react_graph(llm_caller=llm, tool_registry=registry))
        cfg: RunnableConfig = {"configurable": {"thread_id": str(uuid4()), "run_id": run_id}}
        state = await compiled.ainvoke(
            {"messages": [HumanMessage(content="start")], "step_count": 0, "max_steps": 5},
            config=cfg,
        )
    advisory = next(
        (
            m
            for m in state["messages"]
            if isinstance(m, HumanMessage) and "<recovery-advisory>" in str(m.content)
        ),
        None,
    )
    assert advisory is not None, "没有触发 recovery advisory,测试本身无效"
    assert advisory.additional_kwargs.get("expert_work_hide_from_ui") is True
    assert advisory.additional_kwargs[STAMP_RUN_ID] == run_id
    assert advisory.additional_kwargs[STAMP_CREATED_AT]
