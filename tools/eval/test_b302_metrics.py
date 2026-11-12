"""B-302 PR 2 A/B —— 指标与及格线(设计 §5.4),全是纯函数。"""

from __future__ import annotations

import b302_metrics as m
import pytest


def _step(i: int, text: str = "", *, thinking: str = "", reads: list[dict] | None = None) -> m.Step:
    calls = tuple(m.Call("read_page", args) for args in (reads or []))
    return m.Step(index=i, thinking=thinking, reply=text, calls=calls)


# --------------------------------------------------------------------------- text


def test_normalize_is_nfkc_without_whitespace() -> None:
    assert m.normalize("\uff21\uff22 1 2\n\uff13") == "AB123"


def test_correct_fields_are_counted_after_normalising() -> None:
    fields = [m.Field("#AAAA", "型号", "QX-1O5S-l7"), m.Field("#BBBB", "地址", "滨江路 15 号")]
    deliverable = "型号:QX-1O5S-l7\n地址:滨江路15号"
    assert m.count_correct(deliverable, fields) == 2
    assert m.count_correct("型号:QX-105S-l7 地址:滨江路15号", fields) == 1


def test_readings_follow_the_tag_and_count_changes() -> None:
    field = m.Field("#K7QF", "型号", "QX-1O5S")
    texts = [
        "[#K7QF] 型号: QX-1O5S, next",
        "nothing here",
        "#K7QF 型号:QX-105S",
        "[#K7QF] QX-1O5S",
        "[#K7QF] 型号: QX-1O5S",
    ]
    assert m.readings(texts, field) == ["QX-1O5S", "QX-105S", "QX-1O5S", "QX-1O5S"]
    assert m.changes(m.readings(texts, field)) == 2
    assert m.variants(m.readings(texts, field)) == 2


def test_full_width_punctuation_and_amount_commas() -> None:
    code = m.Field("#K7QF", "型号", "QX-1O5S")
    assert m.readings(["[#K7QF] 型号\uff1aQX-105S\uff0c下一项"], code) == ["QX-105S"]
    amount = m.Field("#AMNT", "应付金额", "12,480.50")
    assert m.readings(["[#AMNT] 应付金额: 12,480.50\n"], amount) == ["12,480.50"]
    address = m.Field("#ADDR", "Registered address", "Unit A3, 15 Harbour Rd")
    assert m.readings(["[#ADDR] Registered address: Unit A3, 15 Harbour Rd; next"], address) == [
        "UnitA3,15HarbourRd"
    ]


def test_a_reading_without_the_tag_falls_back_to_the_label() -> None:
    field = m.Field("#K7QF", "Model", "PK-57I7")
    assert m.readings(["Model: PK-5717 and more", "Model: PK-57I7"], field) == [
        "PK-5717",
        "PK-57I7",
    ]


# --------------------------------------------------------------------------- re-copy steps


_PAGE = "本期往来明细如下请核对后签字确认如有差异请于收到后七个工作日内书面提出"


def test_a_step_that_copies_a_fetched_page_again_is_a_recopy_step() -> None:
    steps = [
        _step(0, reads=[{"path": "a.pdf", "units": [1]}]),
        _step(1, thinking=_PAGE),  # 取页后的第一步不算
        _step(2, thinking="写脚本"),
        _step(3, thinking="再看一遍:" + _PAGE[:20]),
    ]
    flags = m.recopy_flags(steps, {("a.pdf", 1): _PAGE})
    assert flags == [False, False, False, True]


def test_copying_a_page_not_yet_fetched_is_not_a_recopy() -> None:
    steps = [_step(0, thinking=_PAGE), _step(1, reads=[{"path": "a.pdf", "units": [1]}])]
    assert m.recopy_flags(steps, {("a.pdf", 1): _PAGE}) == [False, False]


def test_a_small_overlap_is_not_a_recopy() -> None:
    steps = [
        _step(0, reads=[{"path": "a.pdf", "units": [1]}]),
        _step(1),
        _step(2, thinking=_PAGE[:7]),
    ]
    assert m.recopy_flags(steps, {("a.pdf", 1): _PAGE}) == [False, False, False]


# --------------------------------------------------------------------------- refetch after retire


def test_refetches_after_the_window_are_counted() -> None:
    steps = [
        _step(0, reads=[{"path": "a.pdf", "units": [1, 2]}]),
        _step(1),
        _step(2),
        _step(3, reads=[{"path": "a.pdf", "units": [1]}]),  # 1+3 = 4 > 3: 还在
        _step(4),
        _step(5),
        _step(6),
        _step(7, reads=[{"path": "a.pdf", "units": [2]}]),  # 3+1+3 = 7: 已过期
        _step(8, reads=[{"path": "b.pdf", "units": [1]}]),  # 本轮第一次读 b
    ]
    assert m.refetches_after_retire(steps, view_steps=3) == 1
    assert m.refetches_after_retire(steps, view_steps=3, read_earlier={"b.pdf"}) == 2


# --------------------------------------------------------------------------- runaway


def _turn(steps: int | tuple[m.Step, ...] = 5, **kw: object) -> m.Turn:
    base: dict[str, object] = {
        "doc_id": "d",
        "steps": steps if isinstance(steps, tuple) else tuple(_step(i) for i in range(steps)),
        "exit_reason": "text_response",
        "first_deliverable_step": None,
        "warnings": (),
    }
    base.update(kw)
    return m.Turn(**base)  # type: ignore[arg-type]


def test_runaway_symptoms() -> None:
    assert m.runaway_reasons(_turn(), [], {}) == []
    assert m.runaway_reasons(_turn(steps=12, first_deliverable_step=1), [], {}) == [
        "calls_after_deliverable"
    ]
    assert m.runaway_reasons(_turn(steps=11, first_deliverable_step=1), [], {}) == []
    assert m.runaway_reasons(_turn(warnings=("deadline",)), [], {}) == ["limit"]
    assert m.runaway_reasons(_turn(exit_reason="max_steps"), [], {}) == ["limit"]
    # token 提醒 / 封顶不算(复审 R3-M1)
    assert (
        m.runaway_reasons(_turn(warnings=("token_budget",), exit_reason="token_budget"), [], {})
        == []
    )
    field = m.Field("#K7QF", "型号", "A")
    flips = [_step(i, f"[#K7QF] 型号: {'AB'[i % 2]}") for i in range(4)]
    assert m.runaway_reasons(_turn(tuple(flips)), [field], {}) == ["field_flips"]


def test_recopy_share_of_thirty_percent_is_runaway() -> None:
    steps = [_step(0, reads=[{"path": "a.pdf", "units": [1]}]), _step(1), _step(2, thinking=_PAGE)]
    turn = _turn(tuple(steps))
    assert m.runaway_reasons(turn, [], {("a.pdf", 1): _PAGE}) == ["recopy"]


# --------------------------------------------------------------------------- rotation (B-268)


def test_rotation_is_a_re_read_after_every_other_page() -> None:
    rotating = [[1, 2, 3], [4], [1]]
    assert m.rotation(rotating, pages=4) is True
    # 2、3 在 3 槽窗口里还在 —— 重看它们不是「最早那页退出 → 重看」的轮转
    assert m.rotation([[1, 2, 3], [4], [2, 3]], pages=4) is False
    assert m.rotation([[1], [2], [3], [4], [1], [2]], pages=4) is True
    assert m.rotation([[1, 2, 3], [4, 5, 6]], pages=6) is False
    assert m.rotation([[1, 2], [1]], pages=4) is False


# --------------------------------------------------------------------------- verdicts


def _doc(**kw: object) -> m.DocResult:
    base: dict[str, object] = {
        "doc_id": "d",
        "category": "c",
        "runaway": False,
        "correct": 10,
        "flipped_fields": 0,
        "recopy_share": 0.0,
        "calls_after_deliverable": 2,
        "thinking_median": 100.0,
        "refetches": (0, 0, 0),
        "no_progress_runs": 0,
        "wall_s": 100.0,
        "tokens": 1000,
        "correction_applied": True,
        "template_ok": None,
        "structure": 3,
    }
    base.update(kw)
    return m.DocResult(**base)  # type: ignore[arg-type]


def _arm(n: int = 12, **kw: object) -> list[m.DocResult]:
    return [_doc(doc_id=f"d{i}", category=f"c{i // 2}", **kw) for i in range(n)]


def test_criterion_0_needs_three_runaways_in_arm_a() -> None:
    a = _arm()
    assert m.reproduced(a) is False
    a3 = [_doc(doc_id=d.doc_id, category=d.category, runaway=i < 3) for i, d in enumerate(a)]
    assert m.reproduced(a3) is True


def test_criterion_1_pairs_documents() -> None:
    a = [_doc(doc_id=f"d{i}", runaway=i < 4) for i in range(12)]
    x = [_doc(doc_id=f"d{i}", runaway=i in (3, 5)) for i in range(12)]
    assert m.fewer_runaways(a, x) is True  # 3 fixed, 1 new
    y = [_doc(doc_id=f"d{i}", runaway=i in (3, 5, 6)) for i in range(12)]
    assert m.fewer_runaways(a, y) is False  # 2 new


def test_criterion_1_prime_needs_a_real_margin_over_b() -> None:
    b = _arm(calls_after_deliverable=5, thinking_median=200.0)
    c_win = _arm(calls_after_deliverable=4, thinking_median=200.0)
    assert m.layer2_adds(b, c_win) is True
    # 相等不算赢;没交付件算 C 没赢
    tie = _arm(calls_after_deliverable=5, thinking_median=200.0)
    assert m.layer2_adds(b, tie) is False
    nine = [
        _doc(doc_id=f"d{i}", calls_after_deliverable=4 if i < 9 else None, thinking_median=200.0)
        for i in range(12)
    ]
    assert m.layer2_adds(b, nine) is False


def test_criterion_2_accuracy_overall_and_per_category() -> None:
    a = _arm(correct=10)
    assert m.accuracy_held(a, _arm(correct=10)) is True
    three_less = [
        _doc(doc_id=d.doc_id, category=d.category, correct=10 - (i == 0) * 3)
        for i, d in enumerate(a)
    ]
    assert m.accuracy_held(a, three_less) is True
    # 总数不变,但一类少对 4 个(另一类多对 4 个)
    shifted = [
        _doc(
            doc_id=d.doc_id,
            category=d.category,
            correct={"c0": 8, "c1": 12}.get(d.category, 10),
        )
        for d in a
    ]
    assert m.accuracy_held(a, shifted) is False
    # 每类只少 2 个,但总数少 4 个
    four_less = [
        _doc(doc_id=d.doc_id, category=d.category, correct=10 - (i < 4)) for i, d in enumerate(a)
    ]
    assert m.accuracy_held(a, four_less) is False


def test_criterion_3_flips() -> None:
    a = _arm(flipped_fields=1)  # A = 12
    assert m.flips_halved(a, _arm(flipped_fields=0)) is True
    assert (
        m.flips_halved(
            a, [_doc(doc_id=f"d{i}", flipped_fields=1 if i < 7 else 0) for i in range(12)]
        )
        is False
    )
    zero = _arm(flipped_fields=0)
    assert (
        m.flips_halved(zero, [_doc(doc_id="d0", flipped_fields=1), *_arm(11, flipped_fields=0)])
        is True
    )
    assert (
        m.flips_halved(zero, [_doc(doc_id="d0", flipped_fields=2), *_arm(11, flipped_fields=0)])
        is False
    )


def test_criterion_5_refetch_gate() -> None:
    a = _arm()
    assert m.refetch_gate(a, _arm(refetches=(1, 1, 1))) is True
    assert m.refetch_gate(a, _arm(refetches=(2, 2, 2))) is False  # 中位数 2
    spike = [_doc(doc_id=f"d{i}", refetches=(0, 6 if i == 0 else 0)) for i in range(12)]
    assert m.refetch_gate(a, spike) is False
    # 按「每次运行」算(独立审查 MEDIUM-2):每份文档 3 次运行合计 3 次、但每次运行的中位数是 0
    per_run = _arm(refetches=(0, 0, 3))
    assert m.refetch_gate(a, per_run) is True
    # 无进展收尾按运行数计:C 一共 2 次运行,A 0 次
    stuck = [_doc(doc_id=f"d{i}", no_progress_runs=1 if i < 2 else 0) for i in range(12)]
    assert m.refetch_gate(a, stuck) is False
    one_doc_two_runs = [
        _doc(doc_id=f"d{i}", no_progress_runs=2 if i == 0 else 0) for i in range(12)
    ]
    assert m.refetch_gate(a, one_doc_two_runs) is False


def test_criterion_6_cost_and_time_only_on_documents_neither_ran_away() -> None:
    a = _arm(wall_s=100.0, tokens=1000)
    assert m.not_slower(a, _arm(wall_s=110.0, tokens=1100)) is True
    assert m.not_slower(a, _arm(wall_s=120.0, tokens=1000)) is False
    runaway_x = _arm(wall_s=500.0, tokens=1000, runaway=True)
    assert m.not_slower(a, runaway_x) is None  # no document where neither ran away


def test_criterion_6_without_any_usable_ratio_is_none_not_an_error() -> None:
    """独立审查 LOW-4:A 的墙钟 / token 全为 0 时没有可除的,返回 None,不抛 StatisticsError。"""
    zero = _arm(wall_s=0.0, tokens=0)
    assert m.not_slower(zero, _arm()) is None


def test_criterion_8_and_9() -> None:
    a = [_doc(doc_id=f"d{i}", correction_applied=(True if i < 6 else None)) for i in range(12)]
    x = [_doc(doc_id=f"d{i}", correction_applied=(i < 5 if i < 6 else None)) for i in range(12)]
    assert m.corrections_followed(a, x) is True
    y = [_doc(doc_id=f"d{i}", correction_applied=(i < 4 if i < 6 else None)) for i in range(12)]
    assert m.corrections_followed(a, y) is False
    t_a = [_doc(doc_id=f"d{i}", template_ok=(True if i < 4 else None)) for i in range(12)]
    t_x = [_doc(doc_id=f"d{i}", template_ok=(i != 0 if i < 4 else None)) for i in range(12)]
    assert m.format_kept(t_a, t_a) is True
    assert m.format_kept(t_a, t_x) is False
    shaped = [_doc(doc_id=f"d{i}", structure=3 if i < 9 else 6) for i in range(12)]
    assert m.format_kept(_arm(), shaped) is False


def _passing_arms() -> dict[str, list[m.DocResult]]:
    a = [
        _doc(doc_id=f"d{i}", category=f"c{i // 2}", runaway=i < 4, flipped_fields=1)
        for i in range(12)
    ]
    good = [_doc(doc_id=f"d{i}", category=f"c{i // 2}", runaway=False) for i in range(12)]
    return {"A": a, "B": good, "C": good}


def test_a_model_where_everything_passes_passes_both_layers() -> None:
    arms = _passing_arms()
    # C 每份文档交付后的调用都少于 B
    arms["C"] = [
        _doc(doc_id=d.doc_id, category=d.category, calls_after_deliverable=1) for d in arms["B"]
    ]
    verdict = m.model_verdict(arms, regression_ok=True)
    assert verdict.reproduced is True
    assert verdict.layer1 is True
    assert verdict.layer2 is True


def test_layer_2_fails_when_it_adds_nothing_over_layer_1() -> None:
    verdict = m.model_verdict(_passing_arms(), regression_ok=True)
    assert verdict.layer1 is True
    assert verdict.layer2 is False
    assert "1'" in verdict.failed["C"]


def test_a_model_with_only_a_and_c_decides_both_layers_together() -> None:
    arms = _passing_arms()
    del arms["B"]
    verdict = m.model_verdict(arms, regression_ok=None)
    assert verdict.layer1 is True and verdict.layer2 is True
    arms["C"] = [_doc(doc_id=d.doc_id, category=d.category, correct=5) for d in arms["C"]]
    verdict = m.model_verdict(arms, regression_ok=None)
    assert verdict.layer1 is False and verdict.layer2 is False


def test_a_model_that_did_not_reproduce_is_judged_on_not_worse_only() -> None:
    good = _arm()
    verdict = m.model_verdict({"A": good, "B": good, "C": good}, regression_ok=None)
    assert verdict.reproduced is False
    assert verdict.layer1 is True and verdict.layer2 is True


def test_landing_needs_every_tested_model_to_pass() -> None:
    v_pass = m.ModelVerdict(reproduced=True, layer1=True, layer2=True, failed={})
    v_fail = m.ModelVerdict(reproduced=True, layer1=True, layer2=False, failed={"C": ["5"]})
    landing = m.landing({"glm-5.3-flash": v_pass, "deepseek-flash": v_fail})
    assert landing["layer1"] == {
        "platform_default_on": True,
        "models_passed": ["deepseek-flash", "glm-5.3-flash"],
    }
    assert landing["layer2"] == {"platform_default_on": False, "models_passed": ["glm-5.3-flash"]}


@pytest.mark.parametrize("model", ["glm-5.3-flashx"])
def test_flashx_inherits_flash(model: str) -> None:
    assert m.verdict_model(model) == "glm-5.3-flash"
    assert m.verdict_model("deepseek-flash") == "deepseek-flash"
