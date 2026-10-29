"""In-memory ingest estimates (B-222 PR-2) — mixed into ``InMemoryKnowledgeStore``.

The same rules as the SQL store (:mod:`~expert_work.persistence.knowledge.estimates`);
the stored estimate JSON (with its store-only keys) is side state, the
document carries the shown part.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from expert_work.persistence.knowledge.base import DocumentFileFacts
from expert_work.persistence.knowledge.estimates import (
    WAITING_STATUSES,
    ConfirmAllResult,
    ConfirmOutcome,
    EstimateSummary,
    ExpiredEstimate,
    ParseSnapshot,
    UploadStart,
    WithdrawOutcome,
    actual_lands,
    begin_meta,
    capture_restore,
    checks_duplicates,
    confirm_outcome,
    confirmed,
    duplicate_estimate,
    duplicate_ref,
    estimate_id_of,
    is_duplicate,
    is_estimated,
    is_expired,
    merge_actual,
    meta_of,
    public_estimate,
    read_actual,
    summarize,
    with_estimate,
    withdraw_plan,
    withdraws_with_confirm_all,
)
from expert_work.protocol import (
    SEARCHABLE_CHUNK_TYPES,
    DocumentSourceType,
    DocumentStatus,
    IngestErrorCode,
    IngestStage,
    KnowledgeChunk,
    KnowledgeDocument,
)
from expert_work.protocol.knowledge_estimate import (
    CONFIRM_WINDOW,
    SNAPSHOT_GRACE_AFTER_CONFIRM,
    EstimateOrigin,
)


@dataclass(frozen=True)
class HeldSnapshot:
    """``knowledge_parse_snapshot`` row."""

    tenant_id: UUID
    estimate_id: UUID
    snapshot: ParseSnapshot
    expires_at: datetime


def _stage(value: str | None) -> IngestStage | None:
    try:
        return IngestStage(value) if value is not None else None
    except ValueError:
        return None


def _error_code(value: str | None) -> IngestErrorCode | None:
    try:
        return IngestErrorCode(value) if value is not None else None
    except ValueError:
        return IngestErrorCode.INTERNAL_ERROR


class InMemoryEstimates:
    """The estimate methods of ``InMemoryKnowledgeStore`` (its state below)."""

    _documents: list[KnowledgeDocument]
    _chunks: list[KnowledgeChunk]
    _content: dict[UUID, bytes | None]
    _sha256: dict[UUID, str | None]
    _pending: dict[UUID, tuple[bytes, str | None]]
    _lease: dict[UUID, datetime | None]
    #: B-222 PR-2 —— ``ingest_estimate`` as stored (with the store-only keys)
    #: and ``knowledge_parse_snapshot``.
    _estimates: dict[UUID, dict[str, Any]]
    _snapshots: dict[UUID, HeldSnapshot]

    if TYPE_CHECKING:

        def _bump_doc_seq(self, tenant_id: UUID) -> int:
            """Provided by the store this mixin is mixed into."""
            raise NotImplementedError

        def _drop_document_state(self, document_ids: set[UUID]) -> None:
            """Provided by the store this mixin is mixed into."""

    # -- helpers --------------------------------------------------------------

    def _find(self, tenant_id: UUID, document_id: UUID) -> KnowledgeDocument | None:
        return next(
            (d for d in self._documents if d.tenant_id == tenant_id and d.id == document_id),
            None,
        )

    def _store_document(
        self,
        document: KnowledgeDocument,
        update: Mapping[str, Any],
        raw: Mapping[str, Any] | None,
    ) -> KnowledgeDocument:
        """Write ``update`` and the stored estimate (``None``: none) at once."""
        if raw is None:
            self._estimates.pop(document.id, None)
        else:
            self._estimates[document.id] = dict(raw)
        changed = document.model_copy(update={**update, "ingest_estimate": public_estimate(raw)})
        self._documents[self._documents.index(document)] = changed
        return changed

    def _estimated_sha(self, document_id: UUID) -> str | None:
        """The hash of the bytes an ingest of the document would read."""
        pending = self._pending.get(document_id)
        return pending[1] if pending is not None else self._sha256.get(document_id)

    def _duplicate_of(
        self,
        document: KnowledgeDocument | None,
        *,
        tenant_id: UUID,
        kb_id: UUID,
        sha: str | None,
        created_before: bool,
    ) -> dict[str, Any] | None:
        """The base's earliest other document holding ``sha`` (serving or
        pending); only one created before ``document`` when ``created_before``."""
        if sha is None:
            return None
        found = [
            d
            for d in self._documents
            if d.tenant_id == tenant_id
            and d.kb_id == kb_id
            and (document is None or d.id != document.id)
            and (self._sha256.get(d.id) == sha or self._pending.get(d.id, (b"", None))[1] == sha)
        ]
        if created_before and document is not None:
            found = [d for d in found if _order(d) < _order(document)]
        if not found:
            return None
        first = min(found, key=_order)
        return duplicate_ref(document_id=first.id, doc_seq=first.doc_seq, filename=first.filename)

    def _withdraw(self, document: KnowledgeDocument, now: datetime) -> WithdrawOutcome:
        plan = withdraw_plan(self._estimates.get(document.id), chunk_count=document.chunk_count)
        self._snapshots.pop(document.id, None)
        if plan.delete:
            self._documents.remove(document)
            self._chunks = [c for c in self._chunks if c.document_id != document.id]
            for slot in (self._content, self._sha256, self._pending, self._lease):
                slot.pop(document.id, None)
            self._drop_document_state({document.id})
            return "withdrawn_deleted"
        if plan.clear_pending:
            self._pending.pop(document.id, None)
        self._lease[document.id] = None
        self._store_document(
            document,
            {
                "status": plan.status,
                "error": plan.error,
                "error_code": _error_code(plan.error_code),
                "stage": _stage(plan.stage),
                "ingest_generation": document.ingest_generation + 1,
                "attempts": 0,
                "confirm_by": None,
                "has_pending": document.id in self._pending,
                "updated_at": now,
            },
            plan.estimate,
        )
        return "withdrawn"

    # -- the store methods ----------------------------------------------------

    async def begin_upload(
        self,
        *,
        tenant_id: UUID,
        kb_id: UUID,
        filename: str,
        content: bytes,
        content_sha256: str,
        now: datetime,
    ) -> UploadStart:
        existing = next(
            (
                d
                for d in self._documents
                if d.tenant_id == tenant_id and d.kb_id == kb_id and d.filename == filename
            ),
            None,
        )
        if existing is not None:
            ready_same = (
                existing.status is DocumentStatus.READY
                and self._sha256.get(existing.id) == content_sha256
            )
            waiting_same = (
                existing.status in WAITING_STATUSES
                and self._estimated_sha(existing.id) == content_sha256
            )
            if ready_same or waiting_same:
                return UploadStart(existing, "unchanged")
            return self._reupload(existing, content, content_sha256, now)
        meta = begin_meta(
            status=None, raw=None, origin=EstimateOrigin.UPLOAD, allow_duplicate=False, restore=None
        )
        duplicate = self._duplicate_of(
            None, tenant_id=tenant_id, kb_id=kb_id, sha=content_sha256, created_before=False
        )
        facts = DocumentFileFacts.of(filename, content)
        document = KnowledgeDocument(
            id=uuid4(),
            tenant_id=tenant_id,
            kb_id=kb_id,
            filename=filename,
            status=DocumentStatus.ESTIMATING,
            has_content=True,
            doc_seq=self._bump_doc_seq(tenant_id),
            title=facts.title,
            mime=facts.mime,
            size_bytes=facts.size_bytes,
            source_type=DocumentSourceType.FILE,
            stage=IngestStage.QUEUED,
            # The clock, as the SQL row's ``now()`` (``now`` dates the estimate).
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        self._documents.append(document)
        self._content[document.id] = content
        self._sha256[document.id] = content_sha256
        self._lease[document.id] = None
        return self._entered(document, meta, duplicate, now)

    def _reupload(
        self, existing: KnowledgeDocument, content: bytes, content_sha256: str, now: datetime
    ) -> UploadStart:
        raw = self._estimates.get(existing.id)
        meta = begin_meta(
            status=existing.status.value,
            raw=raw,
            origin=EstimateOrigin.REUPLOAD,
            allow_duplicate=False,
            restore=capture_restore(
                status=existing.status.value,
                error=existing.error,
                error_code=existing.error_code.value if existing.error_code else None,
                stage=existing.stage.value if existing.stage else None,
                raw_estimate=raw,
            ),
        )
        update: dict[str, Any] = {}
        # Into the pending slot even when nothing serves: withdrawn, it goes
        # with the slot; ingested, it is promoted (``set_document_status``).
        self._pending[existing.id] = (content, content_sha256)
        if existing.doc_seq is None:
            update["doc_seq"] = self._bump_doc_seq(existing.tenant_id)
        document = existing.model_copy(
            update={
                **update,
                "error": None,
                "error_code": None,
                "stage": IngestStage.QUEUED,
                "attempts": 0,
                "ingest_generation": existing.ingest_generation + 1,
                "has_pending": existing.id in self._pending,
                "has_content": self._content.get(existing.id) is not None,
                "updated_at": now,
            }
        )
        self._documents[self._documents.index(existing)] = document
        self._lease[existing.id] = None
        self._snapshots.pop(existing.id, None)
        duplicate = (
            self._duplicate_of(
                document,
                tenant_id=document.tenant_id,
                kb_id=document.kb_id,
                sha=content_sha256,
                created_before=False,
            )
            if checks_duplicates(meta)
            else None
        )
        return self._entered(document, meta, duplicate, now)

    def _entered(
        self,
        document: KnowledgeDocument,
        meta: Mapping[str, Any],
        duplicate: Mapping[str, Any] | None,
        now: datetime,
    ) -> UploadStart:
        if duplicate is not None:
            raw = duplicate_estimate(meta=meta, duplicate_of=duplicate, now=now)
            waiting = self._store_document(
                document,
                {
                    "status": DocumentStatus.AWAITING_CONFIRMATION,
                    "confirm_by": now + CONFIRM_WINDOW,
                },
                raw,
            )
            return UploadStart(waiting, "duplicate")
        estimating = self._store_document(
            document, {"status": DocumentStatus.ESTIMATING, "confirm_by": None}, meta
        )
        return UploadStart(estimating, "estimating")

    async def begin_estimate(
        self,
        *,
        tenant_id: UUID,
        document_id: UUID,
        origin: EstimateOrigin,
        allow_duplicate: bool = False,
    ) -> KnowledgeDocument | None:
        document = self._find(tenant_id, document_id)
        if document is None:
            return None
        raw = self._estimates.get(document_id)
        meta = begin_meta(
            status=document.status.value,
            raw=raw,
            origin=origin,
            allow_duplicate=allow_duplicate,
            restore=capture_restore(
                status=document.status.value,
                error=document.error,
                error_code=document.error_code.value if document.error_code else None,
                stage=document.stage.value if document.stage else None,
                raw_estimate=raw,
            ),
        )
        self._lease[document_id] = None
        self._snapshots.pop(document_id, None)
        update: dict[str, Any] = {
            "status": DocumentStatus.ESTIMATING,
            "error": None,
            "error_code": None,
            "stage": IngestStage.QUEUED,
            "attempts": 0,
            "ingest_generation": document.ingest_generation + 1,
            "confirm_by": None,
            "updated_at": datetime.now(UTC),
        }
        return self._store_document(document, update, meta)

    async def finish_estimate(
        self,
        *,
        tenant_id: UUID,
        document_id: UUID,
        generation: int,
        estimate: Mapping[str, Any],
        snapshot: ParseSnapshot | None,
        confirm_by: datetime,
    ) -> KnowledgeDocument | None:
        document = self._find(tenant_id, document_id)
        if (
            document is None
            or document.status is not DocumentStatus.ESTIMATING
            or document.ingest_generation != generation
        ):
            return None
        meta = meta_of(self._estimates.get(document_id))
        raw = with_estimate(meta, estimate)
        if checks_duplicates(meta):
            duplicate = self._duplicate_of(
                document,
                tenant_id=tenant_id,
                kb_id=document.kb_id,
                sha=self._estimated_sha(document_id),
                created_before=True,
            )
            if duplicate is not None:
                raw["duplicate_of"] = duplicate
        self._lease[document_id] = None
        self._hold_snapshot(document, raw, snapshot, confirm_by)
        return self._store_document(
            document,
            {"status": DocumentStatus.AWAITING_CONFIRMATION, "confirm_by": confirm_by},
            raw,
        )

    def _hold_snapshot(
        self,
        document: KnowledgeDocument,
        raw: Mapping[str, Any],
        snapshot: ParseSnapshot | None,
        expires_at: datetime,
    ) -> None:
        estimate_id = estimate_id_of(raw)
        if snapshot is None or estimate_id is None:
            self._snapshots.pop(document.id, None)
            return
        self._snapshots[document.id] = HeldSnapshot(
            tenant_id=document.tenant_id,
            estimate_id=estimate_id,
            snapshot=snapshot,
            expires_at=expires_at,
        )

    async def confirm_documents(
        self,
        *,
        tenant_id: UUID,
        kb_id: UUID,
        estimates: Sequence[tuple[UUID, UUID]],
        confirmed_by: str | None,
        now: datetime,
    ) -> dict[UUID, ConfirmOutcome]:
        outcomes: dict[UUID, ConfirmOutcome] = {}
        for document_id, estimate_id in estimates:
            if document_id in outcomes:
                continue
            document = self._find(tenant_id, document_id)
            if document is None or document.kb_id != kb_id:
                outcomes[document_id] = "not_found"
                continue
            raw = self._estimates.get(document_id)
            refused = confirm_outcome(
                status=document.status.value,
                raw=raw,
                estimate_id=estimate_id,
                confirm_by=document.confirm_by,
                now=now,
            )
            if refused is not None:
                outcomes[document_id] = refused
                continue
            self._confirm(document, raw or {}, confirmed_by, now)
            outcomes[document_id] = "confirmed"
        return outcomes

    def _confirm(
        self,
        document: KnowledgeDocument,
        raw: Mapping[str, Any],
        confirmed_by: str | None,
        now: datetime,
    ) -> None:
        held = self._snapshots.get(document.id)
        if held is not None:
            self._snapshots[document.id] = HeldSnapshot(
                tenant_id=held.tenant_id,
                estimate_id=held.estimate_id,
                snapshot=held.snapshot,
                expires_at=now + SNAPSHOT_GRACE_AFTER_CONFIRM,
            )
        self._lease[document.id] = None
        self._store_document(
            document,
            {
                "status": DocumentStatus.PENDING,
                "ingest_generation": document.ingest_generation + 1,
                "attempts": 0,
                "confirm_by": None,
                "updated_at": now,
            },
            confirmed(raw, confirmed_by=confirmed_by, now=now),
        )

    async def confirm_all(
        self,
        *,
        tenant_id: UUID,
        kb_id: UUID,
        expected_count: int,
        expected_total_micros: int,
        confirmed_by: str | None,
        now: datetime,
        withdraw_duplicates: Sequence[UUID] = (),
    ) -> ConfirmAllResult:
        mine = [d for d in self._documents if d.tenant_id == tenant_id and d.kb_id == kb_id]
        summary = summarize((d.status.value, self._estimates.get(d.id), d.confirm_by) for d in mine)
        if summary.awaiting != expected_count or summary.total_micros != expected_total_micros:
            return ConfirmAllResult(matched=False, summary=summary)
        seen = frozenset(withdraw_duplicates)
        confirmed_ids: list[UUID] = []
        withdrawn: dict[UUID, WithdrawOutcome] = {}
        for document in mine:
            if document.status is not DocumentStatus.AWAITING_CONFIRMATION:
                continue
            raw = self._estimates.get(document.id)
            if is_duplicate(raw) or not is_estimated(raw):
                if withdraws_with_confirm_all(document.id, raw, seen):
                    withdrawn[document.id] = self._withdraw(document, now)
                continue
            if is_expired(document.confirm_by, now):
                continue  # the expiry sweep withdraws it
            self._confirm(document, raw or {}, confirmed_by, now)
            confirmed_ids.append(document.id)
        return ConfirmAllResult(
            matched=True, summary=summary, confirmed=tuple(confirmed_ids), withdrawn=withdrawn
        )

    async def return_to_confirmation(
        self,
        *,
        tenant_id: UUID,
        document_id: UUID,
        generation: int,
        estimate: Mapping[str, Any],
        snapshot: ParseSnapshot | None,
        confirm_by: datetime,
    ) -> bool:
        document = self._find(tenant_id, document_id)
        if (
            document is None
            or document.status is not DocumentStatus.PROCESSING
            or document.ingest_generation != generation
        ):
            return False
        raw = with_estimate(meta_of(self._estimates.get(document_id)), estimate)
        self._lease[document_id] = None
        self._hold_snapshot(document, raw, snapshot, confirm_by)
        self._store_document(
            document,
            {"status": DocumentStatus.AWAITING_CONFIRMATION, "confirm_by": confirm_by},
            raw,
        )
        return True

    async def expire_awaiting(self, *, now: datetime, limit: int) -> list[ExpiredEstimate]:
        due = sorted(
            (
                d
                for d in self._documents
                if d.status is DocumentStatus.AWAITING_CONFIRMATION
                and d.confirm_by is not None
                and d.confirm_by < now
            ),
            key=lambda d: (d.confirm_by, d.id),
        )[:limit]
        expired: list[ExpiredEstimate] = []
        for document in due:
            outcome = self._withdraw(document, now)
            expired.append(
                ExpiredEstimate(
                    tenant_id=document.tenant_id,
                    kb_id=document.kb_id,
                    document_id=document.id,
                    filename=document.filename,
                    outcome=outcome,
                )
            )
        return expired

    async def get_parse_snapshot(
        self, *, tenant_id: UUID, document_id: UUID, estimate_id: UUID
    ) -> ParseSnapshot | None:
        held = self._snapshots.get(document_id)
        if held is None or held.tenant_id != tenant_id or held.estimate_id != estimate_id:
            return None
        return held.snapshot

    async def purge_expired_snapshots(self, *, now: datetime) -> int:
        expired = [i for i, held in self._snapshots.items() if held.expires_at < now]
        for document_id in expired:
            del self._snapshots[document_id]
        return len(expired)

    async def record_ingest_actual(
        self,
        *,
        tenant_id: UUID,
        document_id: UUID,
        generation: int,
        estimate_id: UUID | None,
        actual: Mapping[str, Any],
        settled: bool,
    ) -> bool:
        document = self._find(tenant_id, document_id)
        if document is None or not actual_lands(
            row_generation=document.ingest_generation,
            row_status=document.status.value,
            row_estimate=self._estimates.get(document_id),
            generation=generation,
            estimate_id=estimate_id,
        ):
            return False
        existing = (
            document.ingest_actual.model_dump(mode="json")
            if document.ingest_actual is not None
            else None
        )
        merged = read_actual(merge_actual(existing, actual))
        self._documents[self._documents.index(document)] = document.model_copy(
            update={"ingest_actual": merged}
        )
        if settled:
            self._snapshots.pop(document_id, None)
        return True

    async def estimate_summary(self, *, tenant_id: UUID, kb_id: UUID) -> EstimateSummary:
        return summarize(
            (d.status.value, self._estimates.get(d.id), d.confirm_by)
            for d in self._documents
            if d.tenant_id == tenant_id and d.kb_id == kb_id
        )

    async def chunk_hashes_on_model(
        self,
        *,
        tenant_id: UUID,
        document_id: UUID,
        content_hashes: Sequence[str],
        embedding_model: str,
    ) -> set[str]:
        wanted = set(content_hashes)
        return {
            c.content_hash
            for c in self._chunks
            if c.tenant_id == tenant_id
            and c.document_id == document_id
            and c.chunk_type in SEARCHABLE_CHUNK_TYPES
            and c.embedding_model == embedding_model
            and c.content_hash is not None
            and c.content_hash in wanted
        }


def _order(document: KnowledgeDocument) -> tuple[datetime, str]:
    """Creation order (the SQL store's ``created_at, id``)."""
    created = document.created_at or datetime.min.replace(tzinfo=UTC)
    return created, str(document.id)
