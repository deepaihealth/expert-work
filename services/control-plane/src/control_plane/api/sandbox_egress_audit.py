"""Read-side endpoint for sandbox egress audit (sandbox-egress §3.1 Phase 3).

``GET /v1/sandbox-egress-audit`` — the admin view over ``sandbox_egress_audit``
(one row per sandbox→internet connection: host/port/byte volumes/verdict, never
payload). Mirrors ``/v1/audit``: keyset pagination, ``?tenant_id=*`` cross-tenant
for system_admin, raw (non-envelope) JSON to match the other audit reads.

Gated by ``require("audit", "read")`` — stricter than the legacy ``/v1/audit``
(which relies on the middleware principal alone).
"""

from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse

from control_plane.api._authz import console_only, require
from control_plane.tenant_scope import (
    CrossTenant,
    cross_tenant_query_enabled,
    ensure_tenant_scope,
)
from expert_work.common.observability import current_trace_id_hex
from expert_work.persistence.sandbox_egress_audit import (
    EgressAuditQuery,
    EgressAuditRecord,
    SandboxEgressAuditStore,
)
from expert_work.protocol import Principal
from expert_work.runtime.audit.logger import AuditLogger

_MAX_LIMIT = 500
_DEFAULT_LIMIT = 100

EgressVerdict = Literal[
    "allowed",
    "blocked_ssrf",
    "blocked_allowlist",
    "blocked_denylist",
    "blocked_auth",
    "upstream_error",
]


def _get_store(request: Request) -> SandboxEgressAuditStore:
    return request.app.state.sandbox_egress_audit_store  # type: ignore[no-any-return]


def _get_audit(request: Request) -> AuditLogger:
    return request.app.state.audit_logger  # type: ignore[no-any-return]


def _record_dict(r: EgressAuditRecord) -> dict[str, object]:
    return {
        "id": r.id,
        # None for a pre-identity blocked_auth row (audit-eval Phase 4).
        "tenant_id": str(r.tenant_id) if r.tenant_id is not None else None,
        "agent_name": r.agent_name,
        "agent_version": r.agent_version,
        "sandbox_id": r.sandbox_id,
        "target_host": r.target_host,
        "target_port": r.target_port,
        "verdict": r.verdict,
        "bytes_up": r.bytes_up,
        "bytes_down": r.bytes_down,
        "duration_ms": r.duration_ms,
        "error_msg": r.error_msg,
        "occurred_at": r.occurred_at.isoformat(),
    }


def build_sandbox_egress_audit_router() -> APIRouter:
    router = APIRouter(
        prefix="/v1/sandbox-egress-audit", tags=["audit"], dependencies=[Depends(console_only())]
    )

    @router.get("", response_model=None)
    async def list_egress_audit(
        request: Request,
        _principal: Annotated[Principal, Depends(require("audit", "read"))],
        store: Annotated[SandboxEgressAuditStore, Depends(_get_store)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
        agent_name: Annotated[str | None, Query(min_length=1)] = None,
        verdict: Annotated[EgressVerdict | None, Query()] = None,
        target_host: Annotated[str | None, Query(min_length=1)] = None,
        cursor: Annotated[str | None, Query(min_length=1)] = None,
        limit: Annotated[int, Query(ge=1, le=_MAX_LIMIT)] = _DEFAULT_LIMIT,
        tenant_id: Annotated[UUID | Literal["*"] | None, Query()] = None,
    ) -> JSONResponse:
        scope = await ensure_tenant_scope(
            request.state.principal,
            tenant_id,
            audit,
            trace_id=current_trace_id_hex(),
            endpoint="GET /v1/sandbox-egress-audit",
            cross_tenant_enabled=cross_tenant_query_enabled(request),
        )
        query_tenant_id: UUID | Literal["*"] = (
            "*" if isinstance(scope, CrossTenant) else scope.tenant_id
        )
        # ``sandbox_egress_audit`` carries no RLS (migration 0087), so the
        # tenant_id filter on the query IS the isolation; ``"*"`` is gated to
        # system_admin by ``ensure_tenant_scope`` above. No ``applied_scope``
        # needed (nothing to bypass).
        page = await store.query(
            EgressAuditQuery(
                tenant_id=query_tenant_id,
                agent_name=agent_name,
                verdict=verdict,
                target_host=target_host,
                limit=limit,
                cursor=cursor,
            )
        )
        return JSONResponse(
            content={
                "items": [_record_dict(e) for e in page.entries],
                "next_cursor": page.next_cursor,
                "has_more": page.next_cursor is not None,
                "applied_scope": (
                    "cross_tenant" if isinstance(scope, CrossTenant) else str(scope.tenant_id)
                ),
            }
        )

    return router


__all__ = ["build_sandbox_egress_audit_router"]
