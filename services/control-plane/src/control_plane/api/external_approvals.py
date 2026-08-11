"""External approval decisions for third-party apps — ``/v1/agents/{agent_code}/...``.

``POST /v1/agents/{agent_code}/runs/{run_id}:decide`` is the external
counterpart of the console's ``POST /v1/sessions/{id}/runs/{id}/resume``
(J.8): a third-party app applies a human verdict on a run paused for
approval, scoped to its own end-user (``user_id``). Ownership is gated
through ``_external.load_owned_run`` (404 hides cross-user / cross-agent
existence, same as every other external endpoint); the verdict itself is
applied by ``api/runs.py``'s request-free ``resolve_approval_decision`` —
the exact CAS + checkpoint + continuation-spawn core the console resume
endpoint and the timeout sweep already share, so there is exactly one place
that ever applies a decision.

The continuation always runs under a **new** ``run_id`` (never the paused
one) — carried on ``X-Expert-Work-Run-Id`` on every response shape. Unlike
the console resume endpoint (which only ever streams), ``mode`` lets a
third party choose not to hold an SSE connection open just to apply a
verdict: ``stream`` (default) returns the continuation's SSE stream —
identical to the console endpoint; ``queue`` returns 202 with the
continuation's ``run_id`` immediately, and the caller reads it via the
event-replay endpoint (``GET .../runs/{run_id}/events``). Both modes report
the idempotent-replay case (a retried ``idempotency_key`` matching an
already-decided approval) as a plain JSON body — there is no live worker to
stream in that case, mirroring the console endpoint's own 200 JSON reply.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.api._authz import require
from control_plane.api._external import (
    ExternalScopeError,
    external_error,
    load_owned_run,
)
from control_plane.api._user_scope import get_user_repo
from control_plane.api.runs import resolve_approval_decision
from control_plane.runtime import AgentRuntime
from expert_work.persistence import ApprovalStore
from expert_work.persistence.agent_spec import AgentSpecStore
from expert_work.persistence.tenant_user import TenantUserStore
from expert_work.persistence.thread_meta import ThreadMetaStore
from expert_work.protocol import ApprovalStatus, Principal
from expert_work.runtime.audit.logger import AuditLogger
from expert_work.runtime.runs import RunStore
from orchestrator import sse_consumer

#: Maps a decision to the ``agent_approval`` row's terminal status — mirrors
#: ``api/runs.py::apply_approval_decision``'s own ``_status_for``.
_STATUS_FOR: dict[str, ApprovalStatus] = {
    "approve": ApprovalStatus.APPROVED,
    "reject": ApprovalStatus.REJECTED,
    "modify": ApprovalStatus.MODIFIED,
}

#: Fallback envelope ``code`` by HTTP status for an ``HTTPException`` raised
#: inside ``resolve_approval_decision`` whose ``detail`` is a plain string
#: (not the ``{code, message}`` shape ``AGENT_DELETED`` uses). That function
#: is request-free (Stream 9.5) and never builds the external envelope
#: itself — this endpoint owns that translation, same as every other
#: external route's ``external_error``.
_DECISION_ERROR_CODES: dict[int, str] = {
    404: "APPROVAL_NOT_FOUND",
    409: "APPROVAL_CONFLICT",
    403: "AGENT_BLOCKED",
    422: "AGENT_BUILD_FAILED",
}


class ExternalDecideRequest(BaseModel):
    """Body for ``POST /v1/agents/{agent_code}/runs/{run_id}:decide``.

    Mirrors ``api/runs.py``'s ``ResumeRequest`` (the console J.8 resume
    body) plus the app's own end-user identifier — this endpoint verifies
    ``run_id`` belongs to ``(user_id, agent_code)`` before applying the
    verdict. ``modified_args`` is required for — and only for —
    ``decision == "modify"``, exactly like ``ResumeRequest`` /
    ``ApprovalDecision`` (``expert_work.protocol.approval``).
    """

    model_config = ConfigDict(extra="forbid")

    user_id: str = Field(min_length=1, max_length=255)
    decision: Literal["approve", "reject", "modify"]
    modified_args: dict[str, Any] | None = None
    reason: str | None = Field(default=None, max_length=2048)
    # Stream 13.2 parity — a retry / concurrent decide carrying the same key
    # replays the same continuation instead of 409'ing.
    idempotency_key: str | None = Field(default=None, max_length=255)
    #: Unlike the console resume endpoint (always SSE), a third party may not
    #: want to hold a connection open just to apply a verdict. ``queue``
    #: returns the continuation's run_id immediately (202); ``stream``
    #: (default) returns its SSE stream, matching the console endpoint.
    mode: Literal["stream", "queue"] = "stream"

    @model_validator(mode="after")
    def _check_modified_args(self) -> ExternalDecideRequest:
        if self.decision == "modify" and self.modified_args is None:
            msg = "decision 'modify' requires modified_args"
            raise ValueError(msg)
        if self.decision != "modify" and self.modified_args is not None:
            msg = "modified_args is only valid with decision 'modify'"
            raise ValueError(msg)
        return self


def _get_thread_repo(request: Request) -> ThreadMetaStore:
    return request.app.state.thread_meta_repo  # type: ignore[no-any-return]


def _get_run_store(request: Request) -> RunStore:
    return request.app.state.run_store  # type: ignore[no-any-return]


def _get_audit(request: Request) -> AuditLogger:
    return request.app.state.audit_logger  # type: ignore[no-any-return]


def _get_agent_repo(request: Request) -> AgentSpecStore:
    return request.app.state.agent_spec_repo  # type: ignore[no-any-return]


def _get_agent_runtime(request: Request) -> AgentRuntime:
    return request.app.state.agent_runtime  # type: ignore[no-any-return]


def _get_approval_store(request: Request) -> ApprovalStore:
    return request.app.state.approval_store  # type: ignore[no-any-return]


def _decision_error_envelope(exc: HTTPException) -> JSONResponse:
    """Render an ``HTTPException`` raised inside ``resolve_approval_decision``
    as the standard external envelope (``{success, data, error}``)."""
    # Stubs type ``HTTPException.detail`` as plain ``str``, but FastAPI's own
    # subclass accepts ``Any`` (``resolve_approval_decision`` really does
    # raise with a dict detail for AGENT_DELETED) — widen before narrowing so
    # mypy doesn't treat the ``dict`` branch as statically unreachable.
    detail: object = exc.detail
    if isinstance(detail, dict) and "code" in detail:
        code = str(detail["code"])
        message = str(detail.get("message", code))
    else:
        code = _DECISION_ERROR_CODES.get(exc.status_code, "APPROVAL_ERROR")
        message = str(detail)
    return JSONResponse(
        status_code=exc.status_code,
        content={"success": False, "data": None, "error": {"code": code, "message": message}},
    )


def build_external_approvals_router() -> APIRouter:
    """Mount the external approval-decision endpoints."""
    router = APIRouter(prefix="/v1/agents", tags=["external"])

    @router.post("/{agent_code}/runs/{run_id}:decide", response_model=None)
    async def decide_run(
        agent_code: str,
        run_id: UUID,
        payload: ExternalDecideRequest,
        request: Request,
        principal: Annotated[Principal, Depends(require("session", "write"))],
        threads: Annotated[ThreadMetaStore, Depends(_get_thread_repo)],
        users: Annotated[TenantUserStore, Depends(get_user_repo)],
        runs: Annotated[RunStore, Depends(_get_run_store)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
        agent_repo: Annotated[AgentSpecStore, Depends(_get_agent_repo)],
        runtime: Annotated[AgentRuntime, Depends(_get_agent_runtime)],
        approvals: Annotated[ApprovalStore, Depends(_get_approval_store)],
    ) -> StreamingResponse | JSONResponse:
        """Apply a human verdict on a paused run + resume it under a new run_id.

        Ownership-gated identically to every other external endpoint
        (``load_owned_run`` — 404 hides cross-user / cross-agent existence);
        the verdict itself goes through the exact same core the console
        resume endpoint and the approval-timeout sweep already share.
        """
        tenant_id: UUID = request.state.tenant_id
        actor_id: str = request.state.actor_id
        try:
            _run, meta = await load_owned_run(
                tenant_id=tenant_id,
                agent_code=agent_code,
                user_id=payload.user_id,
                run_id=run_id,
                runs=runs,
                threads=threads,
                users=users,
            )
        except ExternalScopeError as exc:
            return external_error(exc)

        try:
            run_record, continuation_run_id, replayed = await resolve_approval_decision(
                tenant_id=tenant_id,
                actor_id=actor_id,
                caller_user_id=meta.user_id,
                oauth_user_id=str(meta.user_id),
                thread_id=meta.thread_id,
                run_id=run_id,
                graph_decision=payload.decision,
                db_status=_STATUS_FOR[payload.decision],
                modified_args=payload.modified_args,
                reason=payload.reason,
                threads=threads,
                audit=audit,
                agent_repo=agent_repo,
                runtime=runtime,
                approvals=approvals,
                idempotency_key=payload.idempotency_key,
                agent_disable_service=getattr(request.app.state, "agent_disable_service", None),
                tenant_status_service=getattr(request.app.state, "tenant_status_service", None),
                workspace_store=getattr(request.app.state, "user_workspace_store", None),
            )
        except HTTPException as exc:
            return _decision_error_envelope(exc)

        # ``replayed`` — an idempotent retry matched an already-decided
        # approval; there is no live worker to stream (mirrors the console
        # resume endpoint's own 200 JSON reply). ``mode == "queue"`` — the
        # caller doesn't want the SSE stream either. Both report 202, except
        # a replay under the default ``stream`` mode stays 200 (parity with
        # the console endpoint, which never queues).
        if payload.mode == "queue" or replayed:
            return JSONResponse(
                status_code=202 if payload.mode == "queue" else 200,
                content={
                    "success": True,
                    "data": {"run_id": str(continuation_run_id)},
                    "error": None,
                },
                headers={"X-Expert-Work-Run-Id": str(continuation_run_id)},
            )

        return StreamingResponse(
            sse_consumer(
                bridge=runtime.stream_bridge,
                record=run_record,
                run_manager=runtime.run_manager,
                is_disconnected=request.is_disconnected,
                last_event_id=request.headers.get("Last-Event-ID"),
            ),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "X-Expert-Work-Run-Id": str(continuation_run_id),
            },
        )

    return router
