"""一轮(run)→ 条目列表的核心推导 —— 三条产出路径共用的**唯一**一个函数。

设计见 ``docs/superpowers/specs/2026-08-25-conversation-items-design.md``。
实时 SSE、单 run 回放、会话历史三条路径都能提供「这一轮的消息 + 辅助信号」
这一个输入,所以推导只写一份;任何一条路径自己再算一遍,第三方立刻会看到
「实时和刷新后不一样」。

纯函数:不碰 IO、不碰数据库、不 import 任何 service 包(orchestrator 与
control-plane 都要用它,而 orchestrator 不能 import control-plane)。消息按
鸭子类型读(``type`` / ``content`` / ``tool_calls`` / …),不绑 LangChain 的
具体类 —— 回放路径喂进来的是从 ``updates`` 帧里反序列化的对象。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import Any

from expert_work.common.conversation_channel import (
    is_hidden,
    message_text,
    visible_turns,
)
from expert_work.common.conversation_items import (
    ApprovalItem,
    AssistantMessageItem,
    ConversationItem,
    ErrorItem,
    PlanItem,
    ToolCallItem,
    ToolResultItem,
    UserMessageItem,
)
from expert_work.common.message_stamp import STAMP_CREATED_AT
from expert_work.common.spotlight import unspotlight

__all__ = ["derive_run_items"]


def _created_at(msg: Any) -> str | None:
    """写入侧盖的 ``expert_work_created_at``,没盖就是 ``None``。

    不回填、不编造:上线前写入的老消息归不到时刻,给 ``None`` 让客户端按
    「缺席」处理,比给一个假时间好。
    """
    stamp = (getattr(msg, "additional_kwargs", None) or {}).get(STAMP_CREATED_AT)
    return stamp if isinstance(stamp, str) else None


def _attachments(content: Any) -> list[dict[str, Any]]:
    """用户消息里的非文本内容块。

    判据是「``message_text`` 没吃掉的那些块」—— 带 ``text`` 字符串的块是正文,
    其余(``image_ref`` 等)是附件。两边严格互补,所以没有哪个块会既算正文
    又算附件,也没有哪个块会两头都不算。
    """
    if not isinstance(content, list):
        return []
    return [
        dict(block)
        for block in content
        if isinstance(block, Mapping) and not isinstance(block.get("text"), str)
    ]


def _tool_call_fields(call: Any) -> tuple[str, str, Mapping[str, Any]]:
    """``AIMessage.tool_calls`` 的一项 → ``(call_id, name, args)``。

    LangChain 给的是 dict(``{"id", "name", "args", "type"}``),但回放路径喂进来
    的可能是别的对象形态,所以两种读法都兜住。
    """
    if isinstance(call, Mapping):
        call_id = call.get("id")
        name = call.get("name")
        args = call.get("args")
    else:
        call_id = getattr(call, "id", None)
        name = getattr(call, "name", None)
        args = getattr(call, "args", None)
    return (
        call_id if isinstance(call_id, str) else "",
        name if isinstance(name, str) else "",
        args if isinstance(args, Mapping) else {},
    )


def _message_items(run_id: str, messages: Sequence[Any]) -> list[ConversationItem]:
    """消息序列 → 条目,严格保持消息顺序。"""
    # 可见轮次(带 channel)与本函数走同一套过滤:``visible.get(seq)`` 命中
    # 才产出文本条目,所以「哪条消息该有气泡」只有这一个判据。
    visible = {t.seq: t for t in visible_turns(messages, include_hidden=False)}
    items: list[ConversationItem] = []
    for seq, msg in enumerate(messages):
        # 编排层写进 checkpoint 的脚手架绝不出现在对外条目里。
        if is_hidden(msg):
            continue
        mtype = getattr(msg, "type", None)
        turn = visible.get(seq)
        if mtype == "human":
            attachments = _attachments(getattr(msg, "content", None))
            # 正文空白但带附件的消息照样是一条用户消息 —— 只发了一张图的
            # 那一轮,丢掉它等于丢掉用户说的全部内容。这是本函数与
            # ``extract_turns`` 有意的一处分歧(那边是纯文本记录,空文本
            # 无可记)。
            if turn is not None or attachments:
                items.append(
                    UserMessageItem(
                        id="",
                        run_id=run_id,
                        created_at=_created_at(msg),
                        content=turn.text if turn is not None else "",
                        attachments=attachments,
                    )
                )
        elif mtype == "ai":
            if turn is not None:
                items.append(
                    AssistantMessageItem(
                        id="",
                        run_id=run_id,
                        created_at=_created_at(msg),
                        content=turn.text,
                        # 助手轮必有 channel(``visible_turns`` 的约定)。
                        channel=turn.channel or "",
                    )
                )
            # 一条 AIMessage 可以带多个 tool_calls,每个各占一条(也各占一个
            # ``id`` 子序号)。它们排在同一条消息的正文之后 —— 模型先说话再
            # 动手。
            for call in getattr(msg, "tool_calls", None) or []:
                call_id, name, args = _tool_call_fields(call)
                items.append(
                    ToolCallItem(
                        id="",
                        run_id=run_id,
                        created_at=_created_at(msg),
                        call_id=call_id,
                        name=name,
                        args=args,
                        # ``worker`` 的数据源是独立的 worker 帧,不在本函数的
                        # 输入里;留给上层按 ``call_id`` 回填。
                        worker=None,
                    )
                )
        elif mtype == "tool":
            status = getattr(msg, "status", None)
            items.append(
                ToolResultItem(
                    id="",
                    run_id=run_id,
                    created_at=_created_at(msg),
                    call_id=getattr(msg, "tool_call_id", "") or "",
                    name=getattr(msg, "name", "") or "",
                    status=status if isinstance(status, str) else "success",
                    # 还原防注入包装 —— 内部表示翻译成产品表示正是本层的价值,
                    # 不把这一步推给客户端。
                    content=unspotlight(message_text(getattr(msg, "content", ""))),
                    artifact=getattr(msg, "artifact", None),
                )
            )
    return items


def _plan_item(run_id: str, plan: Mapping[str, Any]) -> PlanItem:
    steps = plan.get("steps")
    return PlanItem(
        id="",
        run_id=run_id,
        # ``plan`` 帧的 data 里没有时刻,不编。
        created_at=None,
        goal=str(plan.get("goal") or ""),
        steps=[dict(s) for s in steps if isinstance(s, Mapping)] if isinstance(steps, list) else [],
    )


def _approval_item(run_id: str, frame: Mapping[str, Any]) -> ApprovalItem:
    def text(key: str) -> str:
        value = frame.get(key)
        return value if isinstance(value, str) else ""

    def optional_text(key: str) -> str | None:
        value = frame.get(key)
        return value if isinstance(value, str) else None

    args = frame.get("proposed_args")
    return ApprovalItem(
        id="",
        run_id=run_id,
        # 审批的产生时刻就是 ``requested_at`` —— 公共字段与专有字段同值,
        # 客户端按 ``created_at`` 统一排序时不必给审批开特例。
        created_at=optional_text("requested_at"),
        request_id=text("request_id"),
        node=text("node"),
        reason_kind=text("reason_kind"),
        action_summary=text("action_summary"),
        proposed_args=args if isinstance(args, Mapping) else {},
        requested_at=optional_text("requested_at"),
        timeout_at=optional_text("timeout_at"),
        # ``decision`` 不从 approval 帧来(帧是「请求」不是「结果」),由上层
        # 按 ``request_id`` 回填。
        decision=None,
    )


def derive_run_items(
    *,
    run_id: str,
    messages: Sequence[Any],
    plan: Mapping[str, Any] | None = None,
    approvals: Sequence[Mapping[str, Any]] = (),
    error: str | None = None,
) -> list[ConversationItem]:
    """把一轮的消息 + 辅助信号推导成条目列表。

    参数
    ----
    run_id
        这一轮的 id,进每个条目的 ``run_id``,也是 ``id`` 的前缀。
    messages
        这一轮产生的消息,按产生顺序。带 ``expert_work_hide_from_ui`` 的
        脚手架会被排除。
    plan
        该轮**最后一个** ``plan`` 帧的 data(整份快照,不是增量);``None``
        = 这一轮没有计划。
    approvals
        该轮 ``approval`` 帧的 data,按发生顺序。
    error
        该轮 ``error`` 帧的 ``message``;``None`` = 这一轮没失败。

    顺序
    ----
    条目按 ``messages` 的顺序产出,另外三类辅助信号插在:

    * ``plan`` —— 用户消息之后、第一条助手产出之前。它是这一轮**开始时**的
      规划,排在助手动手之前才读得通。
    * ``approval`` —— 末尾、``error`` 之前。**这是一处有意的降级**:
      ``approval`` 帧里没有 ``tool_call_id``(它只有 ``request_id`` /
      ``node`` / ``proposed_args``),拿参数去猜配哪个 ``tool_call`` 在同一
      工具被调多次、或审批时改过参数的情况下会配错,宁可不猜。降级的代价很
      小 —— 一轮一旦停在审批上就以 PAUSED 结束,审批**本来**就是这一轮最后
      发生的事,续跑的工具结果落在下一轮里。
    * ``error`` —— 最后。一轮跑了一半才失败时,错误应当排在已产出内容之后。

    ``id``
    ------
    ``f"{run_id}:{n}"``,``n`` 是条目在**本次推导结果**里的 0 基下标(所以
    多 ``tool_calls`` 的一条消息会占掉连续几个号)。承诺范围见
    :mod:`expert_work.common.conversation_items` 的模块 docstring:同一响应内
    唯一、同一查询可重复,不跨接口稳定。
    """
    items = _message_items(run_id, messages)

    if plan is not None:
        # 第一条非用户消息的位置 = 「助手开始产出」的位置。全是用户消息(或
        # 一条都没有)时落在末尾。
        at = next(
            (i for i, item in enumerate(items) if item.TYPE != UserMessageItem.TYPE),
            len(items),
        )
        items.insert(at, _plan_item(run_id, plan))

    items.extend(_approval_item(run_id, frame) for frame in approvals)

    if error is not None:
        items.append(ErrorItem(id="", run_id=run_id, created_at=None, message=error))

    return [replace(item, id=f"{run_id}:{n}") for n, item in enumerate(items)]
