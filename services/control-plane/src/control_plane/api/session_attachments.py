"""``GET /v1/sessions/{thread_id}/attachment`` — the bytes behind one attachment chip (B-295).

The console transcript shows each attachment a user message carried as a chip
(conversation detail page and the playground). The chip holds exactly what the
control plane wrote into the message: an image's ``expert_work://image/…`` reference
or a document's ``uploads/<leaf>``. This endpoint turns that ``ref`` into bytes for
the preview / download, and nothing else:

- **Who.** The conversation's own visibility (user ruling 2026-11-07): whoever may
  read the conversation — ``session:read``, the gate of ``/v1/conversations`` list and
  detail — may open its attachments. Console plane only (an API key is refused), tenant
  scoped, ``tenant_id`` passthrough for a system admin (:func:`ensure_single_tenant_scope`,
  which audits the cross-tenant read). A read by someone other than the conversation's
  owner leaves a ``user:data_view`` audit row (:mod:`._conversation_view_audit`) — the
  contents are often health data.
- **What.** An image is looked up by the ref's image id in this tenant's
  ``image_upload`` registry, and that row must belong to this thread; the bytes are read
  from the row's object key — the ref is never trusted to locate bytes on its own.
  A document ``ref`` must be the one shape uploads produce
  (:func:`is_safe_document_upload_id`: ``uploads/<one safe leaf>``) and is resolved by
  the server in the thread owner's volume; the client never supplies a path. For a
  conversation-view thread (B-262: ``threads/<id>/work/``) the ``user_upload`` rows of
  this thread for this name are consulted first: if there are rows and none is live, the
  attachment is expired (retention soft-deletes the row and may keep the bytes a while).
  If there is no row at all — console uploads and cross-conversation copies before B-281
  were never registered, and prod has such conversations since 11-04 — the file is served
  by path alone, which is still this thread's own directory. An old-view thread lands
  uploads in the agent root shared by that user's conversations with the agent, where
  nothing is registered (B-281 deliberately does not register there): those are served
  by path alone, so any upload in that shared root is reachable from any of those
  conversations.
- **Gone.** Retention (B-63 / B-283 / B-286 / B-282) deletes rows and bytes; a deleted
  image, a reaped or soft-deleted row, missing bytes, or a ref that is not this
  conversation's all answer the same 410 ``ATTACHMENT_GONE`` so the page can say
  「文件已过期或已删除」 and the answers cannot be told apart. A storage problem is not
  "gone": 500 (permission / object store), 503 (supervisor), 409 (changed mid-read).
- **Headers.** ``Content-Type`` comes from the stored row (images, raster types only)
  or from the stored name plus sniffed magic (documents: a PDF is ``application/pdf``
  only when it starts with ``%PDF-``); everything else, and all active content (HTML,
  SVG, …), goes out as an ``attachment``. Always ``nosniff`` and
  ``Cache-Control: private, no-store``; ``Content-Disposition`` carries the RFC 5987
  ``filename*``.

The whole file is read into memory before it is sent, like ``…/workspace/file`` (the
store's own read cap bounds it). Reading here does not count as a use for retention:
someone looking at a conversation is not the tenant using the file.
"""

from __future__ import annotations

import logging
from pathlib import PurePosixPath
from typing import Annotated, Final, Literal, NoReturn
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response

from control_plane.api._artifact_mime import (
    ContentDisposition,
    content_disposition_header,
    infer_content_type,
)
from control_plane.api._authz import console_only, require, require_key_scope
from control_plane.api._conversation_view_audit import audit_conversation_data_view
from control_plane.api._user_scope import get_user_repo, resolve_caller_user_id
from control_plane.api._workspace_shared import thread_conversation_id, thread_workspace_path
from control_plane.api.uploads import is_safe_document_upload_id
from control_plane.tenant_scope import (
    SingleTenant,
    applied_scope,
    cross_tenant_query_enabled,
    ensure_single_tenant_scope,
)
from expert_work.common.observability import current_trace_id_hex
from expert_work.persistence.image_upload import ImageUploadStore
from expert_work.persistence.tenant_user import TenantUserStore
from expert_work.persistence.thread_meta import ThreadMetaStore
from expert_work.persistence.user_upload import UserUploadStore
from expert_work.protocol import ThreadMeta
from expert_work.protocol.multimodal import IMAGE_REF_PREFIX, parse_image_ref
from expert_work.runtime.audit.logger import AuditLogger
from expert_work.runtime.storage import ObjectNotFoundError, ObjectStore, ObjectStoreError
from orchestrator.tools import (
    SandboxSupervisorError,
    WorkspaceFileTooLargeError,
    WorkspacePermissionError,
    WorkspaceStore,
)
from orchestrator.tools.nas_workspace_store import MISSING_DIR_ERRORS
from orchestrator.tools.sandbox import (
    WorkspaceFileChangedError,
    WorkspaceNotAFileError,
    WorkspacePathEscapeError,
)

logger = logging.getLogger("expert_work.control_plane.session_attachments")

#: Upper bound on the ``ref`` query value — an image ref is ~130 chars, a document leaf
#: at most 255 bytes; anything longer is not something the producers write.
_MAX_REF_CHARS: Final = 1024

#: Image types a browser renders without executing anything — the upload allowlist
#: (``uploads._EXT_BY_CONTENT_TYPE``). A row with any other MIME goes out as opaque bytes.
_RASTER_IMAGE_MIMES: Final[frozenset[str]] = frozenset(
    {"image/png", "image/jpeg", "image/webp", "image/gif"}
)

_PDF_MAGIC: Final = b"%PDF-"
_OPAQUE: Final = "application/octet-stream"
_UNAVAILABLE: Final = "attachment content unavailable"

#: The store's "nothing readable at that name" answers. ``MISSING_DIR_ERRORS`` (the store
#: module's public tuple) adds the NAS store's answer when a *parent* directory is missing —
#: the conversation's whole ``work/`` reaped (B-282) — which is as expired as a missing leaf.
_NOT_THERE: Final = (*MISSING_DIR_ERRORS, WorkspaceNotAFileError)

_VIEW: Final = "conversation_attachment"


def _gone() -> NoReturn:
    raise HTTPException(
        status_code=410,
        detail={
            "code": "ATTACHMENT_GONE",
            "message": "this attachment has expired or was deleted",
        },
    )


def _bytes_response(
    data: bytes, *, filename: str, content_type: str, disposition: ContentDisposition
) -> Response:
    headers = {
        "Content-Disposition": content_disposition_header(filename, disposition=disposition),
        "X-Content-Type-Options": "nosniff",
        # Health data: never kept by a shared cache or the browser's disk cache.
        "Cache-Control": "private, no-store",
    }
    return Response(content=data, media_type=content_type, headers=headers)


def _document_media(leaf: str, data: bytes) -> tuple[str, ContentDisposition]:
    """``(content_type, disposition)`` for a stored document: a PDF only when the bytes
    say so, otherwise the shared whitelist (text inline, active content and unknown
    types as attachments)."""
    if PurePosixPath(leaf).suffix.lower() == ".pdf":
        if data.startswith(_PDF_MAGIC):
            return "application/pdf", "inline"
        return _OPAQUE, "attachment"
    inferred = infer_content_type(path=leaf)
    return inferred.content_type, inferred.disposition


async def _image_response(
    ref: str,
    *,
    meta: ThreadMeta,
    scope: SingleTenant,
    images: ImageUploadStore,
    object_store: ObjectStore | None,
) -> Response:
    try:
        parsed = parse_image_ref(ref)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid attachment ref") from exc
    async with applied_scope(scope):
        row = await images.get(image_id=parsed.image_id, tenant_id=scope.tenant_id)
    # The registry row, not the ref, decides: it must be this conversation's image (an
    # image is only ever used in the thread it was uploaded to). ``get`` already scopes
    # by tenant and does not filter ``deleted_at`` — the caller must.
    if row is None or row.deleted_at is not None or row.thread_id != meta.thread_id:
        _gone()
    if object_store is None:
        raise HTTPException(status_code=503, detail="attachment storage unavailable")
    try:
        data = await object_store.get(row.object_key)
    except ObjectNotFoundError:
        _gone()
    except ObjectStoreError as exc:
        # Subclass order: ObjectNotFoundError above. Readable-but-failing storage is a
        # server problem, not "expired"; the traceback stays in the log.
        logger.warning("session_attachment.object_store_failed", exc_info=True)
        raise HTTPException(status_code=500, detail=_UNAVAILABLE) from exc
    raster = row.mime_type in _RASTER_IMAGE_MIMES
    return _bytes_response(
        data,
        filename=f"{parsed.image_id}{parsed.ext}",
        content_type=row.mime_type if raster else _OPAQUE,
        disposition="inline" if raster else "attachment",
    )


async def _deregistered_in_conversation(
    ref: str, *, meta: ThreadMeta, scope: SingleTenant, uploads: UserUploadStore
) -> bool:
    """Was ``ref`` registered in this thread and every such row deleted since?

    ``False`` both when a live row exists and when there is no row at all: rows are only
    written for uploads into a conversation's own directory since B-281 (external uploads
    always), so "never registered" is an old console upload, not an expired one."""
    async with applied_scope(scope):
        rows = await uploads.list_documents_for_thread_ref(
            tenant_id=scope.tenant_id, thread_id=meta.thread_id, ref=ref
        )
    return bool(rows) and all(row.deleted_at is not None for row in rows)


async def _document_response(
    ref: str,
    *,
    meta: ThreadMeta,
    scope: SingleTenant,
    uploads: UserUploadStore,
    workspace_store: WorkspaceStore | None,
) -> Response:
    if meta.user_id is None:
        # Machine / unowned thread — no per-user volume, so no uploads.
        _gone()
    if thread_conversation_id(meta) is not None and await _deregistered_in_conversation(
        ref, meta=meta, scope=scope, uploads=uploads
    ):
        _gone()
    if workspace_store is None:
        raise HTTPException(status_code=503, detail="attachment storage unavailable")
    try:
        data = await workspace_store.read_file(
            tenant_id=scope.tenant_id, user_id=meta.user_id, path=thread_workspace_path(ref, meta)
        )
    # Every class below subclasses SandboxSupervisorError — the catch-all stays last.
    except WorkspacePermissionError as exc:
        logger.warning("session_attachment.read_permission_denied", exc_info=True)
        raise HTTPException(status_code=500, detail=_UNAVAILABLE) from exc
    except WorkspaceFileTooLargeError as exc:
        logger.warning("session_attachment.file_too_large")
        raise HTTPException(
            status_code=413, detail="attachment exceeds the download size limit"
        ) from exc
    except _NOT_THERE:
        _gone()
    except WorkspacePathEscapeError as exc:
        # A symlink planted where the upload was: refused by the store, and to the
        # reader it is simply not there. Type only — the message carries the path.
        logger.warning("session_attachment.path_escape error=%s", type(exc).__name__)
        _gone()
    except WorkspaceFileChangedError as exc:
        raise HTTPException(
            status_code=409, detail="attachment changed while being read; try again"
        ) from exc
    except SandboxSupervisorError as exc:
        # Supervisor unreachable / unexpected answer: not "expired".
        logger.warning("session_attachment.read_failed", exc_info=True)
        raise HTTPException(status_code=503, detail=_UNAVAILABLE) from exc
    leaf = PurePosixPath(ref).name
    content_type, disposition = _document_media(leaf, data)
    return _bytes_response(data, filename=leaf, content_type=content_type, disposition=disposition)


def _get_thread_repo(request: Request) -> ThreadMetaStore:
    return request.app.state.thread_meta_repo  # type: ignore[no-any-return]


def _get_image_upload_store(request: Request) -> ImageUploadStore:
    return request.app.state.image_upload_store  # type: ignore[no-any-return]


def _get_user_upload_store(request: Request) -> UserUploadStore:
    return request.app.state.user_upload_store  # type: ignore[no-any-return]


def _get_object_store(request: Request) -> ObjectStore | None:
    return getattr(request.app.state, "object_store", None)


def _get_workspace_store(request: Request) -> WorkspaceStore | None:
    return getattr(request.app.state, "workspace_store", None)


def _get_audit(request: Request) -> AuditLogger:
    return request.app.state.audit_logger  # type: ignore[no-any-return]


def build_session_attachments_router() -> APIRouter:
    router = APIRouter(prefix="/v1/sessions", tags=["sessions"])

    @router.get(
        "/{thread_id}/attachment",
        response_model=None,
        dependencies=[
            Depends(require_key_scope("read")),
            Depends(console_only()),
            Depends(require("session", "read")),
        ],
    )
    async def get_session_attachment(
        thread_id: UUID,
        request: Request,
        threads: Annotated[ThreadMetaStore, Depends(_get_thread_repo)],
        users: Annotated[TenantUserStore, Depends(get_user_repo)],
        images: Annotated[ImageUploadStore, Depends(_get_image_upload_store)],
        uploads: Annotated[UserUploadStore, Depends(_get_user_upload_store)],
        object_store: Annotated[ObjectStore | None, Depends(_get_object_store)],
        workspace_store: Annotated[WorkspaceStore | None, Depends(_get_workspace_store)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
        ref: Annotated[str, Query(min_length=1, max_length=_MAX_REF_CHARS)],
        # W2 read scope — a concrete id lets a system_admin read a foreign tenant's
        # conversation from the tenant switcher; "*" is meaningless here.
        tenant_id: Annotated[UUID | Literal["*"] | None, Query()] = None,
    ) -> Response:
        """One attachment of this conversation's user messages, for preview / download."""
        scope = await ensure_single_tenant_scope(
            request.state.principal,
            tenant_id,
            audit,
            trace_id=current_trace_id_hex(),
            endpoint="GET /v1/sessions/{thread_id}/attachment",
            cross_tenant_enabled=cross_tenant_query_enabled(request),
        )
        async with applied_scope(scope):
            meta = await threads.get(thread_id, tenant_id=scope.tenant_id)
        if meta is None:
            raise HTTPException(status_code=404, detail="session not found")
        if ref.startswith(IMAGE_REF_PREFIX):
            response = await _image_response(
                ref, meta=meta, scope=scope, images=images, object_store=object_store
            )
        elif is_safe_document_upload_id(ref):
            response = await _document_response(
                ref, meta=meta, scope=scope, uploads=uploads, workspace_store=workspace_store
            )
        else:
            raise HTTPException(status_code=400, detail="invalid attachment ref")
        await audit_conversation_data_view(
            request,
            audit,
            tenant_id=scope.tenant_id,
            meta=meta,
            caller_user_id=await resolve_caller_user_id(request, users),
            view=_VIEW,
        )
        return response

    return router
