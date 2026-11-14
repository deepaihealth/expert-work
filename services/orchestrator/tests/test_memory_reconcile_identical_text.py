"""B-300 11-14 —— 去重合并前「内容相同」的判定口径(测试环境实测:重复的那条只差标点,含全角 / 半角)。

归一:NFKC(全角半角)、casefold、标点去掉 —— 但夹在两个数字之间的保留(小数点、千分位、日期、时刻),
数字前的负号保留;空白合并,挨着中日文字的空白去掉。宁可判「不同」交给模型,不把意思不同的判成相同。
"""

from __future__ import annotations

import pytest

from orchestrator.graph_builder.memory import _normalized_text


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("用户住在成都。", "用户住在成都"),
        ("user lives in Paris.", "user lives in Paris"),
        ("用户喜欢绿茶,也喜欢红茶", "用户喜欢绿茶\uff0c也喜欢红茶"),  # 半角 vs 全角逗号
        ("User Lives In Paris", "user lives in paris"),
        ("用户 住在 成都", "用户住在成都"),
        ("user  lives\tin\nParis", "user lives in Paris"),
        ("体重 69.1 公斤。", "体重69.1公斤"),
    ],
    ids=[
        "trailing-cjk-stop",
        "trailing-ascii-stop",
        "full-vs-half-width-comma",
        "case",
        "cjk-inner-spaces",
        "whitespace-runs",
        "decimal-kept-spaces-dropped",
    ],
)
def test_same_after_normalisation(a: str, b: str) -> None:
    assert _normalized_text(a) == _normalized_text(b)


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("体重 69.1 公斤", "体重 691 公斤"),
        ("budget 1,000", "budget 1000"),
        ("due 2026-11-14", "due 20261114"),
        ("meeting at 10:30", "meeting at 1030"),
        ("temperature -3", "temperature 3"),
        ("growth 50%", "growth 50"),
        ("dark roast", "darkroast"),
    ],
    ids=["decimal", "thousands", "date", "time", "minus-sign", "percent", "latin-word-space"],
)
def test_different_after_normalisation(a: str, b: str) -> None:
    assert _normalized_text(a) != _normalized_text(b)
