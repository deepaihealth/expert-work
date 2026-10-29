"""``/v1/knowledge`` —— document maintenance (B-175 P3-PR2).

* ``PATCH /bases/{name}/documents/{id}`` — switch a document on / off (``enabled``):
  switched off it stays listed with its chunks but recall leaves it out;
* ``POST /bases/{name}/documents/batch`` — enable / disable / re-ingest many
  documents (``manifest:write``); ``POST …/documents/batch-delete`` — delete many
  (``manifest:delete``). Both answer what was done and what was skipped, and why;
* ``GET /bases/{name}/stale-documents`` — how many ready documents serve chunks
  cut by other chunking parameters than the base's current ones;
  ``POST /bases/{name}/reingest-stale`` re-ingests those that still have their
  file (the rest are listed: re-upload them), at most :data:`REINGEST_STALE_MAX`
  per call (``remaining``: call again). B-222 PR-2: like a batch re-ingest, it
  spends nothing yet — each document is estimated and waits for the user to
  confirm it (``POST …/documents/confirm``).

Console plane, ``manifest`` permissions, like the rest of ``/v1/knowledge``;
every write is audited — a batch with one row per document it changed (which
document, enabled / re-ingested / deleted) plus one row for the batch.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from control_plane.api._authz import console_only, require
from control_plane.api.knowledge import (
    _audit_write,
    _document_dict,
    _get_audit,
    _get_ingestion_runner,
    _get_knowledge_store,
    _redrive,
    _release_duplicates,
    _require_base,
)
from control_plane.knowledge.chunking import chunk_params_hash
from control_plane.knowledge.ingestion import KnowledgeIngestionRunner
from control_plane.knowledge.recognition.figure_storage import delete_figures_quietly
from control_plane.tenant_scope import (
    applied_scope,
    cross_tenant_query_enabled,
    ensure_single_tenant_scope,
)
from expert_work.common.observability import current_trace_id_hex
from expert_work.persistence import KnowledgeStore
from expert_work.persistence.knowledge.estimates import EstimateRefused
from expert_work.protocol import AuditAction, KnowledgeBase, KnowledgeDocument
from expert_work.protocol.knowledge_estimate import EstimateOrigin
from expert_work.runtime.audit.logger import AuditLogger

#: Documents one batch request may name.
BATCH_MAX = 200
#: Stale documents one ``reingest-stale`` call queues (each starts an ingest).
REINGEST_STALE_MAX = 200

_NO_RUNNER = "knowledge ingestion unavailable: no embedding model configured"


class _DocumentPatch(BaseModel):
    """Body of ``PATCH /bases/{name}/documents/{id}``."""

    enabled: bool


class _BatchBody(BaseModel):
    """Body of ``POST /bases/{name}/documents/batch``."""

    action: Literal["enable", "disable", "reingest"]
    document_ids: list[UUID] = Field(min_length=1, max_length=BATCH_MAX)


class _BatchDeleteBody(BaseModel):
    """Body of ``POST /bases/{name}/documents/batch-delete``."""

    document_ids: list[UUID] = Field(min_length=1, max_length=BATCH_MAX)


def _skipped(document_id: UUID, reason: str) -> dict[str, str]:
    return {"id": str(document_id), "reason": reason}


def _params_hash(base: KnowledgeBase) -> str:
    return chunk_params_hash(
        max_tokens=base.chunk_max_tokens, overlap_tokens=base.chunk_overlap_tokens
    )


async def _base_documents(
    store: KnowledgeStore, tenant_id: UUID, base: KnowledgeBase, document_ids: list[UUID]
) -> dict[UUID, KnowledgeDocument]:
    """The named documents that belong to ``base`` (others are absent)."""
    found = await store.get_documents(tenant_id=tenant_id, document_ids=document_ids)
    return {i: d for i, d in found.items() if d.kb_id == base.id}


async def _redrive_or_reason(
    store: KnowledgeStore,
    runner: KnowledgeIngestionRunner,
    tenant_id: UUID,
    document: KnowledgeDocument,
    origin: EstimateOrigin,
) -> str | None:
    """Re-drive one document of a batch; why it was skipped, if it was
    (``not_found``: deleted meanwhile; ``busy``: its new chunks are being
    swapped in)."""
    try:
        reset = await _redrive(store, runner, tenant_id, document, origin=origin)
    except EstimateRefused as exc:
        return exc.reason
    return None if reset is not None else "not_found"


def _runner_or_503(runner: KnowledgeIngestionRunner | None) -> KnowledgeIngestionRunner:
    if runner is None:
        raise HTTPException(status_code=503, detail=_NO_RUNNER)
    return runner


async def _audit_document(
    audit: AuditLogger,
    request: Request,
    action: AuditAction,
    base: KnowledgeBase,
    document: KnowledgeDocument,
    **details: object,
) -> None:
    """One audit row for one document a batch / base-wide call changed."""
    await _audit_write(
        audit,
        request,
        action=action,
        resource_type="knowledge_document",
        resource_id=str(document.id),
        details={"kb": base.name, "filename": document.filename, **details},
    )


def build_knowledge_maintenance_router() -> APIRouter:
    router = APIRouter(
        prefix="/v1/knowledge", tags=["knowledge"], dependencies=[Depends(console_only())]
    )

    @router.patch(
        "/bases/{name}/documents/{document_id}",
        response_model=None,
        dependencies=[Depends(require("manifest", "write"))],
    )
    async def patch_document(
        name: str,
        document_id: UUID,
        body: _DocumentPatch,
        request: Request,
        store: Annotated[KnowledgeStore, Depends(_get_knowledge_store)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
    ) -> JSONResponse:
        tenant_id: UUID = request.state.tenant_id
        base = await _require_base(store, tenant_id, name)
        done = await store.set_documents_enabled(
            tenant_id=tenant_id, kb_id=base.id, document_ids=[document_id], enabled=body.enabled
        )
        document = await store.get_document(tenant_id=tenant_id, document_id=document_id)
        if not done or document is None:
            raise HTTPException(status_code=404, detail="document not found")
        await _audit_write(
            audit,
            request,
            action=AuditAction.KNOWLEDGE_DOCUMENT_TOGGLE,
            resource_type="knowledge_document",
            resource_id=str(document_id),
            details={"kb": base.name, "filename": document.filename, "enabled": body.enabled},
        )
        return JSONResponse(content=_document_dict(document))

    @router.post(
        "/bases/{name}/documents/batch",
        response_model=None,
        dependencies=[Depends(require("manifest", "write"))],
    )
    async def batch_documents(
        name: str,
        body: _BatchBody,
        request: Request,
        store: Annotated[KnowledgeStore, Depends(_get_knowledge_store)],
        runner: Annotated[KnowledgeIngestionRunner | None, Depends(_get_ingestion_runner)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
    ) -> JSONResponse:
        tenant_id: UUID = request.state.tenant_id
        base = await _require_base(store, tenant_id, name)
        asked = list(dict.fromkeys(body.document_ids))
        mine = await _base_documents(store, tenant_id, base, asked)
        done: list[UUID] = []
        skipped: list[dict[str, str]] = []
        if body.action == "reingest":
            ready_runner = _runner_or_503(runner)
            for document_id in asked:
                document = mine.get(document_id)
                if document is None:
                    skipped.append(_skipped(document_id, "not_found"))
                elif not document.has_content and not document.has_pending:
                    skipped.append(_skipped(document_id, "content_missing"))
                elif (
                    outcome := await _redrive_or_reason(
                        store, ready_runner, tenant_id, document, EstimateOrigin.BATCH_REINGEST
                    )
                ) is not None:
                    skipped.append(_skipped(document_id, outcome))
                else:
                    done.append(document_id)
                    await _audit_document(
                        audit,
                        request,
                        AuditAction.KNOWLEDGE_DOCUMENT_REINGEST,
                        base,
                        document,
                        batch=True,
                    )
        else:
            enabled = body.action == "enable"
            done = await store.set_documents_enabled(
                tenant_id=tenant_id, kb_id=base.id, document_ids=asked, enabled=enabled
            )
            skipped = [_skipped(i, "not_found") for i in asked if i not in set(done)]
            for document_id in done:
                await _audit_document(
                    audit,
                    request,
                    AuditAction.KNOWLEDGE_DOCUMENT_TOGGLE,
                    base,
                    mine[document_id],
                    batch=True,
                    enabled=enabled,
                )
        await _audit_write(
            audit,
            request,
            action=AuditAction.KNOWLEDGE_DOCUMENT_BATCH,
            resource_type="knowledge_base",
            resource_id=str(base.id),
            details={
                "kb": base.name,
                "action": body.action,
                "done": len(done),
                "skipped": len(skipped),
            },
        )
        return JSONResponse(
            content={"action": body.action, "done": [str(i) for i in done], "skipped": skipped}
        )

    @router.post(
        "/bases/{name}/documents/batch-delete",
        response_model=None,
        dependencies=[Depends(require("manifest", "delete"))],
    )
    async def batch_delete_documents(
        name: str,
        body: _BatchDeleteBody,
        request: Request,
        store: Annotated[KnowledgeStore, Depends(_get_knowledge_store)],
        runner: Annotated[KnowledgeIngestionRunner | None, Depends(_get_ingestion_runner)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
    ) -> JSONResponse:
        tenant_id: UUID = request.state.tenant_id
        base = await _require_base(store, tenant_id, name)
        asked = list(dict.fromkeys(body.document_ids))
        mine = await _base_documents(store, tenant_id, base, asked)
        done: list[UUID] = []
        skipped: list[dict[str, str]] = []
        for document_id in asked:
            if document_id not in mine or not await store.delete_document(
                tenant_id=tenant_id, document_id=document_id
            ):
                skipped.append(_skipped(document_id, "not_found"))
                continue
            # B-209 PR-2 — its original figures go with it.
            await delete_figures_quietly(
                request.app.state.object_store,
                tenant_id=tenant_id,
                kb_id=base.id,
                document_id=document_id,
            )
            done.append(document_id)
            await _audit_document(
                audit,
                request,
                AuditAction.KNOWLEDGE_DOCUMENT_DELETE,
                base,
                mine[document_id],
                batch=True,
            )
        await _audit_write(
            audit,
            request,
            action=AuditAction.KNOWLEDGE_DOCUMENT_BATCH,
            resource_type="knowledge_base",
            resource_id=str(base.id),
            details={
                "kb": base.name,
                "action": "delete",
                "done": len(done),
                "skipped": len(skipped),
            },
        )
        if done:
            await _release_duplicates(store, runner, base)  # B-222 PR-2: their copies
        return JSONResponse(
            content={"action": "delete", "done": [str(i) for i in done], "skipped": skipped}
        )

    @router.get(
        "/bases/{name}/stale-documents",
        response_model=None,
        dependencies=[Depends(require("manifest", "read"))],
    )
    async def stale_documents(
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
            endpoint="GET /v1/knowledge/bases/{name}/stale-documents",
            cross_tenant_enabled=cross_tenant_query_enabled(request),
        )
        async with applied_scope(scope):
            base = await _require_base(store, scope.tenant_id, name)
            params_hash = _params_hash(base)
            stale = await store.count_stale_documents(
                tenant_id=scope.tenant_id, kb_id=base.id, params_hash=params_hash
            )
        return JSONResponse(content={"stale": stale, "chunk_params_hash": params_hash})

    @router.post(
        "/bases/{name}/reingest-stale",
        response_model=None,
        dependencies=[Depends(require("manifest", "write"))],
    )
    async def reingest_stale(
        name: str,
        request: Request,
        store: Annotated[KnowledgeStore, Depends(_get_knowledge_store)],
        runner: Annotated[KnowledgeIngestionRunner | None, Depends(_get_ingestion_runner)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
    ) -> JSONResponse:
        tenant_id: UUID = request.state.tenant_id
        base = await _require_base(store, tenant_id, name)
        ready_runner = _runner_or_503(runner)
        stale = await store.list_stale_documents(
            tenant_id=tenant_id, kb_id=base.id, params_hash=_params_hash(base)
        )
        queued: list[str] = []
        skipped: list[dict[str, str]] = []
        remaining = 0
        for document in stale:
            if not document.has_content:
                skipped.append(_skipped(document.id, "content_missing"))
            elif len(queued) >= REINGEST_STALE_MAX:
                remaining += 1  # the next call takes them
            elif (
                outcome := await _redrive_or_reason(
                    store, ready_runner, tenant_id, document, EstimateOrigin.REINGEST_STALE
                )
            ) is not None:
                skipped.append(_skipped(document.id, outcome))
            else:
                queued.append(str(document.id))
                await _audit_document(
                    audit,
                    request,
                    AuditAction.KNOWLEDGE_DOCUMENT_REINGEST,
                    base,
                    document,
                    stale=True,
                )
        await _audit_write(
            audit,
            request,
            action=AuditAction.KNOWLEDGE_BASE_REINGEST,
            resource_type="knowledge_base",
            resource_id=str(base.id),
            details={
                "kb": base.name,
                "queued": len(queued),
                "skipped": len(skipped),
                "remaining": remaining,
            },
        )
        content: dict[str, Any] = {"queued": queued, "skipped": skipped, "remaining": remaining}
        return JSONResponse(status_code=202, content=content)

    return router
