"""Gateway-tier rate-limit middleware — Stream B.2.

Implements layer 1 of the three-tier limiter stack in
subsystems/16-quota-rate-limit § 5.1 (network frontline). The other two
tiers — per-tenant business limit (C.6) and per-LLM-provider key limit
(E.6) — slot in later via the same :class:`RateLimiter` Protocol seam.

Bucket key selection:

* ``X-API-Key`` header present → ``("apikey", sha256(key)[:16])``.
  Hashing the key keeps the in-memory bucket map free of raw secrets.
* otherwise → ``("ip", request.client.host)``. We do **not** trust
  ``X-Forwarded-For`` until the nginx-fronting story lands; M0 deploys
  expose uvicorn directly.

When the bucket is empty the middleware short-circuits with HTTP 429
plus a ``Retry-After`` header and the project-wide error envelope.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import math
import secrets
from collections.abc import Awaitable, Callable

from pydantic import SecretStr
from redis.exceptions import RedisError
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp

from control_plane.ratelimit import RateLimiter
from expert_work.common.observability import expert_work_counter

API_KEY_HEADER = "X-API-Key"

logger = logging.getLogger("expert_work.control_plane.rate_limit")

_rate_limit_decisions = expert_work_counter(
    "expert_work_control_plane_rate_limit_decisions_total",
    "Gateway rate-limit decisions by dimension and outcome.",
    ("dimension", "decision"),
)

# Shared with tenant_rate_limit.py (label distinguishes the tier) — see
# subsystems/16 § 5.2 degradation table. A Redis outage must not take the
# HTTP frontline down with it: both rate-limit tiers fail OPEN (request
# proceeds, un-throttled) while recording the outage here so on-call can
# see it. Business-level admission (quota check, ``_quota_admission.py``)
# fails CLOSED instead — that's the deliberate asymmetry this counter's
# ``backend`` label lets us tell apart on a dashboard.
_rate_limit_backend_errors = expert_work_counter(
    "expert_work_rate_limit_backend_errors_total",
    "Rate-limit Redis backend errors; request fails OPEN (allowed) by tier.",
    ("backend",),
)

# HMAC key used to derive bucket identifiers from raw header values. HMAC
# (rather than a bare hash) keeps the bucket map free of any reversible
# secret material: if the process is dumped, an attacker cannot recover the
# raw header without also recovering this key.
#
# Multi-replica note: by default each ``RateLimitMiddleware`` instance mints
# its own random 32-byte key at construction (rotated implicitly on every
# restart, safe only for a single replica). Passing ``hmac_salt`` derives a
# deterministic key instead (``sha256(salt)``), so every replica configured
# with the same salt maps a given ``X-API-Key`` to the same bucket id — see
# ``Settings.apikey_rate_limit_hmac_salt``.


def _derive_bucket_id(value: str, hmac_key: bytes) -> str:
    """Return a stable, non-reversible 16-char id for a request-scoped value.

    Not credential storage — this is purely a bucket-index derivation, so
    HMAC-SHA-256 (fast + keyed) is the correct primitive over a slow KDF.
    """
    return hmac.new(hmac_key, value.encode("utf-8"), "sha256").hexdigest()[:16]


def _resolve_bucket(request: Request, hmac_key: bytes) -> tuple[str, str]:
    header_value = request.headers.get(API_KEY_HEADER)
    if header_value:
        return "apikey", _derive_bucket_id(header_value, hmac_key)
    host = request.client.host if request.client else "unknown"
    return "ip", host


def _retry_after_seconds(retry_after_s: float) -> int:
    """Round up to the next whole second (Retry-After is int per RFC 7231)."""
    if retry_after_s <= 0:
        return 1
    if math.isinf(retry_after_s):
        return 60
    return max(1, math.ceil(retry_after_s))


class RateLimitMiddleware(BaseHTTPMiddleware):
    def __init__(
        self,
        app: ASGIApp,
        *,
        limiter: RateLimiter,
        enabled: bool = True,
        hmac_salt: SecretStr | None = None,
    ) -> None:
        super().__init__(app)
        self._limiter = limiter
        self._enabled = enabled
        self._bucket_hmac_key = (
            hashlib.sha256(hmac_salt.get_secret_value().encode("utf-8")).digest()
            if hmac_salt is not None
            else secrets.token_bytes(32)
        )

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        if not self._enabled:
            return await call_next(request)

        dimension, key = _resolve_bucket(request, self._bucket_hmac_key)
        try:
            decision = await self._limiter.acquire(dimension=dimension, key=key)
        except RedisError:
            # Fail OPEN: an unavailable rate-limit backend must not become
            # an outage. Log (not throttled — no existing log-throttle
            # helper in this codebase; see task-2-report.md) + count, then
            # let the request through un-throttled.
            _rate_limit_backend_errors.labels(backend="gateway").inc()
            logger.warning(
                "rate_limit.backend_unavailable",
                extra={"dimension": dimension},
            )
            return await call_next(request)
        outcome = "allowed" if decision.allowed else "denied"
        _rate_limit_decisions.labels(dimension=dimension, decision=outcome).inc()

        if decision.allowed:
            return await call_next(request)

        retry_after = _retry_after_seconds(decision.retry_after_s)
        logger.info(
            "rate_limit.denied",
            extra={"dimension": dimension, "retry_after_s": retry_after},
        )
        return JSONResponse(
            status_code=429,
            headers={"Retry-After": str(retry_after)},
            content={
                "success": False,
                "data": None,
                "error": {
                    "code": "RATE_LIMIT_EXCEEDED",
                    "message": "Rate limit exceeded; please retry later.",
                    "retry_after_s": retry_after,
                },
            },
        )
