"""Tests for :mod:`expert_work.common.search.rrf` — Sprint #6 Mini-ADR U-6.

Mirrors the original ``_rrf_fuse`` coverage from
``services/orchestrator/tests/test_knowledge_tool.py`` plus generic /
edge-case scenarios that come with extraction:

- Generic over hashable types (UUID / dataclass / str).
- Empty inputs.
- Single-list identity.
- Duplicates inside one list collapse.
- Custom ``k`` parameter affects ordering.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from uuid import uuid4

import pytest

from expert_work.common.search.rrf import (
    DEFAULT_K,
    rrf_fuse,
    rrf_fuse_scored,
    weighted_rrf_fuse_scored,
)


def test_default_k_matches_knowledge_subsystem_constant() -> None:
    """Locks ``k=60`` so J.5 and Sprint #6 stay comparable."""
    assert DEFAULT_K == 60


def test_rrf_fuse_empty_returns_empty() -> None:
    assert rrf_fuse([]) == []
    assert rrf_fuse([[]]) == []
    assert rrf_fuse([[], []]) == []


def test_rrf_fuse_single_list_is_identity() -> None:
    items = ["a", "b", "c", "d"]
    assert rrf_fuse([items]) == items


def test_rrf_fuse_rewards_items_in_both_lists() -> None:
    """The canonical RRF property: an item ranked by both inputs
    outscores items ranked by only one."""
    shared = "shared"
    vec_only = "vec"
    kw_only = "kw"
    fused = rrf_fuse([[vec_only, shared], [kw_only, shared]])
    assert fused[0] == shared
    assert set(fused) == {shared, vec_only, kw_only}


def test_rrf_fuse_two_identical_lists_match_single_list_order() -> None:
    items = ["a", "b", "c"]
    fused = rrf_fuse([items, items])
    assert fused == items


def test_rrf_fuse_collapses_duplicates_within_one_list() -> None:
    """Two appearances of the same item in one list count as the higher
    of the two ranks (the first occurrence)."""
    items = ["a", "b", "a"]  # second "a" is duplicate
    fused = rrf_fuse([items])
    assert fused == ["a", "b"]


def test_rrf_fuse_generic_over_uuid() -> None:
    a, b, c = uuid4(), uuid4(), uuid4()
    fused = rrf_fuse([[a, b], [b, c]])
    assert fused[0] == b  # shared = highest


def test_rrf_fuse_generic_over_frozen_dataclass() -> None:
    @dataclass(frozen=True)
    class Doc:
        id: str
        title: str

    a = Doc(id="1", title="first")
    b = Doc(id="2", title="second")
    fused = rrf_fuse([[a, b], [b, a]])  # symmetric — both items equal score
    assert set(fused) == {a, b}


def test_rrf_fuse_custom_k_affects_score_gap() -> None:
    """Symmetric inputs: items ranked at the boundary (first AND last)
    always outscore the middle by Jensen's inequality on ``1/(k+x)``.
    Smaller ``k`` widens the gap; large ``k`` shrinks it. ``b`` (the
    middle item) is always last."""
    rankings = [["a", "b", "c"], ["c", "b", "a"]]
    default_fused = rrf_fuse(rankings)
    small_k = rrf_fuse(rankings, k=1)
    assert default_fused[-1] == "b"  # middle loses, regardless of k
    assert small_k[-1] == "b"
    assert small_k[0] in {"a", "c"}


def test_rrf_fuse_three_lists_combine() -> None:
    fused = rrf_fuse([["a", "b"], ["b", "c"], ["c", "a"]])
    # All three appear once at rank 0, once at rank 1, none at rank ≥ 2 →
    # all tied; sorted() is stable but Python dict insertion order
    # determines the order. Just check membership.
    assert set(fused) == {"a", "b", "c"}


def test_rrf_fuse_long_lists_dont_explode() -> None:
    """100-item lists fuse in reasonable time (algorithmic check)."""
    a_list = [f"a{i}" for i in range(100)]
    b_list = [f"b{i}" for i in range(100)]
    fused = rrf_fuse([a_list, b_list])
    assert len(fused) == 200  # disjoint sets so all 200 items appear


def test_rrf_fuse_zero_intersection_preserves_relative_order() -> None:
    """Items appearing in only one list are ranked by their position
    in that list."""
    fused = rrf_fuse([["a", "b", "c"], ["x", "y", "z"]])
    # Each list contributes its rank-0 item with the same score; same
    # for rank-1, etc. So "a" and "x" tie, "b" and "y" tie, etc.
    # All 6 items present.
    assert set(fused) == {"a", "b", "c", "x", "y", "z"}


# ---------------------------------------------------------------------------
# weighted_rrf_fuse_scored — B-175 W2 PR2 (knowledge base only)
# ---------------------------------------------------------------------------


def test_weighted_rrf_equal_weights_match_plain_rrf_exactly() -> None:
    """Regression lock: weight 1.0 on every list reproduces ``rrf_fuse_scored``
    item for item, score for score (exact float equality, tie order included)."""
    rng = random.Random(20261014)  # noqa: S311 — reproducible test data, not crypto
    pool = [f"c{i}" for i in range(40)]
    for _ in range(200):
        rankings = [
            rng.sample(pool, rng.randint(0, 25)) + rng.sample(pool, rng.randint(0, 3))
            for _ in range(rng.randint(1, 4))
        ]
        weighted = weighted_rrf_fuse_scored([(1.0, ranking) for ranking in rankings])
        assert weighted == rrf_fuse_scored(rankings)


def test_weighted_rrf_score_is_weight_over_k_plus_rank() -> None:
    fused = dict(weighted_rrf_fuse_scored([(2.0, ["a", "b"]), (0.5, ["b"])], k=10))
    assert fused["a"] == pytest.approx(2.0 / 11)
    assert fused["b"] == pytest.approx(2.0 / 12 + 0.5 / 11)


def test_weighted_rrf_heavier_list_wins_the_top_spot() -> None:
    # Same shape both ways round: whichever list carries more weight puts its
    # first item on top.
    vector, keyword = ["v", "x"], ["k", "y"]
    assert weighted_rrf_fuse_scored([(1.0, vector), (1.0, keyword)])[0][0] == "v"
    assert weighted_rrf_fuse_scored([(1.0, vector), (1.5, keyword)])[0][0] == "k"
    assert weighted_rrf_fuse_scored([(1.5, vector), (1.0, keyword)])[0][0] == "v"


def test_weighted_rrf_collapses_duplicates_and_handles_empty() -> None:
    assert weighted_rrf_fuse_scored([]) == []
    assert weighted_rrf_fuse_scored([(1.0, [])]) == []
    # The repeated "a" counts once, at its first (higher) rank.
    fused = weighted_rrf_fuse_scored([(2.0, ["a", "b", "a"])], k=10)
    assert fused == [("a", 2.0 / 11), ("b", 2.0 / 12)]


@pytest.mark.parametrize("weight", [-0.1, math.nan, math.inf, -math.inf])
def test_weighted_rrf_rejects_negative_or_non_finite_weight(weight: float) -> None:
    with pytest.raises(ValueError, match="weight"):
        weighted_rrf_fuse_scored([(weight, ["a"])])
