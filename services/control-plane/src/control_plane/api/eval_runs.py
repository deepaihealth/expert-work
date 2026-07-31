"""``/v1/eval-runs`` — P1-S2.1d (eval platform enqueue / read API).

Operator / CI surface over the ``eval_run`` + ``eval_case_result`` tables:
enqueue a suite (``POST``) for the resident :class:`EvalWorker` to drain,
then read the run's status + summary + per-case results. Authenticated +
tenant-scoped by default; a system_admin may target another tenant on the
READ endpoints via ``?tenant_id=`` (W3 read scope, see
``control_plane.tenant_scope``). Writes stay bound to the caller's tenant.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Annotated, Any, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from control_plane.tenant_scope import (
    CrossTenant,
    SingleTenant,
    applied_scope,
    cross_tenant_query_enabled,
    ensure_single_tenant_scope,
    ensure_tenant_scope,
)
from expert_work.common.observability import current_trace_id_hex
from expert_work.persistence.eval import EvalRunStore
from expert_work.protocol import (
    EvalCaseResultRecord,
    EvalRunRecord,
    EvalRunStatus,
    EvalTriggeredBy,
)
from expert_work.runtime.audit.logger import AuditLogger

logger = logging.getLogger("expert_work.control_plane.eval_runs")

#: Suites an operator may enqueue. Kept to a fixed set so a typo can't
#: queue a run the worker will only ever fail.
_ALLOWED_SUITES = frozenset({"m0_baseline", "adversarial", "trace_eval"})


def _run_dict(record: EvalRunRecord) -> dict[str, Any]:
    return {
        "id": str(record.id),
        "suite": record.suite,
        "status": record.status.value,
        "triggered_by": record.triggered_by.value,
        "summary": record.summary,
        "created_at": record.created_at.isoformat(),
        "started_at": record.started_at.isoformat() if record.started_at is not None else None,
        "finished_at": record.finished_at.isoformat() if record.finished_at is not None else None,
    }


def _case_dict(record: EvalCaseResultRecord) -> dict[str, Any]:
    return {
        "id": record.id,
        "capability": record.capability,
        "case_id": record.case_id,
        "passed": record.passed,
        "session_id": record.session_id,
        "scores": record.scores,
        "session_metrics": record.session_metrics,
    }


def _get_eval_run_store(request: Request) -> EvalRunStore:
    return request.app.state.eval_run_store  # type: ignore[no-any-return]


def _get_audit(request: Request) -> AuditLogger:
    return request.app.state.audit_logger  # type: ignore[no-any-return]


class _EnqueueBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    suite: str = Field(min_length=1, max_length=64)


def build_eval_runs_router() -> APIRouter:
    """Enqueue + read eval runs."""
    router = APIRouter(prefix="/v1/eval-runs", tags=["eval"])

    @router.get("", response_model=None)
    async def list_runs(
        request: Request,
        store: Annotated[EvalRunStore, Depends(_get_eval_run_store)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
        status: Annotated[EvalRunStatus | None, Query()] = None,
        limit: Annotated[int, Query(ge=1, le=500)] = 50,
        offset: Annotated[int, Query(ge=0)] = 0,
        # W3 read scope — a concrete id lets a system_admin read a foreign
        # tenant's eval runs from the tenant switcher.
        tenant_id: Annotated[UUID | Literal["*"] | None, Query()] = None,
    ) -> JSONResponse:
        scope = await ensure_tenant_scope(
            request.state.principal,
            tenant_id,
            audit,
            trace_id=current_trace_id_hex(),
            endpoint="GET /v1/eval-runs",
            cross_tenant_enabled=cross_tenant_query_enabled(request),
        )
        if isinstance(scope, CrossTenant):
            # ``EvalRunStore`` has no cross-tenant aggregate reader and the
            # "*" aggregate is a spec non-goal here — fall back to the caller's
            # home tenant (the pre-scope-threading behavior, mirroring the
            # front-end ``concreteTenantScope`` collapse).
            scope = SingleTenant(tenant_id=request.state.principal.tenant_id)
        async with applied_scope(scope):
            items, total = await store.list_for_tenant(
                tenant_id=scope.tenant_id, status=status, limit=limit, offset=offset
            )
        return JSONResponse(
            content={"items": [_run_dict(r) for r in items], "total": total},
        )

    @router.post("", response_model=None)
    async def enqueue_run(
        body: _EnqueueBody,
        request: Request,
        store: Annotated[EvalRunStore, Depends(_get_eval_run_store)],
    ) -> JSONResponse:
        if body.suite not in _ALLOWED_SUITES:
            raise HTTPException(
                status_code=422,
                detail=f"unknown suite {body.suite!r}; allowed: {sorted(_ALLOWED_SUITES)}",
            )
        tenant_id: UUID = request.state.tenant_id
        record = EvalRunRecord(
            id=uuid4(),
            tenant_id=tenant_id,
            suite=body.suite,
            status=EvalRunStatus.QUEUED,
            triggered_by=EvalTriggeredBy.MANUAL,
            created_at=datetime.now(UTC),
        )
        await store.create_run(record)
        return JSONResponse(status_code=202, content=_run_dict(record))

    @router.get("/{run_id}", response_model=None)
    async def get_run(
        run_id: UUID,
        request: Request,
        store: Annotated[EvalRunStore, Depends(_get_eval_run_store)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
        # W3 read scope — "*" is meaningless (a run belongs to one tenant).
        tenant_id: Annotated[UUID | Literal["*"] | None, Query()] = None,
    ) -> JSONResponse:
        scope = await ensure_single_tenant_scope(
            request.state.principal,
            tenant_id,
            audit,
            trace_id=current_trace_id_hex(),
            endpoint="GET /v1/eval-runs/{run_id}",
            cross_tenant_enabled=cross_tenant_query_enabled(request),
        )
        async with applied_scope(scope):
            record = await store.get_run(run_id=run_id, tenant_id=scope.tenant_id)
        if record is None:
            raise HTTPException(status_code=404, detail="eval run not found")
        return JSONResponse(content=_run_dict(record))

    @router.get("/{run_id}/cases", response_model=None)
    async def list_cases(
        run_id: UUID,
        request: Request,
        store: Annotated[EvalRunStore, Depends(_get_eval_run_store)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
        # W3 read scope — subordinate detail read: concrete tenant only.
        tenant_id: Annotated[UUID | Literal["*"] | None, Query()] = None,
    ) -> JSONResponse:
        scope = await ensure_single_tenant_scope(
            request.state.principal,
            tenant_id,
            audit,
            trace_id=current_trace_id_hex(),
            endpoint="GET /v1/eval-runs/{run_id}/cases",
            cross_tenant_enabled=cross_tenant_query_enabled(request),
        )
        async with applied_scope(scope):
            run = await store.get_run(run_id=run_id, tenant_id=scope.tenant_id)
            if run is None:
                raise HTTPException(status_code=404, detail="eval run not found")
            cases = await store.list_case_results(run_id=run_id, tenant_id=scope.tenant_id)
        return JSONResponse(content={"cases": [_case_dict(c) for c in cases]})

    return router
