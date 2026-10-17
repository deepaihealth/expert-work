"""B-191 —— 模型正常结束了这一轮,却既没有回答也没有工具调用。

实测形态:思考模型把要发的工具调用(连同一大段参数)写进了思考内容,结束原因是正常的
``stop``,正文为空、``tool_calls`` 为空 —— 调用根本没发出去,run 却记成「成功、空回复」。
判定只看「正常结束 + 空」,不认任何厂商的标记格式;截断(B-105,``length``)与停顿
(B-186,路由盖章)各有各的判定和处理,不归这里。主循环先丢弃重来一次(同 B-158),
重试那一轮还这样就让 run 可见地失败。
"""

from __future__ import annotations

from langchain_core.messages import AIMessage

from expert_work.common.observability.metrics import expert_work_counter
from orchestrator.llm.stall import STALLED_AFTER_S
from orchestrator.llm.structured_output import _message_text
from orchestrator.llm.truncation import is_truncated

#: 空回复次数。``outcome`` = 丢弃后重试("retried")还是 run 以可见错误结束("failed")。
empty_reply_total = expert_work_counter(
    "expert_work_llm_empty_reply_total",
    "Main-loop replies that ended normally with no answer and no tool call, by outcome.",
    ("provider", "model", "outcome"),
)


def is_empty_reply(message: AIMessage) -> bool:
    """正常结束的空回复:没有正文,也没有工具调用(含解析失败的)。截断与停顿不算。"""
    if message.tool_calls or message.invalid_tool_calls:
        return False
    if is_truncated(message) or STALLED_AFTER_S in (message.response_metadata or {}):
        return False
    return not _message_text(message).strip()


class EmptyReplyError(RuntimeError):
    """重试过一次,模型还是正常结束却既没有回答也没有工具调用。在 agent 节点、路由之后抛出,
    run 以可见错误结束。"""

    def __init__(self) -> None:
        super().__init__(
            "模型结束了这一轮,却既没有给出回答,也没有调用工具(已自动重试过一次)。"
            "可以重试;经常出现的话,可以换用别的模型。"
        )
