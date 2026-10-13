"""从客户自带的问答表 / 说明书 Q&A 生成知识库评测集 v2(B-175 L1-T2)。

输入(都在仓库外):资料根目录、清点脚本的 ``manifest.csv``、一份 sources YAML(哪些表 / 哪些文档
是问答来源,表头叫什么)。输出:``<name>.yaml``(评测集,格式见 ``datasets/kb/README.md``)+
``<name>.review.md``(配不出锚文本、或含个人信息被剔除的条目,留人工处理)。

锚文本怎么配:把参考答案按句切开,长的先试,在语料的**解析后文本**里做子串查找
(与入库同一个 ``parse_document``,所以配到的锚在分段正文里也 ``in`` 得到)。答案原文所在的
出处文档(FAQ 表自己)排在最后,前面留给真正要找的文档(说明书 / 课件)。

代码里没有任何客户名;表名、表头、目录全在 sources YAML 里。

用法::

    uv run --no-sync python -I tools/eval/kb_build_dataset.py \\
        --spec tools/eval/datasets/kb/<name>.sources.yaml \\
        --corpus-root <资料根目录> --manifest <清单目录>/manifest.csv \\
        --parsed-cache <仓库外目录> \\
        --out tools/eval/datasets/kb/<name>.yaml --review tools/eval/datasets/kb/<name>.review.md
"""

# ruff: noqa: RUF001  # 中文标点是要匹配 / 要测的目标
from __future__ import annotations

import argparse
import csv
import hashlib
import os
import re
import sys
import unicodedata
from collections import OrderedDict
from collections.abc import Iterable, Mapping, Sequence
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import openpyxl  # type: ignore[import-untyped]
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from kb_dataset import KbExpected, load_dataset  # type: ignore[import-not-found]

#: 锚文本长度:短于下限的子串到处都 ``in`` 得到;长于上限的截到上限(仍是原文子串)。
ANCHOR_MIN_CHARS = 10
ANCHOR_MAX_CHARS = 40
#: 问题文本短于这么多字符的丢掉(问答表里混着「健康指导」这类栏目名,不是问题)。
QUERY_MIN_CHARS = 5
#: 产品分表里的短问题(「产品作用?」「怎么吃?」每个产品一份)离开表就没了主语,
#: 归一化后短于这么多字符的在前面补上产品名(标签),否则去重会把 11 个产品的同名问题合成一条。
QUERY_PREFIX_BELOW_CHARS = 8
#: 一条用例最多列几份期望文档。
DEFAULT_MAX_DOCS = 3
#: 超过这么多页的 PDF(教材 / 书)不解析:答案不会出自它们,解析又慢。
DEFAULT_MAX_PDF_PAGES = 150

_NUMBERING = re.compile(r"^\s*\d+\s*[\.、．:：]\s*")
_NON_WORD = re.compile(r"[\W_]+")
_QA_ITEM = re.compile(r"^\s*(\d+)\s*[\.、．]\s*(.+?)\s*$")
_ANSWER_LEAD = re.compile(r"^\s*(\*\*)?\s*答\s*[:：]\s*(\*\*)?\s*")
_QUESTION_HINT = re.compile(r"[?？]|吗|为什么|如何|怎么|什么|哪|是否|能否|可以")
_SENTENCE_SPLIT = re.compile(r"[。；;！？!?\n]|\\n")
_PERSONAL = (
    re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"),  # 手机号
    re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"),  # 邮箱
    re.compile(
        r"(微信|weixin|vx|wx)\s*(号|id)?\s*[:：]?\s*[A-Za-z][\w-]{4,}", re.IGNORECASE
    ),  # 微信号
)


@dataclass(frozen=True)
class QaRecord:
    """一条原始问答:来自哪张表 / 哪份文档。"""

    query: str
    answer: str
    source: str
    tags: tuple[str, ...]
    origin_document: str | None
    #: 出处文档是不是「真正要找的文档」:说明书里的 Q&A 是(排第一);问答表自己不是(排最后)。
    origin_is_target: bool = False


@dataclass(frozen=True)
class MergedRecord:
    """同一个问题(归一化后相同)合并后的记录:多条答案都留着找锚。"""

    query: str
    answers: tuple[str, ...]
    source: str
    tags: tuple[str, ...]
    origin_document: str | None
    origin_is_target: bool = False


# ---------------------------------------------------------------------------
# 抽取
# ---------------------------------------------------------------------------


def extract_xlsx_qa(
    path: Path,
    *,
    source: str,
    question_headers: Sequence[str],
    answer_headers: Sequence[str],
    tag_header: str | None = None,
    sheets: Sequence[str] | None = None,
    skip_sheets: Sequence[str] = (),
) -> list[QaRecord]:
    """每个 sheet 自己找表头行(第一行里出现问题表头的);没有表头的 sheet 跳过。
    标签 = ``tag_header`` 那一列(空则退回 sheet 名)。"""
    # 不用 read_only:客户表格的维度元数据常是坏的,read_only 下只读得出 1 行。
    workbook = openpyxl.load_workbook(path, data_only=True)
    records: list[QaRecord] = []
    for sheet in workbook.worksheets:
        if sheet.title in skip_sheets or (sheets is not None and sheet.title not in sheets):
            continue
        rows = [tuple(row) for row in sheet.iter_rows(values_only=True)]
        header_index = _find_header_row(rows, question_headers)
        if header_index is None:
            continue
        header = [_cell(c) for c in rows[header_index]]
        q_col = next(i for i, h in enumerate(header) if h in question_headers)
        a_col = next((i for i, h in enumerate(header) if h in answer_headers), None)
        t_col = header.index(tag_header) if tag_header in header else None
        if a_col is None:
            continue
        for row in rows[header_index + 1 :]:
            query, answer = _cell_at(row, q_col), _cell_at(row, a_col)
            if not query or not answer:
                continue
            tag = (_cell_at(row, t_col) if t_col is not None else "") or sheet.title
            records.append(
                QaRecord(
                    query=_with_subject(query, tag),
                    answer=answer,
                    source=source,
                    tags=(tag,),
                    origin_document=path.name,
                )
            )
    workbook.close()
    return records


def _with_subject(query: str, tag: str) -> str:
    """短问题补主语:标签是产品名(不含「通用」)、问题里没有它、归一化后太短 → 前缀产品名。"""
    if not tag or "通用" in tag or tag in query:
        return query
    if len(normalize_query(query)) >= QUERY_PREFIX_BELOW_CHARS:
        return query
    return f"{tag}{query}"


def _find_header_row(
    rows: Sequence[tuple[Any, ...]], question_headers: Sequence[str]
) -> int | None:
    for index, row in enumerate(rows[:10]):
        if any(_cell(c) in question_headers for c in row):
            return index
    return None


def _cell(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _cell_at(row: tuple[Any, ...], index: int) -> str:
    return _cell(row[index]) if index < len(row) else ""


def extract_docx_qa(
    markdown: str, *, source: str, origin_document: str, section_marker: str
) -> list[QaRecord]:
    """从解析后的 Markdown 里取「常见问答」一节:编号行是问题(去掉加粗标记),
    下一个编号行或下一个标题之前的文本是答案(去掉开头的「答:」)。"""
    section = _qa_section(markdown, section_marker)
    if section is None:
        return []
    records: list[QaRecord] = []
    question: str | None = None
    answer_lines: list[str] = []

    def close() -> None:
        if question is not None:
            answer = _ANSWER_LEAD.sub("", "\n".join(answer_lines).strip()).strip()
            if answer:
                records.append(QaRecord(question, answer, source, (), origin_document))

    for line in section:
        match = _QA_ITEM.match(line)
        candidate = match.group(2).strip("*").strip() if match else ""
        if match and len(candidate) <= 80 and _QUESTION_HINT.search(candidate):
            close()
            question, answer_lines = candidate, []
        elif question is not None:
            answer_lines.append(line)
    close()
    return records


def _qa_section(markdown: str, marker: str) -> list[str] | None:
    lines = markdown.splitlines()
    start = next((i for i, line in enumerate(lines) if marker in line), None)
    if start is None:
        return None
    end = next(
        (i for i in range(start + 1, len(lines)) if lines[i].lstrip().startswith("#")),
        len(lines),
    )
    return lines[start + 1 : end]


# ---------------------------------------------------------------------------
# 归一化 / 去重 / 个人信息
# ---------------------------------------------------------------------------


def normalize_query(query: str) -> str:
    """去编号、全角转半角、去标点空白、小写 —— 同一个问题的不同写法归成一个键。"""
    text = unicodedata.normalize("NFKC", _NUMBERING.sub("", query)).replace("*", "")
    return _NON_WORD.sub("", text).lower()


def dedupe_records(records: Iterable[QaRecord]) -> list[MergedRecord]:
    """按归一化问题合并:保留第一次出现的写法 / 来源 / 出处,标签与答案并集(保序)。"""
    merged: OrderedDict[str, MergedRecord] = OrderedDict()
    for record in records:
        key = normalize_query(record.query)
        if len(key) < QUERY_MIN_CHARS:
            continue
        existing = merged.get(key)
        if existing is None:
            merged[key] = MergedRecord(
                query=record.query.strip(),
                answers=(record.answer,),
                source=record.source,
                tags=tuple(t for t in record.tags if t),
                origin_document=record.origin_document,
                origin_is_target=record.origin_is_target,
            )
            continue
        merged[key] = MergedRecord(
            query=existing.query,
            answers=_append_unique(existing.answers, record.answer),
            source=existing.source,
            tags=_append_unique(existing.tags, *record.tags),
            origin_document=existing.origin_document,
            origin_is_target=existing.origin_is_target,
        )
    return list(merged.values())


def _append_unique(base: tuple[str, ...], *items: str) -> tuple[str, ...]:
    out = list(base)
    for item in items:
        if item and item not in out:
            out.append(item)
    return tuple(out)


def contains_personal_data(text: str) -> bool:
    """手机号 / 邮箱 / 微信号任一命中即为真;人名查不出来,靠人工看 review 文件。"""
    return any(pattern.search(text) for pattern in _PERSONAL)


# ---------------------------------------------------------------------------
# 配锚
# ---------------------------------------------------------------------------


def candidate_anchors(answers: Sequence[str]) -> list[str]:
    """答案按句切,去首尾空白,长的在前;超长截到上限(截出来仍是原文子串)。"""
    seen: OrderedDict[str, None] = OrderedDict()
    for answer in answers:
        for raw in _SENTENCE_SPLIT.split(answer):
            sentence = raw.strip()
            if len(sentence) >= ANCHOR_MIN_CHARS:
                seen.setdefault(sentence[:ANCHOR_MAX_CHARS], None)
    return sorted(seen, key=len, reverse=True)


def pick_expected(
    *,
    answers: Sequence[str],
    parsed_texts: Mapping[str, str],
    origin_document: str | None,
    max_docs: int = DEFAULT_MAX_DOCS,
    origin_first: bool = False,
) -> tuple[KbExpected, ...]:
    """每份文档取它命中的最长一句做锚;非出处文档按锚长排序,最多 ``max_docs``。
    出处文档命中时:``origin_first`` 为真(说明书 Q&A)排第一,否则(问答表自己)殿后。"""
    best: dict[str, str] = {}
    for anchor in candidate_anchors(answers):
        for document, text in parsed_texts.items():
            if document not in best and anchor in text:
                best[document] = anchor
    others = sorted((d for d in best if d != origin_document), key=lambda d: (-len(best[d]), d))
    if origin_document not in best:
        ordered = others[:max_docs]
    elif origin_first:
        ordered = [origin_document, *others[: max_docs - 1]]
    else:
        ordered = [*others[: max_docs - 1], origin_document]
    return tuple(KbExpected(document=d, anchor=best[d], page=None) for d in ordered)


# ---------------------------------------------------------------------------
# 语料解析(带缓存,放仓库外)
# ---------------------------------------------------------------------------


def parse_corpus(
    *,
    corpus_root: Path,
    manifest: Path,
    tiers: Sequence[str],
    cache_dir: Path,
    max_pdf_pages: int = DEFAULT_MAX_PDF_PAGES,
    workers: int | None = None,
) -> tuple[dict[str, str], dict[str, int]]:
    """按清单解析进评测库的文档,返回 ``{文件名: 解析文本}`` 与计数。
    重复组只解析第一份;同名不同内容的文件只保留先到的那份(计入 ``name_collisions``)。
    解析走进程池(``workers`` 默认 CPU 数):单进程串行解析 600 份要 3 小时。"""
    cache_dir.mkdir(parents=True, exist_ok=True)
    counts = {
        "parsed": 0,
        "skipped_dup": 0,
        "skipped_big_pdf": 0,
        "failed": 0,
        "name_collisions": 0,
    }
    notes: list[str] = []
    todo = _select_documents(
        _manifest_rows(manifest, tiers),
        corpus_root=corpus_root,
        max_pdf_pages=max_pdf_pages,
        counts=counts,
        notes=notes,
    )
    texts: dict[str, str] = {}
    with ProcessPoolExecutor(max_workers=workers or os.cpu_count() or 1) as pool:
        results = pool.map(_parse_cached, todo, [cache_dir] * len(todo))
        for path, text in zip(todo, results, strict=True):
            if text is None:
                counts["failed"] += 1
                notes.append(f"parse failed: {path.name}")
            else:
                texts[path.name] = text
                counts["parsed"] += 1
    _PARSE_NOTES[:] = notes
    return texts, counts


#: 最近一次 ``parse_corpus`` 的备注(解析失败 / 同名碰撞的文件名),写进 review 文件。
_PARSE_NOTES: list[str] = []


def _select_documents(
    rows: Sequence[Mapping[str, str]],
    *,
    corpus_root: Path,
    max_pdf_pages: int,
    counts: dict[str, int],
    notes: list[str],
) -> list[Path]:
    seen_groups: set[str] = set()
    seen_names: set[str] = set()
    todo: list[Path] = []
    for row in rows:
        if row["dup_group"]:
            if row["dup_group"] in seen_groups:
                counts["skipped_dup"] += 1
                continue
            seen_groups.add(row["dup_group"])
        if row["ext"] == "pdf" and int(row["pages"] or 0) > max_pdf_pages:
            counts["skipped_big_pdf"] += 1
            continue
        path = corpus_root / row["path"]
        if path.name in seen_names:
            counts["name_collisions"] += 1
            notes.append(f"name collision (kept first): {row['path']}")
            continue
        seen_names.add(path.name)
        todo.append(path)
    return todo


def _manifest_rows(manifest: Path, tiers: Sequence[str]) -> list[dict[str, str]]:
    with manifest.open(encoding="utf-8", newline="") as fh:
        return [row for row in csv.DictReader(fh) if row.get("tier") in tiers]


def _parse_cached(path: Path, cache_dir: Path) -> str | None:
    """进程池里跑:命中缓存直接读,否则用与入库同一个 ``parse_document`` 解析并写缓存。"""
    key = hashlib.sha1(str(path).encode("utf-8"), usedforsecurity=False).hexdigest()
    cached = cache_dir / f"{key}.md"
    if cached.exists():
        return cached.read_text(encoding="utf-8")
    from control_plane.knowledge.parsing import parse_document  # 子进程里导入,主进程不拖重依赖

    try:
        text = _sanitize(str(parse_document(path.name, path.read_bytes())))
        cache_dir.mkdir(parents=True, exist_ok=True)
        cached.write_text(text, encoding="utf-8")
    except Exception:  # 解析失败只计数,评测集不因一份坏文件中断
        return None
    return text


def _sanitize(text: str) -> str:
    """去掉孤立代理项(docx 里的半个 emoji 会让 ``parse_document`` 吐出 ``\\ud83d`` 这种
    无法按 UTF-8 编码的字符,写缓存 / 入库都会炸)。"""
    return text.encode("utf-8", errors="replace").decode("utf-8")


def collect_records(
    spec: Mapping[str, Any], *, corpus_root: Path, parsed_texts: Mapping[str, str]
) -> list[QaRecord]:
    records: list[QaRecord] = []
    for src in spec.get("sources", []):
        kind = src["type"]
        if kind == "xlsx_qa":
            records.extend(
                extract_xlsx_qa(
                    corpus_root / src["file"],
                    source=src["source"],
                    question_headers=tuple(src["question_headers"]),
                    answer_headers=tuple(src["answer_headers"]),
                    tag_header=src.get("tag_header"),
                    sheets=tuple(src["sheets"]) if "sheets" in src else None,
                    skip_sheets=tuple(src.get("skip_sheets", ())),
                )
            )
        elif kind == "docx_qa":
            records.extend(_docx_records(src, corpus_root=corpus_root, parsed_texts=parsed_texts))
        else:
            raise ValueError(f"unknown source type {kind!r}")
    return records


def _docx_records(
    src: Mapping[str, Any], *, corpus_root: Path, parsed_texts: Mapping[str, str]
) -> list[QaRecord]:
    records: list[QaRecord] = []
    for path in sorted(corpus_root.glob(src["glob"])):
        text = parsed_texts.get(path.name)
        if text is None:
            continue
        tag = _dir_tag(path.parent.name) if src.get("tag_from_parent_dir") else ""
        for record in extract_docx_qa(
            text, source=src["source"], origin_document=path.name, section_marker=src["section"]
        ):
            records.append(
                QaRecord(
                    record.query,
                    record.answer,
                    record.source,
                    (tag,) if tag else (),
                    path.name,
                    origin_is_target=True,
                )
            )
    return records


def _dir_tag(name: str) -> str:
    return re.sub(r"^[\d\s]+", "", name).strip()


def build_dataset(
    spec: Mapping[str, Any],
    *,
    corpus_root: Path,
    manifest: Path,
    parsed_cache: Path,
    out: Path,
    review: Path,
    workers: int | None = None,
) -> dict[str, int]:
    tiers = tuple(spec.get("corpus", {}).get("tiers") or ("A", "D-A"))
    parsed_texts, counts = parse_corpus(
        corpus_root=corpus_root,
        manifest=manifest,
        tiers=tiers,
        cache_dir=parsed_cache,
        max_pdf_pages=int(spec.get("max_pdf_pages", DEFAULT_MAX_PDF_PAGES)),
        workers=workers,
    )
    merged = dedupe_records(
        collect_records(spec, corpus_root=corpus_root, parsed_texts=parsed_texts)
    )
    cases: list[dict[str, Any]] = []
    unmatched: list[MergedRecord] = []
    dropped = 0
    per_source: dict[str, int] = {}
    for record in merged:
        if contains_personal_data(record.query) or any(
            contains_personal_data(a) for a in record.answers
        ):
            dropped += 1
            continue
        expected = pick_expected(
            answers=record.answers,
            parsed_texts=parsed_texts,
            origin_document=record.origin_document,
            max_docs=int(spec.get("max_docs", DEFAULT_MAX_DOCS)),
            origin_first=record.origin_is_target,
        )
        if not expected:
            unmatched.append(record)
            continue
        per_source[record.source] = per_source.get(record.source, 0) + 1
        cases.append(_case_dict(record, expected, index=per_source[record.source]))
    extra = _extra_cases(spec.get("extra_cases", ()), relative_to=out.parent)
    extra_problems = verify_anchors(extra, parsed_texts)
    _write_dataset(spec, cases + extra, out=out)
    _write_review(
        review,
        unmatched=unmatched,
        dropped=dropped,
        counts=counts,
        spec_name=str(spec["name"]),
        extra_problems=extra_problems,
    )
    load_dataset(out)  # 产出必须能被加载器读回,否则在这里炸
    return {
        **counts,
        "matched": len(cases),
        "unmatched": len(unmatched),
        "dropped_personal": dropped,
        "extra": len(extra),
        "extra_anchor_problems": len(extra_problems),
        "cases": len(cases) + len(extra),
    }


def verify_anchors(
    cases: Sequence[Mapping[str, Any]], parsed_texts: Mapping[str, str]
) -> list[str]:
    """手写样本的核对:期望文档必须在解析文本里、锚文本必须逐字 ``in`` 得到。一条问题一行。"""
    problems: list[str] = []
    for case in cases:
        for exp in case.get("expected", []):
            document, anchor = str(exp.get("document")), str(exp.get("anchor"))
            text = parsed_texts.get(document)
            if text is None:
                problems.append(f"{case.get('id')}: document not in parsed corpus: {document}")
            elif anchor not in text:
                problems.append(f"{case.get('id')}: anchor not found in {document}: {anchor[:40]}")
    return problems


def _case_dict(
    record: MergedRecord, expected: Sequence[KbExpected], *, index: int
) -> dict[str, Any]:
    return {
        "id": f"{record.source}-{index:04d}",
        "query": record.query,
        "source": record.source,
        "tags": list(record.tags),
        "expected": [{"document": e.document, "anchor": e.anchor, "page": None} for e in expected],
        "answer": record.answers[0],
    }


def _extra_cases(paths: Iterable[str], *, relative_to: Path) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    for raw in paths:
        path = Path(raw)
        if not path.is_absolute():
            path = relative_to / path
        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        cases.extend(loaded.get("cases", []))
    return cases


def _write_dataset(
    spec: Mapping[str, Any], cases: Sequence[Mapping[str, Any]], *, out: Path
) -> None:
    corpus = dict(spec.get("corpus", {}))
    payload = {"name": spec["name"], "corpus": corpus, "cases": list(cases)}
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        yaml.safe_dump(payload, allow_unicode=True, sort_keys=False, width=1000), encoding="utf-8"
    )


def _write_review(
    review: Path,
    *,
    unmatched: Sequence[MergedRecord],
    dropped: int,
    counts: Mapping[str, int],
    spec_name: str,
    extra_problems: Sequence[str] = (),
) -> None:
    lines = [
        f"# {spec_name} 待人工处理清单",
        "",
        "解析计数:" + ", ".join(f"{k}={v}" for k, v in counts.items()),
        f"含个人信息被剔除(不列内容):{dropped} 条",
        "",
        "## 解析备注",
        "",
        *(f"- {note}" for note in _PARSE_NOTES),
        "",
        f"## 手写样本核对不过的({len(extra_problems)} 条)",
        "",
        *(f"- {problem}" for problem in extra_problems),
        "",
        f"## 配不出锚文本的问题({len(unmatched)} 条)",
        "",
        "答案句在语料解析文本里一处都没找到:要么答案是客户自己写的、语料里没有,要么出处文档不在清单层里。",
        "逐条决定:补 `expected`(手填文档 + 锚)、改成 `no_answer`,或丢弃。",
        "",
        "| # | 来源 | 出处 | 问题 |",
        "|---|---|---|---|",
    ]
    lines.extend(
        f"| {i} | {r.source} | {r.origin_document or '-'} | {r.query} |"
        for i, r in enumerate(unmatched, 1)
    )
    review.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--spec", type=Path, required=True, help="sources YAML")
    parser.add_argument("--corpus-root", type=Path, required=True, help="资料根目录(仓库外)")
    parser.add_argument("--manifest", type=Path, required=True, help="清点脚本产出的 manifest.csv")
    parser.add_argument("--parsed-cache", type=Path, required=True, help="解析文本缓存目录(仓库外)")
    parser.add_argument("--out", type=Path, required=True, help="评测集 YAML 输出路径")
    parser.add_argument("--review", type=Path, required=True, help="待人工处理清单输出路径")
    parser.add_argument("--workers", type=int, default=None, help="解析进程数(默认 CPU 数)")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    spec = yaml.safe_load(args.spec.read_text(encoding="utf-8"))
    stats = build_dataset(
        spec,
        corpus_root=args.corpus_root.expanduser(),
        manifest=args.manifest.expanduser(),
        parsed_cache=args.parsed_cache.expanduser(),
        out=args.out,
        review=args.review,
        workers=args.workers,
    )
    print(" ".join(f"{k}={v}" for k, v in stats.items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
