"""clinconsensus.py 的确定性部分:计分公式、选题、拼题干、汇总。不连任何模型。"""

from __future__ import annotations

from typing import Any

import pytest
from clinconsensus import cacs, select_cases, summarize, user_message


def _case(case_id: str, role: str) -> dict[str, Any]:
    return {"case_id": case_id, "user_role": role, "clinical_context": "", "user_request": "q"}


@pytest.mark.parametrize(
    ("met", "expected"),
    [
        (0, 0.0),
        (9, 0.0),  # 不到阈值,整题 0 分
        (10, 100.0 / 21),  # 刚到阈值,21 档里只达到第 1 档
        (20, 100.0 * 11 / 21),
        (30, 100.0),
    ],
)
def test_cacs_matches_official_formula(met: int, expected: float) -> None:
    assert cacs(met, 30, 10) == pytest.approx(expected)


def test_cacs_threshold_above_rubric_count_is_zero() -> None:
    assert cacs(5, 4, 10) == 0.0


def test_user_message_joins_context_and_request() -> None:
    assert user_message({"clinical_context": " 背景 ", "user_request": "问题"}) == "背景\n\n问题"
    assert user_message({"clinical_context": None, "user_request": "问题"}) == "问题"


def test_select_cases_splits_lay_and_professional_reproducibly() -> None:
    rows = [_case(f"L{i}", "患者") for i in range(20)] + [
        _case(f"P{i}", "消化科医生") for i in range(20)
    ]
    first = select_cases(rows, 10, seed=1)
    assert [c["case_id"] for c in first] == [c["case_id"] for c in select_cases(rows, 10, seed=1)]
    assert sum(c["case_id"].startswith("L") for c in first) == 5
    assert sum(c["case_id"].startswith("P") for c in first) == 5
    assert sum(c["case_id"].startswith("L") for c in select_cases(rows, 7, seed=1)) == 4


def test_summarize_counts_judge_errors_separately_and_splits_registers() -> None:
    cases = {"A": _case("A", "患者家属"), "B": _case("B", "医生")}
    judged = [
        {
            "case_id": "A",
            "judgments": [{"criteria_met": True}] * 12 + [{"criteria_met": False}] * 18,
        },
        {
            "case_id": "B",
            "judgments": [{"criteria_met": True}] * 6
            + [{"criteria_met": None}] * 2
            + [{"criteria_met": False}] * 22,
        },
    ]
    report = summarize(judged, cases)
    assert report["all"]["rubric_accuracy_pct"] == pytest.approx(30.0)  # (12 + 6) / 60
    assert report["all"]["pass_at_10_pct"] == pytest.approx(50.0)
    assert report["all"]["judge_errors"] == 2
    assert report["lay"]["cases"] == 1
    assert report["lay"]["cacs_at_10_pct"] == pytest.approx(round(100.0 * 3 / 21, 1))
    assert report["professional"]["cacs_at_10_pct"] == 0.0
