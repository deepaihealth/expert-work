"""B-140 对照:两份结果 → Markdown 对照表。

  uv run --no-sync python tools/eval/behavior_compare.py \\
      eval-out/b140/base.jsonl eval-out/b140/cand.jsonl \\
      [--out report.md] [--allow-different-cases]

退出码:有稳定退步 1;用例集不同(且没加 --allow-different-cases)2;否则 0。
规则见规格 §6:改动前 ≥2/3 过、改动后 ≤1/3 过才算稳定退步;infra_error 不计入。
"""

from __future__ import annotations

import argparse
import statistics
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from behavior_schema import CaseResult, ResultHeader, read_results  # noqa: E402

Verdict = Literal["regressed", "improved", "same", "flaky", "insufficient", "missing"]
METRICS = ("tokens_in", "tokens_out", "tool_calls", "wall_s")
METRIC_THRESHOLD = 0.20
_LABEL: dict[Verdict, str] = {
    "regressed": "❌ 稳定退步",
    "improved": "✅ 稳定进步",
    "same": "持平",
    "flaky": "抖动",
    "insufficient": "样本不足",
    "missing": "缺失",
}


@dataclass(frozen=True)
class CaseSummary:
    case_id: str
    n: int  # 不含 infra_error
    passed: int
    infra: int
    medians: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class Row:
    case_id: str
    base: CaseSummary | None
    cand: CaseSummary | None
    verdict: Verdict
    flags: list[str]


def summarize(results: list[CaseResult]) -> dict[str, CaseSummary]:
    by_case: dict[str, list[CaseResult]] = {}
    for r in results:
        by_case.setdefault(r.case_id, []).append(r)
    out: dict[str, CaseSummary] = {}
    for case_id, rows in by_case.items():
        scored = [r for r in rows if r.passed is not None]
        medians: dict[str, float] = {}
        for metric in METRICS:
            values = [r.metrics[metric] for r in scored if metric in r.metrics]
            if values:
                medians[metric] = float(statistics.median(values))
        out[case_id] = CaseSummary(
            case_id=case_id,
            n=len(scored),
            passed=sum(r.passed is True for r in scored),
            infra=len(rows) - len(scored),
            medians=medians,
        )
    return out


def classify(base: CaseSummary | None, cand: CaseSummary | None) -> Verdict:
    if base is None or cand is None:
        return "missing"
    if base.n < 2 or cand.n < 2:
        return "insufficient"
    base_good, base_bad = 3 * base.passed >= 2 * base.n, 3 * base.passed <= base.n
    cand_good, cand_bad = 3 * cand.passed >= 2 * cand.n, 3 * cand.passed <= cand.n
    if base_good and cand_bad:
        return "regressed"
    if base_bad and cand_good:
        return "improved"
    if base.passed * cand.n == cand.passed * base.n:
        return "same"
    return "flaky"


def metric_flags(base: CaseSummary, cand: CaseSummary) -> list[str]:
    flags = []
    for metric in METRICS:
        if metric in base.medians and metric in cand.medians and base.medians[metric] > 0:
            delta = (cand.medians[metric] - base.medians[metric]) / base.medians[metric]
            if abs(delta) >= METRIC_THRESHOLD:
                flags.append(f"{metric} {delta:+.0%}")
    return flags


def compare(
    base_header: ResultHeader,
    base_results: list[CaseResult],
    cand_header: ResultHeader,
    cand_results: list[CaseResult],
    *,
    allow_different_cases: bool = False,
) -> list[Row]:
    if base_header.case_set_hash != cand_header.case_set_hash and not allow_different_cases:
        raise ValueError(
            f"case sets differ ({base_header.case_set_hash} vs {cand_header.case_set_hash}); "
            "pass --allow-different-cases to compare anyway"
        )
    base, cand = summarize(base_results), summarize(cand_results)
    rows = []
    for case_id in sorted(set(base) | set(cand)):
        b, c = base.get(case_id), cand.get(case_id)
        rows.append(Row(case_id, b, c, classify(b, c), metric_flags(b, c) if b and c else []))
    return rows


def _cell(s: CaseSummary | None) -> str:
    if s is None:
        return "—"
    return f"{s.passed}/{s.n}" + (f"(另 {s.infra} 次环境错误)" if s.infra else "")


def render_markdown(
    base_header: ResultHeader,
    cand_header: ResultHeader,
    rows: list[Row],
    cand_results: list[CaseResult],
) -> str:
    lines = [
        "# B-140 行为回归对照",
        "",
        "| | 改动前 | 改动后 |",
        "|---|---|---|",
        f"| 标签 | {base_header.label} | {cand_header.label} |",
        f"| 时间 | {base_header.started_at} | {cand_header.started_at} |",
        f"| 备注 | {base_header.note} | {cand_header.note} |",
        f"| 智能体替换 | {base_header.agent_map or '—'} | {cand_header.agent_map or '—'} |",
        f"| 用例集 | {base_header.case_set_hash} | {cand_header.case_set_hash} |",
        f"| 每个用例次数 | {base_header.repeats} | {cand_header.repeats} |",
        "",
    ]
    counts = Counter(r.verdict for r in rows)
    lines.append("结论:" + "、".join(f"{_LABEL[v]} {counts[v]}" for v in _LABEL if counts[v]))
    lines += [
        "",
        "| 用例 | 改动前 | 改动后 | 结论 | 指标变化(中位数,≥20%) |",
        "|---|---|---|---|---|",
    ]
    for r in rows:
        cells = [r.case_id, _cell(r.base), _cell(r.cand), _LABEL[r.verdict], "、".join(r.flags)]
        lines.append("| " + " | ".join(cells) + " |")
    # 规格 §6:每个任务每次的判定明细 —— 只要改动后有一次没过就列出来,不只退步 / 抖动。
    failing = {r.case_id for r in cand_results if r.passed is not True}
    detail = [r for r in rows if r.case_id in failing]
    if detail:
        lines += ["", "## 改动后没过的判据", ""]
        for r in detail:
            for res in sorted(
                (x for x in cand_results if x.case_id == r.case_id), key=lambda x: x.rep
            ):
                if res.passed is None:
                    state = f"环境错误 {res.infra_error or ''}"
                elif res.passed:
                    state = "过"
                else:
                    state = "不过: " + "; ".join(
                        f"{v.type}({v.detail})" for v in res.verdicts if not v.passed
                    )
                lines.append(f"- {r.case_id} 第 {res.rep} 次:{state}")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("base")
    parser.add_argument("cand")
    parser.add_argument("--out", default="")
    parser.add_argument("--allow-different-cases", action="store_true")
    args = parser.parse_args(argv)
    base_header, base_results = read_results(Path(args.base))
    cand_header, cand_results = read_results(Path(args.cand))
    try:
        rows = compare(
            base_header,
            base_results,
            cand_header,
            cand_results,
            allow_different_cases=args.allow_different_cases,
        )
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 2
    report = render_markdown(base_header, cand_header, rows, cand_results)
    print(report)
    if args.out:
        Path(args.out).write_text(report, encoding="utf-8")
    return 1 if any(r.verdict == "regressed" for r in rows) else 0


if __name__ == "__main__":
    raise SystemExit(main())
