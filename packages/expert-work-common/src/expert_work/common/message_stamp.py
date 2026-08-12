"""写入侧给消息盖时间戳 / run 归属(P2 块 2)。

对外 ``GET .../sessions/{id}/messages`` 要给每条消息 ``created_at`` 与
``run_id``,而 LangGraph 检查点本身不存这两样。补法是在**写入时**把它们塞进
``additional_kwargs`` —— 这是本仓库现成惯用法(``expert_work_hide_from_ui`` /
``expert_work_scheduled_delivery`` / ``expert_work_source_run_id`` 都是这么塞的),
读取侧 ``transcript.extract_turns`` 只读不算。

放在 common 而非任一 service:用户消息在 control-plane 盖,助手消息在
orchestrator 盖,而 orchestrator 不能 import control-plane。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from langchain_core.messages import BaseMessage

#: 消息产生时刻,ISO8601 字符串。
STAMP_CREATED_AT = "expert_work_created_at"
#: 产生这条消息的 run id,字符串形态的 UUID。
STAMP_RUN_ID = "expert_work_run_id"

__all__ = ["STAMP_CREATED_AT", "STAMP_RUN_ID", "stamp_message", "stamp_messages"]


def stamp_message(msg: BaseMessage, *, run_id: str, now: datetime) -> BaseMessage:
    """返回盖好戳的**新**消息,原对象不动(不可变约定)。

    已有的 ``additional_kwargs`` 原样保留 —— 盖戳绝不能顶掉
    ``expert_work_hide_from_ui`` 这类既有标记,否则脚手架会漏给第三方。
    """
    merged = {**msg.additional_kwargs, STAMP_CREATED_AT: now.isoformat(), STAMP_RUN_ID: run_id}
    return msg.model_copy(update={"additional_kwargs": merged})


def stamp_messages(msgs: Sequence[BaseMessage], *, run_id: str, now: datetime) -> list[BaseMessage]:
    """``stamp_message`` 的批量形态。"""
    return [stamp_message(m, run_id=run_id, now=now) for m in msgs]
