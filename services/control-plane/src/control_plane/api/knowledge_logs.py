"""``/v1/knowledge/bases/{name}/retrieval-logs`` — B-175 P2-PR4.

Every knowledge search — an agent's ``knowledge_search`` and the console's
retrieval test — leaves a ``knowledge_retrieval_log`` row (see
:mod:`orchestrator.tools.knowledge_log`). This router reads them per base: a
page newest first (filtered by source / status / zero hits / time) and one row
with each result's document resolved. A search that spanned several bases (an
agent bound to more than one) is listed under each of them, and its
``result_count`` / ``results`` cover all of them — a result's document may
belong to another of its bases (``kb_ids`` in the detail lists them).
``status`` is the search's (``ok`` / ``no_hits`` / ``below_threshold``), or
``error`` when it raised (``flags.error`` names the exception type; the
``zero_hit`` filter leaves those out — they did not come back empty).

Gate (user ruling 2026-10-27): the same as conversation records — every
employee holding ``session:read`` (viewer included), console only (a logged
question is end-user content; an API key gets 403), one concrete tenant per
request (``tenant_id=*`` refused). Rows are deleted with the base, with a
purged conversation or user, and after the retention window (30 days by default).
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from control_plane.api._authz import console_only, require
from control_plane.api.knowledge import (
    _get_audit,
    _get_knowledge_store,
    _get_retrieval_log_store,
    _require_base,
)
from control_plane.api.knowledge_hits import documents_by_id
from control_plane.tenant_scope import (
    applied_scope,
    cross_tenant_query_enabled,
    ensure_single_tenant_scope,
)
from expert_work.common.observability import current_trace_id_hex
from expert_work.persistence import KnowledgeStore
from expert_work.persistence.knowledge import (
    KnowledgeRetrievalLogStore,
    RetrievalLogEntry,
    RetrievalLogSource,
)
from expert_work.runtime.audit.logger import AuditLogger

#: Rows per page (default / ceiling).
LOGS_PAGE_DEFAULT = 50
LOGS_PAGE_MAX = 200
_NOT_FOUND = "retrieval log not found"


def _utc(value: datetime | None) -> datetime | None:
    """A naive bound is read as UTC (same as ``GET /v1/conversations``)."""
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=UTC)


def _log_dict(entry: RetrievalLogEntry) -> dict[str, Any]:
    return {
        "id": str(entry.id),
        "source": entry.source,
        "query": entry.query,
        "status": entry.status,
        "result_count": entry.result_count,
        "latency_ms": entry.latency_ms,
        "run_id": str(entry.run_id) if entry.run_id is not None else None,
        "thread_id": str(entry.thread_id) if entry.thread_id is not None else None,
        "actor_id": entry.actor_id,
        "params": dict(entry.params),
        "flags": dict(entry.flags),
        "created_at": entry.created_at.isoformat(),
    }


def _document_id(result: Mapping[str, Any]) -> UUID | None:
    try:
        return UUID(str(result.get("document_id")))
    except ValueError:
        return None


async def _detail_dict(
    store: KnowledgeStore, tenant_id: UUID, entry: RetrievalLogEntry
) -> dict[str, Any]:
    """The row with every result's document as it is now (``null`` once gone)."""
    document_ids = [_document_id(result) for result in entry.results]
    documents = await documents_by_id(store, tenant_id, (d for d in document_ids if d))
    return {
        **_log_dict(entry),
        "kb_ids": [str(kb_id) for kb_id in entry.kb_ids],
        "results": [
            {**result, "document": documents.get(doc_id) if doc_id else None}
            for result, doc_id in zip(entry.results, document_ids, strict=True)
        ],
    }


def build_knowledge_logs_router() -> APIRouter:
    router = APIRouter(
        prefix="/v1/knowledge", tags=["knowledge"], dependencies=[Depends(console_only())]
    )

    @router.get(
        "/bases/{name}/retrieval-logs",
        response_model=None,
        dependencies=[Depends(require("session", "read"))],
    )
    async def list_retrieval_logs(
        name: str,
        request: Request,
        store: Annotated[KnowledgeStore, Depends(_get_knowledge_store)],
        log_store: Annotated[KnowledgeRetrievalLogStore, Depends(_get_retrieval_log_store)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
        source: Annotated[RetrievalLogSource | None, Query()] = None,
        status: Annotated[str | None, Query(min_length=1, max_length=32)] = None,
        zero_hit: Annotated[bool, Query()] = False,
        since: Annotated[datetime | None, Query()] = None,
        until: Annotated[datetime | None, Query()] = None,
        limit: Annotated[int, Query(ge=1, le=LOGS_PAGE_MAX)] = LOGS_PAGE_DEFAULT,
        offset: Annotated[int, Query(ge=0)] = 0,
        tenant_id: Annotated[UUID | Literal["*"] | None, Query()] = None,
    ) -> JSONResponse:
        scope = await ensure_single_tenant_scope(
            request.state.principal,
            tenant_id,
            audit,
            trace_id=current_trace_id_hex(),
            endpoint="GET /v1/knowledge/bases/{name}/retrieval-logs",
            cross_tenant_enabled=cross_tenant_query_enabled(request),
        )
        async with applied_scope(scope):
            base = await _require_base(store, scope.tenant_id, name)
            entries, total = await log_store.list_for_base(
                tenant_id=scope.tenant_id,
                kb_id=base.id,
                source=source,
                status=status,
                zero_hit=zero_hit,
                since=_utc(since),
                until=_utc(until),
                limit=limit,
                offset=offset,
            )
        return JSONResponse(
            content={
                "logs": [_log_dict(entry) for entry in entries],
                "total": total,
                "offset": offset,
                "limit": limit,
            }
        )

    @router.get(
        "/bases/{name}/retrieval-logs/{log_id}",
        response_model=None,
        dependencies=[Depends(require("session", "read"))],
    )
    async def get_retrieval_log(
        name: str,
        log_id: UUID,
        request: Request,
        store: Annotated[KnowledgeStore, Depends(_get_knowledge_store)],
        log_store: Annotated[KnowledgeRetrievalLogStore, Depends(_get_retrieval_log_store)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
        tenant_id: Annotated[UUID | Literal["*"] | None, Query()] = None,
    ) -> JSONResponse:
        scope = await ensure_single_tenant_scope(
            request.state.principal,
            tenant_id,
            audit,
            trace_id=current_trace_id_hex(),
            endpoint="GET /v1/knowledge/bases/{name}/retrieval-logs/{log_id}",
            cross_tenant_enabled=cross_tenant_query_enabled(request),
        )
        async with applied_scope(scope):
            base = await _require_base(store, scope.tenant_id, name)
            entry = await log_store.get(tenant_id=scope.tenant_id, log_id=log_id)
            # Read through the base it names: a row of another base is not here.
            if entry is None or base.id not in entry.kb_ids:
                raise HTTPException(status_code=404, detail=_NOT_FOUND)
            content = await _detail_dict(store, scope.tenant_id, entry)
        return JSONResponse(content=content)

    return router


__all__ = ["LOGS_PAGE_DEFAULT", "LOGS_PAGE_MAX", "build_knowledge_logs_router"]
