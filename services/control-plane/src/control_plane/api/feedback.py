"""``POST /v1/sessions/{thread_id}/feedback`` — user feedback capture.

Stream G.6. Records a 👍/👎 (+ optional comment) a user leaves on an
agent session or a specific turn, correlated to the W3C trace id.

The endpoint does not check that the thread exists: feedback is a
fire-and-forget user signal, the row is tenant-scoped by RLS, and the
schema carries no foreign key (``turn_seq`` points at ``event_log``,
which is cold-archived — G.8). The 👍/👎 button itself is Stream H
(Admin UI); G.6 is the backend (Mini-ADR G-5).
"""

from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from control_plane.api._authz import require_key_scope
from control_plane.audit import emit
from expert_work.common.observability import current_trace_id_hex
from expert_work.persistence.feedback_store import FeedbackRecord, FeedbackStore
from expert_work.protocol import AuditAction
from expert_work.runtime.audit.logger import AuditLogger


class FeedbackRequest(BaseModel):
    """POST body for a feedback submission."""

    model_config = ConfigDict(extra="forbid")

    rating: Literal["up", "down"]
    comment: str | None = Field(default=None, max_length=4000)
    turn_seq: int | None = Field(default=None, ge=0)


def _get_feedback_store(request: Request) -> FeedbackStore:
    return request.app.state.feedback_store  # type: ignore[no-any-return]


def _get_audit(request: Request) -> AuditLogger:
    return request.app.state.audit_logger  # type: ignore[no-any-return]


def build_feedback_router() -> APIRouter:
    router = APIRouter(prefix="/v1/sessions", tags=["feedback"])

    @router.post(
        "/{thread_id}/feedback",
        response_model=None,
        status_code=201,
        dependencies=[Depends(require_key_scope("write"))],
    )
    async def submit_feedback(
        thread_id: UUID,
        payload: FeedbackRequest,
        request: Request,
        store: Annotated[FeedbackStore, Depends(_get_feedback_store)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
    ) -> JSONResponse:
        tenant_id: UUID = request.state.tenant_id
        actor_id: str = request.state.actor_id
        trace_id = current_trace_id_hex()

        stored = await store.insert(
            FeedbackRecord(
                tenant_id=tenant_id,
                thread_id=thread_id,
                turn_seq=payload.turn_seq,
                trace_id=trace_id,
                rating=payload.rating,
                comment=payload.comment,
                actor_id=actor_id,
            )
        )

        # Audit the action — never the free-text comment (keeps user
        # prose out of the audit trail; the comment lives in `feedback`).
        await emit(
            audit,
            tenant_id=tenant_id,
            actor_id=actor_id,
            action=AuditAction.FEEDBACK_CREATE,
            resource_type="feedback",
            resource_id=str(stored.id),
            trace_id=trace_id,
            details={"thread_id": str(thread_id), "rating": payload.rating},
        )

        return JSONResponse(
            status_code=201,
            content={
                "id": stored.id,
                "thread_id": str(stored.thread_id),
                "rating": stored.rating,
                "turn_seq": stored.turn_seq,
                "trace_id": stored.trace_id,
            },
        )

    return router
