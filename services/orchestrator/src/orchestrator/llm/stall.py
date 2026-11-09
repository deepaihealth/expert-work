"""B-186 —— 模型思考完停住、一个字的回答都没有。

流式输出开始后停顿超过 ``idle_timeout_s``,路由用已收到的部分结束这一轮,并在
``response_metadata`` 盖 :data:`STALLED_AFTER_S` 章(只在厂商还没发结束原因时盖:发了
``length`` 的归 B-105 截断管)。半截照旧交回 —— 规划 / 复盘把空回复当 fail-open,不能让它们
跟着报错;只有主循环用 :func:`stalled_without_answer` 判出「只有思考」后让 run 可见地失败。

B-297 PR2 —— 还有别的厂商组可用时,「只有思考」的半截不再交回:路由丢掉它,抛
:class:`LLMReasoningStallError` 换下一组重做这一步(:func:`has_visible_output` 判「只有思考」)。
"""

from __future__ import annotations

from langchain_core.messages import AIMessage

from expert_work.runtime.middleware import LLMStreamStaleError
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


def has_visible_output(message: AIMessage) -> bool:
    """半截里有没有丢不得的东西:正文(去空白后非空),或工具调用。

    只看组装好的半截:中断时组装器(``build(interrupted=True)``)已把参数没写完整的工具调用
    剔掉。那种调用虽然不会执行,但工具名已作为预览流出去了,所以路由判「能不能丢掉重做」时
    另外看流里出没出现过带名字的工具调用(``failover.StreamAttempt``)。
    """
    if message.tool_calls or message.invalid_tool_calls:
        return True
    return bool(_message_text(message).strip())


class LLMReasoningStallError(LLMStreamStaleError):
    """B-297 PR2 —— 流式输出只有思考就停住 / 出错,且后面还有别的厂商组。

    路由丢掉这次的半截、换下一组重做这一步。继承 :class:`LLMStreamStaleError`:E.4 记一次失败
    (熔断计数)、不原地重试;路由按厂商级失败跳到下一组。只在「没有可见输出 + 还有别的组」时
    抛,其余情形保持原样(B-186 的半截 / ``LLMStreamInterruptedError``)。

    ``outcome`` 是不重做时本该得到的结果(盖章的半截,或 ``LLMStreamInterruptedError``):
    后面的组也没能答完时,路由交回它 —— 调用方看到的与改之前完全一样。
    """

    def __init__(self, message: str, *, outcome: AIMessage | BaseException) -> None:
        super().__init__(message)
        self.outcome = outcome

    def original_outcome(self) -> AIMessage:
        """不重做时的结果:返回盖章的半截,或抛出原来那个中断错误。"""
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome
