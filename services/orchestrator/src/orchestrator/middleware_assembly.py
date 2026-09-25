"""Assemble per-anchor :class:`MiddlewareChain`\\s from an :class:`AgentSpec`.

STREAM-E-DESIGN Mini-ADR E-15: middleware splits into two groups.

* **always-on** — :class:`DynamicContextMiddleware`,
  :class:`LLMErrorHandlingMiddleware`, :class:`LoopDetectionMiddleware`.
  No platform dependency; every agent gets them (cost / stability floor).
  The old sandbox-exec call denylist was removed (it was bypassable theater —
  the gVisor sandbox is the real boundary); sandbox code is now recorded into
  the tool audit instead. See docs/design/sandbox-audit-evaluation.md.
* **env-gated** — :class:`PIIRedactorMiddleware`,
  :class:`LLMCacheLookupMiddleware` / :class:`LLMCacheStoreMiddleware`,
  :class:`LangfuseMiddleware`. Each needs a platform runtime dep
  (redactor / cache / Langfuse client) injected via :class:`MiddlewareEnv`;
  absent the dep, the middleware is silently skipped.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from langchain_core.messages import BaseMessage

from expert_work.persistence.token_usage_store import TokenUsageStore
from expert_work.protocol import AgentSpec
from expert_work.runtime.llm.cache import LLMResponseCache
from expert_work.runtime.middleware import (
    BreakerRegistry,
    ContextPressureMiddleware,
    DynamicContextMiddleware,
    LangfuseClient,
    LangfuseMiddleware,
    LLMCacheLookupMiddleware,
    LLMCacheStoreMiddleware,
    LLMErrorHandlingMiddleware,
    LoopDetectionMiddleware,
    Middleware,
    MiddlewareChain,
    PIIRedactorMiddleware,
    RedactText,
    TokenUsageMiddleware,
)
from expert_work.runtime.tokens import TokenEstimator, estimate_message
from orchestrator.usage_metering import ServedModelResolver, chain_models


@dataclass(frozen=True)
class MiddlewareEnv:
    """Platform runtime deps for the env-gated middlewares.

    A field left ``None`` means that middleware is not wired (Mini-ADR
    E-15). An empty ``MiddlewareEnv()`` still yields the three always-on
    middlewares.
    """

    langfuse_client: LangfuseClient | None = None
    response_cache: LLMResponseCache | None = None
    redact_text: RedactText | None = None
    #: Shared circuit-breaker registry. ``None`` → each agent gets its
    #: own; inject a shared one to pool breaker state across agents.
    breaker_registry: BreakerRegistry | None = None
    #: Stream G.9 — per-LLM-call token-usage recorder. ``None`` skips
    #: both the Prometheus counter (still defined but never incremented
    #: by this agent) and the DB persistence. M0 wires the store from
    #: ``control_plane.app``; tests can leave it unset.
    token_usage_store: TokenUsageStore | None = None


@dataclass(frozen=True)
class MiddlewareChains:
    """The four anchor chains :func:`build_agent` threads into the graph
    and the router. An anchor with no middleware is ``None`` so the graph
    keeps its no-chain fast path."""

    before_llm_call: MiddlewareChain | None
    around_llm_call: MiddlewareChain | None
    after_llm_call: MiddlewareChain | None
    before_tool_dispatch: MiddlewareChain | None


def build_middleware_chains(
    spec: AgentSpec,
    *,
    env: MiddlewareEnv | None = None,
    estimator: TokenEstimator | None = None,
    context_window: int | None = None,
    token_usage_kind: str = "conversation",  # noqa: S107 — usage label, not a secret
) -> MiddlewareChains:
    """Build the anchor chains for ``spec`` (Mini-ADR E-15).

    ``estimator`` (Stream HX-1, Mini-ADR HX-A1) threads the shared token
    estimator into the dynamic-context trim and the token-usage drift
    metric; ``None`` keeps the legacy ``chars // 4`` heuristic.

    ``context_window`` (3.3) is the model's resolved window, the denominator
    the context-pressure middleware measures usage against. The control-plane
    passes ``_resolved_context_window``; ``None`` skips the pressure note
    (no window → no ratio).
    """
    env = env or MiddlewareEnv()
    model = spec.spec.model
    middlewares: list[Middleware] = [
        LLMErrorHandlingMiddleware(breaker_registry=env.breaker_registry or BreakerRegistry()),
        LoopDetectionMiddleware(),
    ]
    # Stream HX-1 (Mini-ADR HX-A5) — the E.3 view trim is opt-in: it is
    # only built when the manifest sets an explicit cap.
    dynamic_context = _dynamic_context(spec, estimator=estimator)
    if dynamic_context is not None:
        middlewares.insert(0, dynamic_context)

    # 3.3 — context-pressure feedback. Default ON + threshold-gated; needs a
    # resolved window to compute usage. Runs after dynamic_context (declared
    # ``after``) so it measures the real post-trim view.
    pressure = _context_pressure(spec, context_window=context_window, estimator=estimator)
    if pressure is not None:
        middlewares.append(pressure)

    if env.redact_text is not None:
        middlewares.append(PIIRedactorMiddleware(redact_text=env.redact_text))
    # Stream K.K4 (Mini-ADR K-3) — manifest can opt out of the LLM
    # response cache entirely. Time-sensitive agents (date / latest-news
    # / per-call randomness) must set ``spec.cache.enabled: false`` so
    # cache hits don't return stale answers. Default ``CacheSpec()`` is
    # ``enabled=True`` to preserve existing manifests.
    if env.response_cache is not None and spec.spec.cache.enabled:
        middlewares.append(
            LLMCacheLookupMiddleware(
                cache=env.response_cache,
                model=model.name,
                temperature=model.temperature,
                # B-105 —— 0 = 厂商默认(未设输出上限),只用于缓存键。
                max_tokens=model.max_tokens or 0,
            )
        )
        middlewares.append(
            LLMCacheStoreMiddleware(
                cache=env.response_cache,
                model=model.name,
                temperature=model.temperature,
                # B-105 —— 0 = 厂商默认(未设输出上限),只用于缓存键。
                max_tokens=model.max_tokens or 0,
            )
        )
    if env.langfuse_client is not None:
        middlewares.append(LangfuseMiddleware(client=env.langfuse_client))
    if env.token_usage_store is not None:
        middlewares.append(
            TokenUsageMiddleware(
                store=env.token_usage_store,
                agent_name=spec.metadata.name,
                agent_version=spec.metadata.version,
                model=model.name,
                provider=model.provider,
                estimator=estimator,
                usage_kind=token_usage_kind,
                # B-102 —— 记在实际应答的模型名下(备用接管时是备用的配置名);agent 构建
                # 给主循环路由加了盖章层(``orchestrator.usage_metering``)。
                served_by=ServedModelResolver(
                    default=(model.provider, model.name), models=chain_models(model)
                ),
            )
        )

    return MiddlewareChains(
        before_llm_call=_chain("before_llm_call", middlewares),
        around_llm_call=_chain("around_llm_call", middlewares),
        after_llm_call=_chain("after_llm_call", middlewares),
        before_tool_dispatch=_chain("before_tool_dispatch", middlewares),
    )


def _dynamic_context(
    spec: AgentSpec,
    *,
    estimator: TokenEstimator | None = None,
) -> DynamicContextMiddleware | None:
    """Build the E.3 view-trim middleware — or ``None`` when the manifest
    doesn't opt in.

    Stream HX-1 (Mini-ADR HX-A5) — ``policies.context_compression.max_turns``
    / ``max_tokens`` both default ``None``, which skips the middleware
    entirely: the M0 naïve trim's legacy defaults (20 messages / 8000
    tokens) silently capped every LLM call far below the
    ``context_window``-proportional thresholds the CM-2 window + L2
    compressor manage. Setting either field opts the trim back in; an
    unset axis falls to the middleware's own constructor default.

    ``estimator`` (when injected) replaces the middleware's default
    ``chars // 4`` per-message heuristic through its existing
    ``token_estimator`` seam, so all context gates share one estimation
    basis."""
    cc = spec.spec.policies.context_compression
    if cc.max_turns is None and cc.max_tokens is None:
        return None
    kwargs: dict[str, Any] = {}
    if cc.max_turns is not None:
        kwargs["max_turns"] = cc.max_turns
    if cc.max_tokens is not None:
        kwargs["max_tokens"] = cc.max_tokens
    if estimator is not None:
        shared = estimator

        def _per_message(msg: BaseMessage) -> int:
            return estimate_message(msg, shared)

        kwargs["token_estimator"] = _per_message
    return DynamicContextMiddleware(**kwargs)


def _context_pressure(
    spec: AgentSpec,
    *,
    context_window: int | None,
    estimator: TokenEstimator | None = None,
) -> ContextPressureMiddleware | None:
    """Build the 3.3 context-pressure middleware — or ``None`` when the
    manifest opts out or no resolved window is available.

    Default ON (``policies.context_compression.pressure_feedback``); the
    note itself is threshold-gated inside the middleware, so a conversation
    below ``pressure_warn_pct`` is untouched. ``estimator`` shares the same
    tiktoken basis as the trim / drift metric via the ``token_estimator``
    seam; ``context_window`` is the usage denominator (skip when unknown).
    """
    cc = spec.spec.policies.context_compression
    if not cc.pressure_feedback or context_window is None:
        return None
    kwargs: dict[str, Any] = {
        "context_window": context_window,
        "warn_pct": cc.pressure_warn_pct,
    }
    if estimator is not None:
        shared = estimator

        def _per_message(msg: BaseMessage) -> int:
            return estimate_message(msg, shared)

        kwargs["token_estimator"] = _per_message
    return ContextPressureMiddleware(**kwargs)


def _chain(anchor: str, middlewares: list[Middleware]) -> MiddlewareChain | None:
    """A chain for ``anchor``, or ``None`` when no middleware binds there."""
    scoped = [m for m in middlewares if m.anchor == anchor]
    if not scoped:
        return None
    return MiddlewareChain.from_middlewares(anchor, scoped)
