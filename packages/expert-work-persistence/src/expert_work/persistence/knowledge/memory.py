"""In-memory ``KnowledgeStore`` for unit tests."""

from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from expert_work.persistence.knowledge.base import (
    UNSET,
    ClaimedIngestion,
    DuplicateKnowledgeBaseError,
    KnowledgeStore,
    _Unset,
)
from expert_work.persistence.knowledge.text_search import tokenize_for_search
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


def _cosine_distance(a: Sequence[float], b: Sequence[float]) -> float:
    """Cosine distance (0 = identical) — mirrors pgvector's ``<=>``."""
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 1.0
    return 1.0 - dot / (norm_a * norm_b)


class InMemoryKnowledgeStore(KnowledgeStore):
    """In-memory knowledge repository — semantics mirror :class:`SqlKnowledgeStore`."""

    def __init__(self) -> None:
        self._bases: list[KnowledgeBase] = []
        self._documents: list[KnowledgeDocument] = []
        self._chunks: list[KnowledgeChunk] = []
        # Durability side-state (the public DTO omits bytes + lease): the SQL
        # store keeps these on the row. ``_lease`` holds the lease expiry; CAS
        # races are only meaningfully testable against real Postgres.
        self._content: dict[UUID, bytes | None] = {}
        self._lease: dict[UUID, datetime | None] = {}

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
        if any(b.tenant_id == tenant_id and b.name == name for b in self._bases):
            raise DuplicateKnowledgeBaseError(tenant_id=tenant_id, name=name)
        now = datetime.now(UTC)
        base = KnowledgeBase(
            id=uuid4(),
            tenant_id=tenant_id,
            name=name,
            description=description,
            created_by=created_by,
            chunk_max_tokens=chunk_max_tokens,
            chunk_overlap_tokens=chunk_overlap_tokens,
            retrieval_top_k=retrieval_top_k,
            retrieval_score_threshold=retrieval_score_threshold,
            retrieval_method=retrieval_method,
            rerank_enabled=rerank_enabled,
            embedding_provider=embedding_provider,
            embedding_model=embedding_model,
            created_at=now,
            updated_at=now,
        )
        self._bases.append(base)
        return base

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
        base = next((b for b in self._bases if b.tenant_id == tenant_id and b.id == kb_id), None)
        if base is None:
            return None
        update: dict[str, object] = {"updated_at": datetime.now(UTC)}
        if not isinstance(description, _Unset):
            update["description"] = description
        if chunk_max_tokens is not None:
            update["chunk_max_tokens"] = chunk_max_tokens
        if chunk_overlap_tokens is not None:
            update["chunk_overlap_tokens"] = chunk_overlap_tokens
        if retrieval_top_k is not None:
            update["retrieval_top_k"] = retrieval_top_k
        if not isinstance(retrieval_score_threshold, _Unset):
            update["retrieval_score_threshold"] = retrieval_score_threshold
        if retrieval_method is not None:
            update["retrieval_method"] = retrieval_method
        if rerank_enabled is not None:
            update["rerank_enabled"] = rerank_enabled
        updated = base.model_copy(update=update)
        self._bases[self._bases.index(base)] = updated
        return updated

    async def get_base(self, *, tenant_id: UUID, name: str) -> KnowledgeBase | None:
        return next((b for b in self._bases if b.tenant_id == tenant_id and b.name == name), None)

    async def list_bases(self, *, tenant_id: UUID) -> list[KnowledgeBase]:
        bases = [b for b in self._bases if b.tenant_id == tenant_id]
        return sorted(bases, key=_created_key, reverse=True)

    async def list_bases_all_tenants(self) -> list[KnowledgeBase]:
        # W4 — no tenant filter; created_at DESC with id tiebreak (two stable
        # sorts) matching the SQL implementation byte-for-byte.
        bases = sorted(self._bases, key=lambda b: b.id.int)
        return sorted(bases, key=_created_key, reverse=True)

    async def base_stats(self, *, tenant_id: UUID, kb_id: UUID) -> tuple[int, int]:
        docs = [d for d in self._documents if d.tenant_id == tenant_id and d.kb_id == kb_id]
        return len(docs), sum(d.chunk_count for d in docs)

    async def base_stats_many(self, *, tenant_id: UUID) -> dict[UUID, tuple[int, int]]:
        stats: dict[UUID, tuple[int, int]] = {}
        for doc in self._documents:
            if doc.tenant_id != tenant_id:
                continue
            count, chunks = stats.get(doc.kb_id, (0, 0))
            stats[doc.kb_id] = (count + 1, chunks + doc.chunk_count)
        return stats

    async def base_stats_many_all_tenants(self) -> dict[UUID, tuple[int, int]]:
        # W4 — no tenant filter; kb_id is globally unique.
        stats: dict[UUID, tuple[int, int]] = {}
        for doc in self._documents:
            count, chunks = stats.get(doc.kb_id, (0, 0))
            stats[doc.kb_id] = (count + 1, chunks + doc.chunk_count)
        return stats

    async def stamp_embedding_model(
        self, *, tenant_id: UUID, kb_id: UUID, embedding_provider: str, embedding_model: str
    ) -> None:
        await self._patch_base(
            tenant_id,
            kb_id,
            {
                "embedding_provider": embedding_provider,
                "embedding_model": embedding_model,
            },
        )

    async def request_reindex(self, *, tenant_id: UUID, kb_id: UUID) -> bool:
        return await self._patch_base(tenant_id, kb_id, {"reindex_requested_at": datetime.now(UTC)})

    async def clear_reindex(self, *, tenant_id: UUID, kb_id: UUID) -> None:
        await self._patch_base(tenant_id, kb_id, {"reindex_requested_at": None})

    async def _patch_base(self, tenant_id: UUID, kb_id: UUID, update: dict[str, object]) -> bool:
        base = next((b for b in self._bases if b.tenant_id == tenant_id and b.id == kb_id), None)
        if base is None:
            return False
        merged = {**update, "updated_at": datetime.now(UTC)}
        self._bases[self._bases.index(base)] = base.model_copy(update=merged)
        return True

    async def delete_base(self, *, tenant_id: UUID, kb_id: UUID) -> bool:
        base = next((b for b in self._bases if b.tenant_id == tenant_id and b.id == kb_id), None)
        if base is None:
            return False
        self._bases.remove(base)
        self._documents = [d for d in self._documents if d.kb_id != kb_id]
        self._chunks = [c for c in self._chunks if c.kb_id != kb_id]
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
        del content_sha256  # tracked alongside bytes in SQL; unused in-memory
        now = datetime.now(UTC)
        existing = next(
            (
                d
                for d in self._documents
                if d.tenant_id == tenant_id and d.kb_id == kb_id and d.filename == filename
            ),
            None,
        )
        if existing is not None:
            reset = existing.model_copy(
                update={
                    "status": DocumentStatus.PENDING,
                    "error": None,
                    "chunk_count": 0,
                    "attempts": 0,
                    "updated_at": now,
                }
            )
            self._documents[self._documents.index(existing)] = reset
            self._chunks = [c for c in self._chunks if c.document_id != existing.id]
            self._lease[existing.id] = None
            if content is not None:
                self._content[existing.id] = content
            return reset
        document = KnowledgeDocument(
            id=uuid4(),
            tenant_id=tenant_id,
            kb_id=kb_id,
            filename=filename,
            status=DocumentStatus.PENDING,
            created_at=now,
            updated_at=now,
        )
        self._documents.append(document)
        self._content[document.id] = content
        self._lease[document.id] = None
        return document

    async def get_document(self, *, tenant_id: UUID, document_id: UUID) -> KnowledgeDocument | None:
        return next(
            (d for d in self._documents if d.tenant_id == tenant_id and d.id == document_id),
            None,
        )

    async def get_documents(
        self, *, tenant_id: UUID, document_ids: Sequence[UUID]
    ) -> dict[UUID, KnowledgeDocument]:
        wanted = set(document_ids)
        return {d.id: d for d in self._documents if d.tenant_id == tenant_id and d.id in wanted}

    async def get_document_content(self, *, tenant_id: UUID, document_id: UUID) -> bytes | None:
        doc = await self.get_document(tenant_id=tenant_id, document_id=document_id)
        return self._content.get(document_id) if doc is not None else None

    async def claim_document(
        self,
        *,
        tenant_id: UUID,
        document_id: UUID,
        now: datetime,
        lease_seconds: int,
        max_attempts: int,
    ) -> ClaimedIngestion | None:
        doc = await self.get_document(tenant_id=tenant_id, document_id=document_id)
        if doc is None or not self._is_claimable(doc, now, max_attempts):
            return None
        return self._claim(doc, now, lease_seconds)

    async def claim_documents_for_ingest(
        self, *, now: datetime, lease_seconds: int, limit: int, max_attempts: int
    ) -> list[ClaimedIngestion]:
        claimable = [d for d in self._documents if self._is_claimable(d, now, max_attempts)]
        claimable.sort(key=_created_key)
        return [self._claim(doc, now, lease_seconds) for doc in claimable[:limit]]

    def _is_claimable(self, doc: KnowledgeDocument, now: datetime, max_attempts: int) -> bool:
        if doc.attempts >= max_attempts:
            return False
        if doc.status is DocumentStatus.PENDING:
            return True
        if doc.status is DocumentStatus.PROCESSING:
            lease = self._lease.get(doc.id)
            return lease is None or lease < now
        return False

    def _claim(self, doc: KnowledgeDocument, now: datetime, lease_seconds: int) -> ClaimedIngestion:
        claimed = doc.model_copy(
            update={
                "status": DocumentStatus.PROCESSING,
                "attempts": doc.attempts + 1,
                "updated_at": now,
            }
        )
        self._documents[self._documents.index(doc)] = claimed
        self._lease[doc.id] = now + timedelta(seconds=lease_seconds)
        base = next((b for b in self._bases if b.id == doc.kb_id), None)
        chunk_max = base.chunk_max_tokens if base else DEFAULT_CHUNK_MAX_TOKENS
        chunk_overlap = base.chunk_overlap_tokens if base else DEFAULT_CHUNK_OVERLAP_TOKENS
        return ClaimedIngestion(
            tenant_id=doc.tenant_id,
            document_id=doc.id,
            kb_id=doc.kb_id,
            filename=doc.filename,
            content=self._content.get(doc.id),
            content_sha256=None,
            chunk_max_tokens=chunk_max,
            chunk_overlap_tokens=chunk_overlap,
            attempts=claimed.attempts,
        )

    async def mark_document_failed_terminal(
        self, *, tenant_id: UUID, document_id: UUID, error: str
    ) -> None:
        await self.set_document_status(
            tenant_id=tenant_id,
            document_id=document_id,
            status=DocumentStatus.FAILED,
            error=error,
        )

    async def list_documents(self, *, tenant_id: UUID, kb_id: UUID) -> list[KnowledgeDocument]:
        docs = [d for d in self._documents if d.tenant_id == tenant_id and d.kb_id == kb_id]
        return sorted(docs, key=_created_key, reverse=True)

    async def set_document_status(
        self,
        *,
        tenant_id: UUID,
        document_id: UUID,
        status: DocumentStatus,
        error: str | None = None,
        chunk_count: int | None = None,
    ) -> bool:
        document = await self.get_document(tenant_id=tenant_id, document_id=document_id)
        if document is None:
            return False
        update: dict[str, object] = {
            "status": status,
            "error": error,
            "updated_at": datetime.now(UTC),
        }
        if chunk_count is not None:
            update["chunk_count"] = chunk_count
        if status in (DocumentStatus.READY, DocumentStatus.FAILED):
            self._lease[document_id] = None  # terminal → release lease
        self._documents[self._documents.index(document)] = document.model_copy(update=update)
        return True

    async def delete_document(self, *, tenant_id: UUID, document_id: UUID) -> bool:
        document = await self.get_document(tenant_id=tenant_id, document_id=document_id)
        if document is None:
            return False
        self._documents.remove(document)
        self._chunks = [c for c in self._chunks if c.document_id != document_id]
        return True

    # -- chunks -------------------------------------------------------------

    async def replace_chunks(
        self, *, tenant_id: UUID, document_id: UUID, chunks: Sequence[KnowledgeChunk]
    ) -> None:
        # Mirror the SQL side's FK (0136): writing chunks back to a deleted
        # document must fail loudly instead of recreating orphan vectors.
        if await self.get_document(tenant_id=tenant_id, document_id=document_id) is None:
            raise KeyError(document_id)
        self._chunks = [
            c
            for c in self._chunks
            if not (c.tenant_id == tenant_id and c.document_id == document_id)
        ]
        self._chunks.extend(chunks)

    async def list_chunks(
        self, *, tenant_id: UUID, document_id: UUID, offset: int = 0, limit: int = 50
    ) -> tuple[list[KnowledgeChunk], int]:
        matching = [
            c for c in self._chunks if c.tenant_id == tenant_id and c.document_id == document_id
        ]
        matching.sort(key=lambda c: c.chunk_index)
        # Mirror the SQL projection: embedding omitted from preview rows.
        page = [c.model_copy(update={"embedding": ()}) for c in matching[offset : offset + limit]]
        return page, len(matching)

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
        kb_set = set(kb_ids)
        candidates = [c for c in self._chunks if c.tenant_id == tenant_id and c.kb_id in kb_set]
        candidates.sort(key=lambda c: _cosine_distance(query_embedding, c.embedding))
        return [
            ScoredChunk(
                chunk=chunk,
                score=1.0 - _cosine_distance(query_embedding, chunk.embedding),
                source="vector",
            )
            for chunk in candidates[:limit]
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
        terms = set(tokenize_for_search(query).split())
        if not terms:
            return []
        kb_set = set(kb_ids)
        scored: list[tuple[int, KnowledgeChunk]] = []
        for chunk in self._chunks:
            if chunk.tenant_id != tenant_id or chunk.kb_id not in kb_set:
                continue
            overlap = len(terms & set(tokenize_for_search(chunk.content).split()))
            if overlap > 0:
                scored.append((overlap, chunk))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [
            ScoredChunk(chunk=chunk, score=float(overlap), source="keyword")
            for overlap, chunk in scored[:limit]
        ]


def _created_key(record: KnowledgeBase | KnowledgeDocument) -> datetime:
    return record.created_at or datetime.min.replace(tzinfo=UTC)
