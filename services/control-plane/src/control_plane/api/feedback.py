"""``POST /v1/sessions/{thread_id}/feedback`` — user feedback capture.

Stream G.6. Records a 👍/👎 (+ optional comment) a user leaves on an
agent session or a specific turn, correlated to the W3C trace id.

The endpoint does not check that the thread exists: feedback is a
fire-and-forget user signal, the row is tenant-scoped by RLS, and the
schema carries no foreign key (``turn_seq`` points at ``event_log``,
which is cold-archived — G.8). The 👍/👎 button itself is Stream H
(Admin UI); G.6 is the backend (Mini-ADR G-5). P-2 起按 run 打分、
同 (run, actor) 覆盖;不再校验 thread 存在这一点不变。
"""

from __future__ import annotations

import logging
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from control_plane.api._authz import console_only, require_key_scope
from control_plane.audit import emit
from control_plane.feedback_candidates import (
    CandidateSyncDeps,
    CandidateSyncResult,
    sync_candidate_for_feedback,
)
from expert_work.common.observability import current_trace_id_hex
from expert_work.persistence.curation import CurationCandidateStore
from expert_work.persistence.feedback_store import FeedbackRecord, FeedbackStore
from expert_work.protocol import AuditAction
from expert_work.runtime.audit.logger import AuditLogger
from orchestrator.trajectory import TrajectoryReader

logger = logging.getLogger("expert_work.control_plane.api.feedback")


class FeedbackRequest(BaseModel):
    """POST body for a feedback submission."""

    model_config = ConfigDict(extra="forbid")

    #: P-2 — 打分对象是一轮(run),必填。
    run_id: UUID
    rating: Literal["up", "down"]
    comment: str | None = Field(default=None, max_length=4000)
    turn_seq: int | None = Field(default=None, ge=0)


def _get_feedback_store(request: Request) -> FeedbackStore:
    return request.app.state.feedback_store  # type: ignore[no-any-return]


def _get_audit(request: Request) -> AuditLogger:
    return request.app.state.audit_logger  # type: ignore[no-any-return]


def _get_candidate_sync_deps(request: Request) -> CandidateSyncDeps:
    """员工打的 👎 与终端用户的 👎 走同一条进池逻辑(spec §6:下游一律不按来源过滤)。"""
    object_store = getattr(request.app.state, "object_store", None)
    candidates: CurationCandidateStore = request.app.state.curation_candidate_store
    return CandidateSyncDeps(
        threads=request.app.state.thread_meta_repo,
        candidates=candidates,
        reader=TrajectoryReader(object_store=object_store) if object_store is not None else None,
    )


def build_feedback_router() -> APIRouter:
    router = APIRouter(prefix="/v1/sessions", tags=["feedback"])

    @router.post(
        "/{thread_id}/feedback",
        response_model=None,
        status_code=201,
        dependencies=[Depends(require_key_scope("write")), Depends(console_only())],
    )
    async def submit_feedback(
        thread_id: UUID,
        payload: FeedbackRequest,
        request: Request,
        store: Annotated[FeedbackStore, Depends(_get_feedback_store)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
        sync_deps: Annotated[CandidateSyncDeps, Depends(_get_candidate_sync_deps)],
    ) -> JSONResponse:
        tenant_id: UUID = request.state.tenant_id
        actor_id: str = request.state.actor_id
        trace_id = current_trace_id_hex()

        # 覆盖前的旧票 —— 👎→👍 的改票标记只认「上一票是 👎」。
        previous = next(
            (
                r.rating
                for r in await store.list_for_thread_scoped(
                    tenant_id=tenant_id, thread_id=thread_id
                )
                if r.run_id == payload.run_id and r.actor_id == actor_id
            ),
            None,
        )
        stored, updated = await store.upsert(
            FeedbackRecord(
                tenant_id=tenant_id,
                thread_id=thread_id,
                run_id=payload.run_id,
                turn_seq=payload.turn_seq,
                trace_id=trace_id,
                rating=payload.rating,
                comment=payload.comment,
                source="console",
                actor_id=actor_id,
            )
        )

        candidate: CandidateSyncResult = "noop"
        try:
            candidate = await sync_candidate_for_feedback(
                deps=sync_deps,
                tenant_id=tenant_id,
                thread_id=thread_id,
                run_id=payload.run_id,
                rating=payload.rating,
                previous_rating=previous,
                comment=payload.comment,
            )
        except Exception:
            # 进池是反馈的副产品:它失败不能让员工那一票丢掉;worker 300s 后兜底。
            logger.warning("feedback.candidate_sync_failed", exc_info=True)

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
            details={
                "thread_id": str(thread_id),
                "run_id": str(payload.run_id),
                "rating": payload.rating,
                "updated": updated,
                "source": "console",
                "candidate": candidate,
            },
        )

        return JSONResponse(
            status_code=201,
            content={
                "id": stored.id,
                "thread_id": str(stored.thread_id),
                "run_id": str(payload.run_id),
                "rating": stored.rating,
                "turn_seq": stored.turn_seq,
                "trace_id": stored.trace_id,
                "updated": updated,
            },
        )

    return router
