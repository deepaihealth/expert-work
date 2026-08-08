"""``/v1/artifacts`` — Stream J.9 artifact list / download / delete / patch / versions.

Artifacts are per-``(tenant, user)`` (Mini-ADR J-1). The endpoints
collectively expose:

* ``GET /v1/artifacts`` — list the caller's artifacts.
* ``GET /v1/artifacts/download?name=…`` — stream the latest version's
  content (MIME-aware + XSS-safe Content-Disposition; J.9-step3,
  STREAM-J-DESIGN § 10.5).
* ``DELETE /v1/artifacts/{name}`` — soft-delete (J.9-step3, Mini-ADR
  J-25). Lifecycle is metadata-only; the J.15 volume bytes stay until
  the retention sweep / volume lifecycle removes them.
* ``PATCH /v1/artifacts/{name}`` — update ``kind`` (J.9-step3).
* ``GET /v1/artifacts/{name}/versions`` — version history (J.9-step3).

Content lives in the user's J.15 workspace volume — only the
sandbox-supervisor can read a docker volume, so the download endpoint
proxies to the supervisor's workspace-file API and backfills the
version's ``size_bytes`` / ``sha256`` on that first read.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import UTC, datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict

from control_plane.api._artifact_mime import content_disposition_header, infer_content_type
from control_plane.api._quota_admission import check_admission
from control_plane.api._user_scope import get_user_repo, resolve_target_user_id
from control_plane.audit import emit as audit_emit
from control_plane.quota.base import QuotaService
from control_plane.tenant_scope import (
    CrossTenant,
    applied_scope,
    cross_tenant_query_enabled,
    ensure_single_tenant_scope,
    ensure_tenant_scope,
)
from expert_work.common.observability import current_trace_id_hex
from expert_work.persistence import ArtifactStore
from expert_work.persistence.rls import current_user_id_var
from expert_work.persistence.tenant_user import TenantUserStore
from expert_work.protocol import ArtifactKind, AuditAction, AuditResult
from expert_work.runtime.audit.logger import AuditLogger
from orchestrator.tools import SandboxSupervisorError, WorkspacePermissionError, WorkspaceStore

logger = logging.getLogger("expert_work.control_plane.artifacts")

#: Allowed values for ``PATCH``'s ``kind`` field — kept narrow on purpose.
_ARTIFACT_KINDS: frozenset[str] = frozenset({"document", "code", "data", "other"})


class _ArtifactPatchBody(BaseModel):
    """Mutable fields the PATCH endpoint accepts.

    M0 only takes ``kind`` — the rest of the row is immutable user
    data. Future fields (description / category / tags) land in M1
    with a corresponding schema bump.
    """

    model_config = ConfigDict(extra="forbid")

    kind: ArtifactKind


def _get_artifact_store(request: Request) -> ArtifactStore:
    return request.app.state.artifact_store  # type: ignore[no-any-return]


def _get_workspace_store(request: Request) -> WorkspaceStore | None:
    return request.app.state.workspace_store  # type: ignore[no-any-return]


def _get_quota(request: Request) -> QuotaService:
    return request.app.state.quota_service  # type: ignore[no-any-return]


def _get_audit(request: Request) -> AuditLogger:
    return request.app.state.audit_logger  # type: ignore[no-any-return]


def build_artifacts_router() -> APIRouter:
    router = APIRouter(prefix="/v1/artifacts", tags=["artifacts"])

    @router.get("", response_model=None)
    async def list_artifacts(
        request: Request,
        store: Annotated[ArtifactStore, Depends(_get_artifact_store)],
        users: Annotated[TenantUserStore, Depends(get_user_repo)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
        tenant_id: Annotated[UUID | Literal["*"] | None, Query()] = None,  # Stream N
        # Conversation-centric IA M2 — a tenant admin's governance view of
        # one member's artifacts (the user-detail Artifacts tab). Non-admins
        # may only read their own; asking for someone else is a 403.
        user_id: Annotated[UUID | None, Query()] = None,
    ) -> JSONResponse:
        scope = await ensure_tenant_scope(
            request.state.principal,
            tenant_id,
            audit,
            trace_id=current_trace_id_hex(),
            endpoint="GET /v1/artifacts",
            cross_tenant_enabled=cross_tenant_query_enabled(request),
        )
        async with applied_scope(scope):
            if isinstance(scope, CrossTenant):
                # Platform-admin view aggregates every user's artifacts.
                artifacts = await store.list_all_tenants()
                items = [
                    {
                        "name": a.name,
                        "kind": a.kind,
                        "latest_version": a.latest_version,
                        "tenant_id": str(a.tenant_id),
                        "user_id": str(a.user_id),
                    }
                    for a in artifacts
                ]
            else:
                target_user_id = await resolve_target_user_id(request, users, requested=user_id)
                # Artifacts are per-user; a machine principal owns none
                # (unless an admin machine principal targets a user).
                if target_user_id is None:
                    return JSONResponse(
                        content={"artifacts": [], "items": [], "cross_tenant": False}
                    )
                current_user_id_var.set(target_user_id)
                artifacts = await store.list_for_user(
                    tenant_id=scope.tenant_id, user_id=target_user_id
                )
                items = [
                    {"name": a.name, "kind": a.kind, "latest_version": a.latest_version}
                    for a in artifacts
                ]
        return JSONResponse(
            content={
                "artifacts": items,
                "items": items,
                "cross_tenant": isinstance(scope, CrossTenant),
            }
        )

    @router.get("/download", response_model=None)
    async def download_artifact(
        name: str,
        request: Request,
        store: Annotated[ArtifactStore, Depends(_get_artifact_store)],
        users: Annotated[TenantUserStore, Depends(get_user_repo)],
        workspace_store: Annotated[WorkspaceStore | None, Depends(_get_workspace_store)],
        quota: Annotated[QuotaService, Depends(_get_quota)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
        # H.8-F1 — tenant-admin governance: act on one member's artifact
        # (the user-detail Artifacts tab). Non-admins targeting someone
        # else get a 403 from the shared gate.
        user_id: Annotated[UUID | None, Query()] = None,
        # W2 read scope — a concrete id lets a system_admin drill into a
        # foreign tenant's artifact from the tenant switcher; "*" is
        # meaningless (an artifact belongs to one tenant).
        tenant_id: Annotated[UUID | Literal["*"] | None, Query()] = None,
    ) -> Response:
        scope = await ensure_single_tenant_scope(
            request.state.principal,
            tenant_id,
            audit,
            trace_id=current_trace_id_hex(),
            endpoint="GET /v1/artifacts/download",
            cross_tenant_enabled=cross_tenant_query_enabled(request),
        )
        target_tenant = scope.tenant_id
        target_user_id = await resolve_target_user_id(request, users, requested=user_id)
        # 404 (not 403) so a cross-user / nonexistent name stays opaque.
        if target_user_id is None:
            raise HTTPException(status_code=404, detail="artifact not found")
        current_user_id_var.set(target_user_id)
        async with applied_scope(scope):
            version = await store.get_latest_version(
                tenant_id=target_tenant, user_id=target_user_id, name=name
            )
            if version is None:
                raise HTTPException(status_code=404, detail="artifact not found")
            # Re-fetch the parent row to know the ``kind`` for MIME inference.
            artifacts = await store.list_for_user(tenant_id=target_tenant, user_id=target_user_id)
        artifact = next((a for a in artifacts if a.name == name), None)
        if artifact is None:
            # Defensive — would mean a version exists but its parent does
            # not, which the store invariants forbid.
            raise HTTPException(status_code=404, detail="artifact not found")
        # Mini-ADR J-25 (J.9-step2) — quota admission. ``cost=1`` deducts
        # from QPS + ``ARTIFACT_DOWNLOAD_COUNT_30D`` (only the
        # dimensions a tenant has rows for run; others are no-ops).
        # Quota stays charged to the CALLER's tenant (home) — a system_admin
        # drill-in must not consume the target tenant's download budget.
        actor_id: str = getattr(request.state, "actor_id", "anonymous")
        denial = await check_admission(
            quota=quota,
            audit=audit,
            tenant_id=request.state.tenant_id,
            actor_id=actor_id,
            agent=None,
            resource_kind="artifact_download",
            cost=1,
        )
        if denial is not None:
            return denial
        if workspace_store is None:
            raise HTTPException(
                status_code=503,
                detail="artifact download unavailable: no sandbox supervisor configured",
            )
        try:
            data = await workspace_store.read_file(
                tenant_id=target_tenant,
                user_id=target_user_id,
                path=version.path_in_workspace,
            )
        except WorkspacePermissionError as exc:
            # 元数据行存在,内容读不动是权限问题(服务端配置),不是"这个 artifact
            # 不存在"——"gone / unreadable" 不能再合并成一个 404(同六处
            # workspace 端点的坑)。必须排在下面的 SandboxSupervisorError 之前
            # (它的子类,顺序反了永远走不到)。``exc_info=True`` 带全 traceback
            # (同其它八处权限归因站点的既有手法),不进响应体;detail 是固定文案。
            logger.warning("artifact.permission_denied version=%s", version.id, exc_info=True)
            raise HTTPException(status_code=500, detail="artifact content unavailable") from exc
        except SandboxSupervisorError as exc:
            # The metadata row exists but the file is gone / unreadable —
            # log the supervisor detail, keep the client response opaque.
            logger.warning("artifact.content_unavailable version=%s reason=%s", version.id, exc)
            raise HTTPException(status_code=404, detail="artifact content not found") from exc

        # Backfill the digest on first read — unknown at save_artifact time.
        if version.size_bytes is None:
            async with applied_scope(scope):
                await store.set_version_digest(
                    version_id=version.id,
                    size_bytes=len(data),
                    sha256=hashlib.sha256(data).hexdigest(),
                )
        # Mini-ADR J-25 (J.9-step3, STREAM-J-DESIGN § 10.5) — MIME +
        # XSS-safe disposition. Active content (HTML / SVG / etc.) is
        # always sent ``attachment`` regardless of how the kind / path
        # are spelled; unknown extensions fall through to
        # ``application/octet-stream`` + attachment.
        inferred = infer_content_type(kind=artifact.kind, path=version.path_in_workspace)
        headers = {
            "Content-Disposition": content_disposition_header(
                artifact.name, disposition=inferred.disposition
            ),
            "X-Content-Type-Options": "nosniff",
        }
        return Response(content=data, media_type=inferred.content_type, headers=headers)

    @router.delete("/{name:path}", response_model=None)
    async def delete_artifact(
        name: str,
        request: Request,
        store: Annotated[ArtifactStore, Depends(_get_artifact_store)],
        users: Annotated[TenantUserStore, Depends(get_user_repo)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
        # H.8-F1 — tenant-admin governance target (see download).
        user_id: Annotated[UUID | None, Query()] = None,
    ) -> JSONResponse:
        """Mini-ADR J-25 — soft-delete one artifact (metadata only).

        Hides the row from list / download / versions; the J.15 volume
        bytes remain until the retention sweep hard-deletes the row, or
        the user re-saves the same name (which un-deletes).
        """
        tenant_id: UUID = request.state.tenant_id
        target_user_id = await resolve_target_user_id(request, users, requested=user_id)
        if target_user_id is None:
            raise HTTPException(status_code=404, detail="artifact not found")
        current_user_id_var.set(target_user_id)
        hit = await store.soft_delete(
            tenant_id=tenant_id, user_id=target_user_id, name=name, now=datetime.now(UTC)
        )
        # Hides cross-user / already-deleted / unknown behind the same 404.
        if not hit:
            raise HTTPException(status_code=404, detail="artifact not found")
        actor_id: str = getattr(request.state, "actor_id", "anonymous")
        await audit_emit(
            audit,
            tenant_id=tenant_id,
            actor_id=actor_id,
            action=AuditAction.ARTIFACT_DELETE,
            resource_type="artifact",
            resource_id=name,
            result=AuditResult.SUCCESS,
            trace_id=current_trace_id_hex(),
            details={"user_id": str(target_user_id)},
        )
        return JSONResponse(status_code=200, content={"deleted": name})

    @router.patch("/{name:path}", response_model=None)
    async def patch_artifact(
        name: str,
        body: _ArtifactPatchBody,
        request: Request,
        store: Annotated[ArtifactStore, Depends(_get_artifact_store)],
        users: Annotated[TenantUserStore, Depends(get_user_repo)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
        # H.8-F1 — tenant-admin governance target (see download).
        user_id: Annotated[UUID | None, Query()] = None,
    ) -> JSONResponse:
        """Mini-ADR J-25 — update an artifact's mutable fields (M0: ``kind``).

        Returns 404 when the name is unknown / soft-deleted /
        cross-user (same hiding rule as the other endpoints). Returns
        409 when ``kind`` is unchanged so callers know the PATCH was a
        no-op and can stop retrying.
        """
        if body.kind not in _ARTIFACT_KINDS:
            # FastAPI already validated this against the ``ArtifactKind``
            # Literal — guard kept for the rare future divergence.
            raise HTTPException(status_code=422, detail="invalid kind")
        tenant_id: UUID = request.state.tenant_id
        target_user_id = await resolve_target_user_id(request, users, requested=user_id)
        if target_user_id is None:
            raise HTTPException(status_code=404, detail="artifact not found")
        current_user_id_var.set(target_user_id)
        updated = await store.update_kind(
            tenant_id=tenant_id, user_id=target_user_id, name=name, kind=body.kind
        )
        if updated is None:
            raise HTTPException(status_code=404, detail="artifact not found")
        actor_id: str = getattr(request.state, "actor_id", "anonymous")
        await audit_emit(
            audit,
            tenant_id=tenant_id,
            actor_id=actor_id,
            action=AuditAction.ARTIFACT_UPDATE,
            resource_type="artifact",
            resource_id=name,
            result=AuditResult.SUCCESS,
            trace_id=current_trace_id_hex(),
            details={"user_id": str(target_user_id), "kind": body.kind},
        )
        return JSONResponse(
            status_code=200,
            content={
                "name": updated.name,
                "kind": updated.kind,
                "latest_version": updated.latest_version,
            },
        )

    @router.get("/{name:path}/versions", response_model=None)
    async def list_versions(
        name: str,
        request: Request,
        store: Annotated[ArtifactStore, Depends(_get_artifact_store)],
        users: Annotated[TenantUserStore, Depends(get_user_repo)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
        # H.8-F1 — tenant-admin governance target (see download).
        user_id: Annotated[UUID | None, Query()] = None,
        # W2 read scope — see ``download_artifact``.
        tenant_id: Annotated[UUID | Literal["*"] | None, Query()] = None,
    ) -> JSONResponse:
        """Mini-ADR J-25 — version history for one artifact.

        Sorted newest-first; soft-deleted artifacts return 404 (same
        hiding rule). Each entry exposes the per-version metadata —
        ``size_bytes`` / ``sha256`` may still be NULL when the version
        has never been downloaded (lazy backfill).
        """
        scope = await ensure_single_tenant_scope(
            request.state.principal,
            tenant_id,
            audit,
            trace_id=current_trace_id_hex(),
            endpoint="GET /v1/artifacts/{name}/versions",
            cross_tenant_enabled=cross_tenant_query_enabled(request),
        )
        target_user_id = await resolve_target_user_id(request, users, requested=user_id)
        if target_user_id is None:
            raise HTTPException(status_code=404, detail="artifact not found")
        current_user_id_var.set(target_user_id)
        async with applied_scope(scope):
            versions = await store.list_versions(
                tenant_id=scope.tenant_id, user_id=target_user_id, name=name
            )
        if versions is None:
            raise HTTPException(status_code=404, detail="artifact not found")
        items: list[dict[str, Any]] = [
            {
                "version": v.version,
                "path_in_workspace": v.path_in_workspace,
                "size_bytes": v.size_bytes,
                "sha256": v.sha256,
                "created_in_thread": v.created_in_thread,
                "created_at": v.created_at.isoformat() if v.created_at else None,
            }
            for v in versions
        ]
        return JSONResponse(content={"name": name, "versions": items})

    return router
