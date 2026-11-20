"""公开文件下载 —— ``GET /v1/public/files/{link_id}[/{file_name}]``(B-326 产物分享链接)。

交付给用户的地址带最后一段文件名(``…/files/<id>/<名字>``):下游按地址最后一段给文件起名,
拿到的就是带扩展名的正常名字。那一段只是装饰 —— 不参与查找、不参与鉴权、写什么都照样下载;
``Content-Disposition`` 仍是产物原名。

智能体用 ``share_file`` 把文件标记为交给用户,平台铸一条限时链接,出现在这一轮 run 的
产物清单里(``share_url``);对接方把它发给终端用户,点开就下载 —— **不登录、不带 API
key**。链接里那段 16 位随机串(行的 ``id``)就是唯一的凭据。所以这条路由在鉴权中间件里
豁免(``Settings.auth_exempt_path_prefixes`` 的 ``/v1/public/files``,``/v1/public`` 下别的
路径照常鉴权),挂在 ``/v1`` 下是为了让现有 ingress 照常转到 control-plane。

* 查不到 / 已过期 / 下载次数用完 / 产物已删 / 租户被停用:同一个不透明的 404,不说是
  哪一种(不让人拿链接试探「这条存在过」)。访问从不删行:过期满宽限期的行由留存作业删,
  读产物清单时从 run 行里记着的标记续新链接,不靠旧行。
* 字节按链接钉死的那一版读(``read_version_bytes``,记一次使用,B-63 的保留期照常续),
  MIME / ``Content-Disposition`` / ``nosniff`` 与对外下载端点同一套
  (:mod:`control_plane.api.external_artifacts`):HTML / SVG 这类可执行内容一律
  attachment。
* 防滥用:网关层的按 IP 限流(``RateLimitMiddleware``)对这里照常生效,且这里不认
  ``X-API-Key`` 头分桶(不鉴权的路径上它没人校验);过载保护(``BackpressureMiddleware``)
  不随鉴权豁免一起免掉;每条链接最多下 ``EXPERT_WORK_SHARE_LINK_MAX_DOWNLOADS`` 次(原子
  计次,先于租户配额:转出去的链接耗不光租户的下载配额);再按链接所属租户走一次下载配额
  (``artifact_download``,与对外下载同一个维度);每次成功下载记一条审计
  (``artifact:share_download``),IP 取自可信代理之后的真实客户端(``control_plane.client_ip``)。
* 按 id 查行是跨租户的,在 RLS 旁路作用域里做;之后的读一律收回到链接所属的租户 + 用户。
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from control_plane.api._artifact_content import VERSION_HEADER, read_version_bytes
from control_plane.api._artifact_mime import content_disposition_header, infer_content_type
from control_plane.api._quota_admission import check_admission
from control_plane.audit import emit as audit_emit
from control_plane.client_ip import client_ip
from control_plane.tenant_status import TenantStatusService
from expert_work.common.observability import current_trace_id_hex
from expert_work.persistence import ArtifactStore
from expert_work.persistence.artifact_share import (
    DEFAULT_MAX_DOWNLOADS,
    LINK_ID_LENGTH,
    ArtifactShareLink,
    ArtifactShareLinkStore,
)
from expert_work.persistence.rls import (
    bypass_rls_session,
    bypass_rls_var,
    current_tenant_id_var,
    current_user_id_var,
)
from expert_work.protocol import AuditAction
from orchestrator.tools import (
    SandboxSupervisorError,
    WorkspaceFileTooLargeError,
    WorkspacePermissionError,
)
from orchestrator.tools.share_file import PUBLIC_FILES_PATH

logger = logging.getLogger("expert_work.control_plane.public_files")

#: A real link id: :data:`LINK_ID_LENGTH` URL-safe characters. Anything else is a 404
#: without a query.
_LINK_ID_RE = re.compile(rf"^[A-Za-z0-9_-]{{{LINK_ID_LENGTH}}}$")
#: Who the audit row and the quota check name as the actor.
_PUBLIC_ACTOR = "public-share-link"


def _not_found() -> JSONResponse:
    """The one response for unknown / expired / deleted — never says which."""
    return JSONResponse(
        status_code=404,
        content={
            "success": False,
            "data": None,
            "error": {"code": "FILE_NOT_FOUND", "message": "file not found or link expired"},
        },
    )


def _unavailable(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"success": False, "data": None, "error": {"code": code, "message": message}},
    )


@contextmanager
def _link_scope(tenant_id: UUID, user_id: UUID) -> Iterator[None]:
    """Scope the reads after the lookup to the link's own tenant + user (RLS GUCs)."""
    tenant = current_tenant_id_var.set(tenant_id)
    bypass = bypass_rls_var.set(False)
    user = current_user_id_var.set(user_id)
    try:
        yield
    finally:
        current_user_id_var.reset(user)
        bypass_rls_var.reset(bypass)
        current_tenant_id_var.reset(tenant)


async def _live_link(links: ArtifactShareLinkStore, link_id: str) -> ArtifactShareLink | None:
    """The row, or ``None`` when unknown or expired. A GET never deletes anything: expired
    rows go with the retention job (a grace window after ``expires_at``)."""
    async with bypass_rls_session():
        link = await links.get_public(link_id=link_id)
    if link is None or link.is_expired(datetime.now(UTC)):
        return None
    return link


def build_public_files_router() -> APIRouter:
    """Mount ``GET /v1/public/files/{link_id}`` (no authentication)."""
    router = APIRouter(prefix=PUBLIC_FILES_PATH, tags=["public"])

    @router.get("/{link_id}/{file_name}", response_model=None)
    async def download_shared_file_named(
        link_id: str, file_name: str, request: Request
    ) -> Response:
        """The form share links are rendered in: ``…/files/<id>/<name>``. ``file_name`` is
        cosmetic — downstream senders name the download by the last URL segment — and is
        never used for lookup or authorization; any value serves the same bytes."""
        del file_name
        return await download_shared_file(link_id, request)

    @router.get("/{link_id}", response_model=None)
    async def download_shared_file(link_id: str, request: Request) -> Response:
        """Download the artifact version a share link points at."""
        if not _LINK_ID_RE.match(link_id):
            return _not_found()
        state = request.app.state
        links: ArtifactShareLinkStore = state.artifact_share_link_store
        link = await _live_link(links, link_id)
        if link is None:
            return _not_found()
        tenant_status: TenantStatusService | None = getattr(state, "tenant_status_service", None)
        if tenant_status is not None and await tenant_status.is_suspended(link.tenant_id):
            return _not_found()
        # M-e —— per-link download cap, counted atomically before the tenant quota is
        # charged: a forwarded link cannot drain the tenant's ``artifact_download``
        # quota. A used-up link answers the same opaque 404 as an expired one — waiting
        # never helps (unlike a 429), and it reveals nothing about the link.
        max_downloads: int = getattr(state, "share_link_max_downloads", DEFAULT_MAX_DOWNLOADS)
        async with bypass_rls_session():
            claimed = await links.claim_download(link_id=link.id, max_downloads=max_downloads)
        if not claimed:
            logger.info("public_file.download_cap_reached link_id_prefix=%s", link.id[:4])
            return _not_found()
        served = False
        try:
            with _link_scope(link.tenant_id, link.user_id):
                response = await _serve(request, link)
            served = response.status_code == 200
            return response
        finally:
            if not served:
                # L-f —— the claim counts downloads, not attempts: a quota 429, a read
                # failure or a vanished artifact gives the count back.
                async with bypass_rls_session():
                    await links.refund_download(link_id=link.id)

    return router


async def _serve(request: Request, link: ArtifactShareLink) -> Response:
    state = request.app.state
    # Quota first: an anonymous caller holding a live link pays before any artifact
    # read (charged per attempt on a live link — ROADMAP B-326 follow-up L8).
    denial = await check_admission(
        quota=state.quota_service,
        audit=state.audit_logger,
        tenant_id=link.tenant_id,
        actor_id=_PUBLIC_ACTOR,
        agent=None,
        resource_kind="artifact_download",
        cost=1,
    )
    if denial is not None:
        return denial
    artifacts: ArtifactStore = state.artifact_store
    # By id, not by listing the user's artifacts: hidden when soft-deleted, and it
    # must still belong to the agent that minted the link.
    artifact = await artifacts.get_by_id(
        tenant_id=link.tenant_id, user_id=link.user_id, artifact_id=link.artifact_id
    )
    if artifact is None or artifact.agent_key != link.agent_key:
        return _not_found()
    version = await artifacts.get_version_by_id(
        tenant_id=link.tenant_id,
        user_id=link.user_id,
        artifact_id=link.artifact_id,
        version=link.version,
    )
    # Pinned to the exact row: a version number re-used after a purge is not this link.
    if version is None or version.id != link.artifact_version_id:
        return _not_found()
    workspace_store = state.workspace_store
    if workspace_store is None:
        return _unavailable(503, "FILE_UNAVAILABLE", "file temporarily unavailable")
    try:
        data = await read_version_bytes(
            version,
            tenant_id=link.tenant_id,
            user_id=link.user_id,
            snapshots=getattr(state, "artifact_snapshots", None),
            workspace_store=workspace_store,
            artifacts=artifacts,
            record_use=True,
        )
    except WorkspacePermissionError:
        # Subclass of SandboxSupervisorError — must come first (see external_artifacts).
        logger.warning("public_file.permission_denied version=%s", version.id, exc_info=True)
        return _unavailable(500, "FILE_UNAVAILABLE", "file temporarily unavailable")
    except WorkspaceFileTooLargeError:
        return _unavailable(413, "FILE_TOO_LARGE", "file exceeds the download size limit")
    except SandboxSupervisorError as exc:
        logger.warning("public_file.content_unavailable version=%s reason=%s", version.id, exc)
        return _not_found()
    await audit_emit(
        state.audit_logger,
        tenant_id=link.tenant_id,
        actor_id=_PUBLIC_ACTOR,
        action=AuditAction.ARTIFACT_SHARE_DOWNLOAD,
        resource_type="artifact",
        resource_id=artifact.name,
        trace_id=current_trace_id_hex(),
        details={
            "link_id_prefix": link.id[:4],
            "agent_key": link.agent_key,
            "version": version.version,
            "run_id": str(link.run_id) if link.run_id else None,
            "client_ip": client_ip(request, getattr(state, "trusted_proxies", ())),
        },
        on_behalf_of=str(link.user_id),
    )
    inferred = infer_content_type(path=version.path_in_workspace)
    headers = {
        "Content-Disposition": content_disposition_header(
            artifact.name, disposition=inferred.disposition
        ),
        "X-Content-Type-Options": "nosniff",
        VERSION_HEADER: str(version.version),
        # The URL is a bearer credential: keep it out of shared caches and Referer.
        "Cache-Control": "private, no-store",
        "Referrer-Policy": "no-referrer",
        # Whatever the bytes are, a browser that renders them runs no script and loads
        # nothing (defence in depth on top of attachment-for-active-content).
        "Content-Security-Policy": "sandbox; default-src 'none'",
    }
    return Response(content=data, media_type=inferred.content_type, headers=headers)
