"""In-memory ``KnowledgeStore`` for unit tests."""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from expert_work.persistence.knowledge.base import (
    ALL_TENANTS_BASES_LIMIT,
    UNSET,
    ClaimedIngestion,
    DocumentFileFacts,
    DuplicateKnowledgeBaseError,
    KnowledgeStore,
    ParseCacheEntry,
    ParseCacheKind,
    _Unset,
)
from expert_work.persistence.knowledge.text_search import (
    KB_DOC_TITLE_HIT_WEIGHT,
    KB_KEYWORD_PHRASE_BOOST,
    KB_KEYWORD_QUERY_PHRASE_BOOST,
    KbKeywordQuery,
    build_kb_keyword_query,
    is_searchable_token,
    kb_index_text,
    tokenize_for_search,
)
from expert_work.protocol import (
    DEFAULT_CHUNK_MAX_TOKENS,
    DEFAULT_CHUNK_OVERLAP_TOKENS,
    DEFAULT_RETRIEVAL_TOP_K,
    DocumentPage,
    DocumentSourceType,
    DocumentStatus,
    FigureStats,
    IngestErrorCode,
    IngestStage,
    KnowledgeBase,
    KnowledgeChunk,
    KnowledgeDocument,
    PageStats,
    RetrievalMethod,
    ScoredChunk,
    chunk_retrieval_text,
)


def _searchable_tokens(text: str) -> list[str]:
    """Tokens as Postgres positions them: punctuation-only tokens dropped
    (assumes a non-C database ctype — see ``is_searchable_token``)."""
    return [token for token in text.split() if is_searchable_token(token)]


def _contains_run(tokens: Sequence[str], run: Sequence[str]) -> bool:
    width = len(run)
    return any(tokens[i : i + width] == run for i in range(len(tokens) - width + 1))


#: ``ts_rank``'s weight for unlabelled positions (label D): ``content_tsv`` is
#: built by ``to_tsvector`` without ``setweight``.
_TS_RANK_WEIGHT = 0.1
#: π²/6 — ``ts_rank`` divides each lexeme's Σ 1/j² over its occurrences by it.
_TS_RANK_NORMALISER = math.pi**2 / 6


def _ts_rank_or(tokens: Sequence[str], lexemes: Sequence[str]) -> float:
    """Approximates Postgres ``ts_rank`` of an OR query over unlabelled
    positions: a query lexeme occurring n times adds
    weight * Σ_{j≤n} 1/j² / (π²/6), averaged over the query's distinct lexemes
    (phrase tokens count as lexemes too). Not exact in general: Postgres ranks
    a query that is one bare phrase by proximity, and its parser may split a
    token into different lexemes than jieba produced."""
    counts = Counter(tokens)
    total = sum(sum(1 / j**2 for j in range(1, counts[lexeme] + 1)) for lexeme in lexemes)
    return _TS_RANK_WEIGHT * total / _TS_RANK_NORMALISER / len(lexemes)


def _bm25_idf(in_scope: int, with_unit: int) -> float:
    """Lucene's BM25 idf, ln(1 + (N - df + 0.5) / (df + 0.5)) — as in SQL;
    above zero for every 0 ≤ df ≤ N."""
    return math.log(1 + (in_scope - with_unit + 0.5) / (with_unit + 0.5))


def _index_tokens(text: str) -> list[str]:
    """``text`` as the SQL store indexes it (:func:`kb_index_text`, segmented)."""
    return _searchable_tokens(tokenize_for_search(kb_index_text(text)))


def _unit_hit(title: Sequence[str], body: Sequence[str], runs: Sequence[Sequence[str]]) -> float:
    """1 when the body holds the unit, :data:`KB_DOC_TITLE_HIT_WEIGHT` when
    only title + body do (title positions come first, as in the SQL
    ``tsvector``), else 0."""
    if any(_contains_run(body, run) for run in runs):
        return 1.0
    whole = [*title, *body]
    return KB_DOC_TITLE_HIT_WEIGHT if any(_contains_run(whole, run) for run in runs) else 0.0


def _keyword_scores(query: KbKeywordQuery, chunks: Sequence[KnowledgeChunk]) -> list[float | None]:
    """The SQL store's match predicate and score over the chunks in scope:
    ``None`` for a chunk holding none of the units (terms, identifier phrases)
    in its title or body; otherwise :data:`KB_KEYWORD_PHRASE_BOOST` when an
    identifier phrase hit, plus :data:`KB_KEYWORD_QUERY_PHRASE_BOOST` when the
    whole question appears verbatim, plus coverage (idf-weighted share of the
    units hit, a title-only hit weighing :data:`KB_DOC_TITLE_HIT_WEIGHT`),
    plus an approximate ``ts_rank`` tie-break over the body. Same predicate
    and ordering intent as SQL; exact scores — and so the order of near-ties
    — can differ."""
    # Each unit as its token runs: the NFKC form, plus the raw form if any.
    units = [
        [_searchable_tokens(unit)]
        + ([_searchable_tokens(raw)] if (raw := query.unit_raw_form(unit)) else [])
        for unit in (*query.terms, *query.phrases)
    ]
    lexemes = list(dict.fromkeys(token for runs in units for run in runs for token in run))
    questions = [_searchable_tokens(phrase) for phrase in query.question_phrases()]
    analysed = [
        (
            _index_tokens(chunk.doc_title) if chunk.doc_title else [],
            _index_tokens(chunk_retrieval_text(chunk.heading_path, chunk.content)),
        )
        for chunk in chunks
    ]
    hits = [[_unit_hit(title, body, runs) for runs in units] for title, body in analysed]
    idf = [
        _bm25_idf(len(chunks), sum(1 for row in hits if row[index] > 0))
        for index in range(len(units))
    ]
    scores: list[float | None] = []
    for (title, body), row in zip(analysed, hits, strict=True):
        if not any(row):
            scores.append(None)
            continue
        coverage = sum(weight * hit for weight, hit in zip(idf, row, strict=True)) / sum(idf)
        identifier = KB_KEYWORD_PHRASE_BOOST if any(row[len(query.terms) :]) else 0.0
        whole = [*title, *body]
        verbatim = (
            KB_KEYWORD_QUERY_PHRASE_BOOST
            if any(_contains_run(whole, question) for question in questions)
            else 0.0
        )
        scores.append(identifier + verbatim + coverage + _ts_rank_or(body, lexemes))
    return scores


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
        self._sha256: dict[UUID, str | None] = {}
        self._pending: dict[UUID, tuple[bytes, str | None]] = {}
        self._lease: dict[UUID, datetime | None] = {}
        #: Per-tenant ``doc_seq`` counter — the SQL ``knowledge_doc_seq`` table.
        self._doc_seq: dict[UUID, int] = {}
        #: B-209 — page records per document (``page_records``, deferred in SQL)
        #: and the per-document recognition cache (``knowledge_parse_cache``).
        self._pages: dict[UUID, tuple[DocumentPage, ...]] = {}
        #: (tenant, document, kind, key) → entry.
        self._parse_cache: dict[tuple[UUID, UUID, str, str], ParseCacheEntry] = {}
        #: B-209 PR-2 — pixel hashes of each document's figures (``figure_hashes``).
        self._figure_hashes: dict[UUID, tuple[str, ...]] = {}

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
        rerank_score_threshold: float | None = None,
        embedding_provider: str | None = None,
        embedding_model: str | None = None,
        rerank_threshold_model: str | None = None,
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
            rerank_score_threshold=rerank_score_threshold,
            rerank_threshold_model=rerank_threshold_model,
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
        description: str | _Unset | None = UNSET,
        chunk_max_tokens: int | None = None,
        chunk_overlap_tokens: int | None = None,
        retrieval_top_k: int | None = None,
        retrieval_score_threshold: float | _Unset | None = UNSET,
        retrieval_method: RetrievalMethod | None = None,
        rerank_enabled: bool | None = None,
        rerank_score_threshold: float | _Unset | None = UNSET,
        rerank_threshold_model: str | _Unset | None = UNSET,
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
        if not isinstance(rerank_score_threshold, _Unset):
            update["rerank_score_threshold"] = rerank_score_threshold
        if not isinstance(rerank_threshold_model, _Unset):
            update["rerank_threshold_model"] = rerank_threshold_model
        updated = base.model_copy(update=update)
        self._bases[self._bases.index(base)] = updated
        return updated

    async def get_base(self, *, tenant_id: UUID, name: str) -> KnowledgeBase | None:
        return next((b for b in self._bases if b.tenant_id == tenant_id and b.name == name), None)

    async def list_bases(self, *, tenant_id: UUID) -> list[KnowledgeBase]:
        bases = await self.list_bases_page(tenant_id=tenant_id, limit=len(self._bases), offset=0)
        return bases[0]

    async def list_bases_all_tenants(
        self, *, limit: int = ALL_TENANTS_BASES_LIMIT
    ) -> list[KnowledgeBase]:
        # W4 — no tenant filter; created_at DESC with id tiebreak (two stable
        # sorts) matching the SQL implementation byte-for-byte.
        bases = sorted(self._bases, key=lambda b: b.id.int)
        return sorted(bases, key=_created_key, reverse=True)[:limit]

    async def list_bases_page(
        self, *, tenant_id: UUID, limit: int, offset: int
    ) -> tuple[list[KnowledgeBase], int]:
        # created_at DESC, id DESC (two stable sorts) — the SQL page order.
        bases = sorted(
            (b for b in self._bases if b.tenant_id == tenant_id),
            key=lambda b: b.id.int,
            reverse=True,
        )
        bases = sorted(bases, key=_created_key, reverse=True)
        return bases[offset : offset + limit], len(bases)

    async def list_bases_all_tenants_page(
        self, *, limit: int, offset: int
    ) -> tuple[list[KnowledgeBase], int]:
        bases = await self.list_bases_all_tenants(limit=len(self._bases))
        return bases[offset : offset + limit], len(bases)

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

    async def base_stats_many_all_tenants(
        self, *, kb_ids: Sequence[UUID]
    ) -> dict[UUID, tuple[int, int]]:
        # W4 — no tenant filter; kb_id is globally unique. Bounded to the
        # kb_ids of the (capped) aggregate base page.
        wanted = set(kb_ids)
        stats: dict[UUID, tuple[int, int]] = {}
        for doc in self._documents:
            if doc.kb_id not in wanted:
                continue
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
        gone = {d.id for d in self._documents if d.kb_id == kb_id}
        self._documents = [d for d in self._documents if d.kb_id != kb_id]
        self._chunks = [c for c in self._chunks if c.kb_id != kb_id]
        self._drop_document_state(gone)
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
        force: bool = False,
    ) -> KnowledgeDocument:
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
            if (
                not force
                and content_sha256 is not None
                and existing.status is DocumentStatus.READY
                and self._sha256.get(existing.id) == content_sha256
            ):
                return existing  # unchanged re-upload: nothing to do
            # New bytes go to the pending slot; the serving version (content +
            # chunks) stays until the new ingest's fenced READY promotes it.
            update: dict[str, object] = {
                "status": DocumentStatus.PENDING,
                "error": None,
                "error_code": None,
                "stage": IngestStage.QUEUED,
                "attempts": 0,
                "ingest_generation": existing.ingest_generation + 1,
                "updated_at": now,
            }
            if content is not None:
                if self._content.get(existing.id) is None and existing.chunk_count == 0:
                    self._content[existing.id] = content  # nothing serving yet
                    self._sha256[existing.id] = content_sha256
                    update["size_bytes"] = len(content)
                else:
                    self._pending[existing.id] = (content, content_sha256)
            update["has_pending"] = existing.id in self._pending
            update["has_content"] = self._content.get(existing.id) is not None
            if existing.doc_seq is None:  # a row from before doc_seq existed
                update["doc_seq"] = self._bump_doc_seq(tenant_id)
            reset = existing.model_copy(update=update)
            self._documents[self._documents.index(existing)] = reset
            self._lease[existing.id] = None
            return reset
        facts = DocumentFileFacts.of(filename, content)
        doc_seq = self._bump_doc_seq(tenant_id)
        document = KnowledgeDocument(
            id=uuid4(),
            tenant_id=tenant_id,
            kb_id=kb_id,
            filename=filename,
            status=DocumentStatus.PENDING,
            has_content=content is not None,
            doc_seq=doc_seq,
            title=facts.title,
            mime=facts.mime,
            size_bytes=facts.size_bytes,
            source_type=DocumentSourceType.FILE,
            stage=IngestStage.QUEUED,
            created_at=now,
            updated_at=now,
        )
        self._documents.append(document)
        self._content[document.id] = content
        self._sha256[document.id] = content_sha256
        self._lease[document.id] = None
        return document

    def _bump_doc_seq(self, tenant_id: UUID) -> int:
        self._doc_seq[tenant_id] = self._doc_seq.get(tenant_id, 0) + 1
        return self._doc_seq[tenant_id]

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
        pending = self._pending.get(doc.id)
        content, sha = (
            pending
            if pending is not None
            else (self._content.get(doc.id), self._sha256.get(doc.id))
        )
        return ClaimedIngestion(
            tenant_id=doc.tenant_id,
            document_id=doc.id,
            kb_id=doc.kb_id,
            filename=doc.filename,
            content=content,
            content_sha256=sha,
            chunk_max_tokens=chunk_max,
            chunk_overlap_tokens=chunk_overlap,
            attempts=claimed.attempts,
            generation=claimed.ingest_generation,
        )

    async def mark_document_failed_terminal(
        self,
        *,
        tenant_id: UUID,
        document_id: UUID,
        error: str,
        error_code: IngestErrorCode | None = None,
        generation: int | None = None,
    ) -> bool:
        failed = await self.set_document_status(
            tenant_id=tenant_id,
            document_id=document_id,
            status=DocumentStatus.FAILED,
            error=error,
            generation=generation,
        )
        if failed:
            self._patch(tenant_id, document_id, {"error_code": error_code})
        return failed

    async def set_document_stage(
        self,
        *,
        tenant_id: UUID,
        document_id: UUID,
        stage: IngestStage,
        generation: int | None = None,
    ) -> bool:
        document = await self.get_document(tenant_id=tenant_id, document_id=document_id)
        if document is None:
            return False
        if generation is not None and not _fence_matches(document, generation):
            return False
        self._patch(tenant_id, document_id, {"stage": stage, "updated_at": datetime.now(UTC)})
        return True

    def _patch(self, tenant_id: UUID, document_id: UUID, update: dict[str, object]) -> None:
        document = next(
            d for d in self._documents if d.tenant_id == tenant_id and d.id == document_id
        )
        self._documents[self._documents.index(document)] = document.model_copy(update=update)

    async def discard_pending(
        self, *, tenant_id: UUID, document_id: UUID
    ) -> KnowledgeDocument | None:
        document = await self.get_document(tenant_id=tenant_id, document_id=document_id)
        if document is None:
            return None
        if document_id not in self._pending:
            return document
        del self._pending[document_id]
        serving = document.chunk_count > 0
        restored = document.model_copy(
            update={
                "status": DocumentStatus.READY if serving else DocumentStatus.FAILED,
                "error": None if serving else _DISCARDED_ERROR,
                "error_code": None,
                "stage": IngestStage.READY if serving else document.stage,
                "has_pending": False,
                "ingest_generation": document.ingest_generation + 1,
                "attempts": 0,
                "updated_at": datetime.now(UTC),
            }
        )
        self._documents[self._documents.index(document)] = restored
        self._lease[document_id] = None
        return restored

    async def sweep_terminal(self, *, now: datetime, max_attempts: int) -> int:
        swept = 0
        for doc in list(self._documents):
            if doc.status is not DocumentStatus.PROCESSING or doc.attempts < max_attempts:
                continue
            lease = self._lease.get(doc.id)
            if lease is not None and lease >= now:
                continue
            await self.mark_document_failed_terminal(
                tenant_id=doc.tenant_id,
                document_id=doc.id,
                error=_EXHAUSTED_ERROR,
                error_code=IngestErrorCode.ATTEMPTS_EXHAUSTED,
            )
            swept += 1
        return swept

    async def renew_lease(
        self,
        *,
        tenant_id: UUID,
        document_id: UUID,
        generation: int,
        now: datetime,
        lease_seconds: int,
    ) -> bool:
        document = await self.get_document(tenant_id=tenant_id, document_id=document_id)
        if document is None or not _fence_matches(document, generation):
            return False
        self._lease[document_id] = now + timedelta(seconds=lease_seconds)
        return True

    async def list_documents(self, *, tenant_id: UUID, kb_id: UUID) -> list[KnowledgeDocument]:
        docs = [d for d in self._documents if d.tenant_id == tenant_id and d.kb_id == kb_id]
        return sorted(docs, key=_created_key, reverse=True)

    async def list_documents_page(
        self, *, tenant_id: UUID, kb_id: UUID, limit: int, offset: int
    ) -> tuple[list[KnowledgeDocument], int]:
        docs = await self.list_documents(tenant_id=tenant_id, kb_id=kb_id)
        return docs[offset : offset + limit], len(docs)

    async def set_document_status(
        self,
        *,
        tenant_id: UUID,
        document_id: UUID,
        status: DocumentStatus,
        error: str | None = None,
        chunk_count: int | None = None,
        generation: int | None = None,
    ) -> bool:
        document = await self.get_document(tenant_id=tenant_id, document_id=document_id)
        if document is None:
            return False
        if generation is not None and not _fence_matches(document, generation):
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
        if status is DocumentStatus.READY:
            update["stage"] = IngestStage.READY
            update["error_code"] = None
        if status is DocumentStatus.READY and generation is not None:
            # The new version is now serving: promote pending → content.
            pending = self._pending.pop(document_id, None)
            if pending is not None:
                self._content[document_id], self._sha256[document_id] = pending
            serving = self._content.get(document_id)
            update["size_bytes"] = len(serving) if serving is not None else None
            update["has_pending"] = False
            update["has_content"] = serving is not None
        self._documents[self._documents.index(document)] = document.model_copy(update=update)
        return True

    async def delete_document(self, *, tenant_id: UUID, document_id: UUID) -> bool:
        document = await self.get_document(tenant_id=tenant_id, document_id=document_id)
        if document is None:
            return False
        self._documents.remove(document)
        self._chunks = [c for c in self._chunks if c.document_id != document_id]
        self._drop_document_state({document_id})
        return True

    def _drop_document_state(self, document_ids: set[UUID]) -> None:
        """Page records and recognition cache of deleted documents (the SQL
        store deletes them with the row / by FK cascade)."""
        for document_id in document_ids:
            self._pages.pop(document_id, None)
            self._figure_hashes.pop(document_id, None)
        self._parse_cache = {
            key: entry for key, entry in self._parse_cache.items() if key[1] not in document_ids
        }

    async def set_document_pages(
        self,
        *,
        tenant_id: UUID,
        document_id: UUID,
        pages: Sequence[DocumentPage],
        generation: int | None = None,
    ) -> bool:
        document = await self.get_document(tenant_id=tenant_id, document_id=document_id)
        if document is None:
            return False
        if generation is not None and not _fence_matches(document, generation):
            return False
        self._pages[document_id] = tuple(pages)
        self._patch(
            tenant_id,
            document_id,
            {
                "page_stats": PageStats.of(pages),
                "page_count": len(pages),
                "updated_at": datetime.now(UTC),
            },
        )
        return True

    async def list_document_pages(
        self, *, tenant_id: UUID, document_id: UUID
    ) -> list[DocumentPage] | None:
        if await self.get_document(tenant_id=tenant_id, document_id=document_id) is None:
            return None
        return list(self._pages.get(document_id, ()))

    async def set_document_figures(
        self,
        *,
        tenant_id: UUID,
        document_id: UUID,
        stats: FigureStats | _Unset | None,
        pixel_hashes: Sequence[str],
        generation: int | None = None,
    ) -> bool:
        document = await self.get_document(tenant_id=tenant_id, document_id=document_id)
        if document is None:
            return False
        if generation is not None and not _fence_matches(document, generation):
            return False
        self._figure_hashes[document_id] = tuple(dict.fromkeys(pixel_hashes))
        update: dict[str, object] = {"updated_at": datetime.now(UTC)}
        if not isinstance(stats, _Unset):
            update["figure_stats"] = stats
        self._patch(tenant_id, document_id, update)
        return True

    async def existing_document_ids(self, *, document_ids: Sequence[UUID]) -> set[UUID]:
        wanted = set(document_ids)
        return {document.id for document in self._documents if document.id in wanted}

    async def count_figure_documents(
        self, *, tenant_id: UUID, pixel_hashes: Sequence[str], exclude_document_id: UUID
    ) -> dict[str, int]:
        wanted = set(pixel_hashes)
        counts: Counter[str] = Counter()
        for document in self._documents:
            if document.tenant_id != tenant_id or document.id == exclude_document_id:
                continue
            counts.update(wanted.intersection(self._figure_hashes.get(document.id, ())))
        return dict(counts)

    async def get_parse_cache(
        self,
        *,
        tenant_id: UUID,
        document_id: UUID,
        kind: ParseCacheKind,
        keys: Sequence[str],
    ) -> dict[str, ParseCacheEntry]:
        found = ((key, self._parse_cache.get((tenant_id, document_id, kind, key))) for key in keys)
        return {key: entry for key, entry in found if entry is not None}

    async def put_parse_cache(
        self,
        *,
        tenant_id: UUID,
        document_id: UUID,
        kind: ParseCacheKind,
        key: str,
        model: str,
        result: dict[str, Any],
    ) -> None:
        if await self.get_document(tenant_id=tenant_id, document_id=document_id) is None:
            # The SQL store's foreign key refuses a deleted document's reading.
            msg = f"no document {document_id}"
            raise KeyError(msg)
        self._parse_cache[(tenant_id, document_id, kind, key)] = ParseCacheEntry(
            model=model, result=dict(result)
        )

    # -- chunks -------------------------------------------------------------

    async def replace_chunks(
        self,
        *,
        tenant_id: UUID,
        document_id: UUID,
        chunks: Sequence[KnowledgeChunk],
        generation: int | None = None,
        expect_status: DocumentStatus = DocumentStatus.PROCESSING,
    ) -> bool:
        document = await self.get_document(tenant_id=tenant_id, document_id=document_id)
        if generation is not None:
            # Fenced: a missing document is just "not ours any more".
            if document is None or not _fence_matches(document, generation, expect_status):
                return False
        elif document is None:
            # Mirror the SQL side's FK (0136): writing chunks back to a deleted
            # document must fail loudly instead of recreating orphan vectors.
            raise KeyError(document_id)
        indices = [c.chunk_index for c in chunks]
        if len(indices) != len(set(indices)):
            # Mirror the SQL unique (document_id, chunk_index) (0165).
            msg = f"duplicate chunk_index in replace_chunks for document {document_id}"
            raise ValueError(msg)
        self._chunks = [
            c
            for c in self._chunks
            if not (c.tenant_id == tenant_id and c.document_id == document_id)
        ]
        self._chunks.extend(chunks)
        return True

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

    async def chunk_embeddings_by_hash(
        self,
        *,
        tenant_id: UUID,
        document_id: UUID,
        content_hashes: Sequence[str],
        embedding_model: str,
    ) -> dict[str, Sequence[float]]:
        wanted = set(content_hashes)
        return {
            c.content_hash: c.embedding
            for c in self._chunks
            if c.tenant_id == tenant_id
            and c.document_id == document_id
            and c.embedding_model == embedding_model
            and c.content_hash is not None
            and c.content_hash in wanted
        }

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
        kb_query = build_kb_keyword_query(query)
        if kb_query is None:
            return []
        kb_set = set(kb_ids)
        in_scope = [c for c in self._chunks if c.tenant_id == tenant_id and c.kb_id in kb_set]
        scores = _keyword_scores(kb_query, in_scope)
        scored = sorted(
            (
                (score, chunk)
                for score, chunk in zip(scores, in_scope, strict=True)
                if score is not None
            ),
            key=lambda pair: pair[0],
            reverse=True,
        )
        return [
            ScoredChunk(chunk=chunk, score=score, source="keyword")
            for score, chunk in scored[:limit]
        ]


def _created_key(record: KnowledgeBase | KnowledgeDocument) -> datetime:
    return record.created_at or datetime.min.replace(tzinfo=UTC)


#: ``error`` written by :meth:`InMemoryKnowledgeStore.sweep_terminal` — same
#: text as the SQL store so operators see one message.
_EXHAUSTED_ERROR = "ingest attempts exhausted"
#: ``error`` after discarding a pending upload when nothing was serving.
_DISCARDED_ERROR = "pending upload discarded; nothing is serving"


def _fence_matches(
    document: KnowledgeDocument,
    generation: int,
    status: DocumentStatus = DocumentStatus.PROCESSING,
) -> bool:
    """The generation fence: the write-back belongs to the claim that still
    owns this exact generation of the document in the expected status."""
    return document.status is status and document.ingest_generation == generation
