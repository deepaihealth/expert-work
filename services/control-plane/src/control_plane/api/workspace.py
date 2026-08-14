"""``/v1/workspace`` — user-scoped persistent workspace browse / download / delete.

The playground workspace inspector (and the M2 user-detail Workspace tab) read a
user's persistent volume directly, independent of any thread. The thread-scoped
``/v1/sessions/{id}/workspace*`` routes 404 once the thread is archived / purged
— even though the ``(tenant, user)``-keyed volume lives on — so a user could no
longer see their own files. These endpoints key on the *user* instead, so the
workspace stays reachable across (and after) every session.

Scope mirrors ``/v1/artifacts`` (Mini-ADR H.8-F1): :func:`resolve_target_user_id`
resolves the caller's own ``tenant_user.id``, or — for a tenant admin — the
``?user_id=`` target; anyone else asking for someone else gets a 403. A machine
principal owns no per-user workspace.
"""

from __future__ import annotations

import logging
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response

from control_plane.api._authz import console_only
from control_plane.api._user_scope import (
    get_user_repo,
    resolve_caller_user_id,
    resolve_target_user_id,
)
from control_plane.api._workspace_shared import (
    _safe_workspace_relpath,
    _workspace_file_response,
    _workspace_files_payload,
)
from control_plane.audit import emit
from control_plane.tenant_scope import (
    applied_scope,
    cross_tenant_query_enabled,
    ensure_single_tenant_scope,
)
from expert_work.common.observability import current_trace_id_hex
from expert_work.persistence.artifact import ArtifactStore
from expert_work.persistence.rls import current_user_id_var
from expert_work.persistence.tenant_user import TenantUserStore
from expert_work.persistence.workspace import UserWorkspaceStore
from expert_work.protocol import DEFAULT_WORKSPACE_BYTES_PER_USER, AuditAction
from expert_work.runtime.audit.logger import AuditLogger
from orchestrator.tools import SandboxSupervisorError, WorkspacePermissionError, WorkspaceStore

logger = logging.getLogger("expert_work.control_plane.workspace")


def _get_workspace_store(request: Request) -> UserWorkspaceStore:
    return request.app.state.user_workspace_store  # type: ignore[no-any-return]


def _get_artifact_store(request: Request) -> ArtifactStore:
    return request.app.state.artifact_store  # type: ignore[no-any-return]


def _get_workspace_file_store(request: Request) -> WorkspaceStore | None:
    """波 1 Task 4 —— 工作区**文件**操作的客户端。

    与上面的 ``_get_workspace_store`` 区分开:那个返回
    ``UserWorkspaceStore``(工作区元数据表,配额/软删状态),这个返回
    ``WorkspaceStore``(文件读写)。两者名字撞车过一次,别再合并。
    """
    return request.app.state.workspace_store  # type: ignore[no-any-return]


def _get_audit(request: Request) -> AuditLogger:
    return request.app.state.audit_logger  # type: ignore[no-any-return]


def build_workspace_router() -> APIRouter:
    router = APIRouter(prefix="/v1/workspace", tags=["workspace"])

    @router.get("", dependencies=[Depends(console_only())])
    async def get_workspace(
        request: Request,
        users: Annotated[TenantUserStore, Depends(get_user_repo)],
        workspaces: Annotated[UserWorkspaceStore, Depends(_get_workspace_store)],
        artifacts: Annotated[ArtifactStore, Depends(_get_artifact_store)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
        # Tenant-admin governance target (the user-detail Workspace tab); a
        # non-admin asking for someone else is a 403. Omitted → the caller.
        user_id: Annotated[UUID | None, Query()] = None,
        # W3 read scope — a concrete id lets a system_admin drill into a
        # foreign tenant user's workspace; "*" is meaningless here.
        tenant_id: Annotated[UUID | Literal["*"] | None, Query()] = None,
    ) -> JSONResponse:
        """The target user's persistent workspace + artifacts.

        Read-only: ``workspaces.get`` never provisions a row, so a ``null``
        workspace truthfully means "no VM has ever started for this user".
        """
        scope = await ensure_single_tenant_scope(
            request.state.principal,
            tenant_id,
            audit,
            trace_id=current_trace_id_hex(),
            endpoint="GET /v1/workspace",
            cross_tenant_enabled=cross_tenant_query_enabled(request),
        )
        target_tenant = scope.tenant_id
        # Task 7 — the effective per-user byte limit (PR-1 WorkspaceQuotaService),
        # never the frozen ``user_workspace.size_limit_bytes`` column (that one
        # is the supervisor's, not this gate's — see workspace_quota.py:20-21).
        quota_service = getattr(request.app.state, "workspace_quota_service", None)
        if quota_service is not None:
            limit_bytes = await quota_service.effective_limit(tenant_id=target_tenant)
        else:
            # 本地 compose / 测试形态没有组装 quota gate —— 上限即平台默认。
            limit_bytes = DEFAULT_WORKSPACE_BYTES_PER_USER
        # Caller-identity resolution stays OUTSIDE applied_scope — it reads /
        # upserts the CALLER's registry row in their home tenant.
        caller_user_id = await resolve_caller_user_id(request, users)
        target_user_id = await resolve_target_user_id(request, users, requested=user_id)
        if target_user_id is None:
            # Machine principal — owns no per-user workspace.
            return JSONResponse(
                {
                    "success": True,
                    "data": {"workspace": None, "artifacts": [], "limit_bytes": limit_bytes},
                }
            )
        # Defence-in-depth for the artifact read — the store already filters by
        # explicit (tenant_id, user_id), but set the RLS GUC too, mirroring
        # ``/v1/artifacts``, so a future user-level policy stays enforced.
        current_user_id_var.set(target_user_id)
        async with applied_scope(scope):
            workspace = await workspaces.get(tenant_id=target_tenant, user_id=target_user_id)
            arts = await artifacts.list_for_user(tenant_id=target_tenant, user_id=target_user_id)
        if target_user_id != caller_user_id:
            # Read auditing — an admin opened another user's workspace
            # ("who looked at whom", Phase 2 governance).
            await emit(
                audit,
                tenant_id=target_tenant,
                actor_id=getattr(request.state, "actor_id", "anonymous"),
                action=AuditAction.USER_DATA_VIEW,
                resource_type="user",
                resource_id=str(target_user_id),
                trace_id=current_trace_id_hex(),
                details={"view": "workspace"},
            )
        return JSONResponse(
            {
                "success": True,
                "data": {
                    "workspace": workspace.model_dump(mode="json") if workspace else None,
                    "artifacts": [a.model_dump(mode="json") for a in arts],
                    "limit_bytes": limit_bytes,
                },
            }
        )

    @router.get("/files", dependencies=[Depends(console_only())])
    async def list_workspace_files(
        request: Request,
        users: Annotated[TenantUserStore, Depends(get_user_repo)],
        workspace_store: Annotated[WorkspaceStore | None, Depends(_get_workspace_file_store)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
        user_id: Annotated[UUID | None, Query()] = None,
        # W3 read scope — concrete tenant only (single-tenant semantics).
        tenant_id: Annotated[UUID | Literal["*"] | None, Query()] = None,
    ) -> JSONResponse:
        """Browse the files in the target user's persistent volume.

        Read-only inventory for the inspector. A machine principal, an absent
        supervisor, or an empty volume all return ``[]``. No store call is made
        here — file isolation is delegated to the supervisor, which resolves
        the volume strictly by ``(tenant_id, user_id)``.
        """
        scope = await ensure_single_tenant_scope(
            request.state.principal,
            tenant_id,
            audit,
            trace_id=current_trace_id_hex(),
            endpoint="GET /v1/workspace/files",
            cross_tenant_enabled=cross_tenant_query_enabled(request),
        )
        # Caller-identity resolution stays OUTSIDE applied_scope.
        target_user_id = await resolve_target_user_id(request, users, requested=user_id)
        if target_user_id is None:
            # Machine principal — owns no per-user workspace.
            return JSONResponse({"success": True, "data": {"files": []}})
        payload = await _workspace_files_payload(
            workspace_store, tenant_id=scope.tenant_id, user_id=target_user_id
        )
        return JSONResponse({"success": True, "data": payload})

    @router.get("/file", response_model=None, dependencies=[Depends(console_only())])
    async def download_workspace_file(
        request: Request,
        users: Annotated[TenantUserStore, Depends(get_user_repo)],
        workspace_store: Annotated[WorkspaceStore | None, Depends(_get_workspace_file_store)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
        path: Annotated[str, Query()],
        user_id: Annotated[UUID | None, Query()] = None,
        # W3 read scope — concrete tenant only (single-tenant semantics).
        tenant_id: Annotated[UUID | Literal["*"] | None, Query()] = None,
    ) -> Response:
        """Download one file from the target user's persistent workspace volume.

        MIME-aware + XSS-safe (active content always ``attachment`` +
        ``nosniff``). ``path`` is validated here and again at the supervisor
        boundary. 404 hides cross-user / missing-file / no-supervisor behind one
        opaque response. No store call is made here — file isolation is
        delegated to the supervisor, which resolves the volume strictly by
        ``(tenant_id, user_id)``.

        The security-relevant plumbing (path validation → read → permission-
        failure-vs-missing-file split → headers) lives in
        :func:`_workspace_file_response`, shared with the external plane's
        ``GET /v1/agents/{agent_code}/workspace/file`` — see
        ``api/_workspace_shared.py``.
        """
        scope = await ensure_single_tenant_scope(
            request.state.principal,
            tenant_id,
            audit,
            trace_id=current_trace_id_hex(),
            endpoint="GET /v1/workspace/file",
            cross_tenant_enabled=cross_tenant_query_enabled(request),
        )
        # Caller-identity resolution stays OUTSIDE applied_scope.
        target_user_id = await resolve_target_user_id(request, users, requested=user_id)
        return await _workspace_file_response(
            workspace_store, tenant_id=scope.tenant_id, user_id=target_user_id, path=path
        )

    @router.delete("/file", response_model=None, dependencies=[Depends(console_only())])
    async def delete_workspace_file(
        request: Request,
        users: Annotated[TenantUserStore, Depends(get_user_repo)],
        workspace_store: Annotated[WorkspaceStore | None, Depends(_get_workspace_file_store)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
        path: Annotated[str, Query()],
        user_id: Annotated[UUID | None, Query()] = None,
    ) -> JSONResponse:
        """Delete one file from the target user's persistent workspace volume.

        Same scope gate as browse/download; the supervisor refuses reserved
        prefixes (seeded machinery). 404 hides cross-user / no-supervisor; a
        missing file is an idempotent no-op. Audited (a governance surface —
        an admin can delete another user's file).
        """
        tenant_id: UUID = request.state.tenant_id
        caller_user_id = await resolve_caller_user_id(request, users)
        target_user_id = await resolve_target_user_id(request, users, requested=user_id)
        safe_path = _safe_workspace_relpath(path)
        if safe_path is None:
            raise HTTPException(status_code=400, detail="invalid workspace path")
        if target_user_id is None or workspace_store is None:
            raise HTTPException(status_code=404, detail="file not found")
        try:
            await workspace_store.delete_file(
                tenant_id=tenant_id, user_id=target_user_id, path=safe_path
            )
        except WorkspacePermissionError as exc:
            # 同上——权限失败不是"这个文件不存在",必须排在 SandboxSupervisorError
            # 之前(它的子类,顺序反了永远走不到)。
            logger.warning("workspace.delete_permission_denied", exc_info=True)
            raise HTTPException(status_code=500, detail="workspace file unavailable") from exc
        except SandboxSupervisorError as exc:
            logger.warning("workspace.delete_failed", exc_info=True)
            raise HTTPException(status_code=404, detail="file not found") from exc
        details: dict[str, object] = {"path": safe_path}
        if target_user_id != caller_user_id:
            details["target_user_id"] = str(target_user_id)
        await emit(
            audit,
            tenant_id=tenant_id,
            actor_id=getattr(request.state, "actor_id", "anonymous"),
            action=AuditAction.WORKSPACE_FILE_DELETE,
            resource_type="user_workspace",
            resource_id=str(target_user_id),
            trace_id=current_trace_id_hex(),
            details=details,
        )
        return JSONResponse({"success": True, "data": {"deleted": safe_path}})

    return router
