"""本轮附件必须活过检查点 —— 审批续跑 / orphan 复活都是 graph_input=None。"""

from __future__ import annotations

from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph

from orchestrator.state import AgentState

_SEEN: list[dict[str, Any]] = []


def _node(state: AgentState) -> dict[str, Any]:
    _SEEN.append(
        {
            "turn_documents": state.get("turn_documents"),
            "turn_image_refs": state.get("turn_image_refs"),
        }
    )
    return {"messages": [AIMessage(content="ok")]}


def _graph(*, pause_before_second: bool = False):
    """一条两节点的图。``pause_before_second`` 在第二个节点前中断 —— 模型化
    审批闸:run 停住,续跑时以 ``graph_input=None`` 从检查点接着走。"""
    g = StateGraph(AgentState)
    g.add_node("first", _node)
    g.add_node("second", _node)
    g.add_edge(START, "first")
    g.add_edge("first", "second")
    g.add_edge("second", END)
    return g.compile(
        checkpointer=InMemorySaver(),
        interrupt_before=["second"] if pause_before_second else [],
    )


@pytest.mark.asyncio
async def test_attachments_survive_a_checkpoint_resume() -> None:
    """``graph_input=None`` 续跑靠检查点恢复 state —— 附件必须还在。

    这正是把附件放进 ``AgentState`` 而不是 ``config["configurable"]`` 的理由:
    审批续跑与 orphan 复活都不带新输入,config 那条路上的值一去不返。
    """
    _SEEN.clear()
    graph = _graph(pause_before_second=True)
    cfg = {"configurable": {"thread_id": "t1"}}

    await graph.ainvoke(
        {
            "messages": [HumanMessage(content="看看这份文件")],
            "step_count": 0,
            "max_steps": 3,
            "turn_documents": ["uploads/a.docx"],
            "turn_image_refs": ["expert_work://image/x"],
        },
        config=cfg,
    )
    assert len(_SEEN) == 1  # 停在审批闸上,第二个节点还没跑

    await graph.ainvoke(None, config=cfg)  # 续跑:不带新输入,只有检查点

    assert len(_SEEN) == 2
    assert _SEEN[1]["turn_documents"] == ["uploads/a.docx"]
    assert _SEEN[1]["turn_image_refs"] == ["expert_work://image/x"]


@pytest.mark.asyncio
async def test_a_later_turn_does_not_inherit_the_previous_turns_attachments() -> None:
    """同一线程的下一轮必须看到自己的附件,不是上一轮的。

    这是搬去 state 换来的风险:对话是长线程,若某一轮**省略**这两个键,
    LangGraph 会保留检查点里的旧值。所以 ``build_run_graph_input`` 每一轮
    都写,哪怕是空列表 —— 这条钉住那个不变式。
    """
    _SEEN.clear()
    graph = _graph()
    cfg = {"configurable": {"thread_id": "t2"}}

    base = {"step_count": 0, "max_steps": 3}
    await graph.ainvoke(
        {
            **base,
            "messages": [HumanMessage(content="第一轮")],
            "turn_documents": ["uploads/a.docx"],
        },
        config=cfg,
    )
    await graph.ainvoke(
        {**base, "messages": [HumanMessage(content="第二轮")], "turn_documents": []},
        config=cfg,
    )

    assert _SEEN[0]["turn_documents"] == ["uploads/a.docx"]
    assert _SEEN[-1]["turn_documents"] == []
