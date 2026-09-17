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
  取消落在「模型给出调用」与「工具执行」之间、续跑还没用掉裁定就结束了,都是这同一个
  形状。
* :func:`pending_request_binding` —— 审批续跑写检查点之前核对:检查点里等着的还是
  不是这条审批(新一轮的图输入总会清掉 ``pending_approval``,任何更新的一轮都过不了),
  并把被批请求的身份抄进裁定,图里再核对一次(``_approval._verdict_is_for_this_turn``)。

只在检查点**尾巴**上补:尾巴已经是更新的一轮时,往末尾追加结果既配不上对
(结果必须紧跟发起调用的那条消息),又会改到别人的一轮。
"""

from __future__ import annotations

from collections.abc import Awaitable, Mapping, Sequence
from types import MappingProxyType
from typing import Any, Protocol

from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langchain_core.runnables import RunnableConfig

from expert_work.common.conversation_channel import is_superseded
from expert_work.common.supersede import stamped_run_id
from expert_work.protocol import ApprovalRequest

__all__ = [
    "APPROVAL_TURN_RESET",
    "UNRECORDED_TOOL_CALL_CONTENT",
    "VOIDED_APPROVAL_CONTENT",
    "ContentFor",
    "pending_request_binding",
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

#: 那一轮不是停在审批上、而是在留下结果之前就结束了(取消 / 出错 / 续跑没跑起来)时的
#: 结果正文。措辞刻意保守:工具可能根本没执行,也可能执行到一半、或者已经执行完只是
#: 结果没来得及记下(并行的兄弟调用、执行中被取消)—— 平台不知道是哪一种。
UNRECORDED_TOOL_CALL_CONTENT = (
    "[no result] no result was recorded for this call; it may not have completed "
    "- check before retrying"
)

#: 补上的工具结果用确定的 id:``add_messages`` 同 id 原地替换,重复收口不会追加两份。
_VOIDED_ID_PREFIX = "approval-voided-"


class ContentFor(Protocol):
    """给定尾巴所属 run 的 id,返回结果正文;``None`` = 那一轮还没结束,不补。

    ``verdict_waiting``:检查点里还有一条没被用掉的裁定(``approval_resume``)。
    这时只有「本该用掉它的那个续跑已经结束」才能补。
    """

    def __call__(self, run_id: str, /, *, verdict_waiting: bool) -> Awaitable[str | None]: ...


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


def pending_request_binding(values: Mapping[str, Any]) -> dict[str, Any] | None:
    """检查点里正在等裁定的审批请求的身份;没有就 ``None``。

    返回 ``request_id`` / ``action_summary`` / ``tool_call_id`` / ``tool_call_index``,
    续跑把它们原样抄进 ``approval_resume``,图里据此核对裁定属于哪一轮。旧检查点里
    缺的项给空值(图里跳过那一项)。
    """
    raw = values.get("pending_approval")
    if isinstance(raw, ApprovalRequest):
        fields: Mapping[str, Any] = raw.model_dump()
    elif isinstance(raw, Mapping):
        fields = raw
    else:
        return None
    request_id = fields.get("request_id")
    if not isinstance(request_id, str) or not request_id:
        return None
    summary = fields.get("action_summary")
    call_id = fields.get("tool_call_id")
    index = fields.get("tool_call_index")
    return {
        "request_id": request_id,
        "action_summary": summary if isinstance(summary, str) else None,
        "tool_call_id": call_id if isinstance(call_id, str) else "",
        "tool_call_index": index if isinstance(index, int) else None,
    }


async def repair_unanswered_tail(
    graph: Any, config: RunnableConfig, *, content_for: ContentFor
) -> int:
    """按历史的形状收口上一轮,返回补了几条工具结果(``0`` = 没有写检查点)。

    ``approval_resume`` 非空(一条裁定写进来了、还没被用掉)时,交给调用方判断本该
    用掉它的续跑是否还在进行 —— 还在就不动;已经结束(取消在它跑第一步之前、被判
    孤儿后收成失败……)就照常补,补的同时把那条过期的裁定清掉。

    ``config`` 只带会话(``thread_id`` / ``tenant_id``),**不要**带新一轮的
    ``run_id``:LangGraph 会把紧随其后、``run_id`` 与最新检查点元数据相同的图输入
    当成重入同一次运行,直接丢掉新一轮的输入。
    """
    snapshot = await graph.aget_state(config)
    values = snapshot.values if isinstance(snapshot.values, dict) else {}
    messages = values.get("messages") or []
    run_id = unanswered_tail_run_id(messages)
    if run_id is None:
        return 0
    content = await content_for(run_id, verdict_waiting=values.get("approval_resume") is not None)
    if content is None:
        return 0
    update = voided_turn_update(messages, run_id=run_id, content=content)
    if update is None:  # pragma: no cover - 上面刚按同一份消息确认过尾巴
        return 0
    # ``as_node="tools"``:这些结果就是 tools 节点本该写的那一批,随后的
    # ``_after_tools`` 读到 ``approval_outcome`` 路由到 END。
    await graph.aupdate_state(config, update, as_node="tools")
    return len(update["messages"])
