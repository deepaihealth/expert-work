"""``/v1/approvals`` — the cross-run approval queue (Stream HX-7).

The RunDetail ``ApprovalCard`` decides one paused run at a time; this
router gives operators the queue view (STREAM-HX-DESIGN § 8.2-③④):

* ``GET /v1/approvals`` — approval rows by status (default ``pending``),
  oldest-first (queue semantics), with the Stream N tenant-scope
  framework (``tenant_id=*`` for system_admin).
* ``POST /v1/approvals:decide`` — apply up to 20 verdicts in one call.
  Each item runs the exact same kernel as the single-run resume
  endpoint (:func:`control_plane.api.runs.apply_approval_decision`);
  failures are per-item (a 409 already-decided race never aborts the
  rest). Non-streaming: the continuation workers are detached tasks,
  so the response just carries each continuation ``run_id``.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from control_plane.api._authz import console_only, require_key_scope
from control_plane.api._user_scope import get_user_repo
from control_plane.api.runs import (
    _get_agent_repo,
    _get_agent_runtime,
    _get_approval_store,
    _get_audit,
    _get_thread_repo,
    apply_approval_decision,
)
from control_plane.runtime import AgentRuntime
from control_plane.tenant_scope import (
    CrossTenant,
    applied_scope,
    cross_tenant_query_enabled,
    ensure_tenant_scope,
)
from expert_work.common.observability import current_trace_id_hex
from expert_work.persistence import ApprovalStore
from expert_work.persistence.agent_spec import AgentSpecStore
from expert_work.persistence.tenant_user import TenantUserStore
from expert_work.protocol import ApprovalRecord, ApprovalStatus
from expert_work.runtime.audit.logger import AuditLogger

logger = logging.getLogger("expert_work.control_plane.approvals")

#: Batch ceiling — every ``approve`` spawns one LLM continuation run,
#: so the batch size must be bounded (Mini-ADR HX-G4).
MAX_BATCH_DECISIONS = 20


class DecisionItem(BaseModel):
    """One verdict in the batch — the ResumeRequest shape + addressing."""

    model_config = ConfigDict(extra="forbid")

    thread_id: UUID
    run_id: UUID
    decision: Literal["approve", "reject", "modify"]
    modified_args: dict[str, Any] | None = None
    reason: str | None = Field(default=None, max_length=2048)
    # Stream 13.2 — per-item idempotency key for deterministic retry.
    idempotency_key: str | None = Field(default=None, max_length=255)


class DecideBatchRequest(BaseModel):
    """POST body for ``/v1/approvals:decide``."""

    model_config = ConfigDict(extra="forbid")

    decisions: list[DecisionItem] = Field(min_length=1, max_length=MAX_BATCH_DECISIONS)


def _record_to_dict(record: ApprovalRecord) -> dict[str, Any]:
    return {
        "id": str(record.id),
        "tenant_id": str(record.tenant_id),
        "user_id": str(record.user_id) if record.user_id is not None else None,
        "run_id": str(record.run_id),
        "thread_id": str(record.thread_id),
        "request_id": record.request_id,
        "node": record.node,
        "reason_kind": record.reason_kind,
        "action_summary": record.action_summary,
        "proposed_args": dict(record.proposed_args),
        "requested_at": record.requested_at.isoformat(),
        "timeout_at": record.timeout_at.isoformat(),
        "status": record.status.value,
        "decided_by": record.decided_by,
        "decided_at": record.decided_at.isoformat() if record.decided_at is not None else None,
    }


def build_approvals_router() -> APIRouter:
    router = APIRouter(prefix="/v1/approvals", tags=["approvals"])

    @router.get(
        "",
        response_model=None,
        dependencies=[Depends(require_key_scope("read")), Depends(console_only())],
    )
    async def list_approvals(
        request: Request,
        approvals: Annotated[ApprovalStore, Depends(_get_approval_store)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
        status: Annotated[ApprovalStatus, Query()] = ApprovalStatus.PENDING,
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
        offset: Annotated[int, Query(ge=0)] = 0,
        tenant_id: Annotated[UUID | Literal["*"] | None, Query()] = None,
    ) -> JSONResponse:
        trace_id = current_trace_id_hex()
        scope = await ensure_tenant_scope(
            request.state.principal,
            tenant_id,
            audit,
            trace_id=trace_id,
            endpoint="GET /v1/approvals",
            cross_tenant_enabled=cross_tenant_query_enabled(request),
        )
        async with applied_scope(scope):
            if isinstance(scope, CrossTenant):
                items, total = await approvals.list_all_tenants(
                    status=status, limit=limit, offset=offset
                )
            else:
                items, total = await approvals.list_for_tenant(
                    tenant_id=scope.tenant_id, status=status, limit=limit, offset=offset
                )
        return JSONResponse(
            content={
                "success": True,
                "data": {
                    "items": [_record_to_dict(r) for r in items],
                    "total": total,
                    "limit": limit,
                    "offset": offset,
                },
                "error": None,
            }
        )

    @router.post(
        ":decide",
        response_model=None,
        dependencies=[Depends(require_key_scope("write")), Depends(console_only())],
    )
    async def decide_batch(
        payload: DecideBatchRequest,
        request: Request,
        threads: Annotated[object, Depends(_get_thread_repo)],
        users: Annotated[TenantUserStore, Depends(get_user_repo)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
        agent_repo: Annotated[AgentSpecStore, Depends(_get_agent_repo)],
        runtime: Annotated[AgentRuntime, Depends(_get_agent_runtime)],
        approvals: Annotated[ApprovalStore, Depends(_get_approval_store)],
    ) -> JSONResponse:
        """Apply each verdict independently (Mini-ADR HX-G4).

        One item's failure (409 already-decided race, 404 gone, 422 bad
        modify) never aborts the rest. Each successful item has already
        spawned its detached continuation worker — operators watch the
        individual runs on RunDetail; nothing here streams.
        """
        results: list[dict[str, Any]] = []
        for item in payload.decisions:
            try:
                _, continuation_run_id, _replayed = await apply_approval_decision(
                    request=request,
                    thread_id=item.thread_id,
                    run_id=item.run_id,
                    decision=item.decision,
                    modified_args=item.modified_args,
                    reason=item.reason,
                    threads=threads,
                    users=users,
                    audit=audit,
                    agent_repo=agent_repo,
                    runtime=runtime,
                    approvals=approvals,
                    idempotency_key=item.idempotency_key,
                )
            except HTTPException as exc:
                results.append(
                    {
                        "run_id": str(item.run_id),
                        "ok": False,
                        "error": str(exc.detail),
                        "status_code": exc.status_code,
                    }
                )
            else:
                results.append(
                    {
                        "run_id": str(item.run_id),
                        "ok": True,
                        "continuation_run_id": str(continuation_run_id),
                    }
                )
        succeeded = sum(1 for r in results if r["ok"])
        logger.info(
            "control_plane.approvals.batch_decided total=%d ok=%d",
            len(results),
            succeeded,
        )
        return JSONResponse(
            content={
                "success": True,
                "data": {"results": results, "succeeded": succeeded},
                "error": None,
            }
        )

    return router
