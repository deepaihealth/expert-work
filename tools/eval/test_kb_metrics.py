"""kb_metrics.py 纯函数:每个函数一组红绿(B-175 L1-T3)。"""

from __future__ import annotations

import math
from dataclasses import dataclass

import pytest
from kb_metrics import (  # type: ignore[import-not-found]
    CaseScore,
    aggregate,
    best_score,
    chunk_coverage,
    chunk_hits,
    coverage_at_k,
    document_coverage,
    document_hits,
    false_refusal_rate,
    grouped,
    hit_at_k,
    is_refused,
    mrr_at_k,
    ndcg_at_k,
    no_answer_refusal_rate,
    score_case,
    zero_hit_rate,
)


@dataclass(frozen=True)
class _Result:
    filename: str
    retrieval_text: str
    chunk_index: int = 0
    score: float | None = None
    recall_source: str | None = None


@dataclass(frozen=True)
class _Expected:
    document: str
    anchor: str
    also: frozenset[str] = frozenset()

    @property
    def documents(self) -> frozenset[str]:
        return frozenset({self.document}) | self.also


_ANCHOR = "常温密封保存即可"
_ANCHOR_B = "专利号 ZL2019-1234567"
_EXP_A = _Expected(document="a.md", anchor=_ANCHOR)
_EXP_B = _Expected(document="b.txt", anchor=_ANCHOR_B)
_EXP_A2 = _Expected(document="a.md", anchor="菌株编号 BL-99")


def _r(filename: str, content: str, score: float | None = 0.5) -> _Result:
    return _Result(filename=filename, retrieval_text=content, score=score)


def _miss(n: int) -> list[_Result]:
    return [_r(f"other{i}.md", f"无关内容 {i}", 0.1) for i in range(n)]


# ---------------------------------------------------------------------------
# document_hits / chunk_hits(逐名次布尔)
# ---------------------------------------------------------------------------


def test_document_hits_by_filename_equality() -> None:
    results = [_r("a.md", "x"), _r("z.md", "x"), _r("a.md", "y")]
    assert document_hits(results, [_EXP_A]) == [True, False, True]


def test_document_hits_is_exact_not_substring() -> None:
    assert document_hits([_r("xa.md", _ANCHOR)], [_EXP_A]) == [False]


def test_chunk_hit_needs_anchor_in_content_of_the_expected_document() -> None:
    results = [
        _r("a.md", f"益生菌{_ANCHOR},不需要冷藏。"),  # 命中
        _r("a.md", "益生菌是活的微生物。"),  # 同文档、锚不在
        _r("z.md", _ANCHOR),  # 锚在、文档不对
    ]
    assert chunk_hits(results, [_EXP_A]) == [True, False, False]


def test_chunk_hit_any_of_several_expected() -> None:
    results = [_r("b.txt", "本品专利号 ZL2019-1234567。")]
    assert chunk_hits(results, [_EXP_A, _EXP_B]) == [True]


# ---------------------------------------------------------------------------
# document_coverage / chunk_coverage(逐名次「新覆盖到的期望」)
# ---------------------------------------------------------------------------


def test_document_coverage_credits_each_expected_document_once_at_first_rank() -> None:
    results = [_r("a.md", "x"), _r("a.md", "y"), _r("b.txt", "z"), _r("a.md", "w")]
    assert document_coverage(results, [_EXP_A, _EXP_B]) == [
        frozenset({0}),
        frozenset(),
        frozenset({1}),
        frozenset(),
    ]


def test_document_coverage_groups_expected_entries_by_distinct_document() -> None:
    # 同一文档两条锚 → 文档级只有一个「期望文档」,索引 0。
    assert document_coverage([_r("a.md", "x")], [_EXP_A, _EXP_A2]) == [frozenset({0})]


def test_chunk_coverage_one_chunk_with_two_anchors_credits_both() -> None:
    # #3:一段同时含两条锚文本 → 两条期望同名次一起记命中。
    results = [_r("a.md", f"益生菌{_ANCHOR}。菌株编号 BL-99。")]
    assert chunk_coverage(results, [_EXP_A, _EXP_A2]) == [frozenset({0, 1})]


def test_chunk_coverage_repeated_hit_of_same_expected_counts_once() -> None:
    # #2:相邻两段重叠都含同一锚 → 只有最早那名记相关,后面的记空。
    results = [_r("a.md", "x" + _ANCHOR), _r("a.md", _ANCHOR + "y")]
    assert chunk_coverage(results, [_EXP_A]) == [frozenset({0}), frozenset()]


# ---------------------------------------------------------------------------
# hit@k / coverage@k / mrr / ndcg(在「逐名次覆盖集」上算)
# ---------------------------------------------------------------------------

_COV_BOTH_SPLIT = [frozenset(), frozenset({0}), frozenset(), frozenset({1})]


def test_hit_at_k_any_vs_all() -> None:
    assert hit_at_k(_COV_BOTH_SPLIT, n_expected=2, k=2, match="any") == 1.0
    assert hit_at_k(_COV_BOTH_SPLIT, n_expected=2, k=2, match="all") == 0.0
    assert hit_at_k(_COV_BOTH_SPLIT, n_expected=2, k=4, match="all") == 1.0
    assert hit_at_k(_COV_BOTH_SPLIT, n_expected=2, k=1, match="any") == 0.0


def test_coverage_at_k_is_fraction_of_expected_in_top_k() -> None:
    assert coverage_at_k(_COV_BOTH_SPLIT, n_expected=2, k=1) == 0.0
    assert coverage_at_k(_COV_BOTH_SPLIT, n_expected=2, k=2) == 0.5
    assert coverage_at_k(_COV_BOTH_SPLIT, n_expected=2, k=4) == 1.0


def test_coverage_at_k_cuts_by_result_rank_not_distinct_documents() -> None:
    # #4:12 条无关结果里只有 3 个不同文档,期望文档在第 13 名 → @10 必须是 0(按名次切)。
    results = [_r(f"x{i % 3}.md", "z") for i in range(12)] + [_r("a.md", "q")]
    cov = document_coverage(results, [_EXP_A])
    assert coverage_at_k(cov, n_expected=1, k=10) == 0.0
    assert coverage_at_k(cov, n_expected=1, k=20) == 1.0


def test_mrr_at_k_first_covered_rank() -> None:
    assert mrr_at_k(_COV_BOTH_SPLIT, k=10) == 0.5
    assert mrr_at_k(_COV_BOTH_SPLIT, k=1) == 0.0
    assert mrr_at_k([frozenset({0})], k=10) == 1.0


def test_ndcg_any_uses_single_ideal_hit() -> None:
    # any:命中一条就是满分;第 2 名命中 = 1/log2(3)。
    assert ndcg_at_k([frozenset({0})], n_expected=2, k=10, match="any") == 1.0
    assert ndcg_at_k(_COV_BOTH_SPLIT, n_expected=2, k=10, match="any") == pytest.approx(
        1 / math.log2(3)
    )


def test_ndcg_all_ideal_is_every_expected() -> None:
    # all:理想序 = 两条都在前两名;实际第 2、第 4 名 → (1/log2(3) + 1/log2(5)) / (1 + 1/log2(3))。
    ideal = 1.0 + 1 / math.log2(3)
    got = 1 / math.log2(3) + 1 / math.log2(5)
    assert ndcg_at_k(_COV_BOTH_SPLIT, n_expected=2, k=10, match="all") == pytest.approx(got / ideal)
    assert ndcg_at_k([frozenset({0}), frozenset({1})], n_expected=2, k=10, match="all") == 1.0


def test_ndcg_never_exceeds_one_when_one_rank_covers_two_expected() -> None:
    # #2/#3:一段含两锚 → 第二条按「下一名次」记(与连续两段命中等价),不超过 1。
    assert ndcg_at_k([frozenset({0, 1})], n_expected=2, k=10, match="all") == 1.0
    assert ndcg_at_k([frozenset(), frozenset({0, 1})], n_expected=2, k=10, match="all") < 1.0


def test_ndcg_counts_only_positions_within_k() -> None:
    cov: list[frozenset[int]] = [frozenset()] * 10 + [frozenset({0})]
    assert ndcg_at_k(cov, n_expected=1, k=10, match="any") == 0.0
    assert ndcg_at_k(cov, n_expected=1, k=20, match="any") == pytest.approx(1 / math.log2(12))


# ---------------------------------------------------------------------------
# is_refused / best_score
# ---------------------------------------------------------------------------


def test_refused_is_none_without_threshold() -> None:
    # #5:没给阈值就没有「拒答」这回事,恒定的 0 / 1 不是指标。
    assert is_refused([], threshold=None) is None
    assert is_refused([_r("a.md", "x", 0.01)], threshold=None) is None


def test_refused_when_no_results_and_threshold_set() -> None:
    assert is_refused([], threshold=0.5) is True


def test_refused_when_every_score_below_threshold() -> None:
    assert is_refused([_r("a.md", "x", 0.2), _r("b.txt", "y", 0.3)], threshold=0.5) is True


def test_not_refused_when_one_score_reaches_threshold() -> None:
    assert is_refused([_r("a.md", "x", 0.2), _r("b.txt", "y", 0.5)], threshold=0.5) is False


def test_keyword_only_hit_counts_as_surfaced_not_refused() -> None:
    # score=None = 只被关键词路召回;它会被工具原样返回给模型,不能算拒答。
    assert is_refused([_r("a.md", "x", None)], threshold=0.5) is False


def test_best_score_is_max_vector_score_ignoring_keyword_only() -> None:
    assert best_score([]) is None
    assert best_score([_r("a.md", "x", None)]) is None
    assert best_score([_r("a.md", "x", 0.2), _r("b.txt", "y", None), _r("c.md", "z", 0.7)]) == 0.7


# ---------------------------------------------------------------------------
# score_case
# ---------------------------------------------------------------------------


def test_score_case_doc_hit_without_chunk_hit() -> None:
    results = [*_miss(2), _r("a.md", "益生菌是活的微生物。", 0.9)]
    score = score_case(results, [_EXP_A], threshold=None)
    assert score.answerable is True
    assert score.match == "any"
    assert score.doc_hit_10 == 1.0
    assert score.doc_hit_20 == 1.0
    assert score.doc_coverage_10 == 1.0
    assert score.chunk_hit_10 == 0.0
    assert score.chunk_coverage_20 == 0.0
    assert score.mrr_10 == 0.0
    assert score.ndcg_10 == 0.0
    assert score.doc_hit_ranks == (3,)
    assert score.chunk_hit_ranks == ()
    assert score.zero_hit is False
    assert score.refused is None
    assert score.best_score == 0.9


def test_score_case_chunk_hit_at_rank_two() -> None:
    results = [*_miss(1), _r("a.md", f"益生菌{_ANCHOR}。", 0.9)]
    score = score_case(results, [_EXP_A], threshold=None)
    assert score.chunk_hit_10 == 1.0
    assert score.mrr_10 == 0.5
    assert score.ndcg_10 == pytest.approx(1 / math.log2(3))
    assert score.chunk_hit_ranks == (2,)


def test_score_case_hit_beyond_ten_counts_only_at_twenty() -> None:
    results = [*_miss(10), _r("a.md", f"益生菌{_ANCHOR}。", 0.9)]
    score = score_case(results, [_EXP_A], threshold=None)
    assert score.doc_hit_10 == 0.0
    assert score.doc_hit_20 == 1.0
    assert score.chunk_hit_10 == 0.0
    assert score.chunk_hit_20 == 1.0
    assert score.mrr_10 == 0.0


def test_score_case_any_two_expected_one_found_is_a_hit_with_half_coverage() -> None:
    # #1:match=any 时列出的文档都含答案,命中任一就算对;coverage 另报 0.5。
    results = [_r("a.md", f"益生菌{_ANCHOR}。", 0.9)]
    score = score_case(results, [_EXP_A, _EXP_B], threshold=None, match="any")
    assert score.doc_hit_10 == 1.0
    assert score.chunk_hit_10 == 1.0
    assert score.doc_coverage_10 == 0.5
    assert score.chunk_coverage_10 == 0.5
    assert score.ndcg_10 == 1.0


def test_score_case_all_two_expected_one_found_is_not_a_hit() -> None:
    results = [_r("a.md", f"益生菌{_ANCHOR}。", 0.9)]
    score = score_case(results, [_EXP_A, _EXP_B], threshold=None, match="all")
    assert score.match == "all"
    assert score.doc_hit_10 == 0.0
    assert score.chunk_hit_10 == 0.0
    assert score.doc_coverage_10 == 0.5
    assert score.chunk_coverage_10 == 0.5
    assert score.mrr_10 == 1.0  # 首个相关仍在第 1 名
    assert score.ndcg_10 == pytest.approx(1.0 / (1.0 + 1 / math.log2(3)))


def test_score_case_duplicate_chunk_hits_do_not_inflate_ndcg() -> None:
    # #2:重叠的相邻两段都含锚 → ndcg 仍是 1.0,chunk_hit_ranks 只记最早那名。
    results = [_r("a.md", "x" + _ANCHOR, 0.9), _r("a.md", _ANCHOR + "y", 0.8)]
    score = score_case(results, [_EXP_A], threshold=None)
    assert score.ndcg_10 == 1.0
    assert score.chunk_hit_ranks == (1,)
    assert score.doc_hit_ranks == (1,)


def test_score_case_rejects_unknown_match() -> None:
    with pytest.raises(ValueError, match="match"):
        score_case([], [_EXP_A], threshold=None, match="some")


def test_score_case_empty_results_is_zero_hit() -> None:
    score = score_case([], [_EXP_A], threshold=0.5)
    assert score.zero_hit is True
    assert score.refused is True
    assert score.best_score is None
    assert score.doc_hit_10 == 0.0
    assert score.mrr_10 == 0.0


def test_score_case_no_answer_has_no_rank_metrics() -> None:
    refused = score_case([], [], threshold=0.5)
    assert refused.answerable is False
    assert refused.refused is True
    assert refused.doc_hit_10 is None
    assert refused.chunk_hit_10 is None
    assert refused.doc_coverage_10 is None
    assert refused.mrr_10 is None
    assert refused.ndcg_10 is None
    surfaced = score_case([_r("a.md", "x", 0.9)], [], threshold=0.5)
    assert surfaced.refused is False
    assert surfaced.best_score == 0.9
    assert surfaced.doc_hit_10 is None


# ---------------------------------------------------------------------------
# 比率与聚合
# ---------------------------------------------------------------------------


def _score(
    *,
    answerable: bool = True,
    refused: bool | None = False,
    zero_hit: bool = False,
    value: float | None = 1.0,
) -> CaseScore:
    rank = None if not answerable else value
    return CaseScore(
        answerable=answerable,
        match="any",
        zero_hit=zero_hit,
        refused=refused,
        best_score=None,
        doc_hit_10=rank,
        doc_hit_20=rank,
        doc_coverage_10=rank,
        doc_coverage_20=rank,
        chunk_hit_10=rank,
        chunk_hit_20=rank,
        chunk_coverage_10=rank,
        chunk_coverage_20=rank,
        mrr_10=rank,
        ndcg_10=rank,
        doc_hit_ranks=(),
        chunk_hit_ranks=(),
    )


def test_zero_hit_rate_over_all_cases() -> None:
    assert zero_hit_rate([]) == 0.0
    rows = [_score(zero_hit=True), _score(), _score(answerable=False)]
    assert zero_hit_rate(rows) == pytest.approx(1 / 3)


def test_no_answer_refusal_rate_only_over_no_answer_cases() -> None:
    assert no_answer_refusal_rate([_score()]) is None
    rows = [
        _score(answerable=False, refused=True),
        _score(answerable=False, refused=False),
        _score(refused=True),  # 有答案的不算进来
    ]
    assert no_answer_refusal_rate(rows) == 0.5


def test_refusal_rates_are_none_when_threshold_unset() -> None:
    rows = [_score(answerable=False, refused=None), _score(refused=None)]
    assert no_answer_refusal_rate(rows) is None
    assert false_refusal_rate(rows) is None


def test_false_refusal_rate_only_over_answerable_cases() -> None:
    assert false_refusal_rate([_score(answerable=False, refused=True)]) is None
    rows = [_score(refused=True), _score(), _score(), _score(answerable=False, refused=False)]
    assert false_refusal_rate(rows) == pytest.approx(1 / 3)


def test_aggregate_means_skip_no_answer_rows() -> None:
    rows = [_score(value=1.0), _score(value=0.0), _score(answerable=False, refused=True)]
    agg = aggregate(rows)
    assert agg["n"] == 3
    assert agg["n_answerable"] == 2
    assert agg["n_no_answer"] == 1
    assert agg["doc_hit@10"] == 0.5
    assert agg["doc_coverage@20"] == 0.5
    assert agg["chunk_hit@20"] == 0.5
    assert agg["chunk_coverage@10"] == 0.5
    assert agg["mrr@10"] == 0.5
    assert agg["ndcg@10"] == 0.5
    assert agg["zero_hit_rate"] == 0.0
    assert agg["no_answer_refusal_rate"] == 1.0
    assert agg["false_refusal_rate"] == 0.0


def test_aggregate_empty() -> None:
    agg = aggregate([])
    assert agg["n"] == 0
    assert agg["doc_hit@10"] is None
    assert agg["no_answer_refusal_rate"] is None


def test_grouped_aggregates_per_key_sorted() -> None:
    pairs = [("faq", _score(value=1.0)), ("manual", _score(value=0.0)), ("faq", _score(value=0.0))]
    out = grouped(pairs)
    assert list(out) == ["faq", "manual"]
    assert out["faq"]["n"] == 2
    assert out["faq"]["doc_hit@10"] == 0.5
    assert out["manual"]["doc_hit@10"] == 0.0


# ---------------------------------------------------------------------------
# 锚文本判对:手列文档之外,含同一锚文本的文档也算对(B-175 波 2 PR0)
# ---------------------------------------------------------------------------

_EXP_A_ALSO = _Expected(document="a.md", anchor=_ANCHOR, also=frozenset({"a2.md"}))


def test_document_hit_counts_an_alternate_document() -> None:
    assert document_hits([_r("a2.md", "无关")], [_EXP_A_ALSO]) == [True]
    assert document_hits([_r("a2.md", "无关")], [_EXP_A]) == [False]


def test_chunk_hit_counts_the_anchor_in_an_alternate_document() -> None:
    results = [_r("a2.md", f"第二版:{_ANCHOR}"), _r("a.md", _ANCHOR)]
    score = score_case(results, [_EXP_A_ALSO], threshold=None)
    assert score.chunk_hit_ranks == (1,)
    assert score.mrr_10 == 1.0


def test_alternate_document_without_the_anchor_is_not_a_chunk_hit() -> None:
    score = score_case(
        [_r("a2.md", "别的段落"), _r("a.md", _ANCHOR)], [_EXP_A_ALSO], threshold=None
    )
    assert score.doc_hit_ranks == (1,)
    assert score.chunk_hit_ranks == (2,)
    assert score.mrr_10 == 0.5


def test_alternate_document_covers_only_its_own_expectation_under_all() -> None:
    """``match=all`` must not loosen: an alternate of A says nothing about B."""
    score = score_case([_r("a2.md", _ANCHOR)], [_EXP_A_ALSO, _EXP_B], threshold=None, match="all")
    assert score.chunk_hit_10 == 0.0
    assert score.chunk_coverage_10 == 0.5
    assert score.doc_coverage_10 == 0.5


def test_document_coverage_groups_alternates_under_the_listed_document() -> None:
    """Two entries listed under a.md stay one expected document, whichever of
    their acceptable files covers them."""
    exp_a2_also = _Expected(document="a.md", anchor="菌株编号 BL-99", also=frozenset({"a2.md"}))
    coverage = document_coverage([_r("a2.md", "x")], [_EXP_A_ALSO, exp_a2_also])
    assert coverage == [frozenset({0})]
