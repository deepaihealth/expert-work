"""Knowledge retrieval — Stream J.5 RAG.

The ``knowledge_search`` tool an agent calls to ground its answers in
the tenant's uploaded documents. Retrieval is **hybrid**:

1. **vector recall** — the query is embedded and matched by cosine
   similarity against ``knowledge_chunk`` embeddings;
2. **keyword recall** — the query is matched by Postgres full-text
   relevance (jieba-segmented, so it is correct for CJK);
3. the two ranked lists are fused by **Reciprocal Rank Fusion**; and
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
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable
from uuid import UUID

from langchain_core.messages import HumanMessage, SystemMessage

from expert_work.common.observability import ExpertWorkComponent, expert_work_span
from expert_work.common.search.rrf import rrf_fuse_scored
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

#: Per-side recall depth fetched before fusion / rerank — wider than the
#: final top-k so fusion and the reranker have candidates to work with.
_DEFAULT_RECALL_LIMIT = 20
#: Each candidate is truncated to this many characters in the rerank
#: prompt — enough to judge relevance without an enormous prompt.
_RERANK_DOC_CHARS = 600
#: ``knowledge_search`` result-count default + hard cap.
_DEFAULT_LIMIT = 5
_MAX_LIMIT = 20


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


@dataclass(frozen=True)
class RetrievedChunk:
    """A retrieved chunk with its source document attribution.

    ``score`` is the vector cosine similarity in [0, 1] when the chunk was
    surfaced by vector recall (``None`` for keyword-only hits, whose
    ``ts_rank`` is not a comparable similarity). ``recall_source`` records
    which recall path(s) found it. Both default to ``None`` so the tool's
    formatted output is unaffected — they exist for the retrieval-test
    endpoint's transparency.

    ``content`` is the chunk body; ``heading_path`` its section breadcrumb.
    The agent sees (and the reranker / eval match against)
    :attr:`retrieval_text`, the two joined.
    """

    content: str
    filename: str
    chunk_index: int
    score: float | None = None
    recall_source: str | None = None
    heading_path: tuple[str, ...] = ()

    @property
    def retrieval_text(self) -> str:
        return chunk_retrieval_text(self.heading_path, self.content)


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
    """

    store: KnowledgeStore
    embedder: Embedder
    reranker: Reranker | None = None
    recall_limit: int = field(default=_DEFAULT_RECALL_LIMIT)

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

        # Per-base recall, honouring each base's method + threshold. Vector
        # similarity (the only [0, 1] score) is recorded per chunk; keyword
        # hits only contribute to fusion + recall_source.
        rankings: list[list[KnowledgeChunk]] = []
        vector_score: dict[UUID, float] = {}
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
                    limit=self.recall_limit,
                )
                if eff_threshold is not None:
                    vector = [hit for hit in vector if hit.score >= eff_threshold]
                rankings.append([hit.chunk for hit in vector])
                for hit in vector:
                    vector_score[hit.chunk.id] = max(vector_score.get(hit.chunk.id, 0.0), hit.score)
                    sources.setdefault(hit.chunk.id, set()).add("vector")
            if eff_method in (RetrievalMethod.HYBRID, RetrievalMethod.KEYWORD):
                keyword = await self.store.keyword_search_scored(
                    tenant_id=tenant_id,
                    kb_ids=[base.id],
                    query=query,
                    limit=self.recall_limit,
                )
                rankings.append([hit.chunk for hit in keyword])
                for hit in keyword:
                    sources.setdefault(hit.chunk.id, set()).add("keyword")

        fused = [chunk for chunk, _score in rrf_fuse_scored(rankings)]
        if not fused:
            return []
        rerank_enabled = rerank if rerank is not None else any(b.rerank_enabled for b in bases)
        chunks = await self._rerank(
            query, fused, limit, tenant_id=tenant_id, enabled=rerank_enabled
        )
        return await self._attribute(tenant_id, chunks, vector_score, sources)

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
        candidates = fused[: self.recall_limit]
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
        vector_score: Mapping[UUID, float],
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
                score=vector_score.get(chunk.id),
                recall_source=_recall_source(sources.get(chunk.id, set())),
                heading_path=chunk.heading_path,
            )
            for chunk in chunks
        ]


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
