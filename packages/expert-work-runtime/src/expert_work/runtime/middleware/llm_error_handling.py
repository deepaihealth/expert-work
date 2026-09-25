"""LLM error-handling middleware — Stream E.4.

Wraps the LLM call with:

- per-provider-key **circuit breaker** (CLOSED → 5 consecutive failures →
  OPEN → 30 s cooldown → HALF_OPEN → CLOSED on success / OPEN on failure)
- **retry with exponential backoff** for transient failures (5xx, 429,
  network errors); 4xx surfaces immediately (caller bug, never retry)

Registers to the ``around_llm_call`` anchor so it can catch exceptions
raised by the terminal handler (the actual LLM call) and decide whether
to retry. The breaker state persists across middleware invocations via
:class:`BreakerRegistry`, keyed by provider key — typically the API
key's hash or name, so two keys for the same vendor (primary + fallback)
get isolated breakers.

Per [STREAM-E-DESIGN § 2.2 + Mini-ADR E-4](../../../../../../../docs/streams/STREAM-E-DESIGN.md),
M0 ships the minimal breaker; production hardening (jittered backoff,
per-tenant rate-limit-error budgets) is M1-D territory.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Literal

from expert_work.runtime.middleware.base import CallNext, MiddlewareContext

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# LLM error hierarchy
# ---------------------------------------------------------------------------


class LLMError(Exception):
    """Base class for LLM-call errors. Adapters (E.11) raise subclasses."""


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
# Circuit breaker
# ---------------------------------------------------------------------------


BreakerState = Literal["CLOSED", "OPEN", "HALF_OPEN"]


class CircuitBreaker:
    """Three-state breaker, async-safe.

    CLOSED → consecutive_failures ≥ threshold → OPEN
    OPEN   → cooldown_s elapsed → HALF_OPEN (on next ``check``)
    HALF_OPEN → next outcome decides: success → CLOSED, failure → OPEN

    ``check_state`` is the public read; the OPEN → HALF_OPEN transition
    is **lazy** — only realised when a caller checks state, so we don't
    need a background scheduler.

    Uses a monotonic clock so tests can inject a fake clock and
    production isn't sensitive to wall-clock jumps.
    """

    __slots__ = (
        "_clock",
        "_consecutive_failures",
        "_cooldown_s",
        "_failure_threshold",
        "_lock",
        "_opened_at",
        "_state",
    )

    def __init__(
        self,
        *,
        failure_threshold: int = 5,
        cooldown_s: float = 30.0,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._failure_threshold = failure_threshold
        self._cooldown_s = cooldown_s
        self._clock = clock or time.monotonic
        self._state: BreakerState = "CLOSED"
        self._consecutive_failures = 0
        self._opened_at: float | None = None
        self._lock = asyncio.Lock()

    async def check_state(self) -> BreakerState:
        """Return the current state, lazily transitioning OPEN → HALF_OPEN."""
        async with self._lock:
            if self._state == "OPEN" and self._opened_at is not None:
                if self._clock() - self._opened_at >= self._cooldown_s:
                    self._state = "HALF_OPEN"
                    logger.info("circuit_breaker.half_open")
            return self._state

    async def record_success(self) -> None:
        async with self._lock:
            self._state = "CLOSED"
            self._consecutive_failures = 0
            self._opened_at = None

    async def record_failure(self) -> None:
        async with self._lock:
            self._consecutive_failures += 1
            if self._state == "HALF_OPEN" or self._consecutive_failures >= self._failure_threshold:
                self._state = "OPEN"
                self._opened_at = self._clock()
                logger.warning(
                    "circuit_breaker.open consecutive_failures=%d",
                    self._consecutive_failures,
                )

    @property
    def consecutive_failures(self) -> int:
        return self._consecutive_failures


class BreakerRegistry:
    """Per-key circuit breakers, lazily constructed.

    Keys are typically API-key hashes or provider names — anything stable
    that identifies an upstream rate-limit bucket. The registry never
    evicts; in M0 the cardinality (≤ a few dozen keys) is bounded.
    """

    def __init__(
        self,
        *,
        failure_threshold: int = 5,
        cooldown_s: float = 30.0,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._defaults = {
            "failure_threshold": failure_threshold,
            "cooldown_s": cooldown_s,
            "clock": clock,
        }
        self._breakers: dict[str, CircuitBreaker] = {}
        self._lock = asyncio.Lock()

    async def get(self, key: str) -> CircuitBreaker:
        async with self._lock:
            breaker = self._breakers.get(key)
            if breaker is None:
                breaker = CircuitBreaker(**self._defaults)  # type: ignore[arg-type]
                self._breakers[key] = breaker
            return breaker


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------


def _default_key_extractor(ctx: MiddlewareContext) -> str:
    """M0 default: ``ctx.payload["provider_key"]`` or ``"default"``.

    E.11 LLMRouter will set ``provider_key`` to the active provider's
    API-key identifier; pre-E.11 dev / tests fall back to ``"default"``.
    """
    raw = ctx.payload.get("provider_key", "default")
    return str(raw)


@dataclass
class LLMErrorHandlingMiddleware:
    """Wrap the LLM call with retry + circuit breaker.

    On every ``__call__``:

    1. Resolve the breaker key from ``ctx`` via ``key_extractor``.
    2. Check the breaker — if OPEN, raise :class:`CircuitOpenError`
       **without** invoking ``call_next`` (saves a wasted upstream hit).
    3. Otherwise enter the retry loop:

       - Invoke ``call_next(ctx)``.
       - On success: ``breaker.record_success()`` and return.
       - On :class:`LLMClientError`: ``record_success`` (don't poison
         the breaker on caller bugs), re-raise.
       - On retryable error: sleep ``base_delay_s * 2**attempt`` (capped
         at ``max_delay_s``) and try again, up to ``max_retries``.
       - On exhausted retries: ``record_failure`` and re-raise the last
         exception (which trips OPEN if this was the 5th consecutive).

    Backoff uses ``asyncio.sleep``; tests can inject a no-op via
    ``sleeper`` to keep runtime fast without timing flakes.
    """

    breaker_registry: BreakerRegistry = field(default_factory=BreakerRegistry)
    max_retries: int = 3
    base_delay_s: float = 1.0
    max_delay_s: float = 30.0
    key_extractor: Callable[[MiddlewareContext], str] = field(default=_default_key_extractor)
    sleeper: Callable[[float], Awaitable[None]] = field(default=asyncio.sleep)

    name: str = "llm_error_handling"
    anchor: str = "around_llm_call"
    after: tuple[str, ...] = field(default_factory=tuple)
    before: tuple[str, ...] = field(default_factory=tuple)

    async def __call__(self, ctx: MiddlewareContext, call_next: CallNext) -> None:
        key = self.key_extractor(ctx)
        breaker = await self.breaker_registry.get(key)

        last_exc: LLMError | None = None
        for attempt in range(self.max_retries + 1):
            state = await breaker.check_state()
            if state == "OPEN":
                raise CircuitOpenError(key)

            try:
                await call_next(ctx)
            except LLMKeyUnavailableError:
                # Y-MK — dead account/key: not retryable (backoff can't
                # un-exhaust balance/quota), but DOES count toward the breaker
                # so the key is skipped during cooldown. The router advances to
                # the next sibling key.
                await breaker.record_failure()
                raise
            except LLMClientError:
                # 4xx: caller's fault, don't poison breaker.
                await breaker.record_success()
                raise
            except _RETRYABLE_ERRORS as exc:
                last_exc = exc
                logger.warning(
                    "llm_error_handling.retryable attempt=%d/%d key=%s err=%s",
                    attempt + 1,
                    self.max_retries + 1,
                    key,
                    type(exc).__name__,
                )
                if attempt < self.max_retries:
                    delay = min(self.max_delay_s, self.base_delay_s * (2**attempt))
                    await self.sleeper(delay)
                    continue
                # Exhausted retries.
                await breaker.record_failure()
                raise
            else:
                await breaker.record_success()
                return

        # Unreachable: loop body either returns or raises.
        if last_exc is not None:
            raise last_exc
