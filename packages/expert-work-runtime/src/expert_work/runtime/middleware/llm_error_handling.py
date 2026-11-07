"""LLM error-handling middleware — Stream E.4.

Wraps the LLM call with:

- per-upstream **circuit breaker** (:mod:`.circuit_breaker`, B-297: every
  failed attempt counts, 3 in a row or ≥50 % of ≥5 in 60 s → OPEN,
  30 s → 300 s exponential cooldown, single half-open probe)
- **retry with backoff** for transient failures (5xx, 429, network errors);
  4xx surfaces immediately (caller bug, never retry). B-297: the budget is
  short (≤1 retry, ≤15 s) when the router still has a backup provider, the
  long-standing one (3 exponential retries) when it does not.

Registers to the ``around_llm_call`` anchor so it can catch exceptions
raised by the terminal handler (the actual LLM call) and decide whether
to retry. The breaker state persists across middleware invocations via
:class:`BreakerRegistry` — one per control-plane process, shared by every
agent build — keyed by the router's breaker key (``provider:model[#idx]``
plus a credential/endpoint fingerprint), so two keys or two tenants'
credentials for the same model get isolated breakers.

Per [STREAM-E-DESIGN § 2.2 + Mini-ADR E-4](../../../../../../../docs/streams/STREAM-E-DESIGN.md).
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from expert_work.common.observability.metrics import expert_work_counter
from expert_work.runtime.middleware.base import CallNext, MiddlewareContext
from expert_work.runtime.middleware.circuit_breaker import (
    BreakerRegistry as BreakerRegistry,
)
from expert_work.runtime.middleware.circuit_breaker import (
    BreakerState as BreakerState,
)
from expert_work.runtime.middleware.circuit_breaker import (
    CircuitBreaker as CircuitBreaker,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# LLM error hierarchy
# ---------------------------------------------------------------------------


class LLMError(Exception):
    """Base class for LLM-call errors. Adapters (E.11) raise subclasses."""

    #: B-297 — the vendor's ``Retry-After`` (seconds) when the HTTP response
    #: carried one (``classify_http_error`` fills it); ``None`` otherwise. The
    #: E.4 middleware honours it for the retry wait and the breaker cooldown.
    retry_after_s: float | None = None


class LLMClientError(LLMError):
    """4xx-class error — caller's fault (bad request, auth fail).

    **Never retried** and **does not** trip the circuit breaker.
    """

    #: B-105 —— 上游 HTTP 状态码(``classify_http_error`` 抛出时填);非 HTTP 来源为 ``None``。
    status: int | None = None


class LLMUnauthorizedError(LLMClientError):
    """Stream L.L8 — HTTP 401-class error specifically.

    A subclass of :class:`LLMClientError` so existing 4xx-no-fallback
    semantics still apply for non-OAuth providers. OAuth-capable
    providers (see
    :class:`~orchestrator.llm.oauth_provider.OAuthCapableProvider`)
    let the router intercept this exception, call
    ``refresh_credentials()`` once, and retry the call.
    """


class LLMServerError(LLMError):
    """5xx-class error — vendor-side fault. Retried + counts toward breaker."""


class LLMAuthError(LLMServerError):
    """Stream L.L8 — credential refresh attempted and still unauthorized.

    Raised by :class:`~orchestrator.llm.router.LLMRouter` *after* a
    single ``refresh_credentials()`` retry on a 401 still fails (or
    the refresh itself returned ``False``). Inherits
    :class:`LLMServerError` so the router treats it as retryable and
    falls back to the next provider in the chain — a provider whose
    credentials are confirmed broken should not lock the run when
    another upstream may still answer.
    """


class LLMRateLimitError(LLMError):
    """429 — vendor rate-limit. Retried + counts toward breaker.

    Kept separate from server error so callers can apply jitter / vendor
    ``Retry-After`` headers in future hardening passes (M1-D).
    """


class LLMKeyUnavailableError(LLMError):
    """Stream Y-MK — this API key / billing account is currently unusable.

    Covers account-level failures that a *sibling key* on the same provider
    can recover from: balance exhausted (e.g. DeepSeek HTTP 402 "Insufficient
    Balance"), quota/billing exhausted (OpenAI 429 ``insufficient_quota`` /
    403 billing), or a revoked static key (401 on a non-OAuth provider, after
    the router's OAuth-refresh path declines it).

    Unlike :class:`LLMRateLimitError` this is **not retried** — exponential
    backoff cannot un-exhaust a dead account, so the middleware records the
    failure (tripping the per-key breaker so the key is skipped during its
    cooldown) and re-raises immediately. The E.11 :class:`LLMRouter` then
    advances to the next key *of the same provider* before falling through to
    the next provider (Stream Y-MK two-level chain).
    """


class LLMNetworkError(LLMError):
    """Connection / timeout / TLS error — treated as retryable server-side."""


class LLMStreamStaleError(LLMError):
    """Stream L.L3 / L (P1) — the provider produced no token within the
    first-token / wall-clock deadline.

    A plain :class:`LLMError` (NOT :class:`LLMServerError`), so it is
    deliberately absent from :data:`_RETRYABLE_ERRORS`: E.4
    :class:`LLMErrorHandlingMiddleware` must NOT retry a hung provider
    in-place (P1 moved the deadline inside the middleware chain; retrying
    a first-token timeout up to ``max_retries`` times would delay
    fallover by that multiple). The router's ``__call__`` still catches
    it via its generic ``except LLMError`` arm and falls over to the next
    provider — prompt fallover, no in-place retries. A hung provider
    should not lock the run; another provider may still respond.
    """


class LLMStreamInterruptedError(LLMError):
    """Stream L (P1) — a streaming provider stalled or errored AFTER the
    first progress delta. Buffer-until-first-token commits the run to
    that provider once tokens flow, so this is terminal: it inherits the
    plain :class:`LLMError` (NOT :class:`LLMServerError`), so the E.4
    error-handling middleware does not retry it and the router does not
    fall back. Carries the partial :class:`AIMessage` assembled so far
    for callers that want to surface it."""

    def __init__(self, message: str, *, partial: object | None = None) -> None:
        super().__init__(message)
        self.partial = partial


class LLMOutputValidationError(LLMError):
    """Stream RT-1 — structured output still failed schema validation
    after the correction retries (RT-ADR-1).

    Raised by :class:`~orchestrator.llm.router.LLMRouter` when a call
    made with ``output_schema`` set keeps producing responses that fail
    JSON-Schema validation. A validation failure is model behaviour,
    not a key or provider fault: this error MUST NOT join the router's
    ``_KEY_LEVEL_ERRORS`` (no key rotation) and MUST NOT trigger
    provider failover — the router re-raises it unchanged so each
    caller's existing defensive degradation path applies.
    """


class CircuitOpenError(LLMError):
    """Breaker is OPEN; raised immediately without dispatching the call.

    Carries the breaker key so callers (E.11 LLMRouter) can decide to
    fall back to a different provider key.
    """

    def __init__(self, key: str) -> None:
        super().__init__(f"circuit breaker OPEN for key={key!r}")
        self.key = key


_RETRYABLE_ERRORS: tuple[type[LLMError], ...] = (
    LLMServerError,
    LLMRateLimitError,
    LLMNetworkError,
)


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------

# B-297 — one increment per CLOSED/HALF_OPEN → OPEN transition. Labelled by the
# router's ``provider_key`` (``provider:model[#idx]``) — never the breaker key,
# whose credential fingerprint would make the label set grow with tenants.
_breaker_open_total = expert_work_counter(
    "expert_work_llm_breaker_open_total",
    "LLM upstream circuit breakers that opened (B-297).",
    ("provider_key",),
)

#: B-297 — failures that count toward the breaker but are never retried
#: in place: a hung provider (first-token timeout) and a stream that died
#: after its first token (committed to this provider; the router re-raises).
_COUNTED_NOT_RETRIED: tuple[type[LLMError], ...] = (
    LLMKeyUnavailableError,
    LLMStreamStaleError,
    LLMStreamInterruptedError,
)


def _default_key_extractor(ctx: MiddlewareContext) -> str:
    """The breaker key: ``ctx.payload["breaker_key"]`` (router-supplied,
    ``provider:model[#idx]`` + credential/endpoint fingerprint), else
    ``provider_key``, else ``"default"`` (pre-router dev / tests)."""
    raw = ctx.payload.get("breaker_key") or ctx.payload.get("provider_key", "default")
    return str(raw)


@dataclass
class LLMErrorHandlingMiddleware:
    """Wrap one provider attempt with in-place retry + the circuit breaker.

    B-297 — the retry budget depends on whether the router still has another
    provider group to fail over to (``ctx.payload["has_fallback"]``, absent =
    ``False``):

    * **with a fallback** — at most ``fallback_max_retries`` (1) same-provider
      retry after a short jittered backoff (≤ ``fallback_max_delay_s``), and
      only while the whole provider attempt stays inside
      ``fallback_budget_s`` (15 s; a retry is assumed to take as long as the
      attempt that just failed). An OPEN breaker refuses at once
      (:class:`CircuitOpenError` → the router moves on), and so does a
      ``Retry-After`` longer than the remaining budget.
    * **without a fallback** — the long-standing budget: ``max_retries`` (3)
      retries, exponential ``base_delay_s * 2**n`` capped at ``max_delay_s``,
      or ``min(Retry-After, max_delay_s)`` when the vendor sent one. The
      breaker is still fed but does not short-circuit: refusing the only
      model would turn a maybe-transient blip into a certain failure
      (LiteLLM likewise never cools down a group's only deployment).

    Every failed upstream attempt counts toward the breaker (see
    :mod:`.circuit_breaker`); a 4xx client error is neutral.
    """

    breaker_registry: BreakerRegistry = field(default_factory=BreakerRegistry)
    max_retries: int = 3
    base_delay_s: float = 1.0
    max_delay_s: float = 30.0
    fallback_max_retries: int = 1
    fallback_base_delay_s: float = 0.5
    fallback_max_delay_s: float = 1.0
    fallback_budget_s: float = 15.0
    key_extractor: Callable[[MiddlewareContext], str] = field(default=_default_key_extractor)
    sleeper: Callable[[float], Awaitable[None]] = field(default=asyncio.sleep)
    clock: Callable[[], float] = field(default=time.monotonic)
    rand: Callable[[], float] = field(default=random.random)

    name: str = "llm_error_handling"
    anchor: str = "around_llm_call"
    after: tuple[str, ...] = field(default_factory=tuple)
    before: tuple[str, ...] = field(default_factory=tuple)

    async def __call__(self, ctx: MiddlewareContext, call_next: CallNext) -> None:
        key = self.key_extractor(ctx)
        provider_key = str(ctx.payload.get("provider_key", "default"))
        has_fallback = bool(ctx.payload.get("has_fallback", False))
        breaker = await self.breaker_registry.get(key)
        started = self.clock()

        last_exc: LLMError | None = None
        attempt = 0
        while True:
            admission = await breaker.acquire()
            if admission == "rejected" and has_fallback:
                if last_exc is not None:
                    raise last_exc
                raise CircuitOpenError(key)
            probe = admission == "probe"
            attempt_started = self.clock()
            try:
                await call_next(ctx)
            except _COUNTED_NOT_RETRIED:
                await self._record_failure(breaker, provider_key, key, probe=probe)
                raise
            except LLMClientError:
                # 4xx: the request's fault, not the upstream's — neutral.
                await breaker.release(probe=probe)
                raise
            except _RETRYABLE_ERRORS as exc:
                last_exc = exc
                opened = await self._record_failure(
                    breaker, provider_key, key, probe=probe, retry_after_s=exc.retry_after_s
                )
                now = self.clock()
                delay = self._retry_delay(
                    exc,
                    attempt,
                    has_fallback=has_fallback,
                    elapsed_s=now - started,
                    last_attempt_s=now - attempt_started,
                )
                logger.warning(
                    "llm_error_handling.retryable attempt=%d key=%s err=%s has_fallback=%s "
                    "retry_in_s=%s",
                    attempt + 1,
                    provider_key,
                    type(exc).__name__,
                    has_fallback,
                    "none" if delay is None else f"{delay:.2f}",
                )
                if delay is None or (opened and has_fallback):
                    raise
                await self.sleeper(delay)
                attempt += 1
                continue
            except BaseException:
                # Anything else (cancellation, adapter bug): not evidence
                # about the upstream's health — free a probe slot and go.
                await breaker.release(probe=probe)
                raise
            else:
                await breaker.record_success(probe=probe)
                return

    def _retry_delay(
        self,
        exc: LLMError,
        attempt: int,
        *,
        has_fallback: bool,
        elapsed_s: float,
        last_attempt_s: float,
    ) -> float | None:
        """Seconds to wait before retry number ``attempt + 1``, or ``None``
        to stop retrying and re-raise (→ the router fails over)."""
        retry_after = exc.retry_after_s
        if not has_fallback:
            if attempt >= self.max_retries:
                return None
            if retry_after is not None:
                return min(retry_after, self.max_delay_s)
            return float(min(self.max_delay_s, self.base_delay_s * (2**attempt)))
        if attempt >= self.fallback_max_retries:
            return None
        cap = min(self.fallback_max_delay_s, self.fallback_base_delay_s * (2.0**attempt))
        # "Equal jitter": half fixed, half random — never 0, never above cap.
        delay = cap * (0.5 + 0.5 * self.rand())
        if retry_after is not None:
            delay = max(delay, retry_after)
        # A retry is assumed to take as long as the attempt that just failed;
        # a Retry-After beyond the remaining budget fails over here too.
        if elapsed_s + delay + last_attempt_s > self.fallback_budget_s:
            return None
        return delay

    async def _record_failure(
        self,
        breaker: CircuitBreaker,
        provider_key: str,
        key: str,
        *,
        probe: bool,
        retry_after_s: float | None = None,
    ) -> bool:
        opened = await breaker.record_failure(probe=probe, retry_after_s=retry_after_s)
        if opened:
            _breaker_open_total.labels(provider_key=provider_key).inc()
            logger.warning(
                "llm_breaker.open provider_key=%s breaker_key=%s consecutive_failures=%d",
                provider_key,
                key,
                breaker.consecutive_failures,
            )
        return opened
