"""``/v1/quality`` — Stream RT-5 (RT-ADR-26) production-quality dashboard reads.

Home-tenant, read-only surface over the ``quality_score`` time-series and the
``quality_drift_alert`` history the RT-5 workers fill:

- ``GET /v1/quality/scores`` — the per-agent score series (trend chart) and the
  low-score rows a caller drills into (each carries ``run_id`` / ``thread_id``
  so the UI can link to ``run_detail``).
- ``GET /v1/quality/drift-alerts`` — the raised drift alerts.

Reads never cross tenants: the caller's tenant (RLS GUC set by the request
middleware) plus the store's explicit ``tenant_id`` filter both enforce it.
Returns the **raw** payload (no ``{success, data, error}`` envelope), matching
``api/eval_runs.py`` and the other operator read surfaces.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse

from control_plane.tenant_scope import (
    CrossTenant,
    SingleTenant,
    applied_scope,
    cross_tenant_query_enabled,
    ensure_tenant_scope,
)
from expert_work.common.observability import current_trace_id_hex
from expert_work.persistence import QualityDriftAlertStore, QualityScoreStore
from expert_work.protocol import QualityDriftAlertRecord, QualityScoreRecord
from expert_work.runtime.audit.logger import AuditLogger


def _score_dict(record: QualityScoreRecord) -> dict[str, Any]:
    return {
        "id": record.id,
        "agent_name": record.agent_name,
        "agent_version": record.agent_version,
        "run_id": str(record.run_id),
        "thread_id": str(record.thread_id),
        "overall": record.overall,
        "dimensions": record.dimensions,
        "rationale": record.rationale,
        "judge_model": record.judge_model,
        "observed_at": record.observed_at.isoformat() if record.observed_at else None,
    }


def _alert_dict(record: QualityDriftAlertRecord) -> dict[str, Any]:
    return {
        "id": record.id,
        "agent_name": record.agent_name,
        "recent_mean": record.recent_mean,
        "baseline_mean": record.baseline_mean,
        "drift_pct": record.drift_pct,
        "recent_count": record.recent_count,
        "baseline_count": record.baseline_count,
        "detected_at": record.detected_at.isoformat() if record.detected_at else None,
    }


def _get_score_store(request: Request) -> QualityScoreStore:
    return request.app.state.quality_score_store  # type: ignore[no-any-return]


def _get_alert_store(request: Request) -> QualityDriftAlertStore:
    return request.app.state.quality_drift_alert_store  # type: ignore[no-any-return]


def _get_audit(request: Request) -> AuditLogger:
    return request.app.state.audit_logger  # type: ignore[no-any-return]


async def _resolve_list_scope(
    request: Request,
    tenant_id: UUID | Literal["*"] | None,
    audit: AuditLogger,
    *,
    endpoint: str,
) -> SingleTenant:
    """W3 read scope for the two quality lists.

    Neither store has a cross-tenant aggregate reader and the "*" aggregate is
    a spec non-goal here — a resolved :class:`CrossTenant` falls back to the
    caller's home tenant (the pre-scope-threading behavior, mirroring the
    front-end ``concreteTenantScope`` collapse).
    """
    scope = await ensure_tenant_scope(
        request.state.principal,
        tenant_id,
        audit,
        trace_id=current_trace_id_hex(),
        endpoint=endpoint,
        cross_tenant_enabled=cross_tenant_query_enabled(request),
    )
    if isinstance(scope, CrossTenant):
        return SingleTenant(tenant_id=request.state.principal.tenant_id)
    return scope


def build_quality_router() -> APIRouter:
    """Read the per-agent quality series + drift alerts (home-tenant)."""
    router = APIRouter(prefix="/v1/quality", tags=["quality"])

    @router.get("/scores", response_model=None)
    async def list_scores(
        request: Request,
        store: Annotated[QualityScoreStore, Depends(_get_score_store)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
        agent_name: Annotated[str | None, Query(max_length=200)] = None,
        window_h: Annotated[int, Query(ge=1, le=8760)] = 168,
        limit: Annotated[int, Query(ge=1, le=1000)] = 500,
        # W3 read scope — a concrete id lets a system_admin read a foreign
        # tenant's quality series from the tenant switcher.
        tenant_id: Annotated[UUID | Literal["*"] | None, Query()] = None,
    ) -> JSONResponse:
        scope = await _resolve_list_scope(
            request, tenant_id, audit, endpoint="GET /v1/quality/scores"
        )
        since = datetime.now(tz=UTC) - timedelta(hours=window_h)
        async with applied_scope(scope):
            rows = await store.list_scores(
                tenant_id=scope.tenant_id, agent_name=agent_name, since=since, limit=limit
            )
        return JSONResponse(content={"items": [_score_dict(r) for r in rows]})

    @router.get("/drift-alerts", response_model=None)
    async def list_drift_alerts(
        request: Request,
        store: Annotated[QualityDriftAlertStore, Depends(_get_alert_store)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
        agent_name: Annotated[str | None, Query(max_length=200)] = None,
        window_h: Annotated[int, Query(ge=1, le=8760)] = 720,
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
        # W3 read scope — same treatment as ``/scores``.
        tenant_id: Annotated[UUID | Literal["*"] | None, Query()] = None,
    ) -> JSONResponse:
        scope = await _resolve_list_scope(
            request, tenant_id, audit, endpoint="GET /v1/quality/drift-alerts"
        )
        since = datetime.now(tz=UTC) - timedelta(hours=window_h)
        async with applied_scope(scope):
            rows = await store.list_alerts(
                tenant_id=scope.tenant_id, agent_name=agent_name, since=since, limit=limit
            )
        return JSONResponse(content={"items": [_alert_dict(r) for r in rows]})

    return router


__all__ = ["build_quality_router"]
