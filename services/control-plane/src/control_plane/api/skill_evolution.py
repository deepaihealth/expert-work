"""``/v1/skill-evolution`` — Stream SE (SE-8-2) admin governance surface.

The read + approval API over the self-evolving-skill backend (SE-1…SE-7):

* ``GET  /v1/skill-evolution/promote-requests``                  review queue
* ``POST /v1/skill-evolution/skills/{id}/promote-requests``      open a request
* ``POST /v1/skill-evolution/promote-requests/{rid}/approve``    approve (→tenant)
* ``POST /v1/skill-evolution/promote-requests/{rid}/reject``     reject
* ``GET  /v1/skill-evolution/skills/{id}/eval-results``          replay evidence
* ``GET  /v1/skill-evolution/skills/{id}/lineage``               fork/distill lineage

Lives on its own prefix (not under ``/v1/skills``) so the literal
``promote-requests`` path can't collide with the ``/v1/skills/{skill_id}``
UUID path param. The kill-switch API is SE-8-3; this PR is read + approval only.

Authz (SE-8): a tenant admin manages their own tenant; a system_admin manages
all tenants (``?tenant_id=<uuid>`` to act on another, ``?tenant_id=*`` to span
the review queue). Enforced via :func:`ensure_tenant_scope`. Responses are raw
``JSONResponse`` (matching ``/v1/skills`` / ``/v1/curation``); every write emits
an audit row.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from control_plane.api._authz import console_only
from control_plane.api.skills import (
    _get_audit,
    _get_skill_store,
    _invalidate_tenant_skills,
    _require_skill_owner_scope,
    _skill_dict,
    _version_dict,
)
from control_plane.audit import emit as audit_emit
from control_plane.auth.rbac import is_admin
from control_plane.tenant_scope import (
    CrossTenant,
    SingleTenant,
    applied_scope,
    bypass_rls_session,
    cross_tenant_query_enabled,
    ensure_tenant_scope,
)
from expert_work.common.observability import current_trace_id_hex
from expert_work.persistence import (
    DuplicatePromoteRequestError,
    PromoteRequestNotFoundError,
    SkillNotFoundError,
    SkillStore,
)
from expert_work.protocol import (
    AuditAction,
    AuditResult,
    KillSwitch,
    KillSwitchScope,
    PromoteRequestStatus,
    SkillEvalResult,
    SkillPromoteRequest,
    SkillVisibility,
)
from expert_work.runtime.audit.logger import AuditLogger

logger = logging.getLogger("expert_work.control_plane.skill_evolution")


class _RequestPromoteBody(BaseModel):
    """``POST /skills/{id}/promote-requests`` body."""

    skill_version: int = Field(ge=1)
    reason: str = Field(default="", max_length=1024)


class _DecideBody(BaseModel):
    """``POST /promote-requests/{rid}/approve|reject`` body."""

    decision_reason: str = Field(default="", max_length=1024)


class _KillSwitchBody(BaseModel):
    """``POST /kill-switch/engage|release`` body (SE-8-3)."""

    scope: KillSwitchScope
    reason: str = Field(default="", max_length=1024)


def _promote_request_dict(req: SkillPromoteRequest) -> dict[str, Any]:
    return {
        "id": str(req.id),
        "tenant_id": str(req.tenant_id),
        "skill_id": str(req.skill_id),
        "skill_version": req.skill_version,
        "status": req.status,
        "requested_by_user_id": (
            str(req.requested_by_user_id) if req.requested_by_user_id is not None else None
        ),
        "requested_by_agent_name": req.requested_by_agent_name,
        "reason": req.reason,
        "decided_by_user_id": (
            str(req.decided_by_user_id) if req.decided_by_user_id is not None else None
        ),
        "decided_at": req.decided_at.isoformat() if req.decided_at is not None else None,
        "decision_reason": req.decision_reason,
        "created_at": req.created_at.isoformat(),
    }


def _eval_result_dict(r: SkillEvalResult) -> dict[str, Any]:
    return {
        "id": str(r.id),
        "tenant_id": str(r.tenant_id) if r.tenant_id is not None else None,
        "skill_id": str(r.skill_id),
        "skill_version": r.skill_version,
        "baseline_score": r.baseline_score,
        "skill_score": r.skill_score,
        "delta": r.delta,
        "n_cases": r.n_cases,
        "replay_source": r.replay_source,
        "verdict": r.verdict,
        "high_risk": r.high_risk,
        "evolution_round": r.evolution_round,
        "created_at": r.created_at.isoformat(),
    }


def _kill_switch_dict(sw: KillSwitch | None) -> dict[str, Any] | None:
    if sw is None:
        return None
    return {
        "id": str(sw.id),
        "scope": sw.scope,
        "tenant_id": str(sw.tenant_id) if sw.tenant_id is not None else None,
        "engaged": sw.engaged,
        "reason": sw.reason,
        "engaged_by_user_id": (
            str(sw.engaged_by_user_id) if sw.engaged_by_user_id is not None else None
        ),
        "engaged_at": sw.engaged_at.isoformat() if sw.engaged_at is not None else None,
        "released_by_user_id": (
            str(sw.released_by_user_id) if sw.released_by_user_id is not None else None
        ),
        "released_at": sw.released_at.isoformat() if sw.released_at is not None else None,
        "updated_at": sw.updated_at.isoformat(),
    }


def build_skill_evolution_router() -> APIRouter:
    """SE-8-2 admin governance router (read + promote-approval)."""
    # Whole-prefix console lockdown: this is an admin governance surface with
    # no third-party story at all, and it carried no ``require(...)`` — a
    # **zero-scope** service-account key read the promote queue, the eval
    # evidence and the kill-switch state, and could engage/release the
    # kill-switch (P1 final review, C2 addendum). Router-level rather than
    # per-route so a future endpoint here is locked by construction.
    # ``console_only`` only rejects ``subject_type == "service_account"``;
    # employee JWTs keep the ``ensure_tenant_scope`` behavior documented above.
    router = APIRouter(
        prefix="/v1/skill-evolution",
        tags=["skill-evolution"],
        dependencies=[Depends(console_only())],
    )

    # ------------------------------------------------ review queue (read)

    @router.get("/promote-requests", response_model=None)
    async def list_promote_requests(
        request: Request,
        store: Annotated[SkillStore, Depends(_get_skill_store)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
        status: Annotated[PromoteRequestStatus | None, Query()] = None,
        cursor: Annotated[UUID | None, Query()] = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        tenant_id: Annotated[UUID | Literal["*"] | None, Query()] = None,
    ) -> JSONResponse:
        scope = await ensure_tenant_scope(
            request.state.principal,
            tenant_id,
            audit,
            trace_id=current_trace_id_hex(),
            endpoint="GET /v1/skill-evolution/promote-requests",
            cross_tenant_enabled=cross_tenant_query_enabled(request),
        )
        async with applied_scope(scope):
            if isinstance(scope, CrossTenant):
                # ``CrossTenant`` (``tenant_id=*``) is system_admin-only
                # (``ensure_tenant_scope``), and ``is_admin`` is True for
                # system_admin too — no non-admin caller reaches this branch,
                # so no ``skill_visibility`` filter is needed here (mirrors
                # ``list_skills_all_tenants``).
                rows, next_cursor = await store.list_promote_requests_all_tenants(
                    status=status, cursor=cursor, limit=limit
                )
            else:
                # Backlog task 8 (SE-8) — a non-admin employee must never see
                # a promote-request targeting an ``agent_private`` skill (it
                # confirms the private skill's existence + who nominated it +
                # why). Admin-opened requests for private skills are a normal,
                # ongoing part of this workflow (not just pre-fix leftovers),
                # so this filters every read, not just historical rows.
                # Filtered at the store query layer — never post-fetch — so a
                # filtered page never comes back short (same rule as
                # ``list_skills``).
                skill_visibility: SkillVisibility | None = (
                    None if is_admin(request.state.principal) else "tenant"
                )
                rows, next_cursor = await store.list_promote_requests(
                    tenant_id=scope.tenant_id,
                    status=status,
                    cursor=cursor,
                    limit=limit,
                    skill_visibility=skill_visibility,
                )
        return JSONResponse(
            status_code=200,
            content={
                "items": [_promote_request_dict(r) for r in rows],
                "next_cursor": str(next_cursor) if next_cursor is not None else None,
                "cross_tenant": isinstance(scope, CrossTenant),
            },
        )

    # ------------------------------------------------ open a request (write)

    @router.post("/skills/{skill_id}/promote-requests", response_model=None)
    async def request_promote(
        skill_id: UUID,
        body: _RequestPromoteBody,
        request: Request,
        store: Annotated[SkillStore, Depends(_get_skill_store)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
        tenant_id: Annotated[UUID | None, Query()] = None,
    ) -> JSONResponse:
        scope = await _single_scope(request, tenant_id, audit, "POST .../promote-requests")
        actor_id = getattr(request.state, "actor_id", "anonymous")
        try:
            async with applied_scope(scope):
                # SE-8 owner gate (backlog task 7, C-3) — a promote-request is
                # a write tied to the target skill (and, on approval, flips
                # its visibility straight to ``tenant``). Must be checked
                # before the store mutation, same ordering rule as C-2.
                target_skill = await store.get_skill(skill_id=skill_id, tenant_id=scope.tenant_id)
                if target_skill is None:
                    raise HTTPException(status_code=404, detail="skill not found")
                _require_skill_owner_scope(target_skill, request.state.principal)
                req = await store.request_skill_promote(
                    request_id=_new_uuid(),
                    tenant_id=scope.tenant_id,
                    skill_id=skill_id,
                    skill_version=body.skill_version,
                    requested_by_user_id=_actor_uuid(request),
                    reason=body.reason,
                )
        except SkillNotFoundError as exc:
            raise HTTPException(status_code=404, detail="skill not found") from exc
        except DuplicatePromoteRequestError as exc:
            raise HTTPException(
                status_code=409, detail="a pending promote request already exists for this skill"
            ) from exc
        await audit_emit(
            audit,
            tenant_id=scope.tenant_id,
            actor_id=actor_id,
            action=AuditAction.SKILL_PROMOTE_REQUESTED,
            resource_type="skill_promote_request",
            resource_id=str(req.id),
            result=AuditResult.SUCCESS,
            trace_id=current_trace_id_hex(),
            details={"skill_id": str(skill_id), "skill_version": body.skill_version},
        )
        return JSONResponse(status_code=201, content=_promote_request_dict(req))

    # ------------------------------------------------ approve / reject (write)

    @router.post("/promote-requests/{request_id}/approve", response_model=None)
    async def approve_promote(
        request_id: UUID,
        body: _DecideBody,
        request: Request,
        store: Annotated[SkillStore, Depends(_get_skill_store)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
        tenant_id: Annotated[UUID | None, Query()] = None,
    ) -> JSONResponse:
        return await _decide(
            request_id,
            body,
            request,
            store,
            audit,
            tenant_id,
            approve=True,
        )

    @router.post("/promote-requests/{request_id}/reject", response_model=None)
    async def reject_promote(
        request_id: UUID,
        body: _DecideBody,
        request: Request,
        store: Annotated[SkillStore, Depends(_get_skill_store)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
        tenant_id: Annotated[UUID | None, Query()] = None,
    ) -> JSONResponse:
        return await _decide(
            request_id,
            body,
            request,
            store,
            audit,
            tenant_id,
            approve=False,
        )

    async def _decide(
        request_id: UUID,
        body: _DecideBody,
        request: Request,
        store: SkillStore,
        audit: AuditLogger,
        tenant_id: UUID | None,
        *,
        approve: bool,
    ) -> JSONResponse:
        scope = await _single_scope(request, tenant_id, audit, "POST .../decide")
        decider = _actor_uuid(request)
        if decider is None:
            raise HTTPException(status_code=403, detail="a user identity is required to decide")
        try:
            async with applied_scope(scope):
                # SE-8 owner gate (backlog task 7, C-3) — ``approve`` flips the
                # target skill's visibility ``agent_private`` → ``tenant``
                # unconditionally (see ``SkillStore.approve_skill_promote``),
                # which would otherwise let any employee de-privatize someone
                # else's agent-authored skill. Load the request's target skill
                # and gate before either decision mutates anything.
                pending = await store.get_promote_request(
                    request_id=request_id, tenant_id=scope.tenant_id
                )
                if pending is None:
                    raise PromoteRequestNotFoundError(str(request_id))
                target_skill = await store.get_skill(
                    skill_id=pending.skill_id, tenant_id=scope.tenant_id
                )
                if target_skill is None:
                    raise HTTPException(status_code=404, detail="skill not found")
                _require_skill_owner_scope(target_skill, request.state.principal)
                if approve:
                    decided = await store.approve_skill_promote(
                        request_id=request_id,
                        tenant_id=scope.tenant_id,
                        decided_by_user_id=decider,
                        decision_reason=body.decision_reason,
                    )
                else:
                    decided = await store.reject_skill_promote(
                        request_id=request_id,
                        tenant_id=scope.tenant_id,
                        decided_by_user_id=decider,
                        decision_reason=body.decision_reason,
                    )
        except PromoteRequestNotFoundError as exc:
            raise HTTPException(status_code=404, detail="promote request not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail="promote request is not pending") from exc
        if approve:
            # PR-E3b — approval flips the skill's visibility ``agent_private``
            # → ``tenant``, changing the resolution set baked into cached
            # builds. Reject changes nothing resolver-visible.
            await _invalidate_tenant_skills(request, scope.tenant_id)
        await audit_emit(
            audit,
            tenant_id=scope.tenant_id,
            actor_id=getattr(request.state, "actor_id", "anonymous"),
            action=(
                AuditAction.SKILL_PROMOTE_APPROVED
                if approve
                else AuditAction.SKILL_PROMOTE_REJECTED
            ),
            resource_type="skill_promote_request",
            resource_id=str(request_id),
            result=AuditResult.SUCCESS,
            trace_id=current_trace_id_hex(),
            details={"skill_id": str(decided.skill_id), "skill_version": decided.skill_version},
        )
        return JSONResponse(status_code=200, content=_promote_request_dict(decided))

    # ------------------------------------------------ eval evidence + lineage (read)

    @router.get("/skills/{skill_id}/eval-results", response_model=None)
    async def eval_results(
        skill_id: UUID,
        request: Request,
        store: Annotated[SkillStore, Depends(_get_skill_store)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
        tenant_id: Annotated[UUID | None, Query()] = None,
    ) -> JSONResponse:
        scope = await _single_scope(request, tenant_id, audit, "GET .../eval-results")
        async with applied_scope(scope):
            # SE-8 owner gate (backlog task 7, I-1) — ``SkillEvalResult`` rows
            # don't carry skill content, but they do confirm an
            # ``agent_private`` skill's existence + performance to any
            # employee who can guess/enumerate the skill_id.
            skill = await store.get_skill(skill_id=skill_id, tenant_id=scope.tenant_id)
            if skill is None:
                raise HTTPException(status_code=404, detail="skill not found")
            _require_skill_owner_scope(skill, request.state.principal)
            rows = await store.list_eval_results(skill_id=skill_id, tenant_id=scope.tenant_id)
        return JSONResponse(
            status_code=200, content={"items": [_eval_result_dict(r) for r in rows]}
        )

    @router.get("/skills/{skill_id}/lineage", response_model=None)
    async def lineage(
        skill_id: UUID,
        request: Request,
        store: Annotated[SkillStore, Depends(_get_skill_store)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
        tenant_id: Annotated[UUID | None, Query()] = None,
    ) -> JSONResponse:
        scope = await _single_scope(request, tenant_id, audit, "GET .../lineage")
        async with applied_scope(scope):
            skill = await store.get_skill(skill_id=skill_id, tenant_id=scope.tenant_id)
            if skill is None:
                raise HTTPException(status_code=404, detail="skill not found")
            # SE-8 owner gate (backlog task 7, C-1) — ``versions`` carries the
            # full ``prompt_fragment`` (skill content), the exact leak the
            # complex review reproduced. Must run before either the versions
            # fetch or the response is built.
            _require_skill_owner_scope(skill, request.state.principal)
            versions = await store.list_versions(skill_id=skill_id, tenant_id=scope.tenant_id)
            # Resolve the fork source's name (same tenant) so the UI can render
            # the lineage edge without a second round-trip; None if it was
            # deleted or lives in another scope.
            forked_from_source = None
            if skill.forked_from is not None:
                src = await store.get_skill(skill_id=skill.forked_from, tenant_id=scope.tenant_id)
                # The fork source can itself be a *different* skill than the
                # one just gated above — e.g. a promoted (now tenant-visible)
                # skill forked from a still-agent_private source. Omit it
                # rather than 403 the whole lineage response, which the
                # caller is otherwise entitled to see.
                if src is not None:
                    try:
                        _require_skill_owner_scope(src, request.state.principal)
                    except HTTPException:
                        src = None
                forked_from_source = _skill_dict(src) if src is not None else None
        return JSONResponse(
            status_code=200,
            content={
                "skill": _skill_dict(skill),
                "forked_from_source": forked_from_source,
                "versions": [_version_dict(v) for v in versions],
            },
        )

    # ------------------------------------------------ kill-switch (SE-8-3)

    @router.get("/kill-switch", response_model=None)
    async def get_kill_switch(
        request: Request,
        store: Annotated[SkillStore, Depends(_get_skill_store)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
        tenant_id: Annotated[UUID | None, Query()] = None,
    ) -> JSONResponse:
        scope = await _single_scope(request, tenant_id, audit, "GET .../kill-switch")
        async with applied_scope(scope):
            tenant_sw = await store.get_kill_switch(scope="tenant", tenant_id=scope.tenant_id)
        # The global row is NULL-tenant — read it under bypass (a tenant-scoped
        # session can't see it). Read-only for a tenant admin; engaging it is
        # system_admin-only (see engage/release below).
        async with bypass_rls_session():
            global_sw = await store.get_kill_switch(scope="global", tenant_id=None)
        effective = bool(
            (global_sw is not None and global_sw.engaged)
            or (tenant_sw is not None and tenant_sw.engaged)
        )
        return JSONResponse(
            status_code=200,
            content={
                "global": _kill_switch_dict(global_sw),
                "tenant": _kill_switch_dict(tenant_sw),
                "effective_halted": effective,
            },
        )

    @router.post("/kill-switch/engage", response_model=None)
    async def engage_kill_switch(
        body: _KillSwitchBody,
        request: Request,
        store: Annotated[SkillStore, Depends(_get_skill_store)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
        tenant_id: Annotated[UUID | None, Query()] = None,
    ) -> JSONResponse:
        return await _set_kill_switch(request, body, store, audit, tenant_id, engaged=True)

    @router.post("/kill-switch/release", response_model=None)
    async def release_kill_switch(
        body: _KillSwitchBody,
        request: Request,
        store: Annotated[SkillStore, Depends(_get_skill_store)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
        tenant_id: Annotated[UUID | None, Query()] = None,
    ) -> JSONResponse:
        return await _set_kill_switch(request, body, store, audit, tenant_id, engaged=False)

    async def _set_kill_switch(
        request: Request,
        body: _KillSwitchBody,
        store: SkillStore,
        audit: AuditLogger,
        tenant_id: UUID | None,
        *,
        engaged: bool,
    ) -> JSONResponse:
        actor = _actor_uuid(request)
        action = (
            AuditAction.SKILL_EVOLUTION_KILL_SWITCH_ENGAGED
            if engaged
            else AuditAction.SKILL_EVOLUTION_KILL_SWITCH_RELEASED
        )
        if body.scope == "global":
            # The whole-platform stop is system_admin-only.
            if not request.state.principal.is_system_admin:
                raise HTTPException(
                    status_code=403,
                    detail="only a system admin may operate the global kill-switch",
                )
            async with bypass_rls_session():
                sw = await store.set_kill_switch(
                    switch_id=_new_uuid(),
                    scope="global",
                    tenant_id=None,
                    engaged=engaged,
                    reason=body.reason,
                    actor_user_id=actor,
                )
            audit_tenant = request.state.principal.tenant_id  # home tenant for attribution
        else:
            scope = await _single_scope(request, tenant_id, audit, "POST .../kill-switch")
            async with applied_scope(scope):
                sw = await store.set_kill_switch(
                    switch_id=_new_uuid(),
                    scope="tenant",
                    tenant_id=scope.tenant_id,
                    engaged=engaged,
                    reason=body.reason,
                    actor_user_id=actor,
                )
            audit_tenant = scope.tenant_id
        await audit_emit(
            audit,
            tenant_id=audit_tenant,
            actor_id=getattr(request.state, "actor_id", "anonymous"),
            action=action,
            resource_type="skill_evolution_kill_switch",
            resource_id=str(sw.id),
            result=AuditResult.SUCCESS,
            trace_id=current_trace_id_hex(),
            details={"scope": body.scope},
        )
        return JSONResponse(status_code=200, content=_kill_switch_dict(sw))

    return router


# ── helpers ────────────────────────────────────────────────────────────────


def _new_uuid() -> UUID:
    from uuid import uuid4

    return uuid4()


def _actor_uuid(request: Request) -> UUID | None:
    """The acting user's id as a UUID, or ``None`` if the subject isn't one
    (e.g. an mTLS service principal)."""
    actor_id = getattr(request.state, "actor_id", None)
    if actor_id is None:
        return None
    try:
        return UUID(str(actor_id))
    except (ValueError, TypeError):
        return None


async def _single_scope(
    request: Request,
    tenant_id: UUID | None,
    audit: AuditLogger,
    endpoint: str,
) -> SingleTenant:
    """Resolve to a concrete tenant (writes + per-skill reads never span ``*``)."""
    scope = await ensure_tenant_scope(
        request.state.principal,
        tenant_id,
        audit,
        trace_id=current_trace_id_hex(),
        endpoint=endpoint,
        cross_tenant_enabled=cross_tenant_query_enabled(request),
    )
    if isinstance(scope, CrossTenant):  # pragma: no cover — query type excludes "*"
        raise HTTPException(status_code=400, detail="tenant_id=* is not valid for this endpoint")
    return scope
