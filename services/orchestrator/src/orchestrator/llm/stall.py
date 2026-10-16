"""B-186 —— 模型思考完停住、一个字的回答都没有。

流式输出开始后停顿超过 ``idle_timeout_s``,路由用已收到的部分结束这一轮,并在
``response_metadata`` 盖 :data:`STALLED_AFTER_S` 章(只在厂商还没发结束原因时盖:发了
``length`` 的归 B-105 截断管)。半截照旧交回 —— 规划 / 复盘把空回复当 fail-open,不能让它们
跟着报错;只有主循环用 :func:`stalled_without_answer` 判出「只有思考」后让 run 可见地失败。
"""

from __future__ import annotations

from langchain_core.messages import AIMessage

from orchestrator.llm.structured_output import _message_text

#: 路由盖的章:值 = 触发的停顿秒数。
STALLED_AFTER_S = "expert_work_stalled_after_s"


class StalledWithoutAnswerError(RuntimeError):
    """停顿时只有思考、既没有正文也没有完整的工具调用。在 agent 节点、路由之后抛出,run 以
    可见错误结束。"""

    def __init__(self, idle_s: float) -> None:
        super().__init__(
            f"模型思考后停顿超过 {idle_s:g} 秒,还没给出回答就中断了。"
            "可以重试;经常出现的话,在智能体配置里调大『token 间隔超时』。"
        )
        self.idle_s = idle_s


def stalled_without_answer(message: AIMessage) -> float | None:
    """盖了停顿章、且没有正文也没有工具调用 → 停顿秒数;否则 ``None``。"""
    idle_s = (message.response_metadata or {}).get(STALLED_AFTER_S)
    if idle_s is None or message.tool_calls or message.invalid_tool_calls:
        return None
    if _message_text(message).strip():
        return None
    return float(idle_s)
