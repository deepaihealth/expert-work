"""Quota admission helper — Stream C.5b.

Business routes (``sessions:create``, ``runs:create``) call
:func:`check_admission` before doing real work. On denial the helper
emits the audit row + returns a fully-formed 429 ``JSONResponse``
with ``Retry-After`` header that the route just hands back to the
client.

Why a separate module and not inline:

* Two handlers share the exact same shape (envelope, header,
  audit). Inline would diverge first time someone touches it.
* The 429 envelope is part of the subsystems/16 § 4.3 contract;
  centralising it keeps the schema in one place for future changes
  (rate-limit dashboard wire format depends on this).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import UUID

from fastapi import HTTPException
from fastapi.responses import JSONResponse
from redis.exceptions import RedisError

from control_plane.audit import emit
from control_plane.quota.base import QuotaService
from expert_work.common.observability import current_trace_id_hex
from expert_work.protocol import (
    AuditAction,
    AuditResult,
    CheckRequest,
    CheckResult,
    QuotaDimension,
)
from expert_work.runtime.audit.logger import AuditLogger

__all__ = ["check_admission", "quota_engine_unavailable_as_503"]


@asynccontextmanager
async def quota_engine_unavailable_as_503() -> AsyncIterator[None]:
    """Translate a ``RedisError`` raised inside the block into the
    subsystems/16 § 6 promise: the quota engine being unreachable maps to
    HTTP 503 ``quota_engine_unavailable`` (fail CLOSED — the opposite of the
    rate-limit tiers, which fail open; see ``redis_quota.py``'s module
    docstring for the wording this ``detail`` must stay byte-identical to).

    Shared by :func:`check_admission` (the in-process admission wire used by
    ``sessions:create`` / ``runs:create`` / uploads) and the ``/v1/quota/*``
    internal HTTP endpoints (``api/quota.py``) so both surfaces honour the
    same contract from one place.
    """
    try:
        yield
    except RedisError as exc:
        raise HTTPException(status_code=503, detail="quota_engine_unavailable") from exc


async def check_admission(
    *,
    quota: QuotaService,
    audit: AuditLogger,
    tenant_id: UUID,
    actor_id: str,
    agent: str | None,
    resource_kind: str,
    cost: int = 1,
    cost_overrides: dict[QuotaDimension, int] | None = None,
) -> JSONResponse | None:
    """Run a quota ``check`` for the call and, on denial, return the 429.

    Returns ``None`` when the call is allowed — the caller proceeds.
    Returns a fully-formed :class:`JSONResponse` when denied — the
    caller just ``return``s it back to the client.

    ``resource_kind`` lands on the audit row as ``resource_type`` so
    SOC / dashboard pipelines can split rate-limit denials by
    consuming surface (``session`` vs ``run`` vs ``image_upload``).

    ``cost_overrides`` (Mini-ADR J-30, J.6.补强-1) lets the caller pass
    a per-dimension cost — the upload endpoint uses it to subtract
    ``file_size`` bytes from ``IMAGE_STORAGE_BYTES`` while keeping the
    default ``cost=1`` for QPS / count dimensions.
    """
    request = CheckRequest(
        tenant_id=tenant_id,
        agent=agent,
        cost=cost,
        cost_overrides=dict(cost_overrides) if cost_overrides else {},
    )
    result: CheckResult
    async with quota_engine_unavailable_as_503():
        result = await quota.check(request)
    if result.allowed:
        return None

    dimension = (
        result.blocked_dimension.value if result.blocked_dimension is not None else "unknown"
    )
    retry_after = result.retry_after_s if result.retry_after_s is not None else 1

    await emit(
        audit,
        tenant_id=tenant_id,
        actor_id=actor_id,
        action=AuditAction.QUOTA_RATE_LIMIT_DENIED,
        resource_type="quota",
        resource_id=resource_kind,
        result=AuditResult.DENIED,
        reason="rate_limit_exceeded",
        trace_id=current_trace_id_hex(),
        details={
            "dimension": dimension,
            "agent": agent,
            "retry_after_s": retry_after,
        },
    )

    return JSONResponse(
        status_code=429,
        content={
            "success": False,
            "data": None,
            "error": {
                "code": "RATE_LIMIT_EXCEEDED",
                "message": "tenant exceeded its rate limit for this dimension",
                "dimension": dimension,
                "retry_after_s": retry_after,
            },
        },
        headers={"Retry-After": str(retry_after)},
    )
