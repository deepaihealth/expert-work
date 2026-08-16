"""对外产物端点 —— ``/v1/agents/{agent_code}/artifacts``(阶段 3 PR-B)。

agent 把产出物(报表、导出文件)登记成 artifact,第三方 app 得能列出来、
下下来、删掉 —— 否则「agent 生成了一份周报」这件事在他们界面上就没有下文。

控制台侧 ``api/artifacts.py`` 的五个端点全挂 ``console_only()``。本模块是
其中三个(list / download / soft-delete)的对外镜像:安全处理(MIME 推断 /
active content 强制 attachment / nosniff / 权限失败与不存在分开)全部复用,
只把控制台的身份解析(跨租户 scope + 管理员代操 ``resolve_target_user_id``)
换成 P1 的 ``_external`` 通路。

产物本身是 ``(tenant_id, user_id)`` 维度的,不按 agent 分 —— ``agent_code``
只是外部平面 URL 结构的一部分(与 ``/v1/agents/{agent_code}/sessions`` 等同款
路径形状对齐),**不参与过滤,也不参与权限判定**,和控制台侧 ``/v1/artifacts``
(压根没有 agent_code)语义一致。

``name`` 走 query 而非 path:控制台侧是 ``{name:path}``,对外用 query 参数,
避免产物名含 ``/`` 时的路径穿越与编码歧义。DELETE 的 ``user_id`` / ``name``
同样在 query —— 与同资源的 GET 保持一致(同资源两个写操作参数位置不一致会
坑对接方,session 的 PATCH/DELETE 已经踩过)。

**不镜像**版本历史(``/versions``)、改 ``kind``(PATCH)、硬删(``:purge``)
—— 前两者对第三方界面价值低,硬删与会话侧一致,永远是 console-only。
"""

from __future__ import annotations

import logging
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse

from control_plane.api._authz import external_only, require
from control_plane.api._external import (
    ExternalScopeError,
    external_error,
    lookup_external_user_id,
    reject_nul_path_params,
)
from control_plane.api._user_scope import get_user_repo
from expert_work.persistence import ArtifactStore
from expert_work.persistence.tenant_user import TenantUserStore
from orchestrator.tools import WorkspaceStore

logger = logging.getLogger("expert_work.control_plane.external_artifacts")


def _get_artifact_store(request: Request) -> ArtifactStore:
    return request.app.state.artifact_store  # type: ignore[no-any-return]


def _get_workspace_store(request: Request) -> WorkspaceStore | None:
    return request.app.state.workspace_store  # type: ignore[no-any-return]


def build_external_artifacts_router() -> APIRouter:
    """Mount the external artifact endpoints."""
    router = APIRouter(
        prefix="/v1/agents",
        tags=["external"],
        dependencies=[Depends(reject_nul_path_params), Depends(external_only())],
    )

    @router.get(
        "/{agent_code}/artifacts",
        response_model=None,
        dependencies=[Depends(require("session", "read"))],
    )
    async def list_artifacts(
        agent_code: str,
        request: Request,
        store: Annotated[ArtifactStore, Depends(_get_artifact_store)],
        users: Annotated[TenantUserStore, Depends(get_user_repo)],
        user_id: Annotated[str, Query(min_length=1, max_length=255)],
    ) -> JSONResponse:
        """List an end-user's agent artifacts, most-recently-updated first.

        ``mint=False`` — a read must never mint a ``tenant_user`` row for a
        ``user_id`` this tenant has never seen (External-API-v1 P1 review,
        T3). An unrecognized user simply has no artifacts, so this returns
        an empty list, not 404 — same as ``GET .../workspace/files``.

        No ``size_bytes``: ``list_for_user`` returns the logical rows, not
        version detail — adding it would need a per-row latest-version
        lookup (an N+1), and the digest is only backfilled on first
        download, so most rows would carry ``null`` anyway.
        """
        del agent_code  # artifacts are (tenant, user)-scoped — see module docstring.
        tenant_id: UUID = request.state.tenant_id
        try:
            end_user_id = await lookup_external_user_id(
                tenant_id=tenant_id, user_id=user_id, users=users
            )
        except ExternalScopeError as exc:
            return external_error(exc)
        if end_user_id is None:
            return JSONResponse({"success": True, "data": {"artifacts": []}, "error": None})
        artifacts = await store.list_for_user(tenant_id=tenant_id, user_id=end_user_id)
        items = [
            {
                "name": a.name,
                "kind": a.kind,
                "latest_version": a.latest_version,
                "created_at": a.created_at.isoformat() if a.created_at else None,
                "updated_at": a.updated_at.isoformat() if a.updated_at else None,
            }
            for a in artifacts
        ]
        return JSONResponse({"success": True, "data": {"artifacts": items}, "error": None})

    return router
