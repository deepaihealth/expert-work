"""External file upload for third-party apps — ``/v1/agents/{agent_code}/uploads``.

Multipart entry point for the external-API upload path (external-API P1 plan,
Task 5): a third-party app posts a file on behalf of one of its own users
(``user_id``), scoped to an agent + (optionally) a session it already holds.

Images cannot exist outside a session — the object-store key and the
``expert_work://image/...`` reference both embed the thread id (ADR-0004,
see :mod:`expert_work.protocol.multimodal`) — so ``session_id`` is optional
here: when omitted, the endpoint mints a new session (via
``agents._resolve_session``, the same kill-switch + resolution the external
run/session endpoints use) and hands it back in the response so the caller's
next ``POST .../runs`` call can reuse it. Documents don't share that
constraint but ride the same session-scoping for symmetry with the console
upload endpoint (``POST /v1/sessions/{thread_id}/uploads``).

This also fixes a bug in that console endpoint: its document branch 400s
whenever ``caller_user_id is None`` (``api/uploads.py``'s
``_handle_document_upload``) — but an API-key caller is a machine identity
with no workspace of its own, so every document upload from a third-party
app would 400. This endpoint lands documents in the *declared end user's*
workspace (the ``tenant_user`` minted from ``user_id``), not the caller's.

Type dispatch + landing logic is shared with the console endpoint via
module-level functions in ``api/uploads.py`` (``_handle_document_upload`` /
``_prepare_image_upload`` / ``_land_image_upload``) rather than duplicated
here — see those functions' docstrings for why (two implementations of the
same upload semantics is how this codebase has drifted before).

Every rejection this endpoint's own validation raises internally as a plain
``HTTPException`` (missing filename, unsupported type, oversize, quota
denial passthrough, etc. — including everything ``_handle_document_upload``
/ ``_prepare_image_upload`` raise) is translated to the external plane's
``{success, data, error}`` envelope by ``_upload_error_envelope`` before it
reaches the wire — mirrors ``external_approvals.py``'s
``_decision_error_envelope``, the sibling endpoint this task shipped
alongside. A third-party SDK parsing ``error.code`` must never hit FastAPI's
bare ``{"detail": ...}`` shape.
"""

from __future__ import annotations

import logging
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import JSONResponse, Response

from control_plane.agent_disable_status import AgentDisableService
from control_plane.api._artifact_mime import content_disposition_header, infer_content_type
from control_plane.api._authz import external_only, require
from control_plane.api._external import (
    ExternalScopeError,
    external_error,
    load_owned_session,
    lookup_external_user_id,
    reject_nul_path_params,
)
from control_plane.api._quota_admission import check_admission
from control_plane.api._user_scope import get_user_repo
from control_plane.api.agents import _resolve_session, _SessionError
from control_plane.api.uploads import (
    _handle_document_upload,
    _land_image_upload,
    _prepare_image_upload,
)
from control_plane.quota.base import QuotaService
from control_plane.settings import Settings
from expert_work.persistence.agent_instance import AgentInstanceStore
from expert_work.persistence.agent_spec import AgentSpecStore
from expert_work.persistence.image_upload import ImageUploadStore
from expert_work.persistence.tenant_user import TenantUserStore
from expert_work.persistence.thread_meta import ThreadMetaStore
from expert_work.persistence.user_upload import UserUploadStore
from expert_work.protocol import Principal, QuotaDimension, parse_upload_id, render_upload_id
from expert_work.protocol.multimodal import parse_image_ref
from expert_work.runtime.audit.logger import AuditLogger
from expert_work.runtime.storage import ObjectNotFoundError, ObjectStore, ObjectStoreError
from orchestrator.tools import SandboxSupervisorError, WorkspacePermissionError, WorkspaceStore

logger = logging.getLogger("expert_work.control_plane.external_uploads")

#: Fallback envelope ``code`` by HTTP status for an ``HTTPException`` raised
#: during upload validation / landing (document or image path). Mirrors
#: ``external_approvals.py``'s ``_DECISION_ERROR_CODES`` /
#: ``_decision_error_envelope`` — every external endpoint owns translating
#: its internal ``HTTPException``s into the ``{success, data, error}``
#: contract rather than letting FastAPI's bare ``{"detail": ...}`` leak.
_UPLOAD_ERROR_CODES: dict[int, str] = {
    400: "INVALID_UPLOAD",
    413: "UPLOAD_TOO_LARGE",
    429: "QUOTA_EXCEEDED",
    500: "UPLOAD_FAILED",
    502: "UPLOAD_FAILED",
    503: "UPLOAD_UNAVAILABLE",
}


def _upload_error_envelope(exc: HTTPException) -> JSONResponse:
    """Render an ``HTTPException`` raised during upload validation / landing
    as the standard external envelope."""
    code = _UPLOAD_ERROR_CODES.get(exc.status_code, "UPLOAD_ERROR")
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "success": False,
            "data": None,
            "error": {"code": code, "message": str(exc.detail)},
        },
    )


def _upload_download_error(code: str, message: str, status: int) -> JSONResponse:
    """错误路径的 ``{success, data, error}`` 信封 —— 下载端点专用。

    成功路径**不走这里**——下载的成功响应是裸文件字节流,与「文件不是
    JSON」这个事实冲突(同 ``external_artifacts._artifact_error``)。与
    ``POST .../uploads`` 的 ``_upload_error_envelope``(按 HTTP 状态码猜
    code)不同,下载端点每条分支的 code 都是明确已知的,直接传参更直白。
    """
    return JSONResponse(
        status_code=status,
        content={"success": False, "data": None, "error": {"code": code, "message": message}},
    )


def _upload_id_or_422(raw: str) -> UUID:
    """Parse the ``upload_id`` path segment; raise the shared
    :class:`ExternalScopeError` (422) on any shape other than ``upl_<uuid>``
    — mirrors ``external_artifacts._name_or_422``'s calling convention."""
    parsed = parse_upload_id(raw)
    if parsed is None:
        raise ExternalScopeError(
            "INVALID_UPLOAD_ID",
            "upload_id must be the value returned by POST /v1/agents/{agent_code}/uploads",
            422,
        )
    return parsed


def _get_repo(request: Request) -> AgentSpecStore:
    return request.app.state.agent_spec_repo  # type: ignore[no-any-return]


def _get_thread_repo(request: Request) -> ThreadMetaStore:
    return request.app.state.thread_meta_repo  # type: ignore[no-any-return]


def _get_instance_store(request: Request) -> AgentInstanceStore:
    return request.app.state.agent_instance_store  # type: ignore[no-any-return]


def _get_disable_service(request: Request) -> AgentDisableService:
    return request.app.state.agent_disable_service  # type: ignore[no-any-return]


def _get_settings(request: Request) -> Settings:
    return request.app.state.settings  # type: ignore[no-any-return]


def _get_object_store(request: Request) -> ObjectStore | None:
    return getattr(request.app.state, "object_store", None)


def _get_quota(request: Request) -> QuotaService:
    return request.app.state.quota_service  # type: ignore[no-any-return]


def _get_audit(request: Request) -> AuditLogger:
    return request.app.state.audit_logger  # type: ignore[no-any-return]


def _get_image_upload_store(request: Request) -> ImageUploadStore:
    return request.app.state.image_upload_store  # type: ignore[no-any-return]


def _get_user_upload_store(request: Request) -> UserUploadStore:
    return request.app.state.user_upload_store  # type: ignore[no-any-return]


def _get_workspace_store(request: Request) -> WorkspaceStore | None:
    return getattr(request.app.state, "workspace_store", None)


def build_external_uploads_router() -> APIRouter:
    """Mount the external upload endpoints."""
    router = APIRouter(
        prefix="/v1/agents",
        tags=["external"],
        dependencies=[Depends(reject_nul_path_params), Depends(external_only())],
    )

    @router.post("/{agent_code}/uploads", status_code=201, response_model=None)
    async def upload_for_user(
        agent_code: str,
        request: Request,
        file: Annotated[UploadFile, File()],
        user_id: Annotated[str, Form(min_length=1, max_length=255)],
        principal: Annotated[Principal, Depends(require("session", "write"))],
        repo: Annotated[AgentSpecStore, Depends(_get_repo)],
        threads: Annotated[ThreadMetaStore, Depends(_get_thread_repo)],
        users: Annotated[TenantUserStore, Depends(get_user_repo)],
        instances: Annotated[AgentInstanceStore, Depends(_get_instance_store)],
        disable_service: Annotated[AgentDisableService, Depends(_get_disable_service)],
        settings: Annotated[Settings, Depends(_get_settings)],
        store: Annotated[ObjectStore | None, Depends(_get_object_store)],
        quota: Annotated[QuotaService, Depends(_get_quota)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
        images: Annotated[ImageUploadStore, Depends(_get_image_upload_store)],
        uploads: Annotated[UserUploadStore, Depends(_get_user_upload_store)],
        workspace_store: Annotated[WorkspaceStore | None, Depends(_get_workspace_store)],
        session_id: Annotated[UUID | None, Form()] = None,
    ) -> JSONResponse:
        """Upload a file on behalf of an external-app end-user.

        ``session_id`` omitted → mints a new session (same resolution +
        kill-switch gate ``POST /{agent_code}/runs`` uses) and returns it so
        the caller's next run reuses it. ``session_id`` supplied → the same
        kill-switch gate, then the usual external-plane ownership check (404
        hides cross-user existence).
        """
        tenant_id: UUID = request.state.tenant_id
        actor_id: str = request.state.actor_id

        if session_id is None:
            try:
                _record, thread_id, end_user_id = await _resolve_session(
                    tenant_id=tenant_id,
                    agent_code=agent_code,
                    actor_id=actor_id,
                    user_id=user_id,
                    session_id=None,
                    repo=repo,
                    threads=threads,
                    users=users,
                    instances=instances,
                    disable_service=disable_service,
                )
            except _SessionError as exc:
                return external_error(ExternalScopeError(exc.code, exc.message, exc.status_code))
        else:
            # Kill-switch first, exactly as ``_resolve_session`` does it on the
            # other branch. ``load_owned_session`` is a pure ownership check
            # with no kill-switch of its own, so without this the gate depended
            # on whether the caller passed ``session_id`` — and a disabled agent
            # kept accepting 201s, writing into the end user's persistent
            # workspace and burning image quota. Worse, the design spec's own
            # recommendation (§四-6: reuse ``session_id`` for follow-up uploads)
            # pointed at the ungated branch. ``POST .../runs`` 403s either way;
            # this is the parity fix (P1 final review, I1).
            if await disable_service.is_disabled(tenant_id, agent_code):
                return external_error(
                    ExternalScopeError("AGENT_DISABLED", f"agent {agent_code!r} is disabled", 403)
                )
            try:
                # ``mint=False`` — this branch only ever runs against a session
                # that already exists, and an existing session's owner already
                # has a ``tenant_user`` row, so there is nothing here to mint.
                # The only row minting could create is one for a ``user_id``
                # that by definition does NOT own this session — i.e. exactly
                # the case that must 404, which is how pointing an existing
                # ``session_id`` at enumerated ``user_id``s left one ghost row
                # per attempt on the user-dimension ops page. Same defect and
                # same reasoning as ``load_owned_run`` (P1 final review, C1);
                # the mint belongs to the ``session_id is None`` branch above,
                # where ``_resolve_session`` genuinely creates the session.
                meta = await load_owned_session(
                    tenant_id=tenant_id,
                    agent_code=agent_code,
                    user_id=user_id,
                    session_id=session_id,
                    threads=threads,
                    users=users,
                    mint=False,
                )
            except ExternalScopeError as exc:
                return external_error(exc)
            # ``load_owned_session`` already matched ``meta.user_id`` against
            # the resolved end-user (else it would have raised above), so
            # it's never None here — assert narrows the type for mypy.
            assert meta.user_id is not None  # noqa: S101
            thread_id = meta.thread_id
            end_user_id = meta.user_id

        try:
            if not file.filename:
                raise HTTPException(status_code=400, detail="uploaded file has no filename")
            content_type = (file.content_type or "").lower()

            # Document upload → lands in the declared end user's persistent
            # workspace (not the API-key caller's — a machine principal has
            # none).
            if content_type in settings.document_allowed_content_types:
                doc_result = await _handle_document_upload(
                    content_type=content_type,
                    filename=file.filename,
                    file=file,
                    request=request,
                    tenant_id=tenant_id,
                    caller_user_id=end_user_id,
                    thread_id=thread_id,
                    settings=settings,
                    workspace_store=workspace_store,
                    audit=audit,
                )
                row = await uploads.insert(
                    upload_id=uuid4(),
                    tenant_id=tenant_id,
                    user_id=end_user_id,
                    thread_id=thread_id,
                    kind="document",
                    ref=doc_result.path,
                    mime_type=content_type,
                    size_bytes=doc_result.size_bytes,
                    filename=doc_result.path.rsplit("/", 1)[-1],
                )
                return JSONResponse(
                    status_code=201,
                    content={
                        "success": True,
                        "data": {
                            "upload_id": render_upload_id(row.id),
                            "session_id": str(thread_id),
                            "type": "document",
                            "mime": content_type,
                            "size": doc_result.size_bytes,
                        },
                        "error": None,
                    },
                )

            if store is None:
                raise HTTPException(status_code=503, detail="object store unavailable")
            raw, ext = await _prepare_image_upload(
                file=file, content_type=content_type, settings=settings
            )

            denial = await check_admission(
                quota=quota,
                audit=audit,
                tenant_id=tenant_id,
                actor_id=actor_id,
                agent=agent_code,
                resource_kind="image_upload",
                cost=1,
                cost_overrides={QuotaDimension.IMAGE_STORAGE_BYTES: len(raw)},
            )
            if denial is not None:
                return denial

            image_ref = await _land_image_upload(
                request=request,
                store=store,
                images=images,
                audit=audit,
                tenant_id=tenant_id,
                thread_id=thread_id,
                user_id=end_user_id,
                ext=ext,
                raw=raw,
                content_type=content_type,
            )
            row = await uploads.insert(
                upload_id=uuid4(),
                tenant_id=tenant_id,
                user_id=end_user_id,
                thread_id=thread_id,
                kind="image",
                ref=image_ref.to_uri(),
                mime_type=content_type,
                size_bytes=len(raw),
                filename=f"{image_ref.image_id}{image_ref.ext}",
            )
            return JSONResponse(
                status_code=201,
                content={
                    "success": True,
                    "data": {
                        "upload_id": render_upload_id(row.id),
                        "session_id": str(thread_id),
                        "type": "image",
                        "mime": content_type,
                        "size": len(raw),
                    },
                    "error": None,
                },
            )
        except HTTPException as exc:
            return _upload_error_envelope(exc)

    @router.get(
        "/{agent_code}/uploads/{upload_id}",
        response_model=None,
        dependencies=[Depends(require("session", "read"))],
    )
    async def download_upload(
        agent_code: str,
        upload_id: str,
        request: Request,
        users: Annotated[TenantUserStore, Depends(get_user_repo)],
        uploads: Annotated[UserUploadStore, Depends(_get_user_upload_store)],
        images: Annotated[ImageUploadStore, Depends(_get_image_upload_store)],
        store: Annotated[ObjectStore | None, Depends(_get_object_store)],
        workspace_store: Annotated[WorkspaceStore | None, Depends(_get_workspace_store)],
        user_id: Annotated[str, Query(min_length=1, max_length=255)],
    ) -> Response:
        """Download one previously-uploaded document or image's raw bytes.

        Structural mirror of ``external_artifacts.download_artifact``:
        success is the raw file body, not the ``{success, data, error}``
        envelope (see ``_upload_download_error``'s docstring); only error
        paths render it. ``lookup_external_user_id`` never mints — an
        unrecognized ``user_id`` must not create a ``tenant_user`` row just
        because a third party probed this GET (same P1 review T3 rule every
        other external read endpoint follows).
        """
        del agent_code  # uploads are (tenant, user)-scoped — mirrors external_artifacts.
        tenant_id: UUID = request.state.tenant_id
        try:
            parsed_id = _upload_id_or_422(upload_id)
            end_user_id = await lookup_external_user_id(
                tenant_id=tenant_id, user_id=user_id, users=users
            )
        except ExternalScopeError as exc:
            return external_error(exc)
        if end_user_id is None:
            return _upload_download_error("UPLOAD_NOT_FOUND", "upload not found", 404)
        row = await uploads.get(upload_id=parsed_id, tenant_id=tenant_id)
        if row is None or row.user_id != end_user_id or row.deleted_at is not None:
            return _upload_download_error("UPLOAD_NOT_FOUND", "upload not found", 404)

        if row.kind == "document":
            if workspace_store is None:
                return _upload_download_error(
                    "UPLOAD_CONTENT_UNAVAILABLE", "upload content unavailable", 503
                )
            try:
                data = await workspace_store.read_file(
                    tenant_id=tenant_id, user_id=end_user_id, path=row.ref
                )
            except WorkspacePermissionError:
                # 元数据行在、内容读不动是权限问题(服务端配置),不是「不存在」
                # ——两者不能合并成一个 404(沙箱迁移 W2-BUG-1 的教训)。这个
                # except **必须排在** SandboxSupervisorError 之前:它是后者的
                # 子类,顺序反了永远走不到(同 external_artifacts.download_
                # artifact 的同款注释)。traceback 只进日志,不进响应体。
                logger.warning(
                    "external_upload.permission_denied upload_id=%s", parsed_id, exc_info=True
                )
                return _upload_download_error(
                    "UPLOAD_CONTENT_UNAVAILABLE", "upload content unavailable", 500
                )
            except SandboxSupervisorError as exc:
                logger.warning(
                    "external_upload.content_unavailable upload_id=%s reason=%s", parsed_id, exc
                )
                return _upload_download_error("UPLOAD_NOT_FOUND", "upload content not found", 404)
            # ArtifactKind = Literal["document", "code", "data", "other"]
            # (expert_work.protocol.artifact) — "document" is an exact match
            # for this branch, no substitution needed.
            inferred = infer_content_type(kind="document", path=row.filename)
        else:
            # row.kind == "image". ``row.ref`` is always a well-formed
            # ``expert_work://image/...`` URI here — the only writer is this
            # same module's ``upload_for_user`` (``image_ref.to_uri()``) — but
            # stay defensive rather than let a violated store invariant escape
            # as a raw 500 (same rule ``download_artifact``'s "artifact is
            # None" guard documents).
            try:
                image_id = parse_image_ref(row.ref).image_id
            except ValueError:
                return _upload_download_error("UPLOAD_NOT_FOUND", "upload not found", 404)
            image_row = await images.get(image_id=image_id, tenant_id=tenant_id)
            # ``ImageUploadStore.get`` does not filter ``deleted_at`` — the
            # caller must (same contract ``uploads.py``'s console-side
            # ``delete_image`` documents: ``if row is None or row.deleted_at
            # is not None``). Without this, a console-deleted image whose
            # bytes are still awaiting the retention sweep stays downloadable
            # to a third party through this endpoint.
            if image_row is None or image_row.deleted_at is not None:
                return _upload_download_error("UPLOAD_NOT_FOUND", "upload not found", 404)
            if store is None:
                return _upload_download_error(
                    "UPLOAD_CONTENT_UNAVAILABLE", "upload content unavailable", 503
                )
            try:
                data = await store.get(image_row.object_key)
            except ObjectNotFoundError:
                return _upload_download_error("UPLOAD_NOT_FOUND", "upload content not found", 404)
            except ObjectStoreError:
                # 对象未丢(上面已排除),但读不动——凭证 / bucket 策略 / 存储侧
                # 5xx 等。同一条「底层读不动就是 500,不能让它逃逸成裸异常」
                # 规则,同文档分支的 WorkspacePermissionError 处理(本文件
                # docstring 第 36-38 行);ObjectNotFoundError 是 ObjectStoreError
                # 的子类,顺序必须子类在前(同 WorkspacePermissionError /
                # SandboxSupervisorError 那对的顺序规则)。traceback 只进日志。
                logger.warning(
                    "external_upload.object_store_failed upload_id=%s", parsed_id, exc_info=True
                )
                return _upload_download_error(
                    "UPLOAD_CONTENT_UNAVAILABLE", "upload content unavailable", 500
                )
            # ArtifactKind (expert_work.protocol.artifact) has no "image"
            # value — Literal["document", "code", "data", "other"] — so
            # "other" is the closest fit (not "document"/"code"/"data").
            # This has no behavioral effect either way: infer_content_type's
            # dispatch is purely extension-based in every branch (`kind` is
            # `del`eted, unused, even in its own fallback branch — verified by
            # reading its body) — "other" is chosen for readability, not
            # because it changes the inferred disposition (the only field of
            # ``inferred`` this endpoint still reads — see the Content-Type
            # comment below).
            inferred = infer_content_type(kind="other", path=row.filename)

        headers = {
            "Content-Disposition": content_disposition_header(
                row.filename, disposition=inferred.disposition
            ),
            "X-Content-Type-Options": "nosniff",
        }
        # spec §一.3 — Content-Type is the MIME recorded at upload time
        # (``row.mime_type``), NOT ``infer_content_type``'s extension-based
        # guess: that guess falls to ``application/octet-stream`` for
        # ``application/pdf`` / the three ``openxmlformats`` document types
        # (none are in ``_artifact_mime``'s whitelist) and to ``text/plain``
        # for ``.csv`` (whose real MIME is ``text/csv``). Only the
        # DISPOSITION side of ``infer_content_type``'s verdict is used —
        # the active-content-forces-attachment / unknown-forces-attachment
        # rule stays in force regardless of what MIME the upload declared.
        return Response(content=data, media_type=row.mime_type, headers=headers)

    return router
