"""B-300 11-14 —— 去重合并前「内容相同」的判定口径(用户 11-14:只忽略不改变意思的差别)。

NFC;只折宽度(全角 / 半角);casefold;空白合并、去首尾;再去掉**末尾**的句末标点
(。 . ! ;,反复;问号不去 —— 问句和陈述句意思不同)。
别的一律不动:句中标点、连字符、括号、上标、带圈数字、符号都算差别;中文旁边的空格也算。
测试数据里易混淆的字符一律写成 ``\\u`` 转义。
"""

from __future__ import annotations

import pytest

from orchestrator.graph_builder.memory import _normalized_text


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("取件码是7190\u3002", "取件码是7190"),
        ("\uff15\uff10\uff05", "50%"),
        ("Likes Tea.", "likes tea"),
        ("体重  69.1 kg", "体重 69.1 kg"),
        ("体重\u300069.1 kg", "体重 69.1 kg"),
        ("OK!!", "ok"),
        ("住在成都\uff01", "住在成都"),
    ],
    ids=[
        "trailing-cjk-stop",
        "full-width-digits-and-percent",
        "case-and-trailing-dot",
        "whitespace-run",
        "ideographic-space",
        "repeated-trailing-bang",
        "full-width-bang",
    ],
)
def test_same_after_normalisation(a: str, b: str) -> None:
    assert _normalized_text(a) == _normalized_text(b)


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("69.1", "691"),
        (".5 mg", "5 mg"),
        ("Rh-", "Rh"),
        ("尿蛋白(-)", "尿蛋白"),
        ("HER2-", "HER2"),
        ("10\u2075", "105"),
        ("x\u00b2", "x2"),
        ("\u2460", "1"),
        ("50%", "50"),
        ("-3", "3"),
        ("\u20132", "2"),
        ("1,000", "1000"),
        ("2026-11-14", "20261114"),
        ("10:30", "1030"),
        ("a.b@x.com", "ab@x.com"),
        ("li_ming", "liming"),
        ("it's", "its"),
        ("每日3次\uff0c每次2片", "每日3次每次2片"),
        ("3-5", "35"),
        ("1/2", "12"),
        ("体重 69.1", "体重69.1"),
        ("用户对青霉素过敏\uff1f", "用户对青霉素过敏\u3002"),
        ("You take aspirin?", "You take aspirin."),
        ("a\u3002b", "ab"),
        ("a?b", "ab"),
        ("a;b", "ab"),
        ("a!b", "ab"),
    ],
    ids=[
        "decimal",
        "leading-decimal",
        "trailing-minus",
        "bracketed-minus",
        "her2-negative",
        "superscript",
        "squared",
        "circled-digit",
        "percent",
        "minus",
        "en-dash",
        "thousands",
        "date",
        "time",
        "email-dot",
        "underscore",
        "apostrophe",
        "inner-cjk-comma",
        "range",
        "fraction",
        "inner-space-presence",
        "question-vs-statement-cjk",
        "question-vs-statement",
        "inner-cjk-stop",
        "inner-question",
        "inner-semicolon",
        "inner-bang",
    ],
)
def test_different_after_normalisation(a: str, b: str) -> None:
    assert _normalized_text(a) != _normalized_text(b)
