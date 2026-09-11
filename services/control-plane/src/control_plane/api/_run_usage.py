"""B-52 —— 对外「按 run 的用量」的单一装配口。

五个消费点全部经过这里:``end`` 帧的四个分支(``sse_consumer`` / replay /
live-join probe / live 实时)与 ``GET /v1/agents/{code}/runs/{run_id}/usage``。

为什么要有这个模块:``end_frame_data`` 的 docstring 记着两条 SSE 流的 ``end`` 帧
字段集合**已经分叉过一次**(P2-a)。用量的口径比字段名更容易悄悄分叉 —— 取哪些
``usage_kind``、露不露 ``llm_calls``、无记录时返回 ``None`` 还是 ``[]`` —— 所以
这三件事只放一处。

用量按 ``trace_id`` 连 ``agent_run`` ↔ ``token_usage``(后者没有 ``run_id`` 列),
因此天然含整棵调用树:worker 与父 run **共用同一个 trace**,其用量以
``{parent}-worker`` 的 ``agent_name`` 记在同一 trace 下。且不会双计 —— 用量按
**每次 LLM 调用**落一行,不是按 span 嵌套记(与墙钟时长那次双计相反:那次是同一段
时间既进工具行又进 subagent 行)。
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Sequence
from contextlib import AbstractAsyncContextManager
from typing import Any
from uuid import UUID

from expert_work.persistence.token_usage_store import ModelTokenTotals, TokenUsageStore
from expert_work.runtime.runs import RunStore

logger = logging.getLogger(__name__)

#: 对外用量只算对话开销。``quality_sampling``(质量抽检)与 ``skill_evolution``
#: (技能进化)是平台自身的流程,算到调用方头上就是替我们的内部开销收钱。
EXTERNAL_USAGE_KINDS: tuple[str, ...] = ("conversation",)


def usage_buckets_to_wire(buckets: Sequence[ModelTokenTotals]) -> list[dict[str, Any]]:
    """把聚合桶转成对外形状。

    四档 token **恒全给** —— 计价口径是调用方的事,我方只负责计量完整准确。
    (一条告知义务:``input_tokens`` 已经**包含** ``cache_read_tokens``,这是
    LangChain ``usage_metadata`` 的约定 —— ``input_tokens`` 是所有输入类型之和,
    ``input_token_details`` 是它的细分。)

    ``llm_calls`` **不对外**:那是我方内部的 LLM 调用次数,调用方不需要。
    """
    return [
        {
            "provider": b.provider,
            "model": b.model,
            "input_tokens": b.input_tokens,
            "output_tokens": b.output_tokens,
            "cache_read_tokens": b.cache_read_tokens,
            "cache_creation_tokens": b.cache_creation_tokens,
        }
        for b in buckets
    ]


def make_usage_loader(
    *,
    usage: TokenUsageStore,
    runs: RunStore,
    run_id: UUID,
    tenant_id: UUID,
    scope: Callable[[], AbstractAsyncContextManager[None]] | None = None,
) -> Callable[[], Awaitable[list[dict[str, Any]] | None]]:
    """工厂,与 :func:`make_run_probe` 同款:单次可用,每次取数重开 ``scope``。

    ``None`` 表示**无记录**(对外帧上字段缺席),``[]`` 表示**确有其事的零用量**
    —— 两者不可混同,这是 ``artifacts`` 立下的既有口径(「缺席 = 老 run 无记录,
    别当零交付」)。``None`` 的三种来路:run 行不存在(已 purge)、``trace_id``
    尚未绑定(排队中,``bind_exec_trace`` 还没跑)、该 trace 没有任何对话用量行
    (历史 run)。

    **绝不抛异常。** 这个 loader 跑在 SSE 的终局路径上 —— 抛出去会让整条流断在
    ``end`` 帧之前,调用方连 run 是怎么结束的都拿不到。查询失败只记日志并返回
    ``None``(语义恰好是「无记录」),run 的终局状态照发。用量查不到,远没有
    终局状态丢失严重。
    """

    async def _load() -> list[dict[str, Any]] | None:
        try:
            if scope is not None:
                async with scope():
                    row = await runs.get(run_id=run_id, tenant_id=tenant_id)
            else:
                row = await runs.get(run_id=run_id, tenant_id=tenant_id)
            if row is None or not row.trace_id:
                return None
            trace_id = row.trace_id
            if scope is not None:
                async with scope():
                    totals = await usage.totals_by_trace_ids(
                        [trace_id], usage_kinds=EXTERNAL_USAGE_KINDS
                    )
            else:
                totals = await usage.totals_by_trace_ids(
                    [trace_id], usage_kinds=EXTERNAL_USAGE_KINDS
                )
            found = totals.get(trace_id)
            if found is None:
                return None
            return usage_buckets_to_wire(found.by_model)
        except Exception:
            # ``run_id`` 是 UUID 对象,承载不了换行 —— 与 ``make_run_probe``
            # 那条同样的 log-injection 抑制论证。
            logger.warning(  # codeql[py/log-injection]
                "run_usage.load_failed run_id=%s",
                run_id,  # codeql[py/log-injection]
                exc_info=True,
            )
            return None

    return _load
