"""B-105 —— 输出被上限截断的判定与报错。

各家形态(2026-09-24 实调):OpenAI 兼容 ``finish_reason == "length"``;Anthropic
``stop_reason == "max_tokens"``。思考模型截断时通常是思考吃满额度、正文为空。
"""

from __future__ import annotations

from langchain_core.messages import AIMessage

from expert_work.common.observability.metrics import expert_work_counter
from orchestrator.llm.structured_output import _message_text

_TRUNCATED = {("finish_reason", "length"), ("stop_reason", "max_tokens")}

#: 截断次数。``usable`` = 正文可用照常交付("true")还是 run 以可见错误结束("false")。
output_truncated_total = expert_work_counter(
    "expert_work_llm_output_truncated_total",
    "Model replies cut off by the output cap, by whether the reply was still usable.",
    ("provider", "model", "usable"),
)


def is_truncated(message: AIMessage) -> bool:
    meta = message.response_metadata or {}
    return any(meta.get(key) == value for key, value in _TRUNCATED)


def is_unusable_truncation(message: AIMessage) -> bool:
    """截断且答案不能用:正文为空,或带着工具调用(最后一个的参数必然被截)。"""
    if not is_truncated(message):
        return False
    if message.tool_calls or message.invalid_tool_calls:
        return True
    return not _message_text(message).strip()


class OutputTruncatedError(RuntimeError):
    """模型输出被上限截断且答案不可用。在 agent 节点、路由之后抛出 —— 不触发 fallback
    (备用模型也是同一个上限),run 以可见错误结束。"""

    def __init__(self, cap: int | None) -> None:
        shown = str(cap) if cap is not None else "厂商默认值"
        super().__init__(
            f"模型输出被截断(已用满输出上限 {shown},含思考)。"
            "请在模型配置里调大『输出上限』,或者降低思考档位。"
        )
        self.cap = cap
