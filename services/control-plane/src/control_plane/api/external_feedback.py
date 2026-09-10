"""对外打分 —— ``POST /v1/agents/{agent_code}/runs/{run_id}/feedback``(P-2)。

打分对象是 **run**(一轮):``/messages`` 不给消息 id,``/items`` 的条目 id 不跨接口稳定,
三条读路径都稳定的一等 id 只有 ``run_id``(spec §2.4)。``item_id`` 只是对接方自己的
段落标签,只存不 join。

同 ``(run, 终端用户)`` 再打 = 覆盖(可改票),``created_at`` 不变、写 ``updated_at``。
归属校验与 ``:cancel`` 同一套:不属于 ``(user, agent)`` 一律 404 ``RUN_NOT_FOUND``,
不泄露存在性。
"""

from __future__ import annotations

import logging
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from control_plane.api._authz import external_only, require
from control_plane.api._external import (
    ExternalScopeError,
    external_error,
    load_owned_run,
    reject_nul,
    reject_nul_path_params,
)
from control_plane.api._user_scope import get_user_repo
from control_plane.audit import emit
from control_plane.feedback_candidates import (
    CandidateSyncDeps,
    CandidateSyncResult,
    sync_candidate_for_feedback,
)
from expert_work.common.observability import current_trace_id_hex
from expert_work.persistence.curation import CurationCandidateStore
from expert_work.persistence.feedback_store import FeedbackRecord, FeedbackStore
from expert_work.persistence.tenant_user import TenantUserStore
from expert_work.persistence.thread_meta import ThreadMetaStore
from expert_work.protocol import AuditAction
from expert_work.runtime.audit.logger import AuditLogger
from expert_work.runtime.runs import RunStore
from orchestrator.trajectory import TrajectoryReader

logger = logging.getLogger("expert_work.control_plane.api.external_feedback")


class ExternalFeedbackRequest(BaseModel):
    """Body for ``POST /v1/agents/{agent_code}/runs/{run_id}/feedback``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    user_id: str = Field(min_length=1, max_length=255)
    rating: Literal["up", "down"]
    comment: str | None = Field(default=None, max_length=4000)
    item_id: str | None = Field(default=None, max_length=255)

    # ``comment`` / ``item_id`` 原样进 ``feedback.comment`` / ``item_id``(text 列),
    # 一个 NUL 就是 asyncpg 的 CharacterNotInRepertoireError → 裸文本 500;
    # ``user_id`` 在 ``external_subject_id`` 里已经守过一次,这里不重复。
    @field_validator("comment", "item_id")
    @classmethod
    def _no_nul(cls, value: str | None, info: object) -> str | None:
        if value is None:
            return None
        field = getattr(info, "field_name", "value")
        return reject_nul(value, field=field)


def _get_thread_repo(request: Request) -> ThreadMetaStore:
    return request.app.state.thread_meta_repo  # type: ignore[no-any-return]


def _get_run_store(request: Request) -> RunStore:
    return request.app.state.run_store  # type: ignore[no-any-return]


def _get_feedback_store(request: Request) -> FeedbackStore:
    return request.app.state.feedback_store  # type: ignore[no-any-return]


def _get_audit(request: Request) -> AuditLogger:
    return request.app.state.audit_logger  # type: ignore[no-any-return]


def _get_candidate_sync_deps(request: Request) -> CandidateSyncDeps:
    """👎 同步进池要的三样:thread_meta(agent 身份)、候选表、trajectory 读面。

    没有 ObjectStore(注入 runtime 的装配)时 ``reader=None`` —— 同步进池整条
    退化成 ``deferred``,300s 的 curation worker 兜底。
    """
    object_store = getattr(request.app.state, "object_store", None)
    candidates: CurationCandidateStore = request.app.state.curation_candidate_store
    return CandidateSyncDeps(
        threads=request.app.state.thread_meta_repo,
        candidates=candidates,
        reader=TrajectoryReader(object_store=object_store) if object_store is not None else None,
    )


def build_external_feedback_router() -> APIRouter:
    """Mount the external per-run feedback endpoint."""
    router = APIRouter(
        prefix="/v1/agents",
        tags=["external"],
        dependencies=[Depends(reject_nul_path_params), Depends(external_only())],
    )

    @router.post(
        "/{agent_code}/runs/{run_id}/feedback",
        response_model=None,
        dependencies=[Depends(require("session", "write"))],
    )
    async def rate_run(
        agent_code: str,
        run_id: UUID,
        payload: ExternalFeedbackRequest,
        request: Request,
        threads: Annotated[ThreadMetaStore, Depends(_get_thread_repo)],
        users: Annotated[TenantUserStore, Depends(get_user_repo)],
        runs: Annotated[RunStore, Depends(_get_run_store)],
        store: Annotated[FeedbackStore, Depends(_get_feedback_store)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
        sync_deps: Annotated[CandidateSyncDeps, Depends(_get_candidate_sync_deps)],
    ) -> JSONResponse:
        tenant_id: UUID = request.state.tenant_id
        try:
            run, meta = await load_owned_run(
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

        trace_id = current_trace_id_hex()
        actor_id = str(meta.user_id)
        # 覆盖前的旧票 —— 👎→👍 的改票标记只认「上一票是 👎」。
        previous = next(
            (
                r.rating
                for r in await store.list_for_thread_scoped(
                    tenant_id=tenant_id, thread_id=run.thread_id
                )
                if r.run_id == run.run_id and r.actor_id == actor_id
            ),
            None,
        )
        stored, updated = await store.upsert(
            FeedbackRecord(
                tenant_id=tenant_id,
                thread_id=run.thread_id,
                run_id=run.run_id,
                rating=payload.rating,
                comment=payload.comment,
                item_id=payload.item_id,
                source="external",
                trace_id=trace_id,
                actor_id=actor_id,
            )
        )
        candidate: CandidateSyncResult = "noop"
        try:
            candidate = await sync_candidate_for_feedback(
                deps=sync_deps,
                tenant_id=tenant_id,
                thread_id=run.thread_id,
                run_id=run.run_id,
                rating=payload.rating,
                previous_rating=previous,
                comment=payload.comment,
                source="external",
            )
        except Exception:
            # 进池是反馈的副产品:它失败不能让用户那一票丢掉;worker 300s 后兜底。
            # 记进审计的 ``candidate`` 一格,否则失败与真正的 ``noop`` 无从分辨。
            candidate = "failed"
            logger.warning("feedback.candidate_sync_failed", exc_info=True)

        # 审计只记动作,永不记评论原文(评论住在 feedback 表里,控制台全员可见是
        # 另一回事;审计流不该复制一份用户散文)。
        await emit(
            audit,
            tenant_id=tenant_id,
            actor_id=request.state.actor_id,
            action=AuditAction.FEEDBACK_CREATE,
            resource_type="feedback",
            resource_id=str(stored.id),
            trace_id=trace_id,
            details={
                "thread_id": str(run.thread_id),
                "run_id": str(run.run_id),
                "rating": payload.rating,
                "updated": updated,
                "source": "external",
                "candidate": candidate,
            },
            on_behalf_of=actor_id,
        )
        return JSONResponse(
            {
                "success": True,
                "data": {"run_id": str(run.run_id), "rating": stored.rating, "updated": updated},
                "error": None,
            }
        )

    return router
