"""B-267 step 2 —— the base-profile methods of ``SqlKnowledgeStore``.

Every write is a read-modify-write of ``knowledge_base.profile`` under the
row lock, through the pure transitions in :mod:`.profiles` — the same ones the
in-memory store runs. ``updated_at`` is not bumped: a profile refresh is not an
edit of the base.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import DateTime, String, and_, case, cast, func, literal, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.sql.elements import ColumnElement

from expert_work.persistence.knowledge import profiles
from expert_work.persistence.knowledge.profiles import ProfileClaim, ProfileDigest
from expert_work.persistence.models import KnowledgeBaseRow, KnowledgeChunkRow, KnowledgeDocumentRow
from expert_work.protocol import DocumentStatus
from expert_work.protocol.knowledge_profile import (
    KnowledgeProfile,
    KnowledgeProfileRecord,
    ProbeCalibration,
    dump_profile_record,
    parse_profile_record,
)

#: Due refreshes one claim pass reads (earliest due first).
_CLAIM_SCAN_LIMIT = 200
_INGESTING = (DocumentStatus.PENDING.value, DocumentStatus.PROCESSING.value)
#: The shape pydantic writes a ``due_at`` in; anything else is never cast (a
#: cast of a malformed value would fail the whole claim query).
_ISO_TIMESTAMP = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$"


def _due_at() -> ColumnElement[Any]:
    raw = KnowledgeBaseRow.profile["refresh"]["due_at"].astext
    return case((raw.op("~")(literal(_ISO_TIMESTAMP, String)), cast(raw, DateTime(timezone=True))))


async def mark_profile_stale(
    session: AsyncSession, tenant_id: UUID, kb_id: UUID, now: datetime
) -> None:
    """B-267 —— inside the caller's transaction: the base's profile no longer
    shows (a document it may describe left the base). Locks the base row
    after the caller's document rows — the order every writer here uses."""
    row = await SqlProfiles._locked_base(session, tenant_id, kb_id)
    if row is None:
        return
    record = profiles.staled(parse_profile_record(row.profile), now=now)
    if record is not None:
        row.profile = dump_profile_record(record)


class SqlProfiles:
    """The profile methods of ``SqlKnowledgeStore``."""

    _sf: async_sessionmaker[AsyncSession]

    @staticmethod
    async def _locked_base(
        session: AsyncSession, tenant_id: UUID, kb_id: UUID
    ) -> KnowledgeBaseRow | None:
        return (
            await session.execute(
                select(KnowledgeBaseRow)
                .where(KnowledgeBaseRow.tenant_id == tenant_id, KnowledgeBaseRow.id == kb_id)
                .with_for_update()
            )
        ).scalar_one_or_none()

    async def _rewrite_profile(
        self,
        tenant_id: UUID,
        kb_id: UUID,
        change: Callable[[KnowledgeProfileRecord | None], KnowledgeProfileRecord | None],
    ) -> bool:
        async with self._sf() as session:
            row = await self._locked_base(session, tenant_id, kb_id)
            if row is None:
                await session.commit()
                return False
            row.profile = dump_profile_record(change(parse_profile_record(row.profile)))
            await session.commit()
        return True

    async def mark_profile_refresh(
        self, *, tenant_id: UUID, kb_id: UUID, now: datetime, calibrate_only: bool = False
    ) -> bool:
        return await self._rewrite_profile(
            tenant_id,
            kb_id,
            lambda record: profiles.marked(record, now=now, calibrate_only=calibrate_only),
        )

    async def claim_profile_refreshes(self, *, now: datetime, limit: int) -> list[ProfileClaim]:
        due = _due_at()
        async with self._sf() as session:
            rows = (
                (
                    await session.execute(
                        select(KnowledgeBaseRow)
                        .where(and_(due.is_not(None), due <= now))
                        .order_by(due)
                        .limit(_CLAIM_SCAN_LIMIT)
                        .with_for_update(skip_locked=True)
                    )
                )
                .scalars()
                .all()
            )
            ingesting = await self._ingesting_bases(session, [row.id for row in rows])
            claims: list[ProfileClaim] = []
            for row in rows:
                if len(claims) >= limit:
                    break
                claim = _claim_row(row, now=now, ingesting=row.id in ingesting)
                if claim is not None:
                    claims.append(claim)
            await session.commit()
        return claims

    @staticmethod
    async def _ingesting_bases(session: AsyncSession, kb_ids: list[UUID]) -> set[UUID]:
        if not kb_ids:
            return set()
        result = await session.execute(
            select(KnowledgeDocumentRow.kb_id)
            .where(
                KnowledgeDocumentRow.kb_id.in_(kb_ids),
                KnowledgeDocumentRow.status.in_(_INGESTING),
            )
            .distinct()
        )
        return set(result.scalars().all())

    async def finish_profile_refresh(
        self,
        *,
        tenant_id: UUID,
        kb_id: UUID,
        holder: str,
        marks: int,
        profile: KnowledgeProfile | None,
        calibration: ProbeCalibration | None,
        now: datetime,
        retry_calibration: bool = False,
    ) -> None:
        await self._rewrite_profile(
            tenant_id,
            kb_id,
            lambda record: profiles.finished(
                record,
                holder=holder,
                marks=marks,
                profile=profile,
                calibration=calibration,
                now=now,
                retry_calibration=retry_calibration,
            ),
        )

    async def fail_profile_refresh(
        self, *, tenant_id: UUID, kb_id: UUID, holder: str, now: datetime
    ) -> None:
        await self._rewrite_profile(
            tenant_id, kb_id, lambda record: profiles.failed(record, holder=holder, now=now)
        )

    async def profile_digest(
        self, *, tenant_id: UUID, kb_id: UUID, max_titles: int, detailed: int, max_headings: int
    ) -> ProfileDigest:
        in_base = (
            KnowledgeDocumentRow.tenant_id == tenant_id,
            KnowledgeDocumentRow.kb_id == kb_id,
            KnowledgeDocumentRow.enabled.is_(True),
            KnowledgeDocumentRow.chunk_count > 0,
        )
        async with self._sf() as session:
            total = (
                await session.execute(
                    select(func.count()).select_from(KnowledgeDocumentRow).where(*in_base)
                )
            ).scalar_one()
            docs = (
                await session.execute(
                    select(
                        KnowledgeDocumentRow.id,
                        KnowledgeDocumentRow.title,
                        KnowledgeDocumentRow.filename,
                    )
                    .where(*in_base)
                    .order_by(KnowledgeDocumentRow.created_at.desc(), KnowledgeDocumentRow.filename)
                    .limit(max_titles)
                )
            ).all()
            sampled = [docs[i].id for i in profiles.sample_positions(len(docs), detailed)]
            headings = await self._digest_headings(session, tenant_id, sampled)
            openings = await self._digest_openings(session, tenant_id, sampled)
        detail = set(sampled)
        digests = tuple(
            profiles.document_digest(
                doc.title or doc.filename,
                headings[doc.id],
                openings.get(doc.id),
                max_headings=max_headings,
            )
            if doc.id in detail
            else profiles.DocumentDigest(title=doc.title or doc.filename)
            for doc in docs
        )
        return ProfileDigest(documents=digests, ready_documents=int(total))

    @staticmethod
    async def _digest_headings(
        session: AsyncSession, tenant_id: UUID, document_ids: list[UUID]
    ) -> defaultdict[UUID, list[list[str] | None]]:
        """Each sampled document's distinct heading paths among its first
        :data:`profiles.PROFILE_HEADING_SCAN` chunks, in order of appearance."""
        out: defaultdict[UUID, list[list[str] | None]] = defaultdict(list)
        if not document_ids:
            return out
        first = func.min(KnowledgeChunkRow.chunk_index)
        rows = await session.execute(
            select(KnowledgeChunkRow.document_id, KnowledgeChunkRow.heading_path)
            .where(
                KnowledgeChunkRow.tenant_id == tenant_id,
                KnowledgeChunkRow.document_id.in_(document_ids),
                KnowledgeChunkRow.enabled.is_(True),
                KnowledgeChunkRow.heading_path.is_not(None),
                KnowledgeChunkRow.chunk_index < profiles.PROFILE_HEADING_SCAN,
            )
            .group_by(KnowledgeChunkRow.document_id, KnowledgeChunkRow.heading_path)
            .order_by(KnowledgeChunkRow.document_id, first)
        )
        for document_id, path in rows.all():
            out[document_id].append(path)
        return out

    @staticmethod
    async def _digest_openings(
        session: AsyncSession, tenant_id: UUID, document_ids: list[UUID]
    ) -> dict[UUID, str]:
        """The start of each sampled document's first enabled chunk only."""
        if not document_ids:
            return {}
        rows = await session.execute(
            select(
                KnowledgeChunkRow.document_id,
                func.substr(KnowledgeChunkRow.content, 1, profiles.PROFILE_OPENING_CHARS * 2),
            )
            .where(
                KnowledgeChunkRow.tenant_id == tenant_id,
                KnowledgeChunkRow.document_id.in_(document_ids),
                KnowledgeChunkRow.enabled.is_(True),
            )
            .distinct(KnowledgeChunkRow.document_id)
            .order_by(KnowledgeChunkRow.document_id, KnowledgeChunkRow.chunk_index)
        )
        return dict(rows.tuples().all())


def _claim_row(row: KnowledgeBaseRow, *, now: datetime, ingesting: bool) -> ProfileClaim | None:
    """Claim one locked row, or clean it: an unreadable record loses its
    refresh (it would sit in every claim pass), a used-up refresh is dropped."""
    record = parse_profile_record(row.profile)
    if record is None:
        raw = dict(row.profile or {})
        raw.pop("refresh", None)
        row.profile = raw or None
        return None
    if profiles.exhausted(record):
        row.profile = dump_profile_record(profiles.dropped(record))
        return None
    if not profiles.claimable(record, now=now, ingesting=ingesting):
        return None
    held = profiles.claimed(record, now=now)
    row.profile = dump_profile_record(held)
    assert held.refresh is not None and held.refresh.holder is not None  # noqa: S101
    return ProfileClaim(
        tenant_id=row.tenant_id,
        kb_id=row.id,
        marks=held.refresh.marks,
        attempts=held.refresh.attempts,
        calibrate_only=held.refresh.calibrate_only,
        holder=held.refresh.holder,
    )
