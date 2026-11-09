"""知识库智能体层评测的统计(B-206 11-09,从 ``kb_agent_eval.py`` 拆出)。

头条三个数**总是一起报**(11-05 研究:任何防编造的改动都会抬高误拒,单看一个会选错):

* 资料没写(含难负例):**该拒拒了**的比例 + 编造题数(编造另按 资料没写 / 难负例 / 部分写了 /
  有答案 四组各数一遍,再给合计);
* 有答案(含近名干扰题):**不该拒却拒了**的比例;
* 部分写了(``partial``):**B 式作答**的比例。

同一份结果按答题模型分节;给几个模型的结果文件就多一张「分模型对比」表。
旧结果行(没有 ``label`` / ``agreement``)按 ``answerable`` 推标签,照样能统计。
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))

from kb_agent_judge import CATEGORIES, DISAGREE, FAILED  # type: ignore[import-not-found]

_LABEL_TITLES = {"no_answer": "资料没写", "answerable": "有答案", "partial": "部分写了"}
#: 分集按这个顺序列,其余集名按字母排在后面。
_SET_ORDER = ("main", "chat", "hard_negative", "distractor")

Rate = tuple[int, int]


def read_results(path: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """``(每题一行, 每次运行末尾的 run 行)``;文件不存在时两个空列表。"""
    rows: list[dict[str, Any]] = []
    runs: list[dict[str, Any]] = []
    if not path.exists():
        return rows, runs
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        item = json.loads(line)
        if "case_id" in item:
            rows.append(item)
        elif "run" in item:
            runs.append(item["run"])
    return rows, runs


def row_label(row: Mapping[str, Any]) -> str:
    label = row.get("label")
    if isinstance(label, str):
        return label
    return "answerable" if row.get("answerable") else "no_answer"


def headline_rates(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """``abstain`` / ``false_refusal`` / ``style_b`` 是 ``(分子, 分母)``;分母 = 该标签全部题
    (判分失败 / 分歧也在分母里,另外单列)。"""
    by_label: dict[str, Counter[str]] = {label: Counter() for label in CATEGORIES}
    for row in rows:
        by_label.setdefault(row_label(row), Counter())[row["category"]] += 1
    no_answer, answerable, partial = (by_label[k] for k in ("no_answer", "answerable", "partial"))
    fabricated_by = Counter(_fabrication_group(r) for r in rows if r["category"] == "编造")
    return {
        "fabricated_by": {k: fabricated_by[k] for k in _FAB_GROUPS},
        "abstain": (no_answer["正确说没写"], no_answer.total()),
        "fabricated": no_answer["编造"],
        "false_refusal": (answerable["误拒"], answerable.total()),
        "style_b": (partial["B式作答"], partial.total()),
        "disagree": sum(1 for r in rows if r["category"] == DISAGREE),
        "failed": sum(1 for r in rows if r["category"] == FAILED),
    }


#: 编造按这几组分开数(11-09 评审:只数「资料没写」会把改标 partial 的题、有答案题上的编造藏起来)。
_FAB_GROUPS = ("no_answer", "hard_negative", "partial", "answerable")


def _fabrication_group(row: Mapping[str, Any]) -> str:
    label = row_label(row)
    if label == "no_answer" and row.get("set") == "hard_negative":
        return "hard_negative"
    return label


def _fab_line(r: Mapping[str, Any]) -> str:
    by = r["fabricated_by"]
    return (
        f"  编造:资料没写 {by['no_answer']}、难负例 {by['hard_negative']}、"
        f"部分写了 {by['partial']}、有答案(含干扰){by['answerable']},合计 {sum(by.values())}"
    )


def _quote_misses(rows: Sequence[Mapping[str, Any]]) -> str:
    parts = []
    for label in ("no_answer", "partial", "answerable"):
        ids = [
            str(r["case_id"])
            for r in rows
            if row_label(r) == label and (r.get("verdict") or {}).get("quote_misses")
        ]
        shown = f"({', '.join(ids[:10])}{', …' if len(ids) > 10 else ''})" if ids else ""
        parts.append(f"{_LABEL_TITLES[label]} {len(ids)} 题{shown}")
    return "  引文在资料里没找到:" + ";".join(parts)


def _pct(rate: Rate) -> str:
    hit, n = rate
    return f"{hit / n:.1%}({hit}/{n})" if n else f"—({hit}/{n})"


def _unanimous(rows: Sequence[Mapping[str, Any]]) -> str:
    voted = [str(r["agreement"]) for r in rows if r.get("agreement")]
    if not voted:
        return "—"
    same = sum(1 for a in voted if a.split("/")[0] == a.split("/")[1])
    return f"{same}/{len(voted)}"


def _headline(rows: Sequence[Mapping[str, Any]]) -> list[str]:
    r = headline_rates(rows)
    return [
        "## 头条(三个数一起看)",
        f"  资料没写 n={r['abstain'][1]}:该拒拒了 {_pct(r['abstain'])},编造 {r['fabricated']}",
        f"  有答案 n={r['false_refusal'][1]}:不该拒却拒了 {_pct(r['false_refusal'])}",
        f"  部分写了 n={r['style_b'][1]}:B 式作答 {_pct(r['style_b'])}",
        _fab_line(r),
        f"  判分:分歧 {r['disagree']},失败 {r['failed']};全票一致 {_unanimous(rows)}",
        _quote_misses(rows),
    ]


def _sets(rows: Iterable[Mapping[str, Any]]) -> list[str]:
    present = {str(r["set"]) for r in rows}
    return [s for s in _SET_ORDER if s in present] + sorted(present - set(_SET_ORDER))


def _breakdown(rows: Sequence[Mapping[str, Any]]) -> list[str]:
    lines: list[str] = []
    for label, cats in CATEGORIES.items():
        mine = [r for r in rows if row_label(r) == label]
        if not mine:
            continue
        lines.append(f"## {_LABEL_TITLES[label]}")
        for scope in ("all", *_sets(mine)):
            sel = [r for r in mine if scope in ("all", r["set"])]
            counts = Counter(r["category"] for r in sel)
            pad = f"{scope:4}" if scope == "all" else scope
            lines.append(f"  {pad} n={len(sel)} " + " ".join(f"{k} {counts[k]}" for k in cats))
    total = Counter(r["category"] for r in rows)
    lines.append(f"total n={len(rows)} " + " ".join(f"{k} {v}" for k, v in sorted(total.items())))
    return lines


def _usage(rows: Sequence[Mapping[str, Any]], runs: Sequence[Mapping[str, Any]]) -> list[str]:
    lines: list[str] = []
    searches = [s for r in rows for s in r["searches"]]
    # B-206 K-2: 读文档单独统计(之前的结果行没有 reads)。
    reads = [s for r in rows for s in r.get("reads", ())]
    lines.append(
        f"searches per question {len(searches) / max(1, len(rows)):.2f}; "
        f"degraded {sum(1 for s in searches if s.get('degraded'))}, "
        f"embedding degraded {sum(1 for s in searches if s.get('embedding_degraded'))}, "
        f"keyword degraded {sum(1 for s in searches if s.get('keyword_degraded'))}, "
        f"errors {sum(1 for s in searches if s.get('error'))}, "
        f"skipped {sum(1 for s in searches if s.get('skipped'))}"
    )
    ran = [s for s in searches if "status" in s]
    outcomes = Counter(s.get("rerank_outcome") or "ok" for s in ran)
    lines.append(
        "rerank outcomes "
        + ", ".join(f"{k} {v}" for k, v in sorted(outcomes.items()))
        + f"; threshold paused {sum(1 for s in ran if s.get('rerank_threshold_paused'))}"
    )
    if reads:
        with_read = sum(1 for r in rows if r.get("reads"))
        modes = Counter(s["mode"] for s in reads if s.get("mode"))
        lines.append(
            f"reads per question {len(reads) / max(1, len(rows)):.2f} "
            f"(questions with a read {with_read}); modes "
            + ", ".join(f"{k} {v}" for k, v in sorted(modes.items()))
            + f"; errors {sum(1 for s in reads if s.get('error'))}, "
            f"skipped {sum(1 for s in reads if s.get('skipped'))}"
        )
    answer = sum(r["cost_cny"]["answer"] for r in rows)
    judge = sum(r["cost_cny"]["judge"] for r in rows)
    rerank = sum(run["spend_cny"]["rerank"] for run in runs)
    lines.append(
        f"spend ¥{answer + judge + rerank:.2f} (answer ¥{answer:.2f}, judge ¥{judge:.2f}, "
        f"rerank ¥{rerank:.2f} over {len(runs)} run(s))"
    )
    return lines


def _compare(by_model: Mapping[str, Sequence[Mapping[str, Any]]]) -> list[str]:
    lines = [
        "## 分模型对比",
        "  模型 | 该拒拒了 | 编造 没写/难负例/部分/有答案/合计 | 不该拒却拒了 | B 式作答",
    ]
    for model in sorted(by_model):
        r = headline_rates(by_model[model])
        by = r["fabricated_by"]
        fab = "/".join(str(by[k]) for k in _FAB_GROUPS) + f"/{sum(by.values())}"
        lines.append(
            f"  {model} | {_pct(r['abstain'])} | {fab} | "
            f"{_pct(r['false_refusal'])} | {_pct(r['style_b'])}"
        )
    return lines


def tally(paths: Path | Sequence[Path]) -> str:
    """一个或几个结果文件 → 按答题模型分节的统计(头条 / 分标签分集 / 检索与花费)。"""
    files = [paths] if isinstance(paths, Path) else list(paths)
    by_model: dict[str, list[dict[str, Any]]] = {}
    runs_by_model: dict[str, list[dict[str, Any]]] = {}
    for path in files:
        rows, runs = read_results(path)
        for row in rows:
            by_model.setdefault(str(row.get("answer_model")), []).append(row)
        for run in runs:
            runs_by_model.setdefault(str(run.get("answer_model")), []).append(run)
    lines: list[str] = []
    for model, rows in by_model.items():
        judges = sorted({str(r.get("judge_model")) for r in rows})
        lines.append(f"# 答题模型 {model}(判分 {', '.join(judges)})")
        lines += _headline(rows) + _breakdown(rows) + _usage(rows, runs_by_model.get(model, []))
    if len(by_model) > 1:
        lines += _compare(by_model)
    return "\n".join(lines)


__all__ = ["headline_rates", "read_results", "row_label", "tally"]
