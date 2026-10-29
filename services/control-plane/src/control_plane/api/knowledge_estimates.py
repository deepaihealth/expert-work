"""``/v1/knowledge`` —— confirming ingest estimates (B-222 PR-2).

Every ingest a tenant pays for is estimated first (local parse and counts, no
model called) and starts only once the user confirms that estimate, whatever
its amount (¥0.00 included):

* ``POST /bases/{name}/documents/confirm`` — confirm documents by
  ``(document_id, estimate_id)``: a compare-and-set, so a document estimated
  again since answers ``estimate_changed``; or every confirmable document of
  the base at once (``all``), provided they still number ``expected_count``
  and add up to ``expected_total_micros`` — what the user was shown — else
  409 with what they add up to now. Duplicates are never confirmed by
  ``all``; those named in ``withdraw_duplicate_ids`` (the ones the user was
  shown) are withdrawn with it. One past its 7 days answers ``expired``;
* ``POST /bases/{name}/documents/{id}/estimate`` — a duplicate caught on
  upload (K-21 ①: another document of the base holds its bytes) the user
  wants ingested anyway: it is estimated, then waits like any other.

Withdrawing an estimate is ``POST …/documents/cancel``; one nobody confirms
within 7 days is withdrawn by the recovery worker. Console plane,
``manifest:write``, audited per document.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Self
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, model_validator

from control_plane.api._authz import console_only, require
from control_plane.api.knowledge import (
    DOCUMENTS_PAGE_MAX,
    _audit_write,
    _document_dict,
    _get_audit,
    _get_ingestion_runner,
    _get_knowledge_store,
    _record_stopped,
    _require_base,
    _summary_dict,
)
from control_plane.knowledge.ingestion import KnowledgeIngestionRunner
from expert_work.persistence import KnowledgeStore
from expert_work.protocol import AuditAction, DocumentStatus, KnowledgeBase
from expert_work.runtime.audit.logger import AuditLogger

_NO_RUNNER = "knowledge ingestion unavailable: no embedding model configured"


class _ConfirmItem(BaseModel):
    document_id: UUID
    estimate_id: UUID


class _ConfirmBody(BaseModel):
    """Body of ``POST /bases/{name}/documents/confirm``: ``documents``, or
    ``all`` with the count and total the user was shown."""

    documents: list[_ConfirmItem] | None = Field(
        default=None, min_length=1, max_length=DOCUMENTS_PAGE_MAX
    )
    all: bool = False
    expected_count: int | None = Field(default=None, ge=0)
    expected_total_micros: int | None = Field(default=None, ge=0)
    #: ``all``: the duplicates the user was shown, withdrawn with it (「跳过 M
    #: 份副本」) — one that arrived since is left waiting.
    withdraw_duplicate_ids: list[UUID] = Field(default_factory=list, max_length=DOCUMENTS_PAGE_MAX)

    @model_validator(mode="after")
    def _one_form(self) -> Self:
        if (self.documents is not None) == self.all:
            msg = "send either documents or all=true"
            raise ValueError(msg)
        if self.all and (self.expected_count is None or self.expected_total_micros is None):
            msg = "all=true needs expected_count and expected_total_micros"
            raise ValueError(msg)
        return self


def _runner_or_503(runner: KnowledgeIngestionRunner | None) -> KnowledgeIngestionRunner:
    if runner is None:
        raise HTTPException(status_code=503, detail=_NO_RUNNER)
    return runner


async def _start_confirmed(
    request: Request,
    store: KnowledgeStore,
    runner: KnowledgeIngestionRunner,
    audit: AuditLogger,
    base: KnowledgeBase,
    document_ids: list[UUID],
) -> None:
    """Audit each confirmed document (with the amount confirmed) and start
    its paid ingest."""
    confirmed = await store.get_documents(tenant_id=base.tenant_id, document_ids=document_ids)
    for document_id in document_ids:
        document = confirmed.get(document_id)
        estimate = document.ingest_estimate if document is not None else None
        await _audit_write(
            audit,
            request,
            action=AuditAction.KNOWLEDGE_DOCUMENT_CONFIRM,
            resource_type="knowledge_document",
            resource_id=str(document_id),
            details={
                "kb": base.name,
                "filename": document.filename if document is not None else None,
                "estimate_id": str(estimate.id) if estimate is not None else None,
                "total_micros": estimate.total_micros if estimate is not None else None,
            },
        )
        runner.submit(tenant_id=base.tenant_id, document_id=document_id)


def build_knowledge_estimates_router() -> APIRouter:
    router = APIRouter(
        prefix="/v1/knowledge", tags=["knowledge"], dependencies=[Depends(console_only())]
    )

    @router.post(
        "/bases/{name}/documents/confirm",
        response_model=None,
        dependencies=[Depends(require("manifest", "write"))],
    )
    async def confirm_documents(
        name: str,
        body: _ConfirmBody,
        request: Request,
        store: Annotated[KnowledgeStore, Depends(_get_knowledge_store)],
        runner: Annotated[KnowledgeIngestionRunner | None, Depends(_get_ingestion_runner)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
    ) -> JSONResponse:
        """Per document: ``confirmed`` (its paid ingest starts),
        ``estimate_changed`` (estimated again since: confirm the new one),
        ``not_awaiting``, ``duplicate`` (never estimated: ingest it anyway
        first), ``expired`` (past its 7 days: being withdrawn) or
        ``not_found``. ``all``: 200 with what was confirmed and the
        duplicates withdrawn, or 409 ``estimate_changed`` with the summary
        now (nothing changed)."""
        tenant_id: UUID = request.state.tenant_id
        base = await _require_base(store, tenant_id, name)
        ready_runner = _runner_or_503(runner)
        confirmed_by: str = request.state.principal.subject_id
        now = datetime.now(UTC)
        if body.documents is not None:
            outcomes = await store.confirm_documents(
                tenant_id=tenant_id,
                kb_id=base.id,
                estimates=[(item.document_id, item.estimate_id) for item in body.documents],
                confirmed_by=confirmed_by,
                now=now,
            )
            confirmed = [doc_id for doc_id, outcome in outcomes.items() if outcome == "confirmed"]
            await _start_confirmed(request, store, ready_runner, audit, base, confirmed)
            return JSONResponse(
                content={
                    "results": [
                        {"document_id": str(doc_id), "outcome": outcome}
                        for doc_id, outcome in outcomes.items()
                    ]
                }
            )
        assert body.expected_count is not None  # noqa: S101 — the body validator
        assert body.expected_total_micros is not None  # noqa: S101
        result = await store.confirm_all(
            tenant_id=tenant_id,
            kb_id=base.id,
            expected_count=body.expected_count,
            expected_total_micros=body.expected_total_micros,
            confirmed_by=confirmed_by,
            now=now,
            withdraw_duplicates=body.withdraw_duplicate_ids,
        )
        if not result.matched:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "estimate_changed",
                    "estimate_summary": _summary_dict(result.summary),
                },
            )
        await _record_stopped(request, audit, ready_runner, base, result.withdrawn)
        await _start_confirmed(request, store, ready_runner, audit, base, list(result.confirmed))
        return JSONResponse(
            content={
                "confirmed": [str(doc_id) for doc_id in result.confirmed],
                "withdrawn": [
                    {"document_id": str(doc_id), "outcome": outcome}
                    for doc_id, outcome in result.withdrawn.items()
                ],
                "estimate_summary": _summary_dict(result.summary),
            }
        )

    @router.post(
        "/bases/{name}/documents/{document_id}/estimate",
        response_model=None,
        dependencies=[Depends(require("manifest", "write"))],
    )
    async def estimate_duplicate(
        name: str,
        document_id: UUID,
        request: Request,
        store: Annotated[KnowledgeStore, Depends(_get_knowledge_store)],
        runner: Annotated[KnowledgeIngestionRunner | None, Depends(_get_ingestion_runner)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
    ) -> JSONResponse:
        """「仍入库」: a duplicate waiting for a decision is estimated like any
        upload (202 ``estimating``) and then waits for its confirmation. 409
        ``not_awaiting`` / ``not_duplicate`` when it is not such a document."""
        tenant_id: UUID = request.state.tenant_id
        base = await _require_base(store, tenant_id, name)
        ready_runner = _runner_or_503(runner)
        document = await store.get_document(tenant_id=tenant_id, document_id=document_id)
        if document is None or document.kb_id != base.id:
            raise HTTPException(status_code=404, detail="document not found")
        if document.status is not DocumentStatus.AWAITING_CONFIRMATION:
            raise HTTPException(status_code=409, detail={"error": "not_awaiting"})
        estimate = document.ingest_estimate
        if estimate is None or estimate.duplicate_of is None:
            raise HTTPException(status_code=409, detail={"error": "not_duplicate"})
        # The upload's own origin: withdrawing it later still deletes a new
        # upload / drops new bytes.
        reset = await store.begin_estimate(
            tenant_id=tenant_id,
            document_id=document_id,
            origin=estimate.origin,
            allow_duplicate=True,
        )
        if reset is None:
            raise HTTPException(status_code=404, detail="document not found")
        ready_runner.submit(tenant_id=tenant_id, document_id=document_id)
        await _audit_write(
            audit,
            request,
            action=AuditAction.KNOWLEDGE_DOCUMENT_REINGEST,
            resource_type="knowledge_document",
            resource_id=str(document_id),
            details={"kb": base.name, "filename": document.filename, "ingest_anyway": True},
        )
        return JSONResponse(status_code=202, content=_document_dict(reset))

    return router
