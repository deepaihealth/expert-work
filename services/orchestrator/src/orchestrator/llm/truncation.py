"""B-105 —— 输出被上限截断的判定与报错。

各家形态(2026-09-24 实调):OpenAI 兼容 ``finish_reason == "length"``;Anthropic
``stop_reason == "max_tokens"``。思考模型截断时通常是思考吃满额度、正文为空。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING

from langchain_core.messages import AIMessage

from expert_work.common.observability.metrics import expert_work_counter
from orchestrator.llm.structured_output import _message_text
from orchestrator.usage_metering import ServedModelResolver, chain_models

if TYPE_CHECKING:
    from expert_work.protocol import ModelSpec

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


@dataclass(frozen=True)
class ServedOutputCap:
    """响应 → 实际应答模型的 ``(provider, model, 输出上限)``。

    实际应答的是谁由路由盖的章认出(``orchestrator.usage_metering``,与用量记账同一个
    解析器);没盖章(没接用量存储)时记配置的主模型。上限取该模型条目自己的
    ``max_tokens``(``None`` = 厂商默认)。
    """

    served_by: Callable[[AIMessage], tuple[str, str]]
    caps: Mapping[tuple[str, str], int | None]

    def __call__(self, response: AIMessage) -> tuple[str, str, int | None]:
        provider, model = self.served_by(response)
        return provider, model, self.caps.get((provider, model))


def served_output_cap(model: ModelSpec) -> ServedOutputCap:
    """``model`` 与它整棵备用树的截断标签 / 上限解析器。"""
    caps: dict[tuple[str, str], int | None] = {}
    pending = [model]
    while pending:
        entry = pending.pop()
        caps[(entry.provider, entry.name)] = entry.max_tokens
        pending.extend(entry.fallback)
    return ServedOutputCap(
        served_by=ServedModelResolver(
            default=(model.provider, model.name), models=chain_models(model)
        ),
        caps=caps,
    )
