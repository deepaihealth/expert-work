"""新一轮与上一轮的待审批 —— 图状态这一侧(班车 2 安全修复)。

会话是长线程,检查点跨轮保留。审批三件套 ``pending_approval`` /
``approval_resume`` / ``approval_outcome`` 却只属于**一轮**:上一轮停在审批上
留下的 ``pending_approval`` 曾让下一轮的 ``tools_node`` 跳过 action screening
与审批门、把门控工具直接执行掉。

* :data:`APPROVAL_TURN_RESET` —— 每一轮的图输入都带上它,把三件套清零
  (省略键时 LangGraph 保留检查点里的旧值)。入口在 control-plane 的
  ``build_run_graph_input`` / ``replay_graph_input``;``tools_node`` 自己也会
  清掉读到的陈旧值,两道防线互不依赖。
* :func:`repair_unanswered_tail` —— 新一轮开跑前**按历史的形状**收口上一轮:
  最后一条是带工具调用、没有结果的助手消息,而它所属的那一轮已经结束(由调用方
  判断,并给出结果正文),就逐个补一条错误结果,三件套清零,
  ``approval_outcome="rejected"`` 让这次写入之后检查点上没有待执行的节点。
  不补的话,新一轮的历史里留着没有结果的工具调用,严格校验配对的模型厂商会直接
  拒绝整段请求,而且之后每一轮都一样。作废审批、审批登记窗口、作废中途失败、
  取消落在「模型给出调用」与「工具执行」之间,都是这同一个形状。
* :func:`pending_request_id` —— 审批续跑写检查点之前核对:检查点里等着的还是
  不是这条审批。新一轮的图输入总会清掉 ``pending_approval``,所以任何更新的一轮
  都过不了这道核对。

只在检查点**尾巴**上补:尾巴已经是更新的一轮时,往末尾追加结果既配不上对
(结果必须紧跟发起调用的那条消息),又会改到别人的一轮。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from types import MappingProxyType
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langchain_core.runnables import RunnableConfig

from expert_work.common.conversation_channel import is_superseded
from expert_work.common.supersede import stamped_run_id
from expert_work.protocol import ApprovalRequest

__all__ = [
    "APPROVAL_TURN_RESET",
    "UNRUN_TOOL_CALL_CONTENT",
    "VOIDED_APPROVAL_CONTENT",
    "pending_request_id",
    "repair_unanswered_tail",
    "unanswered_tail_run_id",
    "voided_turn_update",
]

#: 每一轮图输入里的审批通道清零值。只读映射 —— 调用方用 ``**`` 展开进自己的 dict。
APPROVAL_TURN_RESET: Mapping[str, None] = MappingProxyType(
    {"pending_approval": None, "approval_resume": None, "approval_outcome": None}
)

#: 等审批的那一轮被作废时,它的每个工具调用拿到的结果正文。前缀与人工拒绝
#: (``graph_builder._approval.apply_resume_decision``)一致:模型读到的是同一类
#: 信号 —— 这一步没有执行。
VOIDED_APPROVAL_CONTENT = (
    "[approval rejected] voided: a new message arrived before this action was "
    "approved, so it was not run"
)

#: 那一轮不是停在审批上、而是在工具执行之前就结束了(取消 / 出错)时的结果正文。
UNRUN_TOOL_CALL_CONTENT = "[not run] the turn ended before this tool call was executed"

#: 补上的工具结果用确定的 id:``add_messages`` 同 id 原地替换,重复收口不会追加两份。
_VOIDED_ID_PREFIX = "approval-voided-"

#: 给定尾巴所属 run 的 id,返回结果正文;``None`` = 那一轮还没结束,不补。
ContentFor = Callable[[str], Awaitable[str | None]]


def unanswered_tail_run_id(messages: Sequence[BaseMessage]) -> str | None:
    """尾巴是盖了 run 戳、带工具调用、没被取代的助手消息时,返回那个 run id。"""
    if not messages:
        return None
    tail = messages[-1]
    if not isinstance(tail, AIMessage) or is_superseded(tail) or not tail.tool_calls:
        return None
    return stamped_run_id(tail)


def voided_turn_update(
    messages: Sequence[BaseMessage],
    *,
    run_id: str,
    content: str = VOIDED_APPROVAL_CONTENT,
) -> dict[str, Any] | None:
    """收口 ``run_id`` 那一轮要写回检查点的更新;尾巴不是那一轮的就返回 ``None``。"""
    if unanswered_tail_run_id(messages) != run_id:
        return None
    tail = messages[-1]
    if not isinstance(tail, AIMessage):  # pragma: no cover - 上一行已确认
        return None
    results = [
        ToolMessage(
            id=f"{_VOIDED_ID_PREFIX}{call.get('id') or index}",
            content=content,
            tool_call_id=str(call.get("id") or ""),
            status="error",
            name=call.get("name"),
        )
        for index, call in enumerate(tail.tool_calls)
    ]
    return {
        "messages": results,
        "pending_approval": None,
        "approval_resume": None,
        # 与声明式拒绝同一个终止信号:``_after_tools`` 据此路由到 END,
        # 这次写入之后检查点上没有待执行的节点。下一轮的图输入会把它清零。
        "approval_outcome": "rejected",
    }


def pending_request_id(values: Mapping[str, Any]) -> str | None:
    """检查点里正在等裁定的审批请求的 ``request_id``;没有就 ``None``。"""
    raw = values.get("pending_approval")
    if isinstance(raw, ApprovalRequest):
        return raw.request_id
    if isinstance(raw, Mapping):
        request_id = raw.get("request_id")
        return request_id if isinstance(request_id, str) else None
    return None


async def repair_unanswered_tail(
    graph: Any, config: RunnableConfig, *, content_for: ContentFor
) -> int:
    """按历史的形状收口上一轮,返回补了几条工具结果(``0`` = 没有写检查点)。

    ``approval_resume`` 非空时一律不动:一条裁定已经写进来、续跑还没把它用掉,
    那一轮还在进行中。

    ``config`` 只带会话(``thread_id`` / ``tenant_id``),**不要**带新一轮的
    ``run_id``:LangGraph 会把紧随其后、``run_id`` 与最新检查点元数据相同的图输入
    当成重入同一次运行,直接丢掉新一轮的输入。
    """
    snapshot = await graph.aget_state(config)
    values = snapshot.values if isinstance(snapshot.values, dict) else {}
    if values.get("approval_resume") is not None:
        return 0
    messages = values.get("messages") or []
    run_id = unanswered_tail_run_id(messages)
    if run_id is None:
        return 0
    content = await content_for(run_id)
    if content is None:
        return 0
    update = voided_turn_update(messages, run_id=run_id, content=content)
    if update is None:  # pragma: no cover - 上面刚按同一份消息确认过尾巴
        return 0
    # ``as_node="tools"``:这些结果就是 tools 节点本该写的那一批,随后的
    # ``_after_tools`` 读到 ``approval_outcome`` 路由到 END。
    await graph.aupdate_state(config, update, as_node="tools")
    return len(update["messages"])
