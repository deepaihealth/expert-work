"""kb_agent_tally.py:头条三个数一起报 + 分模型(B-206 11-09)。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from kb_agent_tally import headline_rates, tally  # type: ignore[import-not-found]


def _row(
    case_id: str,
    label: str,
    cat: str,
    *,
    set_: str = "main",
    model: str = "glm-5.3",
    agreement: str = "3/3",
) -> dict[str, Any]:
    return {
        "case_id": case_id,
        "set": set_,
        "answerable": label == "answerable",
        "label": label,
        "category": cat,
        "agreement": agreement,
        "answer_model": model,
        "judge_model": "deepseek-v4-pro",
        "searches": [],
        "cost_cny": {"answer": 0.1, "judge": 0.05},
    }


def _write(path: Path, rows: list[dict[str, Any]]) -> Path:
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
    return path


_ROWS = [
    _row("n1", "no_answer", "正确说没写"),
    _row("n2", "no_answer", "编造", set_="chat"),
    _row("n3", "no_answer", "正确说没写", set_="hard_negative", agreement="2/3"),
    _row("n4", "no_answer", "判分分歧", agreement="1/3"),
    _row("a1", "answerable", "答对"),
    _row("a2", "answerable", "误拒", set_="distractor"),
    _row("p1", "partial", "B式作答", set_="chat"),
    _row("p2", "partial", "只说没写", set_="chat"),
]


def test_headline_rates_always_come_together() -> None:
    rates = headline_rates(_ROWS)
    assert rates["abstain"] == (2, 4)  # 该拒拒了
    assert rates["fabricated"] == 1
    assert rates["false_refusal"] == (1, 2)  # 不该拒却拒了
    assert rates["style_b"] == (1, 2)
    assert rates["disagree"] == 1


def test_tally_prints_the_three_headline_numbers_side_by_side(tmp_path: Path) -> None:
    text = tally([_write(tmp_path / "a.jsonl", _ROWS)])
    assert "资料没写 n=4:该拒拒了 50.0%(2/4),夹带推断 0,编造 1" in text
    assert "有答案 n=2:不该拒却拒了 50.0%(1/2)" in text
    assert "部分写了 n=2:B 式作答 50.0%(1/2)" in text
    assert "判分:分歧 1,失败 0;全票一致 6/8" in text


def test_tally_breaks_each_label_down_by_set_including_the_derived_sets(tmp_path: Path) -> None:
    text = tally([_write(tmp_path / "a.jsonl", _ROWS)]).splitlines()
    assert "  hard_negative n=1 正确说没写 1 夹带推断 0 编造 0 其它 0 判分失败 0 判分分歧 0" in text
    assert "  distractor n=1 答对 0 夹带推断 0 误拒 1 编造 0 答偏 0 判分失败 0 判分分歧 0" in text
    assert "  chat n=2 B式作答 1 夹带推断 0 只说没写 1 编造 0 其它 0 判分失败 0 判分分歧 0" in text


def test_tally_splits_by_answer_model_and_compares_them(tmp_path: Path) -> None:
    other = [{**r, "answer_model": "deepseek-v4-flash"} for r in _ROWS[:2]]
    text = tally([_write(tmp_path / "a.jsonl", _ROWS), _write(tmp_path / "b.jsonl", other)])
    assert "# 答题模型 glm-5.3" in text
    assert "# 答题模型 deepseek-v4-flash" in text
    assert "## 分模型对比" in text
    assert "  模型 | 该拒拒了 | 编造 没写/难负例/部分/有答案/合计 | 不该拒却拒了 | B 式作答" in text
    assert "  deepseek-v4-flash | 50.0%(1/2) | 1/0/0/0/1 | —(0/0) | —(0/0)" in text
    assert "  glm-5.3 | 50.0%(2/4) | 1/0/0/0/1 | 50.0%(1/2) | 50.0%(1/2)" in text


def test_rows_from_before_labels_are_read_by_answerable(tmp_path: Path) -> None:
    old = [{k: v for k, v in r.items() if k not in ("label", "agreement")} for r in _ROWS[:5]]
    text = tally([_write(tmp_path / "a.jsonl", old)])
    assert "资料没写 n=4:该拒拒了 50.0%(2/4),夹带推断 0,编造 1" in text
    assert "有答案 n=1:不该拒却拒了 0.0%(0/1)" in text


def _fab(case_id: str, label: str, set_: str, *, quote_misses: int = 0) -> dict[str, Any]:
    row = _row(case_id, label, "编造", set_=set_)
    row["verdict"] = {"fabricated": True, "quote_misses": quote_misses}
    return row


def test_fabrication_is_counted_for_every_label_not_only_no_answer(tmp_path: Path) -> None:
    # 11-09 评审:0006~0008 改标 partial 后,只数「资料没写」的编造会把它们藏起来
    rows = [
        _fab("n1", "no_answer", "chat"),
        _fab("h1", "no_answer", "hard_negative"),
        _fab("h2", "no_answer", "hard_negative"),
        _fab("p1", "partial", "chat"),
        _fab("a1", "answerable", "main"),
        _fab("d1", "answerable", "distractor"),
    ]
    rates = headline_rates(rows)
    assert rates["fabricated_by"] == {
        "no_answer": 1,
        "hard_negative": 2,
        "partial": 1,
        "answerable": 2,
    }
    text = tally([_write(tmp_path / "a.jsonl", rows)])
    assert "  编造:资料没写 1、难负例 2、部分写了 1、有答案(含干扰)2,合计 6" in text


def test_quote_misses_are_listed_per_label_with_case_ids(tmp_path: Path) -> None:
    rows = [
        _fab("n1", "no_answer", "chat", quote_misses=2),
        _fab("h1", "no_answer", "hard_negative", quote_misses=1),
        {**_row("p1", "partial", "只说没写"), "verdict": {"quote_misses": 1}},
        _row("a1", "answerable", "答对"),
    ]
    text = tally([_write(tmp_path / "a.jsonl", rows)])
    assert "  待复核·引文在资料里没找到:资料没写 2 题(n1, h1);部分写了 1 题(p1);有答案 0 题" in text


def test_quotes_that_cannot_be_checked_are_listed_apart(tmp_path: Path) -> None:
    rows = [
        {**_row("n1", "no_answer", "正确说没写"), "verdict": {"quote_unchecked": 1}},
        {**_row("a1", "answerable", "答对"), "verdict": {"quote_unchecked": 2}},
        _row("a2", "answerable", "答对"),
    ]
    text = tally([_write(tmp_path / "a.jsonl", rows)])
    assert "  引文空或太短、核对不了:资料没写 1 题(n1);部分写了 0 题;有答案 1 题(a1)" in text


def test_the_tally_says_which_production_prompt_parts_each_run_had(tmp_path: Path) -> None:
    rows = [_row("a1", "answerable", "答对")]
    runs = [
        {"run": {"answer_model": "glm-5.3", "spend_cny": {"rerank": 0.0}, "prod_prompt_parts": []}},
        {
            "run": {
                "answer_model": "glm-5.3",
                "spend_cny": {"rerank": 0.0},
                "prod_prompt_parts": ["answer_check", "outgoing_reference"],
            }
        },
    ]
    path = tmp_path / "a.jsonl"
    path.write_text(
        "".join(json.dumps(x, ensure_ascii=False) + "\n" for x in [*rows, *runs]),
        encoding="utf-8",
    )
    text = tally([path])
    assert "照生产加的提示段:无(基线) | answer_check, outgoing_reference" in text


# ---------------------------------------------------------------------------
# 11-10:夹带推断单列、复核剔除不进分母、断言级「有依据的断言占比」
# ---------------------------------------------------------------------------


def test_carrying_inference_is_counted_apart_for_each_label(tmp_path: Path) -> None:
    rows = [
        *_ROWS,
        _row("n5", "no_answer", "夹带推断"),
        _row("a3", "answerable", "夹带推断"),
        _row("p3", "partial", "夹带推断", set_="chat"),
    ]
    rates = headline_rates(rows)
    assert rates["carrying"] == {"no_answer": 1, "answerable": 1, "partial": 1}
    assert rates["abstain"] == (2, 5)  # 夹带推断不算「该拒拒了」,但在分母里
    text = tally([_write(tmp_path / "a.jsonl", rows)])
    assert "资料没写 n=5:该拒拒了 40.0%(2/5),夹带推断 1,编造 1" in text
    assert "有答案 n=3:不该拒却拒了 33.3%(1/3),夹带推断 1" in text
    assert "部分写了 n=3:B 式作答 33.3%(1/3),夹带推断 1" in text


def _excluded(case_id: str) -> dict[str, Any]:
    row = _row(case_id, "no_answer", "excluded_answer_still_present", set_="hard_negative")
    return {**row, "cost_cny": {"answer": 0.0, "judge": 0.2}}


def test_rows_excluded_by_the_recheck_stay_out_of_every_denominator(tmp_path: Path) -> None:
    rows = [*_ROWS, _excluded("hn-x1"), _excluded("hn-x2")]
    assert headline_rates(rows) == headline_rates(_ROWS)
    text = tally([_write(tmp_path / "a.jsonl", rows)])
    assert "资料没写 n=4:该拒拒了 50.0%(2/4),夹带推断 0,编造 1" in text
    assert "  复核剔除(答前检索判为答案仍在库里,不答、不进分母):2 题(hn-x1, hn-x2)" in text
    assert "  hard_negative n=1 正确说没写 1" in text  # 剔除的两题不在分集里
    assert "judge ¥0.80" in text  # 复核的判分花费照算


def _claims(case_id: str, set_: str, grounded: int, counted: int) -> dict[str, Any]:
    row = _row(case_id, "answerable", "答对", set_=set_)
    return {**row, "verdict": {"grounded_claims": grounded, "counted_claims": counted}}


def test_grounded_claim_share_is_reported_per_set_and_per_file(tmp_path: Path) -> None:
    before = _write(
        tmp_path / "before.jsonl", [_claims("a1", "main", 3, 4), _claims("a2", "chat", 1, 4)]
    )
    after = _write(tmp_path / "after.jsonl", [_claims("a1", "main", 4, 4)])
    text = tally([before, after])
    assert "  有依据的断言占比:all 66.7%(8/12);main 87.5%(7/8);chat 25.0%(1/4)" in text
    assert "## 有依据的断言占比(按文件、按集;不需要出处的断言不进分母)" in text
    assert f"  {before} | all 50.0%(4/8) | main 75.0%(3/4) | chat 25.0%(1/4)" in text
    assert f"  {after} | all 100.0%(4/4) | main 100.0%(4/4)" in text


def test_per_question_usage_leaves_out_rows_excluded_by_the_recheck(tmp_path: Path) -> None:
    kept = [
        {**_row(f"n{i}", "no_answer", "正确说没写"), "searches": [{"status": "ok"}], "reads": [{}]}
        for i in range(2)
    ]
    rows = [*kept, _excluded("hn-x1"), _excluded("hn-x2")]
    text = tally([_write(tmp_path / "a.jsonl", rows)])
    assert "searches per question 1.00" in text
    assert "reads per question 1.00" in text


def test_recheck_costs_on_kept_rows_count_as_judge_spend(tmp_path: Path) -> None:
    row = {
        **_row("n1", "no_answer", "正确说没写"),
        "cost_cny": {"answer": 0.1, "judge": 0.1, "recheck": 0.05},
    }
    text = tally([_write(tmp_path / "a.jsonl", [row])])
    assert "judge ¥0.15" in text


def test_files_with_the_same_name_in_different_folders_are_kept_apart(tmp_path: Path) -> None:
    (tmp_path / "x").mkdir()
    (tmp_path / "y").mkdir()
    a = _write(tmp_path / "x" / "r.jsonl", [_claims("a1", "main", 1, 2)])
    b = _write(tmp_path / "y" / "r.jsonl", [_claims("a1", "main", 2, 2)])
    text = tally([a, b])
    assert f"  {a} | all 50.0%(1/2)" in text
    assert f"  {b} | all 100.0%(2/2)" in text


def test_core_claims_marked_as_needing_no_source_are_listed_for_review(tmp_path: Path) -> None:
    # 审查第三轮 LOW-5:这条清单列所有标签的题
    rows = [
        {**_row("n1", "no_answer", "其它"), "verdict": {"core_unsourced": 1}},
        {**_row("a1", "answerable", "答对"), "verdict": {"core_unsourced": 2}},
        _row("p1", "partial", "B式作答"),
    ]
    text = tally([_write(tmp_path / "a.jsonl", rows)])
    assert "  待复核·结论本身标了不需要出处:资料没写 1 题(n1);部分写了 0 题;有答案 1 题(a1)" in text


def test_disagreements_between_the_text_match_and_the_judge_flag_are_listed(
    tmp_path: Path,
) -> None:
    # 审查第五轮:文字比对命中、评委没标 / 评委标了、文字比对没命中 —— 两个方向都列题号,不改类别
    rows = [
        {
            **_row("h1", "no_answer", "正确说没写", set_="hard_negative"),
            "verdict": {"quote_deleted": 1, "repeats_deleted": 0},
        },
        {
            **_row("h2", "no_answer", "编造", set_="hard_negative"),
            "verdict": {"quote_deleted": 0, "repeats_deleted": 1},
        },
        {
            **_row("h3", "no_answer", "编造", set_="hard_negative"),
            "verdict": {"quote_deleted": 1, "repeats_deleted": 1},
        },
    ]
    text = tally([_write(tmp_path / "a.jsonl", rows)])
    assert "  待复核·引文像被删句、评委没标:1 题(h1)" in text
    assert "  待复核·评委标了复述被删原文、引文没对上被删句:1 题(h2)" in text
