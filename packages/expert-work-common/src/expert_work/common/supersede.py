"""P-1「重新生成 / 编辑重发」—— 按轮分组 + 整轮过滤 + 打标 / 墓碑,全平台唯一一份。

为什么在 common:``/items``(control-plane)按 run 分组条目,``agent_node``
(orchestrator)按轮剔除被取代消息,两处必须是同一个分组规则 —— 一轮的
AI(tool_calls) 与它的 ToolMessage 必须同进同出,否则孤儿 tool_call 厂商 400。
orchestrator 不能 import control-plane,所以规则住这里。标记常量与判定在
:mod:`conversation_channel`(与 ``HIDE_FROM_UI`` 同一处),本模块只做组合。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage

from expert_work.common.conversation_channel import (
    SUPERSEDED_AT,
    SUPERSEDED_BY,
    TOMBSTONE,
    is_superseded,
    message_field,
)
from expert_work.common.message_stamp import STAMP_RUN_ID

__all__ = [
    "filter_superseded_turns",
    "group_messages_by_run",
    "mark_superseded",
    "stamped_run_id",
    "tombstone_message",
]


def stamped_run_id(msg: Any) -> str | None:
    """写入侧盖的 ``expert_work_run_id``,没盖就是 ``None``。"""
    stamp = (message_field(msg, "additional_kwargs") or {}).get(STAMP_RUN_ID)
    return stamp if isinstance(stamp, str) else None


def group_messages_by_run(messages: Sequence[Any]) -> dict[str, list[Any]]:
    """按 ``run_id`` 戳把检查点消息分到各轮,保持原始顺序。

    * 盖了戳的消息归戳上那一轮。
    * 工具结果消息(``type == "tool"``)从来不盖戳(写入侧只给 agent 节点的
      助手消息与入口的用户消息盖戳),归到前一条已归属消息的那一轮 —— 工具
      结果结构上必定紧跟发起调用的那条助手消息。
    * 其余没盖戳的消息(每轮的 SystemMessage、上线前的老消息)归不到任何一轮,
      直接丢弃 —— 编一个归属会让它出现在错误的轮次里。
    """
    grouped: dict[str, list[Any]] = {}
    current: str | None = None
    for msg in messages:
        stamped = stamped_run_id(msg)
        if stamped is not None:
            current = stamped
        elif message_field(msg, "type") != "tool":
            continue
        if current is None:
            continue
        grouped.setdefault(current, []).append(msg)
    return grouped


def filter_superseded_turns(messages: Sequence[Any]) -> list[Any]:
    """agent 的 prompt 视图:剔除被取代的消息,并且**按轮整段剔除**。

    两条规则叠加:带 ``SUPERSEDED_BY`` 的消息一律不要(墓碑也带这个标记,一并
    剔除);此外,某一轮只要有任何一条带标记,这一轮里盖了戳的消息与它的工具
    结果全部不要 —— 防某条漏标的 ToolMessage 变成孤儿。每轮开头那条无戳的
    SystemMessage 只靠第一条规则(supersede 时按下标区间打标,它必然带标记)。
    """
    superseded_runs = {
        run_id
        for run_id, group in group_messages_by_run(messages).items()
        if any(is_superseded(m) for m in group)
    }
    out: list[Any] = []
    current: str | None = None
    for msg in messages:
        stamped = stamped_run_id(msg)
        if stamped is not None:
            current = stamped
        if is_superseded(msg):
            continue
        member = stamped is not None or message_field(msg, "type") == "tool"
        if member and current is not None and current in superseded_runs:
            continue
        out.append(msg)
    return out


def mark_superseded(msg: BaseMessage, *, new_run_id: str, now: datetime) -> BaseMessage:
    """返回带「已被取代」标记的**新**消息,原 id 与既有 kwargs 原样保留(不可变约定)。

    id 必须保留:``add_messages`` 只按 id 原地替换,丢了 id 会被当新消息追加
    (spike 反证:15 条而非 10 条)。
    """
    merged = {
        **msg.additional_kwargs,
        SUPERSEDED_BY: new_run_id,
        SUPERSEDED_AT: now.isoformat(),
    }
    return msg.model_copy(update={"additional_kwargs": merged})


def tombstone_message(msg: BaseMessage) -> BaseMessage:
    """正文置空、tool_calls 清空、打 TOMBSTONE;id / 下标 / 既有标记不动。"""
    update: dict[str, Any] = {
        "content": "",
        "additional_kwargs": {**msg.additional_kwargs, TOMBSTONE: True},
    }
    if isinstance(msg, AIMessage):
        update["tool_calls"] = []
    return msg.model_copy(update=update)
