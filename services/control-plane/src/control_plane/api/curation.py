"""``/v1/curation`` + ``/v1/eval-datasets`` — Stream J.12 (Mini-ADR J-43).

Two routers backing the learning / feedback loop's human-curation step
(STREAM-J-DESIGN § 17.5):

* :func:`build_curation_router` — review the ``curation_candidate``
  rows the worker surfaced (``/v1/curation/candidates``) and promote /
  dismiss them.
* :func:`build_eval_dataset_router` — CRUD over the curated
  ``eval_dataset`` rows (``/v1/eval-datasets``), including hand-authored
  ``golden`` cases.

Both are authenticated + tenant-scoped. The curated dataset is scoped
to ``(tenant, agent_name)`` — agent-level, not per-instance.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Annotated, Any, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from control_plane._tenant_resource_lock import tenant_resource_lock
from control_plane.audit import emit
from control_plane.settings import Settings
from control_plane.tenant_scope import (
    CrossTenant,
    applied_scope,
    cross_tenant_query_enabled,
    ensure_single_tenant_scope,
    ensure_tenant_scope,
)
from expert_work.common.observability import current_trace_id_hex
from expert_work.persistence.curation import (
    CurationCandidateStore,
    EvalDatasetInUseError,
    EvalDatasetStore,
)
from expert_work.protocol import (
    AuditAction,
    CandidateStatus,
    CurationCandidateRecord,
    CurationSignal,
    EvalDatasetRecord,
    EvalDatasetSource,
)
from expert_work.runtime.audit.logger import AuditLogger
from expert_work.runtime.storage import ObjectStore
from orchestrator.trajectory import TrajectoryReader

logger = logging.getLogger("expert_work.control_plane.curation")


def _candidate_dict(record: CurationCandidateRecord) -> dict[str, Any]:
    return {
        "id": str(record.id),
        "agent_name": record.agent_name,
        "agent_version": record.agent_version,
        "thread_id": str(record.thread_id),
        "user_id": str(record.user_id) if record.user_id is not None else None,
        "trajectory_key": record.trajectory_key,
        "outcome": record.outcome,
        "signal": record.signal,
        "feedback_rating": record.feedback_rating,
        "status": record.status.value,
        "eval_dataset_id": (
            str(record.eval_dataset_id) if record.eval_dataset_id is not None else None
        ),
        "detected_at": record.detected_at.isoformat(),
        "reviewed_at": record.reviewed_at.isoformat() if record.reviewed_at is not None else None,
    }


def _eval_dataset_dict(record: EvalDatasetRecord) -> dict[str, Any]:
    return {
        "id": str(record.id),
        "agent_name": record.agent_name,
        "name": record.name,
        "input": record.input,
        "expected": record.expected,
        "source": record.source,
        "source_trajectory_key": record.source_trajectory_key,
        "source_user_id": (
            str(record.source_user_id) if record.source_user_id is not None else None
        ),
        "created_at": record.created_at.isoformat(),
        "updated_at": record.updated_at.isoformat(),
    }


def _get_curation_store(request: Request) -> CurationCandidateStore:
    return request.app.state.curation_candidate_store  # type: ignore[no-any-return]


def _get_eval_dataset_store(request: Request) -> EvalDatasetStore:
    return request.app.state.eval_dataset_store  # type: ignore[no-any-return]


def _get_object_store(request: Request) -> ObjectStore | None:
    """The L7 trajectory ObjectStore — absent when a runtime is injected."""
    return getattr(request.app.state, "object_store", None)


def _get_audit(request: Request) -> AuditLogger:
    return request.app.state.audit_logger  # type: ignore[no-any-return]


def _get_settings(request: Request) -> Settings:
    return request.app.state.settings  # type: ignore[no-any-return]


def _get_session_factory(request: Request) -> async_sessionmaker[AsyncSession] | None:
    return request.app.state.session_factory  # type: ignore[no-any-return]


@asynccontextmanager
async def _enforce_quota(
    datasets: EvalDatasetStore,
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession] | None,
    *,
    tenant_id: UUID,
) -> AsyncIterator[None]:
    """Cap a tenant's curated rows — checked at create + promote.

    Task 4 — count-then-insert is TOCTOU-vulnerable across replicas; the
    count check and the caller's insert are one per-tenant advisory-locked
    critical section (the caller does the insert inside this ``async
    with``) so two replicas can't both pass the count check and both
    insert, overshooting ``max_eval_dataset_rows_per_tenant``.
    """
    async with tenant_resource_lock(session_factory, tenant_id, "eval_dataset"):
        count = await datasets.count_by_tenant(tenant_id=tenant_id)
        if count >= settings.max_eval_dataset_rows_per_tenant:
            raise HTTPException(
                status_code=429,
                detail=(
                    "eval-dataset quota exhausted "
                    f"(max {settings.max_eval_dataset_rows_per_tenant} per tenant)"
                ),
            )
        yield


class _PromoteBody(BaseModel):
    """Promote a candidate into a curated ``eval_dataset`` row."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=128)
    input: dict[str, Any] = Field(default_factory=dict)
    expected: dict[str, Any] | None = None
    source: EvalDatasetSource


class _CreateEvalDatasetBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_name: str = Field(min_length=1)
    name: str = Field(min_length=1, max_length=128)
    input: dict[str, Any] = Field(default_factory=dict)
    expected: dict[str, Any] | None = None
    source: EvalDatasetSource = "golden"


class _PatchEvalDatasetBody(BaseModel):
    """All fields optional — only the present ones are applied."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=128)
    input: dict[str, Any] | None = None
    expected: dict[str, Any] | None = None


def build_curation_router() -> APIRouter:
    """Stream J.12 — curation-candidate review + promote / dismiss."""
    router = APIRouter(prefix="/v1/curation", tags=["curation"])

    @router.get("/candidates", response_model=None)
    async def list_candidates(
        request: Request,
        candidates: Annotated[CurationCandidateStore, Depends(_get_curation_store)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
        agent_name: Annotated[str | None, Query(min_length=1)] = None,
        status: Annotated[CandidateStatus | None, Query()] = None,
        signal: Annotated[CurationSignal | None, Query()] = None,
        tenant_id: Annotated[UUID | Literal["*"] | None, Query()] = None,  # Stream N
    ) -> JSONResponse:
        scope = await ensure_tenant_scope(
            request.state.principal,
            tenant_id,
            audit,
            trace_id=current_trace_id_hex(),
            endpoint="GET /v1/curation/candidates",
            cross_tenant_enabled=cross_tenant_query_enabled(request),
        )
        async with applied_scope(scope):
            if isinstance(scope, CrossTenant):
                items = await candidates.list_for_review_all_tenants(
                    agent_name=agent_name, status=status, signal=signal
                )
            else:
                items = await candidates.list_for_review(
                    tenant_id=scope.tenant_id,
                    agent_name=agent_name,
                    status=status,
                    signal=signal,
                )
        return JSONResponse(
            content={
                "items": [_candidate_dict(c) for c in items],
                "total": len(items),
                "cross_tenant": isinstance(scope, CrossTenant),
            }
        )

    @router.get("/candidates/{candidate_id}", response_model=None)
    async def get_candidate(
        candidate_id: UUID,
        request: Request,
        candidates: Annotated[CurationCandidateStore, Depends(_get_curation_store)],
        object_store: Annotated[ObjectStore | None, Depends(_get_object_store)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
        # W3 read scope — a concrete id lets a system_admin drill into a
        # foreign tenant's candidate; "*" is meaningless (one owning tenant).
        tenant_id: Annotated[UUID | Literal["*"] | None, Query()] = None,
    ) -> JSONResponse:
        scope = await ensure_single_tenant_scope(
            request.state.principal,
            tenant_id,
            audit,
            trace_id=current_trace_id_hex(),
            endpoint="GET /v1/curation/candidates/{candidate_id}",
            cross_tenant_enabled=cross_tenant_query_enabled(request),
        )
        async with applied_scope(scope):
            record = await candidates.get(candidate_id=candidate_id, tenant_id=scope.tenant_id)
        if record is None:
            raise HTTPException(status_code=404, detail="curation candidate not found")
        body = _candidate_dict(record)
        # Pull the full trajectory so the reviewer can judge the run.
        # Absent ObjectStore (injected runtime) → trajectory is null.
        trajectory: dict[str, Any] | None = None
        if object_store is not None:
            stored = await TrajectoryReader(object_store=object_store).read(record.trajectory_key)
            if stored is not None:
                trajectory = {"messages": stored.messages, "step_count": stored.step_count}
        body["trajectory"] = trajectory
        return JSONResponse(content=body)

    @router.post("/candidates/{candidate_id}/promote", response_model=None)
    async def promote_candidate(
        candidate_id: UUID,
        body: _PromoteBody,
        request: Request,
        candidates: Annotated[CurationCandidateStore, Depends(_get_curation_store)],
        datasets: Annotated[EvalDatasetStore, Depends(_get_eval_dataset_store)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
        settings: Annotated[Settings, Depends(_get_settings)],
        session_factory: Annotated[
            async_sessionmaker[AsyncSession] | None, Depends(_get_session_factory)
        ],
    ) -> JSONResponse:
        tenant_id: UUID = request.state.tenant_id
        actor_id: str = request.state.actor_id
        record = await candidates.get(candidate_id=candidate_id, tenant_id=tenant_id)
        if record is None:
            raise HTTPException(status_code=404, detail="curation candidate not found")
        if record.status is not CandidateStatus.PENDING:
            raise HTTPException(status_code=409, detail=f"candidate already {record.status.value}")

        now = datetime.now(UTC)
        async with _enforce_quota(datasets, settings, session_factory, tenant_id=tenant_id):
            try:
                dataset = EvalDatasetRecord(
                    id=uuid4(),
                    tenant_id=tenant_id,
                    agent_name=record.agent_name,
                    name=body.name,
                    input=body.input,
                    expected=body.expected,
                    source=body.source,
                    source_trajectory_key=record.trajectory_key,
                    source_user_id=record.user_id,
                    created_at=now,
                    updated_at=now,
                )
            except ValidationError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            await datasets.create(dataset)
        await candidates.update(
            record.model_copy(
                update={
                    "status": CandidateStatus.PROMOTED,
                    "eval_dataset_id": dataset.id,
                    "reviewed_at": now,
                }
            )
        )
        await emit(
            audit,
            tenant_id=tenant_id,
            actor_id=actor_id,
            action=AuditAction.CURATION_PROMOTE,
            resource_type="curation_candidate",
            resource_id=str(candidate_id),
            trace_id=current_trace_id_hex(),
            details={"eval_dataset_id": str(dataset.id), "source": dataset.source},
        )
        return JSONResponse(status_code=201, content=_eval_dataset_dict(dataset))

    @router.post("/candidates/{candidate_id}/dismiss", response_model=None)
    async def dismiss_candidate(
        candidate_id: UUID,
        request: Request,
        candidates: Annotated[CurationCandidateStore, Depends(_get_curation_store)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
    ) -> JSONResponse:
        tenant_id: UUID = request.state.tenant_id
        actor_id: str = request.state.actor_id
        record = await candidates.get(candidate_id=candidate_id, tenant_id=tenant_id)
        if record is None:
            raise HTTPException(status_code=404, detail="curation candidate not found")
        if record.status is not CandidateStatus.PENDING:
            raise HTTPException(status_code=409, detail=f"candidate already {record.status.value}")
        await candidates.update(
            record.model_copy(
                update={"status": CandidateStatus.DISMISSED, "reviewed_at": datetime.now(UTC)}
            )
        )
        await emit(
            audit,
            tenant_id=tenant_id,
            actor_id=actor_id,
            action=AuditAction.CURATION_DISMISS,
            resource_type="curation_candidate",
            resource_id=str(candidate_id),
            trace_id=current_trace_id_hex(),
        )
        return JSONResponse(content={"dismissed": True})

    return router


def build_eval_dataset_router() -> APIRouter:
    """Stream J.12 — curated eval-dataset CRUD."""
    router = APIRouter(prefix="/v1/eval-datasets", tags=["eval-datasets"])

    @router.post("", response_model=None)
    async def create_eval_dataset(
        body: _CreateEvalDatasetBody,
        request: Request,
        datasets: Annotated[EvalDatasetStore, Depends(_get_eval_dataset_store)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
        settings: Annotated[Settings, Depends(_get_settings)],
        session_factory: Annotated[
            async_sessionmaker[AsyncSession] | None, Depends(_get_session_factory)
        ],
    ) -> JSONResponse:
        tenant_id: UUID = request.state.tenant_id
        actor_id: str = request.state.actor_id

        now = datetime.now(UTC)
        async with _enforce_quota(datasets, settings, session_factory, tenant_id=tenant_id):
            try:
                record = EvalDatasetRecord(
                    id=uuid4(),
                    tenant_id=tenant_id,
                    agent_name=body.agent_name,
                    name=body.name,
                    input=body.input,
                    expected=body.expected,
                    source=body.source,
                    created_at=now,
                    updated_at=now,
                )
            except ValidationError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            await datasets.create(record)
        await emit(
            audit,
            tenant_id=tenant_id,
            actor_id=actor_id,
            action=AuditAction.EVAL_DATASET_CREATE,
            resource_type="eval_dataset",
            resource_id=str(record.id),
            trace_id=current_trace_id_hex(),
            details={"agent_name": record.agent_name, "name": record.name, "source": record.source},
        )
        return JSONResponse(status_code=201, content=_eval_dataset_dict(record))

    @router.get("", response_model=None)
    async def list_eval_datasets(
        request: Request,
        datasets: Annotated[EvalDatasetStore, Depends(_get_eval_dataset_store)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
        agent_name: Annotated[str | None, Query(min_length=1)] = None,
        tenant_id: Annotated[UUID | Literal["*"] | None, Query()] = None,  # Stream N
    ) -> JSONResponse:
        scope = await ensure_tenant_scope(
            request.state.principal,
            tenant_id,
            audit,
            trace_id=current_trace_id_hex(),
            endpoint="GET /v1/eval-datasets",
            cross_tenant_enabled=cross_tenant_query_enabled(request),
        )
        async with applied_scope(scope):
            if isinstance(scope, CrossTenant):
                items = await datasets.list_all_tenants()
            elif agent_name is not None:
                items = await datasets.list_by_agent(
                    tenant_id=scope.tenant_id, agent_name=agent_name
                )
            else:
                items = await datasets.list_by_tenant(tenant_id=scope.tenant_id)
        # Apply agent_name filter in CrossTenant mode at the response layer.
        if isinstance(scope, CrossTenant) and agent_name is not None:
            items = [d for d in items if d.agent_name == agent_name]
        return JSONResponse(
            content={
                "items": [_eval_dataset_dict(d) for d in items],
                "total": len(items),
                "cross_tenant": isinstance(scope, CrossTenant),
            }
        )

    @router.get("/{dataset_id}", response_model=None)
    async def get_eval_dataset(
        dataset_id: UUID,
        request: Request,
        datasets: Annotated[EvalDatasetStore, Depends(_get_eval_dataset_store)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
        # W3 read scope — "*" is meaningless (a row belongs to one tenant).
        tenant_id: Annotated[UUID | Literal["*"] | None, Query()] = None,
    ) -> JSONResponse:
        scope = await ensure_single_tenant_scope(
            request.state.principal,
            tenant_id,
            audit,
            trace_id=current_trace_id_hex(),
            endpoint="GET /v1/eval-datasets/{dataset_id}",
            cross_tenant_enabled=cross_tenant_query_enabled(request),
        )
        async with applied_scope(scope):
            record = await datasets.get(dataset_id=dataset_id, tenant_id=scope.tenant_id)
        if record is None:
            raise HTTPException(status_code=404, detail="eval-dataset row not found")
        return JSONResponse(content=_eval_dataset_dict(record))

    @router.patch("/{dataset_id}", response_model=None)
    async def patch_eval_dataset(
        dataset_id: UUID,
        body: _PatchEvalDatasetBody,
        request: Request,
        datasets: Annotated[EvalDatasetStore, Depends(_get_eval_dataset_store)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
    ) -> JSONResponse:
        tenant_id: UUID = request.state.tenant_id
        actor_id: str = request.state.actor_id
        record = await datasets.get(dataset_id=dataset_id, tenant_id=tenant_id)
        if record is None:
            raise HTTPException(status_code=404, detail="eval-dataset row not found")
        updated = record.model_copy(
            update={
                "name": body.name if body.name is not None else record.name,
                "input": body.input if body.input is not None else record.input,
                "expected": body.expected if body.expected is not None else record.expected,
                "updated_at": datetime.now(UTC),
            }
        )
        await datasets.update(updated)
        await emit(
            audit,
            tenant_id=tenant_id,
            actor_id=actor_id,
            action=AuditAction.EVAL_DATASET_UPDATE,
            resource_type="eval_dataset",
            resource_id=str(dataset_id),
            trace_id=current_trace_id_hex(),
        )
        return JSONResponse(content=_eval_dataset_dict(updated))

    @router.delete("/{dataset_id}", response_model=None)
    async def delete_eval_dataset(
        dataset_id: UUID,
        request: Request,
        datasets: Annotated[EvalDatasetStore, Depends(_get_eval_dataset_store)],
        candidates: Annotated[CurationCandidateStore, Depends(_get_curation_store)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
    ) -> JSONResponse:
        tenant_id: UUID = request.state.tenant_id
        actor_id: str = request.state.actor_id
        # Deletion hygiene PR3 — revert PROMOTED candidates BEFORE deleting the
        # dataset row, and let a revert failure propagate (delete-then-crash
        # would recreate the dangling pointer this fixes). Revert-then-crash is
        # safe: the dataset row survives and the candidate is re-promotable.
        reverted = await candidates.revert_promoted_for_dataset(
            dataset_id=dataset_id, tenant_id=tenant_id
        )
        try:
            deleted = await datasets.delete(dataset_id=dataset_id, tenant_id=tenant_id)
        except EvalDatasetInUseError as exc:
            # Normally unreachable — the endpoint reverts referencing
            # candidates before deleting. This surfaces the 0135 RESTRICT FK
            # (hit when a write path bypasses the app-level revert) as a
            # semantic conflict instead of a 500.
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "EVAL_DATASET_IN_USE",
                    "message": "eval-dataset row is still referenced by curation candidates",
                },
            ) from exc
        if not deleted:
            raise HTTPException(status_code=404, detail="eval-dataset row not found")
        await emit(
            audit,
            tenant_id=tenant_id,
            actor_id=actor_id,
            action=AuditAction.EVAL_DATASET_DELETE,
            resource_type="eval_dataset",
            resource_id=str(dataset_id),
            trace_id=current_trace_id_hex(),
            details={"candidates_reverted": reverted},
        )
        return JSONResponse(content={"deleted": True})

    return router
