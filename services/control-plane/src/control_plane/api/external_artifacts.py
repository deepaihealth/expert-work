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

import hashlib
import logging
from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse, Response

from control_plane.api._artifact_mime import content_disposition_header, infer_content_type
from control_plane.api._authz import external_only, require
from control_plane.api._external import (
    ExternalScopeError,
    external_error,
    lookup_external_user_id,
    reject_nul,
    reject_nul_path_params,
)
from control_plane.api._quota_admission import check_admission
from control_plane.api._user_scope import get_user_repo
from control_plane.audit import emit as audit_emit
from control_plane.quota.base import QuotaService
from expert_work.common.observability import current_trace_id_hex
from expert_work.persistence import ArtifactStore
from expert_work.persistence.tenant_user import TenantUserStore
from expert_work.protocol import AuditAction
from expert_work.runtime.audit.logger import AuditLogger
from orchestrator.tools import (
    SandboxSupervisorError,
    WorkspaceFileTooLargeError,
    WorkspacePermissionError,
    WorkspaceStore,
)

logger = logging.getLogger("expert_work.control_plane.external_artifacts")


def _get_artifact_store(request: Request) -> ArtifactStore:
    return request.app.state.artifact_store  # type: ignore[no-any-return]


def _get_workspace_store(request: Request) -> WorkspaceStore | None:
    return request.app.state.workspace_store  # type: ignore[no-any-return]


def _get_quota(request: Request) -> QuotaService:
    return request.app.state.quota_service  # type: ignore[no-any-return]


def _get_audit(request: Request) -> AuditLogger:
    return request.app.state.audit_logger  # type: ignore[no-any-return]


def _name_or_422(name: str) -> str:
    """``reject_nul`` for the ``name`` query param, translated into the same
    envelope shape ``user_id``'s NUL check already produces.

    Unlike ``user_id``, ``name`` is a raw ``Query(...)`` parameter — not a
    pydantic body field with a ``field_validator`` to attach to — so this
    calls :func:`reject_nul` directly and translates its ``ValueError``
    itself, the same calling convention ``_external.py``'s own
    ``_external_subject_id_or_422`` documents and uses for ``user_id``.
    Without this, a NUL byte in ``name`` reaches ``ArtifactRow.name == name``
    (a ``text``-column bind parameter in both ``get_latest_version`` and
    ``soft_delete``, ``persistence/artifact/sql.py``) and asyncpg raises
    ``CharacterNotInRepertoireError`` — uncaught, so it escapes as
    Starlette's bare-text 500, breaking the external plane's envelope
    contract (终审 C1).
    """
    try:
        return reject_nul(name, field="name")
    except ValueError as exc:
        raise ExternalScopeError("INVALID_ARTIFACT_NAME", str(exc), 422) from exc


def _artifact_error(code: str, message: str, status: int) -> JSONResponse:
    """错误路径的 ``{success, data, error}`` 信封。

    成功路径**不走这里** —— 下载的成功响应是裸文件字节流(信封与「文件不是
    JSON」这个事实冲突,同 ``workspace/file``)。
    """
    return JSONResponse(
        status_code=status,
        content={"success": False, "data": None, "error": {"code": code, "message": message}},
    )


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

    @router.get(
        "/{agent_code}/artifacts/download",
        response_model=None,
        dependencies=[Depends(require("session", "read"))],
    )
    async def download_artifact(
        agent_code: str,
        request: Request,
        store: Annotated[ArtifactStore, Depends(_get_artifact_store)],
        users: Annotated[TenantUserStore, Depends(get_user_repo)],
        workspace_store: Annotated[WorkspaceStore | None, Depends(_get_workspace_store)],
        quota: Annotated[QuotaService, Depends(_get_quota)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
        user_id: Annotated[str, Query(min_length=1, max_length=255)],
        name: Annotated[str, Query(min_length=1, max_length=512)],
        version: Annotated[int | None, Query(ge=1)] = None,
    ) -> Response:
        """Download the latest version of one artifact.

        Success is the raw file body, not the ``{success, data, error}``
        envelope (see module docstring); only error paths render it.

        ``mint=False`` — same rationale as the list endpoint. An
        unrecognized ``user_id`` falls through to the same opaque 404 as a
        cross-user / unknown name: a third party must not be able to tell
        "that user doesn't exist" apart from "that user has no such
        artifact".
        """
        del agent_code  # artifacts are (tenant, user)-scoped — see module docstring.
        tenant_id: UUID = request.state.tenant_id
        try:
            name = _name_or_422(name)
            end_user_id = await lookup_external_user_id(
                tenant_id=tenant_id, user_id=user_id, users=users
            )
        except ExternalScopeError as exc:
            return external_error(exc)
        if end_user_id is None:
            return _artifact_error("ARTIFACT_NOT_FOUND", "artifact not found", 404)
        latest = await store.get_latest_version(tenant_id=tenant_id, user_id=end_user_id, name=name)
        if latest is None:
            return _artifact_error("ARTIFACT_NOT_FOUND", "artifact not found", 404)
        # 产物清单契约 —— 可选 ``version`` 是**校验闸**不是取历史:旧版本
        # 字节被同名重登记覆盖后物理上已不存在(版本行只是元数据),所以这里
        # 不提供按版本取内容;传了 version 且 ≠ 最新版 → 409 显式冲突,
        # 保证「拿着旧清单迟到收割」永远不会静默拿到别的内容。
        if version is not None and version != latest.version:
            return _artifact_error(
                "ARTIFACT_VERSION_MISMATCH",
                f"artifact {name!r} is at version {latest.version}, not {version}; "
                "content of superseded versions is not retained",
                409,
            )
        version = latest
        artifacts = await store.list_for_user(tenant_id=tenant_id, user_id=end_user_id)
        artifact = next((a for a in artifacts if a.name == name), None)
        if artifact is None:
            # Defensive — a version without its parent row violates a store
            # invariant; stay opaque rather than 500.
            return _artifact_error("ARTIFACT_NOT_FOUND", "artifact not found", 404)
        # 配额准入 —— 第三方比员工更需要这道限制。cost=1 扣 QPS +
        # ARTIFACT_DOWNLOAD_COUNT_30D(租户没有对应维度行时是 no-op)。
        actor_id: str = getattr(request.state, "actor_id", "anonymous")
        denial = await check_admission(
            quota=quota,
            audit=audit,
            tenant_id=tenant_id,
            actor_id=actor_id,
            agent=None,
            resource_kind="artifact_download",
            cost=1,
        )
        if denial is not None:
            return denial
        if workspace_store is None:
            return _artifact_error(
                "ARTIFACT_CONTENT_UNAVAILABLE", "artifact content unavailable", 503
            )
        try:
            data = await workspace_store.read_file(
                tenant_id=tenant_id, user_id=end_user_id, path=version.path_in_workspace
            )
        except WorkspacePermissionError:
            # 元数据行在、内容读不动是权限问题(服务端配置),不是「不存在」——
            # 两者不能合并成一个 404(沙箱迁移 W2-BUG-1 的教训)。这个 except
            # **必须排在** SandboxSupervisorError 之前:它是后者的子类,顺序
            # 反了永远走不到。traceback 只进日志,不进响应体。
            logger.warning(
                "external_artifact.permission_denied version=%s", version.id, exc_info=True
            )
            return _artifact_error(
                "ARTIFACT_CONTENT_UNAVAILABLE", "artifact content unavailable", 500
            )
        except WorkspaceFileTooLargeError as exc:
            # 「太大」≠「不存在」—— 产物列在 GET /artifacts 里,只是超过单文件
            # 下载闸;折进 404 会让对接方以为产物丢了。子类,必须排在宽
            # except 之前。
            logger.warning("external_artifact.too_large version=%s reason=%s", version.id, exc)
            return _artifact_error(
                "ARTIFACT_TOO_LARGE", "artifact exceeds the download size limit", 413
            )
        except SandboxSupervisorError as exc:
            logger.warning(
                "external_artifact.content_unavailable version=%s reason=%s", version.id, exc
            )
            return _artifact_error("ARTIFACT_NOT_FOUND", "artifact content not found", 404)
        # 首次读回填摘要 —— save 时读不到内容(它在工作区卷里),所以那时未知。
        if version.size_bytes is None:
            await store.set_version_digest(
                version_id=version.id,
                size_bytes=len(data),
                sha256=hashlib.sha256(data).hexdigest(),
            )
        # MIME 推断 + XSS 安全 disposition:可执行内容(HTML / SVG 等)一律
        # attachment,未识别扩展名回退 application/octet-stream + attachment。
        inferred = infer_content_type(kind=artifact.kind, path=version.path_in_workspace)
        headers = {
            "Content-Disposition": content_disposition_header(
                artifact.name, disposition=inferred.disposition
            ),
            "X-Content-Type-Options": "nosniff",
        }
        return Response(content=data, media_type=inferred.content_type, headers=headers)

    @router.delete(
        "/{agent_code}/artifacts",
        response_model=None,
        dependencies=[Depends(require("session", "write"))],
    )
    async def delete_artifact(
        agent_code: str,
        request: Request,
        store: Annotated[ArtifactStore, Depends(_get_artifact_store)],
        users: Annotated[TenantUserStore, Depends(get_user_repo)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
        user_id: Annotated[str, Query(min_length=1, max_length=255)],
        name: Annotated[str, Query(min_length=1, max_length=512)],
    ) -> JSONResponse:
        """Soft-delete one artifact (metadata only).

        ``require("session", "write")`` — **not** ``"delete"``:
        ``ApiKeyScope`` has no standalone delete tier, so gating on it would
        mean only an ``admin``-scope key could delete, forcing a third party
        to hold a key that can also rewrite service accounts just to remove
        its own file. Same ruling as ``archive_session``.

        The workspace bytes are untouched — the retention sweep hard-deletes
        later, and an agent re-saving the same name un-deletes the row.

        Unknown / already-deleted / cross-user all collapse to one 404 so the
        response never reveals whether the name exists.
        """
        del agent_code  # artifacts are (tenant, user)-scoped — see module docstring.
        tenant_id: UUID = request.state.tenant_id
        try:
            name = _name_or_422(name)
            end_user_id = await lookup_external_user_id(
                tenant_id=tenant_id, user_id=user_id, users=users
            )
        except ExternalScopeError as exc:
            return external_error(exc)
        if end_user_id is None:
            return _artifact_error("ARTIFACT_NOT_FOUND", "artifact not found", 404)
        hit = await store.soft_delete(
            tenant_id=tenant_id, user_id=end_user_id, name=name, now=datetime.now(UTC)
        )
        if not hit:
            return _artifact_error("ARTIFACT_NOT_FOUND", "artifact not found", 404)
        await audit_emit(
            audit,
            tenant_id=tenant_id,
            actor_id=request.state.actor_id,
            action=AuditAction.ARTIFACT_DELETE,
            resource_type="artifact",
            resource_id=name,
            trace_id=current_trace_id_hex(),
            details={"op": "soft_delete"},
            on_behalf_of=str(end_user_id),
        )
        return JSONResponse({"success": True, "data": {"deleted": name}, "error": None})

    return router
