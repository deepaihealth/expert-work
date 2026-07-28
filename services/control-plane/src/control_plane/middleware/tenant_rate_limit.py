"""Tenant-tier rate-limit middleware — Stream C.6.

Sits between :class:`AuthMiddleware` and the route handlers. For every
authenticated request, charges one token against the
``("tenant", <tenant_id>)`` bucket via the same :class:`RateLimiter`
Protocol used by the gateway tier (B.2). Distinct buckets keep the
two layers independent — a chatty IP doesn't drain the tenant bucket
and vice versa.

Behaviour:

* Exempt paths (``/healthz`` / ``/metrics``) bypass entirely — same
  prefix set as :class:`AuthMiddleware`.
* Unauthenticated requests (no ``principal`` on ``request.state``)
  also bypass; they'll be 401'd downstream.
* Denied requests get a 429 with the same envelope shape as
  :func:`api._quota_admission.check_admission`:
  ``{code, message, dimension, retry_after_s}`` plus a
  ``Retry-After`` header. A ``quota:rate_limit_denied`` audit row
  lands at the **sampled** rate (every Nth denial, configured via
  ``settings.tenant_rate_limit_audit_sample_every``) to keep the
  log volume bounded under sustained throttle storms.

Toggled off via ``settings.tenant_rate_limit_enabled = False`` to
unblock single-tenant dev / load-test scenarios.
"""

from __future__ import annotations

import logging
import math
import time
from collections.abc import Awaitable, Callable, Iterable
from uuid import UUID

from redis.exceptions import RedisError
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp

from control_plane.audit import emit
from control_plane.middleware.rate_limit import _rate_limit_backend_errors
from control_plane.ratelimit import RateLimiter, RateLimitOverride, parse_rate_limit_override
from expert_work.common.observability import current_trace_id_hex, expert_work_counter
from expert_work.persistence.rls import current_tenant_id_var
from expert_work.persistence.tenant_config import TenantConfigStore
from expert_work.protocol import AuditAction, AuditResult, Principal
from expert_work.runtime.audit.logger import AuditLogger

logger = logging.getLogger("expert_work.control_plane.tenant_rate_limit")

_decisions = expert_work_counter(
    "expert_work_control_plane_tenant_rate_limit_decisions_total",
    "Tenant-tier rate-limit decisions by outcome.",
    ("decision",),
)


def _retry_after_seconds(retry_after_s: float) -> int:
    """Round up to the next whole second (Retry-After is int per RFC 7231)."""
    if retry_after_s <= 0:
        return 1
    if math.isinf(retry_after_s):
        return 60
    return max(1, math.ceil(retry_after_s))


class TenantRateLimitMiddleware(BaseHTTPMiddleware):
    """Charge one token per request to ``("tenant", tenant_id)``."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        limiter: RateLimiter,
        audit_logger: AuditLogger,
        enabled: bool = True,
        exempt_path_prefixes: Iterable[str] = ("/healthz", "/metrics"),
        audit_sample_every: int = 100,
        tenant_config_store: TenantConfigStore | None = None,
        override_ttl_s: float = 30.0,
    ) -> None:
        super().__init__(app)
        self._limiter = limiter
        self._audit_logger = audit_logger
        self._enabled = enabled
        self._exempt = tuple(p.rstrip("/") for p in exempt_path_prefixes)
        # Per subsystems/16 § 8: sample 429 audits to bound log volume.
        self._audit_sample_every = max(1, audit_sample_every)
        self._denial_counter: dict[str, int] = {}
        # Stream C.6 per-tenant rate_limit_override. Resolving it per request
        # would hit the DB on every call, so cache (tenant_id → override) with a
        # short TTL; a config change takes effect within ``override_ttl_s``.
        # ``None`` store (dev / tests without config) → no overrides, defaults.
        self._tenant_config_store = tenant_config_store
        self._override_ttl_s = override_ttl_s
        self._override_cache: dict[str, tuple[float, RateLimitOverride | None]] = {}

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        if not self._enabled or self._is_exempt(request.url.path):
            return await call_next(request)

        principal: Principal | None = getattr(request.state, "principal", None)
        if principal is None:
            # AuthMiddleware will reject or the path is configured
            # exempt at the auth layer (which shouldn't happen here
            # since we already bypass the same prefixes) — let it fall
            # through.
            return await call_next(request)

        tenant_key = str(principal.tenant_id)
        override = await self._resolve_override(principal.tenant_id)
        try:
            decision = await self._limiter.acquire(
                dimension="tenant",
                key=tenant_key,
                capacity=override.capacity if override else None,
                refill_per_sec=override.refill_per_sec if override else None,
            )
        except RedisError:
            # Fail OPEN — see control_plane.middleware.rate_limit for the
            # rationale (shared counter, ``backend`` label tells the tiers
            # apart). Business-level quota admission fails CLOSED instead;
            # that asymmetry is deliberate (subsystems/16 § 5.2).
            _rate_limit_backend_errors.labels(backend="tenant").inc()
            logger.warning(
                "tenant_rate_limit.backend_unavailable",
                extra={"tenant_id": tenant_key},
            )
            return await call_next(request)
        outcome = "allowed" if decision.allowed else "denied"
        _decisions.labels(decision=outcome).inc()

        if decision.allowed:
            return await call_next(request)

        retry_after = _retry_after_seconds(decision.retry_after_s)
        await self._maybe_emit_denial(
            principal=principal,
            retry_after=retry_after,
        )
        logger.info(
            "tenant_rate_limit.denied",
            extra={"tenant_id": tenant_key, "retry_after_s": retry_after},
        )
        return JSONResponse(
            status_code=429,
            headers={"Retry-After": str(retry_after)},
            content={
                "success": False,
                "data": None,
                "error": {
                    "code": "RATE_LIMIT_EXCEEDED",
                    "message": "tenant exceeded its per-tenant rate limit",
                    "dimension": "tenant",
                    "retry_after_s": retry_after,
                },
            },
        )

    async def _resolve_override(self, tenant_id: UUID) -> RateLimitOverride | None:
        """The tenant's ``rate_limit_override`` (cached, TTL). ``None`` → defaults.

        Reads under the tenant's own RLS context (this middleware runs before
        ``RLSContextMiddleware``, so set the ContextVar for the lookup — a tenant
        reading its OWN config is permitted by the RLS self-policy). Best-effort:
        any read/parse error falls back to ``None`` (the configured defaults)."""
        if self._tenant_config_store is None:
            return None
        key = str(tenant_id)
        now = time.monotonic()
        cached = self._override_cache.get(key)
        if cached is not None and cached[0] > now:
            return cached[1]

        override: RateLimitOverride | None = None
        ctx_token = current_tenant_id_var.set(tenant_id)
        try:
            record = await self._tenant_config_store.get(tenant_id=tenant_id)
            if record is not None:
                override = parse_rate_limit_override(record.rate_limit_override)
        except Exception:
            # Never let a config read break rate limiting — fall back to defaults.
            logger.warning("tenant_rate_limit.override_lookup_failed", exc_info=True)
            override = None
        finally:
            current_tenant_id_var.reset(ctx_token)

        self._override_cache[key] = (now + self._override_ttl_s, override)
        return override

    def _is_exempt(self, path: str) -> bool:
        path_clean = path.rstrip("/")
        return any(
            path_clean == prefix or path_clean.startswith(prefix + "/") for prefix in self._exempt
        )

    async def _maybe_emit_denial(self, *, principal: Principal, retry_after: int) -> None:
        """Sampled audit — every Nth denial per tenant lands a row.

        With ``sample_every=N``, emit on denials numbered
        ``1, N+1, 2N+1, ...``. Computed as ``(n - 1) % N == 0`` so
        ``N=1`` correctly emits every denial.
        """
        tenant_key = str(principal.tenant_id)
        n = self._denial_counter.get(tenant_key, 0) + 1
        self._denial_counter[tenant_key] = n
        if (n - 1) % self._audit_sample_every != 0:
            return
        try:
            await emit(
                self._audit_logger,
                tenant_id=principal.tenant_id,
                actor_id=principal.subject_id,
                action=AuditAction.QUOTA_RATE_LIMIT_DENIED,
                resource_type="quota",
                resource_id="tenant",
                result=AuditResult.DENIED,
                reason="rate_limit_exceeded",
                trace_id=current_trace_id_hex(),
                details={
                    "dimension": "tenant",
                    "retry_after_s": retry_after,
                    "sampled_n": n,
                },
            )
        except Exception:
            logger.exception("tenant_rate_limit.audit_emit_failed")
