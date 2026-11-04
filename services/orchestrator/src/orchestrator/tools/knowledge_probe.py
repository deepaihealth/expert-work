"""B-267 step 3 — the per-turn knowledge probe.

A name, a description or even a generated profile cannot show which people,
titles or codes a knowledge base holds; only searching it can. So at the start
of each turn (platform switch ``kb_first_routing`` on, a base bound, the agent
not opted out with ``knowledge.probe: false``) the platform runs ONE cheap
retrieval per bound base with the user's latest message — keyword and vector
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
import unicodedata
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Final, Literal
from uuid import UUID

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage

from expert_work.common.conversation_channel import is_hidden, message_text
from expert_work.common.observability import expert_work_counter
from expert_work.common.spotlight import unspotlight
from expert_work.persistence.knowledge.text_search import (
    KbKeywordQuery,
    build_kb_keyword_query,
    is_searchable_token,
    kb_index_text,
    tokenize_for_search,
)
from expert_work.protocol import KnowledgeBase, KnowledgeChunk
from orchestrator.graph_builder.platform_context import strip_platform_context
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
#: A message this short is probed together with the previous user message (a
#: follow-up such as "what else did he write" names nobody), cut to
#: :data:`CONTEXT_CHARS` from its end.
FOLLOW_UP_MAX_CHARS: Final = 20
CONTEXT_CHARS: Final = 200

#: Greetings, thanks and acknowledgements (normalised: lower-case, no
#: punctuation or spaces). Platform text, Chinese and English.
SMALL_TALK: Final = frozenset(
    {
        "你好",
        "您好",
        "你好啊",
        "嗨",
        "哈喽",
        "在吗",
        "在不在",
        "谢谢",
        "谢谢你",
        "多谢",
        "感谢",
        "好的",
        "好",
        "嗯",
        "嗯嗯",
        "收到",
        "明白",
        "知道了",
        "ok",
        "okay",
        "再见",
        "拜拜",
        "早上好",
        "晚上好",
        "下午好",
        "你是谁",
        "你叫什么",
        "你能做什么",
        "hi",
        "hello",
        "hey",
        "thanks",
        "thankyou",
        "thx",
        "bye",
        "goodbye",
        "goodmorning",
        "whoareyou",
        "whatcanyoudo",
    }
)

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


def _normalised(text: str) -> str:
    """Lower-case, letters / digits / CJK only (spaces and punctuation gone)."""
    return "".join(ch for ch in unicodedata.normalize("NFKC", text).lower() if ch.isalnum())


def is_small_talk(text: str) -> bool:
    """A greeting, thanks or acknowledgement — or too short to be about anything."""
    flat = _normalised(text)
    return len(flat) < PROBE_MIN_CHARS or flat in SMALL_TALK


def _user_text(message: BaseMessage) -> str:
    """What the user wrote: the platform section and the spotlight fence off."""
    return unspotlight(strip_platform_context(message_text(message.content))).strip()


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
    return _user_text(messages[users[-1]]) if users is not None else None


def turn_query(messages: Sequence[BaseMessage]) -> str | None:
    """The probe's query, or ``None`` when this is not the first step of a
    fresh user turn. A short follow-up carries the previous user message (its
    end) along."""
    users = _fresh_turn(messages)
    if users is None:
        return None
    text = _user_text(messages[users[-1]])
    if len(text) <= FOLLOW_UP_MAX_CHARS and len(users) > 1:
        previous = _user_text(messages[users[-2]])[-CONTEXT_CHARS:]
        if previous:
            return f"{previous}\n{text}"
    return text


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
        query = turn_query(messages) or latest
        # Segmenting loads jieba's dictionary on first use (about a second,
        # blocking): done in a thread, outside the time budget.
        terms = await asyncio.to_thread(build_kb_keyword_query, query)
        try:
            hits = await asyncio.wait_for(
                self._hits(tenant_id, query, terms), timeout=self.timeout_s
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
        self, tenant_id: UUID, query: str, terms: KbKeywordQuery | None
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
            vector = await self.retriever.query_vector(query, tenant_id)
        gate = asyncio.Semaphore(PROBE_CONCURRENCY)

        async def related(base: KnowledgeBase, threshold: float | None) -> int:
            async with gate:
                return await self._related(tenant_id, base, query, terms, vector, threshold)

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
        vector: tuple[float, ...] | None,
        threshold: float | None,
    ) -> int:
        store = self.retriever.store
        related: set[UUID] = set()
        if terms is not None:
            keyword = await store.keyword_search_scored(
                tenant_id=tenant_id, kb_ids=[base.id], query=query, limit=PROBE_TOP_K
            )
            related.update(hit.chunk.id for hit in keyword if keyword_related(terms, hit.chunk))
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


def keyword_related(query: KbKeywordQuery, chunk: KnowledgeChunk) -> bool:
    """Whether ``chunk`` holds an identifier the query names, or at least
    :data:`MIN_SHARED_TERMS` of its content words."""
    tokens = _tokens(chunk.retrieval_text)
    for phrase in query.phrases:
        if _holds_run(tokens, [t for t in phrase.split() if is_searchable_token(t)]):
            return True
    present = set(tokens)
    shared = {
        term
        for term in query.terms
        if len(term) >= MIN_TERM_CHARS and term not in _FUNCTION_WORDS and term in present
    }
    return len(shared) >= MIN_SHARED_TERMS


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
