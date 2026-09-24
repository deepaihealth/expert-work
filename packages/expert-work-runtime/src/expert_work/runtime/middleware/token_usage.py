"""``after_llm_call`` token-usage observability — Stream G.9.

For every LLM call we:

1. Increment a Prometheus counter
   ``expert_work_llm_token_usage_total{tenant_id, agent_name, model, type, usage_kind}`` so
   dashboards (Grafana / per-tenant per-agent token spend) and alerts
   see usage in real time.
2. Persist one row in the ``token_usage`` table (via
   :class:`TokenUsageStore`) so the M0→M1 Gate can compute per-agent
   cost over an arbitrary time window, irrespective of metric
   retention.

The middleware reads :attr:`MiddlewareContext.payload`:

* ``response`` — the :class:`AIMessage` the LLM returned; its
  ``usage_metadata`` carries ``input_tokens`` / ``output_tokens`` /
  optional ``input_token_details.{cache_creation, cache_read}``
  (Stream L.L1's cache-aware shape — Anthropic only; other providers
  leave cache details unset).
* ``tenant_id`` — UUID; gates both the counter label and the RLS
  context for the DB insert. A response without one is silently
  dropped (the middleware before the LLM call also requires it).
* ``cache_hit`` — when the LLM cache served the response (Stream E.5),
  no new tokens were spent upstream. We still record a row with the
  cached counts (0 by default) so downstream queries can distinguish
  cached calls from no-call.

Agent identity (``agent_name`` / ``agent_version`` / ``model``) is
baked into the middleware instance at construction by
:func:`build_middleware_chains` — same pattern as
:class:`LLMCacheStoreMiddleware`.

B-102 —— ``model`` / ``provider`` 是**配置的主模型**;备用模型接管时,``served_by``
(构建期给的解析器)从响应上认出**实际应答**的模型条目,行与计数器都记在它的配置名
下(不用厂商回显的 ``response_metadata.model_name``)。缓存命中没有模型应答,仍记主模型。

B-103 —— :func:`usage_tap` 让调用方在一段执行里收到这里记下的每一次调用(与落行同一
口径:同样的「没有用量就不记」、同样的模型名),worker 的 end 帧据此按模型分桶。
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from langchain_core.messages import AIMessage

from expert_work.common.observability import current_trace_id_hex
from expert_work.common.observability.metrics import expert_work_counter
from expert_work.persistence.token_usage_store import (
    TokenUsageRecord,
    TokenUsageStore,
)
from expert_work.runtime.middleware.base import CallNext, MiddlewareContext
from expert_work.runtime.tokens import TokenEstimator, estimate_messages

logger = logging.getLogger(__name__)

#: ``type`` label values for the counter. The order mirrors the
#: ``TokenUsageRecord`` field order so dashboards group by type
#: predictably. ``noqa: S105`` — these are metric label values, not
#: hardcoded secrets (ruff's S105 trips on the ``TOKEN_TYPE_`` prefix).
_TOKEN_TYPE_INPUT = "input"  # noqa: S105
_TOKEN_TYPE_OUTPUT = "output"  # noqa: S105
_TOKEN_TYPE_CACHE_CREATION = "cache_creation"  # noqa: S105
_TOKEN_TYPE_CACHE_READ = "cache_read"  # noqa: S105

_llm_token_usage_total = expert_work_counter(
    "expert_work_llm_token_usage_total",
    "Tokens consumed per LLM call, split by type (Stream G.9).",
    # B-104 —— ``usage_kind``:run 内除主循环外的调用(规划 / 压缩 / 记忆 / 评审 /
    # 重排序…)也走这里,面板与比值按 kind 区分(``platform_overhead`` 不计费)。
    ("tenant_id", "agent_name", "model", "type", "usage_kind"),
)

#: Stream HX-1 (Mini-ADR HX-A6) — estimated prompt tokens, accumulated
#: alongside the actual counts above so dashboards can derive the
#: estimator drift ratio in PromQL:
#: ``rate(expert_work_ew_token_estimated_total) /
#: rate(expert_work_ew_token_estimate_actual_total)``.
#: A counter pair instead of a ratio histogram because the repo metric
#: convention reserves histograms for durations (``_seconds``).
_ew_token_estimated_total = expert_work_counter(
    "expert_work_ew_token_estimated_total",
    "Estimated prompt tokens per LLM call (Stream HX-1 drift numerator).",
    ("tenant_id", "agent_name", "model", "usage_kind"),
)

#: B-104 —— 漂移比的分母:**同一批**调用(带估算器、非缓存命中、有 prompt 视图)的上游
#: 实报 prompt tokens(input + cache_creation + cache_read)。原来分母用
#: ``expert_work_llm_token_usage_total`` 的全部调用,而只有主循环带估算器 —— 看图、规划、
#: 压缩、记忆、评审、重排序这些不估算的调用只进分母,比值被系统性压低。
_ew_token_estimate_actual_total = expert_work_counter(
    "expert_work_ew_token_estimate_actual_total",
    "Provider-reported prompt tokens of the calls counted in "
    "expert_work_ew_token_estimated_total (HX-1 drift denominator, B-104).",
    ("tenant_id", "agent_name", "model", "usage_kind"),
)


@dataclass(frozen=True)
class MeteredCall:
    """一次记下的调用 —— :func:`usage_tap` 收到的东西。

    ``usage_metadata`` 是响应原样的用量(含 ``output_token_details.reasoning``,
    ``token_usage`` 行没有这一列);缓存命中落的全 0 行这里是 ``None``。
    """

    provider: str | None
    model: str
    usage_kind: str
    usage_metadata: Mapping[str, Any] | None


_USAGE_TAP: ContextVar[Callable[[MeteredCall], None] | None] = ContextVar(
    "expert_work_usage_tap", default=None
)


@contextmanager
def usage_tap(tap: Callable[[MeteredCall], None]) -> Iterator[None]:
    """在这段执行(及其中创建的任务)里,每记一次调用就交给 ``tap`` 一份。

    **替换**而不是叠加外层的 tap:嵌套的 worker 收自己的,外层不重复收(孙 worker 的
    账记在孙 worker 自己的 end 帧上)。
    """
    token = _USAGE_TAP.set(tap)
    try:
        yield
    finally:
        _USAGE_TAP.reset(token)


@dataclass
class TokenUsageMiddleware:
    """``after_llm_call`` — emit counter + persist one row per LLM call.

    Errors in the metrics counter or DB insert are logged at ``warning``
    and swallowed: token-usage observability is **never** allowed to
    fail the LLM call. The persistence write happens inside the same
    request as the rest of the after-call chain; the store
    implementation is responsible for its own RLS handling.
    """

    store: TokenUsageStore
    agent_name: str
    agent_version: str
    model: str
    # Stream Y-3 — the ModelSpec provider, baked in at construction so Y4 can
    # price by ``(provider, model)``. ``None`` when the caller can't supply it.
    provider: str | None = None
    # SE-16 (SE-A43) — what this build's LLM calls count as. The evolution
    # replay path builds agents with ``skill_evolution`` so with/without
    # replay spend never pollutes the agent's conversation cost.
    usage_kind: str = "conversation"
    # Stream HX-1 (Mini-ADR HX-A6) — when injected, the estimator re-counts
    # the prompt that was actually sent (``payload["prompt_messages"]``) so
    # the drift counter accumulates next to the provider-reported truth.
    estimator: TokenEstimator | None = None
    # B-102 —— 响应 → 实际应答模型的配置 ``(provider, model)``;``None`` 时一律记
    # ``provider`` / ``model``。
    served_by: Callable[[AIMessage], tuple[str, str]] | None = None

    name: str = "token_usage"
    anchor: str = "after_llm_call"
    after: tuple[str, ...] = field(default_factory=tuple)
    before: tuple[str, ...] = field(default_factory=tuple)

    async def __call__(self, ctx: MiddlewareContext, call_next: CallNext) -> None:
        # Run downstream first so middlewares that depend on the
        # response (cache store, langfuse) finish before we account
        # the call — this keeps the persisted timestamp closer to
        # "observed by the chain" than "received from the LLM".
        await call_next(ctx)

        tenant_id = ctx.payload.get("tenant_id")
        response = ctx.payload.get("response")
        if not isinstance(tenant_id, UUID) or not isinstance(response, AIMessage):
            return

        counts = _extract_token_counts(response.usage_metadata)
        if counts is None:
            # B-66 — E.13 缓存条目不存 ``usage_metadata``,命中时读回来的消息没有用量。
            # 仍落一行全 0(见模块 docstring 的 ``cache_hit`` 条):否则对外用量把
            # 「命中缓存、零上游开销」报成「无记录」。未命中又没有用量的调用照旧不落。
            if ctx.payload.get("cache_hit") is not True:
                return
            counts = (0, 0, 0, 0)
        input_t, output_t, cache_creation_t, cache_read_t = counts
        provider: str | None = self.provider
        model = self.model
        if self.served_by is not None and ctx.payload.get("cache_hit") is not True:
            provider, model = self.served_by(response)

        # Counter — even when cache_hit=True we increment so dashboards
        # show the fact that a call landed; counts may legitimately be
        # zero (cache served, no upstream tokens spent).
        tenant_label = str(tenant_id)
        try:
            _llm_token_usage_total.labels(
                tenant_id=tenant_label,
                agent_name=self.agent_name,
                model=model,
                type=_TOKEN_TYPE_INPUT,
                usage_kind=self.usage_kind,
            ).inc(input_t)
            _llm_token_usage_total.labels(
                tenant_id=tenant_label,
                agent_name=self.agent_name,
                model=model,
                type=_TOKEN_TYPE_OUTPUT,
                usage_kind=self.usage_kind,
            ).inc(output_t)
            if cache_creation_t > 0:
                _llm_token_usage_total.labels(
                    tenant_id=tenant_label,
                    agent_name=self.agent_name,
                    model=model,
                    type=_TOKEN_TYPE_CACHE_CREATION,
                    usage_kind=self.usage_kind,
                ).inc(cache_creation_t)
            if cache_read_t > 0:
                _llm_token_usage_total.labels(
                    tenant_id=tenant_label,
                    agent_name=self.agent_name,
                    model=model,
                    type=_TOKEN_TYPE_CACHE_READ,
                    usage_kind=self.usage_kind,
                ).inc(cache_read_t)
        except Exception:
            logger.warning(
                "token_usage.counter_failed tenant=%s agent=%s model=%s",
                tenant_label,
                self.agent_name,
                model,
                exc_info=True,
            )

        # Stream HX-1 — drift numerator. Skipped on local-cache hits
        # (no upstream tokens were spent, the denominator stays 0) and
        # when the prompt view is unavailable. Same never-fail contract
        # as the counters above.
        if self.estimator is not None and not ctx.payload.get("cache_hit"):
            prompt = ctx.payload.get("prompt_messages")
            if isinstance(prompt, list) and prompt:
                try:
                    estimated = estimate_messages(prompt, self.estimator)
                    _ew_token_estimated_total.labels(
                        tenant_id=tenant_label,
                        agent_name=self.agent_name,
                        model=model,
                        usage_kind=self.usage_kind,
                    ).inc(estimated)
                    _ew_token_estimate_actual_total.labels(
                        tenant_id=tenant_label,
                        agent_name=self.agent_name,
                        model=model,
                        usage_kind=self.usage_kind,
                    ).inc(input_t + cache_creation_t + cache_read_t)
                except Exception:
                    logger.warning(
                        "token_usage.estimate_failed tenant=%s agent=%s model=%s",
                        tenant_label,
                        self.agent_name,
                        model,
                        exc_info=True,
                    )

        # Stream Agent-Templates (M1-5a) — per-user cost attribution. The run
        # worker threads the end-user (tenant_user.id) through configurable →
        # payload; absent / non-UUID (system / preview builds) → NULL.
        usage_user_id = ctx.payload.get("user_id")
        if not isinstance(usage_user_id, UUID):
            usage_user_id = None
        tap = _USAGE_TAP.get()
        if tap is not None:
            try:
                tap(
                    MeteredCall(
                        provider=provider,
                        model=model,
                        usage_kind=self.usage_kind,
                        usage_metadata=(
                            response.usage_metadata
                            if isinstance(response.usage_metadata, Mapping)
                            else None
                        ),
                    )
                )
            except Exception:
                logger.warning("token_usage.tap_failed model=%s", model, exc_info=True)
        try:
            await self.store.insert(
                TokenUsageRecord(
                    tenant_id=tenant_id,
                    agent_name=self.agent_name,
                    agent_version=self.agent_version,
                    model=model,
                    provider=provider,
                    user_id=usage_user_id,
                    usage_kind=self.usage_kind,
                    input_tokens=input_t,
                    output_tokens=output_t,
                    cache_creation_tokens=cache_creation_t,
                    cache_read_tokens=cache_read_t,
                    trace_id=current_trace_id_hex(),
                )
            )
        except Exception:
            logger.warning(
                "token_usage.persist_failed tenant=%s agent=%s model=%s",
                tenant_label,
                self.agent_name,
                model,
                exc_info=True,
            )


def _extract_token_counts(
    usage_metadata: Any,
) -> tuple[int, int, int, int] | None:
    """Pull (input, output, cache_creation, cache_read) out of LangChain's
    ``usage_metadata`` shape.

    Returns ``None`` when nothing useful is present (eg. provider didn't
    populate usage) — the middleware then no-ops. The cache counters
    live in ``usage_metadata['input_token_details']`` per LangChain's
    convention (see Stream L.L1).
    """
    if not isinstance(usage_metadata, dict):
        return None
    input_t = _coerce_int(usage_metadata.get("input_tokens"))
    output_t = _coerce_int(usage_metadata.get("output_tokens"))
    details = usage_metadata.get("input_token_details")
    cache_creation_t = 0
    cache_read_t = 0
    if isinstance(details, dict):
        cache_creation_t = _coerce_int(details.get("cache_creation")) or 0
        cache_read_t = _coerce_int(details.get("cache_read")) or 0
    if input_t is None and output_t is None and cache_creation_t == 0 and cache_read_t == 0:
        return None
    return (input_t or 0, output_t or 0, cache_creation_t, cache_read_t)


def _coerce_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return None
