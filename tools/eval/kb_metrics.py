"""知识库检索评测指标(B-175 L1-T3):纯函数,不碰库、不碰网络。

排名类指标复用 ``longmem/metrics.py``(``recall_at_k`` / ``mrr_at_k`` / ``ndcg_at_k`` /
``ordered_unique``);本模块补三件事:

* 两级命中判定 —— 文档级 = ``filename == expected.document``;段级 = 同一文档且
  ``expected.anchor in content``;
* 「拒答」判定 —— 结果为空,或给了阈值且**每条结果都带分数且都低于阈值**。``score is None``
  (只被关键词路召回)的结果会被工具原样返回给模型,算作「给出了结果」,不算拒答;
* 三个比率 —— ``zero_hit_rate``(返回为空)、``no_answer_refusal_rate``(无答案 case 被拒答)、
  ``false_refusal_rate``(有答案 case 被拒答)。

``longmem`` 的三个排名函数对空 relevant 返回 1.0(那边把它当 no-op case);无答案 case
(``expected`` 为空)在这里**不算排名指标**(字段为 ``None``),只进拒答率。

段级 id 的构造:第 i 名结果若命中期望条目 j,记作 ``exp:j``,否则 ``miss:i``;
一段同时含两条期望锚文本时只记前一条(同一文档写两条期望锚且落在同一段里,评测集
设计上不该出现)。
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass
from typing import Protocol

from longmem.metrics import (  # type: ignore[import-not-found]
    mrr_at_k,
    ndcg_at_k,
    ordered_unique,
    recall_at_k,
)

#: 两个 recall 截断点;``mrr`` / ``ndcg`` 只算 @10。
RECALL_KS: tuple[int, int] = (10, 20)
RANK_K = 10
#: 聚合里的均值字段(对无答案 case 为 ``None``,聚合时跳过)。
MEAN_FIELDS: tuple[tuple[str, str], ...] = (
    ("doc_recall@10", "doc_recall_10"),
    ("doc_recall@20", "doc_recall_20"),
    ("chunk_recall@10", "chunk_recall_10"),
    ("chunk_recall@20", "chunk_recall_20"),
    ("mrr@10", "mrr_10"),
    ("ndcg@10", "ndcg_10"),
)


class ResultLike(Protocol):
    """``orchestrator.tools.knowledge.RetrievedChunk`` 的结构镜像(只用到这三个字段)。"""

    @property
    def filename(self) -> str: ...
    @property
    def content(self) -> str: ...
    @property
    def score(self) -> float | None: ...


class ExpectedLike(Protocol):
    """``kb_dataset.KbExpected`` 的结构镜像。"""

    @property
    def document(self) -> str: ...
    @property
    def anchor(self) -> str: ...


@dataclass(frozen=True)
class CaseScore:
    """一条 case 的打分;``answerable=False`` 时六个排名指标全为 ``None``。"""

    answerable: bool
    zero_hit: bool
    refused: bool
    doc_recall_10: float | None
    doc_recall_20: float | None
    chunk_recall_10: float | None
    chunk_recall_20: float | None
    mrr_10: float | None
    ndcg_10: float | None
    doc_hit_ranks: tuple[int, ...]  # 1-based,按文档去重后的名次
    chunk_hit_ranks: tuple[int, ...]  # 1-based,结果列表里的名次

    def metrics(self) -> dict[str, float | None]:
        data = asdict(self)
        return {label: data[field] for label, field in MEAN_FIELDS}


# ---------------------------------------------------------------------------
# 命中判定
# ---------------------------------------------------------------------------


def document_hits(results: Sequence[ResultLike], expected: Sequence[ExpectedLike]) -> list[bool]:
    """逐名次:结果文件名是否等于某条期望的文档名。"""
    docs = {e.document for e in expected}
    return [r.filename in docs for r in results]


def chunk_hits(results: Sequence[ResultLike], expected: Sequence[ExpectedLike]) -> list[bool]:
    """逐名次:是否命中某条期望(同一文档 **且** 锚文本是内容的子串)。"""
    return [_matched_expected(r, expected) is not None for r in results]


def document_ranking(results: Sequence[ResultLike]) -> list[str]:
    """结果列表 → 按首次出现顺序去重的文档名列表。"""
    ranking: list[str] = ordered_unique([r.filename for r in results])
    return ranking


def _matched_expected(result: ResultLike, expected: Sequence[ExpectedLike]) -> int | None:
    for index, exp in enumerate(expected):
        if exp.document == result.filename and exp.anchor in result.content:
            return index
    return None


# ---------------------------------------------------------------------------
# 拒答判定
# ---------------------------------------------------------------------------


def is_refused(results: Sequence[ResultLike], *, threshold: float | None) -> bool:
    """空结果 = 拒答;给了阈值且每条都有分数且都低于阈值 = 拒答;其余都算给出了结果。"""
    if not results:
        return True
    if threshold is None:
        return False
    return all(r.score is not None and r.score < threshold for r in results)


# ---------------------------------------------------------------------------
# 单 case 打分
# ---------------------------------------------------------------------------


def score_case(
    results: Sequence[ResultLike],
    expected: Sequence[ExpectedLike],
    *,
    threshold: float | None,
) -> CaseScore:
    refused = is_refused(results, threshold=threshold)
    zero_hit = not results
    if not expected:
        return CaseScore(
            answerable=False,
            zero_hit=zero_hit,
            refused=refused,
            doc_recall_10=None,
            doc_recall_20=None,
            chunk_recall_10=None,
            chunk_recall_20=None,
            mrr_10=None,
            ndcg_10=None,
            doc_hit_ranks=(),
            chunk_hit_ranks=(),
        )
    doc_ranked = document_ranking(results)
    doc_relevant = frozenset(e.document for e in expected)
    chunk_ids = [
        f"exp:{m}" if (m := _matched_expected(r, expected)) is not None else f"miss:{i}"
        for i, r in enumerate(results)
    ]
    chunk_relevant = frozenset(f"exp:{j}" for j in range(len(expected)))
    k10, k20 = RECALL_KS
    return CaseScore(
        answerable=True,
        zero_hit=zero_hit,
        refused=refused,
        doc_recall_10=recall_at_k(doc_ranked, doc_relevant, k10),
        doc_recall_20=recall_at_k(doc_ranked, doc_relevant, k20),
        chunk_recall_10=recall_at_k(chunk_ids, chunk_relevant, k10),
        chunk_recall_20=recall_at_k(chunk_ids, chunk_relevant, k20),
        mrr_10=mrr_at_k(chunk_ids, chunk_relevant, RANK_K),
        ndcg_10=ndcg_at_k(chunk_ids, chunk_relevant, RANK_K),
        doc_hit_ranks=tuple(i + 1 for i, name in enumerate(doc_ranked) if name in doc_relevant),
        chunk_hit_ranks=tuple(i + 1 for i, cid in enumerate(chunk_ids) if cid in chunk_relevant),
    )


# ---------------------------------------------------------------------------
# 比率与聚合
# ---------------------------------------------------------------------------


def zero_hit_rate(scores: Sequence[CaseScore]) -> float:
    """返回为空的 case 占比(全部 case,含无答案的)。"""
    if not scores:
        return 0.0
    return sum(1 for s in scores if s.zero_hit) / len(scores)


def no_answer_refusal_rate(scores: Sequence[CaseScore]) -> float | None:
    """无答案 case 里被判拒答的占比;没有无答案 case 时为 ``None``。"""
    rows = [s for s in scores if not s.answerable]
    if not rows:
        return None
    return sum(1 for s in rows if s.refused) / len(rows)


def false_refusal_rate(scores: Sequence[CaseScore]) -> float | None:
    """有答案 case 里被判拒答的占比;没有有答案 case 时为 ``None``。"""
    rows = [s for s in scores if s.answerable]
    if not rows:
        return None
    return sum(1 for s in rows if s.refused) / len(rows)


def aggregate(scores: Sequence[CaseScore]) -> dict[str, float | int | None]:
    """一组 case 的汇总:计数、六个排名指标的均值(只算有答案的)、三个比率。"""
    answerable = [s for s in scores if s.answerable]
    out: dict[str, float | int | None] = {
        "n": len(scores),
        "n_answerable": len(answerable),
        "n_no_answer": len(scores) - len(answerable),
    }
    for label, field in MEAN_FIELDS:
        values = [v for s in answerable if (v := getattr(s, field)) is not None]
        out[label] = sum(values) / len(values) if values else None
    out["zero_hit_rate"] = zero_hit_rate(scores)
    out["no_answer_refusal_rate"] = no_answer_refusal_rate(scores)
    out["false_refusal_rate"] = false_refusal_rate(scores)
    return out


def grouped(pairs: Iterable[tuple[str, CaseScore]]) -> dict[str, dict[str, float | int | None]]:
    """``(分组键, 打分)`` 对 → 每个键一份 :func:`aggregate`,键按字典序。"""
    buckets: dict[str, list[CaseScore]] = defaultdict(list)
    for key, score in pairs:
        buckets[key].append(score)
    return {key: aggregate(buckets[key]) for key in sorted(buckets)}


__all__ = [
    "MEAN_FIELDS",
    "RANK_K",
    "RECALL_KS",
    "CaseScore",
    "ExpectedLike",
    "ResultLike",
    "aggregate",
    "chunk_hits",
    "document_hits",
    "document_ranking",
    "false_refusal_rate",
    "grouped",
    "is_refused",
    "no_answer_refusal_rate",
    "score_case",
    "zero_hit_rate",
]
