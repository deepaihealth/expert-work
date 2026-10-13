"""kb_metrics.py 纯函数:每个函数一组红绿(B-175 L1-T3)。"""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from kb_metrics import (  # type: ignore[import-not-found]
    CaseScore,
    aggregate,
    chunk_hits,
    document_hits,
    document_ranking,
    false_refusal_rate,
    grouped,
    is_refused,
    no_answer_refusal_rate,
    score_case,
    zero_hit_rate,
)


@dataclass(frozen=True)
class _Result:
    filename: str
    content: str
    chunk_index: int = 0
    score: float | None = None
    recall_source: str | None = None


@dataclass(frozen=True)
class _Expected:
    document: str
    anchor: str


_ANCHOR = "常温密封保存即可"
_EXP_A = _Expected(document="a.md", anchor=_ANCHOR)
_EXP_B = _Expected(document="b.txt", anchor="专利号 ZL2019-1234567")


def _r(filename: str, content: str, score: float | None = 0.5) -> _Result:
    return _Result(filename=filename, content=content, score=score)


# ---------------------------------------------------------------------------
# document_hits / chunk_hits / document_ranking
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


def test_document_ranking_is_order_preserving_unique() -> None:
    results = [_r("b.txt", "1"), _r("a.md", "2"), _r("b.txt", "3")]
    assert document_ranking(results) == ["b.txt", "a.md"]


# ---------------------------------------------------------------------------
# is_refused
# ---------------------------------------------------------------------------


def test_refused_when_no_results() -> None:
    assert is_refused([], threshold=None) is True
    assert is_refused([], threshold=0.5) is True


def test_not_refused_without_threshold_when_anything_returned() -> None:
    assert is_refused([_r("a.md", "x", 0.01)], threshold=None) is False


def test_refused_when_every_score_below_threshold() -> None:
    assert is_refused([_r("a.md", "x", 0.2), _r("b.txt", "y", 0.3)], threshold=0.5) is True


def test_not_refused_when_one_score_reaches_threshold() -> None:
    assert is_refused([_r("a.md", "x", 0.2), _r("b.txt", "y", 0.5)], threshold=0.5) is False


def test_keyword_only_hit_counts_as_surfaced_not_refused() -> None:
    # score=None = 只被关键词路召回;它会被工具原样返回给模型,不能算拒答。
    assert is_refused([_r("a.md", "x", None)], threshold=0.5) is False


# ---------------------------------------------------------------------------
# score_case
# ---------------------------------------------------------------------------


def _miss(n: int) -> list[_Result]:
    return [_r(f"other{i}.md", f"无关内容 {i}", 0.1) for i in range(n)]


def test_score_case_doc_hit_without_chunk_hit() -> None:
    results = [*_miss(2), _r("a.md", "益生菌是活的微生物。", 0.9)]
    score = score_case(results, [_EXP_A], threshold=None)
    assert score.answerable is True
    assert score.doc_recall_10 == 1.0
    assert score.doc_recall_20 == 1.0
    assert score.chunk_recall_10 == 0.0
    assert score.chunk_recall_20 == 0.0
    assert score.mrr_10 == 0.0
    assert score.ndcg_10 == 0.0
    assert score.doc_hit_ranks == (3,)
    assert score.chunk_hit_ranks == ()
    assert score.zero_hit is False
    assert score.refused is False


def test_score_case_chunk_hit_at_rank_two() -> None:
    results = [*_miss(1), _r("a.md", f"益生菌{_ANCHOR}。", 0.9)]
    score = score_case(results, [_EXP_A], threshold=None)
    assert score.chunk_recall_10 == 1.0
    assert score.mrr_10 == 0.5
    assert 0.0 < (score.ndcg_10 or 0.0) < 1.0
    assert score.chunk_hit_ranks == (2,)


def test_score_case_hit_beyond_ten_counts_only_at_twenty() -> None:
    results = [*_miss(10), _r("a.md", f"益生菌{_ANCHOR}。", 0.9)]
    score = score_case(results, [_EXP_A], threshold=None)
    assert score.doc_recall_10 == 0.0
    assert score.doc_recall_20 == 1.0
    assert score.chunk_recall_10 == 0.0
    assert score.chunk_recall_20 == 1.0
    assert score.mrr_10 == 0.0


def test_score_case_two_expected_half_recall() -> None:
    results = [_r("a.md", f"益生菌{_ANCHOR}。", 0.9)]
    score = score_case(results, [_EXP_A, _EXP_B], threshold=None)
    assert score.doc_recall_10 == 0.5
    assert score.chunk_recall_10 == 0.5


def test_score_case_empty_results_is_zero_hit_and_refused() -> None:
    score = score_case([], [_EXP_A], threshold=None)
    assert score.zero_hit is True
    assert score.refused is True
    assert score.doc_recall_10 == 0.0
    assert score.mrr_10 == 0.0


def test_score_case_no_answer_has_no_rank_metrics() -> None:
    # longmem 的 recall/ndcg/mrr 对空 relevant 返回 1.0 —— 无答案 case 不能拿那个 1.0。
    refused = score_case([], [], threshold=None)
    assert refused.answerable is False
    assert refused.refused is True
    assert refused.doc_recall_10 is None
    assert refused.chunk_recall_10 is None
    assert refused.mrr_10 is None
    assert refused.ndcg_10 is None
    surfaced = score_case([_r("a.md", "x", 0.9)], [], threshold=0.5)
    assert surfaced.refused is False
    assert surfaced.doc_recall_10 is None


# ---------------------------------------------------------------------------
# 比率与聚合
# ---------------------------------------------------------------------------


def _score(
    *,
    answerable: bool = True,
    refused: bool = False,
    zero_hit: bool = False,
    doc10: float | None = 1.0,
) -> CaseScore:
    rank = None if not answerable else doc10
    return CaseScore(
        answerable=answerable,
        zero_hit=zero_hit,
        refused=refused,
        doc_recall_10=rank,
        doc_recall_20=rank,
        chunk_recall_10=rank,
        chunk_recall_20=rank,
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


def test_false_refusal_rate_only_over_answerable_cases() -> None:
    assert false_refusal_rate([_score(answerable=False, refused=True)]) is None
    rows = [_score(refused=True), _score(), _score(), _score(answerable=False, refused=False)]
    assert false_refusal_rate(rows) == pytest.approx(1 / 3)


def test_aggregate_means_skip_no_answer_rows() -> None:
    rows = [_score(doc10=1.0), _score(doc10=0.0), _score(answerable=False, refused=True)]
    agg = aggregate(rows)
    assert agg["n"] == 3
    assert agg["n_answerable"] == 2
    assert agg["n_no_answer"] == 1
    assert agg["doc_recall@10"] == 0.5
    assert agg["chunk_recall@20"] == 0.5
    assert agg["mrr@10"] == 0.5
    assert agg["ndcg@10"] == 0.5
    assert agg["zero_hit_rate"] == 0.0
    assert agg["no_answer_refusal_rate"] == 1.0
    assert agg["false_refusal_rate"] == 0.0


def test_aggregate_empty() -> None:
    agg = aggregate([])
    assert agg["n"] == 0
    assert agg["doc_recall@10"] is None
    assert agg["no_answer_refusal_rate"] is None


def test_grouped_aggregates_per_key_sorted() -> None:
    pairs = [("faq", _score(doc10=1.0)), ("manual", _score(doc10=0.0)), ("faq", _score(doc10=0.0))]
    out = grouped(pairs)
    assert list(out) == ["faq", "manual"]
    assert out["faq"]["n"] == 2
    assert out["faq"]["doc_recall@10"] == 0.5
    assert out["manual"]["doc_recall@10"] == 0.0
