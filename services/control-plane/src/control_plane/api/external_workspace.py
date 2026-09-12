"""对外工作区端点 —— ``GET /v1/agents/{agent_code}/workspace/{files,file}``。

agent 把产出物写进终端用户的持久工作区,第三方 app 得能列出来、下下来 ——
否则「agent 生成了一份报表」这件事在他们界面上就没有下文。

控制台侧 ``api/workspace.py`` 的四个端点全挂 ``console_only()``(P1 控制台平面
收口刻意锁的)。本模块是它们的对外镜像:安全处理(MIME 嗅探 / attachment +
nosniff / 路径校验 / 权限失败与不存在分开)全部复用,只把控制台的身份解析换成
P1 的 ``_external`` 通路。

工作区按 ``(tenant_id, user_id, agent)`` 分(B-50)。**这与本模块此前的
说法相反** —— 原来写的是「工作区本身是 ``(tenant_id, user_id)`` 维度的,不按
agent 分,``agent_code`` 不参与过滤」,那句话从 B-50 起不成立:同一用户的多个
agent 此前在同一棵扁平树里互相读错写错,喂给模型的事实是错的。

对外 ``path`` 相对**该 agent 的根**,不带 ``agents/<agent_key>/`` 前缀(投影见
``_external_agent_scope``):对接方缓存过的 path 继续有效,带 sha256 后缀的内部
``agent_key`` 也不漏给第三方。``shared/``(搬迁时反推不出归属的 legacy)与搬迁
前的顶层扁平残留都**不对外投影** —— 第三方按 ``agent_code`` 提问,答案里混进
「不知道谁的历史文件」没有意义,而那批正是归属不明的那批。

与控制台侧的差异:控制台 ``/v1/workspace/files`` 看**全量**(Task 13 的浏览面
按 agent 分组,``shared/`` 也看得见),对外只看本 agent —— 或 ``?scope=user``
时看该终端用户全部 agent 的并集,条目带 ``agent_code`` 标明归属。

下载端点的成功响应是文件字节流,不是 ``{success, data, error}`` 信封 ——
信封只包裹错误响应(与「文件不是 JSON」这个事实本身冲突,业界惯例 + P2-a
设计文档都是这么处理二进制下载的)。

**不镜像 DELETE** —— 破坏性操作,第三方缺上下文,需单独拍板。
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response

from control_plane.api._authz import external_only, require
from control_plane.api._external import (
    ExternalScopeError,
    external_error,
    lookup_external_user_id,
    reject_nul_path_params,
)
from control_plane.api._external_agent_scope import (
    ExternalScope,
    agent_code_by_key,
    agent_key_for_code,
    external_storage_path,
    storage_to_external,
)
from control_plane.api._user_scope import get_user_repo
from control_plane.api._workspace_shared import _workspace_file_response, list_workspace_entries
from expert_work.persistence.tenant_user import TenantUserStore
from expert_work.persistence.thread_meta import ThreadMetaStore
from orchestrator.tools import WorkspaceStore


def _get_workspace_store(request: Request) -> WorkspaceStore | None:
    return request.app.state.workspace_store  # type: ignore[no-any-return]


def _get_thread_repo(request: Request) -> ThreadMetaStore:
    return request.app.state.thread_meta_repo  # type: ignore[no-any-return]


def build_external_workspace_router() -> APIRouter:
    """Mount the external workspace endpoints."""
    router = APIRouter(
        prefix="/v1/agents",
        tags=["external"],
        dependencies=[Depends(reject_nul_path_params), Depends(external_only())],
    )

    @router.get(
        "/{agent_code}/workspace/files",
        response_model=None,
        dependencies=[Depends(require("session", "read"))],
    )
    async def list_workspace_files(
        agent_code: str,
        request: Request,
        users: Annotated[TenantUserStore, Depends(get_user_repo)],
        threads: Annotated[ThreadMetaStore, Depends(_get_thread_repo)],
        workspace_store: Annotated[WorkspaceStore | None, Depends(_get_workspace_store)],
        user_id: Annotated[str, Query(min_length=1, max_length=255)],
        scope: Annotated[ExternalScope, Query()] = "agent",
    ) -> JSONResponse:
        """Browse the files in an end-user's persistent workspace volume.

        ``mint=False`` — a read must never mint a ``tenant_user`` row for a
        ``user_id`` this tenant has never seen (External-API-v1 P1 review,
        T3): a third party spraying arbitrary ``user_id``s at this endpoint
        must not leave one ghost row per attempt. An unrecognized user simply
        has no files, so it returns an empty list, not 404 — same as
        ``GET .../sessions``.

        Every entry carries ``agent_code`` (B-50 PR4). Under the default
        ``scope=agent`` that is a constant echo of the path segment; under
        ``scope=user`` it is the only way the caller can tell two same-named
        files apart **and** the only way to download either, because the
        download endpoint deliberately does not honour ``scope``. One
        response shape for both scopes: a field that appears only sometimes
        is harder to document and harder to consume than one that is always
        there, and adding a field is backward-compatible.
        """
        tenant_id: UUID = request.state.tenant_id
        try:
            end_user_id = await lookup_external_user_id(
                tenant_id=tenant_id, user_id=user_id, users=users
            )
        except ExternalScopeError as exc:
            return external_error(exc)
        if end_user_id is None:
            return JSONResponse({"success": True, "data": {"files": []}, "error": None})
        if scope == "user":
            # 反查表只在 scope=user 时才建 —— 默认路径不该为一个用不到的
            # 映射多查一次库。
            codes = await threads.list_agent_names_for_user(
                tenant_id=tenant_id, user_id=end_user_id
            )
            by_key = agent_code_by_key(codes)
        else:
            by_key = {agent_key_for_code(agent_code): agent_code}
        try:
            entries = await list_workspace_entries(
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
        files: list[dict[str, object]] = []
        for entry in entries:
            for key, code in by_key.items():
                external = storage_to_external(entry.path, agent_key=key)
                if external is not None:
                    files.append({"path": external, "size": entry.size, "agent_code": code})
                    break
        return JSONResponse({"success": True, "data": {"files": files}, "error": None})

    @router.get(
        "/{agent_code}/workspace/file",
        response_model=None,
        dependencies=[Depends(require("session", "read"))],
    )
    async def download_workspace_file(
        agent_code: str,
        request: Request,
        users: Annotated[TenantUserStore, Depends(get_user_repo)],
        workspace_store: Annotated[WorkspaceStore | None, Depends(_get_workspace_store)],
        user_id: Annotated[str, Query(min_length=1, max_length=255)],
        # External-API-v1 P2-b — unlike ``user_id`` (255) / the session
        # ``title`` (200), this had no upper bound at all: a 30004-character
        # ``path`` round-tripped as a 200 straight through to the supervisor
        # (terminal-review finding). 4096 matches POSIX ``PATH_MAX`` — the
        # longest a real filesystem path can be — so every legitimate
        # workspace path fits and an oversized one 422s before reaching
        # ``_safe_workspace_relpath`` / the supervisor at all. The NUL check
        # itself is NOT duplicated here — ``_safe_workspace_relpath``
        # (``_workspace_shared.py``) already rejects an embedded ``\x00``
        # (added the prior P2-b task), so this only adds the missing length
        # bound.
        path: Annotated[str, Query(max_length=4096)],
    ) -> Response:
        """Download one file from an end-user's persistent workspace volume.

        ``mint=False`` — same rationale as ``.../workspace/files`` above: a
        read must never mint a ``tenant_user`` row for a ``user_id`` this
        tenant has never seen.

        404 hides cross-user (an unrecognized ``user_id``) / missing-file /
        no-supervisor behind one opaque response — a third party must not be
        able to tell "that user doesn't exist" apart from "that user exists
        but has no such file" apart from "the sandbox supervisor isn't
        configured". B-50 PR4 folds **cross-agent** into that same 404: ``path``
        is resolved under this ``agent_code``'s own root, so another agent's
        file is simply not there. The success response is the raw file body,
        not the ``{success, data, error}`` envelope (see module docstring);
        only the error path renders that envelope.

        **No ``scope`` parameter here, on purpose.** The list endpoints take
        one; letting a download take it would mean agent A's code fetches
        agent B's bytes — exactly what this design exists to prevent. To
        download another agent's file, use that agent's code; the
        ``scope=user`` listing already names the owner.
        """
        tenant_id: UUID = request.state.tenant_id
        try:
            end_user_id = await lookup_external_user_id(
                tenant_id=tenant_id, user_id=user_id, users=users
            )
        except ExternalScopeError as exc:
            return external_error(exc)
        try:
            # 校验 → 投影,顺序锁在 ``external_storage_path`` 里(见那个函数的
            # docstring:反过来 ``..`` 会被前缀掩盖成一条看起来合法的路径)。
            storage_path = external_storage_path(path, agent_key=agent_key_for_code(agent_code))
            return await _workspace_file_response(
                workspace_store, tenant_id=tenant_id, user_id=end_user_id, path=storage_path
            )
        except HTTPException as exc:
            return JSONResponse(
                status_code=exc.status_code,
                content={
                    "success": False,
                    "data": None,
                    "error": {"code": "WORKSPACE_FILE_FAILED", "message": str(exc.detail)},
                },
            )

    return router
