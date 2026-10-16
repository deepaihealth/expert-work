"""Knowledge retrieval — Stream J.5 RAG.

The ``knowledge_search`` tool an agent calls to ground its answers in
the tenant's uploaded documents. Retrieval is **hybrid**:

1. **vector recall** — the query is embedded and matched by cosine
   similarity against ``knowledge_chunk`` embeddings;
2. **keyword recall** — the query is matched by Postgres full-text
   relevance (jieba-segmented, so it is correct for CJK);
3. the two ranked lists are fused by (weighted) **Reciprocal Rank Fusion**; and
4. an optional **LLM reranker** reorders the fused candidates by
   judged relevance before the top-k is returned.

Vector recall alone misses exact-term matches (error codes, names);
keyword recall alone misses paraphrases — hybrid covers both, and the
rerank pass fixes ordering precision. See ``STREAM-J-DESIGN.md`` § 12.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable
from uuid import UUID

from langchain_core.messages import HumanMessage, SystemMessage

from expert_work.common.observability import ExpertWorkComponent, expert_work_span
from expert_work.common.search.rrf import weighted_rrf_fuse_scored
from expert_work.persistence import KnowledgeStore
from expert_work.protocol import (
    KnowledgeBase,
    KnowledgeChunk,
    RetrievalMethod,
    chunk_retrieval_text,
)
from orchestrator.tools.registry import ToolBlockedError, ToolContext, ToolResult, ToolSpec

if TYPE_CHECKING:
    # Imported under TYPE_CHECKING only — a runtime import of
    # ``orchestrator.llm`` here would cycle (llm → tools.registry → tools).
    from orchestrator.llm import Embedder, LLMCaller

logger = logging.getLogger(__name__)

#: Default per-path recall depth = ``limit`` x this, clamped to
#: [``_MIN_RECALL_DEPTH``, ``_MAX_RERANK_CANDIDATES``] — wider than the final
#: top-k so fusion and the reranker have candidates to work with.
_RECALL_DEPTH_FACTOR = 2
_MIN_RECALL_DEPTH = 50
#: ``qwen3-vl-rerank`` takes at most 100 documents per request: the ceiling of
#: the default recall depth and a hard cap on rerank candidates.
_MAX_RERANK_CANDIDATES = 100
#: Each candidate is truncated to this many characters in the rerank
#: prompt — enough to judge relevance without an enormous prompt.
_RERANK_DOC_CHARS = 600
#: ``knowledge_search`` result-count default + hard cap.
_DEFAULT_LIMIT = 5
_MAX_LIMIT = 20
#: Two-view rerank (B-175 wave 2 PR2b): a scoring reranker judges every
#: candidate with its document title, then its best ``_BODY_VIEW_DEPTH`` again
#: on the body alone, and they are ranked by ``_TITLED_VIEW_WEIGHT`` x titled +
#: the rest x body. The title places a passage that never names its product;
#: on its own it makes every passage of a file named after the code a question
#: asks about (a patent certificate titled with the strain) look on topic, and
#: they bury the passage that carries the code.
_TITLED_VIEW_WEIGHT = 0.7
_BODY_VIEW_DEPTH = 20


@runtime_checkable
class Reranker(Protocol):
    """Reorders retrieval candidates by judged relevance to a query.

    Stream O (Mini-ADR O-9) — ``tenant_id`` lets a credential-resolving
    reranker pick the per-tenant API key at call time. Implementations
    without per-tenant keys accept and ignore it. A resolving reranker
    that finds no credential degrades to the fused order (rerank is
    optional), so it is *not* gated at mode-switch time."""

    async def rerank(
        self, *, query: str, documents: Sequence[str], top_k: int, tenant_id: UUID
    ) -> list[int]:
        """Return the indices of the ``top_k`` most relevant ``documents``,
        most relevant first. Indices are positions in ``documents``."""


@runtime_checkable
class ScoredReranker(Protocol):
    """A :class:`Reranker` that can also say how relevant each document is."""

    async def rerank_scored(
        self, *, query: str, documents: Sequence[str], top_k: int, tenant_id: UUID
    ) -> list[tuple[int, float | None]]:
        """Like :meth:`Reranker.rerank`, each index paired with its relevance
        score: ``None`` throughout when the backend ranks without scoring (a
        chat-model reranker) or rerank was skipped."""


@dataclass(frozen=True)
class RetrievedChunk:
    """A retrieved chunk with its source document attribution.

    ``score`` is the vector cosine similarity in [0, 1] when the chunk was
    surfaced by vector recall (``None`` for keyword-only hits, whose
    ``ts_rank`` is not a comparable similarity). ``recall_source`` records
    which recall path(s) found it. Both default to ``None`` so the tool's
    formatted output is unaffected — they exist for the retrieval-test
    endpoint's transparency.

    The per-path sub-scores (B-175 wave 2) are kept for diagnostics and are
    ``None`` when a path did not surface the chunk: ``vector_score`` (same
    value as ``score``), ``keyword_score`` (the keyword path's rank value)
    and ``fused_score`` (the weighted RRF score that ordered the fusion).

    ``content`` is the chunk body; ``heading_path`` its section breadcrumb.
    The agent sees (and the eval matches against) :attr:`retrieval_text`, the
    two joined, under a ``[filename#index]`` header. The reranker ranks the
    stored chunk's own retrieval text, which also carries the document title
    — here it would only repeat the header.
    """

    content: str
    filename: str
    chunk_index: int
    score: float | None = None
    recall_source: str | None = None
    heading_path: tuple[str, ...] = ()
    chunk_id: UUID | None = None
    vector_score: float | None = None
    keyword_score: float | None = None
    fused_score: float | None = None

    @property
    def retrieval_text(self) -> str:
        return chunk_retrieval_text(self.heading_path, self.content)


@dataclass(frozen=True)
class _SubScores:
    """Per-chunk scores from one search, keyed by chunk id."""

    vector: Mapping[UUID, float]
    keyword: Mapping[UUID, float]
    fused: Mapping[UUID, float]


def default_recall_depth(limit: int) -> int:
    """Production per-path recall depth for a ``limit``-result search:
    ``2 x limit`` clamped to [50, 100] (100 = the reranker's per-request cap)."""
    return max(_MIN_RECALL_DEPTH, min(_RECALL_DEPTH_FACTOR * limit, _MAX_RERANK_CANDIDATES))


@dataclass(frozen=True)
class LLMReranker:
    """LLM-backed :class:`Reranker` — reuses an :class:`LLMCaller`.

    A cheap, dependency-free reranker: the LLM is asked to rank the
    candidates and reply with their numbers. A response it cannot parse
    falls back to the input order, so a flaky rerank never breaks search.
    """

    llm_caller: LLMCaller

    async def rerank(
        self, *, query: str, documents: Sequence[str], top_k: int, tenant_id: UUID
    ) -> list[int]:
        del tenant_id  # fixed-key reranker — credential baked into llm_caller
        if not documents:
            return []
        listing = "\n\n".join(
            f"[{i + 1}] {doc[:_RERANK_DOC_CHARS]}" for i, doc in enumerate(documents)
        )
        prompt = [
            SystemMessage(
                content=(
                    "You rank documents by relevance to a query. Reply with "
                    "ONLY a JSON array of the document numbers, most relevant "
                    "first, e.g. [3, 1, 5]. Include only genuinely relevant "
                    "documents."
                )
            ),
            HumanMessage(content=f"Query: {query}\n\nDocuments:\n{listing}"),
        ]
        try:
            with expert_work_span(ExpertWorkComponent.ORCHESTRATOR, "rerank"):
                response = await self.llm_caller(messages=prompt, tools=[])
            order = _parse_rerank_order(_message_text(response), len(documents))
        except Exception:
            logger.warning("knowledge.rerank_failed — using fused order", exc_info=True)
            order = []
        if not order:
            order = list(range(len(documents)))
        return order[:top_k]


@dataclass(frozen=True)
class KnowledgeRetriever:
    """Hybrid retrieval over a tenant's knowledge bases — Stream J.5.

    Bundles the :class:`KnowledgeStore`, the query :class:`Embedder`, and
    an optional :class:`Reranker`. Injected into the orchestrator's
    ``ToolEnv`` by the control-plane (which configures the embedder /
    rerank LLM). With no reranker the fused order is returned directly.

    ``recall_limit`` fixes the per-path recall depth; ``None`` (production)
    derives it per call (:func:`default_recall_depth`). ``vector_weight`` /
    ``keyword_weight`` weight the two paths in the fusion; equal weights
    give plain RRF.
    """

    store: KnowledgeStore
    embedder: Embedder
    reranker: Reranker | None = None
    recall_limit: int | None = None
    vector_weight: float = 1.0
    keyword_weight: float = 1.0

    def recall_depth(self, limit: int) -> int:
        """Per-path recall depth for a ``limit``-result search."""
        return self.recall_limit if self.recall_limit is not None else default_recall_depth(limit)

    async def search(
        self,
        *,
        tenant_id: UUID,
        base_names: Sequence[str],
        query: str,
        limit: int,
        method: RetrievalMethod | None = None,
        score_threshold: float | None = None,
        rerank: bool | None = None,
    ) -> list[RetrievedChunk]:
        """Hybrid-search ``base_names`` for ``query``, returning the
        ``limit`` most relevant chunks with source attribution + scores.

        ``method`` / ``score_threshold`` / ``rerank`` are *overrides*: when
        ``None`` each spanned base's own configured default is used. The
        recall method and similarity threshold are applied **per base**
        (a query may span bases with different configs); ``rerank`` is
        resolved query-wide (override > "any spanned base enables it").
        Base names that do not resolve to a knowledge base are skipped.
        """
        bases = await self._resolve_bases(tenant_id, base_names)
        if not bases:
            return []
        query_embedding = (await self.embedder.embed([query], tenant_id=tenant_id))[0]
        depth = self.recall_depth(limit)

        # Per-base recall, honouring each base's method + threshold. Each
        # path's score is recorded per chunk for diagnostics; only the vector
        # similarity is a comparable [0, 1] score. Fusion keys on the chunk id
        # (the chunk value need not be hashable, and the two paths may return
        # unequal copies of one chunk); the first copy seen is kept.
        rankings: list[tuple[float, list[UUID]]] = []
        chunk_by_id: dict[UUID, KnowledgeChunk] = {}
        vector_score: dict[UUID, float] = {}
        keyword_score: dict[UUID, float] = {}
        sources: dict[UUID, set[str]] = {}
        for base in bases:
            eff_method = method if method is not None else base.retrieval_method
            eff_threshold = (
                score_threshold if score_threshold is not None else base.retrieval_score_threshold
            )
            if eff_method in (RetrievalMethod.HYBRID, RetrievalMethod.VECTOR):
                vector = await self.store.search_scored(
                    tenant_id=tenant_id,
                    kb_ids=[base.id],
                    query_embedding=query_embedding,
                    limit=depth,
                )
                if eff_threshold is not None:
                    vector = [hit for hit in vector if hit.score >= eff_threshold]
                rankings.append((self.vector_weight, [hit.chunk.id for hit in vector]))
                for hit in vector:
                    chunk_by_id.setdefault(hit.chunk.id, hit.chunk)
                    vector_score[hit.chunk.id] = max(vector_score.get(hit.chunk.id, 0.0), hit.score)
                    sources.setdefault(hit.chunk.id, set()).add("vector")
            if eff_method in (RetrievalMethod.HYBRID, RetrievalMethod.KEYWORD):
                keyword = await self.store.keyword_search_scored(
                    tenant_id=tenant_id,
                    kb_ids=[base.id],
                    query=query,
                    limit=depth,
                )
                rankings.append((self.keyword_weight, [hit.chunk.id for hit in keyword]))
                for hit in keyword:
                    chunk_by_id.setdefault(hit.chunk.id, hit.chunk)
                    keyword_score[hit.chunk.id] = max(
                        keyword_score.get(hit.chunk.id, hit.score), hit.score
                    )
                    sources.setdefault(hit.chunk.id, set()).add("keyword")

        fused_scored = weighted_rrf_fuse_scored(rankings)
        if not fused_scored:
            return []
        fused = [chunk_by_id[chunk_id] for chunk_id, _score in fused_scored]
        rerank_enabled = rerank if rerank is not None else any(b.rerank_enabled for b in bases)
        chunks = await self._rerank(
            query, fused, limit, tenant_id=tenant_id, enabled=rerank_enabled
        )
        scores = _SubScores(
            vector=vector_score,
            keyword=keyword_score,
            fused=dict(fused_scored),
        )
        return await self._attribute(tenant_id, chunks, scores, sources)

    async def _resolve_bases(
        self, tenant_id: UUID, base_names: Sequence[str]
    ) -> list[KnowledgeBase]:
        # One list_bases read + in-memory match instead of a get_base per name
        # (an N+1 over the manifest's knowledge_base_refs). Order + skip-missing
        # semantics preserved. Bases per tenant are few, so the full read is cheap.
        by_name = {b.name: b for b in await self.store.list_bases(tenant_id=tenant_id)}
        return [by_name[name] for name in base_names if name in by_name]

    async def _rerank(
        self,
        query: str,
        fused: list[KnowledgeChunk],
        limit: int,
        *,
        tenant_id: UUID,
        enabled: bool,
    ) -> list[KnowledgeChunk]:
        if self.reranker is None or not enabled:
            return fused[:limit]
        # Capped after fusion: several bases (and an explicit recall_limit) can
        # fuse into more than one rerank request takes.
        candidates = fused[: min(self.recall_depth(limit), _MAX_RERANK_CANDIDATES)]
        if isinstance(self.reranker, ScoredReranker):
            order = await _two_view_order(
                self.reranker, query, candidates, limit, tenant_id=tenant_id
            )
        else:
            order = await self.reranker.rerank(
                query=query,
                documents=[chunk.retrieval_text for chunk in candidates],
                top_k=limit,
                tenant_id=tenant_id,
            )
        return [candidates[i] for i in order]

    async def _attribute(
        self,
        tenant_id: UUID,
        chunks: Sequence[KnowledgeChunk],
        scores: _SubScores,
        sources: Mapping[UUID, set[str]],
    ) -> list[RetrievedChunk]:
        # One batched lookup for every source document instead of a per-document
        # get_document (an N+1 over the result set — up to _MAX_LIMIT docs).
        doc_ids = {chunk.document_id for chunk in chunks}
        docs = await self.store.get_documents(tenant_id=tenant_id, document_ids=list(doc_ids))
        filenames: dict[UUID, str] = {
            doc_id: (docs[doc_id].filename if doc_id in docs else "(unknown)") for doc_id in doc_ids
        }
        return [
            RetrievedChunk(
                content=chunk.content,
                filename=filenames[chunk.document_id],
                chunk_index=chunk.chunk_index,
                score=scores.vector.get(chunk.id),
                recall_source=_recall_source(sources.get(chunk.id, set())),
                heading_path=chunk.heading_path,
                chunk_id=chunk.id,
                vector_score=scores.vector.get(chunk.id),
                keyword_score=scores.keyword.get(chunk.id),
                fused_score=scores.fused.get(chunk.id),
            )
            for chunk in chunks
        ]


async def _two_view_order(
    reranker: ScoredReranker,
    query: str,
    candidates: Sequence[KnowledgeChunk],
    limit: int,
    *,
    tenant_id: UUID,
) -> list[int]:
    """Indices of the ``limit`` best ``candidates``, ranked on both views (see
    :data:`_TITLED_VIEW_WEIGHT`). Falls back to the titled order when the
    reranker gives no scores, and skips the body view when no candidate it
    would judge has a title (chunks ingested before titles were stored)."""
    titled = await reranker.rerank_scored(
        query=query,
        documents=[chunk.retrieval_text for chunk in candidates],
        top_k=max(limit, _BODY_VIEW_DEPTH),
        tenant_id=tenant_id,
    )
    top = [index for index, _score in titled]
    bodies = [chunk_retrieval_text(candidates[i].heading_path, candidates[i].content) for i in top]
    titled_score = {index: score for index, score in titled if score is not None}
    if len(titled_score) < len(top) or all(
        body == candidates[i].retrieval_text for i, body in zip(top, bodies, strict=True)
    ):
        return top[:limit]
    body_view = await reranker.rerank_scored(
        query=query, documents=bodies, top_k=len(bodies), tenant_id=tenant_id
    )
    body_score = {top[j]: score for j, score in body_view if score is not None}
    if len(body_score) < len(top):
        return top[:limit]
    combined = {
        i: _TITLED_VIEW_WEIGHT * titled_score[i] + (1 - _TITLED_VIEW_WEIGHT) * body_score[i]
        for i in top
    }
    return sorted(top, key=lambda i: (-combined[i], i))[:limit]


@dataclass(frozen=True)
class KnowledgeSearchTool:
    """The ``knowledge_search`` tool — Stream J.5.

    One instance per agent whose manifest declares a ``knowledge:``
    block; ``knowledge_base_refs`` are the base names it may query. The
    LLM calls it with a natural-language ``query``; the result is a
    formatted block of relevant chunks, each tagged with its source.
    """

    retriever: KnowledgeRetriever
    knowledge_base_refs: tuple[str, ...]

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="knowledge_search",
            description=(
                "Search the tenant's knowledge base for information relevant "
                "to a query. Use it to ground answers in internal documents "
                "rather than guessing."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Natural-language search query.",
                    },
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": _MAX_LIMIT,
                        "description": f"Max results (default {_DEFAULT_LIMIT}).",
                    },
                },
                "required": ["query"],
            },
            # Stream L.L6 — vector retrieval is a pure read; multiple
            # ``knowledge_search`` calls in one batch parallelise.
            is_read_only=True,
        )

    async def call(self, args: Mapping[str, Any], *, ctx: ToolContext) -> ToolResult:
        if ctx.tenant_id is None:
            msg = "knowledge_search requires a tenant binding"
            raise ToolBlockedError(msg)
        query = self._require_query(args)
        limit = _coerce_limit(args.get("limit"))
        results = await self.retriever.search(
            tenant_id=ctx.tenant_id,
            base_names=self.knowledge_base_refs,
            query=query,
            limit=limit,
        )
        if not results:
            return ToolResult(
                content="[no relevant knowledge found for the query]",
                meta={"hits": 0},
            )
        return ToolResult(content=_format_results(results), meta={"hits": len(results)})

    def _require_query(self, args: Mapping[str, Any]) -> str:
        raw = args.get("query")
        if not isinstance(raw, str) or not raw.strip():
            msg = "knowledge_search requires a non-empty 'query' string"
            raise ValueError(msg)
        return raw.strip()


def _parse_rerank_order(text: str, count: int) -> list[int]:
    """Parse an LLM rerank reply into 0-based, in-range, de-duplicated
    indices. An unparseable reply yields ``[]`` (caller falls back)."""
    numbers: list[int] = []
    match = re.search(r"\[[^\]]*\]", text)
    if match is not None:
        try:
            parsed = json.loads(match.group())
            numbers = [int(n) for n in parsed if isinstance(n, int | float)]
        except (ValueError, TypeError):
            numbers = []
    if not numbers:
        numbers = [int(n) for n in re.findall(r"\d+", text)]
    seen: set[int] = set()
    order: list[int] = []
    for one_based in numbers:
        index = one_based - 1
        if 0 <= index < count and index not in seen:
            seen.add(index)
            order.append(index)
    return order


def _recall_source(found_by: set[str]) -> str | None:
    """Collapse the set of recall paths that surfaced a chunk into a label:
    ``"both"`` when vector and keyword agreed, else the single path."""
    if "vector" in found_by and "keyword" in found_by:
        return "both"
    if found_by:
        return next(iter(found_by))
    return None


def _message_text(message: object) -> str:
    content = getattr(message, "content", "")
    return content if isinstance(content, str) else str(content)


def _coerce_limit(raw: object) -> int:
    if not isinstance(raw, int) or isinstance(raw, bool):
        return _DEFAULT_LIMIT
    return max(1, min(raw, _MAX_LIMIT))


def _format_results(results: Sequence[RetrievedChunk]) -> str:
    blocks = [
        f"[{result.filename}#{result.chunk_index}]\n{result.retrieval_text}" for result in results
    ]
    return "\n\n---\n\n".join(blocks)
