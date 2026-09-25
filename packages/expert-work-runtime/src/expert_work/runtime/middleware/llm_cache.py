"""LLM response cache middlewares — Stream E.13.

Two middlewares wrap the :class:`LLMResponseCache` onto the chain:

- :class:`LLMCacheLookupMiddleware` (``before_llm_call``) — derives the
  cache key, and on a hit stashes the cached :class:`AIMessage` under
  ``ctx.payload["llm_cache_hit"]``. The agent node checks that key and
  **skips the LLM call entirely** when present.
- :class:`LLMCacheStoreMiddleware` (``after_llm_call``) — stores the
  fresh response, unless this turn was itself a cache hit
  (``ctx.payload["cache_hit"]`` is ``True``) or the call is not
  cacheable.

The two middlewares cannot share in-process state — each chain
``invoke`` builds a fresh :class:`MiddlewareContext`. So they
independently re-derive the cache key from their own payloads;
:func:`~expert_work.runtime.llm.cache.is_cacheable` is the single
source of truth both consult, guaranteeing lookup and store agree on
which calls are cacheable.

Why two middlewares on two anchors rather than one at ``around_llm_call``:
the around anchor fires **per provider** inside ``LLMRouter`` (Mini-ADR
E-13), but the cache must be consulted **once**, before the router even
picks a provider. ``before_llm_call`` / ``after_llm_call`` fire exactly
once per agent step — the correct granularity.

Per [STREAM-E-DESIGN § 2.8](../../../../../../../docs/streams/STREAM-E-DESIGN.md).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from uuid import UUID

from langchain_core.messages import AIMessage, BaseMessage

from expert_work.protocol import StructuredOutputSpec
from expert_work.runtime.llm.cache import LLMResponseCache, is_cacheable
from expert_work.runtime.middleware.base import CallNext, MiddlewareContext

logger = logging.getLogger(__name__)

#: B-105 —— ``after_llm_call`` 载荷里的「照常记账,但别存进缓存」标记。截断成不可用的回答
#: 已经计费,要落用量;但存进缓存会让下一次同样的提示词直接拿到这个坏回答。
SKIP_STORE_KEY = "llm_cache_skip_store"


def _coerce_messages(raw: object) -> list[BaseMessage]:
    if isinstance(raw, list):
        return [m for m in raw if isinstance(m, BaseMessage)]
    return []


def _coerce_output_schema(raw: object) -> StructuredOutputSpec | None:
    """The ``output_schema`` payload entry, when it is a real spec.

    Stream RT-1 PR-2 — a structured call must be keyed apart from an
    unstructured call over the same messages. Anything other than a
    :class:`StructuredOutputSpec` (e.g. the router's ``{name, strict}``
    summary dict on the *around* anchor) is ignored, falling back to
    the unstructured key.

    Activation: live since RT-1 PR-3 — the agent loop's structured
    finalization resend places its :class:`StructuredOutputSpec`
    instance into ``payload["output_schema"]`` on BOTH anchors (lookup
    and store key identically; either side missing would make
    structured entries write-only). Unstructured calls still omit the
    key, so their derivation stays byte-identical to pre-RT-1 and
    existing entries remain reachable. Aux structured calls bypass
    these anchors entirely.
    """
    return raw if isinstance(raw, StructuredOutputSpec) else None


@dataclass
class LLMCacheLookupMiddleware:
    """``before_llm_call`` — populate ``ctx.payload["llm_cache_hit"]`` on a hit.

    Reads ``ctx.payload``:

    - ``messages`` — the prompt about to go to the LLM (post
      dynamic_context / pii_redact rewrite — this middleware declares
      ``after`` so it runs last in the anchor).
    - ``tenant_id`` — per-tenant namespace; missing → cache disabled
      for this call (dev / unit-test path that doesn't bind a tenant).
    - ``output_schema`` — optional :class:`StructuredOutputSpec` for a
      structured call (Stream RT-1); joins the cache key as a
      fingerprint so structured and unstructured calls never collide.
      Set by the RT-1 PR-3 agent-loop wiring; see
      :func:`_coerce_output_schema` for the activation contract.
    - ``llm_cache_bypass`` — ``True`` skips the lookup (B-66, a
      ``:regenerate`` run). The store middleware is unaffected.

    ``model`` / ``temperature`` / ``max_tokens`` are per-agent
    constants supplied at construction (from the manifest ``ModelSpec``)
    — they do not vary call to call within one agent.
    """

    cache: LLMResponseCache
    model: str
    temperature: float
    max_tokens: int

    name: str = "llm_cache_lookup"
    anchor: str = "before_llm_call"
    #: Run after the prompt-mutating middlewares so the cache key
    #: reflects the final prompt the LLM will actually see.
    after: tuple[str, ...] = ("dynamic_context", "pii_redact")
    before: tuple[str, ...] = field(default_factory=tuple)

    async def __call__(self, ctx: MiddlewareContext, call_next: CallNext) -> None:
        tenant_id = ctx.payload.get("tenant_id")
        messages = _coerce_messages(ctx.payload.get("messages"))
        # B-66 — ``:regenerate`` 重放的 prompt 与原轮字节相同;查了就必然命中旧答案。
        # 只跳过查找,store 侧照常写,之后同样的普通调用拿到的是新答案。
        bypass = ctx.payload.get("llm_cache_bypass") is True

        if not bypass and isinstance(tenant_id, UUID) and is_cacheable(messages, self.temperature):
            key = self.cache.make_key(
                tenant_id=tenant_id,
                model=self.model,
                messages=messages,
                temperature=self.temperature,
                max_tokens=self.max_tokens,
                output_schema=_coerce_output_schema(ctx.payload.get("output_schema")),
            )
            cached = await self.cache.get(key)
            if cached is not None:
                logger.info(
                    "llm_cache.hit tenant=%s model=%s key=%s",
                    tenant_id,
                    self.model,
                    key,
                )
                ctx.payload["llm_cache_hit"] = cached

        await call_next(ctx)


@dataclass
class LLMCacheStoreMiddleware:
    """``after_llm_call`` — persist a fresh, cacheable response.

    Reads ``ctx.payload``:

    - ``prompt_messages`` — the exact messages sent to the LLM (the
      cache-key input; identical to what the lookup middleware hashed).
    - ``response`` — the :class:`AIMessage` the LLM returned.
    - ``tenant_id`` — per-tenant namespace.
    - ``cache_hit`` — ``True`` when this turn was served from cache;
      storing again would be wasted work, so we skip.
    - :data:`SKIP_STORE_KEY` — ``True`` when the response must be metered
      but never replayed (B-105: a truncated, unusable reply).
    - ``output_schema`` — optional :class:`StructuredOutputSpec`; must
      mirror the lookup middleware's keying exactly, or structured
      entries would be stored under keys the lookup never derives.
    """

    cache: LLMResponseCache
    model: str
    temperature: float
    max_tokens: int
    ttl_s: int | None = None

    name: str = "llm_cache_store"
    anchor: str = "after_llm_call"
    after: tuple[str, ...] = field(default_factory=tuple)
    before: tuple[str, ...] = field(default_factory=tuple)

    async def __call__(self, ctx: MiddlewareContext, call_next: CallNext) -> None:
        await call_next(ctx)

        if ctx.payload.get("cache_hit") is True or ctx.payload.get(SKIP_STORE_KEY) is True:
            return

        tenant_id = ctx.payload.get("tenant_id")
        response = ctx.payload.get("response")
        messages = _coerce_messages(ctx.payload.get("prompt_messages"))

        if not isinstance(tenant_id, UUID) or not isinstance(response, AIMessage):
            return
        if not is_cacheable(messages, self.temperature):
            return

        key = self.cache.make_key(
            tenant_id=tenant_id,
            model=self.model,
            messages=messages,
            temperature=self.temperature,
            max_tokens=self.max_tokens,
            output_schema=_coerce_output_schema(ctx.payload.get("output_schema")),
        )
        await self.cache.put(key, response, self.ttl_s)
        logger.info(
            "llm_cache.store tenant=%s model=%s key=%s",
            tenant_id,
            self.model,
            key,
        )
