"""新一轮与上一轮的待审批 —— 图状态这一侧的两件事(班车 2 安全修复)。

会话是长线程,检查点跨轮保留。审批三件套 ``pending_approval`` /
``approval_resume`` / ``approval_outcome`` 却只属于**一轮**:上一轮停在审批上
留下的 ``pending_approval`` 曾让下一轮的 ``tools_node`` 跳过 action screening
与审批门、把门控工具直接执行掉。

* :data:`APPROVAL_TURN_RESET` —— 每一轮的图输入都带上它,把三件套清零
  (省略键时 LangGraph 保留检查点里的旧值)。入口在 control-plane 的
  ``build_run_graph_input`` / ``replay_graph_input``;``tools_node`` 自己也会
  清掉读到的陈旧值,两道防线互不依赖。
* :func:`close_voided_turn` —— 新一轮作废上一轮的待审批时,收口被作废的那一轮:
  它最后那条助手消息的工具调用一个都没执行,逐个补一条「已作废」的工具结果
  (与人工拒绝同一个 ``[approval rejected]`` 前缀),三件套清零,
  ``approval_outcome="rejected"`` 让这次写入之后检查点上没有待执行的节点。
  不补的话,下一轮的历史里会留着没有结果的工具调用,严格校验配对的模型厂商
  (OpenAI / Anthropic)会直接拒绝整段请求。

只在检查点**尾巴**正是被作废那一轮的助手消息时才写:尾巴已经是更新的一轮时,
往末尾追加结果既配不上对(结果必须紧跟发起调用的那条消息),又会改到别人的一轮。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from types import MappingProxyType
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langchain_core.runnables import RunnableConfig

from expert_work.common.conversation_channel import is_superseded
from expert_work.common.supersede import stamped_run_id

__all__ = [
    "APPROVAL_TURN_RESET",
    "VOIDED_APPROVAL_CONTENT",
    "close_voided_turn",
    "voided_turn_update",
]

#: 每一轮图输入里的审批通道清零值。只读映射 —— 调用方用 ``**`` 展开进自己的 dict。
APPROVAL_TURN_RESET: Mapping[str, None] = MappingProxyType(
    {"pending_approval": None, "approval_resume": None, "approval_outcome": None}
)

#: 被作废那一轮的每个工具调用拿到的结果正文。前缀与人工拒绝
#: (``graph_builder._approval.apply_resume_decision``)一致:模型读到的是同一类
#: 信号 —— 这一步没有执行。
VOIDED_APPROVAL_CONTENT = (
    "[approval rejected] voided: a new message arrived before this action was "
    "approved, so it was not run"
)

#: 补上的工具结果用确定的 id:``add_messages`` 同 id 原地替换,重复收口不会追加两份。
_VOIDED_ID_PREFIX = "approval-voided-"


def voided_turn_update(messages: Sequence[BaseMessage], *, run_id: str) -> dict[str, Any] | None:
    """收口 ``run_id`` 那一轮要写回检查点的更新;不该写时返回 ``None``。

    只认一种形状:最后一条是 ``run_id`` 盖戳、带工具调用、没被取代的助手消息。
    """
    if not messages:
        return None
    tail = messages[-1]
    if not isinstance(tail, AIMessage) or is_superseded(tail):
        return None
    if stamped_run_id(tail) != run_id:
        return None
    calls = list(tail.tool_calls or [])
    if not calls:
        return None
    results = [
        ToolMessage(
            id=f"{_VOIDED_ID_PREFIX}{call.get('id') or index}",
            content=VOIDED_APPROVAL_CONTENT,
            tool_call_id=str(call.get("id") or ""),
            status="error",
            name=call.get("name"),
        )
        for index, call in enumerate(calls)
    ]
    return {
        "messages": results,
        "pending_approval": None,
        "approval_resume": None,
        # 与声明式拒绝同一个终止信号:``_after_tools`` 据此路由到 END,
        # 这次写入之后检查点上没有待执行的节点。下一轮的图输入会把它清零。
        "approval_outcome": "rejected",
    }


async def close_voided_turn(graph: Any, config: RunnableConfig, *, run_id: str) -> int:
    """收口被作废的那一轮,返回补了几条工具结果(``0`` = 没有写检查点)。

    调用方必须已经赢下审批的作废 CAS —— 否则会和同时进行的裁定续跑抢同一个检查点。
    """
    snapshot = await graph.aget_state(config)
    values = snapshot.values if isinstance(snapshot.values, dict) else {}
    update = voided_turn_update(values.get("messages") or [], run_id=run_id)
    if update is None:
        return 0
    # ``as_node="tools"``:这些结果就是 tools 节点本该写的那一批,随后的
    # ``_after_tools`` 读到 ``approval_outcome`` 路由到 END。
    await graph.aupdate_state(config, update, as_node="tools")
    return len(update["messages"])
