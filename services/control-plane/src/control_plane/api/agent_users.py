"""``GET /v1/agents/{name}/{version}/users`` — the per-agent users rollup.

Conversation-centric IA M2 (``docs/design/conversation-centric-ia.md`` §5):
the "users" tab of an agent lists every end-user with ≥1 conversation on
that agent, with their conversation / run rollup and token totals — the
top of the user → conversation → run drill-down.

Composition, not a new store aggregate: ``agent_run`` has no agent
column (the agent dimension lives on ``thread_meta``), so this endpoint
folds ``thread_meta.list_by_tenant`` (agent filter) through the existing
``RunStore.aggregate_by_threads`` per-user, then joins display names from
``tenant_user`` and token totals from ``token_usage`` (which carries
``agent_name`` / ``agent_version`` / ``user_id`` directly — no trace
join). The thread window is capped at ``MAX_LIST_LIMIT`` like the
conversations list; a capped read is flagged via ``X-Limit-Capped``.

Tenant-scoped like the conversations detail: an agent's operations view
lives inside one tenant, so the cross-tenant ``"*"`` scope is rejected
(a system_admin passes a concrete ``tenant_id`` to drill in).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from control_plane.api._authz import console_only, require
from control_plane.api._user_scope import get_user_repo, resolve_target_user_id
from control_plane.audit import emit
from control_plane.purge import PurgeUserDeps, purge_user
from control_plane.tenant_scope import (
    CrossTenant,
    SingleTenant,
    applied_scope,
    cross_tenant_query_enabled,
    ensure_tenant_scope,
)
from expert_work.common.observability import current_trace_id_hex
from expert_work.persistence.tenant_member import TenantMemberStore
from expert_work.persistence.tenant_user.base import TenantUserStore
from expert_work.persistence.thread_meta import ThreadMetaStore
from expert_work.persistence.token_usage_store import TokenTotals, TokenUsageStore
from expert_work.protocol import AuditAction, AuditResult, Principal, TenantMember, TenantUser
from expert_work.runtime.audit.logger import AuditLogger
from expert_work.runtime.runs import RunStore
from expert_work.runtime.runs.schemas import ThreadRunAggregate
from expert_work.runtime.runs.store import MAX_LIST_LIMIT


def _get_thread_repo(request: Request) -> ThreadMetaStore:
    return request.app.state.thread_meta_repo  # type: ignore[no-any-return]


def _get_run_store(request: Request) -> RunStore:
    return request.app.state.run_store  # type: ignore[no-any-return]


def _get_token_usage_store(request: Request) -> TokenUsageStore:
    return request.app.state.token_usage_store  # type: ignore[no-any-return]


def _get_audit(request: Request) -> AuditLogger:
    return request.app.state.audit_logger  # type: ignore[no-any-return]


def _get_member_repo(request: Request) -> TenantMemberStore | None:
    return getattr(request.app.state, "tenant_member_repo", None)


@dataclass
class _UserFold:
    """Mutable per-user accumulator for the thread → user fold."""

    conversation_count: int = 0
    run_count: int = 0
    error_count: int = 0
    pending_count: int = 0
    last_run_at: datetime | None = field(default=None)


def _fold_by_owner(
    owner_by_thread: dict[UUID, UUID],
    aggs: dict[UUID, ThreadRunAggregate],
) -> dict[UUID, _UserFold]:
    """Fold per-thread run aggregates up to their owning ``user_id``."""
    folds: dict[UUID, _UserFold] = {}
    for thread_id, agg in aggs.items():
        owner = owner_by_thread.get(thread_id)
        if owner is None:
            continue
        fold = folds.setdefault(owner, _UserFold())
        fold.conversation_count += 1
        fold.run_count += agg.run_count
        fold.error_count += agg.error_count
        fold.pending_count += agg.pending_count
        if agg.last_run_at is not None and (
            fold.last_run_at is None or agg.last_run_at > fold.last_run_at
        ):
            fold.last_run_at = agg.last_run_at
    return folds


def _tokens_to_dict(t: TokenTotals) -> dict[str, Any]:
    return {
        "input_tokens": t.input_tokens,
        "output_tokens": t.output_tokens,
        "cache_creation_tokens": t.cache_creation_tokens,
        "cache_read_tokens": t.cache_read_tokens,
        "total_tokens": t.total_tokens,
        "llm_calls": t.llm_calls,
        "models": list(t.models),
    }


def _registry_user_to_dict(
    user: TenantUser,
    fold: _UserFold | None,
    member: TenantMember | None,
) -> dict[str, Any]:
    """One row of the tenant-wide user roster (Phase 2 ``GET /v1/users``).

    Keyed on the registry row (so a user whose threads were all purged still
    appears with zero counts). ``subject_id`` is the identifier the calling
    app / operator recognises (the ``user_id`` an external caller passed, or
    an employee's OIDC sub). ``is_member`` distinguishes an employee (a linked
    ``tenant_member``) from an external API end-user (none)."""
    return {
        "user_id": str(user.id),
        "subject_id": user.subject_id,
        "subject_type": user.subject_type,
        "display_name": user.display_name,
        "is_member": member is not None,
        "member_email": member.email if member is not None else None,
        "member_role": member.role if member is not None else None,
        "conversation_count": fold.conversation_count if fold is not None else 0,
        "run_count": fold.run_count if fold is not None else 0,
        "error_count": fold.error_count if fold is not None else 0,
        "pending_count": fold.pending_count if fold is not None else 0,
        "last_active_at": user.last_active_at.isoformat() if user.last_active_at else None,
        "last_run_at": (
            fold.last_run_at.isoformat() if fold is not None and fold.last_run_at else None
        ),
    }


def _user_to_dict(
    user_id: UUID,
    fold: _UserFold,
    user: TenantUser | None,
    tokens: TokenTotals | None,
) -> dict[str, Any]:
    return {
        "user_id": str(user_id),
        "display_name": user.display_name if user is not None else None,
        "conversation_count": fold.conversation_count,
        "run_count": fold.run_count,
        "error_count": fold.error_count,
        "pending_count": fold.pending_count,
        "last_run_at": fold.last_run_at.isoformat() if fold.last_run_at is not None else None,
        "tokens": _tokens_to_dict(tokens) if tokens is not None else None,
    }


def build_agent_users_router() -> APIRouter:
    """Mount ``GET /v1/agents/{name}/{version}/users``."""
    router = APIRouter(prefix="/v1/agents", tags=["agents"])

    # ``console_only()`` per route, not per prefix: ``/v1/agents`` is shared
    # with the third-party plane, so the prefix sweep that closed the rest of
    # the console cannot reach here — which is why this one stayed open. It is
    # the admin-UI "Agent → users" tab, the sibling of the ``/v1/users`` roster
    # already locked in this same file, and it answered 200 to a **zero-scope**
    # service-account key: every end-user id, display name, conversation / run /
    # error counts, last-active time and token usage (P1 final review, C2).
    @router.get(
        "/{agent_name}/{agent_version}/users",
        response_model=None,
        dependencies=[Depends(console_only())],
    )
    async def list_agent_users(
        agent_name: str,
        agent_version: str,
        request: Request,
        threads: Annotated[ThreadMetaStore, Depends(_get_thread_repo)],
        runs: Annotated[RunStore, Depends(_get_run_store)],
        token_usage: Annotated[TokenUsageStore, Depends(_get_token_usage_store)],
        users: Annotated[TenantUserStore, Depends(get_user_repo)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
        offset: Annotated[int, Query(ge=0)] = 0,
        # An agent's users view lives inside one tenant — a concrete id lets
        # a system_admin drill in; the "*" scope is rejected below.
        tenant_id: Annotated[UUID | None, Query()] = None,
    ) -> JSONResponse:
        trace_id = current_trace_id_hex()
        start = time.monotonic()

        scope = await ensure_tenant_scope(
            request.state.principal,
            tenant_id,
            audit,
            trace_id=trace_id,
            endpoint="GET /v1/agents/{name}/{version}/users",
            cross_tenant_enabled=cross_tenant_query_enabled(request),
        )
        if isinstance(scope, CrossTenant):
            raise HTTPException(
                status_code=422,
                detail="an agent's users view is per-tenant; pass a concrete tenant_id",
            )
        target = scope.tenant_id

        async with applied_scope(SingleTenant(tenant_id=target)):
            # Thread window — same cap semantics as the conversations list.
            # A tenant+agent with more than MAX_LIST_LIMIT non-empty threads
            # gets a partial rollup, flagged via X-Limit-Capped.
            metas = await threads.list_by_tenant(
                target,
                agent_name=agent_name,
                agent_version=agent_version,
                nonempty=True,
                limit=MAX_LIST_LIMIT,
                offset=0,
            )
            thread_capped = len(metas) >= MAX_LIST_LIMIT

            owner_by_thread = {m.thread_id: m.user_id for m in metas if m.user_id is not None}
            aggs = (
                await runs.aggregate_by_threads(thread_ids=list(owner_by_thread), tenant_id=target)
                if owner_by_thread
                else {}
            )

            folds = _fold_by_owner(owner_by_thread, aggs)

            user_ids = list(folds)
            names = await users.get_many(user_ids, tenant_id=target) if user_ids else {}
            totals = (
                await token_usage.totals_by_users(
                    agent_name=agent_name,
                    agent_version=agent_version,
                    user_ids=user_ids,
                )
                if user_ids
                else {}
            )

        # Most-recently-active first — the operator's "who used this agent"
        # question is recency-shaped.
        ordered = sorted(
            folds.items(),
            key=lambda kv: (
                kv[1].last_run_at.timestamp() if kv[1].last_run_at is not None else float("-inf")
            ),
            reverse=True,
        )
        page = ordered[offset : offset + limit]
        items = [_user_to_dict(uid, fold, names.get(uid), totals.get(uid)) for uid, fold in page]

        await emit(
            audit,
            tenant_id=request.state.tenant_id,
            actor_id=request.state.actor_id,
            action=AuditAction.RUN_LIST_READ,
            resource_type="run",
            resource_id=None,
            result=AuditResult.SUCCESS,
            trace_id=trace_id,
            details={
                "view": "agent_users",
                "agent_name": agent_name,
                "agent_version": agent_version,
                "count": len(items),
                "elapsed_ms": round((time.monotonic() - start) * 1000, 1),
            },
        )

        headers = {"X-Limit-Capped": "true"} if thread_capped else None
        return JSONResponse(
            content={
                "success": True,
                "data": {"items": items, "total": len(folds), "cross_tenant": False},
                "error": None,
            },
            headers=headers,
        )

    return router


def _build_purge_deps(request: Request) -> PurgeUserDeps:
    """Assemble the ``purge_user`` deps from ``request.app.state`` (Phase 3a).

    The workspace store, the supervisor-owned volume-backup DLQ, and the
    object store are optional (a deployment may not wire them); every other
    store is a hard dependency of the cascade purge."""
    state = request.app.state
    return PurgeUserDeps(
        threads=state.thread_meta_repo,
        runtime=state.agent_runtime,
        memory=state.memory_repo,
        memory_dlq=state.memory_writeback_dlq,
        artifacts=state.artifact_store,
        mcp_oauth=state.mcp_oauth_connection_store,
        agent_instances=state.agent_instance_store,
        approvals=state.approval_store,
        triggers=state.trigger_store,
        trigger_runs=state.trigger_run_store,
        webhook_endpoints=state.webhook_endpoint_store,
        webhook_deliveries=state.webhook_delivery_store,
        image_uploads=state.image_upload_store,
        user_uploads=state.user_upload_store,
        feedback=state.feedback_store,
        object_store=getattr(state, "object_store", None),
        volume_backup_dlq=getattr(state, "volume_backup_dlq", None),
        token_usage=state.token_usage_store,
        runs=state.run_store,
        skills=state.skill_store,
        eval_datasets=state.eval_dataset_store,
        curation_candidates=state.curation_candidate_store,
        tenant_users=state.tenant_user_repo,
        audit=state.audit_logger,
        workspace_store=getattr(state, "workspace_store", None),
    )


def build_tenant_users_router() -> APIRouter:
    """Mount ``GET /v1/users/{user_id}`` — one registry row.

    Conversation-centric IA fast-follow: the user-detail page needs the
    member's ``display_name`` on a direct URL open (it previously only
    rode the Users-tab navigation state). Same per-user gate as the
    governance filters — the caller reads themself, a tenant admin reads
    any member (``resolve_target_user_id``).
    """
    router = APIRouter(prefix="/v1/users", tags=["users"])

    @router.get("", response_model=None, dependencies=[Depends(console_only())])
    async def list_users(
        request: Request,
        principal: Annotated[Principal, Depends(require("user", "read"))],
        threads: Annotated[ThreadMetaStore, Depends(_get_thread_repo)],
        runs: Annotated[RunStore, Depends(_get_run_store)],
        users: Annotated[TenantUserStore, Depends(get_user_repo)],
        members: Annotated[TenantMemberStore | None, Depends(_get_member_repo)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
        offset: Annotated[int, Query(ge=0)] = 0,
        # A concrete id lets a system_admin drill into one tenant; the "*"
        # cross-tenant scope is rejected below (a roster is per-tenant).
        tenant_id: Annotated[UUID | None, Query()] = None,
    ) -> JSONResponse:
        """Phase 2 — the tenant-wide user-dimension roster (admin-only).

        Lists every principal that has acted as ``subject_type="user"`` in the
        tenant (API-supplied end-users + logged-in employees), each with a
        conversation/run rollup and an ``is_member`` tag (an employee has a
        linked ``tenant_member``; an external API end-user has none). Keyed on
        the registry row, so a user whose threads were all purged still shows
        (zero counts) — the data-owner never silently vanishes.
        """
        trace_id = current_trace_id_hex()
        start = time.monotonic()

        scope = await ensure_tenant_scope(
            principal,
            tenant_id,
            audit,
            trace_id=trace_id,
            endpoint="GET /v1/users",
            cross_tenant_enabled=cross_tenant_query_enabled(request),
        )
        if isinstance(scope, CrossTenant):
            raise HTTPException(
                status_code=422,
                detail="the user roster is per-tenant; pass a concrete tenant_id",
            )
        target = scope.tenant_id

        async with applied_scope(SingleTenant(tenant_id=target)):
            # The canonical population — every human-principal registry row.
            user_rows = await users.list_by_tenant(
                target, subject_type="user", limit=MAX_LIST_LIMIT, offset=0
            )
            user_capped = len(user_rows) >= MAX_LIST_LIMIT

            # Live-thread run stats, folded per owner (same cap as the
            # conversations list). A user with no live thread folds to None.
            # A truncated thread window silently under-counts every user's
            # run stats, so its cap must also raise X-Limit-Capped.
            metas = await threads.list_by_tenant(
                target, nonempty=True, limit=MAX_LIST_LIMIT, offset=0
            )
            thread_capped = len(metas) >= MAX_LIST_LIMIT
            owner_by_thread = {m.thread_id: m.user_id for m in metas if m.user_id is not None}
            aggs = (
                await runs.aggregate_by_threads(thread_ids=list(owner_by_thread), tenant_id=target)
                if owner_by_thread
                else {}
            )
            folds = _fold_by_owner(owner_by_thread, aggs)

            # Roster tags — a member whose subject_id back-fills to this
            # tenant_user.id is an employee (vs an external API end-user). A
            # truncated roster would mis-tag members past the cap as external,
            # so its cap also raises X-Limit-Capped.
            member_by_user: dict[UUID, TenantMember] = {}
            member_capped = False
            if members is not None:
                roster = await members.list_for_tenant(tenant_id=target, limit=MAX_LIST_LIMIT)
                member_capped = len(roster) >= MAX_LIST_LIMIT
                member_by_user = {m.subject_id: m for m in roster if m.subject_id is not None}

        rows = [
            _registry_user_to_dict(u, folds.get(u.id), member_by_user.get(u.id)) for u in user_rows
        ]
        page = rows[offset : offset + limit]

        await emit(
            audit,
            tenant_id=request.state.tenant_id,
            actor_id=request.state.actor_id,
            action=AuditAction.RUN_LIST_READ,
            resource_type="run",
            resource_id=None,
            result=AuditResult.SUCCESS,
            trace_id=trace_id,
            details={
                "view": "users",
                "count": len(page),
                "total": len(rows),
                "elapsed_ms": round((time.monotonic() - start) * 1000, 1),
            },
        )

        capped = user_capped or thread_capped or member_capped
        headers = {"X-Limit-Capped": "true"} if capped else None
        return JSONResponse(
            content={
                "success": True,
                "data": {"items": page, "total": len(rows), "cross_tenant": False},
                "error": None,
            },
            headers=headers,
        )

    @router.get("/{user_id}", response_model=None, dependencies=[Depends(console_only())])
    async def get_tenant_user(
        user_id: UUID,
        request: Request,
        users: Annotated[TenantUserStore, Depends(get_user_repo)],
        members: Annotated[TenantMemberStore | None, Depends(_get_member_repo)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
        # A registry row belongs to one tenant — a concrete id lets a
        # system_admin drill in; the "*" scope is rejected below.
        tenant_id: Annotated[UUID | None, Query()] = None,
    ) -> JSONResponse:
        scope = await ensure_tenant_scope(
            request.state.principal,
            tenant_id,
            audit,
            trace_id=current_trace_id_hex(),
            endpoint="GET /v1/users/{user_id}",
            cross_tenant_enabled=cross_tenant_query_enabled(request),
        )
        if isinstance(scope, CrossTenant):
            raise HTTPException(
                status_code=422,
                detail="a user belongs to one tenant; pass a concrete tenant_id",
            )
        target = scope.tenant_id

        # Self-or-admin gate (403 for a plain member asking about someone
        # else); the actual read hides cross-tenant existence behind 404.
        await resolve_target_user_id(request, users, requested=user_id)
        user = await users.get(user_id, tenant_id=target)
        if user is None:
            raise HTTPException(status_code=404, detail="user not found")

        # Same member join the roster does — an employee's ``subject_id`` is an
        # OIDC sub UUID, so the email is the only name the detail page can show.
        # An external end-user has no member row and needs none: the id their
        # app passed in *is* the recognisable name.
        member = (
            await members.get_by_subject_id(tenant_id=target, subject_id=user.id)
            if members is not None
            else None
        )

        return JSONResponse(
            content={
                "success": True,
                "data": {
                    "user_id": str(user.id),
                    "subject_id": user.subject_id,
                    "display_name": user.display_name,
                    "subject_type": user.subject_type,
                    "is_member": member is not None,
                    "member_email": member.email if member is not None else None,
                    "member_role": member.role if member is not None else None,
                    "created_at": user.created_at.isoformat() if user.created_at else None,
                    "last_active_at": (
                        user.last_active_at.isoformat() if user.last_active_at else None
                    ),
                },
                "error": None,
            }
        )

    @router.post("/{user_id}:purge", response_model=None, dependencies=[Depends(console_only())])
    async def purge_tenant_user(
        user_id: UUID,
        request: Request,
        # Admin-only: ``user:write`` is ADMIN-exclusive (same gate the members
        # page revoke uses). A viewer / operator gets a 403 here.
        principal: Annotated[Principal, Depends(require("user", "write"))],
        users: Annotated[TenantUserStore, Depends(get_user_repo)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
        # A user belongs to one tenant — a concrete id lets a system_admin drill
        # in; the "*" cross-tenant scope is rejected (a purge is per-tenant).
        tenant_id: Annotated[UUID | None, Query()] = None,
    ) -> JSONResponse:
        """Phase 3a — irreversibly cascade-purge a user's data + assets.

        HARD-DELETE the user's high-PII rows (threads / memory / artifacts /
        agent-instances / MCP OAuth / triggers / webhooks / approvals / image
        uploads / DLQs / sandboxes), ANONYMIZE the tenant-owned billing /
        analytics / asset rows (token_usage / agent_run / skill / eval /
        curation — the row is KEPT, the user link nulled), soft-delete the
        workspace volume (mark deleted; archive and release are completed
        asynchronously by janitor at 30-minute granularity; purge only
        guarantees the mark has landed by the time it returns), and
        soft-deactivate the ``tenant_user`` row. Best-effort per step +
        idempotent — the returned summary carries per-store counts and any step
        that failed; re-running is a safe no-op that retries the failures.

        Data-only, for *any* user in the tenant — an external end-user, an
        employee (console member), or the caller purging themself: purging
        never touches ``tenant_member`` / Keycloak / role bindings, so it
        cannot revoke console access. Deleting an employee's *account* stays a
        separate flow on the members page.
        """
        trace_id = current_trace_id_hex()
        scope = await ensure_tenant_scope(
            principal,
            tenant_id,
            audit,
            trace_id=trace_id,
            endpoint="POST /v1/users/{user_id}:purge",
            cross_tenant_enabled=cross_tenant_query_enabled(request),
        )
        if isinstance(scope, CrossTenant):
            raise HTTPException(
                status_code=422,
                detail="a user belongs to one tenant; pass a concrete tenant_id",
            )
        target = scope.tenant_id

        async with applied_scope(SingleTenant(tenant_id=target)):
            # 404 (hides cross-tenant existence) when the user is unknown. A
            # re-purge still finds the row — 3a only soft-deactivates it, never
            # hard-deletes — so the purge stays idempotent.
            user = await users.get(user_id, tenant_id=target)
            if user is None:
                raise HTTPException(status_code=404, detail="user not found")

            summary = await purge_user(
                tenant_id=target,
                user_id=user_id,
                subject_id=user.subject_id,
                deps=_build_purge_deps(request),
                actor_id=request.state.actor_id,
                trace_id=trace_id,
            )

        return JSONResponse(content={"success": True, "data": summary.as_dict(), "error": None})

    return router
