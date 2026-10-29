"""In-memory ``KnowledgeStore`` for unit tests."""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Collection, Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from expert_work.persistence.knowledge.base import (
    ALL_TENANTS_BASES_LIMIT,
    UNSET,
    CancelOutcome,
    ClaimedIngestion,
    DocumentFileFacts,
    DuplicateKnowledgeBaseError,
    KnowledgeStore,
    ParseCacheEntry,
    ParseCacheKind,
    _Unset,
)
from expert_work.persistence.knowledge.estimates import (
    WAITING_STATUSES,
    discarded_estimate,
    reindex_fence_statuses,
)
from expert_work.persistence.knowledge.memory_estimates import HeldSnapshot, InMemoryEstimates
from expert_work.persistence.knowledge.passages import (
    SWITCH_OFF_REVISION,
    ChunkVector,
    DisabledReason,
    ManualPassage,
    Passage,
    PassageChild,
    PassageConflictError,
    PassageLayout,
    PassageRevision,
    ReviewCounts,
    SwitchOffRecord,
    edited_since,
    manual_passages,
    passage_rows,
    settle_switch_off,
    unapplied_revisions,
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
    SEARCHABLE_CHUNK_TYPES,
    ChunkType,
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


def _vector(chunk: KnowledgeChunk) -> tuple[float, ...]:
    """A recalled chunk's vector. Only a parent row has none, and recall never
    reads one — so a missing vector here is a bug, not a miss."""
    if chunk.embedding is None:
        msg = f"chunk {chunk.id} ({chunk.chunk_type.value}) has no embedding"
        raise ValueError(msg)
    return chunk.embedding


def _without_vector(chunk: KnowledgeChunk) -> KnowledgeChunk:
    """``chunk`` as the SQL store's vector-less projection reads it: ``()``
    for a chunk with a vector, ``None`` for a parent row."""
    return chunk.model_copy(update={"embedding": None if chunk.embedding is None else ()})


def _public(passage: Passage) -> Passage:
    """``passage`` as the SQL store reads it: vectors not loaded."""
    row = _without_vector(passage.row)
    if len(passage.children) == 1 and passage.children[0] is passage.row:
        return Passage(row, (row,))
    return Passage(row, tuple(_without_vector(child) for child in passage.children))


def _cosine_distance(a: Sequence[float], b: Sequence[float]) -> float:
    """Cosine distance (0 = identical) — mirrors pgvector's ``<=>``."""
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 1.0
    return 1.0 - dot / (norm_a * norm_b)


class InMemoryKnowledgeStore(InMemoryEstimates, KnowledgeStore):
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
        #: B-175 P3-PR2 — ``knowledge_chunk_revision``: (tenant, document, row);
        #: the text revisions, and apart from them the switch-off records.
        self._revisions: list[tuple[UUID, UUID, PassageRevision]] = []
        self._switch_offs: list[tuple[UUID, UUID, PassageRevision]] = []
        #: B-222 PR-2 — ``ingest_estimate`` as stored and ``knowledge_parse_snapshot``.
        self._estimates: dict[UUID, dict[str, Any]] = {}
        self._snapshots: dict[UUID, HeldSnapshot] = {}

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

    async def count_documents_capped(
        self, *, tenant_id: UUID, kb_ids: Sequence[UUID], cap: int
    ) -> dict[UUID, int]:
        counts = dict.fromkeys(kb_ids, 0)
        for doc in self._documents:
            if doc.tenant_id == tenant_id and doc.kb_id in counts:
                counts[doc.kb_id] = min(counts[doc.kb_id] + 1, cap)
        return counts

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
                # B-222 PR-2: queued without an estimate.
                "ingest_estimate": None,
                "confirm_by": None,
            }
            self._estimates.pop(existing.id, None)
            self._snapshots.pop(existing.id, None)
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

    async def get_document_by_seq(
        self, *, tenant_id: UUID, doc_seq: int
    ) -> KnowledgeDocument | None:
        return next(
            (d for d in self._documents if d.tenant_id == tenant_id and d.doc_seq == doc_seq),
            None,
        )

    async def find_documents_by_name(
        self,
        *,
        tenant_id: UUID,
        kb_ids: Sequence[UUID],
        name: str,
        limit: int,
        exact: bool = False,
    ) -> list[KnowledgeDocument]:
        needle = name.lower()
        bases = set(kb_ids)

        def matches(field: str) -> bool:
            return field == needle if exact else needle in field

        found = [
            d
            for d in self._documents
            if d.tenant_id == tenant_id
            and d.kb_id in bases
            and d.enabled
            and (matches(d.filename.lower()) or matches((d.title or "").lower()))
        ]
        found.sort(key=lambda d: (d.doc_seq is None, d.doc_seq or 0, str(d.id)))
        return found[:limit]

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
        # B-222 PR-2: an estimate is claimed like an ingest, and stays estimating.
        if doc.status in (DocumentStatus.PROCESSING, DocumentStatus.ESTIMATING):
            lease = self._lease.get(doc.id)
            return lease is None or lease < now
        return False

    def _claim(self, doc: KnowledgeDocument, now: datetime, lease_seconds: int) -> ClaimedIngestion:
        estimating = doc.status is DocumentStatus.ESTIMATING
        claimed = doc.model_copy(
            update={
                "status": DocumentStatus.ESTIMATING if estimating else DocumentStatus.PROCESSING,
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
            phase="estimate" if estimating else "ingest",
            estimate=claimed.ingest_estimate,
            origin=self._estimates.get(doc.id, {}).get("origin"),
            allow_duplicate=bool(self._estimates.get(doc.id, {}).get("allow_duplicate")),
            serving_sha256=self._sha256.get(doc.id),
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
        document = await self.get_document(tenant_id=tenant_id, document_id=document_id)
        if document is None:
            return False
        if generation is not None and not _fence_in(document, generation, _WORKING):
            return False
        failed = await self.set_document_status(
            tenant_id=tenant_id,
            document_id=document_id,
            status=DocumentStatus.FAILED,
            error=error,
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
        if document_id not in self._pending or _writing_chunks(document):
            return document
        del self._pending[document_id]
        serving = document.chunk_count > 0
        update: dict[str, Any] = {
            "status": DocumentStatus.READY if serving else DocumentStatus.FAILED,
            "error": None if serving else _DISCARDED_ERROR,
            "error_code": None,
            "stage": IngestStage.READY if serving else document.stage,
            "has_pending": False,
            "ingest_generation": document.ingest_generation + 1,
            "attempts": 0,
            "updated_at": datetime.now(UTC),
        }
        if document.status in WAITING_STATUSES:
            # B-222 PR-2: the estimate was for the bytes dropped — it goes too.
            previous = discarded_estimate(self._estimates.get(document_id))
            self._snapshots.pop(document_id, None)
            update["confirm_by"] = None
            restored = self._store_document(document, update, previous)
        else:
            restored = document.model_copy(update=update)
            self._documents[self._documents.index(document)] = restored
        self._lease[document_id] = None
        return restored

    async def cancel_documents(
        self, *, tenant_id: UUID, kb_id: UUID, document_ids: Sequence[UUID]
    ) -> dict[UUID, CancelOutcome]:
        outcomes: dict[UUID, CancelOutcome] = {}
        now = datetime.now(UTC)
        for document_id in dict.fromkeys(document_ids):
            document = await self.get_document(tenant_id=tenant_id, document_id=document_id)
            if document is None or document.kb_id != kb_id:
                outcomes[document_id] = "not_found"
            elif document.status in WAITING_STATUSES:
                # B-222 PR-2: nothing was spent yet — the estimate is withdrawn.
                outcomes[document_id] = self._withdraw(document, now)
            elif document.status not in _CANCELLABLE_STATUSES or _writing_chunks(document):
                outcomes[document_id] = "not_cancellable"
            else:
                self._snapshots.pop(document_id, None)
                self._patch(
                    tenant_id,
                    document_id,
                    {
                        "status": DocumentStatus.CANCELLED,
                        "error": _CANCELLED_ERROR,
                        "ingest_generation": document.ingest_generation + 1,
                        "updated_at": now,
                    },
                )
                self._lease[document_id] = None
                outcomes[document_id] = "cancelled"
        return outcomes

    async def sweep_terminal(self, *, now: datetime, max_attempts: int) -> int:
        swept = 0
        for doc in list(self._documents):
            if doc.status not in _WORKING or doc.attempts < max_attempts:
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
        if document is None or not _fence_in(document, generation, _WORKING):
            return False
        self._lease[document_id] = now + timedelta(seconds=lease_seconds)
        return True

    async def list_documents(
        self,
        *,
        tenant_id: UUID,
        kb_id: UUID,
        enabled: bool | None = None,
        statuses: Collection[DocumentStatus] | None = None,
    ) -> list[KnowledgeDocument]:
        docs = [
            d
            for d in self._documents
            if d.tenant_id == tenant_id
            and d.kb_id == kb_id
            and (enabled is None or d.enabled is enabled)
            and (statuses is None or d.status in statuses)
        ]
        return sorted(docs, key=_created_key, reverse=True)

    async def list_documents_page(
        self,
        *,
        tenant_id: UUID,
        kb_id: UUID,
        limit: int,
        offset: int,
        enabled: bool | None = None,
        statuses: Collection[DocumentStatus] | None = None,
    ) -> tuple[list[KnowledgeDocument], int]:
        docs = await self.list_documents(
            tenant_id=tenant_id, kb_id=kb_id, enabled=enabled, statuses=statuses
        )
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
            # B-222 PR-2 — the SQL FK cascades the snapshot (0179).
            self._estimates.pop(document_id, None)
            self._snapshots.pop(document_id, None)
        self._parse_cache = {
            key: entry for key, entry in self._parse_cache.items() if key[1] not in document_ids
        }
        # B-175 P3-PR2 — the passages' history (the SQL FK cascades, 0176).
        self._revisions = [r for r in self._revisions if r[1] not in document_ids]
        self._switch_offs = [r for r in self._switch_offs if r[1] not in document_ids]

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

    # -- maintenance (B-175 P3-PR2) -------------------------------------------

    async def set_documents_enabled(
        self, *, tenant_id: UUID, kb_id: UUID, document_ids: Sequence[UUID], enabled: bool
    ) -> list[UUID]:
        wanted = set(document_ids)
        done: set[UUID] = set()
        now = datetime.now(UTC)
        for index, document in enumerate(self._documents):
            if (
                document.tenant_id == tenant_id
                and document.kb_id == kb_id
                and document.id in wanted
            ):
                self._documents[index] = document.model_copy(
                    update={"enabled": enabled, "updated_at": now}
                )
                done.add(document.id)
        return [document_id for document_id in dict.fromkeys(document_ids) if document_id in done]

    async def count_stale_documents(self, *, tenant_id: UUID, kb_id: UUID, params_hash: str) -> int:
        stale = await self.list_stale_documents(
            tenant_id=tenant_id, kb_id=kb_id, params_hash=params_hash
        )
        return len(stale)

    async def list_stale_documents(
        self, *, tenant_id: UUID, kb_id: UUID, params_hash: str
    ) -> list[KnowledgeDocument]:
        ready = await self.list_documents(
            tenant_id=tenant_id, kb_id=kb_id, statuses=(DocumentStatus.READY,)
        )
        return [d for d in ready if d.chunk_params_hash != params_hash]

    async def list_passages(
        self,
        *,
        tenant_id: UUID,
        document_id: UUID,
        enabled_only: bool = False,
        offset: int = 0,
        limit: int | None = None,
        disabled_reason: DisabledReason | None = None,
    ) -> list[Passage]:
        passages = [_public(p) for p in self._passages(tenant_id, document_id)]
        if disabled_reason is not None:
            passages = [p for p in passages if p.row.disabled_reason == disabled_reason]
        if enabled_only:
            document = await self.get_document(tenant_id=tenant_id, document_id=document_id)
            if document is None or not document.enabled:
                return []
            passages = [
                Passage(p.row, tuple(c for c in p.children if c.enabled))
                for p in passages
                if p.row.enabled
            ]
        end = None if limit is None else offset + limit
        return passages[offset:end]

    async def count_passages(self, *, tenant_id: UUID, document_id: UUID) -> int:
        return len(self._passages(tenant_id, document_id))

    async def get_passage(
        self, *, tenant_id: UUID, document_id: UUID, passage_id: UUID
    ) -> Passage | None:
        found = self._passage(tenant_id, document_id, passage_id)
        return _public(found) if found is not None else None

    async def replace_passage(
        self,
        *,
        tenant_id: UUID,
        document_id: UUID,
        passage_id: UUID,
        expected_revision: int,
        content: str,
        heading_path: Sequence[str],
        children: Sequence[PassageChild],
        edited_by: str | None,
    ) -> Passage | None:
        document = await self._settled_document(tenant_id, document_id)
        if document is None:
            return None
        old = self._passage(tenant_id, document_id, passage_id)
        if old is None:
            return None
        if old.row.revision != expected_revision:
            raise PassageConflictError("revision_conflict", current=_public(old))
        history = self._history(tenant_id, document_id, passage_id)
        if not history:
            # The text ingested from the file, kept before its first change.
            history = [
                self._record(
                    document,
                    passage_id,
                    old.row.revision,
                    old.row.content,
                    old.row.heading_path,
                    None,
                )
            ]
        revision = max(old.row.revision, history[-1].revision) + 1
        self._record(document, passage_id, revision, content, heading_path, edited_by)
        self._write_passage(
            document,
            old,
            content=content,
            heading_path=heading_path,
            children=children,
            revision=revision,
            edited=edited_since(history[0], content, heading_path),
            enabled=old.row.enabled,
            disabled_reason=old.row.disabled_reason,
        )
        return await self.get_passage(
            tenant_id=tenant_id, document_id=document_id, passage_id=passage_id
        )

    async def add_passage(
        self,
        *,
        tenant_id: UUID,
        document_id: UUID,
        content: str,
        heading_path: Sequence[str],
        children: Sequence[PassageChild],
        edited_by: str | None,
    ) -> Passage | None:
        document = await self._settled_document(tenant_id, document_id)
        if document is None:
            return None
        passage_id = uuid4()
        self._record(document, passage_id, 1, content, heading_path, edited_by)
        self._write_passage(
            document,
            None,
            content=content,
            heading_path=heading_path,
            children=children,
            revision=1,
            edited=True,
            enabled=True,
            passage_id=passage_id,
        )
        return await self.get_passage(
            tenant_id=tenant_id, document_id=document_id, passage_id=passage_id
        )

    async def set_passage_enabled(
        self, *, tenant_id: UUID, document_id: UUID, passage_id: UUID, enabled: bool
    ) -> Passage | None:
        if await self._settled_document(tenant_id, document_id) is None:
            return None
        passage = self._passage(tenant_id, document_id, passage_id)
        if passage is None:
            return None
        ids = {passage.row.id, *(c.id for c in passage.children)}
        update = {"enabled": enabled, "disabled_reason": None if enabled else "manual"}
        self._chunks = [c.model_copy(update=update) if c.id in ids else c for c in self._chunks]
        if passage.row.disabled_reason == "inherited":
            self._settle_switch_offs(tenant_id, document_id, passage.row.content)
        return await self.get_passage(
            tenant_id=tenant_id, document_id=document_id, passage_id=passage_id
        )

    def _settle_switch_offs(self, tenant_id: UUID, document_id: UUID, passage: str) -> None:
        """A person decided about a passage off by inheritance: its paragraphs
        leave the document's switch-off records (an emptied one goes)."""
        settled: list[tuple[UUID, UUID, PassageRevision]] = []
        for tenant, document, record in self._switch_offs:
            if (tenant, document) == (tenant_id, document_id):
                content = settle_switch_off(record.content, passage)
                if not content:
                    continue
                record = replace(record, content=content)
            settled.append((tenant, document, record))
        self._switch_offs = settled

    def _records(self, tenant_id: UUID, document_id: UUID) -> list[PassageRevision]:
        return [
            record
            for tenant, document, record in self._switch_offs
            if (tenant, document) == (tenant_id, document_id)
        ]

    async def list_passage_revisions(
        self, *, tenant_id: UUID, document_id: UUID, passage_id: UUID
    ) -> list[PassageRevision]:
        return self._history(tenant_id, document_id, passage_id)

    async def list_switch_offs(
        self, *, tenant_id: UUID, document_id: UUID
    ) -> list[PassageRevision]:
        return self._records(tenant_id, document_id)

    async def list_unapplied_revisions(
        self, *, tenant_id: UUID, document_id: UUID
    ) -> list[PassageRevision]:
        histories: dict[UUID, list[PassageRevision]] = {}
        for tenant, document, record in self._revisions:
            if tenant == tenant_id and document == document_id:
                histories.setdefault(record.passage_id, []).append(record)
        current = {p.row.id: p.row.revision for p in self._passages(tenant_id, document_id)}
        return unapplied_revisions(histories, current, self._records(tenant_id, document_id))

    async def discard_unapplied_revisions(self, *, tenant_id: UUID, document_id: UUID) -> int:
        unapplied = await self.list_unapplied_revisions(
            tenant_id=tenant_id, document_id=document_id
        )
        current = {p.row.id: p.row.revision for p in self._passages(tenant_id, document_id)}
        # Gone: the whole history; still there: the revisions above its own;
        # a switch-off record: itself.
        records = {r.passage_id for r in unapplied if r.switch_off_lost}
        floor = {
            r.passage_id: current.get(r.passage_id, 0) for r in unapplied if not r.switch_off_lost
        }
        mine = (tenant_id, document_id)
        kept = [
            entry
            for entry in self._revisions
            if not (
                entry[:2] == mine
                and entry[2].passage_id in floor
                and entry[2].revision > floor[entry[2].passage_id]
            )
        ]
        kept_records = [
            entry
            for entry in self._switch_offs
            if not (entry[:2] == mine and entry[2].passage_id in records)
        ]
        discarded = len(self._revisions) - len(kept) + len(self._switch_offs) - len(kept_records)
        self._revisions, self._switch_offs = kept, kept_records
        return discarded

    async def count_review_items(
        self, *, tenant_id: UUID, document_ids: Sequence[UUID]
    ) -> dict[UUID, ReviewCounts]:
        counts: dict[UUID, ReviewCounts] = {}
        for document_id in dict.fromkeys(document_ids):
            inherited = sum(
                1
                for p in self._passages(tenant_id, document_id)
                if p.row.disabled_reason == "inherited"
            )
            unapplied = len(
                await self.list_unapplied_revisions(tenant_id=tenant_id, document_id=document_id)
            )
            if inherited or unapplied:
                counts[document_id] = ReviewCounts(inherited=inherited, unapplied=unapplied)
        return counts

    async def list_manual_passages(
        self, *, tenant_id: UUID, document_id: UUID
    ) -> list[ManualPassage]:
        histories: dict[UUID, list[PassageRevision]] = {}
        for tenant, document, record in sorted(self._revisions, key=lambda e: e[2].revision):
            if tenant == tenant_id and document == document_id:
                histories.setdefault(record.passage_id, []).append(record)
        rows = [_without_vector(p.row) for p in self._passages(tenant_id, document_id)]
        return manual_passages(rows, histories)

    async def update_chunk_embeddings(
        self,
        *,
        tenant_id: UUID,
        document_id: UUID,
        vectors: Sequence[ChunkVector],
        embedding_model: str | None,
        generation: int,
        expect_status: DocumentStatus = DocumentStatus.READY,
    ) -> int | None:
        document = await self.get_document(tenant_id=tenant_id, document_id=document_id)
        if document is None or not _fence_in(
            document, generation, reindex_fence_statuses(expect_status)
        ):
            return None
        wanted = {v.chunk_id: v for v in vectors}
        updated = 0
        for index, chunk in enumerate(self._chunks):
            vector = wanted.get(chunk.id)
            if (
                vector is None
                or chunk.tenant_id != tenant_id
                or chunk.document_id != document_id
                or chunk.chunk_type not in SEARCHABLE_CHUNK_TYPES
                or chunk.content_hash != vector.content_hash
            ):
                continue
            self._chunks[index] = chunk.model_copy(
                update={"embedding": tuple(vector.embedding), "embedding_model": embedding_model}
            )
            updated += 1
        return updated

    def _passages(self, tenant_id: UUID, document_id: UUID) -> list[Passage]:
        """The document's passages in document order, vectors kept (the
        SQL store's ``_passages_stmt`` order)."""
        rows = [
            c for c in self._chunks if c.tenant_id == tenant_id and c.document_id == document_id
        ]
        under: dict[UUID, list[KnowledgeChunk]] = {}
        for chunk in rows:
            if chunk.chunk_type in SEARCHABLE_CHUNK_TYPES and chunk.parent_chunk_id is not None:
                under.setdefault(chunk.parent_chunk_id, []).append(chunk)
        passages: list[Passage] = []
        for chunk in rows:
            if chunk.chunk_type is ChunkType.PARENT:
                kids = sorted(under.get(chunk.id, []), key=lambda c: c.chunk_index)
                passages.append(Passage(chunk, tuple(kids)))
            elif chunk.chunk_type in SEARCHABLE_CHUNK_TYPES and chunk.parent_chunk_id is None:
                passages.append(Passage(chunk, (chunk,)))

        def place(passage: Passage) -> tuple[bool, int, str, int]:
            first = passage.children[0].chunk_index if passage.children else None
            row = passage.row
            return (first is None, first or 0, row.chunk_type.value, row.chunk_index)

        return sorted(passages, key=place)

    def _passage(self, tenant_id: UUID, document_id: UUID, passage_id: UUID) -> Passage | None:
        return next(
            (p for p in self._passages(tenant_id, document_id) if p.row.id == passage_id), None
        )

    async def _settled_document(
        self, tenant_id: UUID, document_id: UUID
    ) -> KnowledgeDocument | None:
        """The document a passage write may change, or ``None`` when there is
        none; :class:`PassageConflictError` while its chunks are about to be
        replaced (being ingested, or a new version waits)."""
        document = await self.get_document(tenant_id=tenant_id, document_id=document_id)
        if document is None:
            return None
        if document.status is not DocumentStatus.READY or document.has_pending:
            raise PassageConflictError("document_busy")
        return document

    def _history(
        self, tenant_id: UUID, document_id: UUID, passage_id: UUID
    ) -> list[PassageRevision]:
        found = [
            record
            for tenant, document, record in self._revisions
            if tenant == tenant_id and document == document_id and record.passage_id == passage_id
        ]
        return sorted(found, key=lambda record: record.revision)

    def _record(
        self,
        document: KnowledgeDocument,
        passage_id: UUID,
        revision: int,
        content: str,
        heading_path: Sequence[str],
        edited_by: str | None,
    ) -> PassageRevision:
        if any(
            r.passage_id == passage_id and r.revision == revision for _, _, r in self._revisions
        ):
            # Mirror the SQL unique (chunk_id, revision).
            msg = f"revision {revision} of passage {passage_id} already exists"
            raise ValueError(msg)
        record = PassageRevision(
            passage_id=passage_id,
            revision=revision,
            content=content,
            heading_path=tuple(heading_path),
            edited_by=edited_by,
            created_at=datetime.now(UTC),
        )
        self._revisions.append((document.tenant_id, document.id, record))
        return record

    def _write_passage(
        self,
        document: KnowledgeDocument,
        old: Passage | None,
        *,
        content: str,
        heading_path: Sequence[str],
        children: Sequence[PassageChild],
        revision: int,
        edited: bool,
        enabled: bool,
        passage_id: UUID | None = None,
        disabled_reason: DisabledReason | None = None,
    ) -> None:
        mine = [c for c in self._chunks if c.document_id == document.id]
        searchable = [c.chunk_index for c in mine if c.chunk_type in SEARCHABLE_CHUNK_TYPES]
        parents = [c.chunk_index for c in mine if c.chunk_type is ChunkType.PARENT]
        layout = PassageLayout(
            passage_id=old.row.id if old is not None else (passage_id or uuid4()),
            tenant_id=document.tenant_id,
            kb_id=document.kb_id,
            document_id=document.id,
            old=old,
            next_index=max(searchable, default=-1) + 1,
            next_parent_index=max(parents, default=-1) + 1,
        )
        rows = passage_rows(
            layout,
            content=content,
            heading_path=heading_path,
            children=children,
            revision=revision,
            edited=edited,
            enabled=enabled,
            disabled_reason=disabled_reason,
        )
        replaced = {old.row.id, *(c.id for c in old.children)} if old is not None else set()
        self._chunks = [c for c in self._chunks if c.id not in replaced]
        self._chunks.extend(rows)
        count = sum(
            1
            for c in self._chunks
            if c.document_id == document.id and c.chunk_type in SEARCHABLE_CHUNK_TYPES
        )
        self._patch(
            document.tenant_id,
            document.id,
            {"chunk_count": count, "updated_at": datetime.now(UTC)},
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
        chunk_params_hash: str | None = None,
        switch_offs: Collection[SwitchOffRecord] = (),
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
        keys = [(c.chunk_type, c.chunk_index) for c in chunks]
        if len(keys) != len(set(keys)):
            # Mirror the SQL unique (document_id, chunk_type, chunk_index) (0175).
            msg = f"duplicate chunk_index in replace_chunks for document {document_id}"
            raise ValueError(msg)
        if any(c.embedding is None and c.chunk_type is not ChunkType.PARENT for c in chunks):
            # Mirror the SQL CHECK: only a parent row goes without a vector (0175).
            msg = f"a chunk that is not a parent has no embedding (document {document_id})"
            raise ValueError(msg)
        if switch_offs:
            self._write_switch_offs(tenant_id, document_id, switch_offs)
        self._chunks = [
            c
            for c in self._chunks
            if not (c.tenant_id == tenant_id and c.document_id == document_id)
        ]
        # A parent row is stored without a vector, and an enabled row without a
        # disabled reason, as the SQL store writes them.
        self._chunks.extend(
            c.model_copy(
                update={
                    "embedding": None if c.chunk_type is ChunkType.PARENT else c.embedding,
                    "disabled_reason": None if c.enabled else c.disabled_reason,
                }
            )
            for c in chunks
        )
        if chunk_params_hash is not None:
            self._documents[self._documents.index(document)] = document.model_copy(
                update={"chunk_params_hash": chunk_params_hash}
            )
        self._stamp_orphans(tenant_id, document_id)
        return True

    def _write_switch_offs(
        self, tenant_id: UUID, document_id: UUID, records: Collection[SwitchOffRecord]
    ) -> None:
        """``replace_chunks(switch_offs=…)``: a record this ingest made is
        inserted; an earlier one only updated, if it still exists (one
        discarded or expired meanwhile stays gone). ``orphaned_at`` cleared
        when placed, else set once."""
        now = datetime.now(UTC)
        known = {r.passage_id: r for r in self._records(tenant_id, document_id)}
        written: dict[UUID, PassageRevision] = {}
        for record in records:
            previous = known.get(record.passage_id)
            if previous is None and not record.new:
                continue
            since = previous.orphaned_at if previous is not None else None
            orphaned_at = None if record.placed else (since or now)
            written[record.passage_id] = (
                replace(previous, orphaned_at=orphaned_at)
                if previous is not None
                else PassageRevision(
                    passage_id=record.passage_id,
                    revision=SWITCH_OFF_REVISION,
                    content=record.content,
                    heading_path=record.heading_path,
                    edited_by=None,
                    created_at=now,
                    orphaned_at=orphaned_at,
                    switch_off_lost=True,
                )
            )
        self._switch_offs = [
            entry
            for entry in self._switch_offs
            if not (entry[:2] == (tenant_id, document_id) and entry[2].passage_id in written)
        ] + [(tenant_id, document_id, record) for record in written.values()]

    def _stamp_orphans(self, tenant_id: UUID, document_id: UUID) -> None:
        """B-175 P3-PR2 — histories of passages the document no longer has
        get ``orphaned_at`` (once); a passage that is there again clears it."""
        present = {p.row.id for p in self._passages(tenant_id, document_id)}
        now = datetime.now(UTC)
        stamped: list[tuple[UUID, UUID, PassageRevision]] = []
        for tenant, document, record in self._revisions:
            if tenant == tenant_id and document == document_id:
                if record.passage_id in present:
                    record = replace(record, orphaned_at=None)
                elif record.orphaned_at is None:
                    record = replace(record, orphaned_at=now)
            stamped.append((tenant, document, record))
        self._revisions = stamped

    async def list_chunks(
        self,
        *,
        tenant_id: UUID,
        document_id: UUID,
        offset: int = 0,
        limit: int = 50,
        chunk_types: Collection[ChunkType] | None = None,
        after: tuple[int, ChunkType] | None = None,
    ) -> tuple[list[KnowledgeChunk], int]:
        wanted = SEARCHABLE_CHUNK_TYPES if chunk_types is None else frozenset(chunk_types)
        matching = [
            c
            for c in self._chunks
            if c.tenant_id == tenant_id and c.document_id == document_id and c.chunk_type in wanted
        ]
        matching.sort(key=lambda c: (c.chunk_index, c.chunk_type.value))
        start = (after[0], after[1].value) if after is not None else None
        listed = [
            c for c in matching if start is None or (c.chunk_index, c.chunk_type.value) > start
        ]
        # Mirror the SQL projection: embedding omitted from preview rows.
        page = [_without_vector(c) for c in listed[offset : offset + limit]]
        return page, len(matching)

    async def get_chunks(
        self, *, tenant_id: UUID, chunk_ids: Sequence[UUID]
    ) -> list[KnowledgeChunk]:
        found = {c.id: c for c in self._chunks if c.tenant_id == tenant_id}
        return [_without_vector(found[i]) for i in dict.fromkeys(chunk_ids) if i in found]

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
            c.content_hash: _vector(c)
            for c in self._chunks
            if c.tenant_id == tenant_id
            and c.document_id == document_id
            and c.chunk_type in SEARCHABLE_CHUNK_TYPES
            and c.embedding_model == embedding_model
            and c.content_hash is not None
            and c.content_hash in wanted
        }

    async def has_chunks_off_model(
        self, *, tenant_id: UUID, kb_id: UUID, embedding_model: str | None
    ) -> bool:
        return any(
            c.tenant_id == tenant_id
            and c.kb_id == kb_id
            and c.chunk_type in SEARCHABLE_CHUNK_TYPES
            and c.embedding_model != embedding_model
            for c in self._chunks
        )

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
        candidates = self._recallable(tenant_id, kb_ids)
        candidates.sort(key=lambda c: _cosine_distance(query_embedding, _vector(c)))
        return [
            ScoredChunk(
                chunk=chunk,
                score=1.0 - _cosine_distance(query_embedding, _vector(chunk)),
                source="vector",
            )
            for chunk in candidates[:limit]
        ]

    def _recallable(self, tenant_id: UUID, kb_ids: Sequence[UUID]) -> list[KnowledgeChunk]:
        """The chunks recall may return: the tenant's, in ``kb_ids``, of the
        searchable types, enabled, of a document that is not disabled — also
        BM25's N for keyword search (the SQL store's ``_recall_scope``)."""
        kb_set = set(kb_ids)
        disabled = {
            d.id
            for d in self._documents
            if d.tenant_id == tenant_id and d.kb_id in kb_set and not d.enabled
        }
        return [
            c
            for c in self._chunks
            if c.tenant_id == tenant_id
            and c.kb_id in kb_set
            and c.chunk_type in SEARCHABLE_CHUNK_TYPES
            and c.enabled
            and c.document_id not in disabled
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
        in_scope = self._recallable(tenant_id, kb_ids)
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
#: B-222 —— the statuses :meth:`InMemoryKnowledgeStore.cancel_documents` may
#: stop (short of the point of no return, :func:`_writing_chunks`), and the
#: ``error`` it writes (same text as the SQL store).
_CANCELLABLE_STATUSES = (DocumentStatus.PENDING, DocumentStatus.PROCESSING)
_CANCELLED_ERROR = "cancelled by user"


def _writing_chunks(document: KnowledgeDocument) -> bool:
    """B-222 —— past the point of no return: ``processing`` at ``indexing``,
    whatever the lease (the SQL store's ``_writing_chunks``)."""
    return document.status is DocumentStatus.PROCESSING and document.stage is IngestStage.INDEXING


def _fence_matches(
    document: KnowledgeDocument,
    generation: int,
    status: DocumentStatus = DocumentStatus.PROCESSING,
) -> bool:
    """The generation fence: the write-back belongs to the claim that still
    owns this exact generation of the document in the expected status."""
    return document.status is status and document.ingest_generation == generation


#: B-222 PR-2 —— what a worker's lease renewal and terminal failure are fenced
#: on: the paid ingest, or the free estimate.
_WORKING = frozenset({DocumentStatus.PROCESSING, DocumentStatus.ESTIMATING})


def _fence_in(
    document: KnowledgeDocument, generation: int, statuses: Collection[DocumentStatus]
) -> bool:
    return document.status in statuses and document.ingest_generation == generation
