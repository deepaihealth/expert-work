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
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

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

#: How many bases one claim pass looks at (due or not): refreshes are rare and
#: the table is small, so the pass reads the rows that carry a refresh at all.
_CLAIM_SCAN_LIMIT = 200
_INGESTING = (DocumentStatus.PENDING.value, DocumentStatus.PROCESSING.value)
#: Only a document's first chunks are read for its headings and opening: a
#: digest keeps a handful of headings, and a long document's rows are many.
_DIGEST_CHUNK_SCAN = 400


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
        async with self._sf() as session:
            rows = (
                (
                    await session.execute(
                        select(KnowledgeBaseRow)
                        .where(KnowledgeBaseRow.profile.has_key("refresh"))
                        .order_by(KnowledgeBaseRow.created_at.asc())
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
                record = parse_profile_record(row.profile)
                if record is None or not profiles.claimable(
                    record, now=now, ingesting=row.id in ingesting
                ):
                    continue
                held = profiles.claimed(record, now=now)
                row.profile = dump_profile_record(held)
                assert held.refresh is not None  # noqa: S101 - claimed keeps the refresh
                claims.append(
                    ProfileClaim(
                        tenant_id=row.tenant_id,
                        kb_id=row.id,
                        marks=held.refresh.marks,
                        attempts=held.refresh.attempts,
                        calibrate_only=held.refresh.calibrate_only,
                    )
                )
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
        marks: int,
        profile: KnowledgeProfile | None,
        calibration: ProbeCalibration | None,
    ) -> None:
        await self._rewrite_profile(
            tenant_id,
            kb_id,
            lambda record: profiles.finished(
                record, marks=marks, profile=profile, calibration=calibration
            ),
        )

    async def fail_profile_refresh(self, *, tenant_id: UUID, kb_id: UUID, now: datetime) -> None:
        await self._rewrite_profile(
            tenant_id, kb_id, lambda record: profiles.failed(record, now=now)
        )

    async def profile_digest(
        self, *, tenant_id: UUID, kb_id: UUID, max_documents: int, max_headings: int
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
                    .order_by(KnowledgeDocumentRow.created_at.asc(), KnowledgeDocumentRow.filename)
                    .limit(max_documents)
                )
            ).all()
            chunks = await self._digest_chunks(session, tenant_id, [doc.id for doc in docs])
        digests = tuple(
            profiles.document_digest(
                doc.title or doc.filename,
                [path for _index, path, _text in chunks[doc.id]],
                chunks[doc.id][0][2] if chunks[doc.id] else None,
                max_headings=max_headings,
            )
            for doc in docs
        )
        return ProfileDigest(documents=digests, ready_documents=int(total))

    @staticmethod
    async def _digest_chunks(
        session: AsyncSession, tenant_id: UUID, document_ids: list[UUID]
    ) -> defaultdict[UUID, list[tuple[int, list[str] | None, str]]]:
        """Each document's enabled chunks in order: index, heading path and
        the start of the text (only the first chunk's is used)."""
        out: defaultdict[UUID, list[tuple[int, list[str] | None, str]]] = defaultdict(list)
        if not document_ids:
            return out
        rows = await session.execute(
            select(
                KnowledgeChunkRow.document_id,
                KnowledgeChunkRow.chunk_index,
                KnowledgeChunkRow.heading_path,
                func.substr(KnowledgeChunkRow.content, 1, profiles.PROFILE_OPENING_CHARS * 2),
            )
            .where(
                KnowledgeChunkRow.tenant_id == tenant_id,
                KnowledgeChunkRow.document_id.in_(document_ids),
                KnowledgeChunkRow.enabled.is_(True),
                KnowledgeChunkRow.chunk_index < _DIGEST_CHUNK_SCAN,
            )
            .order_by(KnowledgeChunkRow.document_id, KnowledgeChunkRow.chunk_index)
        )
        for document_id, index, path, text in rows.all():
            out[document_id].append((index, path, text))
        return out
