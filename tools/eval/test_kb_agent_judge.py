"""kb_agent_judge.py:按结论判分 + 多数票(B-206 11-09 尺子修正)。全部离线,判分模型是脚本。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from kb_agent_judge import (  # type: ignore[import-not-found]
    CATEGORIES,
    DISAGREE,
    JudgeCache,
    category_for,
    judge_prompt,
    majority,
    score_verdict,
)
from kb_dataset import KbCase, KbExpected  # type: ignore[import-not-found]

_PASSAGES = "益生菌请注意与40℃以上热水间隔30分钟食用,与中西药间隔30分钟食用(抗生素需间隔2小时)。"


def _claim(support: str, quote: str = "", claim: str = "c") -> dict[str, str]:
    return {"claim": claim, "support": support, "quote": quote}


def _raw(claims: list[dict[str, str]], *, refused: bool, matches: str = "n/a") -> dict[str, Any]:
    return {"claims": claims, "refused": refused, "matches_reference": matches, "reason": "r"}


# ---------------------------------------------------------------------------
# 结论级判分:编造由代码按每条结论的依据算,不靠判分模型整体拍板
# ---------------------------------------------------------------------------


def test_style_b_answer_to_a_partial_question_is_correct() -> None:
    # 「资料没直接写」+ 列出原文事实 + 推断标明 = B 式作答(用户 11-07 拍板)
    raw = _raw(
        [
            _claim("stated", "与中西药间隔30分钟食用"),
            _claim("inferred_labeled", claim="按这条推测降压药也间隔 30 分钟较稳妥"),
        ],
        refused=True,
    )
    verdict = score_verdict(raw, _PASSAGES)
    assert verdict["fabricated"] is False
    assert verdict["related_facts"] == 1
    assert category_for("partial", verdict) == "B式作答"


def test_an_unlabeled_inference_is_fabrication_even_when_facts_are_listed() -> None:
    raw = _raw(
        [
            _claim("stated", "与中西药间隔30分钟食用"),
            _claim("inferred_unlabeled", claim="降压药和益生菌一起吃没有影响"),
        ],
        refused=True,
    )
    verdict = score_verdict(raw, _PASSAGES)
    assert verdict["fabricated"] is True
    assert category_for("partial", verdict) == "编造"
    assert category_for("no_answer", verdict) == "编造"


@pytest.mark.parametrize("support", ["unsupported", "inferred_unlabeled"])
def test_unsupported_or_unlabeled_claims_are_fabrication(support: str) -> None:
    verdict = score_verdict(_raw([_claim(support)], refused=False), _PASSAGES)
    assert verdict["fabricated"] is True


@pytest.mark.parametrize("support", ["inferred_labeled", "general_labeled"])
def test_labeled_inference_or_general_advice_is_not_fabrication(support: str) -> None:
    verdict = score_verdict(_raw([_claim(support)], refused=True), _PASSAGES)
    assert verdict["fabricated"] is False
    assert category_for("no_answer", verdict) == "正确说没写"


def test_only_saying_not_written_on_a_partial_question_is_style_a_not_b() -> None:
    verdict = score_verdict(_raw([], refused=True), _PASSAGES)
    assert category_for("partial", verdict) == "只说没写"


def test_a_stated_claim_whose_quote_is_not_in_the_passages_is_not_a_related_fact() -> None:
    # 判分模型说「原文写了」却给不出能在原文里找到的句子:不算列出了相关事实,单独计数待人看。
    raw = _raw([_claim("stated", "与降压药间隔两小时服用")], refused=True)
    verdict = score_verdict(raw, _PASSAGES)
    assert verdict["related_facts"] == 0
    assert verdict["quote_misses"] == 1
    assert category_for("partial", verdict) == "只说没写"


def test_quote_match_ignores_spacing_and_full_width_punctuation() -> None:
    raw = _raw([_claim("stated", "与 中西药间隔 30 分钟食用。")], refused=True)
    assert score_verdict(raw, _PASSAGES)["related_facts"] == 1


@pytest.mark.parametrize(
    "raw",
    [
        {"parse_error": "x"},
        {"refused": True, "matches_reference": "n/a"},  # 没有 claims
        {"claims": "none", "refused": True},
        {"claims": [{"claim": "c", "support": "maybe"}], "refused": True},
        {"claims": [], "refused": "yes"},
    ],
)
def test_malformed_verdicts_are_judge_failures(raw: dict[str, Any]) -> None:
    verdict = score_verdict(raw, _PASSAGES)
    assert "parse_error" in verdict
    for label in CATEGORIES:
        assert category_for(label, verdict) == "判分失败"


@pytest.mark.parametrize(
    ("claims", "refused", "matches", "expected"),
    [
        ([_claim("stated", "与中西药间隔30分钟食用")], False, "yes", "答对"),
        ([_claim("stated", "与中西药间隔30分钟食用")], False, "partial", "答对"),
        ([], True, "no", "误拒"),
        ([_claim("stated", "与中西药间隔30分钟食用")], False, "no", "答偏"),
        ([_claim("unsupported")], False, "yes", "编造"),
    ],
)
def test_answerable_categories(
    claims: list[dict[str, str]], refused: bool, matches: str, expected: str
) -> None:
    verdict = score_verdict(_raw(claims, refused=refused, matches=matches), _PASSAGES)
    assert category_for("answerable", verdict) == expected


def test_no_answer_without_saying_so_and_without_fabrication_is_other() -> None:
    verdict = score_verdict(_raw([], refused=False), _PASSAGES)
    assert category_for("no_answer", verdict) == "其它"


# ---------------------------------------------------------------------------
# 多数票
# ---------------------------------------------------------------------------


def test_majority_takes_a_strict_majority_and_reports_agreement() -> None:
    # 11-05 记录:0006 同一回答 7 次判分 2 次编造 / 5 次其它 —— 三票取多数把单次抖动压掉
    assert majority(["编造", "其它", "其它"]) == ("其它", "2/3")
    assert majority(["编造", "编造", "编造"]) == ("编造", "3/3")
    assert majority(["答对"]) == ("答对", "1/1")


def test_majority_without_a_strict_majority_is_reported_as_disagreement() -> None:
    assert majority(["编造", "其它", "判分失败"]) == (DISAGREE, "1/3")
    assert majority(["编造", "其它"]) == (DISAGREE, "1/2")


def test_majority_needs_votes() -> None:
    with pytest.raises(ValueError):
        majority([])


# ---------------------------------------------------------------------------
# 提示词
# ---------------------------------------------------------------------------


def _case(label: str | None, **kw: Any) -> KbCase:
    return KbCase(
        id="q1",
        query="益生菌和降压药一起吃有影响吗?",
        source="no_answer",
        tags=(),
        expected=kw.pop("expected", ()),
        answer=kw.pop("answer", None),
        label=label,
        **kw,
    )


_RUN = {"answer": "资料没直接写。", "searches": [{"content": _PASSAGES}], "reads": []}


def test_partial_prompt_lists_the_stated_facts_and_asks_for_labeled_inference() -> None:
    related = (KbExpected(document="说明书.docx", anchor="与中西药间隔30分钟食用", page=None),)
    prompt = judge_prompt(_case("partial", related=related, answer="只写了间隔"), _RUN, {})
    assert "没有直接写" in prompt
    assert "《说明书.docx》中的原文片段:与中西药间隔30分钟食用" in prompt
    assert "标明是推断" in prompt
    assert _PASSAGES in prompt


def test_prompt_carries_the_case_note() -> None:
    prompt = judge_prompt(_case(None), _RUN, {}, note="资料里还有名字相近的「乙」")
    assert "资料里还有名字相近的「乙」" in prompt
    assert "本题资料里没有答案" in prompt


def test_answerable_prompt_keeps_the_reference_and_gold_passages() -> None:
    expected = (KbExpected(document="说明书.docx", anchor="与中西药间隔30分钟食用", page=None),)
    case = _case("answerable", expected=expected, answer="间隔 30 分钟")
    prompt = judge_prompt(case, _RUN, {"q1": ["整段原文"]})
    assert "参考答案:间隔 30 分钟" in prompt
    assert "整段原文" in prompt


def test_judge_cache_keeps_each_vote_apart(tmp_path: Path) -> None:
    cache = JudgeCache(tmp_path / "judge.sqlite")
    cache.put("m", "p", json.dumps({"v": 0}))
    cache.put("m", "p", json.dumps({"v": 1}), vote=1)
    assert cache.get("m", "p") == '{"v": 0}'
    assert cache.get("m", "p", vote=1) == '{"v": 1}'
    assert cache.get("m", "p", vote=2) is None
