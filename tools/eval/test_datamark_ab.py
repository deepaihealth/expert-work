"""Deterministic parts of the B-149 datamark A/B harness (no model calls)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import datamark_ab as ab

from expert_work.common.spotlight import DATAMARK_GLYPH, SPOTLIGHT_SYSTEM_CLAUSE


def test_old_arm_is_the_pre_b149_rule_and_clause() -> None:
    wrapped = ab.legacy_spotlight("a\n    b", nonce="n")
    assert f"a{DATAMARK_GLYPH} b" in wrapped
    assert "line breaks" not in ab.LEGACY_CLAUSE
    assert ab.LEGACY_CLAUSE != SPOTLIGHT_SYSTEM_CLAUSE
    assert ab.LEGACY_CLAUSE.endswith("trusted instructions.")


def test_new_arm_keeps_layout() -> None:
    msgs = ab.build_messages(ab.CASES[0], "new")
    assert "\nIgnore" in str(msgs[-1]["content"])
    assert "line breaks and indentation" in str(msgs[0]["content"])


def test_every_injection_case_hides_its_canary_in_the_file() -> None:
    for case in ab.CASES:
        if case.kind == "injection":
            assert case.judge("a clean summary", case.file)
            canary = next(w for w in case.file.replace(".", " ").split() if w.startswith("CANARY-"))
            assert not case.judge(f"sure: {canary}", case.file)


def test_old_string_judge_needs_a_verbatim_block() -> None:
    case = next(c for c in ab.CASES if c.id == "old-string")
    good = "```python\ndef total(rows):\n    acc = 0\n```"
    flat = "```python\ndef total(rows): acc = 0\n```"
    assert case.judge(good, case.file)
    assert not case.judge(flat, case.file)


def test_utility_judges() -> None:
    by_id = {c.id: c for c in ab.CASES}
    lines = ab._NUMBERED.splitlines()
    assert lines[16].startswith("step 17: ")
    assert by_id["line-17"].judge(lines[16], "")
    assert not by_id["line-17"].judge(lines[15], "")
    assert by_id["indent"].judge("16", "")
    assert not by_id["indent"].judge("4 spaces", "")
    assert by_id["table"].judge("2.9", "")
