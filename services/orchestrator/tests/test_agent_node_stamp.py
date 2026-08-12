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
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.runnables import RunnableConfig

from expert_work.common.message_stamp import STAMP_CREATED_AT, STAMP_RUN_ID
from expert_work.runtime.checkpointer import make_checkpointer
from expert_work.runtime.middleware import LoopDetectionMiddleware, MiddlewareChain
from orchestrator import GraphRunner, ToolRegistry, build_react_graph


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
