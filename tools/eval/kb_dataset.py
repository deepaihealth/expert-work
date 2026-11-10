"""知识库评测集 v2(B-175 L1-T1):frozen dataclass + ``load_dataset(path)``。

格式见 ``datasets/kb/README.md``。只做加载与结构校验,不读语料原文;
``check_corpus`` 只拿 ``kb_corpus_inventory.py`` 产出的 ``manifest.csv`` 的 ``path`` / ``tier``
两列核对「期望文档在不在 A / D-A 层」。
"""

from __future__ import annotations

import csv
import random
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

#: 用例来源。``faq_xlsx`` 产品 FAQ 表 / ``faq_2026`` FAQ-2026 表 / ``manual_qa`` 说明书 Q&A /
#: ``hard_cross`` 跨文档 / ``hard_exact`` 精确标识 / ``hard_table`` 表格 /
#: ``no_answer`` 资料里没有(应拒答)/ ``chat_qa`` 客户聊天里的真实问法(脱敏改写)。
#: (``kb_hard_sets.py`` 运行时派生的难负例 / 近名干扰题另记 ``hard_negative`` / ``distractor``,
#: 不是评测集文件里能写的值。)
SOURCES = frozenset(
    {
        "faq_xlsx",
        "faq_2026",
        "manual_qa",
        "hard_cross",
        "hard_exact",
        "hard_table",
        "no_answer",
        "chat_qa",
    }
)
#: 锚文本下限(字符数,去首尾空白后)。短于这个值的子串在长文档里到处都能 ``in`` 到。
MIN_ANCHOR_CHARS = 8
#: 期望文档的命中规则:``any`` = 列出的文档都含答案,命中任一即算对(FAQ 表 + 说明书那种);
#: ``all`` = 问题本身要求找全(「哪些产品含 X」),按覆盖率算。
MATCH_MODES = frozenset({"any", "all"})
DEFAULT_MATCH = "any"
#: ``check_corpus`` 只认这两层:有文字层、会进评测库的文档。
CORPUS_CHECK_TIERS = frozenset({"A", "D-A"})
#: 智能体层的期望作答(B-206 11-09)。``answerable`` 有答案;``no_answer`` 资料没写、应说没写;
#: ``partial`` 资料只写了相关事实、没直接写问题要的结论 —— 应按用户 11-07 拍板的 B 式作答:
#: 说「资料没直接写」+ 列出相关的原文事实 + 推断处标明是推断。文件里不写 ``label`` 时按
#: ``expected`` 推(非空 = answerable,空 = no_answer)。
LABELS = frozenset({"answerable", "no_answer", "partial"})


class KbDatasetError(ValueError):
    """评测集文件不合格式;消息里带用例 id(或序号)。"""


@dataclass(frozen=True)
class KbExpected:
    """一条期望命中:文档名 + 期望段落里的原文子串 + 页码(P5 之前恒为 None)。

    ``also`` 不来自评测集文件,由运行器入库后填:含同一锚文本的其他文档(同一份 FAQ 的两个
    版本之类),它们排在前面也算对。"""

    document: str
    anchor: str
    page: int | None
    also: frozenset[str] = frozenset()

    @property
    def documents(self) -> frozenset[str]:
        return frozenset({self.document}) | self.also


@dataclass(frozen=True)
class KbCase:
    id: str
    query: str
    source: str
    tags: tuple[str, ...]
    expected: tuple[KbExpected, ...]  # 空元组 = 应「无答案」
    answer: str | None
    match: str = DEFAULT_MATCH  # any | all,见 MATCH_MODES
    #: 文件里写的 ``label``;None = 按 ``expected`` 推(见 :attr:`answer_label`)。
    label: str | None = None
    #: ``partial`` 题:资料里写了的相关事实(原文锚)。``expected`` 仍为空,检索层照旧按
    #: 「没有期望段」算,召回分数不受改标签影响;只给智能体层判分看。
    related: tuple[KbExpected, ...] = ()

    @property
    def answer_label(self) -> str:
        """:data:`LABELS` 之一。"""
        if self.label is not None:
            return self.label
        return "answerable" if self.expected else "no_answer"


@dataclass(frozen=True)
class KbDataset:
    name: str
    corpus_manifest: str | None  # 仓库外路径,运行时可覆盖
    corpus_tiers: tuple[str, ...]
    cases: tuple[KbCase, ...]


def load_dataset(path: Path) -> KbDataset:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, Mapping):
        raise KbDatasetError(f"{path}: top level must be a mapping")
    name = raw.get("name")
    if not isinstance(name, str) or not name.strip():
        raise KbDatasetError(f"{path}: 'name' is required")
    cases_raw = raw.get("cases")
    if not isinstance(cases_raw, list):
        raise KbDatasetError(f"{path}: 'cases' must be a list")
    manifest, tiers = _parse_corpus(raw.get("corpus"), path=path)
    cases = tuple(_parse_case(entry, index=i) for i, entry in enumerate(cases_raw))
    _reject_duplicate_ids(cases)
    return KbDataset(name=name, corpus_manifest=manifest, corpus_tiers=tiers, cases=cases)


def check_corpus(dataset: KbDataset, manifest_csv: Path) -> list[str]:
    """期望文档(按文件名)不在清单 A / D-A 层的,一条一个问题;``expected`` 为空的用例跳过。"""
    present = set(_manifest_basenames(manifest_csv))
    return [
        f"{case.id}: document not in corpus manifest (tiers A/D-A): {exp.document}"
        for case in dataset.cases
        for exp in case.expected
        if exp.document not in present
    ]


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------


def _parse_corpus(block: Any, *, path: Path) -> tuple[str | None, tuple[str, ...]]:
    if block is None:
        return None, ()
    if not isinstance(block, Mapping):
        raise KbDatasetError(f"{path}: 'corpus' must be a mapping")
    manifest = block.get("manifest")
    if manifest is not None and not isinstance(manifest, str):
        raise KbDatasetError(f"{path}: 'corpus.manifest' must be a string")
    tiers_raw = block.get("tiers", [])
    if not isinstance(tiers_raw, list) or not all(isinstance(t, str) for t in tiers_raw):
        raise KbDatasetError(f"{path}: 'corpus.tiers' must be a list of strings")
    return manifest, tuple(tiers_raw)


def _parse_case(entry: Any, *, index: int) -> KbCase:
    label = f"case #{index}"
    if not isinstance(entry, Mapping):
        raise KbDatasetError(f"{label}: must be a mapping")
    case_id = entry.get("id")
    if not isinstance(case_id, str) or not case_id.strip():
        raise KbDatasetError(f"{label}: 'id' is required")
    query = entry.get("query")
    if not isinstance(query, str) or not query.strip():
        raise KbDatasetError(f"{case_id}: 'query' is required and must be non-empty")
    source = entry.get("source")
    if source not in SOURCES:
        raise KbDatasetError(f"{case_id}: 'source' {source!r} not in {sorted(SOURCES)}")
    expected_raw = entry.get("expected")
    if not isinstance(expected_raw, list):
        raise KbDatasetError(f"{case_id}: 'expected' must be a list (empty = no-answer case)")
    answer = entry.get("answer")
    if answer is not None and not isinstance(answer, str):
        raise KbDatasetError(f"{case_id}: 'answer' must be a string or null")
    match = entry.get("match", DEFAULT_MATCH)
    if match not in MATCH_MODES:
        raise KbDatasetError(f"{case_id}: 'match' {match!r} not in {sorted(MATCH_MODES)}")
    expected = tuple(_parse_expected(e, case_id=case_id) for e in expected_raw)
    answer_label, related = _parse_label(entry, expected=expected, case_id=case_id)
    return KbCase(
        id=case_id,
        query=query,
        source=source,
        tags=_parse_tags(entry.get("tags"), case_id=case_id),
        expected=expected,
        answer=answer,
        match=str(match),
        label=answer_label,
        related=related,
    )


def _parse_label(
    entry: Mapping[str, Any], *, expected: tuple[KbExpected, ...], case_id: str
) -> tuple[str | None, tuple[KbExpected, ...]]:
    label = entry.get("label")
    if label is not None and label not in LABELS:
        raise KbDatasetError(f"{case_id}: 'label' {label!r} not in {sorted(LABELS)}")
    related_raw = entry.get("related") or []
    if not isinstance(related_raw, list):
        raise KbDatasetError(f"{case_id}: 'related' must be a list")
    related = tuple(_parse_expected(e, case_id=case_id) for e in related_raw)
    if related and label != "partial":
        raise KbDatasetError(f"{case_id}: 'related' only with label partial")
    if label in ("partial", "no_answer") and expected:
        raise KbDatasetError(f"{case_id}: 'expected' must be empty for label {label}")
    if label == "partial" and not related:
        raise KbDatasetError(f"{case_id}: label partial needs 'related' (the stated facts)")
    if label == "answerable" and not expected:
        raise KbDatasetError(f"{case_id}: label answerable needs 'expected'")
    return label, related


def _parse_tags(raw: Any, *, case_id: str) -> tuple[str, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list) or not all(isinstance(t, str) for t in raw):
        raise KbDatasetError(f"{case_id}: 'tags' must be a list of strings")
    return tuple(raw)


def _parse_expected(raw: Any, *, case_id: str) -> KbExpected:
    if not isinstance(raw, Mapping):
        raise KbDatasetError(f"{case_id}: each 'expected' entry must be a mapping")
    document = raw.get("document")
    if not isinstance(document, str) or not document.strip():
        raise KbDatasetError(f"{case_id}: expected.document is required")
    anchor = raw.get("anchor")
    if not isinstance(anchor, str) or len(anchor.strip()) < MIN_ANCHOR_CHARS:
        raise KbDatasetError(
            f"{case_id}: expected.anchor must be at least {MIN_ANCHOR_CHARS} characters"
        )
    page = raw.get("page")
    if page is not None and (isinstance(page, bool) or not isinstance(page, int)):
        raise KbDatasetError(f"{case_id}: expected.page must be an int or null")
    return KbExpected(document=document, anchor=anchor, page=page)


def _reject_duplicate_ids(cases: Sequence[KbCase]) -> None:
    seen: set[str] = set()
    for case in cases:
        if case.id in seen:
            raise KbDatasetError(f"{case.id}: duplicate case id")
        seen.add(case.id)


def _manifest_basenames(manifest_csv: Path) -> Iterator[str]:
    with manifest_csv.open(encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            if row.get("tier") in CORPUS_CHECK_TIERS:
                yield row["path"].replace("\\", "/").rsplit("/", 1)[-1]


__all__ = [
    "CORPUS_CHECK_TIERS",
    "DEFAULT_MATCH",
    "LABELS",
    "MATCH_MODES",
    "MIN_ANCHOR_CHARS",
    "SOURCES",
    "KbCase",
    "KbDataset",
    "KbDatasetError",
    "KbExpected",
    "check_corpus",
    "load_dataset",
]


#: B-206 智能体层评测的固定抽样(放这里而不是 kb_agent_cases,免得 kb_hard_sets 与它互相导入)。
SEED = 20261018
N_ANSWERABLE_PER_SET = 15


def select_cases(main: KbDataset, chat: KbDataset) -> list[tuple[str, KbCase]]:
    """两个集各取全部无答案题 + 固定种子抽 :data:`N_ANSWERABLE_PER_SET` 道有答案题。"""
    rng = random.Random(SEED)  # noqa: S311 - a fixed, reproducible sample
    picked: list[tuple[str, KbCase]] = []
    for label, dataset in (("main", main), ("chat", chat)):
        no_answer = [c for c in dataset.cases if not c.expected]
        answerable = [c for c in dataset.cases if c.expected]
        picked += [(label, c) for c in no_answer]
        picked += [(label, c) for c in rng.sample(answerable, N_ANSWERABLE_PER_SET)]
    return picked
