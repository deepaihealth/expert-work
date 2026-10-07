"""B-140 behavior_compare 单元测试。"""

from __future__ import annotations

from pathlib import Path

import behavior_compare as cmp
import pytest
from behavior_schema import CaseResult, CheckVerdict, ResultHeader, append_line


def _results(case_id: str, outcomes: list[bool | None], tokens: float = 1000.0) -> list[CaseResult]:
    out = []
    for rep, ok in enumerate(outcomes, start=1):
        verdicts = (
            []
            if ok is None
            else [CheckVerdict(type="tool_used", passed=ok, detail="" if ok else "tools used: []")]
        )
        out.append(
            CaseResult(
                case_id=case_id,
                rep=rep,
                passed=ok,
                verdicts=verdicts,
                metrics={} if ok is None else {"tokens_in": tokens, "tool_calls": 3.0},
                infra_error="cut" if ok is None else None,
            )
        )
    return out


def _header(label: str, h: str = "a" * 12) -> ResultHeader:
    return ResultHeader(
        label=label,
        started_at="t",
        base_url="http://localhost",
        case_set_hash=h,
        repeats=3,
        case_ids=[],
    )


def test_summarize_excludes_infra_and_takes_medians() -> None:
    s = cmp.summarize(_results("g01", [True, None, False], tokens=900.0))["g01"]
    assert (s.n, s.passed, s.infra) == (2, 1, 1)
    assert s.medians == {"tokens_in": 900.0, "tool_calls": 3.0}


@pytest.mark.parametrize(
    ("base", "cand", "verdict"),
    [
        ([True] * 3, [False] * 3, "regressed"),
        ([True, True, False], [True, False, False], "regressed"),
        ([True] * 3, [True, True, False], "flaky"),
        ([False] * 3, [True] * 3, "improved"),
        ([True, True, False], [False, True, True], "same"),
        ([True, None, None], [True] * 3, "insufficient"),
    ],
)
def test_classify(base: list[bool | None], cand: list[bool | None], verdict: str) -> None:
    b = cmp.summarize(_results("g01", base))["g01"]
    c = cmp.summarize(_results("g01", cand))["g01"]
    assert cmp.classify(b, c) == verdict


def test_classify_missing() -> None:
    b = cmp.summarize(_results("g01", [True] * 3))["g01"]
    assert cmp.classify(b, None) == "missing"


def test_metric_flags_threshold() -> None:
    b = cmp.summarize(_results("g01", [True] * 3, tokens=1000.0))["g01"]
    assert cmp.metric_flags(
        b, cmp.summarize(_results("g01", [True] * 3, tokens=1350.0))["g01"]
    ) == ["tokens_in +35%"]
    assert (
        cmp.metric_flags(b, cmp.summarize(_results("g01", [True] * 3, tokens=1100.0))["g01"]) == []
    )


def test_compare_rejects_different_case_sets() -> None:
    with pytest.raises(ValueError, match="case sets differ"):
        cmp.compare(_header("a"), [], _header("b", "b" * 12), [])
    assert (
        cmp.compare(_header("a"), [], _header("b", "b" * 12), [], allow_different_cases=True) == []
    )


def test_main_exit_codes_and_report(tmp_path: Path) -> None:
    def write(name: str, header: ResultHeader, results: list[CaseResult]) -> Path:
        p = tmp_path / name
        append_line(p, header)
        for r in results:
            append_line(p, r)
        return p

    base = write(
        "base.jsonl", _header("base"), _results("g01", [True] * 3) + _results("g02", [True] * 3)
    )
    same = write(
        "same.jsonl", _header("same"), _results("g01", [True] * 3) + _results("g02", [True] * 3)
    )
    worse = write(
        "worse.jsonl", _header("worse"), _results("g01", [True] * 3) + _results("g02", [False] * 3)
    )
    other = write("other.jsonl", _header("other", "c" * 12), [])
    report = tmp_path / "r.md"
    assert cmp.main([str(base), str(same)]) == 0
    assert cmp.main([str(base), str(worse), "--out", str(report)]) == 1
    text = report.read_text(encoding="utf-8")
    assert "| g02 | 3/3 | 0/3 | ❌ 稳定退步 |" in text
    assert "g02 第 1 次:不过: tool_used(tools used: [])" in text
    assert cmp.main([str(base), str(other)]) == 2


def test_report_lists_every_failed_run_not_only_regressions(tmp_path: Path) -> None:
    rows = cmp.compare(
        _header("a"),
        _results("g01", [True] * 3),
        _header("b"),
        _results("g01", [True, True, False]),
    )
    assert rows[0].verdict == "flaky"
    same = cmp.compare(
        _header("a"),
        _results("g03", [True, False, False]),
        _header("b"),
        _results("g03", [False, True, False]),
    )
    text = cmp.render_markdown(
        _header("a"), _header("b"), same, _results("g03", [False, True, False])
    )
    assert "g03 第 1 次:不过" in text and "g03 第 3 次:不过" in text


def _indeterminate(case_id: str, rep: int) -> CaseResult:
    return CaseResult(
        case_id=case_id,
        rep=rep,
        passed=None,
        verdicts=[CheckVerdict(type="completed", passed=False, detail="x")],
        metrics={"tokens_in": 99999.0, "compactions": 0.0},
        indeterminate="no compaction in any turn",
    )


def test_indeterminate_runs_are_missing_data_not_failures() -> None:
    rows = [*_results("c01", [True, True]), _indeterminate("c01", 3)]
    s = cmp.summarize(rows)["c01"]
    assert (s.n, s.passed, s.infra, s.indeterminate) == (2, 2, 0, 1)
    assert s.medians["tokens_in"] == 1000.0  # 不可判那次的指标不进中位数
    # 改动后两次不可判、一次过:样本不足,不是「从 3/3 掉到 1/3」的稳定退步
    base = cmp.summarize(_results("c01", [True] * 3))["c01"]
    cand = cmp.summarize(
        [*_results("c01", [True]), _indeterminate("c01", 2), _indeterminate("c01", 3)]
    )["c01"]
    assert cmp.classify(base, cand) == "insufficient"


def test_report_shows_indeterminate_counts() -> None:
    cand_results = [*_results("c01", [True, True]), _indeterminate("c01", 3)]
    rows = cmp.compare(_header("a"), _results("c01", [True] * 3), _header("b"), cand_results)
    assert rows[0].verdict == "same"
    text = cmp.render_markdown(_header("a"), _header("b"), rows, cand_results)
    assert "| c01 | 3/3 | 2/2(另 1 次不可判) | 持平 |" in text
    assert "c01 第 3 次:不可判 no compaction in any turn" in text
