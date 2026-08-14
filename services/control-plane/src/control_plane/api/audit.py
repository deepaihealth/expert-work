"""``/v1/audit`` — Stream H.4 PR 3 (subsystems/17-audit-log § 3.2).

Read-side query endpoint that wraps :class:`AuditLogger` so reviewers
in the Admin UI (H.4 PR 4) can filter, paginate, and inspect audit
rows. Two endpoints:

* ``GET /v1/audit`` — paginated list filtered by actor / action /
  resource / result / time range, with cross-tenant support via
  ``tenant_id=*`` (system_admin only).
* ``GET /v1/audit/{audit_id}`` — single entry detail with full
  ``details`` payload (already redacted at write time).

Every read emits a self-audit row (``action='audit:read'``) through the
:class:`AuditLogger` so the audit trail stays self-describing — that
mechanism already exists in :meth:`AuditLogger.query`; this PR adds
:meth:`AuditLogger.get_by_id` for the detail endpoint to use the same
invariant.

Cursor pagination is **opaque base64** — clients must transparently
pass back ``next_cursor`` without parsing it. Matches the
:meth:`AuditLogStore.query` contract.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from control_plane.api._authz import console_only
from control_plane.tenant_scope import (
    CrossTenant,
    applied_scope,
    cross_tenant_query_enabled,
    ensure_single_tenant_scope,
    ensure_tenant_scope,
)
from expert_work.common.observability import (
    current_trace_id_hex,
    expert_work_counter,
    expert_work_histogram,
)
from expert_work.protocol import (
    AuditAction,
    AuditEntry,
    AuditQuery,
    AuditResult,
)
from expert_work.runtime.audit.logger import AuditLogger

logger = logging.getLogger("expert_work.control_plane.audit")

# Hard cap on ``limit`` — same shape as Mini-ADR H-7 (D). The AuditQuery
# model itself enforces ``limit <= 1000`` so the cap is informational
# unless we further reduce; H.4 sets it at 500 to match runs list.
_MAX_AUDIT_LIMIT = 500
_DEFAULT_AUDIT_LIMIT = 100

_audit_query_total = expert_work_counter(
    "expert_work_control_plane_audit_query_total",
    "GET /v1/audit invocations partitioned by tenant scope + result.",
    label_names=("tenant_scope", "result"),
)
_audit_query_seconds = expert_work_histogram(
    "expert_work_control_plane_audit_query_seconds",
    "GET /v1/audit wall time, including AuditLogger self-audit emit.",
    label_names=("tenant_scope",),
)


def _audit_entry_dict(entry: AuditEntry) -> dict[str, Any]:
    """Serialise an ``AuditEntry`` to a JSON-safe dict.

    Mirrors the protocol field order so the UI Drawer can render
    deterministically. Note: ``details`` is already redactor-cleaned
    at write time, so the response carries it verbatim (no further
    masking).
    """
    return {
        "id": entry.id,
        "tenant_id": str(entry.tenant_id),
        "actor_type": entry.actor_type,
        "actor_id": entry.actor_id,
        "on_behalf_of": entry.on_behalf_of,
        "action": entry.action.value,
        "resource_type": entry.resource_type,
        "resource_id": entry.resource_id,
        "result": entry.result.value,
        "reason": entry.reason,
        "ip": str(entry.ip) if entry.ip is not None else None,
        "user_agent": entry.user_agent,
        "request_id": str(entry.request_id) if entry.request_id is not None else None,
        "trace_id": entry.trace_id,
        "details": entry.details,
        "occurred_at": entry.occurred_at.isoformat() if entry.occurred_at is not None else None,
    }


def _get_audit(request: Request) -> AuditLogger:
    return request.app.state.audit_logger  # type: ignore[no-any-return]


def build_audit_router() -> APIRouter:
    """Read-side audit endpoints. Stream H.4 PR 3."""
    router = APIRouter(prefix="/v1/audit", tags=["audit"], dependencies=[Depends(console_only())])

    @router.get("", response_model=None)
    async def list_audit(
        request: Request,
        audit: Annotated[AuditLogger, Depends(_get_audit)],
        actor_id: Annotated[str | None, Query(min_length=1)] = None,
        action: Annotated[AuditAction | None, Query()] = None,
        resource_type: Annotated[str | None, Query(min_length=1)] = None,
        resource_id: Annotated[str | None, Query(min_length=1)] = None,
        result: Annotated[AuditResult | None, Query()] = None,
        from_ts: Annotated[datetime | None, Query()] = None,
        to_ts: Annotated[datetime | None, Query()] = None,
        cursor: Annotated[str | None, Query(min_length=1)] = None,
        limit: Annotated[int, Query(ge=1, le=_MAX_AUDIT_LIMIT)] = _DEFAULT_AUDIT_LIMIT,
        tenant_id: Annotated[UUID | Literal["*"] | None, Query()] = None,  # Stream N
    ) -> JSONResponse:
        trace_id = current_trace_id_hex()
        start = time.monotonic()

        if from_ts is not None and to_ts is not None and from_ts > to_ts:
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "INVALID_TIME_RANGE",
                    "message": "from_ts must be <= to_ts",
                },
            )

        scope = await ensure_tenant_scope(
            request.state.principal,
            tenant_id,
            audit,
            trace_id=trace_id,
            endpoint="GET /v1/audit",
            cross_tenant_enabled=cross_tenant_query_enabled(request),
        )
        tenant_scope_label = (
            "cross"
            if isinstance(scope, CrossTenant)
            else ("home" if scope.tenant_id == request.state.principal.tenant_id else "target")
        )

        query_tenant_id: UUID | Literal["*"] = (
            "*" if isinstance(scope, CrossTenant) else scope.tenant_id
        )
        # ``AuditQuery.tenant_id`` is required; wildcard is gated on
        # ``ensure_tenant_scope`` above (system_admin only).
        q = AuditQuery(
            tenant_id=query_tenant_id,
            actor_id=actor_id,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            result=result,
            from_ts=from_ts,
            to_ts=to_ts,
            limit=limit,
            cursor=cursor,
        )
        try:
            async with applied_scope(scope):
                page = await audit.query(
                    q,
                    actor_id=request.state.actor_id,
                    actor_tenant_id=str(request.state.principal.tenant_id),
                )
        except Exception:
            _audit_query_total.labels(tenant_scope=tenant_scope_label, result="error").inc()
            _audit_query_seconds.labels(tenant_scope=tenant_scope_label).observe(
                time.monotonic() - start
            )
            raise

        _audit_query_total.labels(tenant_scope=tenant_scope_label, result="success").inc()
        _audit_query_seconds.labels(tenant_scope=tenant_scope_label).observe(
            time.monotonic() - start
        )

        return JSONResponse(
            content={
                "items": [_audit_entry_dict(e) for e in page.entries],
                "next_cursor": page.next_cursor,
                "has_more": page.next_cursor is not None,
                "applied_scope": (
                    "cross_tenant" if isinstance(scope, CrossTenant) else str(scope.tenant_id)
                ),
            }
        )

    @router.get("/{audit_id}", response_model=None)
    async def get_audit_entry(
        audit_id: int,
        request: Request,
        audit: Annotated[AuditLogger, Depends(_get_audit)],
        # W2 read scope — a concrete id lets a system_admin drill into a
        # foreign tenant's audit row from the tenant switcher; "*" is
        # meaningless (a row belongs to one tenant — the list endpoint
        # owns the aggregate).
        tenant_id: Annotated[UUID | Literal["*"] | None, Query()] = None,
    ) -> JSONResponse:
        scope = await ensure_single_tenant_scope(
            request.state.principal,
            tenant_id,
            audit,
            trace_id=current_trace_id_hex(),
            endpoint="GET /v1/audit/{audit_id}",
            cross_tenant_enabled=cross_tenant_query_enabled(request),
        )
        async with applied_scope(scope):
            entry = await audit.get_by_id(
                audit_id,
                tenant_id=scope.tenant_id,
                actor_id=request.state.actor_id,
            )
        if entry is None:
            raise HTTPException(status_code=404, detail="audit entry not found")
        return JSONResponse(content=_audit_entry_dict(entry))

    return router
