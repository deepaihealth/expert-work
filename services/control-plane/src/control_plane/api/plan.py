"""Stream CM-8 — the plan UI channel (STREAM-CM-DESIGN §10.2).

``GET /v1/sessions/{thread_id}/plan`` reads ``AgentState.plan`` from the
thread's checkpoint; ``PUT`` rewrites it through ``aupdate_state`` —
the same write seam the J.8 approval resume uses, so the DB stays the
single source of truth and the next turn's CM-0 projection syncs
PLAN.md automatically (no separate reconciliation, Mini-ADR CM-I2).

Both paths resolve the cached built agent via ``AgentRuntime.get_agent``
(a manifest compiles once per ``(tenant, name, version)``), so polling
the GET does not rebuild anything. Writes are rejected with 409 while
the thread's latest run is PENDING / RUNNING — an external edit racing
a live agent would be silently overwritten by its next ``update_plan``
or projection (Mini-ADR CM-I3).
"""

from __future__ import annotations

import logging
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from langchain_core.runnables import RunnableConfig

from control_plane.api._authz import require_key_scope
from control_plane.api._user_scope import (
    caller_owns_thread,
    get_user_repo,
    resolve_caller_user_id,
)
from control_plane.audit import emit
from expert_work.common.observability import current_trace_id_hex
from expert_work.common.threat_patterns import scan_for_threats
from expert_work.persistence.agent_spec import AgentSpecStore
from expert_work.persistence.tenant_user import TenantUserStore
from expert_work.protocol import AuditAction, Plan
from expert_work.runtime.audit.logger import AuditLogger
from expert_work.runtime.runs import RunStatus, RunStore

logger = logging.getLogger(__name__)

#: Statuses under which an external plan write is rejected (CM-I3) — the
#: agent owns the plan while a run is queued or live.
_WRITE_BLOCKED_STATUSES = frozenset({RunStatus.PENDING, RunStatus.RUNNING})


def _get_audit(request: Request) -> AuditLogger:
    return request.app.state.audit_logger  # type: ignore[no-any-return]


def _get_agent_repo(request: Request) -> AgentSpecStore:
    return request.app.state.agent_spec_repo  # type: ignore[no-any-return]


def _get_agent_runtime(request: Request):
    return request.app.state.agent_runtime


def _get_run_store(request: Request) -> RunStore:
    return request.app.state.run_store  # type: ignore[no-any-return]


def _get_thread_repo(request: Request) -> object:
    return request.app.state.thread_meta_repo


def _plan_scan_text(plan: Plan) -> str:
    """Goal + step descriptions — what the strict injection scan vets.

    Mirrors the CM-0 ingest scan surface (the orchestrator module is not
    importable here — control-plane never imports orchestrator at the
    top level)."""
    return "\n".join([plan.goal, *(step.description for step in plan.steps)])


def build_plan_router() -> APIRouter:
    router = APIRouter(prefix="/v1/sessions", tags=["sessions"])

    async def _resolve_built_graph(
        request: Request,
        *,
        thread_id: UUID,
        tenant_id: UUID,
        threads: object,
        users: TenantUserStore,
        agent_repo: AgentSpecStore,
        runtime: object,
    ):
        """Scope-check the thread and resolve its cached built agent."""
        meta = await threads.get(thread_id, tenant_id=tenant_id)  # type: ignore[attr-defined]
        if meta is None:
            raise HTTPException(status_code=404, detail="session not found")
        caller_user_id = await resolve_caller_user_id(request, users)
        if not caller_owns_thread(
            meta=meta, caller_user_id=caller_user_id, principal=request.state.principal
        ):
            raise HTTPException(status_code=404, detail="session not found")
        if meta.agent_name is None or meta.agent_version is None:
            raise HTTPException(status_code=409, detail="session is not bound to an agent")
        spec_record = await agent_repo.get(
            tenant_id=tenant_id, name=meta.agent_name, version=meta.agent_version
        )
        if spec_record is None:
            raise HTTPException(status_code=404, detail="agent not found")
        return await runtime.get_agent(  # type: ignore[attr-defined]
            tenant_id=tenant_id,
            name=meta.agent_name,
            version=meta.agent_version,
            spec=spec_record.spec,
            user_id=request.state.principal.subject_id,
        )

    @router.get(
        "/{thread_id}/plan", response_model=None, dependencies=[Depends(require_key_scope("read"))]
    )
    async def get_plan(
        thread_id: UUID,
        request: Request,
        threads: Annotated[object, Depends(_get_thread_repo)],
        users: Annotated[TenantUserStore, Depends(get_user_repo)],
        agent_repo: Annotated[AgentSpecStore, Depends(_get_agent_repo)],
        runtime: Annotated[object, Depends(_get_agent_runtime)],
    ) -> Response:
        """Stream CM-8 — read the thread's current plan from its checkpoint."""
        tenant_id: UUID = request.state.tenant_id
        built = await _resolve_built_graph(
            request,
            thread_id=thread_id,
            tenant_id=tenant_id,
            threads=threads,
            users=users,
            agent_repo=agent_repo,
            runtime=runtime,
        )
        config: RunnableConfig = {
            "configurable": {"thread_id": str(thread_id), "tenant_id": str(tenant_id)}
        }
        snapshot = await built.graph.aget_state(config)
        plan = (snapshot.values or {}).get("plan") if snapshot is not None else None
        if plan is None:
            return Response(status_code=204)
        return JSONResponse(Plan.model_validate(plan).model_dump(mode="json"))

    @router.put(
        "/{thread_id}/plan", response_model=None, dependencies=[Depends(require_key_scope("write"))]
    )
    async def put_plan(
        thread_id: UUID,
        payload: Plan,
        request: Request,
        threads: Annotated[object, Depends(_get_thread_repo)],
        users: Annotated[TenantUserStore, Depends(get_user_repo)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
        agent_repo: Annotated[AgentSpecStore, Depends(_get_agent_repo)],
        runtime: Annotated[object, Depends(_get_agent_runtime)],
        runs: Annotated[RunStore, Depends(_get_run_store)],
    ) -> JSONResponse:
        """Stream CM-8 — rewrite the thread's plan (the UI channel write).

        Rejected with 409 while the latest run is queued or live; the
        strict injection scan vets goal + step descriptions (same surface
        as the CM-0 file ingest) so the authoritative DB copy never takes
        tainted content (Mini-ADR CM-I6).
        """
        tenant_id: UUID = request.state.tenant_id
        actor_id: str = request.state.actor_id
        if scan_for_threats(_plan_scan_text(payload), scope="strict"):
            raise HTTPException(status_code=422, detail="plan content failed the injection scan")
        built = await _resolve_built_graph(
            request,
            thread_id=thread_id,
            tenant_id=tenant_id,
            threads=threads,
            users=users,
            agent_repo=agent_repo,
            runtime=runtime,
        )
        # ``list_by_thread`` returns oldest-first — the LAST row is the
        # thread's latest run.
        run_rows = await runs.list_by_thread(thread_id=thread_id, tenant_id=tenant_id)
        latest = run_rows[-1] if run_rows else None
        if latest is not None and latest.status in _WRITE_BLOCKED_STATUSES:
            raise HTTPException(
                status_code=409,
                detail=f"plan is owned by the agent while the run is {latest.status.value}",
            )
        config: RunnableConfig = {
            "configurable": {"thread_id": str(thread_id), "tenant_id": str(tenant_id)}
        }
        # ``as_node="agent"`` (the resume seam) re-evaluates the agent's
        # conditional edge, which needs a non-empty message history — a
        # fresh thread that never ran gets ``__start__`` instead (the
        # documented initial-state-update position).
        snapshot = await built.graph.aget_state(config)
        has_history = bool(snapshot is not None and (snapshot.values or {}).get("messages"))
        await built.graph.aupdate_state(
            config, {"plan": payload}, as_node="agent" if has_history else "__start__"
        )
        await emit(
            audit,
            tenant_id=tenant_id,
            actor_id=actor_id,
            action=AuditAction.PLAN_EDITED,
            resource_type="session",
            resource_id=str(thread_id),
            trace_id=current_trace_id_hex(),
            details={"steps": len(payload.steps)},
        )
        logger.info("plan.edited steps=%d", len(payload.steps))
        return JSONResponse(payload.model_dump(mode="json"))

    return router
