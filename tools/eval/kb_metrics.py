"""知识库检索评测指标(B-175 L1-T3):纯函数,不碰库、不碰网络。

命中有两级:文档级 = ``filename in expected.documents``(手列文档 + 运行器找出的含同一锚文本的
文档);段级 = 文件在其中 **且**
``expected.anchor in retrieval_text``(检索文本 = 标题路径 + 正文,智能体看到的那段字;
嵌入 / 关键词索引另在前面加了文档名,判对不算文档名;两侧先过 :func:`anchor_key` 归一,
见 :func:`anchor_in`)。B-206 K-1 起一条结果是一个**父段**(召回命中的是子段,返回的是它所在的
父段),``retrieval_text`` 是父段的文本,所以段级命中、``mrr@10``、``ndcg@10`` 都是父段级:锚文本
跨了同一父段的两个子段也算命中。两级都先算成「逐名次覆盖集」——第 r 名结果**首次**覆盖到
哪些期望条目(:func:`document_coverage` / :func:`chunk_coverage`):

* 每条期望只在最早命中它的那一名记一次,后面再命中(相邻段重叠都含锚)记空,
  所以 nDCG 不会超过 1;
* 一段同时含两条锚文本 → 两条期望同一名次一起记命中;
* 文档级按**结果名次**切 k(前 k 条结果里出现了哪些期望文档),不按去重后的文档序列。

在覆盖集上算的指标(``expected`` 非空的 case 才算;无答案 case 这些字段为 ``None``):

* ``hit@k``:``match=any`` → 任一期望进前 k 即 1;``match=all`` → 全部期望进前 k 才 1;
* ``coverage@k``:进前 k 的期望条目数 / 期望总数(两种 match 都报);
* ``mrr@10``:首个相关结果名次的倒数;
* ``ndcg@10``:``any`` → 理想序只有 1 条相关;``all`` → 理想序 = 全部期望依次在前 n 名;
  同一名次覆盖两条期望时第二条按「下一名次」记(与连续两段命中等价),保证 ≤ 1。

``context_hit``(B-206「父段级段命中」)不看名次也不看段号:锚文本(归一后)是否落在
``knowledge_search`` 按默认条数**渲染给模型的那段文本**里(``any`` 任一条、``all`` 全部);
运行器把工具真正的输出传进来,所以工具改成返回父段 / 改了条数或格式,它跟着变。没给渲染文本时
为 ``None``。

``dup_share@10``(B-206 K-21②)也不看期望:前 10 条结果里,与更靠前某条是同一段(生产去副本
同一把键 ``orchestrator.tools.knowledge_dedup.passage_key``:标题路径 + 正文,只合并空白)的占比
—— 被副本(同一内容换个文件名重传)占掉的名额。每条有结果的 case 都算(含无答案的),汇总取它们
的均值;没有结果为 ``None``。

「拒答」只在给了阈值时有定义:结果为空,或每条结果都带分数且都低于阈值;``score is None``
(只被关键词路召回)的结果会被工具原样返回给模型,算作「给出了结果」。阈值未设时
``refused`` / 两个拒答率都是 ``None``;每条 case 的 ``best_score``(最佳向量分数)另存,
便于离线扫阈值。
"""

from __future__ import annotations

import functools
import math
import re
import unicodedata
from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import asdict, dataclass
from typing import Protocol

from orchestrator.tools.knowledge_dedup import passage_key

#: 两个截断点;``mrr`` / ``ndcg`` 只算 @10。
RECALL_KS: tuple[int, int] = (10, 20)
RANK_K = 10
MATCH_MODES = frozenset({"any", "all"})
#: 聚合里的均值字段(对无答案 case 为 ``None``,聚合时跳过)。
MEAN_FIELDS: tuple[tuple[str, str], ...] = (
    ("doc_hit@10", "doc_hit_10"),
    ("doc_hit@20", "doc_hit_20"),
    ("doc_coverage@10", "doc_coverage_10"),
    ("doc_coverage@20", "doc_coverage_20"),
    ("chunk_hit@10", "chunk_hit_10"),
    ("chunk_hit@20", "chunk_hit_20"),
    ("chunk_coverage@10", "chunk_coverage_10"),
    ("chunk_coverage@20", "chunk_coverage_20"),
    ("mrr@10", "mrr_10"),
    ("ndcg@10", "ndcg_10"),
    ("context_hit", "context_hit"),
)
#: 副本占名额的汇总键(不在 ``MEAN_FIELDS`` 里:它对无答案 case 也有值)。
DUP_SHARE = "dup_share@10"

#: 归一时去掉的 HTML 标签(``<br>``、``<u>`` 之类;PDF / 表格转出来的文字里常见)。
_HTML_TAG = re.compile(r"</?[A-Za-z][^<>]{0,40}>")


@functools.lru_cache(maxsize=200_000)
def anchor_key(text: str) -> str:
    """锚文本比对用的归一形:去 HTML 标签,NFKC + 小写,只留字母与数字。

    同一段字在原文里常带空格、加粗、全角标点、``<br>`` 断行(``**专利号 ZL 2019 1 1234.5**``),
    评测集里的锚文本是连写的;逐字子串比对认不出,检索命中了也记成没中(B-175 波 2 T0.5)。
    字母与数字本身一个不改,所以只差一位数字的两段仍然对不上。"""
    folded = unicodedata.normalize("NFKC", _HTML_TAG.sub(" ", text)).lower()
    return "".join(ch for ch in folded if unicodedata.category(ch)[0] in "LN")


def anchor_in(anchor: str, text: str) -> bool:
    """``anchor`` 归一后是否是 ``text`` 归一后的子串。"""
    return anchor_key(anchor) in anchor_key(text)


#: 逐名次覆盖集:第 i 项 = 第 i+1 名结果首次覆盖到的期望条目索引。
Coverage = Sequence[frozenset[int]]


class ResultLike(Protocol):
    """``orchestrator.tools.knowledge.RetrievedChunk`` 的结构镜像(只用到这三个字段)。"""

    @property
    def filename(self) -> str:
        """Source document filename of the hit."""

    @property
    def retrieval_text(self) -> str:
        """Text the agent sees for the hit — section breadcrumb + body of the
        passage the result returns: the parent of the matched chunk since
        B-206 K-1 (the index also carries the document title; hits are not
        judged on it)."""

    @property
    def score(self) -> float | None:
        """Recall score (vector similarity when available)."""

    @property
    def heading_path(self) -> tuple[str, ...]:
        """Section breadcrumb of the returned passage."""

    @property
    def content(self) -> str:
        """Body of the returned passage."""


class ExpectedLike(Protocol):
    """``kb_dataset.KbExpected`` 的结构镜像。"""

    @property
    def document(self) -> str:
        """Expected document filename."""

    @property
    def anchor(self) -> str:
        """Expected anchor text inside that document."""

    @property
    def documents(self) -> frozenset[str]:
        """Every filename that counts as this expectation's document: the
        listed one plus any other ingested document that contains the same
        anchor text (resolved by the runner; just the listed one otherwise)."""


@dataclass(frozen=True)
class CaseScore:
    """一条 case 的打分;``answerable=False`` 时十个排名指标全为 ``None``。"""

    answerable: bool
    match: str
    zero_hit: bool
    refused: bool | None  # 阈值未设时 None
    best_score: float | None  # 结果里最高的向量分数;空结果 / 全关键词路时 None
    doc_hit_10: float | None
    doc_hit_20: float | None
    doc_coverage_10: float | None
    doc_coverage_20: float | None
    chunk_hit_10: float | None
    chunk_hit_20: float | None
    chunk_coverage_10: float | None
    chunk_coverage_20: float | None
    mrr_10: float | None
    ndcg_10: float | None
    doc_hit_ranks: tuple[int, ...]  # 1-based 结果名次,每个期望文档首次出现的那名
    chunk_hit_ranks: tuple[int, ...]  # 1-based 结果名次,首次覆盖到某条期望的那些名
    context_hit: float | None = None  # 没给渲染文本 / 无答案 case 为 None
    dup_share_10: float | None = None  # 有答案、无答案都算;没有结果为 None

    def metrics(self) -> dict[str, float | None]:
        data = asdict(self)
        return {
            **{label: data[field] for label, field in MEAN_FIELDS},
            DUP_SHARE: self.dup_share_10,
        }


# ---------------------------------------------------------------------------
# 命中判定
# ---------------------------------------------------------------------------


def document_hits(results: Sequence[ResultLike], expected: Sequence[ExpectedLike]) -> list[bool]:
    """逐名次:结果文件名是否是某条期望可接受的文档(手列的 + 含同一锚文本的)。"""
    docs: set[str] = set()
    for exp in expected:
        docs |= exp.documents
    return [r.filename in docs for r in results]


def chunk_hits(results: Sequence[ResultLike], expected: Sequence[ExpectedLike]) -> list[bool]:
    """逐名次:是否命中某条期望(同一文档 **且** 锚文本是内容的子串)。"""
    return [bool(_matching_expected(r, expected)) for r in results]


def document_coverage(
    results: Sequence[ResultLike], expected: Sequence[ExpectedLike]
) -> list[frozenset[int]]:
    """逐名次首次覆盖到的**期望文档**索引(同一手列文档的多条期望合成一个,按首次出现顺序
    编号);某一组的任一条期望可接受该结果的文件,就算覆盖这一组。"""
    groups: dict[str, int] = {}
    for exp in expected:
        groups.setdefault(exp.document, len(groups))
    return _first_coverage(
        results,
        lambda r: sorted({groups[e.document] for e in expected if r.filename in e.documents}),
    )


def chunk_coverage(
    results: Sequence[ResultLike], expected: Sequence[ExpectedLike]
) -> list[frozenset[int]]:
    """逐名次首次覆盖到的期望条目索引;一段含两锚则两条一起记。"""
    return _first_coverage(results, lambda r: _matching_expected(r, expected))


def context_hit(shown_text: str, expected: Sequence[ExpectedLike], *, match: str) -> float:
    """锚文本是否落在交给模型的文本里:``any`` 任一条、``all`` 全部(见模块说明)。"""
    _check_match(match)
    found = [anchor_in(exp.anchor, shown_text) for exp in expected]
    return 1.0 if (all(found) if match == "all" else any(found)) else 0.0


def _matching_expected(result: ResultLike, expected: Sequence[ExpectedLike]) -> list[int]:
    return [
        index
        for index, exp in enumerate(expected)
        if result.filename in exp.documents and anchor_in(exp.anchor, result.retrieval_text)
    ]


def _first_coverage(
    results: Sequence[ResultLike], matcher: Callable[[ResultLike], Iterable[int]]
) -> list[frozenset[int]]:
    seen: set[int] = set()
    out: list[frozenset[int]] = []
    for result in results:
        new = frozenset(j for j in matcher(result) if j not in seen)
        seen |= new
        out.append(new)
    return out


# ---------------------------------------------------------------------------
# 覆盖集上的排名指标
# ---------------------------------------------------------------------------


def _covered_within(coverage: Coverage, k: int) -> set[int]:
    return set().union(*coverage[:k]) if k > 0 else set()


def hit_at_k(coverage: Coverage, *, n_expected: int, k: int, match: str) -> float:
    """``any``:前 k 名覆盖到任一期望;``all``:前 k 名覆盖到全部期望。"""
    _check_match(match)
    covered = _covered_within(coverage, k)
    hit = len(covered) == n_expected if match == "all" else bool(covered)
    return 1.0 if hit else 0.0


def coverage_at_k(coverage: Coverage, *, n_expected: int, k: int) -> float:
    """前 k 名覆盖到的期望条目数 / 期望总数。"""
    if n_expected <= 0:
        return 0.0
    return len(_covered_within(coverage, k)) / n_expected


def mrr_at_k(coverage: Coverage, *, k: int) -> float:
    for rank, covered in enumerate(coverage[:k], start=1):
        if covered:
            return 1.0 / rank
    return 0.0


def ndcg_at_k(coverage: Coverage, *, n_expected: int, k: int, match: str) -> float:
    """二元增益 nDCG。``any`` 只给首个相关记分、理想序 1 条;``all`` 每条期望按首次覆盖名次记分、
    理想序 ``n_expected`` 条;同一名次覆盖多条时后面的按「下一名次」记,所以永远 ≤ 1。"""
    _check_match(match)
    positions = _hit_positions(coverage, first_only=match == "any")
    ideal_n = 1 if match == "any" else n_expected
    idcg = sum(_discount(p) for p in range(1, min(k, ideal_n) + 1))
    if idcg == 0.0:
        return 0.0
    dcg = sum(_discount(p) for p in positions if p <= k)
    return dcg / idcg


def _hit_positions(coverage: Coverage, *, first_only: bool) -> list[int]:
    positions: list[int] = []
    for rank, covered in enumerate(coverage, start=1):
        for _ in covered:
            positions.append(rank if not positions else max(rank, positions[-1] + 1))
            if first_only:
                return positions
    return positions


def _discount(position: int) -> float:
    return 1.0 / math.log2(position + 1.0)


def _check_match(match: str) -> None:
    if match not in MATCH_MODES:
        raise ValueError(f"match must be one of {sorted(MATCH_MODES)}, got {match!r}")


# ---------------------------------------------------------------------------
# 拒答判定 / 最佳分数
# ---------------------------------------------------------------------------


def is_refused(results: Sequence[ResultLike], *, threshold: float | None) -> bool | None:
    """阈值未设 → ``None``;空结果 = 拒答;每条都有分数且都低于阈值 = 拒答;其余算给出了结果。"""
    if threshold is None:
        return None
    if not results:
        return True
    return all(r.score is not None and r.score < threshold for r in results)


def best_score(results: Sequence[ResultLike]) -> float | None:
    """结果里最高的向量分数(关键词路 ``None`` 不算);没有带分数的结果时 ``None``。"""
    scores = [r.score for r in results if r.score is not None]
    return max(scores) if scores else None


def dup_share_at_k(results: Sequence[ResultLike], *, k: int = RANK_K) -> float | None:
    """前 ``k`` 条结果里,与更靠前某条是同一段(生产去副本的键 ``passage_key``:标题路径 + 正文,
    只合并空白)的占比;没有结果时 ``None``。"""
    top = results[:k]
    if not top:
        return None
    seen: set[bytes] = set()
    copies = 0
    for result in top:
        key = passage_key(result.heading_path, result.content)
        if key in seen:
            copies += 1
        seen.add(key)
    return copies / len(top)


# ---------------------------------------------------------------------------
# 单 case 打分
# ---------------------------------------------------------------------------


def score_case(
    results: Sequence[ResultLike],
    expected: Sequence[ExpectedLike],
    *,
    threshold: float | None,
    match: str = "any",
    rerank_threshold: float | None = None,
    shown_text: str | None = None,
) -> CaseScore:
    """``rerank_threshold`` (B-175 wave 2 PR3) is the rerank-score floor the
    retriever already applied: with it set, an empty result is a refusal.
    ``shown_text`` is what ``knowledge_search`` handed the model for this
    query; it only feeds ``context_hit``."""
    _check_match(match)
    refused = is_refused(results, threshold=threshold)
    if rerank_threshold is not None:
        refused = bool(refused) or not results
    zero_hit = not results
    best = best_score(results)
    dup_share = dup_share_at_k(results)
    if not expected:
        return CaseScore(
            answerable=False,
            match=match,
            zero_hit=zero_hit,
            refused=refused,
            best_score=best,
            doc_hit_10=None,
            doc_hit_20=None,
            doc_coverage_10=None,
            doc_coverage_20=None,
            chunk_hit_10=None,
            chunk_hit_20=None,
            chunk_coverage_10=None,
            chunk_coverage_20=None,
            mrr_10=None,
            ndcg_10=None,
            doc_hit_ranks=(),
            chunk_hit_ranks=(),
            dup_share_10=dup_share,
        )
    doc_cov = document_coverage(results, expected)
    n_docs = len({e.document for e in expected})
    chunk_cov = chunk_coverage(results, expected)
    n_exp = len(expected)
    k10, k20 = RECALL_KS
    return CaseScore(
        answerable=True,
        match=match,
        zero_hit=zero_hit,
        refused=refused,
        best_score=best,
        doc_hit_10=hit_at_k(doc_cov, n_expected=n_docs, k=k10, match=match),
        doc_hit_20=hit_at_k(doc_cov, n_expected=n_docs, k=k20, match=match),
        doc_coverage_10=coverage_at_k(doc_cov, n_expected=n_docs, k=k10),
        doc_coverage_20=coverage_at_k(doc_cov, n_expected=n_docs, k=k20),
        chunk_hit_10=hit_at_k(chunk_cov, n_expected=n_exp, k=k10, match=match),
        chunk_hit_20=hit_at_k(chunk_cov, n_expected=n_exp, k=k20, match=match),
        chunk_coverage_10=coverage_at_k(chunk_cov, n_expected=n_exp, k=k10),
        chunk_coverage_20=coverage_at_k(chunk_cov, n_expected=n_exp, k=k20),
        mrr_10=mrr_at_k(chunk_cov, k=RANK_K),
        ndcg_10=ndcg_at_k(chunk_cov, n_expected=n_exp, k=RANK_K, match=match),
        doc_hit_ranks=tuple(i + 1 for i, covered in enumerate(doc_cov) if covered),
        chunk_hit_ranks=tuple(i + 1 for i, covered in enumerate(chunk_cov) if covered),
        context_hit=(
            None if shown_text is None else context_hit(shown_text, expected, match=match)
        ),
        dup_share_10=dup_share,
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
    """无答案 case 里被判拒答的占比;没有无答案 case 或阈值未设(``refused is None``)时 ``None``。"""
    rows = [s.refused for s in scores if not s.answerable and s.refused is not None]
    if not rows:
        return None
    return sum(1 for r in rows if r) / len(rows)


def false_refusal_rate(scores: Sequence[CaseScore]) -> float | None:
    """有答案 case 里被判拒答的占比;没有有答案 case 或阈值未设时 ``None``。"""
    rows = [s.refused for s in scores if s.answerable and s.refused is not None]
    if not rows:
        return None
    return sum(1 for r in rows if r) / len(rows)


def aggregate(scores: Sequence[CaseScore]) -> dict[str, float | int | None]:
    """一组 case 的汇总:计数、十个排名指标的均值(只算有答案的)、三个比率。"""
    answerable = [s for s in scores if s.answerable]
    out: dict[str, float | int | None] = {
        "n": len(scores),
        "n_answerable": len(answerable),
        "n_no_answer": len(scores) - len(answerable),
    }
    for label, field in MEAN_FIELDS:
        values = [v for s in answerable if (v := getattr(s, field)) is not None]
        out[label] = sum(values) / len(values) if values else None
    shares = [s.dup_share_10 for s in scores if s.dup_share_10 is not None]
    out[DUP_SHARE] = sum(shares) / len(shares) if shares else None
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
    "DUP_SHARE",
    "MATCH_MODES",
    "MEAN_FIELDS",
    "RANK_K",
    "RECALL_KS",
    "CaseScore",
    "Coverage",
    "ExpectedLike",
    "ResultLike",
    "aggregate",
    "best_score",
    "chunk_coverage",
    "chunk_hits",
    "context_hit",
    "coverage_at_k",
    "document_coverage",
    "document_hits",
    "dup_share_at_k",
    "false_refusal_rate",
    "grouped",
    "hit_at_k",
    "is_refused",
    "mrr_at_k",
    "ndcg_at_k",
    "no_answer_refusal_rate",
    "score_case",
    "zero_hit_rate",
]
