"""B-267 step 3 — the per-turn knowledge probe.

A name, a description or even a generated profile cannot show which people,
titles or codes a knowledge base holds; only searching it can. So at the start
of each turn (platform switch ``kb_first_routing`` on, a base bound, the agent
not opted out with ``knowledge.probe: false``) the platform runs ONE cheap
retrieval per bound base with the user's latest message (a short or referring
follow-up carries the previous turn along, ``retrieval_query``) — keyword and vector
recall, no rerank, no model call, a small top-k, all bases at once under a hard
time budget — and, when a base holds related passages, adds one line to this
turn's platform section (B-162: appended to the last user message, never the
cached system prompt)::

    [knowledge probe] Knowledge base K1 has 3 passages related to this message;
    search it with knowledge_search before answering.

It never inserts passages (measured before: that makes fabrication worse) and
no tenant text: handles and counts only. No hit → nothing is added.

**What counts as related** — never a global vector-score constant (vendors and
corpora score differently):

* vector: a passage scoring above the base's own calibration
  (``knowledge_base.profile.calibration``: the 90th percentile of the top score
  generic, content-free queries reach in that base). Only when that
  calibration, the base's vectors and the query embedded now are the same
  model (the platform's current one, ``ToolEnv.embedding_model_key``);
  otherwise — or with no calibration yet — the vector side is not used for
  the base, and no query is embedded for it.
* keyword: among the base's top keyword hits (``keyword_search_scored``,
  ranked by BM25-idf coverage over the base's own passages), a passage that
  holds an identifier the message names (a product / patent / document code),
  or at least :data:`MIN_SHARED_TERMS` distinct content words of the message
  (two characters or more, function words dropped). One shared word is often
  chance ("today", "plan"); two content words in one passage, or an exact
  code, rarely is. It counts words, not scores, so it means the same in every
  base and for every vendor.

Skipped (nothing runs): small talk or a very short message, a turn that is
not a fresh user turn (resumes, approvals, later steps of the turn), and any
failure or overrun of :data:`PROBE_TIMEOUT_S` — silently, with a metric.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Final, Literal
from uuid import UUID

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage

from expert_work.common.conversation_channel import is_hidden
from expert_work.common.observability import expert_work_counter
from expert_work.persistence.knowledge.text_search import (
    KbKeywordQuery,
    build_kb_keyword_query,
    is_searchable_token,
    kb_index_text,
    tokenize_for_search,
)
from expert_work.protocol import KnowledgeBase, KnowledgeChunk
from orchestrator.graph_builder.retrieval_query import (
    SMALL_TALK,
    RetrievalQuery,
    build_retrieval_query,
    log_retrieval_query,
    normalised,
    user_text,
)
from orchestrator.tools.knowledge import KnowledgeRetriever
from orchestrator.tools.knowledge_catalog import MAX_CATALOG_BASES

logger = logging.getLogger(__name__)

#: Passages each path recalls per base.
PROBE_TOP_K: Final = 3
#: Bases probed at most (the catalog lists as many) and at the same time.
PROBE_MAX_BASES: Final = MAX_CATALOG_BASES
PROBE_CONCURRENCY: Final = 4
#: The whole probe (every base, both paths, the query embedding) gets this long.
PROBE_TIMEOUT_S: Final = 1.5
#: The probe reads only the tail of a very long message: segmenting and
#: searching a whole paste would mostly run past the time budget anyway.
PROBE_MAX_QUERY_CHARS: Final = 2000
#: A keyword hit without an identifier needs this many distinct content words
#: of the message in one passage, each at least :data:`MIN_TERM_CHARS` long.
MIN_SHARED_TERMS: Final = 2
MIN_TERM_CHARS: Final = 2
#: English function words (the Chinese ones are already dropped from the query,
#: ``KB_QUERY_STOPWORDS``).
_FUNCTION_WORDS: Final = frozenset(
    {
        "the",
        "and",
        "for",
        "are",
        "was",
        "were",
        "is",
        "am",
        "be",
        "been",
        "of",
        "to",
        "in",
        "on",
        "at",
        "by",
        "an",
        "or",
        "as",
        "it",
        "its",
        "if",
        "do",
        "does",
        "did",
        "what",
        "who",
        "whom",
        "how",
        "why",
        "when",
        "where",
        "which",
        "this",
        "that",
        "these",
        "those",
        "with",
        "you",
        "your",
        "can",
        "could",
        "would",
        "should",
        "will",
        "have",
        "has",
        "had",
        "about",
        "from",
        "into",
        "there",
        "their",
        "they",
        "them",
        "me",
        "my",
        "we",
        "our",
        "he",
        "she",
        "his",
        "her",
        "not",
        "no",
        "yes",
        "any",
        "some",
        "all",
        "please",
        "tell",
        "know",
        "get",
        "give",
        "want",
        "need",
    }
)
#: Messages shorter than this (letters / digits / CJK, after stripping) are not probed.
PROBE_MIN_CHARS: Final = 2


ProbeReason = Literal["hit", "miss", "small_talk", "not_turn_start", "no_bases", "timeout", "error"]

_probes = expert_work_counter(
    "expert_work_knowledge_probe_total",
    "Per-turn knowledge probes by outcome (hit / miss / skip reason / timeout / error).",
    label_names=("outcome",),
)
_follow_ups = expert_work_counter(
    "expert_work_knowledge_probe_follow_up_total",
    "The model's first step after a probe: whether it called knowledge_search.",
    label_names=("probe", "searched"),
)


@dataclass(frozen=True)
class ProbeOutcome:
    """What the probe did: the platform line (``None``: add nothing), why, and
    the related-passage count per handle."""

    reason: ProbeReason
    line: str | None = None
    hits: tuple[tuple[str, int], ...] = ()
    elapsed_ms: int = 0


def is_small_talk(text: str) -> bool:
    """A greeting, thanks or acknowledgement (:data:`SMALL_TALK`) — or too short to
    be about anything."""
    flat = normalised(text)
    return len(flat) < PROBE_MIN_CHARS or flat in SMALL_TALK


def _fresh_turn(messages: Sequence[BaseMessage]) -> list[int] | None:
    """The user messages' positions — ``None`` when this is not the first
    step of a fresh user turn (an answer or a tool call already follows the
    latest user message: a resume, an approval, a later step)."""
    users = [
        index
        for index, message in enumerate(messages)
        if isinstance(message, HumanMessage) and not is_hidden(message)
    ]
    if not users or any(isinstance(m, AIMessage) for m in messages[users[-1] + 1 :]):
        return None
    return users


def latest_user_text(messages: Sequence[BaseMessage]) -> str | None:
    """What the user wrote this turn — ``None`` when not a fresh user turn."""
    users = _fresh_turn(messages)
    return user_text(messages[users[-1]]) if users is not None else None


def _turn_retrieval_query(messages: Sequence[BaseMessage]) -> RetrievalQuery | None:
    if _fresh_turn(messages) is None:
        return None
    return build_retrieval_query(messages)


def turn_query(messages: Sequence[BaseMessage]) -> str | None:
    """The probe's (vector) query, or ``None`` when this is not the first step of
    a fresh user turn: the memory recall's query (:func:`build_retrieval_query`)
    — a short or referring follow-up carries the previous user message and the
    previous answer along, bounded. The keyword side reads the same text with
    the message first (:attr:`RetrievalQuery.keyword_text`)."""
    query = _turn_retrieval_query(messages)
    return query.text if query is not None else None


def probe_line(hits: Sequence[tuple[str, int]]) -> str | None:
    """The one platform line for the bases with related passages (handles and
    counts only — never tenant text)."""
    if not hits:
        return None
    if len(hits) == 1:
        handle, count = hits[0]
        return (
            f"[knowledge probe] Knowledge base {handle} has {count} "
            f"{'passage' if count == 1 else 'passages'} related to this message; search it "
            "with knowledge_search before answering."
        )
    listed = ", ".join(
        f"{handle} ({count} {'passage' if count == 1 else 'passages'})" for handle, count in hits
    )
    return (
        f"[knowledge probe] Knowledge bases {listed} have passages related to this message; "
        "search them with knowledge_search before answering."
    )


def calibrated_threshold(base: KnowledgeBase, live_model: str | None) -> float | None:
    """The base's vector threshold — only when its calibration, its vectors
    and the query embedded now (``live_model``, the platform's current
    ``provider/model``) are all the same model; otherwise the scores do not
    compare and the vector side is not used."""
    record = base.profile
    calibration = record.calibration if record is not None else None
    if calibration is None or not base.embedding_provider or not base.embedding_model:
        return None
    stamped = f"{base.embedding_provider}/{base.embedding_model}"
    if not (calibration.embedding_model == stamped == live_model):
        return None
    return calibration.vector_threshold


@dataclass(frozen=True)
class KnowledgeProbe:
    """The probe of one agent build: its bound bases (manifest order — the
    handle is the position) and the retriever ``knowledge_search`` uses (its
    query embedding is billed the same way)."""

    retriever: KnowledgeRetriever
    refs: tuple[str, ...]
    #: The platform's current embedding model (``ToolEnv.embedding_model_key``);
    #: ``None``: unknown, so keywords only.
    live_model: Callable[[], Awaitable[str | None]] | None = None
    timeout_s: float = PROBE_TIMEOUT_S

    async def run(self, *, tenant_id: UUID, messages: Sequence[BaseMessage]) -> ProbeOutcome:
        """Probe for this turn; never raises (a failure adds nothing)."""
        started = time.monotonic()
        latest = latest_user_text(messages)
        if latest is None:
            # Every later step of a turn lands here: not counted, not logged.
            return ProbeOutcome(reason="not_turn_start")
        if is_small_talk(latest):
            return self._done(ProbeOutcome(reason="small_talk"))
        try:
            built = _turn_retrieval_query(messages)
            if built is not None:
                log_retrieval_query("knowledge_probe", built)
            # The keyword query keeps its first terms only (``KB_QUERY_MAX_TERMS``):
            # the message goes first so the context cannot push its words out.
            keyword_query = (built.keyword_text if built else latest)[-PROBE_MAX_QUERY_CHARS:]
            vector_query = (built.text if built else latest)[-PROBE_MAX_QUERY_CHARS:]
            # Segmenting loads jieba's dictionary on first use (about a second,
            # blocking): done in a thread, outside the time budget.
            terms = await asyncio.to_thread(build_kb_keyword_query, keyword_query)
            # With context carried along, a keyword hit must also share a word of
            # the message itself: the context alone is the previous topic.
            anchor = (
                await asyncio.to_thread(build_kb_keyword_query, built.current)
                if built is not None and built.augmented
                else None
            )
            hits = await asyncio.wait_for(
                self._hits(tenant_id, keyword_query, vector_query, terms, anchor),
                timeout=self.timeout_s,
            )
        except TimeoutError:
            return self._done(ProbeOutcome(reason="timeout", elapsed_ms=_ms(started)))
        except Exception:
            logger.warning("knowledge.probe_failed", exc_info=True)
            return self._done(ProbeOutcome(reason="error", elapsed_ms=_ms(started)))
        if hits is None:
            return self._done(ProbeOutcome(reason="no_bases", elapsed_ms=_ms(started)))
        outcome = ProbeOutcome(
            reason="hit" if hits else "miss",
            line=probe_line(hits),
            hits=tuple(hits),
            elapsed_ms=_ms(started),
        )
        return self._done(outcome)

    @staticmethod
    def _done(outcome: ProbeOutcome) -> ProbeOutcome:
        _probes.labels(outcome=outcome.reason).inc()
        # Counts and timings only — never the message or a passage.
        logger.info(
            "knowledge.probe outcome=%s bases_hit=%d passages=%d elapsed_ms=%d",
            outcome.reason,
            len(outcome.hits),
            sum(count for _handle, count in outcome.hits),
            outcome.elapsed_ms,
        )
        return outcome

    async def _hits(
        self,
        tenant_id: UUID,
        keyword_query: str,
        vector_query: str,
        terms: KbKeywordQuery | None,
        anchor: KbKeywordQuery | None,
    ) -> list[tuple[str, int]] | None:
        """``(handle, related passages)`` of each base with any — ``None``
        when no bound base exists. At most :data:`PROBE_MAX_BASES` bases (the
        catalog's), :data:`PROBE_CONCURRENCY` at a time."""
        by_name = {b.name: b for b in await self.retriever.store.list_bases(tenant_id=tenant_id)}
        bases = [
            (f"K{position}", by_name[ref])
            for position, ref in enumerate(self.refs, 1)
            if ref in by_name
        ][:PROBE_MAX_BASES]
        if not bases:
            return None
        live = await self.live_model() if self.live_model is not None else None
        thresholds = [calibrated_threshold(base, live) for _handle, base in bases]
        vector: tuple[float, ...] | None = None
        if any(threshold is not None for threshold in thresholds):
            vector = await self.retriever.query_vector(vector_query, tenant_id)
        gate = asyncio.Semaphore(PROBE_CONCURRENCY)

        async def related(base: KnowledgeBase, threshold: float | None) -> int:
            async with gate:
                return await self._related(
                    tenant_id, base, keyword_query, terms, anchor, vector, threshold
                )

        counts = await asyncio.gather(
            *(
                related(base, threshold)
                for (_handle, base), threshold in zip(bases, thresholds, strict=True)
            )
        )
        return [
            (handle, count) for (handle, _base), count in zip(bases, counts, strict=True) if count
        ]

    async def _related(
        self,
        tenant_id: UUID,
        base: KnowledgeBase,
        query: str,
        terms: KbKeywordQuery | None,
        anchor: KbKeywordQuery | None,
        vector: tuple[float, ...] | None,
        threshold: float | None,
    ) -> int:
        store = self.retriever.store
        related: set[UUID] = set()
        if terms is not None:
            keyword = await store.keyword_search_scored(
                tenant_id=tenant_id, kb_ids=[base.id], query=query, limit=PROBE_TOP_K
            )
            related.update(
                hit.chunk.id for hit in keyword if keyword_related(terms, hit.chunk, anchor)
            )
        if vector is not None and threshold is not None:
            nearest = await store.search_scored(
                tenant_id=tenant_id, kb_ids=[base.id], query_embedding=vector, limit=PROBE_TOP_K
            )
            related.update(hit.chunk.id for hit in nearest if hit.score > threshold)
        return len(related)


def _tokens(text: str) -> list[str]:
    """``text`` as the keyword index sees it (normalised, segmented)."""
    return [t for t in tokenize_for_search(kb_index_text(text)).split() if is_searchable_token(t)]


def _holds_run(tokens: Sequence[str], run: Sequence[str]) -> bool:
    width = len(run)
    return width > 0 and any(
        list(tokens[i : i + width]) == list(run) for i in range(len(tokens) - width + 1)
    )


def _holds_phrase(tokens: Sequence[str], phrase: str) -> bool:
    return _holds_run(tokens, [t for t in phrase.split() if is_searchable_token(t)])


def _content_terms(query: KbKeywordQuery, present: set[str]) -> set[str]:
    return {
        term
        for term in query.terms
        if len(term) >= MIN_TERM_CHARS and term not in _FUNCTION_WORDS and term in present
    }


def keyword_related(
    query: KbKeywordQuery, chunk: KnowledgeChunk, anchor: KbKeywordQuery | None = None
) -> bool:
    """Whether ``chunk`` holds an identifier the query names, or at least
    :data:`MIN_SHARED_TERMS` of its content words. With an ``anchor`` (the
    user's own message, when the query also carries the previous turn) the
    chunk must besides share one of the anchor's identifiers or content words."""
    tokens = _tokens(chunk.retrieval_text)
    present = set(tokens)
    if anchor is not None and not (
        any(_holds_phrase(tokens, phrase) for phrase in anchor.phrases)
        or _content_terms(anchor, present)
    ):
        return False
    if any(_holds_phrase(tokens, phrase) for phrase in query.phrases):
        return True
    return len(_content_terms(query, present)) >= MIN_SHARED_TERMS


def _ms(started: float) -> int:
    return round((time.monotonic() - started) * 1000)


def record_follow_up(outcome: ProbeOutcome, response: BaseMessage) -> bool:
    """Count whether the model's step after a probe called knowledge_search;
    return it (the caller puts it on the span)."""
    calls = response.tool_calls if isinstance(response, AIMessage) else []
    searched = any(call.get("name") == "knowledge_search" for call in calls)
    probe = "hit" if outcome.reason == "hit" else "miss"
    _follow_ups.labels(probe=probe, searched=str(searched).lower()).inc()
    return searched
