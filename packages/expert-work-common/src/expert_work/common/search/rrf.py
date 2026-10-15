"""Reciprocal Rank Fusion — Mini-ADR U-6.

Pure algorithm extracted from ``orchestrator/tools/knowledge.py`` so it
can be shared between J.5 RAG hybrid retrieval and the Sprint #6 memory
hybrid recall path.

Reciprocal Rank Fusion combines several ranked lists by scoring each
item with ``Σ 1/(k + rank)`` across the lists it appears in. An item
ranked well by every list rises to the top; ``k`` (default 60, the
canonical value from Cormack/Clarke/Buettcher 2009) dampens the weight
of the very top ranks so a single list cannot dominate the fusion.

The function is generic over the item type — any hashable type works
(``MemoryItem`` / ``KnowledgeChunk`` / ``UUID`` / dataclasses with
``frozen=True``). Callers keep ownership of comparison semantics.
"""

from __future__ import annotations

import math
from collections.abc import Hashable, Sequence

#: Canonical RRF dampener — Cormack/Clarke/Buettcher 2009 baseline.
#: expert_work locks this for both J.5 and Sprint #6 so the algorithm behavior
#: is comparable across subsystems. Per-recall-path tuning is M1.
DEFAULT_K = 60


def rrf_fuse_scored[T: Hashable](
    rankings: Sequence[Sequence[T]], *, k: int = DEFAULT_K
) -> list[tuple[T, float]]:
    """Fuse several ranked lists by RRF, returning ``(item, score)`` pairs.

    An item's fused score is the sum over all input lists of
    ``1 / (k + rank)`` (rank is 0-indexed). Items appearing high in
    multiple lists rise to the top. Items appearing in only one list
    are still kept, ranked by their position in that list.

    The scored variant (Stream CM-6) exists so callers can re-weight the
    fusion — e.g. the memory store's temporal decay — before cutting to
    their final limit. :func:`rrf_fuse` stays the plain-items facade.

    ``k`` should match across callers that compare fused outputs;
    defaults to :data:`DEFAULT_K`.

    Returns ``(item, score)`` pairs most-relevant first. An empty input
    returns ``[]``. Duplicate items inside one input list are collapsed
    to the first occurrence (the higher rank).
    """
    if not rankings:
        return []
    scores: dict[T, float] = {}
    for ranking in rankings:
        seen: set[T] = set()
        for rank, item in enumerate(ranking):
            if item in seen:
                continue
            seen.add(item)
            scores[item] = scores.get(item, 0.0) + 1.0 / (k + rank + 1)
    return sorted(scores.items(), key=lambda pair: pair[1], reverse=True)


def weighted_rrf_fuse_scored[T: Hashable](
    rankings: Sequence[tuple[float, Sequence[T]]], *, k: int = DEFAULT_K
) -> list[tuple[T, float]]:
    """Weighted RRF: like :func:`rrf_fuse_scored`, but each input list
    carries a weight and contributes ``weight / (k + rank)`` per item (rank
    counted from 1).

    Knowledge retrieval only (B-175 wave 2) — it lets the vector and
    keyword paths count differently. With every weight ``1.0`` the result
    is identical to :func:`rrf_fuse_scored` (same items, scores and tie
    order), which long-term memory keeps using unchanged.

    ``rankings`` is a sequence of ``(weight, ranked_list)``; weights must
    be finite and non-negative. Duplicates inside one list collapse to the first
    occurrence, as in :func:`rrf_fuse_scored`.
    """
    scores: dict[T, float] = {}
    for weight, ranking in rankings:
        if not math.isfinite(weight) or weight < 0:
            msg = f"RRF list weight must be finite and non-negative, got {weight}"
            raise ValueError(msg)
        seen: set[T] = set()
        for rank, item in enumerate(ranking):
            if item in seen:
                continue
            seen.add(item)
            scores[item] = scores.get(item, 0.0) + weight / (k + rank + 1)
    return sorted(scores.items(), key=lambda pair: pair[1], reverse=True)


def rrf_fuse[T: Hashable](rankings: Sequence[Sequence[T]], *, k: int = DEFAULT_K) -> list[T]:
    """Fuse several ranked lists by Reciprocal Rank Fusion.

    Thin facade over :func:`rrf_fuse_scored` for callers that only need
    the fused order (J.5 knowledge retrieval); see there for semantics.
    """
    return [item for item, _score in rrf_fuse_scored(rankings, k=k)]


__all__ = ["DEFAULT_K", "rrf_fuse", "rrf_fuse_scored", "weighted_rrf_fuse_scored"]
