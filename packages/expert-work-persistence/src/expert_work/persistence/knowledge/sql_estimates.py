"""SQL ingest estimates (B-222 PR-2) — mixed into ``SqlKnowledgeStore``.

The rules are :mod:`~expert_work.persistence.knowledge.estimates`'s (shared with
the in-memory store); this module only reads and writes the rows. Every change
of a document locks its row first (``FOR UPDATE``), the order a fenced chunk
swap and a cancel take, so a confirmation, a withdrawal, an expiry, a re-upload
and an estimate finishing serialise on the row.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, Final
from uuid import UUID, uuid4

from sqlalchemy import DateTime, Text, any_, delete, func, literal, or_, select, tuple_, update
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.sql.elements import ColumnElement

from expert_work.persistence.knowledge.base import DocumentFileFacts
from expert_work.persistence.knowledge.estimates import (
    WAITING_STATUSES,
    ConfirmAllResult,
    ConfirmOutcome,
    EstimateRefused,
    EstimateSummary,
    ExpiredEstimate,
    ParseSnapshot,
    UploadStart,
    WithdrawOutcome,
    WithdrawPlan,
    actual_lands,
    begin_meta,
    capture_restore,
    checks_duplicates,
    confirm_outcome,
    confirmed,
    discard_plan,
    duplicate_estimate,
    duplicate_ref,
    duplicate_ref_id,
    estimate_id_of,
    estimate_refusal,
    is_duplicate,
    is_estimated,
    is_expired,
    kept_deadline,
    merge_actual,
    meta_of,
    released_duplicate,
    summarize,
    summary_matches,
    with_estimate,
    withdraw_plan,
    withdraws_with_confirm_all,
    writing_chunks,
)
from expert_work.persistence.models import (
    KnowledgeChunkRow,
    KnowledgeDocumentRow,
    KnowledgeParseSnapshotRow,
)
from expert_work.protocol import (
    SEARCHABLE_CHUNK_TYPES,
    DocumentSourceType,
    DocumentStatus,
    IngestStage,
    KnowledgeDocument,
)
from expert_work.protocol.knowledge_estimate import (
    CONFIRM_WINDOW,
    SNAPSHOT_GRACE_AFTER_CONFIRM,
    EstimateOrigin,
)

_WAITING_VALUES = tuple(sorted(status.value for status in WAITING_STATUSES))
#: Re-sending these bytes changes nothing (a confirmed ingest is not restarted).
_UNDER_WAY_VALUES = (
    *_WAITING_VALUES,
    DocumentStatus.PENDING.value,
    DocumentStatus.PROCESSING.value,
)
_SEARCHABLE_VALUES = tuple(sorted(chunk_type.value for chunk_type in SEARCHABLE_CHUNK_TYPES))
#: Hashes per ``chunk_hashes_on_model`` query.
_HASH_READ_BATCH: Final = 5_000
#: Duplicates locked (and originals read) per ``release_stale_duplicates`` batch.
_RELEASE_BATCH: Final = 500


def _estimated_sha(row: KnowledgeDocumentRow) -> str | None:
    """The hash of the bytes an ingest of the document would read."""
    return row.pending_content_sha256 if row.has_pending else row.content_sha256


def _restore_of(row: KnowledgeDocumentRow) -> dict[str, Any]:
    return capture_restore(
        status=row.status,
        error=row.error,
        error_code=row.error_code,
        stage=row.stage,
        raw_estimate=row.ingest_estimate,
    )


class SqlEstimates:
    """The estimate methods of ``SqlKnowledgeStore``."""

    _sf: async_sessionmaker[AsyncSession]

    @staticmethod
    def _document_of(row: KnowledgeDocumentRow) -> KnowledgeDocument:
        raise NotImplementedError  # the store's row → document reader

    @staticmethod
    async def _next_doc_seq(session: AsyncSession, tenant_id: UUID) -> int:
        raise NotImplementedError  # the store's per-tenant counter

    # -- helpers --------------------------------------------------------------

    @staticmethod
    async def _lock(
        session: AsyncSession, tenant_id: UUID, document_id: UUID
    ) -> KnowledgeDocumentRow | None:
        return (
            await session.execute(
                select(KnowledgeDocumentRow)
                .where(
                    KnowledgeDocumentRow.tenant_id == tenant_id,
                    KnowledgeDocumentRow.id == document_id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()

    @staticmethod
    async def _duplicate_of(
        session: AsyncSession,
        *,
        tenant_id: UUID,
        kb_id: UUID,
        sha: str | None,
        exclude: UUID | None,
        before: KnowledgeDocumentRow | None,
    ) -> dict[str, Any] | None:
        """The base's earliest other document holding ``sha`` (serving or
        pending); only one created before ``before`` when given."""
        if sha is None:
            return None
        where = [
            KnowledgeDocumentRow.tenant_id == tenant_id,
            KnowledgeDocumentRow.kb_id == kb_id,
            or_(
                KnowledgeDocumentRow.content_sha256 == sha,
                KnowledgeDocumentRow.pending_content_sha256 == sha,
            ),
        ]
        if exclude is not None:
            where.append(KnowledgeDocumentRow.id != exclude)
        if before is not None:
            where.append(
                tuple_(KnowledgeDocumentRow.created_at, KnowledgeDocumentRow.id)
                < tuple_(
                    literal(before.created_at, DateTime(timezone=True)),
                    literal(before.id, PG_UUID(as_uuid=True)),
                )
            )
        found = (
            await session.execute(
                select(
                    KnowledgeDocumentRow.id,
                    KnowledgeDocumentRow.doc_seq,
                    KnowledgeDocumentRow.filename,
                )
                .where(*where)
                .order_by(KnowledgeDocumentRow.created_at, KnowledgeDocumentRow.id)
                .limit(1)
            )
        ).first()
        if found is None:
            return None
        return duplicate_ref(document_id=found.id, doc_seq=found.doc_seq, filename=found.filename)

    @staticmethod
    async def _keep_snapshot(
        session: AsyncSession,
        row: KnowledgeDocumentRow,
        raw: Mapping[str, Any],
        snapshot: ParseSnapshot | None,
        expires_at: datetime,
    ) -> None:
        """``snapshot`` replaces the document's (``None`` deletes it)."""
        estimate_id = estimate_id_of(raw)
        if snapshot is None or estimate_id is None:
            await _drop_snapshot(session, row.tenant_id, row.id)
            return
        values = {
            "estimate_id": estimate_id,
            "format": snapshot.format,
            "payload": snapshot.payload,
            "size_bytes": len(snapshot.payload),
            "created_at": func.now(),
            "expires_at": expires_at,
        }
        await session.execute(
            pg_insert(KnowledgeParseSnapshotRow)
            .values(tenant_id=row.tenant_id, document_id=row.id, **values)
            .on_conflict_do_update(
                index_elements=[KnowledgeParseSnapshotRow.document_id], set_=values
            )
        )

    @classmethod
    async def _withdraw_row(
        cls, session: AsyncSession, row: KnowledgeDocumentRow, now: datetime
    ) -> WithdrawOutcome:
        plan = withdraw_plan(row.ingest_estimate, chunk_count=row.chunk_count)
        return await cls._apply_row(session, row, plan, now)

    @classmethod
    async def discard_waiting_row(
        cls, session: AsyncSession, row: KnowledgeDocumentRow, now: datetime
    ) -> None:
        """``discard_pending`` of a waiting document (:func:`discard_plan`)."""
        plan = discard_plan(row.ingest_estimate, chunk_count=row.chunk_count)
        await cls._apply_row(session, row, plan, now)

    @staticmethod
    async def _apply_row(
        session: AsyncSession, row: KnowledgeDocumentRow, plan: WithdrawPlan, now: datetime
    ) -> WithdrawOutcome:
        await _drop_snapshot(session, row.tenant_id, row.id)
        if plan.delete:
            # Nothing of it was ever ingested; the FKs take whatever hangs off it.
            await session.execute(
                delete(KnowledgeDocumentRow).where(
                    KnowledgeDocumentRow.tenant_id == row.tenant_id,
                    KnowledgeDocumentRow.id == row.id,
                )
            )
            return "withdrawn_deleted"
        if plan.clear_pending:
            row.pending_content = None
            row.pending_content_sha256 = None
        row.status = (plan.status or DocumentStatus.FAILED).value
        row.error = plan.error
        row.error_code = plan.error_code
        row.stage = plan.stage
        row.ingest_estimate = plan.estimate
        row.ingest_generation = row.ingest_generation + 1
        row.attempts = 0
        row.confirm_by = None
        row.claimed_at = None
        row.lease_until = None
        row.updated_at = now
        return "withdrawn"

    def _confirm_row(
        self, row: KnowledgeDocumentRow, confirmed_by: str | None, now: datetime
    ) -> None:
        row.status = DocumentStatus.PENDING.value
        row.ingest_estimate = confirmed(
            row.ingest_estimate or {}, confirmed_by=confirmed_by, now=now
        )
        row.ingest_generation = row.ingest_generation + 1
        row.attempts = 0
        row.confirm_by = None
        row.claimed_at = None
        row.lease_until = None
        row.updated_at = now

    @staticmethod
    async def _extend_snapshot(
        session: AsyncSession, row: KnowledgeDocumentRow, now: datetime
    ) -> None:
        await session.execute(
            update(KnowledgeParseSnapshotRow)
            .where(
                KnowledgeParseSnapshotRow.tenant_id == row.tenant_id,
                KnowledgeParseSnapshotRow.document_id == row.id,
            )
            .values(expires_at=now + SNAPSHOT_GRACE_AFTER_CONFIRM)
        )

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
        async with self._sf() as session:
            existing = (
                await session.execute(
                    select(KnowledgeDocumentRow)
                    .where(
                        KnowledgeDocumentRow.tenant_id == tenant_id,
                        KnowledgeDocumentRow.kb_id == kb_id,
                        KnowledgeDocumentRow.filename == filename,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if existing is not None:
                ready_same = (
                    existing.status == DocumentStatus.READY.value
                    and existing.content_sha256 == content_sha256
                )
                under_way_same = (
                    existing.status in _UNDER_WAY_VALUES
                    and _estimated_sha(existing) == content_sha256
                    # Past its deadline it is about to be withdrawn: estimate again.
                    and not is_expired(existing.confirm_by, now)
                )
                if ready_same or under_way_same:
                    await session.commit()
                    return UploadStart(self._document_of(existing), "unchanged")
                if writing_chunks(existing.status, existing.stage):
                    await session.commit()
                    return UploadStart(self._document_of(existing), "busy")
                row = existing
                meta = begin_meta(
                    status=row.status,
                    raw=row.ingest_estimate,
                    origin=EstimateOrigin.REUPLOAD,
                    allow_duplicate=False,
                    restore=_restore_of(row),
                )
                # Into the pending slot even when nothing serves: withdrawn, it
                # goes with the slot; ingested, it is promoted (READY).
                row.pending_content = content
                row.pending_content_sha256 = content_sha256
                if row.doc_seq is None:
                    row.doc_seq = await self._next_doc_seq(session, tenant_id)
                row.error = None
                row.error_code = None
                row.stage = IngestStage.QUEUED.value
                row.attempts = 0
                row.ingest_generation = row.ingest_generation + 1
                row.claimed_at = None
                row.lease_until = None
                row.updated_at = now
                await _drop_snapshot(session, tenant_id, row.id)
                exclude: UUID | None = row.id
            else:
                meta = begin_meta(
                    status=None,
                    raw=None,
                    origin=EstimateOrigin.UPLOAD,
                    allow_duplicate=False,
                    restore=None,
                )
                facts = DocumentFileFacts.of(filename, content)
                row = KnowledgeDocumentRow(
                    id=uuid4(),
                    tenant_id=tenant_id,
                    kb_id=kb_id,
                    filename=filename,
                    status=DocumentStatus.ESTIMATING.value,
                    content=content,
                    content_sha256=content_sha256,
                    doc_seq=await self._next_doc_seq(session, tenant_id),
                    title=facts.title,
                    mime=facts.mime,
                    size_bytes=facts.size_bytes,
                    source_type=DocumentSourceType.FILE.value,
                    stage=IngestStage.QUEUED.value,
                )
                session.add(row)
                exclude = None
            duplicate = (
                await self._duplicate_of(
                    session,
                    tenant_id=tenant_id,
                    kb_id=kb_id,
                    sha=content_sha256,
                    exclude=exclude,
                    before=None,
                )
                if checks_duplicates(meta)
                else None
            )
            if duplicate is not None:
                row.status = DocumentStatus.AWAITING_CONFIRMATION.value
                row.ingest_estimate = duplicate_estimate(meta=meta, duplicate_of=duplicate, now=now)
                row.confirm_by = now + CONFIRM_WINDOW
            else:
                row.status = DocumentStatus.ESTIMATING.value
                row.ingest_estimate = dict(meta)
                row.confirm_by = None
            await session.commit()
            await session.refresh(row)
            return UploadStart(
                self._document_of(row), "duplicate" if duplicate is not None else "estimating"
            )

    async def begin_estimate(
        self,
        *,
        tenant_id: UUID,
        document_id: UUID,
        origin: EstimateOrigin,
        allow_duplicate: bool = False,
        expect_status: DocumentStatus | None = None,
        expect_estimate_id: UUID | None = None,
    ) -> KnowledgeDocument | None:
        async with self._sf() as session:
            row = await self._lock(session, tenant_id, document_id)
            if row is None:
                await session.commit()
                return None
            refused = estimate_refusal(
                status=row.status,
                stage=row.stage,
                raw=row.ingest_estimate,
                expect_status=expect_status,
                expect_estimate_id=expect_estimate_id,
            )
            if refused is not None:
                await session.commit()
                raise EstimateRefused(refused)
            meta = begin_meta(
                status=row.status,
                raw=row.ingest_estimate,
                origin=origin,
                allow_duplicate=allow_duplicate,
                restore=_restore_of(row),
            )
            row.status = DocumentStatus.ESTIMATING.value
            row.ingest_estimate = dict(meta)
            row.error = None
            row.error_code = None
            row.stage = IngestStage.QUEUED.value
            row.attempts = 0
            row.ingest_generation = row.ingest_generation + 1
            row.confirm_by = None
            row.claimed_at = None
            row.lease_until = None
            row.updated_at = datetime.now(UTC)
            await _drop_snapshot(session, tenant_id, document_id)
            await session.commit()
            await session.refresh(row)
            return self._document_of(row)

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
        async with self._sf() as session:
            row = await self._lock(session, tenant_id, document_id)
            if (
                row is None
                or row.status != DocumentStatus.ESTIMATING.value
                or row.ingest_generation != generation
            ):
                await session.commit()
                return None
            confirm_by = kept_deadline(row.ingest_estimate, confirm_by)
            meta = meta_of(row.ingest_estimate)
            raw = with_estimate(meta, estimate)
            if checks_duplicates(meta):
                duplicate = await self._duplicate_of(
                    session,
                    tenant_id=tenant_id,
                    kb_id=row.kb_id,
                    sha=_estimated_sha(row),
                    exclude=row.id,
                    before=row,
                )
                if duplicate is not None:
                    raw["duplicate_of"] = duplicate
            row.status = DocumentStatus.AWAITING_CONFIRMATION.value
            row.ingest_estimate = raw
            row.confirm_by = confirm_by
            row.claimed_at = None
            row.lease_until = None
            row.updated_at = datetime.now(UTC)
            await self._keep_snapshot(session, row, raw, snapshot, confirm_by)
            await session.commit()
            await session.refresh(row)
            return self._document_of(row)

    async def confirm_documents(
        self,
        *,
        tenant_id: UUID,
        kb_id: UUID,
        estimates: Sequence[tuple[UUID, UUID]],
        confirmed_by: str | None,
        now: datetime,
    ) -> dict[UUID, ConfirmOutcome]:
        wanted: dict[UUID, UUID] = {}
        for document_id, estimate_id in estimates:
            wanted.setdefault(document_id, estimate_id)
        if not wanted:
            return {}
        outcomes: dict[UUID, ConfirmOutcome] = {}
        async with self._sf() as session:
            rows = {
                row.id: row
                for row in (
                    await session.execute(
                        select(KnowledgeDocumentRow)
                        .where(
                            KnowledgeDocumentRow.tenant_id == tenant_id,
                            KnowledgeDocumentRow.kb_id == kb_id,
                            KnowledgeDocumentRow.id.in_(list(wanted)),
                        )
                        .order_by(KnowledgeDocumentRow.id)
                        .with_for_update()
                    )
                ).scalars()
            }
            for document_id, estimate_id in wanted.items():
                row = rows.get(document_id)
                if row is None:
                    outcomes[document_id] = "not_found"
                    continue
                refused = confirm_outcome(
                    status=row.status,
                    raw=row.ingest_estimate,
                    estimate_id=estimate_id,
                    confirm_by=row.confirm_by,
                    now=now,
                )
                if refused is not None:
                    outcomes[document_id] = refused
                    continue
                self._confirm_row(row, confirmed_by, now)
                await self._extend_snapshot(session, row, now)
                outcomes[document_id] = "confirmed"
            await session.commit()
        return outcomes

    async def confirm_all(
        self,
        *,
        tenant_id: UUID,
        kb_id: UUID,
        expected_count: int,
        expected_total_micros: int,
        expected_fingerprint: str,
        confirmed_by: str | None,
        now: datetime,
        withdraw_duplicates: Sequence[UUID] = (),
    ) -> ConfirmAllResult:
        seen = frozenset(withdraw_duplicates)
        async with self._sf() as session:
            rows = list(
                (
                    await session.execute(
                        select(KnowledgeDocumentRow)
                        .where(
                            KnowledgeDocumentRow.tenant_id == tenant_id,
                            KnowledgeDocumentRow.kb_id == kb_id,
                            KnowledgeDocumentRow.status.in_(_WAITING_VALUES),
                        )
                        .order_by(KnowledgeDocumentRow.id)
                        .with_for_update()
                    )
                ).scalars()
            )
            summary = summarize(
                ((row.id, row.status, row.ingest_estimate, row.confirm_by) for row in rows),
                now=now,
            )
            if not summary_matches(
                summary,
                count=expected_count,
                total_micros=expected_total_micros,
                fingerprint=expected_fingerprint,
            ):
                await session.commit()
                return ConfirmAllResult(matched=False, summary=summary)
            confirmed_ids: list[UUID] = []
            skipped: list[UUID] = []
            withdrawn: dict[UUID, WithdrawOutcome] = {}
            for row in rows:
                if row.status != DocumentStatus.AWAITING_CONFIRMATION.value:
                    continue
                if is_expired(row.confirm_by, now):
                    skipped.append(row.id)  # the expiry sweep withdraws it
                    continue
                if is_duplicate(row.ingest_estimate) or not is_estimated(row.ingest_estimate):
                    if withdraws_with_confirm_all(row.id, row.ingest_estimate, seen):
                        withdrawn[row.id] = await self._withdraw_row(session, row, now)
                    continue
                self._confirm_row(row, confirmed_by, now)
                await self._extend_snapshot(session, row, now)
                confirmed_ids.append(row.id)
            await session.commit()
        return ConfirmAllResult(
            matched=True,
            summary=summary,
            confirmed=tuple(confirmed_ids),
            withdrawn=withdrawn,
            skipped=tuple(skipped),
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
        async with self._sf() as session:
            row = await self._lock(session, tenant_id, document_id)
            if (
                row is None
                or row.status != DocumentStatus.PROCESSING.value
                or row.ingest_generation != generation
                or writing_chunks(row.status, row.stage)
            ):
                await session.commit()
                return False
            raw = with_estimate(meta_of(row.ingest_estimate), estimate)
            row.status = DocumentStatus.AWAITING_CONFIRMATION.value
            row.ingest_estimate = raw
            row.confirm_by = confirm_by
            row.claimed_at = None
            row.lease_until = None
            row.updated_at = datetime.now(UTC)
            await self._keep_snapshot(session, row, raw, snapshot, confirm_by)
            await session.commit()
        return True

    async def expire_awaiting(self, *, now: datetime, limit: int) -> list[ExpiredEstimate]:
        expired: list[ExpiredEstimate] = []
        async with self._sf() as session:
            rows = list(
                (
                    await session.execute(
                        select(KnowledgeDocumentRow)
                        .where(
                            KnowledgeDocumentRow.status
                            == DocumentStatus.AWAITING_CONFIRMATION.value,
                            KnowledgeDocumentRow.confirm_by < now,
                        )
                        .order_by(KnowledgeDocumentRow.confirm_by, KnowledgeDocumentRow.id)
                        .limit(limit)
                        .with_for_update(skip_locked=True)
                    )
                ).scalars()
            )
            for row in rows:
                tenant_id, kb_id, document_id, filename = (
                    row.tenant_id,
                    row.kb_id,
                    row.id,
                    row.filename,
                )
                outcome = await self._withdraw_row(session, row, now)
                expired.append(
                    ExpiredEstimate(
                        tenant_id=tenant_id,
                        kb_id=kb_id,
                        document_id=document_id,
                        filename=filename,
                        outcome=outcome,
                    )
                )
            await session.commit()
        return expired

    async def get_parse_snapshot(
        self, *, tenant_id: UUID, document_id: UUID, estimate_id: UUID
    ) -> ParseSnapshot | None:
        async with self._sf() as session:
            found = (
                await session.execute(
                    select(
                        KnowledgeParseSnapshotRow.format, KnowledgeParseSnapshotRow.payload
                    ).where(
                        KnowledgeParseSnapshotRow.tenant_id == tenant_id,
                        KnowledgeParseSnapshotRow.document_id == document_id,
                        KnowledgeParseSnapshotRow.estimate_id == estimate_id,
                    )
                )
            ).first()
        return ParseSnapshot(format=found.format, payload=found.payload) if found else None

    async def purge_expired_snapshots(self, *, now: datetime) -> int:
        async with self._sf() as session:
            purged = (
                await session.execute(
                    delete(KnowledgeParseSnapshotRow)
                    .where(KnowledgeParseSnapshotRow.expires_at < now)
                    .returning(KnowledgeParseSnapshotRow.document_id)
                )
            ).all()
            await session.commit()
        return len(purged)

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
        async with self._sf() as session:
            row = await self._lock(session, tenant_id, document_id)
            if row is None or not actual_lands(
                row_generation=row.ingest_generation,
                row_status=row.status,
                row_estimate=row.ingest_estimate,
                generation=generation,
                estimate_id=estimate_id,
            ):
                await session.commit()
                return False
            row.ingest_actual = merge_actual(row.ingest_actual, actual)
            if settled:
                await _drop_snapshot(session, tenant_id, document_id)
            await session.commit()
        return True

    async def estimate_summary(
        self, *, tenant_id: UUID, kb_id: UUID, now: datetime | None = None
    ) -> EstimateSummary:
        async with self._sf() as session:
            rows = (
                await session.execute(
                    select(
                        KnowledgeDocumentRow.id,
                        KnowledgeDocumentRow.status,
                        KnowledgeDocumentRow.ingest_estimate,
                        KnowledgeDocumentRow.confirm_by,
                    ).where(
                        KnowledgeDocumentRow.tenant_id == tenant_id,
                        KnowledgeDocumentRow.kb_id == kb_id,
                        KnowledgeDocumentRow.status.in_(_WAITING_VALUES),
                    )
                )
            ).all()
        return summarize(
            ((row.id, row.status, row.ingest_estimate, row.confirm_by) for row in rows),
            now=now or datetime.now(UTC),
        )

    async def release_stale_duplicates(
        self,
        *,
        tenant_id: UUID | None = None,
        kb_id: UUID | None = None,
        now: datetime | None = None,
    ) -> list[KnowledgeDocument]:
        now = now or datetime.now(UTC)
        where = [
            KnowledgeDocumentRow.status == DocumentStatus.AWAITING_CONFIRMATION.value,
            KnowledgeDocumentRow.ingest_estimate["duplicate_of"].is_not(None),
            func.jsonb_typeof(KnowledgeDocumentRow.ingest_estimate["duplicate_of"]) == "object",
            # Past its deadline: the expiry sweep withdraws it.
            or_(
                KnowledgeDocumentRow.confirm_by.is_(None),
                KnowledgeDocumentRow.confirm_by > now,
            ),
        ]
        if tenant_id is not None:
            where.append(KnowledgeDocumentRow.tenant_id == tenant_id)
        if kb_id is not None:
            where.append(KnowledgeDocumentRow.kb_id == kb_id)
        released: list[KnowledgeDocument] = []
        after: UUID | None = None
        while True:  # one transaction per batch: bounded locks and binds
            batch = [*where, KnowledgeDocumentRow.id > after] if after is not None else where
            scanned, changed = await self._release_batch(batch)
            released.extend(changed)
            if len(scanned) < _RELEASE_BATCH:
                return released
            after = scanned[-1]

    async def _release_batch(
        self, where: Sequence[ColumnElement[bool]]
    ) -> tuple[list[UUID], list[KnowledgeDocument]]:
        """One batch of :meth:`release_stale_duplicates`: the ids scanned (in
        order) and the documents released."""
        released: list[KnowledgeDocumentRow] = []
        async with self._sf() as session:
            rows = list(
                (
                    await session.execute(
                        select(KnowledgeDocumentRow)
                        .where(*where)
                        .order_by(KnowledgeDocumentRow.id)
                        .limit(_RELEASE_BATCH)
                        .with_for_update(skip_locked=True)
                    )
                ).scalars()
            )
            scanned = [row.id for row in rows]
            wanted = {
                original_id
                for row in rows
                if (original_id := duplicate_ref_id(row.ingest_estimate)) is not None
            }
            holders = {}
            if wanted:
                found_rows = await session.execute(
                    select(
                        KnowledgeDocumentRow.id,
                        KnowledgeDocumentRow.tenant_id,
                        KnowledgeDocumentRow.kb_id,
                        KnowledgeDocumentRow.content_sha256,
                        KnowledgeDocumentRow.pending_content_sha256,
                    ).where(KnowledgeDocumentRow.id.in_(sorted(wanted)))
                )
                holders = {found.id: found for found in found_rows.all()}
            for row in rows:
                original_id = duplicate_ref_id(row.ingest_estimate)
                original = holders.get(original_id) if original_id is not None else None
                sha = _estimated_sha(row)
                if (
                    original is not None
                    and original.tenant_id == row.tenant_id
                    and original.kb_id == row.kb_id
                    and sha is not None
                    and sha in (original.content_sha256, original.pending_content_sha256)
                ):
                    continue
                status, kept = released_duplicate(
                    row.ingest_estimate or {}, confirm_by=row.confirm_by
                )
                row.status = status.value
                row.ingest_estimate = kept
                row.updated_at = datetime.now(UTC)
                if status is DocumentStatus.ESTIMATING:
                    row.ingest_generation = row.ingest_generation + 1
                    row.attempts = 0
                    row.confirm_by = None
                    row.claimed_at = None
                    row.lease_until = None
                released.append(row)
            await session.commit()
            for row in released:
                await session.refresh(row)
            return scanned, [self._document_of(row) for row in released]

    async def chunk_hashes_on_model(
        self,
        *,
        tenant_id: UUID,
        document_id: UUID,
        content_hashes: Sequence[str],
        embedding_model: str,
    ) -> set[str]:
        wanted = sorted(set(content_hashes))
        found: set[str] = set()
        async with self._sf() as session:
            for start in range(0, len(wanted), _HASH_READ_BATCH):
                # One ``text[]`` bind (``= ANY``), as the vector read: a document
                # can have tens of thousands of chunks.
                hashes = literal(wanted[start : start + _HASH_READ_BATCH], ARRAY(Text))
                rows = (
                    await session.execute(
                        select(KnowledgeChunkRow.content_hash)
                        .where(
                            KnowledgeChunkRow.tenant_id == tenant_id,
                            KnowledgeChunkRow.document_id == document_id,
                            KnowledgeChunkRow.chunk_type.in_(_SEARCHABLE_VALUES),
                            KnowledgeChunkRow.embedding_model == embedding_model,
                            KnowledgeChunkRow.content_hash == any_(hashes),
                        )
                        .distinct()
                    )
                ).scalars()
                found.update(digest for digest in rows if digest is not None)
        return found


async def _drop_snapshot(session: AsyncSession, tenant_id: UUID, document_id: UUID) -> None:
    await session.execute(
        delete(KnowledgeParseSnapshotRow).where(
            KnowledgeParseSnapshotRow.tenant_id == tenant_id,
            KnowledgeParseSnapshotRow.document_id == document_id,
        )
    )
