"""B-328 PR R —— 厂商说「输入太长」时狠缩一次、重试一次。

设计:``docs/superpowers/specs/2026-11-20-b328-oversized-tool-result-stuck-design.md`` §4.2。

平台的发送前检查(A③)只防「估算超过主模型整个窗口」;估算偏低(图片、英文代码、别家分词器)
或路由换到窗口更小的备用模型时,厂商会回「上下文太长」—— 4xx,或流式中途的错误事件,两处都由
``llm.providers._errors.looks_context_too_long`` 认成 :class:`LLMContextTooLongError`。它仍是
4xx:路由不换厂商、不重试(主模型 5xx 之后备用模型报的 4xx 也直接抛出来),由这里接:

1. 用 A③ 同一个 :func:`~orchestrator.context.result_budget.hard_shrink` 缩**进 before chain
   之前**的那份提示词;
2. 重新走一遍 before chain(用缩后的提示词查响应缓存);
3. 命中就用缓存;否则再调一次模型(从主模型开始,路由照常走备用);
4. 还说太长 → 干净失败 ``ContextOverflowError(reason="vendor_rejected")``,带厂商原话前 200 字
   和平台估算。

被拒的那次没有用量、不写缓存(它没有回答,after chain 不跑)。不记住被拒过(D7):下一步、
下一轮照常走,再被拒就再缩一次。
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage

from expert_work.common.observability import expert_work_counter
from expert_work.runtime.middleware import (
    LLMContextTooLongError,
    MiddlewareChain,
    MiddlewareContext,
)
from expert_work.runtime.tokens import CharTokenEstimator, TokenEstimator, estimate_message
from orchestrator.context.compressor import ContextOverflowError
from orchestrator.context.result_budget import hard_shrink, tool_definition_tokens
from orchestrator.tools.registry import ToolSpec

logger = logging.getLogger(__name__)

#: 报错里带的厂商原话长度。
_VENDOR_TEXT_CHARS = 200

_vendor_too_long_total = expert_work_counter(
    "expert_work_llm_vendor_too_long_total",
    "Main-loop calls the vendor rejected as too long, by what the one retry did (B-328 PR R).",
    ["outcome"],
)


async def _noop(_ctx: MiddlewareContext) -> None:
    return None


async def retry_after_too_long(
    rejected: LLMContextTooLongError,
    *,
    pre_chain_messages: Sequence[BaseMessage],
    tools: Sequence[ToolSpec],
    call: Callable[[list[BaseMessage]], Awaitable[AIMessage]],
    before_llm_chain: MiddlewareChain | None,
    chain_payload: Mapping[str, Any],
    estimator: TokenEstimator | None,
) -> tuple[list[BaseMessage], AIMessage, bool]:
    """狠缩一次、重试一次。返回 ``(本步最终的提示词, 回答, 是否缓存命中)``。

    ``chain_payload`` 是 before chain 除 ``messages`` / ``tools`` 以外的那几个键(租户、
    缓存旁路),与第一次一样。
    """
    shrunk = hard_shrink(pre_chain_messages)
    messages = list(shrunk)
    if before_llm_chain is not None:
        ctx = MiddlewareContext(
            payload={**chain_payload, "messages": messages, "tools": list(tools)}
        )
        await before_llm_chain.invoke(ctx, _noop)
        messages = list(ctx.payload.get("messages", messages))
        hit = ctx.payload.get("llm_cache_hit")
        if isinstance(hit, AIMessage):
            _vendor_too_long_total.labels(outcome="cache_hit").inc()
            return messages, hit, True
    logger.warning("agent_node.vendor_too_long_retry error=%s", str(rejected)[:_VENDOR_TEXT_CHARS])
    try:
        response = await call(messages)
    except LLMContextTooLongError as again:
        _vendor_too_long_total.labels(outcome="rejected_again").inc()
        est = estimator or CharTokenEstimator()
        estimate = sum(estimate_message(m, est) for m in messages) + tool_definition_tokens(
            tools, est
        )
        vendor_text = str(again)[:_VENDOR_TEXT_CHARS]
        detail = (
            "context overflow: the model rejected the prompt as too long even after one "
            f"hard shrink; platform estimate {estimate} tokens (messages + tool "
            f"definitions); vendor said: {vendor_text}"
        )
        raise ContextOverflowError(
            estimated_tokens=estimate, threshold=0, reason="vendor_rejected", detail=detail
        ) from again
    _vendor_too_long_total.labels(outcome="retried").inc()
    return messages, response, False
