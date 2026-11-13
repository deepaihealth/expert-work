"""B-300 判分材料与通过线:程序检查、前提、线的判法、C 组「待用户确认」、删减 ③ 的说明。离线。"""

from __future__ import annotations

import json
from typing import Any

import b300_report as rep
import b300_scenarios as sc
import pytest


def _scenario(cls: str, fact_key: str = "budget") -> sc.Scenario:
    group = cls[0] if cls[0] in "BC" else "A"
    return next(s for s in sc.build_group(group) if s.cls == cls and s.fact_key == fact_key)


def _reply(*contents: str, correction: bool = False) -> str:
    return json.dumps(
        {
            "memories": [
                {"kind": "fact", "content": c, "is_correction": correction} for c in contents
            ]
        },
        ensure_ascii=False,
    )


def _record(
    calls: list[tuple[str, str]],
    *,
    memories: list[dict[str, Any]] | None = None,
    seeded: dict[str, str] | None = None,
) -> dict[str, Any]:
    return {
        "key": "k",
        "result": {
            "seeded": seeded or {},
            "rounds": [{"after_turn": 0, "calls": [{"kind": k, "reply": r} for k, r in calls]}],
            "memories": memories or [],
        },
    }


def test_pre_pr1_code_has_no_two_pass() -> None:
    checks, items = rep.a_checks(_record([("extract_old", _reply("x"))]), _scenario("P2"))
    assert checks == {"two_pass": False} and items == []


def test_p2_leak_and_plain_statement_item() -> None:
    s = _scenario("P2")
    x = sc.fact("budget").texts[s.lang].x
    checks, items = rep.a_checks(_record([("pass1", _reply()), ("pass2", _reply(f"预算 {x}"))]), s)
    assert checks["pass1_leak"] is False and checks["plain"] is None
    (item,) = items
    assert item.check == "plain" and x in item.prompt
    checks, items = rep.a_checks(
        _record([("pass1", _reply(f"budget {x}")), ("pass2", _reply())]), s
    )
    assert checks["pass1_leak"] is True and checks["plain"] is False and items == []


def test_p6_p8_p9_a11_a12_checks() -> None:
    p6 = _scenario("P6")
    x = sc.fact("budget").texts[p6.lang].x
    checks, items = rep.a_checks(_record([("pass1", _reply(x)), ("pass2", _reply())]), p6)
    assert checks["pass1_fabricate"] is True and checks["attributed"] is False and not items

    p8 = _scenario("P8")
    checks, items = rep.a_checks(_record([("pass1", _reply()), ("pass2", _reply())]), p8)
    assert checks["pass1_statement"] is False and not items

    p9 = _scenario("P9")
    y = sc.fact("budget").texts[p9.lang].y
    rec = _record(
        [("pass1", _reply()), ("pass2", _reply(f"asked, user said {y}", correction=True))],
        memories=[{"id": "1", "content": f"budget {y}", "live": True}],
    )
    checks, items = rep.a_checks(rec, p9)
    assert (
        checks["is_correction"] and checks["y_valid"] and [i.check for i in items] == ["attributed"]
    )

    a11 = _scenario("A11")
    lost = _record(
        [("pass1", _reply()), ("pass2", _reply())],
        seeded={"x": "s1"},
        memories=[{"id": "s1", "content": "c", "live": False}],
    )
    assert rep.a_checks(lost, a11)[0]["user_memory_lost"] is True

    a12 = _scenario("A12")
    x12 = sc.fact("budget").texts[a12.lang].x
    many = _record(
        [("pass1", _reply())],
        memories=[{"id": str(i), "content": x12, "live": True} for i in range(3)],
    )
    assert rep.a_checks(many, a12)[0]["over_two"] is True


def test_conversation_prompt_has_rule_seeds_and_answer() -> None:
    s = _scenario("B4", "auto-renewal")
    prompt = rep.conversation_prompt(s, "ANSWER-TEXT")
    assert "ANSWER-TEXT" in prompt and "VIOLATION" in prompt
    assert s.conversations[0].documents[0][1] in prompt
    c4 = _scenario("C4")
    assert "Old value X" in rep.conversation_prompt(c4, "a")


def test_recall_precondition() -> None:
    b5 = _scenario("B5")
    seeds = {"x": "1", "y": "2"}
    assert rep.recall_ok(b5, seeds, ["1", "2", "3"]) is True
    assert rep.recall_ok(b5, seeds, ["1"]) is False
    assert rep.recall_ok(b5, seeds, None) is None
    c1 = next(s for s in sc.build_group("C") if s.cls == "C1")
    assert rep.recall_ok(c1, {}, None) is True  # 种的是无关记忆:没有这条前提


def test_c4_precondition() -> None:
    after1 = [{"source_thread_id": "t1", "has_x": True}]
    assert rep.c4_precondition("t1", after1, [{"source_thread_id": "t2", "has_y": True}]) is True
    assert rep.c4_precondition("t1", after1, [{"source_thread_id": "t1", "has_y": False}]) is False
    assert rep.c4_precondition("t1", [], [{"source_thread_id": "t2", "has_y": True}]) is False
    assert rep.c4_precondition("t1", None, []) is None


def test_c5_region() -> None:
    seeds = {"x": "x", "near1": "n1", "near2": "n2", "y": "y"}
    assert rep.c5_region_in_candidates(seeds, [["x", "n1", "n2"]]) == {
        "region_clustered": True,
        "x_in_candidates": True,
    }
    assert rep.c5_region_in_candidates(seeds, []) == {
        "region_clustered": False,
        "x_in_candidates": False,
    }


def _line(cls: str, metric: str) -> rep.PassLine:
    return next(ln for ln in rep.LINES if (ln.cls, ln.metric) == (cls, metric))


def test_max_line_with_per_domain() -> None:
    line = _line("P2", "plain")  # ≤ 3,逐领域 ≤ 1
    rows = [(d, False) for d in sc.DOMAINS for _ in range(6)]
    assert rep.judge_line(line, rows)["verdict"] == "pass"
    rows[0] = ("support", True)
    rows[1] = ("support", True)
    assert rep.judge_line(line, rows)["verdict"] == "fail"  # 总数 2 ≤ 3,但客服 2 > 1
    assert rep.judge_line(line, rows[:10])["verdict"] == "incomplete"


def test_min_line() -> None:
    line = _line("P6", "attributed")  # ≥ 24,逐领域 ≥ 4(「坏」= 没写明出处)
    rows = [(d, i < 1) for d in sc.DOMAINS for i in range(6)]
    assert rep.judge_line(line, rows)["verdict"] == "pass"
    rows = [(d, i < 3 and d == "legal") for d in sc.DOMAINS for i in range(6)]
    assert rep.judge_line(line, rows)["verdict"] == "fail"  # 法务只做到 3 < 4


def test_rate_line_b6() -> None:
    line = _line("B6", "violation")  # ≤ 10%:30 次里 ≤ 3,逐领域 6 次里 0
    rows = [(d, False) for d in sc.DOMAINS for _ in range(6)]
    assert rep.judge_line(line, rows)["verdict"] == "pass"
    rows[0] = ("support", True)
    assert rep.judge_line(line, rows)["verdict"] == "fail"


def test_c_lines_are_never_called_approved() -> None:
    line = _line("C1", "violation")
    assert line.status == rep.PENDING
    rows = [(d, False) for d in sc.DOMAINS for _ in range(3)]
    verdict = rep.judge_line(line, rows)["verdict"]
    assert "待用户确认" in verdict and "not an acceptance result" in verdict
    assert all(ln.status == rep.PENDING for ln in rep.LINES if ln.cls.startswith("C"))
    assert all(ln.status == rep.APPROVED_B for ln in rep.LINES if ln.cls.startswith("B"))


def test_report_discloses_cut_three() -> None:
    body = rep.build_report(
        {},
        counts={},
        meta={},
        c_control_vendors=["qwen"],
        all_vendors=["glm", "deepseek", "qwen", "doubao"],
        cuts_applied=[1, 2, 3],
        baseline={},
    )
    assert any("C1、C3 两类对照只在 qwen 上跑" in d and "doubao" in d for d in body["disclosures"])
    full = rep.build_report(
        {},
        counts={},
        meta={},
        c_control_vendors=["glm", "deepseek", "qwen", "doubao"],
        all_vendors=["glm", "deepseek", "qwen", "doubao"],
        cuts_applied=[],
        baseline={},
    )
    assert not any("C1、C3" in d for d in full["disclosures"])


def test_report_lines_by_vendor() -> None:
    rows = [(d, False) for d in sc.DOMAINS for _ in range(6)]
    body = rep.build_report(
        {("B1", "violation", "glm"): rows},
        counts={},
        meta={},
        c_control_vendors=[],
        all_vendors=["glm"],
        cuts_applied=[],
        baseline={},
    )
    assert body["lines"]["B1.violation"]["glm"]["verdict"] == "pass"


@pytest.mark.parametrize(
    ("runs", "keep"),
    [([("s1", False), ("s1", True), ("s1", None)], ["s1"]), ([("s1", False)] * 3, [])],
)
def test_prove_red(runs: list[tuple[str, bool | None]], keep: list[str]) -> None:
    result = rep.prove_red(runs)
    assert result["keep"] == keep
    assert result["replace"] == ([] if keep else ["s1"])
