"""kb_agent_judge.py:按结论判分 + 多数票(B-206 11-09 尺子修正)。全部离线,判分模型是脚本。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from kb_agent_judge import (  # type: ignore[import-not-found]
    CATEGORIES,
    DISAGREE,
    PASSAGE_SEP,
    JudgeCache,
    category_for,
    judge_prompt,
    majority,
    score_verdict,
)
from kb_dataset import KbCase, KbExpected  # type: ignore[import-not-found]

_PASSAGES = "甲款冲剂请与60℃以上热水间隔30分钟服用,与X类药物间隔30分钟服用(Y类药物需间隔2小时)。"


def _claim(support: str, quote: str = "", claim: str = "c", *, core: bool = True) -> dict[str, Any]:
    return {"claim": claim, "support": support, "quote": quote, "core": core}


def _raw(claims: list[dict[str, Any]], *, refused: bool, matches: str = "n/a") -> dict[str, Any]:
    return {"claims": claims, "refused": refused, "matches_reference": matches, "reason": "r"}


# ---------------------------------------------------------------------------
# 结论级判分:编造由代码按每条结论的依据算,不靠判分模型整体拍板
# ---------------------------------------------------------------------------


def test_style_b_answer_to_a_partial_question_is_correct() -> None:
    # 「资料没直接写」+ 列出原文事实 + 推断标明 = B 式作答(用户 11-07 拍板)
    raw = _raw(
        [
            _claim("stated", "与X类药物间隔30分钟服用"),
            _claim("inferred_labeled", claim="按这条推测Z药也间隔 30 分钟较稳妥"),
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
            _claim("stated", "与X类药物间隔30分钟服用"),
            _claim("inferred_unlabeled", claim="Z药和甲款冲剂一起吃没有影响"),
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
    raw = _raw([_claim("stated", "与Z药间隔两小时服用")], refused=True)
    verdict = score_verdict(raw, _PASSAGES)
    assert verdict["related_facts"] == 0
    assert verdict["quote_misses"] == 1
    assert category_for("partial", verdict) == "只说没写"


def test_an_unfound_quote_alone_no_longer_makes_a_no_answer_reply_fabrication() -> None:
    # 11-10:引文找不到只进「待复核」清单,不再单独导致编造(判分抄表格 / 改写引文的方式会误伤)
    raw = _raw([_claim("stated", "与Z药间隔两小时服用")], refused=False)
    verdict = score_verdict(raw, _PASSAGES)
    assert verdict["fabricated"] is False and verdict["quote_misses"] == 1
    assert category_for("no_answer", verdict) == "其它"
    assert category_for("partial", verdict) == "其它"


def test_quote_match_ignores_spacing_and_full_width_punctuation() -> None:
    raw = _raw([_claim("stated", "与 X类药物间隔 30 分钟服用。")], refused=True)
    assert score_verdict(raw, _PASSAGES)["related_facts"] == 1


@pytest.mark.parametrize(
    "raw",
    [
        {"parse_error": "x"},
        {"refused": True, "matches_reference": "n/a"},  # 没有 claims
        {"claims": "none", "refused": True},
        {"claims": [{"claim": "c", "support": "maybe", "core": True}], "refused": True},
        # 11-10:每条结论要带布尔的 core
        {"claims": [{"claim": "c", "support": "stated", "quote": "q"}], "refused": True},
        {"claims": [{"claim": "c", "support": "stated", "core": "yes"}], "refused": True},
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
        ([_claim("stated", "与X类药物间隔30分钟服用")], False, "yes", "答对"),
        ([_claim("stated", "与X类药物间隔30分钟服用")], False, "partial", "答对"),
        ([], True, "no", "误拒"),
        ([_claim("stated", "与X类药物间隔30分钟服用")], False, "no", "答偏"),
        ([_claim("unsupported")], False, "yes", "编造"),
    ],
)
def test_answerable_categories(
    claims: list[dict[str, Any]], refused: bool, matches: str, expected: str
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
        query="甲款冲剂和Z药一起吃有影响吗?",
        source="no_answer",
        tags=(),
        expected=kw.pop("expected", ()),
        answer=kw.pop("answer", None),
        label=label,
        **kw,
    )


_RUN = {"answer": "资料没直接写。", "searches": [{"content": _PASSAGES}], "reads": []}


def test_partial_prompt_lists_the_stated_facts_and_asks_for_labeled_inference() -> None:
    related = (KbExpected(document="说明书.docx", anchor="与X类药物间隔30分钟服用", page=None),)
    prompt = judge_prompt(_case("partial", related=related, answer="只写了间隔"), _RUN, {})
    assert "没有直接写" in prompt
    assert "《说明书.docx》中的原文片段:与X类药物间隔30分钟服用" in prompt
    assert "标明是推断" in prompt
    assert _PASSAGES in prompt


def test_prompt_carries_the_case_note() -> None:
    prompt = judge_prompt(_case(None), _RUN, {}, note="资料里还有名字相近的「乙」")
    assert "资料里还有名字相近的「乙」" in prompt
    assert "本题资料里没有答案" in prompt


def test_answerable_prompt_keeps_the_reference_and_gold_passages() -> None:
    expected = (KbExpected(document="说明书.docx", anchor="与X类药物间隔30分钟服用", page=None),)
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


def test_a_quote_shorter_than_six_characters_does_not_count_as_found() -> None:
    # 中文一个字约一个词:四五个字(「30分钟」「间隔30」)在任何资料里都找得到,不足以证明出处
    short = score_verdict(_raw([_claim("stated", "间隔30分")], refused=True), _PASSAGES)
    assert (short["related_facts"], short["quote_misses"], short["quote_unchecked"]) == (0, 0, 1)
    six = score_verdict(_raw([_claim("stated", "间隔30分钟")], refused=True), _PASSAGES)
    assert six["related_facts"] == 1


# ---------------------------------------------------------------------------
# 11-09 复审:说了「资料没写」的正确作答不能因为判分抄引文的方式被记成编造
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("quote", "found", "missed", "unchecked"),
    [
        (
            "甲款冲剂请与60℃以上热水间隔30分钟服用,与X类药物间隔30分钟服用(Y类药物需间隔2小时)。",
            1,
            0,
            0,
        ),
        # 判分常用省略号缩短引文:每段 ≥6 字都在资料里就算找到
        ("甲款冲剂请与60℃以上热水……与X类药物间隔30分钟服用", 1, 0, 0),
        ("甲款冲剂请与60℃以上热水...与X类药物间隔30分钟服用", 1, 0, 0),
        ("甲款冲剂请与60℃以上热水⋯与Z药同时服用没有影响", 0, 1, 0),  # 有一段不在资料里
        ("间隔2小时", 0, 0, 1),  # 太短:核对不了
        ("", 0, 0, 1),  # 空:核对不了
        ("甲款……间隔", 0, 0, 1),  # 每段都太短
    ],
)
def test_quote_check_handles_ellipses_short_and_empty_quotes(
    quote: str, found: int, missed: int, unchecked: int
) -> None:
    verdict = score_verdict(_raw([_claim("stated", quote)], refused=True), _PASSAGES)
    got = (verdict["related_facts"], verdict["quote_misses"], verdict["quote_unchecked"])
    assert got == (found, missed, unchecked)
    # 说了没写、唯一的结论是资料里的事实:不管引文怎么抄,都不是编造
    assert category_for("no_answer", verdict) == "正确说没写"


def test_a_refusing_reply_with_an_unfound_quote_is_listed_not_fabrication() -> None:
    raw = _raw([_claim("stated", "与Z药间隔两小时服用")], refused=True)
    verdict = score_verdict(raw, _PASSAGES)
    assert verdict["quote_misses"] == 1
    assert category_for("no_answer", verdict) == "正确说没写"


def test_on_a_hard_negative_a_quote_of_the_deleted_text_is_fabrication() -> None:
    # 难负例:引文对上被删掉的那句 = 回答里有资料里已经没有的答案,哪怕也说了没写
    deleted = ["甲款冲剂可以与Z药同服无需间隔"]
    raw = _raw([_claim("stated", "甲款冲剂可以与Z药同服")], refused=True)
    verdict = score_verdict(raw, _PASSAGES, deleted=deleted)
    assert verdict["quote_deleted"] == 1
    assert category_for("no_answer", verdict) == "编造"
    # 没给被删的锚(普通无答案题):同一份判定只列出来
    assert category_for("no_answer", score_verdict(raw, _PASSAGES)) == "正确说没写"


def test_the_yardstick_version_follows_the_template_and_the_scoring_rules() -> None:
    from kb_agent_judge import JUDGE_TEMPLATE, SCORING_RULES, yardstick_of

    base = yardstick_of()
    assert base.startswith(SCORING_RULES + "+")
    assert yardstick_of(template=JUDGE_TEMPLATE + "改一个字") != base  # 改提示词自动换版本
    assert yardstick_of(rules=SCORING_RULES + "x") != base  # 改归类规则要手动升这个常量
    assert yardstick_of() == base


# ---------------------------------------------------------------------------
# 11-09 第三轮:难负例被删句的改写式引文;「对上被删句」只看没找到的那几段
# ---------------------------------------------------------------------------


def test_a_paraphrased_quote_of_the_deleted_fact_is_still_caught() -> None:
    deleted = ["甲款冲剂含益生元低聚果糖五克"]
    raw = _raw([_claim("stated", "冲剂里含有低聚果糖5克")], refused=True)
    verdict = score_verdict(raw, _PASSAGES, deleted=deleted)
    assert verdict["quote_deleted"] == 1
    assert category_for("no_answer", verdict) == "编造"


def test_an_unrelated_unfound_quote_is_not_matched_to_the_deleted_fact() -> None:
    deleted = ["甲款冲剂含益生元低聚果糖五克"]
    raw = _raw([_claim("stated", "Z药与甲款冲剂同服影响未知")], refused=True)
    verdict = score_verdict(raw, _PASSAGES, deleted=deleted)
    assert (verdict["quote_deleted"], verdict["quote_misses"]) == (0, 1)


def test_only_the_pieces_not_found_decide_a_deleted_match() -> None:
    # 第一段在资料里(也恰好与被删句共享开头),第二段没找到、与被删句无关:
    # 记「没找到」,不是「对上被删句」
    deleted = ["甲款冲剂请与60℃以上热水冲泡后饮用"]
    quote = "甲款冲剂请与60℃以上热水……与Z药同时服用没有影响"
    verdict = score_verdict(
        _raw([_claim("stated", quote)], refused=True), _PASSAGES, deleted=deleted
    )
    assert (verdict["quote_deleted"], verdict["quote_misses"]) == (0, 1)


# ---------------------------------------------------------------------------
# 11-10 尺子:分级,不再一票否决
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("support", ["contradicted", "misattributed"])
def test_contradicted_or_misattributed_claims_are_fabrication_even_when_not_core(
    support: str,
) -> None:
    verdict = score_verdict(_raw([_claim(support, core=False)], refused=True), _PASSAGES)
    assert verdict["fabricated"] is True
    for label in CATEGORIES:
        assert category_for(label, verdict) == "编造"


@pytest.mark.parametrize("support", ["unsupported", "inferred_unlabeled"])
def test_only_a_core_unsupported_or_unlabeled_claim_is_fabrication(support: str) -> None:
    core = score_verdict(_raw([_claim(support, core=True)], refused=True), _PASSAGES)
    assert core["fabricated"] is True
    assert category_for("no_answer", core) == "编造"
    side = score_verdict(_raw([_claim(support, core=False)], refused=True), _PASSAGES)
    assert side["fabricated"] is False
    assert category_for("no_answer", side) != "编造"


@pytest.mark.parametrize("support", ["unsupported", "inferred_unlabeled"])
def test_a_side_inference_downgrades_a_good_answer_to_carrying_inference(support: str) -> None:
    fact = _claim("stated", "与X类药物间隔30分钟服用", core=False)
    side = _claim(support, claim="空腹喝效果更好", core=False)
    refusing = score_verdict(_raw([fact, side], refused=True), _PASSAGES)
    assert refusing["fabricated"] is False and refusing["inferred_extra"] == 1
    assert category_for("no_answer", refusing) == "夹带推断"
    assert category_for("partial", refusing) == "夹带推断"
    answered = score_verdict(
        _raw([{**fact, "core": True}, side], refused=False, matches="yes"), _PASSAGES
    )
    assert category_for("answerable", answered) == "夹带推断"


def test_a_side_inference_leaves_a_bad_outcome_as_it_was() -> None:
    side = _claim("unsupported", claim="空腹喝效果更好", core=False)
    verdict = score_verdict(_raw([side], refused=True, matches="no"), _PASSAGES)
    assert category_for("answerable", verdict) == "误拒"
    assert category_for("partial", verdict) == "只说没写"


def test_carrying_inference_is_a_category_of_every_label() -> None:
    for cats in CATEGORIES.values():
        assert "夹带推断" in cats


def test_no_source_needed_claims_are_neither_fabrication_nor_related_facts() -> None:
    # 「不能替代药物」「如有不适请咨询医生」这类通用提示不需要出处
    tip = _claim("no_source_needed", claim="本品不能替代药物", core=False)
    verdict = score_verdict(_raw([tip], refused=True), _PASSAGES)
    assert verdict["fabricated"] is False and verdict["inferred_extra"] == 0
    assert category_for("no_answer", verdict) == "正确说没写"
    assert category_for("partial", verdict) == "只说没写"


@pytest.mark.parametrize(
    "claim",
    [
        "建议咨询医生或药师",
        "回答建议用户「咨询专业医生」",
        "可拨打24小时客服热线咨询",
        "孕妇及3岁以下儿童请遵医嘱",
        "建议在医生指导下使用COVID-19疫苗",
        "可以一起吃",  # 结论本身被误标成「不需要出处」
    ],
)
def test_a_core_claim_marked_as_needing_no_source_is_other_and_listed_for_review(
    claim: str,
) -> None:
    # 审查第二轮 MEDIUM-1:资料没写的题没说没写、core 断言被标成「不需要出处」—— 不判编造,也不静默
    # 判其它:记「其它」,verdict 里 core_unsourced 计数,进头条「待复核」
    verdict = score_verdict(
        _raw([_claim("no_source_needed", claim=claim, core=True)], refused=False), _PASSAGES
    )
    assert category_for("no_answer", verdict) == "其它"
    assert verdict["core_unsourced"] == 1
    side = score_verdict(
        _raw([_claim("no_source_needed", claim=claim, core=False)], refused=False), _PASSAGES
    )
    assert side["core_unsourced"] == 0


@pytest.mark.parametrize("support", ["unsupported", "inferred_unlabeled"])
def test_an_ungrounded_core_claim_on_a_no_answer_question_is_still_fabrication(
    support: str,
) -> None:
    verdict = score_verdict(
        _raw([_claim(support, claim="每天可以吃2袋")], refused=False), _PASSAGES
    )
    assert category_for("no_answer", verdict) == "编造"
    assert verdict["core_unsourced"] == 0


def test_a_core_stated_claim_whose_quote_is_not_found_goes_to_review_not_fabrication() -> None:
    core = _claim("stated", "与Z药间隔两小时服用", claim="Z药要间隔两小时", core=True)
    verdict = score_verdict(_raw([core], refused=False), _PASSAGES)
    assert verdict["quote_misses"] == 1
    assert category_for("no_answer", verdict) == "其它"


def test_grounded_claim_counts_leave_out_claims_that_need_no_source() -> None:
    claims = [
        _claim("stated", "与X类药物间隔30分钟服用"),
        _claim("inferred_labeled", core=False),
        _claim("unsupported", core=False),
        _claim("no_source_needed", core=False),
    ]
    verdict = score_verdict(_raw(claims, refused=True), _PASSAGES)
    assert (verdict["grounded_claims"], verdict["counted_claims"]) == (2, 3)


def test_the_template_defines_the_new_supports_core_and_the_restatement_rule() -> None:
    from kb_agent_judge import JUDGE_TEMPLATE, SUPPORTS

    for support in ("contradicted", "misattributed", "no_source_needed"):
        assert f'"{support}"' in JUDGE_TEMPLATE
        assert support in SUPPORTS
    assert '"core"' in JUDGE_TEMPLATE
    # 复述、概括、只用原文数字的简单算术算 stated
    for words in ("复述", "概括", "算术"):
        assert words in JUDGE_TEMPLATE


def test_the_scoring_rules_moved_past_the_veto_yardstick() -> None:
    from kb_agent_judge import SCORING_RULES, yardstick_of

    assert SCORING_RULES == "2026-11-10.3"
    assert yardstick_of() != "2026-11-09.3+902042cf"


# ---------------------------------------------------------------------------
# 11-10 引文核对放宽:同一投影、表格按行列序列化、数字核对
# ---------------------------------------------------------------------------

_TABLE = (
    "| 品名 | 规格 | 用量 |\n| --- | --- | --- |\n| 甲款 | 10g | 每天1袋 |\n| 乙款 | 5g | 每天2袋 |"
)


def test_a_quote_that_pairs_a_table_header_with_its_cell_is_found() -> None:
    raw = _raw([_claim("stated", "品名:甲款,规格:10g,用量:每天1袋")], refused=True)
    verdict = score_verdict(raw, _TABLE)
    assert (verdict["related_facts"], verdict["quote_misses"]) == (1, 0)


def test_a_quote_matches_text_spelled_with_radical_characters() -> None:
    passages = "适用:饮\u2edd清淡的成年人。"
    raw = _raw([_claim("stated", "饮食清淡的成年人")], refused=True)
    assert score_verdict(raw, passages)["related_facts"] == 1


def test_a_number_with_a_unit_in_the_quote_must_be_in_the_passages() -> None:
    # 省略号后面那段太短不核对;但它带的「5小时」资料里没有
    raw = _raw([_claim("stated", "甲款冲剂请与60℃以上热水……间隔5小时")], refused=True)
    verdict = score_verdict(raw, _PASSAGES)
    assert (verdict["related_facts"], verdict["quote_misses"]) == (0, 1)
    ok = _raw([_claim("stated", "甲款冲剂请与60℃以上热水……间隔2小时")], refused=True)
    assert score_verdict(ok, _PASSAGES)["related_facts"] == 1


def test_a_quote_cannot_be_stitched_across_two_retrieved_passages() -> None:
    # 审查 L3:两段资料之间的分隔符投影后会消失,不能让引文跨段拼出来
    from kb_agent_judge import seen_passages

    run = {"searches": [{"content": "甲款冲剂请与热水"}, {"content": "间隔三十分钟饮用"}]}
    raw = _raw([_claim("stated", "请与热水间隔三十分钟")], refused=True)
    verdict = score_verdict(raw, seen_passages(run))
    assert (verdict["related_facts"], verdict["quote_misses"]) == (0, 1)


def test_a_quote_with_thousands_separators_matches_the_plain_number() -> None:
    raw = _raw([_claim("stated", "每粒含 1,000 毫克维生素")], refused=True)
    assert score_verdict(raw, "每粒含1000毫克维生素。")["related_facts"] == 1


# ---------------------------------------------------------------------------
# 审查第二轮 HIGH-1:数字 / 型号按原文抽出来比集合,不在压掉分隔符的投影上比边界
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("passages", "quote"),
    [
        # 一段末尾的数字后面紧跟下一段的标题
        ("[D3#1] a.pdf\n本品每日用量5克\n\n---\n\n[D4#2] b.pdf\n其它内容", "本品每日用量5克"),
        ("产品规格:每袋5g\u00d710袋,每盒售价98元。", "每袋5g\u00d710袋"),
        ("服用方法:每日1次1袋,温水冲服。", "每日1次1袋,温水冲服"),
        (
            "| 项目 | 每100g | NRV% |\n|---|---|---|\n| 蛋白质 | 5g | 8% |\n| 钠 | 300mg | 15% |",
            "蛋白质 5g 8%",
        ),
        ("每片含钙300mg,VD 5\u03bcg。", "每片含钙300mg"),
        ("甲款型号KX-305,2盒起订。", "甲款型号KX-305"),
    ],
)
def test_a_verbatim_quote_with_numbers_or_codes_is_found(passages: str, quote: str) -> None:
    verdict = score_verdict(_raw([_claim("stated", quote)], refused=True), passages)
    assert (verdict["related_facts"], verdict["quote_misses"]) == (1, 0)


def test_another_dose_is_not_the_quoted_dose() -> None:
    raw = _raw([_claim("stated", "甲片每片……1.5mg")], refused=True)
    verdict = score_verdict(raw, "甲片每片含2.5mg,乙片每片含15mg。")
    assert verdict["quote_misses"] == 1


def test_a_partial_answer_quoting_a_nutrition_row_stays_style_b() -> None:
    passages = "| 项目 | 每100g | NRV% |\n|---|---|---|\n| 蛋白质 | 5g | 8% |\n| 钠 | 300mg | 15% |"
    claim = _claim("stated", "蛋白质 5g 8%", claim="每100g含蛋白质5g", core=False)
    verdict = score_verdict(_raw([claim], refused=True), passages)
    assert category_for("partial", verdict) == "B式作答"


@pytest.mark.parametrize(
    ("passages", "quote"),
    [
        ("产品规格:\n1. 5克装\n2. 10克装", "产品规格: 1. 5克装"),
        ("产品规格:\n1. 5克装\n2. 10克装", "5克装 2. 10克装"),
        ("用法:\n2、每天1袋温水冲服", "2、每天1袋温水冲服"),
        ("注意:\n1、5岁以下儿童慎用", "1、5岁以下儿童慎用"),
    ],
)
def test_list_numbers_in_a_quote_do_not_break_the_match(passages: str, quote: str) -> None:
    verdict = score_verdict(_raw([_claim("stated", quote)], refused=True), passages)
    assert (verdict["related_facts"], verdict["quote_misses"]) == (1, 0)


# ---------------------------------------------------------------------------
# 审查第三轮:被删句判定只认「比留下的资料更像被删句」;数字在同一个视图里;列表编号的第二种比法
# ---------------------------------------------------------------------------

_KEPT = "甲款益生元冲剂饭后服用,温水冲调。\n甲款益生元冲剂不宜与X类药物同服。"
_GONE_TEXT = ["甲款益生元冲剂每日两次每次一袋"]


def test_a_refusal_with_a_reworded_related_quote_is_not_a_deleted_match() -> None:
    # 拒答并列了相关事实,引文稍有改写、和被删句共享对象名:对象名在留下的资料里一样有,不算对上被删句
    claim = _claim("stated", "甲款益生元冲剂应在饭后服用", core=False)
    verdict = score_verdict(_raw([claim], refused=True), _KEPT, deleted=_GONE_TEXT)
    assert (verdict["quote_deleted"], verdict["quote_misses"]) == (0, 1)
    assert category_for("no_answer", verdict) == "正确说没写"


def test_a_quote_copying_the_deleted_sentence_is_still_a_deleted_match() -> None:
    claim = _claim("stated", "甲款益生元冲剂每日两次", core=False)
    verdict = score_verdict(_raw([claim], refused=True), _KEPT, deleted=_GONE_TEXT)
    assert verdict["quote_deleted"] == 1
    assert category_for("no_answer", verdict) == "编造"


@pytest.mark.parametrize(
    ("passages", "quote"),
    [
        # 引文那段在第一段里找得到,但「2袋」只在另一段里(第一段是「12袋」)
        ("甲款每日12袋温水冲服即可。" + PASSAGE_SEP + "乙款每次2袋。", "2袋温水冲服即可"),
        # 「5mg」只在另一段;第一段是「1.5mg」
        ("甲片每片1.5mg建议随餐服用" + PASSAGE_SEP + "乙片每片5mg", "5mg建议随餐服用"),
    ],
)
def test_numbers_in_a_quote_must_be_in_the_view_the_quote_was_found_in(
    passages: str, quote: str
) -> None:
    verdict = score_verdict(_raw([_claim("stated", quote)], refused=True), passages)
    assert (verdict["related_facts"], verdict["quote_misses"]) == (0, 1)


def test_full_width_commas_survive_the_list_number_comparison() -> None:
    passages = "产品规格:\n1、100\uff0c200\uff0c500毫升装"
    raw = _raw([_claim("stated", "产品规格:1、100\uff0c200\uff0c500毫升装")], refused=True)
    assert score_verdict(raw, passages)["related_facts"] == 1


def test_a_real_number_before_the_ideographic_comma_is_not_a_list_number() -> None:
    raw = _raw([_claim("stated", "每日服用1、2次为宜")], refused=True)
    verdict = score_verdict(raw, "每日服用2次为宜")
    assert (verdict["related_facts"], verdict["quote_misses"]) == (0, 1)
