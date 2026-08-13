"""对外工作区端点 —— ``GET /v1/agents/{agent_code}/workspace/{files,file}``。

agent 把产出物写进终端用户的持久工作区,第三方 app 得能列出来、下下来 ——
否则「agent 生成了一份报表」这件事在他们界面上就没有下文。

控制台侧 ``api/workspace.py`` 的四个端点全挂 ``console_only()``(P1 控制台平面
收口刻意锁的)。本模块是它们的对外镜像:安全处理(MIME 嗅探 / attachment +
nosniff / 路径校验 / 权限失败与不存在分开)全部复用,只把控制台的身份解析换成
P1 的 ``_external`` 通路。

工作区本身是 ``(tenant_id, user_id)`` 维度的,不按 agent 分——``agent_code``
只是外部平面 URL 结构的一部分(与 ``/v1/agents/{agent_code}/sessions`` 等同款
路径形状对齐),不参与过滤,和控制台侧 ``/v1/workspace/files``(压根没有
agent_code)语义一致。

**不镜像 DELETE** —— 破坏性操作,第三方缺上下文,需单独拍板。
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from control_plane.api._authz import require
from control_plane.api._external import (
    ExternalScopeError,
    external_error,
    lookup_external_user_id,
)
from control_plane.api._user_scope import get_user_repo
from control_plane.api._workspace_shared import _workspace_files_payload
from expert_work.persistence.tenant_user import TenantUserStore
from orchestrator.tools import WorkspaceStore


def _get_workspace_store(request: Request) -> WorkspaceStore | None:
    return request.app.state.workspace_store  # type: ignore[no-any-return]


def build_external_workspace_router() -> APIRouter:
    """Mount the external workspace endpoints."""
    router = APIRouter(prefix="/v1/agents", tags=["external"])

    @router.get(
        "/{agent_code}/workspace/files",
        response_model=None,
        dependencies=[Depends(require("session", "read"))],
    )
    async def list_workspace_files(
        agent_code: str,
        request: Request,
        users: Annotated[TenantUserStore, Depends(get_user_repo)],
        workspace_store: Annotated[WorkspaceStore | None, Depends(_get_workspace_store)],
        user_id: Annotated[str, Query(min_length=1, max_length=255)],
    ) -> JSONResponse:
        """Browse the files in an end-user's persistent workspace volume.

        ``mint=False`` — a read must never mint a ``tenant_user`` row for a
        ``user_id`` this tenant has never seen (External-API-v1 P1 review,
        T3): a third party spraying arbitrary ``user_id``s at this endpoint
        must not leave one ghost row per attempt. An unrecognized user simply
        has no files, so it returns an empty list, not 404 — same as
        ``GET .../sessions``.
        """
        del agent_code  # workspace is (tenant, user)-scoped, not per-agent — see module docstring.
        tenant_id: UUID = request.state.tenant_id
        try:
            end_user_id = await lookup_external_user_id(
                tenant_id=tenant_id, user_id=user_id, users=users
            )
        except ExternalScopeError as exc:
            return external_error(exc)
        if end_user_id is None:
            return JSONResponse({"success": True, "data": {"files": []}, "error": None})
        try:
            payload = await _workspace_files_payload(
                workspace_store, tenant_id=tenant_id, user_id=end_user_id
            )
        except HTTPException as exc:
            return JSONResponse(
                status_code=exc.status_code,
                content={
                    "success": False,
                    "data": None,
                    "error": {"code": "WORKSPACE_LIST_FAILED", "message": str(exc.detail)},
                },
            )
        return JSONResponse({"success": True, "data": payload, "error": None})

    return router
