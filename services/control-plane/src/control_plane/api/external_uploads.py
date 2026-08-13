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

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse

from control_plane.agent_disable_status import AgentDisableService
from control_plane.api._authz import external_only, require
from control_plane.api._external import (
    ExternalScopeError,
    external_error,
    load_owned_session,
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
from expert_work.protocol import Principal, QuotaDimension
from expert_work.runtime.audit.logger import AuditLogger
from expert_work.runtime.storage import ObjectStore
from orchestrator.tools import WorkspaceStore

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
                return JSONResponse(
                    status_code=201,
                    content={
                        "success": True,
                        "data": {
                            "upload_id": doc_result.path,
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
            return JSONResponse(
                status_code=201,
                content={
                    "success": True,
                    "data": {
                        "upload_id": image_ref.to_uri(),
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

    return router
