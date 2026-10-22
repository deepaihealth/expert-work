"""A knowledge search's rerank-score threshold (B-175 wave 2 PR3; B-206 K-7b).

A base's ``rerank_score_threshold`` drops the reranked candidates scored below it,
applied per base before the cut to the result count; a search whose every
candidate it drops finds nothing. B-206 K-7b adds:

* **the model binding** — scores are not comparable across rerank models (a
  request-relative score, a domain-calibrated 0-1, a raw logit), so a stored
  threshold applies only while the model that scored is the one it was set under
  (:func:`~expert_work.protocol.rerank_threshold_paused`); otherwise this search
  leaves it out and says so, rather than filter on another model's scale;
* **a code the query names keeps its passage** — rerank models score a passage
  low when what it shares with the query is a bare product, strain, patent or
  catalogue code, so a stored threshold drops exactly the passage that carries
  it. A candidate under its base's stored threshold is still kept when its text
  holds an identifier the query names
  (:func:`~expert_work.persistence.knowledge.text_search.kb_query_identifiers`,
  any shape), matched as the keyword search matches it: case, width and dash
  look-alikes folded, any joiner or space between its parts. At most
  :data:`CODE_KEEP_MAX` such candidates per search, best scored first: a common
  term a query names (a test or ingredient name) appears verbatim too, and the
  threshold is there to stop answers the documents do not give.

A threshold passed with the search (the retrieval test's or the eval's) is being
calibrated now: it applies to every base, whichever model scored, and keeps no
candidate for a code.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from uuid import UUID

from expert_work.persistence.knowledge.text_search import kb_index_text, kb_query_identifiers
from expert_work.protocol import (
    KnowledgeBase,
    KnowledgeChunk,
    chunk_retrieval_text,
    rerank_threshold_paused,
)

#: The most candidates under a stored threshold one search keeps for a code match.
CODE_KEEP_MAX = 3
#: What may sit between two parts of a code (``ZX-9041``, ``ZX 9041``, ``ZX_9041``).
_CODE_JOINER = r"[\s\-_./]+"


@dataclass(frozen=True)
class Floors:
    """Each searched base's threshold for this search (``None`` = none applies),
    whether a stored one was left out because another model scored (``paused``),
    and whether they are the bases' stored thresholds (``stored``) — only those
    keep a candidate for a code the query names."""

    by_base: Mapping[UUID, float | None]
    paused: bool = False
    stored: bool = False


@dataclass(frozen=True)
class ThresholdCut:
    """The candidates the threshold kept, best first and not yet cut to the
    result count, and which of them were kept only for a code match."""

    chunks: list[KnowledgeChunk]
    code_kept: frozenset[UUID] = field(default_factory=frozenset)


def search_floors(
    bases: Sequence[KnowledgeBase],
    *,
    override: float | None,
    scored: bool,
    scoring_model: str | None,
) -> Floors:
    """The thresholds a search of ``bases`` applies. None when the reranker gave
    no scores (``scored``), and nothing is reported then. ``override`` (a
    threshold passed with the search) for every base, always. Else each base's
    own, unless it was set under another model than ``scoring_model`` (``None``
    when the reranker did not say)."""
    if not scored:
        return Floors(dict.fromkeys(b.id for b in bases))
    if override is not None:
        return Floors({b.id: override for b in bases})
    stale = {b.id for b in bases if rerank_threshold_paused(b, scoring_model)}
    by_base = {b.id: None if b.id in stale else b.rerank_score_threshold for b in bases}
    return Floors(by_base, paused=bool(stale), stored=True)


def apply_threshold(
    chunks: Sequence[KnowledgeChunk],
    scores: Mapping[UUID, float],
    floors: Floors,
    *,
    query: str,
) -> ThresholdCut:
    """``chunks`` (best first) that clear their base's floor, plus — under stored
    thresholds — the best :data:`CODE_KEEP_MAX` of the rest whose text holds a
    code the query names. A chunk with no score or no floor always clears."""
    below = [c for c in chunks if not _clears(c, scores, floors.by_base)]
    if not below:
        return ThresholdCut(list(chunks))
    keep = _holding_a_code(below, scores, query) if floors.stored else frozenset()
    kept = [c for c in chunks if c.id in keep or _clears(c, scores, floors.by_base)]
    return ThresholdCut(kept, keep)


def _clears(
    chunk: KnowledgeChunk, scores: Mapping[UUID, float], floors: Mapping[UUID, float | None]
) -> bool:
    """Whether ``chunk`` survives its base's floor: always when the base has none
    or the reranker gave this chunk no score."""
    floor = floors.get(chunk.kb_id)
    score = scores.get(chunk.id)
    return floor is None or score is None or score >= floor


def _holding_a_code(
    below: Collection[KnowledgeChunk], scores: Mapping[UUID, float], query: str
) -> frozenset[UUID]:
    """The best scored :data:`CODE_KEEP_MAX` of ``below`` whose section path or
    body holds a code ``query`` names (the document title does not count: every
    passage of a file named after a code would match)."""
    patterns = [code_pattern(code) for code in kb_query_identifiers(query)]
    if not patterns:
        return frozenset()
    holding = []
    for chunk in below:
        text = _searchable(chunk)
        if any(pattern.search(text) for pattern in patterns):
            holding.append(chunk)
    holding.sort(key=lambda chunk: -scores[chunk.id])
    return frozenset(chunk.id for chunk in holding[:CODE_KEEP_MAX])


def code_kept_notice(n: int) -> str:
    """What ``knowledge_search`` tells the model about ``n`` results kept only for
    a code match: a fact, so it weighs them knowing they scored low."""
    return (
        f"[knowledge_search: {n} of these passages scored below the knowledge base's "
        "relevance threshold and are included because they contain a code from the query.]"
    )


def code_pattern(code: str) -> re.Pattern[str]:
    """Matches ``code`` in text normalised by :func:`_searchable`: its letter and
    digit runs in order, any joiner or space between them, not inside a longer
    run of letters or digits (``ZX-9041`` is not in ``ZX-90412``)."""
    parts = re.findall(r"[0-9a-z]+", code.lower())
    return re.compile(r"(?<![0-9a-z])" + _CODE_JOINER.join(map(re.escape, parts)) + r"(?![0-9a-z])")


def _searchable(chunk: KnowledgeChunk) -> str:
    """``chunk``'s section path and body as the keyword search indexes them
    (NFKC, dash look-alikes folded, spaced numbers also joined), lower-cased."""
    return kb_index_text(chunk_retrieval_text(chunk.heading_path, chunk.content)).lower()
