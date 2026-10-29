"""``/v1/knowledge`` — Stream J.5 knowledge-base management + document ingest.

Knowledge bases are tenant-scoped (shared, not per-user). This router
manages bases and their documents:

* bases — create / list / delete;
* documents — upload (async ingest) / list (with status) / delete.

Upload is **off the request path**: the document is recorded ``pending``
and handed to the :class:`KnowledgeIngestionRunner`; the caller polls the
document list for its ``status``.
"""

from __future__ import annotations

import hashlib
import logging
import time
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, Response, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from control_plane.api._authz import console_only, require
from control_plane.api.knowledge_hits import documents_by_id
from control_plane.api.knowledge_rerank_threshold import (
    current_rerank_model,
    model_for_threshold,
    threshold_binding,
)
from control_plane.audit import emit
from control_plane.knowledge.chunking import PARENT_MAX_TOKENS, chunk_params_hash
from control_plane.knowledge.ingestion import KnowledgeIngestionRunner
from control_plane.knowledge.parsing import SUPPORTED_EXTENSIONS
from control_plane.knowledge.recognition.figure_storage import (
    confirmed_figure_ids,
    delete_figures_quietly,
)
from control_plane.settings import Settings
from control_plane.tenant_scope import (
    CrossTenant,
    applied_scope,
    cross_tenant_query_enabled,
    ensure_single_tenant_scope,
    ensure_tenant_scope,
)
from expert_work.common.observability import current_trace_id_hex
from expert_work.persistence import KnowledgeStore
from expert_work.persistence.knowledge import (
    ALL_TENANTS_BASES_LIMIT,
    UNSET,
    DuplicateKnowledgeBaseError,
    KnowledgeRetrievalLogStore,
)
from expert_work.protocol import (
    DEFAULT_CHUNK_MAX_TOKENS,
    DEFAULT_CHUNK_OVERLAP_TOKENS,
    DEFAULT_RETRIEVAL_TOP_K,
    MAX_CHUNK_MAX_TOKENS,
    MIN_CHUNK_MAX_TOKENS,
    AuditAction,
    ChunkType,
    DocumentStatus,
    KnowledgeBase,
    KnowledgeChunk,
    KnowledgeDocument,
    RetrievalMethod,
)
from expert_work.runtime.audit.logger import AuditLogger
from orchestrator.tools.knowledge_log import RetrievalLogContext, retrieval_log_scope

# NOTE: ``KnowledgeRetriever`` lives in ``orchestrator`` — importing it at
# module top level would cycle (control-plane → orchestrator). The
# retrieval-test endpoint depends on the object duck-typed (``Any``); it is
# the same instance the lifespan builds and stashes on ``app.state``.

logger = logging.getLogger("expert_work.control_plane.knowledge")

# W4 cap for the ``tenant_id=*`` aggregate: ``ALL_TENANTS_BASES_LIMIT`` is the
# store-side constant (single source — the store default and the ``truncated``
# flag below must never drift).

#: Document / base list paging. Without ``limit`` the whole list is returned
#: (the manifest editor's knowledge picker still reads every base); an
#: explicit ``limit`` is clamped to the maximum.
DOCUMENTS_PAGE_MAX = 200
BASES_PAGE_MAX = 200

#: Upload is read in slices this large and rejected (413) as soon as the
#: running total passes the configured cap — the whole file is never buffered
#: before the size check.
_UPLOAD_READ_SLICE = 1024 * 1024


class _CreateBaseBody(BaseModel):
    """Body of ``POST /v1/knowledge/bases``."""

    name: str = Field(min_length=1, max_length=128)
    description: str | None = Field(default=None, max_length=2000)
    chunk_max_tokens: int | None = Field(
        default=None, ge=MIN_CHUNK_MAX_TOKENS, le=MAX_CHUNK_MAX_TOKENS
    )
    chunk_overlap_tokens: int | None = Field(default=None, ge=0)
    retrieval_top_k: int | None = Field(default=None, ge=1, le=50)
    retrieval_score_threshold: float | None = Field(default=None, ge=0.0, le=1.0)
    retrieval_method: RetrievalMethod | None = None
    rerank_enabled: bool | None = None
    rerank_score_threshold: float | None = Field(default=None, ge=0.0, le=1.0)


class _UpdateBaseBody(BaseModel):
    """Body of ``PATCH /v1/knowledge/bases/{name}``.

    Only the fields the caller actually sends are applied (decided via
    ``model_fields_set``), so a nullable field can be cleared with an
    explicit ``null`` and left alone by omission. ``name`` is intentionally
    absent — renaming would silently break agent ``knowledge_base_refs``.
    """

    description: str | None = Field(default=None, max_length=2000)
    chunk_max_tokens: int | None = Field(
        default=None, ge=MIN_CHUNK_MAX_TOKENS, le=MAX_CHUNK_MAX_TOKENS
    )
    chunk_overlap_tokens: int | None = Field(default=None, ge=0)
    retrieval_top_k: int | None = Field(default=None, ge=1, le=50)
    retrieval_score_threshold: float | None = Field(default=None, ge=0.0, le=1.0)
    retrieval_method: RetrievalMethod | None = None
    rerank_enabled: bool | None = None
    rerank_score_threshold: float | None = Field(default=None, ge=0.0, le=1.0)
    #: B-206 K-7b — bind the stored threshold, unchanged, to the live rerank model.
    rerank_threshold_confirm: bool = False


class _TestBody(BaseModel):
    """Body of ``POST /v1/knowledge/bases/{name}/test`` (retrieval hit-test)."""

    query: str = Field(min_length=1, max_length=4000)
    top_k: int | None = Field(default=None, ge=1, le=50)
    method: RetrievalMethod | None = None
    score_threshold: float | None = Field(default=None, ge=0.0, le=1.0)
    rerank: bool | None = None
    rerank_score_threshold: float | None = Field(default=None, ge=0.0, le=1.0)


class _CancelBody(BaseModel):
    """Body of ``POST /v1/knowledge/bases/{name}/documents/cancel`` (B-222)."""

    document_ids: list[UUID] = Field(min_length=1, max_length=DOCUMENTS_PAGE_MAX)


def _get_knowledge_store(request: Request) -> KnowledgeStore:
    return request.app.state.knowledge_store  # type: ignore[no-any-return]


def _get_ingestion_runner(request: Request) -> KnowledgeIngestionRunner | None:
    return request.app.state.knowledge_ingestion_runner  # type: ignore[no-any-return]


def _get_knowledge_retriever(request: Request) -> Any | None:
    return getattr(request.app.state, "knowledge_retriever", None)


def _get_retrieval_log_store(request: Request) -> KnowledgeRetrievalLogStore:
    return request.app.state.knowledge_retrieval_log_store  # type: ignore[no-any-return]


def _get_embedding_config_service(request: Request) -> Any | None:
    return getattr(request.app.state, "platform_embedding_config_service", None)


def _get_audit(request: Request) -> AuditLogger:
    return request.app.state.audit_logger  # type: ignore[no-any-return]


def _get_settings(request: Request) -> Settings:
    return request.app.state.settings  # type: ignore[no-any-return]


async def _audit_write(
    audit: AuditLogger,
    request: Request,
    *,
    action: AuditAction,
    resource_type: Literal["knowledge_base", "knowledge_document"],
    resource_id: str,
    details: dict[str, object] | None = None,
) -> None:
    await emit(
        audit,
        tenant_id=request.state.tenant_id,
        actor_id=request.state.principal.subject_id,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        trace_id=current_trace_id_hex(),
        details=details,
    )


async def _read_upload_capped(file: UploadFile, limit_bytes: int) -> bytes:
    """Read the upload in slices; 413 as soon as it exceeds ``limit_bytes``.

    The 413 payload shape is a contract with the admin UI (it reads
    ``detail.limit_bytes`` to print the cap): ``{"error": "file_too_large",
    "limit_bytes": N}``."""
    buffer = bytearray()
    while chunk := await file.read(_UPLOAD_READ_SLICE):
        buffer.extend(chunk)
        if len(buffer) > limit_bytes:
            raise HTTPException(
                status_code=413, detail={"error": "file_too_large", "limit_bytes": limit_bytes}
            )
    return bytes(buffer)


async def _current_embedding_model(config_service: Any | None) -> tuple[str, str] | None:
    if config_service is None:
        return None
    result: tuple[str, str] | None = await config_service.effective_embedding_config()
    return result


def _needs_reindex(base: KnowledgeBase, current: tuple[str, str] | None) -> bool:
    """A base needs re-indexing when its recorded embedding model differs from
    the live platform model. Unknown on either side (legacy base / unconfigured
    platform) → ``False`` (nothing to compare)."""
    if base.embedding_model is None or current is None:
        return False
    return (base.embedding_provider, base.embedding_model) != current


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _base_dict(
    base: KnowledgeBase,
    stats: tuple[int, int] = (0, 0),
    *,
    needs_reindex: bool = False,
    rerank_model: str | None = None,
) -> dict[str, Any]:
    """``rerank_model`` is the live platform rerank model (B-206 K-7b): whether
    the base's rerank-score threshold is in force under it."""
    document_count, chunk_count = stats
    return {
        "id": str(base.id),
        # W4 response contract — every item carries its owning tenant in both
        # the aggregate ("*") and the single-tenant branch.
        "tenant_id": str(base.tenant_id),
        "name": base.name,
        "description": base.description,
        "created_by": base.created_by,
        "chunk_max_tokens": base.chunk_max_tokens,
        "chunk_overlap_tokens": base.chunk_overlap_tokens,
        # B-206 K-3 —— what a document's ``chunk_params_hash`` must equal to be
        # chunked by the current parameters (else: re-ingest it).
        "chunk_params_hash": chunk_params_hash(
            max_tokens=base.chunk_max_tokens, overlap_tokens=base.chunk_overlap_tokens
        ),
        # B-175 P3-PR2 —— read-only: a passage's cap (an edit may be up to twice it).
        "parent_max_tokens": PARENT_MAX_TOKENS,
        "retrieval_config": {
            "top_k": base.retrieval_top_k,
            "score_threshold": base.retrieval_score_threshold,
            "method": base.retrieval_method.value,
            "rerank_enabled": base.rerank_enabled,
            "rerank_score_threshold": base.rerank_score_threshold,
            **threshold_binding(base, rerank_model),
        },
        "embedding_provider": base.embedding_provider,
        "embedding_model": base.embedding_model,
        "needs_reindex": needs_reindex,
        "reindexing": base.reindex_requested_at is not None,
        "stats": {"document_count": document_count, "chunk_count": chunk_count},
        "created_at": _iso(base.created_at),
        "updated_at": _iso(base.updated_at),
    }


#: B-209 —— page records per ``GET …/pages`` response (default / ceiling).
PAGES_PAGE_DEFAULT = 200
PAGES_PAGE_MAX = 1000


def _document_dict(document: KnowledgeDocument) -> dict[str, Any]:
    return {
        "id": str(document.id),
        "filename": document.filename,
        "status": document.status.value,
        "error": document.error,
        "chunk_count": document.chunk_count,
        "attempts": document.attempts,
        "has_pending": document.has_pending,
        "doc_seq": document.doc_seq,
        "title": document.title,
        "source_type": document.source_type.value,
        "stage": document.stage.value if document.stage is not None else None,
        "error_code": document.error_code.value if document.error_code is not None else None,
        "size_bytes": document.size_bytes,
        "mime": document.mime,
        "page_count": document.page_count,
        # B-209 —— 「已识别 N 页 · 失败 M 页 · 待识别 K 页」; null when not a PDF.
        "page_stats": document.page_stats.model_dump(mode="json")
        if document.page_stats is not None
        else None,
        # B-209 PR-2 —— 「已描述 N 张图」; null when ingested without figures.
        "figure_stats": document.figure_stats.model_dump(mode="json")
        if document.figure_stats is not None
        else None,
        # B-206 K-3 —— the chunking of the serving chunks (null: before it was recorded).
        "chunk_params_hash": document.chunk_params_hash,
        "enabled": document.enabled,
        "created_at": _iso(document.created_at),
        "updated_at": _iso(document.updated_at),
    }


def _chunk_dict(chunk: KnowledgeChunk, parent_index: int | None) -> dict[str, Any]:
    """Segment-preview row. ``content`` is the clean body; ``heading_path``
    the section breadcrumb (empty for chunks ingested before it was stored
    apart — their body still carries the ``[Section: …]`` prefix).
    ``parent_index`` is the ``chunk_index`` of the parent row the chunk belongs
    to (B-206 K-1; ``None`` without one, and for a parent row itself)."""
    return {
        "id": str(chunk.id),
        "chunk_index": chunk.chunk_index,
        "content": chunk.content,
        "heading_path": list(chunk.heading_path),
        "chunk_type": chunk.chunk_type.value,
        "enabled": chunk.enabled,
        "edited": chunk.edited,
        "revision": chunk.revision,
        "parent_index": parent_index,
    }


async def _parent_indexes(
    store: KnowledgeStore, tenant_id: UUID, chunks: Sequence[KnowledgeChunk]
) -> list[int | None]:
    """Each chunk's parent row's ``chunk_index`` (``None`` without one) — one
    read for the whole page, none when no chunk has a parent row."""
    ids = list(dict.fromkeys(c.parent_chunk_id for c in chunks if c.parent_chunk_id is not None))
    if not ids:
        return [None] * len(chunks)
    parents = await store.get_chunks(tenant_id=tenant_id, chunk_ids=ids)
    index_of = {parent.id: parent.chunk_index for parent in parents}
    return [None if c.parent_chunk_id is None else index_of.get(c.parent_chunk_id) for c in chunks]


async def _require_base(store: KnowledgeStore, tenant_id: UUID, name: str) -> KnowledgeBase:
    base = await store.get_base(tenant_id=tenant_id, name=name)
    if base is None:
        raise HTTPException(status_code=404, detail="knowledge base not found")
    return base


async def _require_redrivable(
    store: KnowledgeStore,
    runner: KnowledgeIngestionRunner | None,
    tenant_id: UUID,
    base: KnowledgeBase,
    document_id: UUID,
) -> KnowledgeDocument:
    """The base's document, provided ingestion is running and its bytes
    were retained (404 / 409 / 503 otherwise) — shared by 「重新摄取」 and
    「补识别」."""
    if runner is None:
        raise HTTPException(
            status_code=503,
            detail="knowledge ingestion unavailable: no embedding model configured",
        )
    document = await store.get_document(tenant_id=tenant_id, document_id=document_id)
    if document is None or document.kb_id != base.id:
        raise HTTPException(status_code=404, detail="document not found")
    if not document.has_content and not document.has_pending:
        # Legacy document predating retained bytes — cannot re-drive.
        raise HTTPException(
            status_code=409,
            detail="original file not retained; please re-upload this document",
        )
    return document


async def _redrive(
    store: KnowledgeStore,
    runner: KnowledgeIngestionRunner,
    tenant_id: UUID,
    base: KnowledgeBase,
    document: KnowledgeDocument,
) -> KnowledgeDocument:
    # Reset to pending without touching either byte slot and re-drive the
    # fast path: the claim picks the pending upload if there is one (retry
    # a failed new version), else the serving bytes. ``force``: an explicit
    # re-ingest must run even though the bytes are unchanged.
    reset = await store.upsert_document(
        tenant_id=tenant_id,
        kb_id=base.id,
        filename=document.filename,
        force=True,
    )
    runner.submit(tenant_id=tenant_id, document_id=reset.id)
    return reset


def build_knowledge_router() -> APIRouter:
    router = APIRouter(
        prefix="/v1/knowledge", tags=["knowledge"], dependencies=[Depends(console_only())]
    )

    @router.post(
        "/bases", response_model=None, dependencies=[Depends(require("manifest", "write"))]
    )
    async def create_base(
        body: _CreateBaseBody,
        request: Request,
        store: Annotated[KnowledgeStore, Depends(_get_knowledge_store)],
        config_service: Annotated[Any | None, Depends(_get_embedding_config_service)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
    ) -> JSONResponse:
        tenant_id: UUID = request.state.tenant_id
        max_tokens = body.chunk_max_tokens or DEFAULT_CHUNK_MAX_TOKENS
        overlap_tokens = (
            body.chunk_overlap_tokens
            if body.chunk_overlap_tokens is not None
            else DEFAULT_CHUNK_OVERLAP_TOKENS
        )
        if overlap_tokens >= max_tokens:
            raise HTTPException(
                status_code=400,
                detail="chunk_overlap_tokens must be less than chunk_max_tokens",
            )
        # Pin the live platform embedding model so a later model swap is
        # detectable (``needs_reindex``). NULL when embedding is unconfigured.
        current = await _current_embedding_model(config_service)
        provider, model = current if current is not None else (None, None)
        rerank_model = await current_rerank_model(config_service)
        try:
            base = await store.create_base(
                tenant_id=tenant_id,
                name=body.name,
                description=body.description,
                created_by=getattr(request.state, "actor_id", None),
                chunk_max_tokens=max_tokens,
                chunk_overlap_tokens=overlap_tokens,
                retrieval_top_k=body.retrieval_top_k or DEFAULT_RETRIEVAL_TOP_K,
                retrieval_score_threshold=body.retrieval_score_threshold,
                retrieval_method=body.retrieval_method or RetrievalMethod.HYBRID,
                rerank_enabled=body.rerank_enabled if body.rerank_enabled is not None else True,
                rerank_score_threshold=body.rerank_score_threshold,
                embedding_provider=provider,
                embedding_model=model,
                rerank_threshold_model=model_for_threshold(
                    body.rerank_score_threshold, rerank_model
                ),
            )
        except DuplicateKnowledgeBaseError as exc:
            raise HTTPException(status_code=409, detail="knowledge base already exists") from exc
        await _audit_write(
            audit,
            request,
            action=AuditAction.KNOWLEDGE_BASE_CREATE,
            resource_type="knowledge_base",
            resource_id=str(base.id),
            details={"name": base.name},
        )
        return JSONResponse(
            status_code=201,
            content=_base_dict(
                base, needs_reindex=_needs_reindex(base, current), rerank_model=rerank_model
            ),
        )

    @router.get("/bases", response_model=None, dependencies=[Depends(require("manifest", "read"))])
    async def list_bases(
        request: Request,
        store: Annotated[KnowledgeStore, Depends(_get_knowledge_store)],
        config_service: Annotated[Any | None, Depends(_get_embedding_config_service)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
        # W3 read scope — a concrete id lets a system_admin read a foreign
        # tenant's knowledge bases from the tenant switcher.
        tenant_id: Annotated[UUID | Literal["*"] | None, Query()] = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> JSONResponse:
        # W4 — real cross-tenant aggregate: "*" reads every tenant via the
        # store's all-tenants readers; a concrete id keeps the W3 single-tenant
        # read (see members.py for the pattern).
        scope = await ensure_tenant_scope(
            request.state.principal,
            tenant_id,
            audit,
            trace_id=current_trace_id_hex(),
            endpoint="GET /v1/knowledge/bases",
            cross_tenant_enabled=cross_tenant_query_enabled(request),
        )
        safe_offset = max(0, offset)
        safe_limit = max(1, min(limit, BASES_PAGE_MAX)) if limit is not None else None
        async with applied_scope(scope):
            if isinstance(scope, CrossTenant):
                if safe_limit is None:
                    bases = await store.list_bases_all_tenants(limit=ALL_TENANTS_BASES_LIMIT)
                    total = len(bases)
                else:
                    bases, total = await store.list_bases_all_tenants_page(
                        limit=safe_limit, offset=safe_offset
                    )
                stats = await store.base_stats_many_all_tenants(kb_ids=[b.id for b in bases])
            else:
                if safe_limit is None:
                    bases = await store.list_bases(tenant_id=scope.tenant_id)
                    total = len(bases)
                else:
                    bases, total = await store.list_bases_page(
                        tenant_id=scope.tenant_id, limit=safe_limit, offset=safe_offset
                    )
                stats = await store.base_stats_many(tenant_id=scope.tenant_id)
        current = await _current_embedding_model(config_service)
        rerank_model = await current_rerank_model(config_service)
        return JSONResponse(
            content={
                "bases": [
                    _base_dict(
                        base,
                        stats.get(base.id, (0, 0)),
                        needs_reindex=_needs_reindex(base, current),
                        rerank_model=rerank_model,
                    )
                    for base in bases
                ],
                # W4 response contract — aggregate branch flagged for the UI.
                "cross_tenant": isinstance(scope, CrossTenant),
                # W4 review #3 — a full aggregate page signals the cap was hit
                # (the single-tenant branch has no cap, and a paged read
                # carries the real ``total``, so neither is ever truncated).
                "truncated": isinstance(scope, CrossTenant)
                and safe_limit is None
                and len(bases) == ALL_TENANTS_BASES_LIMIT,
                "total": total,
                # Unpaged reads ignore ``offset`` (always the whole list).
                "offset": safe_offset if safe_limit is not None else 0,
                "limit": safe_limit,
            }
        )

    @router.get(
        "/bases/{name}", response_model=None, dependencies=[Depends(require("manifest", "read"))]
    )
    async def get_base(
        name: str,
        request: Request,
        store: Annotated[KnowledgeStore, Depends(_get_knowledge_store)],
        config_service: Annotated[Any | None, Depends(_get_embedding_config_service)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
        # W3 read scope — "*" is meaningless (a base belongs to one tenant).
        tenant_id: Annotated[UUID | Literal["*"] | None, Query()] = None,
    ) -> JSONResponse:
        scope = await ensure_single_tenant_scope(
            request.state.principal,
            tenant_id,
            audit,
            trace_id=current_trace_id_hex(),
            endpoint="GET /v1/knowledge/bases/{name}",
            cross_tenant_enabled=cross_tenant_query_enabled(request),
        )
        async with applied_scope(scope):
            base = await _require_base(store, scope.tenant_id, name)
            stats = await store.base_stats(tenant_id=scope.tenant_id, kb_id=base.id)
        current = await _current_embedding_model(config_service)
        return JSONResponse(
            content=_base_dict(
                base,
                stats,
                needs_reindex=_needs_reindex(base, current),
                rerank_model=await current_rerank_model(config_service),
            )
        )

    @router.patch(
        "/bases/{name}", response_model=None, dependencies=[Depends(require("manifest", "write"))]
    )
    async def update_base(
        name: str,
        body: _UpdateBaseBody,
        request: Request,
        store: Annotated[KnowledgeStore, Depends(_get_knowledge_store)],
        config_service: Annotated[Any | None, Depends(_get_embedding_config_service)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
    ) -> JSONResponse:
        tenant_id: UUID = request.state.tenant_id
        base = await _require_base(store, tenant_id, name)
        sent = body.model_fields_set
        # Resolve the effective overlap/max for the cross-field check, falling
        # back to the stored values for whichever side the caller omitted (or
        # sent as null — these columns are non-nullable, so null = leave alone).
        new_max = (
            body.chunk_max_tokens
            if "chunk_max_tokens" in sent and body.chunk_max_tokens is not None
            else base.chunk_max_tokens
        )
        new_overlap = (
            body.chunk_overlap_tokens
            if "chunk_overlap_tokens" in sent and body.chunk_overlap_tokens is not None
            else base.chunk_overlap_tokens
        )
        if new_overlap >= new_max:
            raise HTTPException(
                status_code=400,
                detail="chunk_overlap_tokens must be less than chunk_max_tokens",
            )
        rerank_model = await current_rerank_model(config_service)
        updated = await store.update_base(
            tenant_id=tenant_id,
            kb_id=base.id,
            description=body.description if "description" in sent else UNSET,
            chunk_max_tokens=body.chunk_max_tokens if "chunk_max_tokens" in sent else None,
            chunk_overlap_tokens=(
                body.chunk_overlap_tokens if "chunk_overlap_tokens" in sent else None
            ),
            retrieval_top_k=body.retrieval_top_k if "retrieval_top_k" in sent else None,
            retrieval_score_threshold=(
                body.retrieval_score_threshold if "retrieval_score_threshold" in sent else UNSET
            ),
            retrieval_method=body.retrieval_method if "retrieval_method" in sent else None,
            rerank_enabled=body.rerank_enabled if "rerank_enabled" in sent else None,
            rerank_score_threshold=(
                body.rerank_score_threshold if "rerank_score_threshold" in sent else UNSET
            ),
            rerank_threshold_model=(
                model_for_threshold(body.rerank_score_threshold, rerank_model)
                if "rerank_score_threshold" in sent
                else model_for_threshold(base.rerank_score_threshold, rerank_model)
                if body.rerank_threshold_confirm
                else UNSET
            ),
        )
        if updated is None:  # pragma: no cover - guarded by _require_base above
            raise HTTPException(status_code=404, detail="knowledge base not found")
        await _audit_write(
            audit,
            request,
            action=AuditAction.KNOWLEDGE_BASE_UPDATE,
            resource_type="knowledge_base",
            resource_id=str(updated.id),
            details={"name": updated.name, "fields": sorted(sent)},
        )
        stats = await store.base_stats(tenant_id=tenant_id, kb_id=updated.id)
        current = await _current_embedding_model(config_service)
        return JSONResponse(
            content=_base_dict(
                updated,
                stats,
                needs_reindex=_needs_reindex(updated, current),
                rerank_model=rerank_model,
            )
        )

    @router.post(
        "/bases/{name}/reindex",
        response_model=None,
        dependencies=[Depends(require("manifest", "write"))],
    )
    async def reindex_base(
        name: str,
        request: Request,
        store: Annotated[KnowledgeStore, Depends(_get_knowledge_store)],
        runner: Annotated[KnowledgeIngestionRunner | None, Depends(_get_ingestion_runner)],
        config_service: Annotated[Any | None, Depends(_get_embedding_config_service)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
    ) -> JSONResponse:
        tenant_id: UUID = request.state.tenant_id
        base = await _require_base(store, tenant_id, name)
        if runner is None:
            raise HTTPException(
                status_code=503,
                detail="knowledge ingestion unavailable: no embedding model configured",
            )
        current = await _current_embedding_model(config_service)
        if current is None:
            raise HTTPException(
                status_code=503,
                detail="knowledge ingestion unavailable: no embedding model configured",
            )
        provider, model = current
        # Mark in-flight (UI shows "re-indexing"); the runner re-embeds retained
        # chunk text, stamps the model, and clears the flag on completion.
        await store.request_reindex(tenant_id=tenant_id, kb_id=base.id)
        runner.submit_reindex(
            tenant_id=tenant_id,
            kb_id=base.id,
            embedding_provider=provider,
            embedding_model=model,
        )
        await _audit_write(
            audit,
            request,
            action=AuditAction.KNOWLEDGE_BASE_REINDEX,
            resource_type="knowledge_base",
            resource_id=str(base.id),
            details={"name": base.name, "embedding_provider": provider, "embedding_model": model},
        )
        return JSONResponse(status_code=202, content={"status": "reindexing", "name": base.name})

    @router.delete(
        "/bases/{name}",
        status_code=204,
        response_model=None,
        dependencies=[Depends(require("manifest", "delete"))],
    )
    async def delete_base(
        name: str,
        request: Request,
        store: Annotated[KnowledgeStore, Depends(_get_knowledge_store)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
        log_store: Annotated[KnowledgeRetrievalLogStore, Depends(_get_retrieval_log_store)],
    ) -> Response:
        tenant_id: UUID = request.state.tenant_id
        base = await _require_base(store, tenant_id, name)
        # B-175 P2-PR4 — the base's retrieval logs (questions asked of it) go
        # with it: first, so a failure leaves the base to retry the delete on …
        try:
            await log_store.delete_for_base(tenant_id=tenant_id, kb_id=base.id)
        except Exception as exc:
            logger.warning("knowledge.base_delete_logs_failed error=%s", type(exc).__name__)
            raise HTTPException(
                status_code=503,
                detail="retrieval logs could not be deleted; the base was kept, retry",
            ) from exc
        await store.delete_base(tenant_id=tenant_id, kb_id=base.id)
        # … and again after it, for a search that logged while it was going.
        try:
            await log_store.delete_for_base(tenant_id=tenant_id, kb_id=base.id)
        except Exception as exc:  # best-effort: retention removes a straggler
            logger.warning("knowledge.base_delete_logs_failed error=%s", type(exc).__name__)
        # B-209 PR-2 — the base's original figures go with it.
        await delete_figures_quietly(
            request.app.state.object_store, tenant_id=tenant_id, kb_id=base.id
        )
        await _audit_write(
            audit,
            request,
            action=AuditAction.KNOWLEDGE_BASE_DELETE,
            resource_type="knowledge_base",
            resource_id=str(base.id),
            details={"name": base.name},
        )
        return Response(status_code=204)

    @router.post(
        "/bases/{name}/documents",
        response_model=None,
        dependencies=[Depends(require("manifest", "write"))],
    )
    async def upload_document(
        name: str,
        request: Request,
        file: Annotated[UploadFile, File()],
        store: Annotated[KnowledgeStore, Depends(_get_knowledge_store)],
        runner: Annotated[KnowledgeIngestionRunner | None, Depends(_get_ingestion_runner)],
        settings: Annotated[Settings, Depends(_get_settings)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
    ) -> JSONResponse:
        tenant_id: UUID = request.state.tenant_id
        base = await _require_base(store, tenant_id, name)
        if runner is None:
            raise HTTPException(
                status_code=503,
                detail="knowledge ingestion unavailable: no embedding model configured",
            )
        filename = file.filename
        if not filename:
            raise HTTPException(status_code=400, detail="uploaded file has no filename")
        if Path(filename).suffix.lower() not in SUPPORTED_EXTENSIONS:
            raise HTTPException(status_code=400, detail=f"unsupported document type: {filename!r}")
        raw = await _read_upload_capped(file, settings.knowledge_document_max_bytes)
        # Retain the original bytes so a crashed/failed document can be
        # re-driven (crash recovery + re-ingest) without a re-upload.
        document = await store.upsert_document(
            tenant_id=tenant_id,
            kb_id=base.id,
            filename=filename,
            content=raw,
            content_sha256=hashlib.sha256(raw).hexdigest(),
        )
        # Same bytes as the READY document already there → nothing to ingest:
        # 200 + ``unchanged`` (the store left status / chunks untouched).
        unchanged = document.status is DocumentStatus.READY
        if not unchanged:
            # The runner re-reads bytes + chunking parameters at claim time,
            # so a re-upload that lands while this task is queued wins.
            runner.submit(tenant_id=tenant_id, document_id=document.id)
        await _audit_write(
            audit,
            request,
            action=AuditAction.KNOWLEDGE_DOCUMENT_UPLOAD,
            resource_type="knowledge_document",
            resource_id=str(document.id),
            details={
                "kb": base.name,
                "filename": filename,
                "size_bytes": len(raw),
                "unchanged": unchanged,
            },
        )
        # 202 Accepted — ingestion runs in the background; poll the
        # document list for its status. 200 when nothing changed.
        return JSONResponse(
            status_code=200 if unchanged else 202,
            content={**_document_dict(document), "unchanged": unchanged},
        )

    @router.get(
        "/bases/{name}/documents",
        response_model=None,
        dependencies=[Depends(require("manifest", "read"))],
    )
    async def list_documents(
        name: str,
        request: Request,
        store: Annotated[KnowledgeStore, Depends(_get_knowledge_store)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
        limit: int | None = None,
        offset: int = 0,
        # W3 read scope — subordinate detail read: concrete tenant only.
        tenant_id: Annotated[UUID | Literal["*"] | None, Query()] = None,
        # B-175 P3-PR2 — only the documents switched that way / in these statuses.
        enabled: bool | None = None,
        status: Annotated[list[DocumentStatus] | None, Query()] = None,
    ) -> JSONResponse:
        scope = await ensure_single_tenant_scope(
            request.state.principal,
            tenant_id,
            audit,
            trace_id=current_trace_id_hex(),
            endpoint="GET /v1/knowledge/bases/{name}/documents",
            cross_tenant_enabled=cross_tenant_query_enabled(request),
        )
        safe_offset = max(0, offset)
        safe_limit = max(1, min(limit, DOCUMENTS_PAGE_MAX)) if limit is not None else None
        async with applied_scope(scope):
            base = await _require_base(store, scope.tenant_id, name)
            if safe_limit is None:
                everything = await store.list_documents(
                    tenant_id=scope.tenant_id, kb_id=base.id, enabled=enabled, statuses=status
                )
                documents, total = everything[safe_offset:], len(everything)
            else:
                documents, total = await store.list_documents_page(
                    tenant_id=scope.tenant_id,
                    kb_id=base.id,
                    limit=safe_limit,
                    offset=safe_offset,
                    enabled=enabled,
                    statuses=status,
                )
        return JSONResponse(
            content={
                "documents": [_document_dict(doc) for doc in documents],
                "total": total,
                "offset": safe_offset,
                "limit": safe_limit,
            }
        )

    @router.delete(
        "/bases/{name}/documents/{document_id}",
        status_code=204,
        response_model=None,
        dependencies=[Depends(require("manifest", "delete"))],
    )
    async def delete_document(
        name: str,
        document_id: UUID,
        request: Request,
        store: Annotated[KnowledgeStore, Depends(_get_knowledge_store)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
    ) -> Response:
        tenant_id: UUID = request.state.tenant_id
        base = await _require_base(store, tenant_id, name)
        # B-209 PR-2 — its original figures go with it (keyed under its base).
        document = await store.get_document(tenant_id=tenant_id, document_id=document_id)
        deleted = await store.delete_document(tenant_id=tenant_id, document_id=document_id)
        if not deleted:
            raise HTTPException(status_code=404, detail="document not found")
        await delete_figures_quietly(
            request.app.state.object_store,
            tenant_id=tenant_id,
            kb_id=document.kb_id if document is not None else base.id,
            document_id=document_id,
        )
        await _audit_write(
            audit,
            request,
            action=AuditAction.KNOWLEDGE_DOCUMENT_DELETE,
            resource_type="knowledge_document",
            resource_id=str(document_id),
            details={"kb": base.name},
        )
        return Response(status_code=204)

    @router.post(
        "/bases/{name}/documents/{document_id}/reingest",
        response_model=None,
        dependencies=[Depends(require("manifest", "write"))],
    )
    async def reingest_document(
        name: str,
        document_id: UUID,
        request: Request,
        store: Annotated[KnowledgeStore, Depends(_get_knowledge_store)],
        runner: Annotated[KnowledgeIngestionRunner | None, Depends(_get_ingestion_runner)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
    ) -> JSONResponse:
        tenant_id: UUID = request.state.tenant_id
        base = await _require_base(store, tenant_id, name)
        document = await _require_redrivable(store, runner, tenant_id, base, document_id)
        assert runner is not None  # noqa: S101 — checked above
        reset = await _redrive(store, runner, tenant_id, base, document)
        await _audit_write(
            audit,
            request,
            action=AuditAction.KNOWLEDGE_DOCUMENT_REINGEST,
            resource_type="knowledge_document",
            resource_id=str(reset.id),
            details={"kb": base.name, "filename": document.filename},
        )
        return JSONResponse(status_code=202, content=_document_dict(reset))

    @router.post(
        "/bases/{name}/documents/{document_id}/recognize",
        response_model=None,
        dependencies=[Depends(require("manifest", "write"))],
    )
    async def recognize_document(
        name: str,
        document_id: UUID,
        request: Request,
        store: Annotated[KnowledgeStore, Depends(_get_knowledge_store)],
        runner: Annotated[KnowledgeIngestionRunner | None, Depends(_get_ingestion_runner)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
    ) -> JSONResponse:
        """B-209 §5.6 「补识别」 —— re-drive a document whose scanned pages failed
        or waited for recognition (e.g. the model was configured since). The
        whole document goes through the pipeline again, but pages already
        recognized come from the recognition cache: only the failed / waiting
        pages are sent to a model (and billed)."""
        tenant_id: UUID = request.state.tenant_id
        base = await _require_base(store, tenant_id, name)
        document = await _require_redrivable(store, runner, tenant_id, base, document_id)
        assert runner is not None  # noqa: S101 — checked above
        pages = document.page_stats
        figures = document.figure_stats  # B-209 PR-2: pictures waiting / failed too
        if not (pages is not None and pages.needs_recognition) and not (
            figures is not None and figures.needs_description
        ):
            raise HTTPException(
                status_code=409,
                detail="document has no page or picture that failed, waits or is past the limit",
            )
        reset = await _redrive(store, runner, tenant_id, base, document)
        await _audit_write(
            audit,
            request,
            action=AuditAction.KNOWLEDGE_DOCUMENT_RECOGNIZE,
            resource_type="knowledge_document",
            resource_id=str(reset.id),
            details={
                "kb": base.name,
                "filename": document.filename,
                "pages_failed": pages.pages_failed if pages is not None else 0,
                "pages_pending": pages.pages_pending if pages is not None else 0,
                "figures_failed": figures.failed if figures is not None else 0,
                "figures_pending": figures.pending if figures is not None else 0,
            },
        )
        return JSONResponse(status_code=202, content=_document_dict(reset))

    @router.get(
        "/bases/{name}/documents/{document_id}/pages",
        response_model=None,
        dependencies=[Depends(require("manifest", "read"))],
    )
    async def list_document_pages(
        name: str,
        document_id: UUID,
        request: Request,
        store: Annotated[KnowledgeStore, Depends(_get_knowledge_store)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
        offset: int = 0,
        limit: int = PAGES_PAGE_DEFAULT,
        # W3 read scope — subordinate detail read: concrete tenant only.
        tenant_id: Annotated[UUID | Literal["*"] | None, Query()] = None,
    ) -> JSONResponse:
        """B-209 §5.6 —— the document's per-page records (how each PDF page's
        text was obtained; for a scanned page the model, outcome and error
        class), a page of them at a time (``offset`` / ``limit``, at most
        :data:`PAGES_PAGE_MAX`). Empty for a document that is not a PDF."""
        scope = await ensure_single_tenant_scope(
            request.state.principal,
            tenant_id,
            audit,
            trace_id=current_trace_id_hex(),
            endpoint="GET /v1/knowledge/bases/{name}/documents/{document_id}/pages",
            cross_tenant_enabled=cross_tenant_query_enabled(request),
        )
        async with applied_scope(scope):
            base = await _require_base(store, scope.tenant_id, name)
            document = await store.get_document(tenant_id=scope.tenant_id, document_id=document_id)
            if document is None or document.kb_id != base.id:
                raise HTTPException(status_code=404, detail="document not found")
            pages = await store.list_document_pages(
                tenant_id=scope.tenant_id, document_id=document_id
            )
        records = pages or []
        safe_offset = max(0, offset)
        safe_limit = max(1, min(limit, PAGES_PAGE_MAX))
        return JSONResponse(
            content={
                "pages": [
                    page.model_dump(mode="json")
                    for page in records[safe_offset : safe_offset + safe_limit]
                ],
                "total": len(records),
                "offset": safe_offset,
                "limit": safe_limit,
                "page_stats": _document_dict(document)["page_stats"],
            }
        )

    @router.post(
        "/bases/{name}/documents/{document_id}/discard-pending",
        response_model=None,
        dependencies=[Depends(require("manifest", "write"))],
    )
    async def discard_pending(
        name: str,
        document_id: UUID,
        request: Request,
        store: Annotated[KnowledgeStore, Depends(_get_knowledge_store)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
    ) -> JSONResponse:
        """Drop a pending (failed or in-flight) upload and keep serving the
        previous version — the way back when a re-upload turned out bad."""
        tenant_id: UUID = request.state.tenant_id
        base = await _require_base(store, tenant_id, name)
        document = await store.get_document(tenant_id=tenant_id, document_id=document_id)
        if document is None or document.kb_id != base.id:
            raise HTTPException(status_code=404, detail="document not found")
        if not document.has_pending:
            raise HTTPException(status_code=409, detail="document has no pending upload")
        restored = await store.discard_pending(tenant_id=tenant_id, document_id=document_id)
        if restored is None:  # pragma: no cover - checked just above
            raise HTTPException(status_code=404, detail="document not found")
        if restored.has_pending:
            # B-222: its chunks are being swapped in (processing at indexing) —
            # the new version is about to serve; the store left it alone.
            raise HTTPException(
                status_code=409,
                detail="the new version is being written and about to finish; it can't be dropped",
            )
        await _audit_write(
            audit,
            request,
            action=AuditAction.KNOWLEDGE_DOCUMENT_DISCARD_PENDING,
            resource_type="knowledge_document",
            resource_id=str(document_id),
            details={"kb": base.name, "filename": document.filename},
        )
        return JSONResponse(content=_document_dict(restored))

    @router.post(
        "/bases/{name}/documents/cancel",
        response_model=None,
        dependencies=[Depends(require("manifest", "write"))],
    )
    async def cancel_documents(
        name: str,
        body: _CancelBody,
        request: Request,
        store: Annotated[KnowledgeStore, Depends(_get_knowledge_store)],
        runner: Annotated[KnowledgeIngestionRunner | None, Depends(_get_ingestion_runner)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
    ) -> JSONResponse:
        """B-222 —— stop the ingest of the base's ``pending`` / ``processing``
        documents. Per document: ``cancelled``, ``not_cancellable`` (already
        settled, or already writing its chunks — about to finish) or
        ``not_found``. What was written stays (recognition and topic-break
        caches: a re-ingest does not pay for them again); the version serving
        before, if any, keeps serving. An ingest this pod drives (upload or
        recovery) stops at once; elsewhere at its next batch."""
        tenant_id: UUID = request.state.tenant_id
        base = await _require_base(store, tenant_id, name)
        outcomes = await store.cancel_documents(
            tenant_id=tenant_id, kb_id=base.id, document_ids=body.document_ids
        )
        cancelled = [doc_id for doc_id, outcome in outcomes.items() if outcome == "cancelled"]
        if runner is not None and cancelled:
            runner.cancel_local(cancelled)
        for document_id in cancelled:
            await _audit_write(
                audit,
                request,
                action=AuditAction.KNOWLEDGE_DOCUMENT_CANCEL,
                resource_type="knowledge_document",
                resource_id=str(document_id),
                details={"kb": base.name},
            )
        return JSONResponse(
            content={
                "results": [
                    {"document_id": str(doc_id), "outcome": outcome}
                    for doc_id, outcome in outcomes.items()
                ]
            }
        )

    @router.get(
        "/bases/{name}/documents/{document_id}/chunks",
        response_model=None,
        dependencies=[Depends(require("manifest", "read"))],
    )
    async def list_chunks(
        name: str,
        document_id: UUID,
        request: Request,
        store: Annotated[KnowledgeStore, Depends(_get_knowledge_store)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
        offset: int = 0,
        limit: int = 50,
        # B-206 K-1 — ``children``: the searchable chunks, each naming its parent
        # row (``parent_index``); ``parents``: the parent rows themselves.
        view: Literal["children", "parents"] = "children",
        # W3 read scope — subordinate detail read: concrete tenant only.
        tenant_id: Annotated[UUID | Literal["*"] | None, Query()] = None,
    ) -> JSONResponse:
        scope = await ensure_single_tenant_scope(
            request.state.principal,
            tenant_id,
            audit,
            trace_id=current_trace_id_hex(),
            endpoint="GET /v1/knowledge/bases/{name}/documents/{document_id}/chunks",
            cross_tenant_enabled=cross_tenant_query_enabled(request),
        )
        safe_offset = max(0, offset)
        safe_limit = max(1, min(limit, 200))
        async with applied_scope(scope):
            base = await _require_base(store, scope.tenant_id, name)
            document = await store.get_document(tenant_id=scope.tenant_id, document_id=document_id)
            if document is None or document.kb_id != base.id:
                raise HTTPException(status_code=404, detail="document not found")
            chunks, total = await store.list_chunks(
                tenant_id=scope.tenant_id,
                document_id=document_id,
                offset=safe_offset,
                limit=safe_limit,
                chunk_types=(ChunkType.PARENT,) if view == "parents" else None,
            )
            parent_indexes = await _parent_indexes(store, scope.tenant_id, chunks)
        # B-209 PR-2 —— figures whose originals this document really has.
        figure_ids = await confirmed_figure_ids(
            request.app.state.object_store,
            tenant_id=scope.tenant_id,
            kb_id=base.id,
            document_id=document_id,
            contents=[c.content for c in chunks],
        )
        return JSONResponse(
            content={
                "chunks": [
                    _chunk_dict(c, parent_index)
                    for c, parent_index in zip(chunks, parent_indexes, strict=True)
                ],
                "figure_ids": figure_ids,
                "total": total,
                "offset": safe_offset,
                "limit": safe_limit,
            }
        )

    @router.post(
        "/bases/{name}/test",
        response_model=None,
        # B-49 —— 阶段 1.5 按「POST 只是因为查询走 body,什么都没改」判成
        # ``manifest:read``。但这条会真打一次 embedding + 向量检索:对外是花钱的
        # 调用,不是免费的读。抬到 ``manifest:write``(operator+),与本 router 里
        # 其它会触发 embedding 的写(``reindex`` / ``reingest``)同档。
        dependencies=[Depends(require("manifest", "write"))],
    )
    async def test_retrieval(
        name: str,
        body: _TestBody,
        request: Request,
        store: Annotated[KnowledgeStore, Depends(_get_knowledge_store)],
        retriever: Annotated[Any | None, Depends(_get_knowledge_retriever)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
        # W3 read scope — read-only retrieval hit-test: concrete tenant only.
        tenant_id: Annotated[UUID | Literal["*"] | None, Query()] = None,
    ) -> JSONResponse:
        scope = await ensure_single_tenant_scope(
            request.state.principal,
            tenant_id,
            audit,
            trace_id=current_trace_id_hex(),
            endpoint="POST /v1/knowledge/bases/{name}/test",
            cross_tenant_enabled=cross_tenant_query_enabled(request),
        )
        async with applied_scope(scope):
            base = await _require_base(store, scope.tenant_id, name)
            # AFTER the base lookup — a missing base stays 404 even when the
            # retriever is unconfigured (pre-W3 error priority).
            if retriever is None:
                raise HTTPException(
                    status_code=503,
                    detail="knowledge retrieval unavailable: no embedding model configured",
                )
            # B-175 P2-PR4 — logged as this employee's console test; the receipt
            # brings back the search's latency and the row id once it is written.
            # A caller searching another tenant's base than their home one
            # (platform staff, switched in) is marked on the row, so that tenant
            # sees who searched (user ruling 2026-10-28).
            principal = request.state.principal
            caller = RetrievalLogContext(
                source="console_test",
                actor_id=principal.subject_id,
                flags={"platform_admin": True} if scope.tenant_id != principal.tenant_id else {},
            )
            started = time.monotonic()
            with retrieval_log_scope(caller) as receipt:
                outcome = await retriever.search_detailed(
                    tenant_id=scope.tenant_id,
                    base_names=[base.name],
                    query=body.query,
                    limit=body.top_k or base.retrieval_top_k,
                    method=body.method,
                    score_threshold=body.score_threshold,
                    rerank=body.rerank,
                    rerank_score_threshold=body.rerank_score_threshold,
                )
            # A retriever that logs nothing reports no latency: time it here.
            elapsed_ms = (
                receipt.latency_ms
                if receipt.latency_ms is not None
                else int((time.monotonic() - started) * 1000)
            )
            documents = await documents_by_id(
                store, scope.tenant_id, (r.document_id for r in outcome.results if r.document_id)
            )
        return JSONResponse(
            content={
                "query": body.query,
                "results": [
                    {
                        "content": r.content,
                        "heading_path": list(r.heading_path),
                        "source": f"{r.filename}#{r.chunk_index}",
                        "filename": r.filename,
                        "chunk_index": r.chunk_index,
                        "score": r.score,
                        "rerank_score": r.rerank_score,
                        "recall_source": r.recall_source,
                        # B-206 K-1 — ``content`` / ``heading_path`` are the parent
                        # row's (``parent_index``, ``null`` when the hit has none);
                        # the scores and ``chunk_index`` are its best hit child's,
                        # ``hit_chunk_indexes`` every hit child (best first) and
                        # ``hit_content`` the best one's own body (``null`` when
                        # the result is that child itself).
                        "parent_index": r.parent_index,
                        "hit_chunk_indexes": list(r.hit_chunk_indexes),
                        "hit_content": r.hit_content,
                        "hit_heading_path": list(r.hit_heading_path),
                        # B-175 P2-PR4 (B-206 §6) — the best hit child's four
                        # scores (``score`` is ``vector_score``), and its document
                        # (``null`` once deleted).
                        "vector_score": r.vector_score,
                        "keyword_score": r.keyword_score,
                        "fused_score": r.fused_score,
                        "document": documents.get(r.document_id) if r.document_id else None,
                    }
                    for r in outcome.results
                ],
                "count": len(outcome.results),
                # B-175 wave 2 PR3 — how the search ended: ok / no_hits /
                # below_threshold (every hit under the rerank-score threshold,
                # ``candidates`` of them); rerank failure degrades to the fused
                # order instead of a 500.
                "status": outcome.status,
                "candidates": outcome.candidates,
                "rerank_degraded": outcome.rerank_degraded,
                # B-206 K-7 — how the rerank ended (null when none was asked for).
                "rerank_outcome": outcome.rerank_outcome,
                # B-206 K-8 — the query could not be embedded: keyword results only.
                "embedding_degraded": outcome.embedding_degraded,
                # B-206 K-22 — the keyword path could not finish (its statement
                # timeout, a lost connection): the other path's results only.
                "keyword_degraded": outcome.keyword_degraded,
                # B-206 K-21② — passages (parents, or children shown as
                # themselves; not child chunks) left out as a copy of a
                # better-ranked one.
                "duplicates_dropped": outcome.duplicates_dropped,
                # B-206 K-14 — candidates the reranker was handed but never
                # judged (its model's per-request cap, or a length refusal).
                "rerank_dropped": outcome.rerank_dropped,
                # B-206 K-7b — the base's threshold was set under another rerank
                # model than the one that scored, so this search left it out (both
                # named here, so the notice never contradicts the search).
                "rerank_threshold_paused": outcome.rerank_threshold_paused,
                "rerank_threshold_model": base.rerank_threshold_model,
                "rerank_model": outcome.rerank_model,
                # B-175 P2-PR4 — how long the search took (not its logging), and
                # its log row (``null`` when none was written).
                "elapsed_ms": elapsed_ms,
                "log_id": str(receipt.log_id) if receipt.log_id is not None else None,
            }
        )

    return router
