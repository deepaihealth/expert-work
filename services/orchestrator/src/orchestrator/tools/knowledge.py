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

import logging
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal
from uuid import UUID

import httpx

from expert_work.common.search.rrf import weighted_rrf_fuse_scored
from expert_work.persistence import KnowledgeStore
from expert_work.protocol import (
    KnowledgeBase,
    KnowledgeChunk,
    RetrievalMethod,
    chunk_retrieval_text,
)
from orchestrator.tools.error_classifier import GuidedToolError
from orchestrator.tools.knowledge_rerank import (
    RERANK_FAILED,
    RerankError,
    RerankOutcome,
    current_rerank_budget,
    ranking_dropped,
    ranking_outcome,
    rerank_budget,
    weighted_views,
)
from orchestrator.tools.knowledge_rerank import Reranker as Reranker
from orchestrator.tools.knowledge_rerank import RerankRanking as RerankRanking
from orchestrator.tools.knowledge_rerank import ScoredReranker as ScoredReranker
from orchestrator.tools.registry import ToolBlockedError, ToolContext, ToolResult, ToolSpec
from orchestrator.tools.rerank_passage import rerank_passage

if TYPE_CHECKING:
    # Imported under TYPE_CHECKING only — a runtime import of
    # ``orchestrator.llm`` here would cycle (llm → tools.registry → tools).
    from orchestrator.llm import Embedder

logger = logging.getLogger(__name__)

#: Default per-path recall depth = ``limit`` x this, clamped to
#: [``_MIN_RECALL_DEPTH``, ``_MAX_RERANK_CANDIDATES``] — wider than the final
#: top-k so fusion and the reranker have candidates to work with.
_RECALL_DEPTH_FACTOR = 2
_MIN_RECALL_DEPTH = 50
#: The ceiling of the default recall depth (``qwen3-vl-rerank`` takes at most
#: 100 documents per request). A model's own per-request cap is applied by its
#: reranker, from the model catalog (B-206 K-14).
_MAX_RERANK_CANDIDATES = 100
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

#: B-206 K-14 — how a search's candidates are put to the reranker:
#: ``dual`` the two views above (B-175 wave 2 PR2b); ``single`` each candidate
#: once, as one structured text (:func:`~orchestrator.tools.rerank_passage.rerank_passage`:
#: title, section, body); ``single_clean`` the same with the body's Markdown /
#: URLs / table rules / HTML stripped first. Cohere, Onyx and Qwen3-Reranker all
#: judge one string, but on the dev set (main 303 / chat 172 questions,
#: qwen3-vl-rerank, 2026-10-22) single / 100 took main MRR@10 0.945 → 0.938 and
#: the exact-number group (12) 0.917 → 0.699 — the case the dual view was made
#: for — and the instruction / cleaned body did worse still on main. So ``dual``
#: stays the default; the single views are internal options (parent chunks, K-1,
#: may change the picture). A per-base ``rerank_score_threshold`` was calibrated
#: on the dual view's weighted score and keeps its meaning under this default; a
#: single view scores on another scale, so making it the default would need every
#: such threshold recalibrated.
RerankView = Literal["dual", "single", "single_clean"]
RERANK_VIEWS: tuple[RerankView, ...] = ("dual", "single", "single_clean")
DEFAULT_RERANK_VIEW: RerankView = "dual"
#: How many of the fused candidates the reranker judges (K-14: up from the
#: 50-deep recall — the 2026-10-21 replay found every correct document that fell
#: out of the fused top 50 within the top 100). Dev set, dual / 100 against
#: dual / 50: chat chunk hit@10 0.934 → 0.967, MRR@10 0.793 → 0.801, context hit
#: 0.895 → 0.914; main MRR@10 (0.945) and the exact-number group (0.917) unchanged.
DEFAULT_RERANK_POOL = 100
#: The task a single view states to a reranker whose model takes an instruction
#: (English, as the vendors advise; DashScope reads it as ``parameters.instruct``,
#: checked live 2026-10-22). Off by default (``KnowledgeRetriever.rerank_instruction``);
#: the dual view sends none.
RERANK_INSTRUCTION = (
    "Given a question, judge whether the passage content itself states the answer; "
    "the title only identifies the document"
)

#: B-175 wave 2 PR3 — what the model is told about a knowledge result. Results
#: are the closest passages, which are often on topic without stating the fact
#: asked (a question about a product the documents cover, for a detail they never
#: give): a rerank score cannot tell those apart, the model reading them can. The
#: rule is worded on whether a passage *states* the answer, not on whether it is
#: related — "related" is exactly what such passages are. It ships only with this
#: tool, so an agent with no knowledge base never sees it. The rule itself sits in
#: the tool description; the text appended to results states facts rather than
#: commands (B-163: command-voiced out-of-band text trips injection defenses).
KNOWLEDGE_ANSWERABILITY_RULE = (
    "Results are the closest passages, not confirmed answers: rely only on what they "
    "explicitly state, and if none states the specific fact asked, say the knowledge "
    "base does not state it instead of inferring or guessing it."
)
#: B-206 K-6 — how to search, in the tool description only (so, like the rule
#: above, an agent with no knowledge base never sees it). Searching more widely is
#: paired with checking what was read: on find-all questions an agent exploring
#: with more searches found more of the right documents yet answered fewer
#: correctly (Onyx EnterpriseRAG-Bench). Dev-set agent layer (60 questions, the
#: platform answer model) 2026-10-22: fabrications 2 → 1, wrong refusals 1 → 0
#: with only this wording changed; the K-4 grouped result format measured on its
#: own raised fabrications (2 → 7) and is held back.
KNOWLEDGE_SEARCH_GUIDANCE = (
    "Use several small, targeted searches rather than one broad one; if a result does not "
    "state the fact asked, search again in the documents' own terms, and do not repeat the "
    "same or a very similar query. For questions that ask for all / every / how many, keep "
    "searching with different terms until a search returns no document you have not seen, "
    "then check each candidate against every condition in the question before counting it. "
    "Before concluding, verify each condition in the question against text you have actually "
    "read; conflicting documents are reported as conflicting, not resolved by guessing. Say "
    "you searched and found nothing only when a search actually ran."
)
_RESULTS_NOTICE = (
    "[knowledge_search: these are the closest passages found, which is not the same as "
    "passages that answer the question. What none of them explicitly states is not "
    "supported by the knowledge base, so an answer that infers or guesses it is not "
    "grounded in it; the related information they do contain can still be mentioned. "
    "Documents may word a fact differently from the query, so a search in their terms "
    "can find what this one missed.]"
)
_NO_HITS_NOTICE = (
    "[knowledge_search: documents may word a fact differently from the query, so a "
    "search in their terms can find what this one missed; if that finds nothing either, "
    "the knowledge base does not cover this.]"
)
_NO_BASES_ADVICE = (
    "None of the knowledge bases this agent is configured with exists, so nothing was "
    "searched: this says nothing about whether the information exists, and the same "
    "search fails the same way until the agent's knowledge base setting is fixed."
)

#: B-206 K-8 —— what a failed query embedding raises when the vendor, not the
#: platform, failed: ``HTTPEmbeddingClient`` raises ``httpx.HTTPStatusError`` for
#: any error status (4xx / 5xx, the vendor's body in the message) and httpx's
#: transport errors (timeouts, refused / reset connections) — all ``httpx.HTTPError``;
#: ``OSError`` (``TimeoutError`` and ``ConnectionError`` among it) covers an
#: embedder that times out or loses its socket outside httpx.
_EMBEDDING_VENDOR_FAILURES = (httpx.HTTPError, OSError)

#: How a search ended — the tool words its result per outcome (B-175 wave 2 PR3):
#: ``ok`` hits returned; ``no_bases`` none of the named bases exists; ``no_hits``
#: recall found nothing; ``below_threshold`` every hit fell under a base's
#: rerank-score threshold.
SearchStatus = Literal["ok", "no_bases", "no_hits", "below_threshold"]


#: Longest document handed to a reranker. DashScope refuses the whole request
#: (HTTP 400, it does not truncate) when any one document is over the model's
#: per-document limit — 4,000 tokens for qwen3-rerank, 8,000 for
#: qwen3-vl-rerank — so one over-long candidate cost a search its rerank
#: (B-198). Every caller cuts with :func:`rerank_documents`, whichever
#: reranker it holds (B-200); a normal chunk is far shorter.
RERANK_DOC_MAX_CHARS = 3000


def rerank_documents(texts: Iterable[str]) -> list[str]:
    """``texts`` each cut to :data:`RERANK_DOC_MAX_CHARS` — what a caller
    hands any reranker."""
    return [text[:RERANK_DOC_MAX_CHARS] for text in texts]


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
    ``rerank_score`` is the score the reranker ranked it by — the two-view
    weighted score when both views ran — and ``None`` when rerank was off,
    failed, or ranked without scores.

    ``content`` is the chunk body; ``heading_path`` its section breadcrumb.
    The agent sees (and the eval matches against) :attr:`retrieval_text`, the
    two joined, under a ``[filename#index]`` header. The reranker judges a
    text built from the stored chunk (see :data:`RerankView`), which also
    carries the document title — here it would only repeat the header.
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
    rerank_score: float | None = None

    @property
    def retrieval_text(self) -> str:
        return chunk_retrieval_text(self.heading_path, self.content)


@dataclass(frozen=True)
class KnowledgeSearchOutcome:
    """One search's results and how it ended (B-175 wave 2 PR3).

    ``candidates`` is how many ranked passages the rerank-score threshold was
    applied to — the reranker's output, not the recall depth (``0`` for
    ``no_bases`` / ``no_hits``). ``rerank_degraded`` is set
    when the reranker failed or answered with nothing and the fused order was
    used instead — then no threshold applied either. ``rerank_outcome`` (B-206
    K-7) says how the rerank ended (:data:`RerankOutcome`; ``None`` when none was
    asked for or there was nothing to rerank): ``rerank_degraded`` is exactly its
    failure values, ``rate_limited`` / ``model_error``. ``embedding_degraded``
    (B-206 K-8) is set when the query could not be embedded and every base was
    searched by keywords alone.
    ``rerank_dropped`` (B-206 K-14) counts candidates the reranker was handed
    but never judged (its model's per-request cap, or a size refusal it retried
    with fewer): the rerank ran, over part of the pool.
    """

    results: tuple[RetrievedChunk, ...]
    status: SearchStatus
    candidates: int = 0
    rerank_degraded: bool = False
    embedding_degraded: bool = False
    rerank_dropped: int = 0
    rerank_outcome: RerankOutcome | None = None


@dataclass(frozen=True)
class _SubScores:
    """Per-chunk scores from one search, keyed by chunk id."""

    vector: Mapping[UUID, float]
    keyword: Mapping[UUID, float]
    fused: Mapping[UUID, float]
    rerank: Mapping[UUID, float]


@dataclass(frozen=True)
class _Reranked:
    """The rerank stage's output: chunks best first — at least ``limit`` of them
    when there are that many, more when the reranker scored more, so a
    threshold that drops some can still fill ``limit`` — each one's rerank score
    when the reranker gave one, how many candidates it never judged, and how
    the rerank ended (``None`` when it was not asked for)."""

    chunks: list[KnowledgeChunk]
    scores: Mapping[UUID, float]
    dropped: int = 0
    outcome: RerankOutcome | None = None

    @property
    def degraded(self) -> bool:
        return self.outcome in RERANK_FAILED


#: B-206 K-7a —— outcomes that are a deployment's configuration, not a failure.
_CONFIGURATION_STATES: frozenset[RerankOutcome | None] = frozenset(
    {"no_model", "model_unavailable", "model_unsupported"}
)


def default_recall_depth(limit: int) -> int:
    """Production per-path recall depth for a ``limit``-result search:
    ``2 x limit`` clamped to [50, 100] (100 = the reranker's per-request cap)."""
    return max(_MIN_RECALL_DEPTH, min(_RECALL_DEPTH_FACTOR * limit, _MAX_RERANK_CANDIDATES))


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

    B-206 K-14 — internal knobs, not tenant settings (the eval sets them for
    A/B arms): ``rerank_view`` how candidates are put to the reranker
    (:data:`RerankView`; default ``dual``, under which a base's
    ``rerank_score_threshold`` keeps its calibrated meaning), ``rerank_pool`` how
    many of the fused candidates it judges, ``rerank_instruction`` whether a
    single view sends :data:`RERANK_INSTRUCTION` — refused with ``dual``, which
    sends none, so no caller believes it instructed a rerank that was not.
    """

    store: KnowledgeStore
    embedder: Embedder
    reranker: Reranker | None = None
    recall_limit: int | None = None
    vector_weight: float = 1.0
    keyword_weight: float = 1.0
    rerank_view: RerankView = DEFAULT_RERANK_VIEW
    rerank_pool: int = DEFAULT_RERANK_POOL
    rerank_instruction: bool = False

    def __post_init__(self) -> None:
        if self.rerank_instruction and self.rerank_view == "dual":
            msg = "rerank_instruction needs a single view: the dual view sends no instruction"
            raise ValueError(msg)

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
        rerank_score_threshold: float | None = None,
    ) -> list[RetrievedChunk]:
        """Hybrid-search ``base_names`` for ``query``, returning the
        ``limit`` most relevant chunks with source attribution + scores —
        :meth:`search_detailed` without the outcome."""
        outcome = await self.search_detailed(
            tenant_id=tenant_id,
            base_names=base_names,
            query=query,
            limit=limit,
            method=method,
            score_threshold=score_threshold,
            rerank=rerank,
            rerank_score_threshold=rerank_score_threshold,
        )
        return list(outcome.results)

    async def search_detailed(
        self,
        *,
        tenant_id: UUID,
        base_names: Sequence[str],
        query: str,
        limit: int,
        method: RetrievalMethod | None = None,
        score_threshold: float | None = None,
        rerank: bool | None = None,
        rerank_score_threshold: float | None = None,
    ) -> KnowledgeSearchOutcome:
        """Hybrid-search ``base_names`` for ``query``: the ``limit`` most
        relevant chunks with source attribution + scores, and how it ended.

        ``method`` / ``score_threshold`` / ``rerank`` / ``rerank_score_threshold``
        are *overrides*: when ``None`` each spanned base's own configured default
        is used. The recall method, similarity threshold and rerank-score
        threshold are applied **per base** (a query may span bases with
        different configs); ``rerank`` is resolved query-wide (override > "any
        spanned base enables it"). Base names that do not resolve to a
        knowledge base are skipped; when none resolves the outcome is
        ``no_bases``. The rerank-score threshold only applies to chunks the
        reranker scored.
        """
        bases = await self._resolve_bases(tenant_id, base_names)
        if not bases:
            return KnowledgeSearchOutcome(results=(), status="no_bases")
        query_embedding = await self._embed_query(query, tenant_id)
        degraded = query_embedding is None
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
            if query_embedding is None:
                # K-8 —— no query vector: keywords for every base, a vector-only one
                # included — some results beat none, and the outcome says so.
                eff_method = RetrievalMethod.KEYWORD
            eff_threshold = (
                score_threshold if score_threshold is not None else base.retrieval_score_threshold
            )
            if query_embedding is not None and eff_method in (
                RetrievalMethod.HYBRID,
                RetrievalMethod.VECTOR,
            ):
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
            return KnowledgeSearchOutcome(results=(), status="no_hits", embedding_degraded=degraded)
        fused = [chunk_by_id[chunk_id] for chunk_id, _score in fused_scored]
        rerank_enabled = rerank if rerank is not None else any(b.rerank_enabled for b in bases)
        ranked = await self._rerank(
            query, fused, limit, tenant_id=tenant_id, enabled=rerank_enabled
        )
        floor = {
            b.id: (
                rerank_score_threshold
                if rerank_score_threshold is not None
                else b.rerank_score_threshold
            )
            for b in bases
        }
        # Filtered before the cut to ``limit``: with several bases, one base's
        # floor dropping the top hits must leave room for another base's.
        kept = [c for c in ranked.chunks if _clears_floor(c, ranked.scores, floor)][:limit]
        if not kept:
            return KnowledgeSearchOutcome(
                results=(),
                status="below_threshold",
                candidates=len(ranked.chunks),
                rerank_degraded=ranked.degraded,
                embedding_degraded=degraded,
                rerank_dropped=ranked.dropped,
                rerank_outcome=ranked.outcome,
            )
        scores = _SubScores(
            vector=vector_score,
            keyword=keyword_score,
            fused=dict(fused_scored),
            rerank=ranked.scores,
        )
        results = await self._attribute(tenant_id, kept, scores, sources)
        return KnowledgeSearchOutcome(
            results=tuple(results),
            status="ok",
            candidates=len(ranked.chunks),
            rerank_degraded=ranked.degraded,
            embedding_degraded=degraded,
            rerank_dropped=ranked.dropped,
            rerank_outcome=ranked.outcome,
        )

    async def _embed_query(self, query: str, tenant_id: UUID) -> tuple[float, ...] | None:
        """The query's vector — ``None`` when the embedding vendor failed (B-206
        K-8): the search then runs on keywords alone and says so, logged here
        and flagged ``embedding_degraded``, instead of failing outright.

        Only :data:`_EMBEDDING_VENDOR_FAILURES` degrade. Anything else — the
        deployment has no embedding configured, a bug, a body that is not an
        embeddings response — propagates as before K-8: those fail every search
        the same way, and keyword results flagged "degraded" would hide them.

        K-5 —— an embedder with ``embed_query`` (the platform's) adds the model's
        query-side marker; long-term memory recall calls ``embed`` and gets none.
        """
        embed_query = getattr(self.embedder, "embed_query", None)
        try:
            if embed_query is not None:
                vector: tuple[float, ...] = await embed_query(query, tenant_id=tenant_id)
                return vector
            return (await self.embedder.embed([query], tenant_id=tenant_id))[0]
        except _EMBEDDING_VENDOR_FAILURES:
            logger.warning(
                "knowledge.embedding_degraded — query embedding failed, searching by keyword only",
                exc_info=True,
            )
            return None

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
    ) -> _Reranked:
        if self.reranker is None or not enabled:
            return _Reranked(
                chunks=fused[:limit], scores={}, outcome="no_model" if enabled else None
            )
        # Cut after fusion: several bases (and an explicit recall_limit) can fuse
        # into more than the pool; a reranker whose model takes fewer per request
        # keeps the leading ones.
        candidates = fused[: self.rerank_pool]
        # K-7 —— both views' requests share one time budget (queueing + retries).
        with rerank_budget() as budget:
            try:
                ranked = await self._order(
                    self.reranker, query, candidates, limit, tenant_id=tenant_id
                )
            except Exception as exc:
                # Rerank is an optional quality pass (B-175 wave 2 PR3): a vendor
                # error used to surface as a tool error / console 500.
                failed: RerankOutcome = (
                    exc.outcome if isinstance(exc, RerankError) else "model_error"
                )
                logger.warning(
                    "knowledge.rerank_failed outcome=%s attempts=%d — using the fused order",
                    failed,
                    budget.attempts,
                    exc_info=True,
                )
                return _Reranked(chunks=fused[:limit], scores={}, outcome=failed)
        if not ranked:
            # An empty answer for a non-empty candidate list is a rerank fault,
            # not "nothing relevant" — that is the threshold's call.
            logger.warning(
                "knowledge.rerank_empty outcome=model_error attempts=%d — using the fused order",
                budget.attempts,
            )
            return _Reranked(chunks=fused[:limit], scores={}, outcome="model_error")
        outcome = ranking_outcome(ranked)
        if outcome not in ("ok", "partial"):  # ``partial`` was logged by the dual view
            # The reranker had nothing to call, or could not read its answer: the
            # input order came back, which is the fused order.
            logger.log(
                logging.INFO if outcome in _CONFIGURATION_STATES else logging.WARNING,
                "knowledge.rerank_fallback outcome=%s attempts=%d — using the fused order",
                outcome,
                budget.attempts,
            )
        return _Reranked(
            chunks=[candidates[i] for i, _score in ranked],
            scores={candidates[i].id: score for i, score in ranked if score is not None},
            dropped=ranking_dropped(ranked),
            outcome=outcome,
        )

    async def _order(
        self,
        reranker: Reranker,
        query: str,
        candidates: Sequence[KnowledgeChunk],
        limit: int,
        *,
        tenant_id: UUID,
    ) -> list[tuple[int, float | None]]:
        """``candidates`` best first as ``(index, score)``, judged per
        :attr:`rerank_view`; a reranker that cannot score ranks the best ``limit``."""
        if self.rerank_view == "dual":
            if isinstance(reranker, ScoredReranker):
                return await _two_view_order(
                    reranker, query, candidates, limit, tenant_id=tenant_id
                )
            texts = [chunk.retrieval_text for chunk in candidates]
        else:
            clean = self.rerank_view == "single_clean"
            texts = [rerank_passage(chunk, clean=clean) for chunk in candidates]
        documents = rerank_documents(texts)
        if isinstance(reranker, ScoredReranker):
            # Every candidate is ranked, so a threshold can still fill ``limit``.
            extra: dict[str, Any] = (
                {"instruction": RERANK_INSTRUCTION} if self.rerank_instruction else {}
            )
            return await reranker.rerank_scored(
                query=query,
                documents=documents,
                top_k=len(documents),
                tenant_id=tenant_id,
                **extra,
            )
        order = await reranker.rerank(
            query=query, documents=documents, top_k=limit, tenant_id=tenant_id
        )
        return [(index, None) for index in order]

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
                rerank_score=scores.rerank.get(chunk.id),
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
) -> list[tuple[int, float | None]]:
    """The best ``candidates`` as ``(index, score)``, ranked on both views (see
    :data:`_TITLED_VIEW_WEIGHT`) — every one the titled view returned, at least
    ``limit``, so the caller can apply a threshold before cutting to ``limit``;
    the score is the weighted one they were ranked by. Uses the titled order and
    its scores (``None`` when it gave none) when the titled view is not fully
    scored, or when no candidate it would judge has a title (chunks ingested
    before titles were stored — both views are then the same text, so the
    titled score is on the weighted scale). When the body view fails or comes
    back without scores, the titled order is kept with no scores — titled-only
    scores run higher than the weighted ones a threshold is calibrated on — and
    marked ``partial`` (B-206 K-7a: the titled view's paid-for ranking is not
    thrown away for the fused order)."""
    titled = await reranker.rerank_scored(
        query=query,
        documents=rerank_documents(chunk.retrieval_text for chunk in candidates),
        top_k=max(limit, _BODY_VIEW_DEPTH),
        tenant_id=tenant_id,
    )
    top = [index for index, _score in titled]
    bodies = [chunk_retrieval_text(candidates[i].heading_path, candidates[i].content) for i in top]
    titled_score = {index: score for index, score in titled if score is not None}
    if len(titled_score) < len(top) or all(
        body == candidates[i].retrieval_text for i, body in zip(top, bodies, strict=True)
    ):
        return RerankRanking(
            titled, dropped=ranking_dropped(titled), outcome=ranking_outcome(titled)
        )
    try:
        body_view = await reranker.rerank_scored(
            query=query,
            documents=rerank_documents(bodies),
            top_k=len(bodies),
            tenant_id=tenant_id,
        )
    except RerankError as exc:
        return _titled_only(top, ranking_dropped(titled), f"failed ({exc})")
    dropped = ranking_dropped(titled) + ranking_dropped(body_view)
    body_score = {top[j]: score for j, score in body_view if score is not None}
    if len(body_score) < len(top):
        return _titled_only(top, dropped, "answered without scores")
    return weighted_views(top, titled, body_view, _TITLED_VIEW_WEIGHT, dropped=dropped)


def _titled_only(top: Sequence[int], dropped: int, why: str) -> RerankRanking:
    """The titled view's order, unscored and ``partial``: the body view
    ``why``. Logged here, once per search, with the search's attempts."""
    budget = current_rerank_budget()
    logger.warning(
        "knowledge.rerank_partial outcome=partial attempts=%d — the body view %s; "
        "keeping the titled view's order",
        budget.attempts if budget is not None else 0,
        why,
    )
    return RerankRanking(((i, None) for i in top), dropped=dropped, outcome="partial")


def _clears_floor(
    chunk: KnowledgeChunk, scores: Mapping[UUID, float], floor: Mapping[UUID, float | None]
) -> bool:
    """Whether ``chunk`` survives its base's rerank-score threshold: always when
    the base sets none or the reranker gave this chunk no score."""
    threshold = floor.get(chunk.kb_id)
    score = scores.get(chunk.id)
    return threshold is None or score is None or score >= threshold


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
                f"rather than guessing. {KNOWLEDGE_ANSWERABILITY_RULE} {KNOWLEDGE_SEARCH_GUIDANCE}"
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
        # A failed search propagates as-is: the classifier keeps a timeout or
        # outage ``transient``, and its advice already says a failure is no
        # evidence about the target.
        outcome = await self.retriever.search_detailed(
            tenant_id=ctx.tenant_id,
            base_names=self.knowledge_base_refs,
            query=query,
            limit=limit,
        )
        if outcome.status == "no_bases":
            msg = "none of this agent's knowledge bases exists: " + ", ".join(
                self.knowledge_base_refs
            )
            raise GuidedToolError(msg, advice=_NO_BASES_ADVICE)
        meta: dict[str, Any] = {"hits": len(outcome.results), "status": outcome.status}
        if outcome.rerank_degraded:
            meta["rerank_degraded"] = True
        if outcome.embedding_degraded:
            meta["embedding_degraded"] = True
        if outcome.rerank_dropped:
            meta["rerank_dropped"] = outcome.rerank_dropped
        if outcome.rerank_outcome not in (None, "ok"):
            meta["rerank_outcome"] = outcome.rerank_outcome
        if outcome.status == "below_threshold":
            return ToolResult(
                content=(
                    "[no relevant knowledge found for the query: "
                    f"{outcome.candidates} passages were retrieved but none was relevant "
                    "enough]"
                ),
                meta=meta,
                notice=_NO_HITS_NOTICE,
            )
        if not outcome.results:
            return ToolResult(
                content="[no relevant knowledge found for the query]",
                meta=meta,
                notice=_NO_HITS_NOTICE,
            )
        return ToolResult(
            content=_format_results(outcome.results), meta=meta, notice=_RESULTS_NOTICE
        )

    def _require_query(self, args: Mapping[str, Any]) -> str:
        raw = args.get("query")
        if not isinstance(raw, str) or not raw.strip():
            msg = "knowledge_search requires a non-empty 'query' string"
            raise ValueError(msg)
        return raw.strip()


def _recall_source(found_by: set[str]) -> str | None:
    """Collapse the set of recall paths that surfaced a chunk into a label:
    ``"both"`` when vector and keyword agreed, else the single path."""
    if "vector" in found_by and "keyword" in found_by:
        return "both"
    if found_by:
        return next(iter(found_by))
    return None


def _coerce_limit(raw: object) -> int:
    if not isinstance(raw, int) or isinstance(raw, bool):
        return _DEFAULT_LIMIT
    return max(1, min(raw, _MAX_LIMIT))


def _format_results(results: Sequence[RetrievedChunk]) -> str:
    blocks = [
        f"[{result.filename}#{result.chunk_index}]\n{result.retrieval_text}" for result in results
    ]
    return "\n\n---\n\n".join(blocks)
