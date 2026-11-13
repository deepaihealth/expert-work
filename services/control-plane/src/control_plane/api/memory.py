"""``/v1/memory`` CRUD endpoints — Stream K.K6.

The Stream J.3 long-term memory layer let the agent *write* and *recall*
memories, but the user had no way to list, edit, or forget what the
agent had remembered. STREAM-K-DESIGN § 3.K6 calls this the (c)-class
"memory ops" gap: a per-user persistent agent without user-facing
memory controls is a weak version of the product form.

Three endpoints, all per-user scoped:

* ``GET /v1/memory`` — list a user's live (non-deleted) memories,
  newest first. Embedding vectors are stripped from the response so
  the JSON stays small.
* ``PATCH /v1/memory/{id}`` — rewrite ``content`` (and optionally
  ``kind``). Triggers a re-embed against the configured embedder so
  subsequent recall ranks the updated text correctly.
* ``DELETE /v1/memory/{id}`` — soft-delete (the forget action). The
  row is hidden from recall / list immediately; the retention sweep
  hard-deletes 90 days after (``retention-cleanup-job``).

Per-user enforcement: every call resolves the caller's ``user_id``
via ``resolve_caller_user_id`` and the store filters
``(tenant_id, user_id)`` directly (defence in depth with the migration
0017 RLS policy). A machine principal — no ``user_id`` claim —
receives 403.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Annotated, Any, Literal, NamedTuple
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from control_plane.api._authz import console_only, require
from control_plane.api._user_scope import get_user_repo, resolve_caller_user_id
from control_plane.audit import emit
from control_plane.auth.rbac import is_admin
from control_plane.tenant_scope import (
    CrossTenant,
    SingleTenant,
    applied_scope,
    cross_tenant_query_enabled,
    ensure_single_tenant_scope,
    ensure_tenant_scope,
)
from control_plane.uplift.threat_metrics import (
    record_threat_pattern_hits,
    record_threat_scan,
)
from expert_work.common.observability import current_trace_id_hex
from expert_work.common.threat_patterns import ThreatFinding, scan_for_threats
from expert_work.persistence.memory import MemoryStore
from expert_work.persistence.memory.base import NON_CORRECTION_MAX_CONFIDENCE
from expert_work.persistence.tenant_user import TenantUserStore
from expert_work.protocol import AuditAction, MemoryItem, Principal
from expert_work.runtime.audit.logger import AuditLogger
from orchestrator import AgentFactoryError
from orchestrator.llm import embed_with_model

logger = logging.getLogger("expert_work.control_plane.api.memory")


_LIST_LIMIT_MAX: int = 200
_LIST_LIMIT_DEFAULT: int = 50
_PATCH_CONTENT_MAX_CHARS: int = 4000


class UpdateMemoryRequest(BaseModel):
    """Body for ``PATCH /v1/memory/{id}``."""

    model_config = ConfigDict(extra="forbid")
    content: str = Field(min_length=1, max_length=_PATCH_CONTENT_MAX_CHARS)
    kind: Literal["fact", "episodic"] | None = None


class CorrectMemoryRequest(BaseModel):
    """Body for ``POST /v1/memory/{id}/correct`` — Stream Memory-Enhance (M-4).

    An end-user's authoritative correction of their own memory:
    ``action="rewrite"`` replaces the content (and asserts it as truth →
    confidence 1.0); ``action="forget"`` soft-deletes it as wrong. ``content``
    is required for (and only used by) ``rewrite``.
    """

    model_config = ConfigDict(extra="forbid")
    action: Literal["rewrite", "forget"]
    content: str | None = Field(default=None, max_length=_PATCH_CONTENT_MAX_CHARS)


def _get_memory_repo(request: Request) -> MemoryStore:
    return request.app.state.memory_repo  # type: ignore[no-any-return]


def _get_audit(request: Request) -> AuditLogger:
    return request.app.state.audit_logger  # type: ignore[no-any-return]


def _serialise(item: MemoryItem) -> dict[str, Any]:
    """Project a ``MemoryItem`` for the wire — drop the embedding vector and,
    with it, the name of the model that computed it (B-300: internal only)."""
    data = item.model_dump(mode="json")
    data.pop("embedding", None)
    data.pop("embedding_model", None)
    return data


def _finding_to_dict(f: ThreatFinding) -> dict[str, Any]:
    return {
        "pattern_id": f.pattern_id,
        "category": f.category,
        "severity": f.severity,
        "excerpt": f.excerpt,
    }


# Capability Uplift Sprint #2 (Mini-ADR U-3 Layer A) — strict pre-scan.
async def _scan_memory_strict(
    *,
    content: str,
    memory_id: UUID,
    tenant_id: UUID,
    actor_id: str,
    audit: AuditLogger,
) -> None:
    """Strict-scope scan of USER-authored ``content`` — flag + audit, do NOT
    block (audit-eval Phase 3).

    A human authoring their own tenant's memory should not have a write
    silently rejected because a strict pattern (e.g. ``cat .env``,
    ``authorized_keys``) appears in legitimate devops/security notes. The write
    proceeds; the hit is recorded as ``MEMORY_INJECTION_WARN`` for traceability.
    The runtime injection vectors still block — recalled memory (model-facing)
    and auto-extracted write-back. See docs/design/sandbox-audit-evaluation.md."""
    findings = scan_for_threats(content, scope="strict")
    if not findings:
        record_threat_scan(scope="strict", result="clean")
        return
    record_threat_scan(scope="strict", result="warn")
    record_threat_pattern_hits(findings, scope="strict")
    await emit(
        audit,
        tenant_id=tenant_id,
        actor_id=actor_id,
        action=AuditAction.MEMORY_INJECTION_WARN,
        resource_type="memory_item",
        resource_id=str(memory_id),
        trace_id=current_trace_id_hex(),
        details={
            "scope": "strict",
            "source": "api",
            "pattern_count": len(findings),
            "findings": [_finding_to_dict(f) for f in findings],
        },
    )


async def _require_caller_user(request: Request, users: TenantUserStore) -> tuple[UUID, UUID]:
    """Resolve ``(tenant_id, user_id)``, 403 for machine principals.

    Memory is per-user — every endpoint here needs both. A JWT carrying
    a service-account / mTLS principal has no user binding so the
    endpoint is not applicable.
    """
    principal: Principal = request.state.principal
    user_id = await resolve_caller_user_id(request, users)
    if user_id is None:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "USER_SCOPE_REQUIRED",
                "message": "memory endpoints are per-user; caller has no user binding",
            },
        )
    return principal.tenant_id, user_id


class _TargetUser(NamedTuple):
    """Who a memory call acts on, and who is asking (both in the caller's
    home tenant's user registry)."""

    user_id: UUID
    caller_user_id: UUID
    #: True when an admin acts on a different user — drives the audit
    #: ``target_user_id`` so the governance action is attributable.
    cross_user: bool


async def _resolve_target_user(
    request: Request, users: TenantUserStore, *, requested: UUID | None
) -> _TargetUser:
    """Resolve the target user for a per-user call.

    Phase 2 — the user-detail Memory tab lets a tenant admin edit / forget
    another member's memory. Machine principal → 403 (memory is per-user);
    ``requested`` None or the caller's own id → the caller; a tenant admin may
    target another member; anyone else asking for someone else is 403.
    """
    _, caller_user_id = await _require_caller_user(request, users)
    if requested is None or requested == caller_user_id:
        return _TargetUser(caller_user_id, caller_user_id, False)
    if not is_admin(request.state.principal):
        raise HTTPException(
            status_code=403,
            detail={
                "code": "USER_SCOPE_FORBIDDEN",
                "message": "only tenant admins may act on another user's memories",
            },
        )
    return _TargetUser(requested, caller_user_id, True)


async def _resolve_write_target(
    request: Request,
    users: TenantUserStore,
    audit: AuditLogger,
    *,
    requested_user: UUID | None,
    requested_tenant: UUID | None,
    endpoint: str,
) -> tuple[SingleTenant, _TargetUser]:
    """B-300 D8 — the tenant + user an edit / forget acts on.

    The console's non-home views show rows from other tenants, so a write may
    name the row's tenant. It resolves like ``GET`` does, through
    ``ensure_single_tenant_scope`` (``allowed_tenants``, the tenant-switch
    audit, the deployment's cross-tenant switch; ``*`` is a read aggregate and
    is refused for a write). On top of that, writing into a tenant other than
    the caller's home one needs a platform admin: the caller's admin role
    belongs to their home tenant, and ``allowed_tenants`` only grants reading
    elsewhere. Naming another tenant requires naming the user too: the
    caller's own user lives in their home tenant. Omitting the tenant keeps
    today's meaning — the caller's home tenant.
    """
    principal: Principal = request.state.principal
    if (
        requested_tenant is not None
        and requested_tenant != principal.tenant_id
        and requested_user is None
    ):
        # Before resolution: no tenant-switch audit for a request that can
        # never succeed.
        raise HTTPException(
            status_code=422,
            detail={
                "code": "TARGET_USER_REQUIRED",
                "message": "a write into another tenant must name the target user_id",
            },
        )
    scope = await ensure_single_tenant_scope(
        principal,
        requested_tenant,
        audit,
        trace_id=current_trace_id_hex(),
        endpoint=endpoint,
        cross_tenant_enabled=cross_tenant_query_enabled(request),
    )
    if scope.tenant_id != principal.tenant_id and not principal.is_system_admin:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "PLATFORM_ADMIN_REQUIRED",
                "message": "only a platform admin may change memories in another tenant",
            },
        )
    target = await _resolve_target_user(request, users, requested=requested_user)
    return scope, target


def build_memory_router() -> APIRouter:
    router = APIRouter(prefix="/v1/memory", tags=["memory"], dependencies=[Depends(console_only())])

    @router.get("")
    async def list_memories(
        request: Request,
        principal: Annotated[Principal, Depends(require("memory", "read"))],
        store: Annotated[MemoryStore, Depends(_get_memory_repo)],
        users: Annotated[TenantUserStore, Depends(get_user_repo)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
        kind: Annotated[Literal["fact", "episodic"] | None, Query()] = None,
        limit: Annotated[int, Query(ge=1, le=_LIST_LIMIT_MAX)] = _LIST_LIMIT_DEFAULT,
        tenant_id: Annotated[UUID | Literal["*"] | None, Query()] = None,  # Stream N
        # Conversation-centric IA M2 — a tenant admin's governance view of
        # one member's memories (the user-detail Memory tab). Non-admins
        # may only read their own; asking for someone else is a 403.
        user_id: Annotated[UUID | None, Query()] = None,
        # Stream P5b-2c — the memory tab's time-travel control. None (default)
        # is today's behavior (list_for_user applies no bi-temporal filter);
        # given, only memories valid AT that instant are returned.
        as_of: Annotated[datetime | None, Query()] = None,
    ) -> dict[str, Any]:
        scope = await ensure_tenant_scope(
            principal,
            tenant_id,
            audit,
            trace_id=current_trace_id_hex(),
            endpoint="GET /v1/memory",
            cross_tenant_enabled=cross_tenant_query_enabled(request),
        )
        # B-300 D8 — the list page offers "correct" only on the caller's own
        # rows, and the console identity is the login subject, not the memory
        # user id. None where the caller can have no rows: the cross-tenant
        # aggregate, or a tenant other than their home one.
        caller_user_id: UUID | None = None
        async with applied_scope(scope):
            if isinstance(scope, CrossTenant):
                # Platform-admin view aggregates every user's memories
                # across every tenant — per-user binding is intentionally
                # dropped (system_admin sees the whole picture).
                items = await store.list_all_tenants(kind=kind, limit=limit)
            else:
                # Single-tenant: keep the per-user enforcement. A tenant admin
                # may read another member's memory (the user-detail Memory tab);
                # anyone else asking for someone else is a 403.
                target = await _resolve_target_user(request, users, requested=user_id)
                target_user_id, cross_user = target.user_id, target.cross_user
                if scope.tenant_id == principal.tenant_id:
                    caller_user_id = target.caller_user_id
                items = await store.list_for_user(
                    tenant_id=scope.tenant_id,
                    user_id=target_user_id,
                    kind=kind,
                    as_of=as_of,
                    limit=limit,
                )
                if cross_user:
                    # Read auditing — "who looked at whom" (Phase 2 governance).
                    await emit(
                        audit,
                        tenant_id=scope.tenant_id,
                        actor_id=principal.subject_id,
                        action=AuditAction.USER_DATA_VIEW,
                        resource_type="user",
                        resource_id=str(target_user_id),
                        trace_id=current_trace_id_hex(),
                        details={"view": "memory", "count": len(items)},
                    )
        return {
            "success": True,
            "data": {
                "items": [_serialise(i) for i in items],
                "total": len(items),
                "cross_tenant": isinstance(scope, CrossTenant),
                "caller_user_id": None if caller_user_id is None else str(caller_user_id),
            },
            "error": None,
        }

    @router.patch("/{memory_id}")
    async def update_memory(
        memory_id: UUID,
        payload: UpdateMemoryRequest,
        request: Request,
        principal: Annotated[Principal, Depends(require("memory", "write"))],
        store: Annotated[MemoryStore, Depends(_get_memory_repo)],
        users: Annotated[TenantUserStore, Depends(get_user_repo)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
        # Phase 2 — a tenant admin edits another member's memory (user-detail
        # Memory tab). Omitted / own id → the caller; non-admin → 403.
        user_id: Annotated[UUID | None, Query()] = None,
        # B-300 D8 — the row's tenant, from the list page's cross-tenant view.
        # Omitted → the caller's home tenant.
        tenant_id: Annotated[UUID | None, Query()] = None,
    ) -> dict[str, Any]:
        scope, target = await _resolve_write_target(
            request,
            users,
            audit,
            requested_user=user_id,
            requested_tenant=tenant_id,
            endpoint="PATCH /v1/memory/{memory_id}",
        )
        target_tenant_id = scope.tenant_id
        async with applied_scope(scope):
            # B-300 D8 — find the row first: a 404 must not cost a strict-scan
            # audit row or an embedding call for content that is never stored.
            existing = await store.get_live(
                tenant_id=target_tenant_id, user_id=target.user_id, memory_id=memory_id
            )
            if existing is None:
                raise HTTPException(status_code=404, detail="memory not found")
            # Capability Uplift Sprint #2 — strict scan BEFORE embedder so
            # poisoned content doesn't cost an OpenAI call.
            await _scan_memory_strict(
                content=payload.content,
                memory_id=memory_id,
                tenant_id=target_tenant_id,
                actor_id=principal.subject_id,
                audit=audit,
            )
            # Stream T (PR B) — the embedder is always a ``DynamicResolvingEmbedder``
            # object now; it resolves the live platform embedding config at call time
            # and raises ``AgentFactoryError`` when embedding is unconfigured.
            # Re-embedding is required to keep recall ranking honest; without it the
            # row would carry a vector for the old content and silently mis-rank.
            # Catch the unconfigured failure and surface the same typed 503 as before.
            embedder = request.app.state.embedder
            try:
                vectors, embedding_model = await embed_with_model(
                    embedder, [payload.content], tenant_id=target_tenant_id
                )
            except AgentFactoryError as exc:
                raise HTTPException(
                    status_code=503,
                    detail={
                        "code": "EMBEDDER_UNCONFIGURED",
                        "message": (
                            "memory PATCH requires an embedder — set "
                            "EXPERT_WORK_EMBEDDING_API_KEY_REF + "
                            "EXPERT_WORK_EMBEDDING_MODEL"
                        ),
                    },
                ) from exc

            # B-300 —— 来源按编辑人区分(用户 11-13 拍板):本人改自己的,这段文字是用户
            # 自己写的 →「用户消息里说的」,置信度不动(1.0 仍是更正);别人改的 → 来源置空、
            # 置信度压到 0.99 以内(在存储里按行上的值压),显示「来源未记录」,也就不再受
            # 拦截规则保护。「本人」= 这一行的用户就是调用者自己的记忆用户(D8 判「更正」
            # 按钮用的同一个身份),且在调用者的主租户里。
            own = (
                target_tenant_id == principal.tenant_id
                and existing.user_id == target.caller_user_id
            )
            updated = await store.update_content(
                tenant_id=target_tenant_id,
                user_id=target.user_id,
                memory_id=memory_id,
                content=payload.content,
                embedding=vectors[0],
                kind=payload.kind,
                embedding_model=embedding_model,
                origin="user_stated" if own else None,
                clear_origin=not own,
                max_confidence=None if own else NON_CORRECTION_MAX_CONFIDENCE,
            )
            # Still possible: forgotten between the lookup and the write.
            if updated is None:
                raise HTTPException(status_code=404, detail="memory not found")
            details: dict[str, Any] = {"kind": updated.kind, "content_len": len(payload.content)}
            if target.cross_user:
                details["target_user_id"] = str(target.user_id)
            await emit(
                audit,
                tenant_id=target_tenant_id,
                actor_id=principal.subject_id,
                action=AuditAction.MEMORY_UPDATE,
                resource_type="memory_item",
                resource_id=str(memory_id),
                trace_id=current_trace_id_hex(),
                details=details,
            )
        return {"success": True, "data": _serialise(updated), "error": None}

    @router.delete("/{memory_id}", status_code=204)
    async def forget_memory(
        memory_id: UUID,
        request: Request,
        principal: Annotated[Principal, Depends(require("memory", "delete"))],
        store: Annotated[MemoryStore, Depends(_get_memory_repo)],
        users: Annotated[TenantUserStore, Depends(get_user_repo)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
        # Phase 2 — a tenant admin forgets another member's memory (user-detail
        # Memory tab). Omitted / own id → the caller; non-admin → 403.
        user_id: Annotated[UUID | None, Query()] = None,
        # B-300 D8 — the row's tenant, from the list page's cross-tenant view.
        # Omitted → the caller's home tenant.
        tenant_id: Annotated[UUID | None, Query()] = None,
    ) -> None:
        scope, target = await _resolve_write_target(
            request,
            users,
            audit,
            requested_user=user_id,
            requested_tenant=tenant_id,
            endpoint="DELETE /v1/memory/{memory_id}",
        )
        async with applied_scope(scope):
            ok = await store.soft_delete(
                tenant_id=scope.tenant_id, user_id=target.user_id, memory_id=memory_id
            )
            if not ok:
                raise HTTPException(status_code=404, detail="memory not found")
            await emit(
                audit,
                tenant_id=scope.tenant_id,
                actor_id=principal.subject_id,
                action=AuditAction.MEMORY_FORGET,
                resource_type="memory_item",
                resource_id=str(memory_id),
                trace_id=current_trace_id_hex(),
                details={"target_user_id": str(target.user_id)} if target.cross_user else None,
            )

    @router.post("/{memory_id}/correct")
    async def correct_memory(
        memory_id: UUID,
        payload: CorrectMemoryRequest,
        request: Request,
        principal: Annotated[Principal, Depends(require("memory", "write"))],
        store: Annotated[MemoryStore, Depends(_get_memory_repo)],
        users: Annotated[TenantUserStore, Depends(get_user_repo)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
    ) -> dict[str, Any]:
        """End-user self-correction of their own memory — Stream Memory-Enhance (M-4).

        ``rewrite`` replaces the content (re-embedded for honest recall) and
        asserts it as truth (confidence → 1.0); ``forget`` soft-deletes it as
        wrong. Per-user scoped (machine principals 403) and audited as
        ``MEMORY_CORRECT`` so a correction is distinguishable from an admin edit.
        """
        tenant_id, user_id = await _require_caller_user(request, users)

        if payload.action == "forget":
            ok = await store.soft_delete(tenant_id=tenant_id, user_id=user_id, memory_id=memory_id)
            if not ok:
                raise HTTPException(status_code=404, detail="memory not found")
            await emit(
                audit,
                tenant_id=tenant_id,
                actor_id=principal.subject_id,
                action=AuditAction.MEMORY_CORRECT,
                resource_type="memory_item",
                resource_id=str(memory_id),
                trace_id=current_trace_id_hex(),
                details={"action": "forget"},
            )
            return {"success": True, "data": None, "error": None}

        # action == "rewrite" — content is required.
        content = (payload.content or "").strip()
        if not content:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "CONTENT_REQUIRED",
                    "message": "rewrite corrections require non-empty content",
                },
            )
        # B-300 D8 — find the row first (same reason as PATCH). Corrections stay
        # self-only: a ``user_id`` / ``tenant_id`` on the query is not read here.
        if await store.get_live(tenant_id=tenant_id, user_id=user_id, memory_id=memory_id) is None:
            raise HTTPException(status_code=404, detail="memory not found")
        await _scan_memory_strict(
            content=content,
            memory_id=memory_id,
            tenant_id=tenant_id,
            actor_id=principal.subject_id,
            audit=audit,
        )
        embedder = request.app.state.embedder
        try:
            vectors, embedding_model = await embed_with_model(
                embedder, [content], tenant_id=tenant_id
            )
        except AgentFactoryError as exc:
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "EMBEDDER_UNCONFIGURED",
                    "message": (
                        "memory correction requires an embedder — set "
                        "EXPERT_WORK_EMBEDDING_API_KEY_REF + EXPERT_WORK_EMBEDDING_MODEL"
                    ),
                },
            ) from exc

        # A user correction asserts the rewrite as truth → confidence 1.0.
        # CONTRACT (RT-2 PR-2): 1.0 is this endpoint's EXCLUSIVE sentinel —
        # the memory injection budget's correction guarantee keys on
        # ``confidence >= 1.0`` and the extraction write path caps its
        # LLM-scored confidence at 0.99 (orchestrator
        # ``memory._MAX_EXTRACTED_CONFIDENCE``); no other caller may write 1.0.
        updated = await store.update_content(
            tenant_id=tenant_id,
            user_id=user_id,
            memory_id=memory_id,
            content=content,
            embedding=vectors[0],
            confidence=1.0,
            # B-300 —— 来源列不动:有效来源由置信度 1.0 表达(「用户更正」)。
            embedding_model=embedding_model,
        )
        if updated is None:
            raise HTTPException(status_code=404, detail="memory not found")
        await emit(
            audit,
            tenant_id=tenant_id,
            actor_id=principal.subject_id,
            action=AuditAction.MEMORY_CORRECT,
            resource_type="memory_item",
            resource_id=str(memory_id),
            trace_id=current_trace_id_hex(),
            details={"action": "rewrite", "content_len": len(content)},
        )
        return {"success": True, "data": _serialise(updated), "error": None}

    return router
