"""``/v1/quota/*`` internal endpoints — Stream C.5.

Service-to-service runtime ops on the quota engine (check / reserve
/ commit / release). All four endpoints require the ``quota:check``
RBAC action so mTLS service principals (subsystems/15 § 3.3) can
consume them; ``quota:check`` is *not* granted to viewer / unknown
principals.

The endpoints accept ``tenant_id`` in the request body rather than
inferring from ``principal.tenant_id``: service principals operate
*on behalf of* tenants, so the request body is the source of truth.
The auditing path still records the calling service via the
principal so downstream traces show who triggered the spend.
"""

from __future__ import annotations

import logging
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request

from control_plane.api._authz import require
from control_plane.api._quota_admission import quota_engine_unavailable_as_503
from control_plane.audit import emit
from control_plane.quota.base import QuotaService
from expert_work.common.observability import current_trace_id_hex
from expert_work.persistence.quota import ReservationNotFoundError
from expert_work.protocol import (
    AuditAction,
    AuditResult,
    CheckRequest,
    CheckResult,
    CommitRequest,
    Principal,
    ReserveRequest,
    ReserveResult,
)
from expert_work.runtime.audit.logger import AuditLogger

logger = logging.getLogger("expert_work.control_plane.api.quota")


def _get_quota(request: Request) -> QuotaService:
    return request.app.state.quota_service  # type: ignore[no-any-return]


def _get_audit(request: Request) -> AuditLogger:
    return request.app.state.audit_logger  # type: ignore[no-any-return]


def build_quota_router() -> APIRouter:
    router = APIRouter(prefix="/v1/quota", tags=["quota"])

    @router.post("/check")
    async def check_quota(
        payload: CheckRequest,
        principal: Annotated[Principal, Depends(require("quota", "check"))],
        quota: Annotated[QuotaService, Depends(_get_quota)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
    ) -> CheckResult:
        async with quota_engine_unavailable_as_503():
            result = await quota.check(payload)
        if not result.allowed:
            # Sample rate-limit denials (audit can blow up under
            # sustained 429 storms). Subsystems/16 § 8 specifies 1%
            # sampling + per-minute aggregate; M0 emits every 100th
            # denial inline + a Prometheus counter (Stream I covers
            # the aggregation pipeline).
            await _maybe_emit_denial(
                audit,
                principal=principal,
                target_tenant=payload.tenant_id,
                dimension=str(
                    result.blocked_dimension.value if result.blocked_dimension else "unknown"
                ),
            )
        return result

    @router.post("/reserve")
    async def reserve_quota(
        payload: ReserveRequest,
        principal: Annotated[Principal, Depends(require("quota", "check"))],
        quota: Annotated[QuotaService, Depends(_get_quota)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
    ) -> ReserveResult:
        async with quota_engine_unavailable_as_503():
            result = await quota.reserve_tokens(payload)
        if not result.granted:
            await emit(
                audit,
                tenant_id=payload.tenant_id,
                actor_id=principal.subject_id,
                action=AuditAction.QUOTA_BUDGET_EXCEEDED,
                resource_type="quota",
                resource_id="reserve",
                result=AuditResult.DENIED,
                reason=result.reason,
                trace_id=current_trace_id_hex(),
                details={
                    "agent": payload.agent,
                    "thread_id": str(payload.thread_id),
                    "estimated_tokens": payload.estimated_tokens,
                },
            )
        return result

    @router.post("/commit", status_code=204)
    async def commit_quota(
        payload: CommitRequest,
        _principal: Annotated[Principal, Depends(require("quota", "check"))],
        quota: Annotated[QuotaService, Depends(_get_quota)],
    ) -> None:
        try:
            async with quota_engine_unavailable_as_503():
                await quota.commit_tokens(payload)
        except ReservationNotFoundError as exc:
            raise HTTPException(
                status_code=404,
                detail={
                    "code": "RESERVATION_NOT_FOUND",
                    "message": "reservation does not exist for this tenant",
                },
            ) from exc

    @router.post("/release/{reservation_id}", status_code=204)
    async def release_quota(
        reservation_id: UUID,
        tenant_id: Annotated[UUID, Depends(_tenant_id_from_query_or_principal)],
        _principal: Annotated[Principal, Depends(require("quota", "check"))],
        quota: Annotated[QuotaService, Depends(_get_quota)],
    ) -> None:
        try:
            async with quota_engine_unavailable_as_503():
                await quota.release_tokens(reservation_id, tenant_id=tenant_id)
        except ReservationNotFoundError as exc:
            raise HTTPException(
                status_code=404,
                detail={
                    "code": "RESERVATION_NOT_FOUND",
                    "message": "reservation does not exist for this tenant",
                },
            ) from exc

    return router


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


# Counter for the simple "every Nth denial" sampler. Per-process is
# fine for M0; Stream I lifts this into a proper sampler.
_DENIAL_SAMPLE_EVERY = 100
_denial_counter: dict[str, int] = {}


async def _maybe_emit_denial(
    audit: AuditLogger,
    *,
    principal: Principal,
    target_tenant: UUID,
    dimension: str,
) -> None:
    key = f"{target_tenant}:{dimension}"
    n = _denial_counter.get(key, 0) + 1
    _denial_counter[key] = n
    if n % _DENIAL_SAMPLE_EVERY != 1:
        return
    await emit(
        audit,
        tenant_id=target_tenant,
        actor_id=principal.subject_id,
        action=AuditAction.QUOTA_RATE_LIMIT_DENIED,
        resource_type="quota",
        resource_id="check",
        result=AuditResult.DENIED,
        reason="rate_limit_exceeded",
        trace_id=current_trace_id_hex(),
        details={"dimension": dimension, "sampled_n": n},
    )


def _tenant_id_from_query_or_principal(
    request: Request,
    principal: Annotated[Principal, Depends(require("quota", "check"))],
) -> UUID:
    """Extract ``tenant_id`` for release calls.

    Release URLs use a path parameter for the reservation id; the tenant
    has to come from either an explicit ``?tenant_id=`` query param or
    the calling principal's home tenant. mTLS service principals carry
    the system tenant — query param is then required.
    """
    raw = request.query_params.get("tenant_id")
    if not raw:
        return principal.tenant_id
    try:
        target = UUID(raw)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail={"code": "INVALID_TENANT_ID", "message": "tenant_id is not a UUID"},
        ) from exc
    # mTLS service principals (orchestrator-tier infra) keep the documented
    # semantics: they sit on the system tenant and releasing any tenant's
    # reservation is their job.
    if principal.auth_method == "mtls":
        return target
    # User / service-account principals: the target must be within their
    # authorized tenants (system_admin's allowed_tenants == "*" always passes).
    if principal.allowed_tenants != "*" and target not in principal.allowed_tenants:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "TENANT_NOT_ALLOWED",
                "message": "the caller is not authorized for this tenant",
            },
        )
    return target
