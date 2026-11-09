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
    assert "资料没写 n=4:该拒拒了 50.0%(2/4),编造 1" in text
    assert "有答案 n=2:不该拒却拒了 50.0%(1/2)" in text
    assert "部分写了 n=2:B 式作答 50.0%(1/2)" in text
    assert "判分:分歧 1,失败 0;全票一致 6/8" in text


def test_tally_breaks_each_label_down_by_set_including_the_derived_sets(tmp_path: Path) -> None:
    text = tally([_write(tmp_path / "a.jsonl", _ROWS)]).splitlines()
    assert "  hard_negative n=1 正确说没写 1 编造 0 其它 0 判分失败 0 判分分歧 0" in text
    assert "  distractor n=1 答对 0 误拒 1 编造 0 答偏 0 判分失败 0 判分分歧 0" in text
    assert "  chat n=2 B式作答 1 只说没写 1 编造 0 其它 0 判分失败 0 判分分歧 0" in text


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
    assert "资料没写 n=4:该拒拒了 50.0%(2/4),编造 1" in text
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
    assert "  引文在资料里没找到:资料没写 2 题(n1, h1);部分写了 1 题(p1);有答案 0 题" in text
