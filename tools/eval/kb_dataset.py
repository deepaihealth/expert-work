"""知识库评测集 v2(B-175 L1-T1):frozen dataclass + ``load_dataset(path)``。

格式见 ``datasets/kb/README.md``。只做加载与结构校验,不读语料原文;
``check_corpus`` 只拿 ``kb_corpus_inventory.py`` 产出的 ``manifest.csv`` 的 ``path`` / ``tier``
两列核对「期望文档在不在 A / D-A 层」。
"""

from __future__ import annotations

import csv
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

#: 用例来源。``faq_xlsx`` 产品 FAQ 表 / ``faq_2026`` FAQ-2026 表 / ``manual_qa`` 说明书 Q&A /
#: ``hard_cross`` 跨文档 / ``hard_exact`` 精确标识 / ``hard_table`` 表格 /
#: ``no_answer`` 资料里没有(应拒答)。
SOURCES = frozenset(
    {"faq_xlsx", "faq_2026", "manual_qa", "hard_cross", "hard_exact", "hard_table", "no_answer"}
)
#: 锚文本下限(字符数,去首尾空白后)。短于这个值的子串在长文档里到处都能 ``in`` 到。
MIN_ANCHOR_CHARS = 8
#: ``check_corpus`` 只认这两层:有文字层、会进评测库的文档。
CORPUS_CHECK_TIERS = frozenset({"A", "D-A"})


class KbDatasetError(ValueError):
    """评测集文件不合格式;消息里带用例 id(或序号)。"""


@dataclass(frozen=True)
class KbExpected:
    """一条期望命中:文档名 + 期望段落里的原文子串 + 页码(P5 之前恒为 None)。"""

    document: str
    anchor: str
    page: int | None


@dataclass(frozen=True)
class KbCase:
    id: str
    query: str
    source: str
    tags: tuple[str, ...]
    expected: tuple[KbExpected, ...]  # 空元组 = 应「无答案」
    answer: str | None


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
    return KbCase(
        id=case_id,
        query=query,
        source=source,
        tags=_parse_tags(entry.get("tags"), case_id=case_id),
        expected=tuple(_parse_expected(e, case_id=case_id) for e in expected_raw),
        answer=answer,
    )


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
    "MIN_ANCHOR_CHARS",
    "SOURCES",
    "KbCase",
    "KbDataset",
    "KbDatasetError",
    "KbExpected",
    "check_corpus",
    "load_dataset",
]
