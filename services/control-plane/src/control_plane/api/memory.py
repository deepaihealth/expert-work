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
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from control_plane.api._authz import console_only, require
from control_plane.api._user_scope import get_user_repo, resolve_caller_user_id
from control_plane.audit import emit
from control_plane.auth.rbac import is_admin
from control_plane.tenant_scope import (
    CrossTenant,
    applied_scope,
    cross_tenant_query_enabled,
    ensure_tenant_scope,
)
from control_plane.uplift.threat_metrics import (
    record_threat_pattern_hits,
    record_threat_scan,
)
from expert_work.common.observability import current_trace_id_hex
from expert_work.common.threat_patterns import ThreatFinding, scan_for_threats
from expert_work.persistence.memory import MemoryStore
from expert_work.persistence.tenant_user import TenantUserStore
from expert_work.protocol import AuditAction, MemoryItem, Principal
from expert_work.runtime.audit.logger import AuditLogger
from orchestrator import AgentFactoryError

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
    """Project a ``MemoryItem`` for the wire — drop the embedding vector."""
    data = item.model_dump(mode="json")
    data.pop("embedding", None)
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


async def _resolve_target_user(
    request: Request, users: TenantUserStore, *, requested: UUID | None
) -> tuple[UUID, UUID, bool]:
    """Resolve ``(tenant_id, target_user_id, is_cross_user)`` for a mutating call.

    Phase 2 — the user-detail Memory tab lets a tenant admin edit / forget
    another member's memory. Machine principal → 403 (memory is per-user);
    ``requested`` None or the caller's own id → the caller; a tenant admin may
    target another member; anyone else asking for someone else is 403.
    ``is_cross_user`` is True when an admin acts on a different user — it drives
    the audit ``target_user_id`` so the governance action is attributable.
    """
    tenant_id, caller_user_id = await _require_caller_user(request, users)
    if requested is None or requested == caller_user_id:
        return tenant_id, caller_user_id, False
    if not is_admin(request.state.principal):
        raise HTTPException(
            status_code=403,
            detail={
                "code": "USER_SCOPE_FORBIDDEN",
                "message": "only tenant admins may act on another user's memories",
            },
        )
    return tenant_id, requested, True


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
                _, target_user_id, cross_user = await _resolve_target_user(
                    request, users, requested=user_id
                )
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
    ) -> dict[str, Any]:
        tenant_id, target_user_id, cross_user = await _resolve_target_user(
            request, users, requested=user_id
        )
        # Capability Uplift Sprint #2 — strict scan BEFORE embedder so
        # poisoned content doesn't cost an OpenAI call.
        await _scan_memory_strict(
            content=payload.content,
            memory_id=memory_id,
            tenant_id=tenant_id,
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
            vectors = await embedder.embed([payload.content], tenant_id=tenant_id)
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

        updated = await store.update_content(
            tenant_id=tenant_id,
            user_id=target_user_id,
            memory_id=memory_id,
            content=payload.content,
            embedding=vectors[0],
            kind=payload.kind,
        )
        if updated is None:
            raise HTTPException(status_code=404, detail="memory not found")
        details: dict[str, Any] = {"kind": updated.kind, "content_len": len(payload.content)}
        if cross_user:
            details["target_user_id"] = str(target_user_id)
        await emit(
            audit,
            tenant_id=tenant_id,
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
    ) -> None:
        tenant_id, target_user_id, cross_user = await _resolve_target_user(
            request, users, requested=user_id
        )
        ok = await store.soft_delete(
            tenant_id=tenant_id, user_id=target_user_id, memory_id=memory_id
        )
        if not ok:
            raise HTTPException(status_code=404, detail="memory not found")
        await emit(
            audit,
            tenant_id=tenant_id,
            actor_id=principal.subject_id,
            action=AuditAction.MEMORY_FORGET,
            resource_type="memory_item",
            resource_id=str(memory_id),
            trace_id=current_trace_id_hex(),
            details={"target_user_id": str(target_user_id)} if cross_user else None,
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
        await _scan_memory_strict(
            content=content,
            memory_id=memory_id,
            tenant_id=tenant_id,
            actor_id=principal.subject_id,
            audit=audit,
        )
        embedder = request.app.state.embedder
        try:
            vectors = await embedder.embed([content], tenant_id=tenant_id)
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
