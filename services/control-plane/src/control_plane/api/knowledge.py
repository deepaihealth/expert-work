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
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, Response, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from control_plane.knowledge.ingestion import KnowledgeIngestionRunner
from control_plane.knowledge.parsing import SUPPORTED_EXTENSIONS
from control_plane.tenant_scope import (
    CrossTenant,
    applied_scope,
    cross_tenant_query_enabled,
    ensure_single_tenant_scope,
    ensure_tenant_scope,
)
from expert_work.common.observability import current_trace_id_hex
from expert_work.persistence import KnowledgeStore
from expert_work.persistence.knowledge import UNSET, DuplicateKnowledgeBaseError
from expert_work.protocol import (
    DEFAULT_CHUNK_MAX_TOKENS,
    DEFAULT_CHUNK_OVERLAP_TOKENS,
    DEFAULT_RETRIEVAL_TOP_K,
    KnowledgeBase,
    KnowledgeDocument,
    RetrievalMethod,
)
from expert_work.runtime.audit.logger import AuditLogger

# NOTE: ``KnowledgeRetriever`` lives in ``orchestrator`` — importing it at
# module top level would cycle (control-plane → orchestrator). The
# retrieval-test endpoint depends on the object duck-typed (``Any``); it is
# the same instance the lifespan builds and stashes on ``app.state``.

logger = logging.getLogger("expert_work.control_plane.knowledge")

#: W4 cap for the ``tenant_id=*`` aggregate — ``list_bases`` (single tenant)
#: has no cap; the cross-tenant read is bounded so it can never become an
#: unbounded full-table page.
_ALL_TENANTS_BASES_LIMIT = 200


class _CreateBaseBody(BaseModel):
    """Body of ``POST /v1/knowledge/bases``."""

    name: str = Field(min_length=1, max_length=128)
    description: str | None = Field(default=None, max_length=2000)
    chunk_max_tokens: int | None = Field(default=None, gt=0)
    chunk_overlap_tokens: int | None = Field(default=None, ge=0)
    retrieval_top_k: int | None = Field(default=None, ge=1, le=50)
    retrieval_score_threshold: float | None = Field(default=None, ge=0.0, le=1.0)
    retrieval_method: RetrievalMethod | None = None
    rerank_enabled: bool | None = None


class _UpdateBaseBody(BaseModel):
    """Body of ``PATCH /v1/knowledge/bases/{name}``.

    Only the fields the caller actually sends are applied (decided via
    ``model_fields_set``), so a nullable field can be cleared with an
    explicit ``null`` and left alone by omission. ``name`` is intentionally
    absent — renaming would silently break agent ``knowledge_base_refs``.
    """

    description: str | None = Field(default=None, max_length=2000)
    chunk_max_tokens: int | None = Field(default=None, gt=0)
    chunk_overlap_tokens: int | None = Field(default=None, ge=0)
    retrieval_top_k: int | None = Field(default=None, ge=1, le=50)
    retrieval_score_threshold: float | None = Field(default=None, ge=0.0, le=1.0)
    retrieval_method: RetrievalMethod | None = None
    rerank_enabled: bool | None = None


class _TestBody(BaseModel):
    """Body of ``POST /v1/knowledge/bases/{name}/test`` (retrieval hit-test)."""

    query: str = Field(min_length=1, max_length=4000)
    top_k: int | None = Field(default=None, ge=1, le=50)
    method: RetrievalMethod | None = None
    score_threshold: float | None = Field(default=None, ge=0.0, le=1.0)
    rerank: bool | None = None


def _get_knowledge_store(request: Request) -> KnowledgeStore:
    return request.app.state.knowledge_store  # type: ignore[no-any-return]


def _get_ingestion_runner(request: Request) -> KnowledgeIngestionRunner | None:
    return request.app.state.knowledge_ingestion_runner  # type: ignore[no-any-return]


def _get_knowledge_retriever(request: Request) -> Any | None:
    return getattr(request.app.state, "knowledge_retriever", None)


def _get_embedding_config_service(request: Request) -> Any | None:
    return getattr(request.app.state, "platform_embedding_config_service", None)


def _get_audit(request: Request) -> AuditLogger:
    return request.app.state.audit_logger  # type: ignore[no-any-return]


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
) -> dict[str, Any]:
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
        "retrieval_config": {
            "top_k": base.retrieval_top_k,
            "score_threshold": base.retrieval_score_threshold,
            "method": base.retrieval_method.value,
            "rerank_enabled": base.rerank_enabled,
        },
        "embedding_provider": base.embedding_provider,
        "embedding_model": base.embedding_model,
        "needs_reindex": needs_reindex,
        "reindexing": base.reindex_requested_at is not None,
        "stats": {"document_count": document_count, "chunk_count": chunk_count},
        "created_at": _iso(base.created_at),
        "updated_at": _iso(base.updated_at),
    }


def _document_dict(document: KnowledgeDocument) -> dict[str, Any]:
    return {
        "id": str(document.id),
        "filename": document.filename,
        "status": document.status.value,
        "error": document.error,
        "chunk_count": document.chunk_count,
        "attempts": document.attempts,
        "created_at": _iso(document.created_at),
        "updated_at": _iso(document.updated_at),
    }


async def _require_base(store: KnowledgeStore, tenant_id: UUID, name: str) -> KnowledgeBase:
    base = await store.get_base(tenant_id=tenant_id, name=name)
    if base is None:
        raise HTTPException(status_code=404, detail="knowledge base not found")
    return base


def build_knowledge_router() -> APIRouter:
    router = APIRouter(prefix="/v1/knowledge", tags=["knowledge"])

    @router.post("/bases", response_model=None)
    async def create_base(
        body: _CreateBaseBody,
        request: Request,
        store: Annotated[KnowledgeStore, Depends(_get_knowledge_store)],
        config_service: Annotated[Any | None, Depends(_get_embedding_config_service)],
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
                embedding_provider=provider,
                embedding_model=model,
            )
        except DuplicateKnowledgeBaseError as exc:
            raise HTTPException(status_code=409, detail="knowledge base already exists") from exc
        return JSONResponse(
            status_code=201, content=_base_dict(base, needs_reindex=_needs_reindex(base, current))
        )

    @router.get("/bases", response_model=None)
    async def list_bases(
        request: Request,
        store: Annotated[KnowledgeStore, Depends(_get_knowledge_store)],
        config_service: Annotated[Any | None, Depends(_get_embedding_config_service)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
        # W3 read scope — a concrete id lets a system_admin read a foreign
        # tenant's knowledge bases from the tenant switcher.
        tenant_id: Annotated[UUID | Literal["*"] | None, Query()] = None,
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
        async with applied_scope(scope):
            if isinstance(scope, CrossTenant):
                bases = await store.list_bases_all_tenants(limit=_ALL_TENANTS_BASES_LIMIT)
                stats = await store.base_stats_many_all_tenants(kb_ids=[b.id for b in bases])
            else:
                bases = await store.list_bases(tenant_id=scope.tenant_id)
                stats = await store.base_stats_many(tenant_id=scope.tenant_id)
        current = await _current_embedding_model(config_service)
        return JSONResponse(
            content={
                "bases": [
                    _base_dict(
                        base,
                        stats.get(base.id, (0, 0)),
                        needs_reindex=_needs_reindex(base, current),
                    )
                    for base in bases
                ],
                # W4 response contract — aggregate branch flagged for the UI.
                "cross_tenant": isinstance(scope, CrossTenant),
            }
        )

    @router.get("/bases/{name}", response_model=None)
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
            content=_base_dict(base, stats, needs_reindex=_needs_reindex(base, current))
        )

    @router.patch("/bases/{name}", response_model=None)
    async def update_base(
        name: str,
        body: _UpdateBaseBody,
        request: Request,
        store: Annotated[KnowledgeStore, Depends(_get_knowledge_store)],
        config_service: Annotated[Any | None, Depends(_get_embedding_config_service)],
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
        )
        if updated is None:  # pragma: no cover - guarded by _require_base above
            raise HTTPException(status_code=404, detail="knowledge base not found")
        stats = await store.base_stats(tenant_id=tenant_id, kb_id=updated.id)
        current = await _current_embedding_model(config_service)
        return JSONResponse(
            content=_base_dict(updated, stats, needs_reindex=_needs_reindex(updated, current))
        )

    @router.post("/bases/{name}/reindex", response_model=None)
    async def reindex_base(
        name: str,
        request: Request,
        store: Annotated[KnowledgeStore, Depends(_get_knowledge_store)],
        runner: Annotated[KnowledgeIngestionRunner | None, Depends(_get_ingestion_runner)],
        config_service: Annotated[Any | None, Depends(_get_embedding_config_service)],
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
        return JSONResponse(status_code=202, content={"status": "reindexing", "name": base.name})

    @router.delete("/bases/{name}", status_code=204, response_model=None)
    async def delete_base(
        name: str,
        request: Request,
        store: Annotated[KnowledgeStore, Depends(_get_knowledge_store)],
    ) -> Response:
        tenant_id: UUID = request.state.tenant_id
        base = await _require_base(store, tenant_id, name)
        await store.delete_base(tenant_id=tenant_id, kb_id=base.id)
        return Response(status_code=204)

    @router.post("/bases/{name}/documents", response_model=None)
    async def upload_document(
        name: str,
        request: Request,
        file: Annotated[UploadFile, File()],
        store: Annotated[KnowledgeStore, Depends(_get_knowledge_store)],
        runner: Annotated[KnowledgeIngestionRunner | None, Depends(_get_ingestion_runner)],
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
        raw = await file.read()
        # Retain the original bytes so a crashed/failed document can be
        # re-driven (crash recovery + re-ingest) without a re-upload.
        document = await store.upsert_document(
            tenant_id=tenant_id,
            kb_id=base.id,
            filename=filename,
            content=raw,
            content_sha256=hashlib.sha256(raw).hexdigest(),
        )
        runner.submit(
            tenant_id=tenant_id,
            document_id=document.id,
            kb_id=base.id,
            filename=filename,
            raw=raw,
            chunk_max_tokens=base.chunk_max_tokens,
            chunk_overlap_tokens=base.chunk_overlap_tokens,
        )
        # 202 Accepted — ingestion runs in the background; poll the
        # document list for its status.
        return JSONResponse(status_code=202, content=_document_dict(document))

    @router.get("/bases/{name}/documents", response_model=None)
    async def list_documents(
        name: str,
        request: Request,
        store: Annotated[KnowledgeStore, Depends(_get_knowledge_store)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
        # W3 read scope — subordinate detail read: concrete tenant only.
        tenant_id: Annotated[UUID | Literal["*"] | None, Query()] = None,
    ) -> JSONResponse:
        scope = await ensure_single_tenant_scope(
            request.state.principal,
            tenant_id,
            audit,
            trace_id=current_trace_id_hex(),
            endpoint="GET /v1/knowledge/bases/{name}/documents",
            cross_tenant_enabled=cross_tenant_query_enabled(request),
        )
        async with applied_scope(scope):
            base = await _require_base(store, scope.tenant_id, name)
            documents = await store.list_documents(tenant_id=scope.tenant_id, kb_id=base.id)
        return JSONResponse(content={"documents": [_document_dict(doc) for doc in documents]})

    @router.delete("/bases/{name}/documents/{document_id}", status_code=204, response_model=None)
    async def delete_document(
        name: str,
        document_id: UUID,
        request: Request,
        store: Annotated[KnowledgeStore, Depends(_get_knowledge_store)],
    ) -> Response:
        tenant_id: UUID = request.state.tenant_id
        await _require_base(store, tenant_id, name)
        deleted = await store.delete_document(tenant_id=tenant_id, document_id=document_id)
        if not deleted:
            raise HTTPException(status_code=404, detail="document not found")
        return Response(status_code=204)

    @router.post("/bases/{name}/documents/{document_id}/reingest", response_model=None)
    async def reingest_document(
        name: str,
        document_id: UUID,
        request: Request,
        store: Annotated[KnowledgeStore, Depends(_get_knowledge_store)],
        runner: Annotated[KnowledgeIngestionRunner | None, Depends(_get_ingestion_runner)],
    ) -> JSONResponse:
        tenant_id: UUID = request.state.tenant_id
        base = await _require_base(store, tenant_id, name)
        if runner is None:
            raise HTTPException(
                status_code=503,
                detail="knowledge ingestion unavailable: no embedding model configured",
            )
        document = await store.get_document(tenant_id=tenant_id, document_id=document_id)
        if document is None or document.kb_id != base.id:
            raise HTTPException(status_code=404, detail="document not found")
        content = await store.get_document_content(tenant_id=tenant_id, document_id=document_id)
        if content is None:
            # Legacy document predating retained bytes — cannot re-drive.
            raise HTTPException(
                status_code=409,
                detail="original file not retained; please re-upload this document",
            )
        # Reset to pending (keeps the same bytes) and re-drive the fast path.
        reset = await store.upsert_document(
            tenant_id=tenant_id,
            kb_id=base.id,
            filename=document.filename,
            content=content,
            content_sha256=hashlib.sha256(content).hexdigest(),
        )
        runner.submit(
            tenant_id=tenant_id,
            document_id=reset.id,
            kb_id=base.id,
            filename=document.filename,
            raw=content,
            chunk_max_tokens=base.chunk_max_tokens,
            chunk_overlap_tokens=base.chunk_overlap_tokens,
        )
        return JSONResponse(status_code=202, content=_document_dict(reset))

    @router.get("/bases/{name}/documents/{document_id}/chunks", response_model=None)
    async def list_chunks(
        name: str,
        document_id: UUID,
        request: Request,
        store: Annotated[KnowledgeStore, Depends(_get_knowledge_store)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
        offset: int = 0,
        limit: int = 50,
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
            )
        return JSONResponse(
            content={
                "chunks": [
                    {"id": str(c.id), "chunk_index": c.chunk_index, "content": c.content}
                    for c in chunks
                ],
                "total": total,
                "offset": safe_offset,
                "limit": safe_limit,
            }
        )

    @router.post("/bases/{name}/test", response_model=None)
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
            results = await retriever.search(
                tenant_id=scope.tenant_id,
                base_names=[base.name],
                query=body.query,
                limit=body.top_k or base.retrieval_top_k,
                method=body.method,
                score_threshold=body.score_threshold,
                rerank=body.rerank,
            )
        return JSONResponse(
            content={
                "query": body.query,
                "results": [
                    {
                        "content": r.content,
                        "source": f"{r.filename}#{r.chunk_index}",
                        "filename": r.filename,
                        "chunk_index": r.chunk_index,
                        "score": r.score,
                        "recall_source": r.recall_source,
                    }
                    for r in results
                ],
                "count": len(results),
            }
        )

    return router
