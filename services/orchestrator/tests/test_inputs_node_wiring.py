"""B-61 Task 3 —— inputs 节点接进 ReAct 图(而不只是节点自身的单元测试)。

``test_inputs_node.py`` 直接调用 ``make_inputs_node`` 返回的裸函数,从不经过
``build_react_graph``/``agent_factory`` 这两处真正的接线点。这里补一条最短路径的
真图测试,照 ``test_workspace_ingest_wiring.py`` 的形状 —— 用仓库里现成的
``RecordingSandboxRuntime``(真的实现 acquire/exec/release,不是本模块自造的假
对象),证明 ``builder.py`` 的 ``inputs_node`` 参数与 START 侧的边确实把节点接进了
图,run 一旦起来就会打这条通道,不是只在直接单测里成立。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from uuid import uuid4

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.runnables import RunnableConfig

from expert_work.protocol import PromptVariableSpec
from expert_work.runtime.checkpointer import make_checkpointer
from orchestrator import GraphRunner, ToolRegistry, ToolSpec, build_react_graph
from orchestrator.graph_builder import make_inputs_node
from orchestrator.tools.sandbox import RecordingSandboxRuntime, SandboxOutcome


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
        return self.responses[idx]


def _ok_outcome() -> SandboxOutcome:
    # SandboxWorkspaceWriter.write() 解析 stdout 上的 JSON envelope;预拉那次
    # exec 不看 stdout,这个通用「成功」outcome 对两种调用都成立。
    return SandboxOutcome(stdout='{"ok": true}', stderr="", exit_code=0, timed_out=False)


async def test_inputs_node_actually_fires_when_wired_into_the_real_graph() -> None:
    client = RecordingSandboxRuntime(outcome=_ok_outcome())
    node = make_inputs_node(client=client, variables=(PromptVariableSpec(name="org_logo"),))
    llm = _ScriptedLLM(responses=[AIMessage(content="done")])
    run_id = uuid4()
    async with make_checkpointer("memory") as cp:
        compiled = GraphRunner(checkpointer=cp).compile(
            build_react_graph(llm_caller=llm, tool_registry=ToolRegistry(), inputs_node=node)
        )
        cfg: RunnableConfig = {
            "configurable": {
                "thread_id": str(uuid4()),
                "tenant_id": str(uuid4()),
                "user_id": str(uuid4()),
                "run_id": str(run_id),
                "prompt_inputs": {"org_logo": "https://x/a.jpg"},
            }
        }
        await compiled.ainvoke(
            {"messages": [HumanMessage(content="go")], "step_count": 0, "max_steps": 5},
            config=cfg,
        )
    # 一次写 inputs.json、一次跑预拉——证明节点真的在 START 侧被图调用了一次,
    # 不是只在直接调用裸函数的单测里成立。
    assert len(client.execs) == 2
    assert f"inputs/{run_id}/inputs.json" in client.execs[0][1]


async def test_inputs_node_absent_means_zero_sandbox_calls() -> None:
    """``inputs_node=None``(没有声明变量的 agent)——图里连这个节点都不存在,
    ``RecordingSandboxRuntime`` 上不该有任何 acquire/exec 记录。"""
    client = RecordingSandboxRuntime(outcome=_ok_outcome())
    llm = _ScriptedLLM(responses=[AIMessage(content="done")])
    async with make_checkpointer("memory") as cp:
        compiled = GraphRunner(checkpointer=cp).compile(
            build_react_graph(llm_caller=llm, tool_registry=ToolRegistry(), inputs_node=None)
        )
        cfg: RunnableConfig = {
            "configurable": {
                "thread_id": str(uuid4()),
                "tenant_id": str(uuid4()),
                "user_id": str(uuid4()),
                "run_id": str(uuid4()),
                "prompt_inputs": {"org_logo": "https://x/a.jpg"},
            }
        }
        await compiled.ainvoke(
            {"messages": [HumanMessage(content="go")], "step_count": 0, "max_steps": 5},
            config=cfg,
        )
    assert client.acquired == []
    assert client.execs == []
