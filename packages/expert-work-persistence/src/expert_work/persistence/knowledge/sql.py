"""SQLAlchemy + pgvector ``KnowledgeStore`` (Postgres / asyncpg)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.sql.elements import ColumnElement

from expert_work.persistence.knowledge.base import (
    UNSET,
    ClaimedIngestion,
    DuplicateKnowledgeBaseError,
    KnowledgeStore,
    _Unset,
)
from expert_work.persistence.knowledge.text_search import tokenize_for_search
from expert_work.persistence.models import (
    KnowledgeBaseRow,
    KnowledgeChunkRow,
    KnowledgeDocumentRow,
)
from expert_work.protocol import (
    DEFAULT_CHUNK_MAX_TOKENS,
    DEFAULT_CHUNK_OVERLAP_TOKENS,
    DEFAULT_RETRIEVAL_TOP_K,
    DocumentStatus,
    KnowledgeBase,
    KnowledgeChunk,
    KnowledgeDocument,
    RetrievalMethod,
    ScoredChunk,
)

#: Postgres text-search config — chunks are pre-segmented app-side, so
#: ``simple`` (no stemming / stopwords) is the correct universal config.
_TS_CONFIG = "simple"

#: Terminal ingestion states — reaching one clears the durability lease so the
#: document is never re-claimed.
_TERMINAL_STATUSES = (DocumentStatus.READY.value, DocumentStatus.FAILED.value)


def _claimable(now: datetime, max_attempts: int) -> ColumnElement[bool]:
    """A document is claimable when it is ``pending``, or ``processing`` with
    an expired (or absent) lease, and still under its retry budget."""
    return and_(
        KnowledgeDocumentRow.attempts < max_attempts,
        or_(
            KnowledgeDocumentRow.status == DocumentStatus.PENDING.value,
            and_(
                KnowledgeDocumentRow.status == DocumentStatus.PROCESSING.value,
                or_(
                    KnowledgeDocumentRow.lease_until.is_(None),
                    KnowledgeDocumentRow.lease_until < now,
                ),
            ),
        ),
    )


def _to_base(row: KnowledgeBaseRow) -> KnowledgeBase:
    return KnowledgeBase(
        id=row.id,
        tenant_id=row.tenant_id,
        name=row.name,
        description=row.description,
        created_by=row.created_by,
        chunk_max_tokens=row.chunk_max_tokens,
        chunk_overlap_tokens=row.chunk_overlap_tokens,
        retrieval_top_k=row.retrieval_top_k,
        retrieval_score_threshold=row.retrieval_score_threshold,
        retrieval_method=RetrievalMethod(row.retrieval_method),
        rerank_enabled=row.rerank_enabled,
        embedding_provider=row.embedding_provider,
        embedding_model=row.embedding_model,
        reindex_requested_at=row.reindex_requested_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _to_document(row: KnowledgeDocumentRow) -> KnowledgeDocument:
    return KnowledgeDocument(
        id=row.id,
        tenant_id=row.tenant_id,
        kb_id=row.kb_id,
        filename=row.filename,
        status=DocumentStatus(row.status),
        error=row.error,
        chunk_count=row.chunk_count,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _to_chunk(row: KnowledgeChunkRow) -> KnowledgeChunk:
    return KnowledgeChunk(
        id=row.id,
        tenant_id=row.tenant_id,
        kb_id=row.kb_id,
        document_id=row.document_id,
        chunk_index=row.chunk_index,
        content=row.content,
        embedding=tuple(float(value) for value in row.embedding),
        created_at=row.created_at,
    )


class SqlKnowledgeStore(KnowledgeStore):
    """Postgres-backed knowledge repository (pgvector)."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sf = session_factory

    # -- knowledge bases ----------------------------------------------------

    async def create_base(
        self,
        *,
        tenant_id: UUID,
        name: str,
        description: str | None = None,
        created_by: str | None = None,
        chunk_max_tokens: int = DEFAULT_CHUNK_MAX_TOKENS,
        chunk_overlap_tokens: int = DEFAULT_CHUNK_OVERLAP_TOKENS,
        retrieval_top_k: int = DEFAULT_RETRIEVAL_TOP_K,
        retrieval_score_threshold: float | None = None,
        retrieval_method: RetrievalMethod = RetrievalMethod.HYBRID,
        rerank_enabled: bool = True,
        embedding_provider: str | None = None,
        embedding_model: str | None = None,
    ) -> KnowledgeBase:
        row = KnowledgeBaseRow(
            tenant_id=tenant_id,
            name=name,
            description=description,
            created_by=created_by,
            chunk_max_tokens=chunk_max_tokens,
            chunk_overlap_tokens=chunk_overlap_tokens,
            retrieval_top_k=retrieval_top_k,
            retrieval_score_threshold=retrieval_score_threshold,
            retrieval_method=retrieval_method.value,
            rerank_enabled=rerank_enabled,
            embedding_provider=embedding_provider,
            embedding_model=embedding_model,
        )
        try:
            async with self._sf() as session:
                session.add(row)
                await session.commit()
                await session.refresh(row)
        except IntegrityError as exc:
            raise DuplicateKnowledgeBaseError(tenant_id=tenant_id, name=name) from exc
        return _to_base(row)

    async def update_base(
        self,
        *,
        tenant_id: UUID,
        kb_id: UUID,
        description: str | None | _Unset = UNSET,
        chunk_max_tokens: int | None = None,
        chunk_overlap_tokens: int | None = None,
        retrieval_top_k: int | None = None,
        retrieval_score_threshold: float | None | _Unset = UNSET,
        retrieval_method: RetrievalMethod | None = None,
        rerank_enabled: bool | None = None,
    ) -> KnowledgeBase | None:
        async with self._sf() as session:
            row = (
                await session.execute(
                    select(KnowledgeBaseRow).where(
                        KnowledgeBaseRow.tenant_id == tenant_id,
                        KnowledgeBaseRow.id == kb_id,
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                return None
            if not isinstance(description, _Unset):
                row.description = description
            if chunk_max_tokens is not None:
                row.chunk_max_tokens = chunk_max_tokens
            if chunk_overlap_tokens is not None:
                row.chunk_overlap_tokens = chunk_overlap_tokens
            if retrieval_top_k is not None:
                row.retrieval_top_k = retrieval_top_k
            if not isinstance(retrieval_score_threshold, _Unset):
                row.retrieval_score_threshold = retrieval_score_threshold
            if retrieval_method is not None:
                row.retrieval_method = retrieval_method.value
            if rerank_enabled is not None:
                row.rerank_enabled = rerank_enabled
            row.updated_at = datetime.now(UTC)
            await session.commit()
            await session.refresh(row)
            return _to_base(row)

    async def get_base(self, *, tenant_id: UUID, name: str) -> KnowledgeBase | None:
        stmt = select(KnowledgeBaseRow).where(
            KnowledgeBaseRow.tenant_id == tenant_id,
            KnowledgeBaseRow.name == name,
        )
        async with self._sf() as session:
            row = (await session.execute(stmt)).scalar_one_or_none()
        return _to_base(row) if row is not None else None

    async def list_bases(self, *, tenant_id: UUID) -> list[KnowledgeBase]:
        stmt = (
            select(KnowledgeBaseRow)
            .where(KnowledgeBaseRow.tenant_id == tenant_id)
            .order_by(KnowledgeBaseRow.created_at.desc())
        )
        async with self._sf() as session:
            rows = (await session.execute(stmt)).scalars().all()
        return [_to_base(row) for row in rows]

    async def list_bases_all_tenants(self) -> list[KnowledgeBase]:
        # W4 — no tenant filter; caller must wrap in bypass_rls_session().
        stmt = select(KnowledgeBaseRow).order_by(
            KnowledgeBaseRow.created_at.desc(), KnowledgeBaseRow.id
        )
        async with self._sf() as session:
            rows = (await session.execute(stmt)).scalars().all()
        return [_to_base(row) for row in rows]

    async def base_stats(self, *, tenant_id: UUID, kb_id: UUID) -> tuple[int, int]:
        stmt = select(
            func.count(KnowledgeDocumentRow.id),
            func.coalesce(func.sum(KnowledgeDocumentRow.chunk_count), 0),
        ).where(
            KnowledgeDocumentRow.tenant_id == tenant_id,
            KnowledgeDocumentRow.kb_id == kb_id,
        )
        async with self._sf() as session:
            row = (await session.execute(stmt)).one()
        return int(row[0]), int(row[1])

    async def base_stats_many(self, *, tenant_id: UUID) -> dict[UUID, tuple[int, int]]:
        stmt = (
            select(
                KnowledgeDocumentRow.kb_id,
                func.count(KnowledgeDocumentRow.id),
                func.coalesce(func.sum(KnowledgeDocumentRow.chunk_count), 0),
            )
            .where(KnowledgeDocumentRow.tenant_id == tenant_id)
            .group_by(KnowledgeDocumentRow.kb_id)
        )
        async with self._sf() as session:
            rows = (await session.execute(stmt)).all()
        return {row[0]: (int(row[1]), int(row[2])) for row in rows}

    async def base_stats_many_all_tenants(self) -> dict[UUID, tuple[int, int]]:
        # W4 — no tenant filter; caller must wrap in bypass_rls_session().
        stmt = select(
            KnowledgeDocumentRow.kb_id,
            func.count(KnowledgeDocumentRow.id),
            func.coalesce(func.sum(KnowledgeDocumentRow.chunk_count), 0),
        ).group_by(KnowledgeDocumentRow.kb_id)
        async with self._sf() as session:
            rows = (await session.execute(stmt)).all()
        return {row[0]: (int(row[1]), int(row[2])) for row in rows}

    async def stamp_embedding_model(
        self, *, tenant_id: UUID, kb_id: UUID, embedding_provider: str, embedding_model: str
    ) -> None:
        async with self._sf() as session:
            await session.execute(
                update(KnowledgeBaseRow)
                .where(KnowledgeBaseRow.tenant_id == tenant_id, KnowledgeBaseRow.id == kb_id)
                .values(
                    embedding_provider=embedding_provider,
                    embedding_model=embedding_model,
                    updated_at=datetime.now(UTC),
                )
            )
            await session.commit()

    async def request_reindex(self, *, tenant_id: UUID, kb_id: UUID) -> bool:
        async with self._sf() as session:
            exists = (
                await session.execute(
                    select(KnowledgeBaseRow.id).where(
                        KnowledgeBaseRow.tenant_id == tenant_id,
                        KnowledgeBaseRow.id == kb_id,
                    )
                )
            ).scalar_one_or_none()
            if exists is None:
                return False
            await session.execute(
                update(KnowledgeBaseRow)
                .where(KnowledgeBaseRow.tenant_id == tenant_id, KnowledgeBaseRow.id == kb_id)
                .values(reindex_requested_at=datetime.now(UTC), updated_at=datetime.now(UTC))
            )
            await session.commit()
        return True

    async def clear_reindex(self, *, tenant_id: UUID, kb_id: UUID) -> None:
        async with self._sf() as session:
            await session.execute(
                update(KnowledgeBaseRow)
                .where(KnowledgeBaseRow.tenant_id == tenant_id, KnowledgeBaseRow.id == kb_id)
                .values(reindex_requested_at=None, updated_at=datetime.now(UTC))
            )
            await session.commit()

    async def delete_base(self, *, tenant_id: UUID, kb_id: UUID) -> bool:
        async with self._sf() as session:
            exists = (
                await session.execute(
                    select(KnowledgeBaseRow.id).where(
                        KnowledgeBaseRow.tenant_id == tenant_id,
                        KnowledgeBaseRow.id == kb_id,
                    )
                )
            ).scalar_one_or_none()
            if exists is None:
                return False
            await session.execute(
                delete(KnowledgeChunkRow).where(
                    KnowledgeChunkRow.tenant_id == tenant_id,
                    KnowledgeChunkRow.kb_id == kb_id,
                )
            )
            await session.execute(
                delete(KnowledgeDocumentRow).where(
                    KnowledgeDocumentRow.tenant_id == tenant_id,
                    KnowledgeDocumentRow.kb_id == kb_id,
                )
            )
            await session.execute(
                delete(KnowledgeBaseRow).where(
                    KnowledgeBaseRow.tenant_id == tenant_id,
                    KnowledgeBaseRow.id == kb_id,
                )
            )
            await session.commit()
        return True

    # -- documents ----------------------------------------------------------

    async def upsert_document(
        self,
        *,
        tenant_id: UUID,
        kb_id: UUID,
        filename: str,
        content: bytes | None = None,
        content_sha256: str | None = None,
    ) -> KnowledgeDocument:
        async with self._sf() as session:
            existing = (
                await session.execute(
                    select(KnowledgeDocumentRow).where(
                        KnowledgeDocumentRow.tenant_id == tenant_id,
                        KnowledgeDocumentRow.kb_id == kb_id,
                        KnowledgeDocumentRow.filename == filename,
                    )
                )
            ).scalar_one_or_none()
            if existing is not None:
                existing.status = DocumentStatus.PENDING.value
                existing.error = None
                existing.chunk_count = 0
                existing.attempts = 0
                existing.claimed_at = None
                existing.lease_until = None
                if content is not None:
                    existing.content = content
                    existing.content_sha256 = content_sha256
                existing.updated_at = datetime.now(UTC)
                await session.execute(
                    delete(KnowledgeChunkRow).where(
                        KnowledgeChunkRow.tenant_id == tenant_id,
                        KnowledgeChunkRow.document_id == existing.id,
                    )
                )
                await session.commit()
                await session.refresh(existing)
                return _to_document(existing)
            row = KnowledgeDocumentRow(
                tenant_id=tenant_id,
                kb_id=kb_id,
                filename=filename,
                status=DocumentStatus.PENDING.value,
                content=content,
                content_sha256=content_sha256,
            )
            session.add(row)
            await session.commit()
            await session.refresh(row)
            return _to_document(row)

    async def get_document(self, *, tenant_id: UUID, document_id: UUID) -> KnowledgeDocument | None:
        stmt = select(KnowledgeDocumentRow).where(
            KnowledgeDocumentRow.tenant_id == tenant_id,
            KnowledgeDocumentRow.id == document_id,
        )
        async with self._sf() as session:
            row = (await session.execute(stmt)).scalar_one_or_none()
        return _to_document(row) if row is not None else None

    async def get_documents(
        self, *, tenant_id: UUID, document_ids: Sequence[UUID]
    ) -> dict[UUID, KnowledgeDocument]:
        ids = list(dict.fromkeys(document_ids))
        if not ids:
            return {}
        stmt = select(KnowledgeDocumentRow).where(
            KnowledgeDocumentRow.tenant_id == tenant_id,
            KnowledgeDocumentRow.id.in_(ids),
        )
        async with self._sf() as session:
            rows = (await session.execute(stmt)).scalars().all()
        return {row.id: _to_document(row) for row in rows}

    async def get_document_content(self, *, tenant_id: UUID, document_id: UUID) -> bytes | None:
        stmt = select(KnowledgeDocumentRow.content).where(
            KnowledgeDocumentRow.tenant_id == tenant_id,
            KnowledgeDocumentRow.id == document_id,
        )
        async with self._sf() as session:
            return (await session.execute(stmt)).scalar_one_or_none()

    async def claim_document(
        self,
        *,
        tenant_id: UUID,
        document_id: UUID,
        now: datetime,
        lease_seconds: int,
        max_attempts: int,
    ) -> ClaimedIngestion | None:
        lease_until = now + timedelta(seconds=lease_seconds)
        async with self._sf() as session:
            row = (
                (
                    await session.execute(
                        update(KnowledgeDocumentRow)
                        .where(
                            KnowledgeDocumentRow.tenant_id == tenant_id,
                            KnowledgeDocumentRow.id == document_id,
                            _claimable(now, max_attempts),
                        )
                        .values(
                            status=DocumentStatus.PROCESSING.value,
                            claimed_at=now,
                            lease_until=lease_until,
                            attempts=KnowledgeDocumentRow.attempts + 1,
                        )
                        .returning(KnowledgeDocumentRow)
                    )
                )
                .scalars()
                .first()
            )
            if row is None:
                await session.commit()
                return None
            claim = await self._build_claim(session, tenant_id, row)
            await session.commit()
            return claim

    async def claim_documents_for_ingest(
        self, *, now: datetime, lease_seconds: int, limit: int, max_attempts: int
    ) -> list[ClaimedIngestion]:
        lease_until = now + timedelta(seconds=lease_seconds)
        async with self._sf() as session:
            candidate_ids = (
                (
                    await session.execute(
                        select(KnowledgeDocumentRow.id)
                        .where(_claimable(now, max_attempts))
                        .order_by(KnowledgeDocumentRow.created_at.asc())
                        .limit(limit)
                        .with_for_update(skip_locked=True)
                    )
                )
                .scalars()
                .all()
            )
            if not candidate_ids:
                await session.commit()
                return []
            rows = (
                (
                    await session.execute(
                        update(KnowledgeDocumentRow)
                        .where(KnowledgeDocumentRow.id.in_(candidate_ids))
                        .values(
                            status=DocumentStatus.PROCESSING.value,
                            claimed_at=now,
                            lease_until=lease_until,
                            attempts=KnowledgeDocumentRow.attempts + 1,
                        )
                        .returning(KnowledgeDocumentRow)
                    )
                )
                .scalars()
                .all()
            )
            claims = [await self._build_claim(session, row.tenant_id, row) for row in rows]
            await session.commit()
            return claims

    async def _build_claim(
        self, session: AsyncSession, tenant_id: UUID, row: KnowledgeDocumentRow
    ) -> ClaimedIngestion:
        params = (
            await session.execute(
                select(
                    KnowledgeBaseRow.chunk_max_tokens,
                    KnowledgeBaseRow.chunk_overlap_tokens,
                ).where(KnowledgeBaseRow.id == row.kb_id)
            )
        ).first()
        chunk_max, chunk_overlap = (
            (params.chunk_max_tokens, params.chunk_overlap_tokens)
            if params is not None
            else (DEFAULT_CHUNK_MAX_TOKENS, DEFAULT_CHUNK_OVERLAP_TOKENS)
        )
        return ClaimedIngestion(
            tenant_id=tenant_id,
            document_id=row.id,
            kb_id=row.kb_id,
            filename=row.filename,
            content=row.content,
            content_sha256=row.content_sha256,
            chunk_max_tokens=chunk_max,
            chunk_overlap_tokens=chunk_overlap,
            attempts=row.attempts,
        )

    async def mark_document_failed_terminal(
        self, *, tenant_id: UUID, document_id: UUID, error: str
    ) -> None:
        async with self._sf() as session:
            await session.execute(
                update(KnowledgeDocumentRow)
                .where(
                    KnowledgeDocumentRow.tenant_id == tenant_id,
                    KnowledgeDocumentRow.id == document_id,
                )
                .values(
                    status=DocumentStatus.FAILED.value,
                    error=error,
                    claimed_at=None,
                    lease_until=None,
                    updated_at=datetime.now(UTC),
                )
            )
            await session.commit()

    async def list_documents(self, *, tenant_id: UUID, kb_id: UUID) -> list[KnowledgeDocument]:
        stmt = (
            select(KnowledgeDocumentRow)
            .where(
                KnowledgeDocumentRow.tenant_id == tenant_id,
                KnowledgeDocumentRow.kb_id == kb_id,
            )
            .order_by(KnowledgeDocumentRow.created_at.desc())
        )
        async with self._sf() as session:
            rows = (await session.execute(stmt)).scalars().all()
        return [_to_document(row) for row in rows]

    async def set_document_status(
        self,
        *,
        tenant_id: UUID,
        document_id: UUID,
        status: DocumentStatus,
        error: str | None = None,
        chunk_count: int | None = None,
    ) -> bool:
        values: dict[str, object] = {
            "status": status.value,
            "error": error,
            "updated_at": datetime.now(UTC),
        }
        if chunk_count is not None:
            values["chunk_count"] = chunk_count
        # Reaching a terminal state releases the durability lease so the
        # document is never re-claimed.
        if status.value in _TERMINAL_STATUSES:
            values["claimed_at"] = None
            values["lease_until"] = None
        async with self._sf() as session:
            result = await session.execute(
                update(KnowledgeDocumentRow)
                .where(
                    KnowledgeDocumentRow.tenant_id == tenant_id,
                    KnowledgeDocumentRow.id == document_id,
                )
                .values(**values)
            )
            await session.commit()
        return int(getattr(result, "rowcount", 0) or 0) > 0

    async def delete_document(self, *, tenant_id: UUID, document_id: UUID) -> bool:
        async with self._sf() as session:
            exists = (
                await session.execute(
                    select(KnowledgeDocumentRow.id).where(
                        KnowledgeDocumentRow.tenant_id == tenant_id,
                        KnowledgeDocumentRow.id == document_id,
                    )
                )
            ).scalar_one_or_none()
            if exists is None:
                return False
            await session.execute(
                delete(KnowledgeChunkRow).where(
                    KnowledgeChunkRow.tenant_id == tenant_id,
                    KnowledgeChunkRow.document_id == document_id,
                )
            )
            await session.execute(
                delete(KnowledgeDocumentRow).where(
                    KnowledgeDocumentRow.tenant_id == tenant_id,
                    KnowledgeDocumentRow.id == document_id,
                )
            )
            await session.commit()
        return True

    # -- chunks -------------------------------------------------------------

    async def replace_chunks(
        self, *, tenant_id: UUID, document_id: UUID, chunks: Sequence[KnowledgeChunk]
    ) -> None:
        async with self._sf() as session:
            await session.execute(
                delete(KnowledgeChunkRow).where(
                    KnowledgeChunkRow.tenant_id == tenant_id,
                    KnowledgeChunkRow.document_id == document_id,
                )
            )
            session.add_all(
                [
                    KnowledgeChunkRow(
                        id=chunk.id,
                        tenant_id=chunk.tenant_id,
                        kb_id=chunk.kb_id,
                        document_id=chunk.document_id,
                        chunk_index=chunk.chunk_index,
                        content=chunk.content,
                        embedding=list(chunk.embedding),
                        # Keyword-search vector — segmented app-side so it
                        # is correct for CJK (see text_search).
                        content_tsv=func.to_tsvector(
                            _TS_CONFIG, tokenize_for_search(chunk.content)
                        ),
                    )
                    for chunk in chunks
                ]
            )
            await session.commit()

    async def list_chunks(
        self, *, tenant_id: UUID, document_id: UUID, offset: int = 0, limit: int = 50
    ) -> tuple[list[KnowledgeChunk], int]:
        # Explicit column projection — never SELECT the large embedding vector
        # for a preview. The returned chunks carry ``embedding == ()``.
        page_stmt = (
            select(
                KnowledgeChunkRow.id,
                KnowledgeChunkRow.tenant_id,
                KnowledgeChunkRow.kb_id,
                KnowledgeChunkRow.document_id,
                KnowledgeChunkRow.chunk_index,
                KnowledgeChunkRow.content,
                KnowledgeChunkRow.created_at,
            )
            .where(
                KnowledgeChunkRow.tenant_id == tenant_id,
                KnowledgeChunkRow.document_id == document_id,
            )
            .order_by(KnowledgeChunkRow.chunk_index.asc())
            .offset(offset)
            .limit(limit)
        )
        count_stmt = (
            select(func.count())
            .select_from(KnowledgeChunkRow)
            .where(
                KnowledgeChunkRow.tenant_id == tenant_id,
                KnowledgeChunkRow.document_id == document_id,
            )
        )
        async with self._sf() as session:
            rows = (await session.execute(page_stmt)).all()
            total = (await session.execute(count_stmt)).scalar_one()
        chunks = [
            KnowledgeChunk(
                id=row.id,
                tenant_id=row.tenant_id,
                kb_id=row.kb_id,
                document_id=row.document_id,
                chunk_index=row.chunk_index,
                content=row.content,
                embedding=(),
                created_at=row.created_at,
            )
            for row in rows
        ]
        return chunks, int(total)

    async def search(
        self,
        *,
        tenant_id: UUID,
        kb_ids: Sequence[UUID],
        query_embedding: Sequence[float],
        limit: int = 5,
    ) -> list[KnowledgeChunk]:
        scored = await self.search_scored(
            tenant_id=tenant_id, kb_ids=kb_ids, query_embedding=query_embedding, limit=limit
        )
        return [hit.chunk for hit in scored]

    async def search_scored(
        self,
        *,
        tenant_id: UUID,
        kb_ids: Sequence[UUID],
        query_embedding: Sequence[float],
        limit: int = 5,
    ) -> list[ScoredChunk]:
        if not kb_ids:
            return []
        vec = list(query_embedding)
        # Cosine *distance* (``<=>``) drives the (HNSW-backed) sort; similarity
        # = ``1 - distance`` is surfaced as the score.
        distance = KnowledgeChunkRow.embedding.cosine_distance(vec)
        stmt = (
            select(KnowledgeChunkRow, distance.label("distance"))
            .where(
                KnowledgeChunkRow.tenant_id == tenant_id,
                KnowledgeChunkRow.kb_id.in_(list(kb_ids)),
            )
            .order_by(distance)
            .limit(limit)
        )
        async with self._sf() as session:
            rows = (await session.execute(stmt)).all()
        return [
            ScoredChunk(chunk=_to_chunk(row), score=1.0 - float(dist), source="vector")
            for row, dist in rows
        ]

    async def keyword_search(
        self,
        *,
        tenant_id: UUID,
        kb_ids: Sequence[UUID],
        query: str,
        limit: int = 5,
    ) -> list[KnowledgeChunk]:
        scored = await self.keyword_search_scored(
            tenant_id=tenant_id, kb_ids=kb_ids, query=query, limit=limit
        )
        return [hit.chunk for hit in scored]

    async def keyword_search_scored(
        self,
        *,
        tenant_id: UUID,
        kb_ids: Sequence[UUID],
        query: str,
        limit: int = 5,
    ) -> list[ScoredChunk]:
        if not kb_ids:
            return []
        tokenized = tokenize_for_search(query)
        if not tokenized:
            return []
        ts_query = func.plainto_tsquery(_TS_CONFIG, tokenized)
        rank = func.ts_rank(KnowledgeChunkRow.content_tsv, ts_query)
        stmt = (
            select(KnowledgeChunkRow, rank.label("rank"))
            .where(
                KnowledgeChunkRow.tenant_id == tenant_id,
                KnowledgeChunkRow.kb_id.in_(list(kb_ids)),
                KnowledgeChunkRow.content_tsv.op("@@")(ts_query),
            )
            .order_by(rank.desc())
            .limit(limit)
        )
        async with self._sf() as session:
            rows = (await session.execute(stmt)).all()
        return [
            ScoredChunk(chunk=_to_chunk(row), score=float(rank_value), source="keyword")
            for row, rank_value in rows
        ]
