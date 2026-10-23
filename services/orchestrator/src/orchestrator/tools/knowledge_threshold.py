"""A knowledge search's rerank-score threshold (B-175 wave 2 PR3; B-206 K-7b).

A base's ``rerank_score_threshold`` drops the reranked candidates scored below it,
applied per base before the cut to the result count; a search whose every
candidate it drops finds nothing. B-206 K-7b binds a stored threshold to the
rerank model it was set under: scores are not comparable across rerank models (a
request-relative score, a domain-calibrated 0-1, a raw logit), so it applies only
while the model that scored is that one
(:func:`~expert_work.protocol.rerank_threshold_paused`); otherwise this search
leaves it out and says so, rather than filter on another model's scale.

A threshold passed with the search (the retrieval test's or the eval's) is being
calibrated now: it applies to every base, whichever model scored.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from uuid import UUID

from expert_work.protocol import KnowledgeBase, KnowledgeChunk, rerank_threshold_paused


@dataclass(frozen=True)
class Floors:
    """Each searched base's threshold for this search (``None`` = none applies),
    and whether a stored one was left out because another model scored."""

    by_base: Mapping[UUID, float | None]
    paused: bool = False


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
    return Floors(by_base, paused=bool(stale))


def clears_floor(
    chunk: KnowledgeChunk, scores: Mapping[UUID, float], floors: Mapping[UUID, float | None]
) -> bool:
    """Whether ``chunk`` survives its base's floor: always when the base has none
    or the reranker gave this chunk no score."""
    floor = floors.get(chunk.kb_id)
    score = scores.get(chunk.id)
    return floor is None or score is None or score >= floor
