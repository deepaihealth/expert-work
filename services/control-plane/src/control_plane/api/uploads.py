"""``POST /v1/sessions/{thread_id}/uploads`` — Stream J.6 image upload.

Multipart entry point for the multimodal input path: the user uploads an
image alongside a session, the bytes land in the object store, and the
endpoint returns the ``expert_work://image/...`` reference the run request's
``image_refs`` field carries.

Image references — not inline base64 — ride in the run message so each
checkpoint snapshot stays small; the provider adapter / ``ask_image``
tool resolves the ref to bytes only at LLM-call time.

Auth follows the same J.14 pattern as ``POST .../runs`` — the caller
must own the thread, and 404 hides cross-user existence.
"""

from __future__ import annotations

import hashlib
import io
import logging
import re
import zipfile
from typing import Annotated, Final
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse

from control_plane.api._image_sanitize import ImageSanitizeError, strip_exif
from control_plane.api._quota_admission import check_admission
from control_plane.api._user_scope import (
    caller_owns_thread,
    get_user_repo,
    resolve_caller_user_id,
)
from control_plane.audit import emit as audit_emit
from control_plane.quota.base import QuotaService
from control_plane.settings import Settings
from expert_work.common.observability import current_trace_id_hex
from expert_work.persistence import WORKSPACE_UPLOADS_DIR
from expert_work.persistence.image_upload import ImageUploadStore
from expert_work.persistence.tenant_user import TenantUserStore
from expert_work.protocol import AuditAction, AuditResult, QuotaDimension
from expert_work.protocol.multimodal import ImageRef
from expert_work.runtime.audit.logger import AuditLogger
from expert_work.runtime.storage import ObjectStore
from orchestrator.tools import SandboxSupervisorError, WorkspacePermissionError, WorkspaceStore

logger = logging.getLogger("expert_work.control_plane.uploads")

#: File extension per accepted image content type. The reverse direction
#: (ext → media_type) lives in the orchestrator's image resolver.
_EXT_BY_CONTENT_TYPE: Final[dict[str, str]] = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
}

#: Canonical file extension per accepted document content type. The extension
#: drives the sandbox ``read_document`` parser dispatch, so it is derived from
#: the (trusted) content type — not the user-supplied filename.
_DOC_EXT_BY_CONTENT_TYPE: Final[dict[str, str]] = {
    "application/pdf": ".pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
    "text/plain": ".txt",
    "text/markdown": ".md",
    "text/csv": ".csv",
}

#: Office formats that are ZIP containers — guarded against decompression bombs.
_ZIP_DOC_EXTS: Final[frozenset[str]] = frozenset({".docx", ".xlsx", ".pptx"})
#: Reject a ZIP doc whose declared uncompressed total exceeds this — bounds the
#: sandbox parse memory (read from the central directory, nothing is extracted).
_MAX_ZIP_UNCOMPRESSED_BYTES: Final[int] = 200 * 1024 * 1024
#: Workspace subdirectory uploaded documents land in (namespaced away from
#: agent-written files so an upload can't clobber the agent's own output, and
#: hidden from the "agent products" browse — shared source of truth in
#: :mod:`expert_work.persistence.workspace.layout`).
_UPLOAD_DIR: Final[str] = WORKSPACE_UPLOADS_DIR
_SAFE_STEM_RE: Final[re.Pattern[str]] = re.compile(r"[^A-Za-z0-9._-]+")


def _safe_workspace_name(filename: str, ext: str) -> str:
    """Build a safe ``uploads/<stem><ext>`` workspace path from a user filename.

    The stem is sanitised to ``[A-Za-z0-9._-]`` (path separators / NUL / ``..``
    cannot survive); the extension comes from the trusted content type, not the
    filename. Falls back to a uuid stem when nothing usable remains."""
    stem = _SAFE_STEM_RE.sub("_", filename.rsplit("/", 1)[-1].rsplit(".", 1)[0]).strip("._")
    if not stem:
        stem = uuid4().hex
    return f"{_UPLOAD_DIR}/{stem[:120]}{ext}"


def _reject_zip_bomb(raw: bytes, ext: str) -> None:
    """Raise 400/413 if a ZIP-container document is malformed or a bomb.

    Reads only the central directory (``ZipInfo.file_size``) — nothing is
    extracted — so the check itself cannot be turned into a bomb."""
    if ext not in _ZIP_DOC_EXTS:
        return
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            total = sum(info.file_size for info in zf.infolist())
    except zipfile.BadZipFile as exc:
        raise HTTPException(status_code=400, detail="document is not a valid office file") from exc
    if total > _MAX_ZIP_UNCOMPRESSED_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"document decompresses past the {_MAX_ZIP_UNCOMPRESSED_BYTES}-byte limit",
        )


async def _handle_document_upload(
    *,
    content_type: str,
    filename: str,
    file: UploadFile,
    request: Request,
    tenant_id: UUID,
    caller_user_id: UUID | None,
    thread_id: UUID,
    settings: Settings,
    workspace_store: WorkspaceStore | None,
    audit: AuditLogger,
) -> JSONResponse:
    """Land an uploaded document in the caller's persistent workspace.

    The parse itself runs later in the sandbox (``read_document``); the
    control-plane only validates + writes the bytes, keeping the malicious-
    document attack surface out of this process."""
    ext = _DOC_EXT_BY_CONTENT_TYPE.get(content_type)
    if ext is None:
        # Config drift: the allowlist admitted a type the extension table
        # doesn't know. Refuse rather than land an unparseable file.
        raise HTTPException(
            status_code=400,
            detail=f"no extension known for content type {content_type!r}",
        )
    if caller_user_id is None:
        # A document needs a per-user workspace to land in.
        raise HTTPException(status_code=400, detail="document upload requires a user workspace")
    if workspace_store is None:
        raise HTTPException(status_code=503, detail="document upload unavailable: no sandbox")

    max_bytes = settings.document_max_bytes
    if file.size is not None and file.size > max_bytes:
        raise HTTPException(status_code=413, detail=f"document exceeds {max_bytes}-byte limit")
    raw = await file.read()
    if len(raw) > max_bytes:
        raise HTTPException(status_code=413, detail=f"document exceeds {max_bytes}-byte limit")
    if not raw:
        raise HTTPException(status_code=400, detail="uploaded file is empty")
    _reject_zip_bomb(raw, ext)

    workspace_path = _safe_workspace_name(filename, ext)
    try:
        await workspace_store.write_file(
            tenant_id=tenant_id,
            user_id=caller_user_id,
            path=workspace_path,
            data=raw,
        )
    except WorkspacePermissionError as exc:
        # 权限失败是服务端配置问题(共享 uid 没配上/存量目录属主没迁移/mode 不
        # 对),不是笼统的"上游写失败"——必须排在下面的 SandboxSupervisorError
        # 之前(它的子类,顺序反了这一分支永远走不到,同六处 workspace 端点的
        # 坑)。500(而非下面的 502)与那六处的约定一致:服务端配置问题不该被
        # 当成"上游 502"。detail 是固定文案,不插值异常对象:store 的消息里带
        # 工作区相对路径,Task A 已经把它从绝对路径/uid/mode 里 scrub 掉了,但
        # 这里原样 f"...: {exc}" 会把 store 消息原封不动地 echo 回响应体,等于
        # 白 scrub。
        logger.warning("upload.write_permission_denied", exc_info=True)
        raise HTTPException(status_code=500, detail="workspace write failed") from exc
    except SandboxSupervisorError as exc:
        # 同上——固定文案,不插值异常对象;诊断细节只进下面这条结构化日志。
        logger.warning("upload.write_failed", exc_info=True)
        raise HTTPException(status_code=502, detail="workspace write failed") from exc

    actor_id: str = getattr(request.state, "actor_id", "anonymous")
    sha256_hex = hashlib.sha256(raw).hexdigest()
    await audit_emit(
        audit,
        tenant_id=tenant_id,
        actor_id=actor_id,
        action=AuditAction.DOCUMENT_UPLOAD,
        resource_type="user_workspace",
        resource_id=workspace_path,
        result=AuditResult.SUCCESS,
        trace_id=current_trace_id_hex(),
        details={
            "thread_id": str(thread_id),
            "workspace_path": workspace_path,
            "file_size_bytes": len(raw),
            "mime_type": content_type,
            "sha256": sha256_hex,
        },
    )
    return JSONResponse(
        status_code=201,
        content={"path": workspace_path, "kind": "document"},
    )


def _get_object_store(request: Request) -> ObjectStore | None:
    store: ObjectStore | None = getattr(request.app.state, "object_store", None)
    return store


def _get_thread_repo(request: Request) -> object:
    return request.app.state.thread_meta_repo


def _get_settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def _get_quota(request: Request) -> QuotaService:
    return request.app.state.quota_service  # type: ignore[no-any-return]


def _get_audit(request: Request) -> AuditLogger:
    return request.app.state.audit_logger  # type: ignore[no-any-return]


def _get_image_upload_store(request: Request) -> ImageUploadStore:
    return request.app.state.image_upload_store  # type: ignore[no-any-return]


def _get_workspace_store(request: Request) -> WorkspaceStore | None:
    return getattr(request.app.state, "workspace_store", None)


def build_uploads_router() -> APIRouter:
    """One router housing the J.6 upload + lifecycle endpoints.

    ``POST /v1/sessions/{thread_id}/uploads`` (Stream J.6) sits under
    the session because uploads are scoped to a thread. ``DELETE
    /v1/uploads/{image_id}`` (Mini-ADR J-32 / J.6.补强-3) sits at the
    top-level because the caller looks up the image by its id without
    needing to know which thread it belonged to.
    """
    router = APIRouter()

    @router.post("/v1/sessions/{thread_id}/uploads", response_model=None, tags=["sessions"])
    async def upload_image(
        thread_id: UUID,
        request: Request,
        file: Annotated[UploadFile, File()],
        threads: Annotated[object, Depends(_get_thread_repo)],
        users: Annotated[TenantUserStore, Depends(get_user_repo)],
        store: Annotated[ObjectStore | None, Depends(_get_object_store)],
        settings: Annotated[Settings, Depends(_get_settings)],
        quota: Annotated[QuotaService, Depends(_get_quota)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
        images: Annotated[ImageUploadStore, Depends(_get_image_upload_store)],
        workspace_store: Annotated[WorkspaceStore | None, Depends(_get_workspace_store)],
    ) -> JSONResponse:
        tenant_id: UUID = request.state.tenant_id

        # Thread ownership — Stream J.14 (404 hides cross-user existence).
        meta = await threads.get(thread_id, tenant_id=tenant_id)  # type: ignore[attr-defined]
        if meta is None:
            raise HTTPException(status_code=404, detail="session not found")
        caller_user_id = await resolve_caller_user_id(request, users)
        if not caller_owns_thread(
            meta=meta,
            caller_user_id=caller_user_id,
            principal=request.state.principal,
        ):
            raise HTTPException(status_code=404, detail="session not found")

        # Validate the upload at the boundary.
        if not file.filename:
            raise HTTPException(status_code=400, detail="uploaded file has no filename")
        content_type = (file.content_type or "").lower()

        # Document upload → lands in the user's persistent workspace so a later
        # run's ``read_document`` can parse it. Separate path from images: docs
        # ride the supervisor workspace-write, not the image object store.
        if content_type in settings.document_allowed_content_types:
            return await _handle_document_upload(
                content_type=content_type,
                filename=file.filename,
                file=file,
                request=request,
                tenant_id=tenant_id,
                caller_user_id=caller_user_id,
                thread_id=thread_id,
                settings=settings,
                workspace_store=workspace_store,
                audit=audit,
            )

        if store is None:
            raise HTTPException(status_code=503, detail="object store unavailable")
        if content_type not in settings.multimodal_allowed_content_types:
            raise HTTPException(
                status_code=400,
                detail=f"unsupported content type: {content_type or 'missing'!r}",
            )
        ext = _EXT_BY_CONTENT_TYPE.get(content_type)
        if ext is None:
            # Config drift: the allowlist admitted a type the canonical
            # extension table doesn't know. Refuse rather than forge a key.
            raise HTTPException(
                status_code=400,
                detail=f"no extension known for content type {content_type!r}",
            )

        max_bytes = settings.multimodal_max_image_bytes
        if file.size is not None and file.size > max_bytes:
            raise HTTPException(status_code=413, detail=f"image exceeds {max_bytes}-byte limit")
        raw = await file.read()
        if len(raw) > max_bytes:
            raise HTTPException(status_code=413, detail=f"image exceeds {max_bytes}-byte limit")
        if not raw:
            raise HTTPException(status_code=400, detail="uploaded file is empty")

        # Mini-ADR J-34 (J.6.补强-4) — strip EXIF / metadata before any
        # downstream code touches the bytes. The sanitised payload is
        # what lands in the object store + gets counted against quota
        # + gets sha256'd for the audit row — every consumer sees the
        # same metadata-free bytes.
        try:
            raw = strip_exif(raw, mime_type=content_type)
        except ImageSanitizeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        # Mini-ADR J-30 (J.6.补强-1) — quota admission. The single
        # ``check`` call deducts ``cost=1`` from QPS +
        # ``IMAGE_UPLOAD_COUNT_30D`` and ``cost=len(raw)`` from
        # ``IMAGE_STORAGE_BYTES`` (only the dimensions a tenant has
        # rows for run; others are no-ops). 429 on denial.
        actor_id: str = getattr(request.state, "actor_id", "anonymous")
        denial = await check_admission(
            quota=quota,
            audit=audit,
            tenant_id=tenant_id,
            actor_id=actor_id,
            agent=None,
            resource_kind="image_upload",
            cost=1,
            cost_overrides={QuotaDimension.IMAGE_STORAGE_BYTES: len(raw)},
        )
        if denial is not None:
            return denial

        image_ref = ImageRef(
            tenant_id=tenant_id,
            thread_id=thread_id,
            image_id=uuid4(),
            ext=ext,
        )
        await store.put(image_ref.storage_key, raw, content_type=content_type)

        # Mini-ADR J-32 (J.6.补强-3) — register the upload in the
        # ``image_upload`` table so the lifecycle (soft-delete + retention
        # sweep) can find it later. ``sha256`` doubles as a dedup key for
        # ops investigations.
        sha256_hex = hashlib.sha256(raw).hexdigest()
        await images.insert(
            image_id=image_ref.image_id,
            tenant_id=tenant_id,
            thread_id=thread_id,
            user_id=caller_user_id,
            object_key=image_ref.storage_key,
            size_bytes=len(raw),
            mime_type=content_type,
            sha256=sha256_hex,
        )

        # Mini-ADR J-31 (J.6.补强-2) — uploads emit their own audit row.
        # The audit middleware's SESSION_WRITE row covers run dispatch but
        # not image-specific metadata (size / mime / object_key / sha256),
        # which the SOC / compliance pipeline needs to trace every byte.
        principal = getattr(request.state, "principal", None)
        subject_type = (
            principal.subject_type
            if principal is not None and hasattr(principal, "subject_type")
            else "user"
        )
        auth_method = (
            principal.auth_method
            if principal is not None and hasattr(principal, "auth_method")
            else "jwt"
        )
        await audit_emit(
            audit,
            tenant_id=tenant_id,
            actor_id=actor_id,
            action=AuditAction.IMAGE_UPLOAD,
            resource_type="image_upload",
            resource_id=str(image_ref.image_id),
            result=AuditResult.SUCCESS,
            trace_id=current_trace_id_hex(),
            details={
                "thread_id": str(thread_id),
                "object_key": image_ref.storage_key,
                "file_size_bytes": len(raw),
                "mime_type": content_type,
                "sha256": sha256_hex,
                "subject_type": str(subject_type),
                "auth_method": str(auth_method),
                "ext": ext,
            },
        )
        return JSONResponse(status_code=201, content={"image_ref": image_ref.to_uri()})

    @router.delete("/v1/uploads/{image_id}", response_model=None, tags=["uploads"])
    async def delete_image(
        image_id: UUID,
        request: Request,
        users: Annotated[TenantUserStore, Depends(get_user_repo)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
        images: Annotated[ImageUploadStore, Depends(_get_image_upload_store)],
    ) -> JSONResponse:
        """Soft-delete an image upload.

        Mini-ADR J-32 (J.6.补强-3) — flips ``image_upload.deleted_at`` so
        the retention sweep eventually clears the object-store key.
        The endpoint hides cross-tenant + cross-user existence behind
        404, same J.14 rule as ``POST /uploads`` + ``POST /runs``.
        """
        from datetime import UTC
        from datetime import datetime as _dt

        tenant_id: UUID = request.state.tenant_id
        row = await images.get(image_id=image_id, tenant_id=tenant_id)
        if row is None or row.deleted_at is not None:
            raise HTTPException(status_code=404, detail="image not found")

        # Thread ownership — re-resolve the thread to confirm caller still
        # owns it (the row could outlive a session ownership change).
        threads = request.app.state.thread_meta_repo
        meta = await threads.get(row.thread_id, tenant_id=tenant_id)
        caller_user_id = await resolve_caller_user_id(request, users)
        if meta is None or not caller_owns_thread(
            meta=meta,
            caller_user_id=caller_user_id,
            principal=request.state.principal,
        ):
            raise HTTPException(status_code=404, detail="image not found")

        now = _dt.now(UTC)
        flipped = await images.soft_delete(image_id=image_id, tenant_id=tenant_id, now=now)
        if not flipped:
            # Race — another deleter beat us; idempotent 404.
            raise HTTPException(status_code=404, detail="image not found")

        actor_id: str = getattr(request.state, "actor_id", "anonymous")
        await audit_emit(
            audit,
            tenant_id=tenant_id,
            actor_id=actor_id,
            action=AuditAction.IMAGE_UPLOAD,  # same action verb; result=DENIED distinguishes
            resource_type="image_upload",
            resource_id=str(image_id),
            result=AuditResult.SUCCESS,
            reason="soft_delete",
            trace_id=current_trace_id_hex(),
            details={
                "thread_id": str(row.thread_id),
                "object_key": row.object_key,
                "file_size_bytes": row.size_bytes,
                "mime_type": row.mime_type,
                "operation": "soft_delete",
            },
        )
        return JSONResponse(status_code=204, content=None)

    return router
