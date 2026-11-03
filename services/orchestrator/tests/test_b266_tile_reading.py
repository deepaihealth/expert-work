"""B-266 review M3 —— ``region="auto"`` 每块的 JSON 回答:解析阶梯、散文兜底、读数归一、
保守汇总(宁可报冲突,不可错合并)。"""

from __future__ import annotations

import json

import pytest

from orchestrator.tools.region_zoom import auto_bands
from orchestrator.tools.tile_reading import (
    TILE_INSTRUCTION,
    TileResult,
    aggregate_tiles,
    compatible,
    decide_auto,
    parse_reading,
    parse_tile_reply,
    tile_meta,
    tile_question,
)

_TILES = auto_bands(6)


def _json(status: str, value: str = "", *, unit: str = "", label: str = "") -> str:
    return json.dumps({"status": status, "label": label, "value": value, "unit": unit})


# ---------------------------------------------------------------------------
# 提示词
# ---------------------------------------------------------------------------


def test_the_tile_prompt_keeps_the_question_and_asks_for_json() -> None:
    prompt = tile_question("What is the glucose value?")
    assert prompt.startswith("What is the glucose value?\n\n")
    assert prompt.endswith(TILE_INSTRUCTION)
    for needle in ("JSON", '"not_found"', '"partial"', "Do not guess", '"label"', '"unit"'):
        assert needle in TILE_INSTRUCTION
    assert "reason" not in TILE_INSTRUCTION
    assert TILE_INSTRUCTION.isascii() is False  # 保留了 < > ↑ ↓ ± 这些记号的说明


# ---------------------------------------------------------------------------
# 解析阶梯
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        _json("found", "5.6", unit="mmol/L", label="GLU"),
        "```json\n" + _json("found", "5.6", unit="mmol/L", label="GLU") + "\n```",
        "Here is the reading: " + _json("found", "5.6", unit="mmol/L", label="GLU") + " Done.",
        "<think>the row says {GLU}</think>" + _json("found", "5.6", unit="mmol/L", label="GLU"),
        _json("FOUND", "5.6", unit="mmol/L", label="GLU"),
    ],
    ids=["clean", "fenced", "prose-then-json", "think-prefix", "status-casing"],
)
def test_json_replies_parse(text: str) -> None:
    reply = parse_tile_reply(text)
    assert (reply.status, reply.parse_mode) == ("found", "json")
    assert (reply.label, reply.value, reply.unit) == ("GLU", "5.6", "mmol/L")


def test_found_with_an_empty_value_is_not_found() -> None:
    reply = parse_tile_reply(_json("found", ""))
    assert reply.status == "not_found"
    assert reply.note == "found_without_value"


def test_found_with_a_not_found_phrase_as_value_is_not_found() -> None:
    reply = parse_tile_reply(_json("found", "not visible"))
    assert reply.status == "not_found"


def test_not_found_with_a_value_stays_not_found_and_keeps_the_raw_text() -> None:
    text = _json("not_found", "5.6")
    reply = parse_tile_reply(text)
    assert (reply.status, reply.value, reply.note) == ("not_found", "", "not_found_with_value")
    assert reply.raw == text


def test_partial_is_kept() -> None:
    assert parse_tile_reply(_json("partial", "5.")).status == "partial"


def test_an_unknown_status_is_unparseable() -> None:
    assert parse_tile_reply(_json("maybe", "5.6")).status == "unparseable"


@pytest.mark.parametrize(
    "text",
    [
        "NOT_FOUND",
        "**NOT_FOUND**",
        "The value is NOT_FOUND",
        "\u672a\u627e\u5230",  # 未找到
        "\u56fe\u4e2d\u6ca1\u6709\u8be5\u9879",  # 图中没有该项
        # 该图块中未显示血红蛋白
        "\u8be5\u56fe\u5757\u4e2d\u672a\u663e\u793a\u8840\u7ea2\u86cb\u767d",
        "\u770b\u4e0d\u5230\u5b8c\u6574\u6570\u503c",  # 看不到完整数值
        "\u65e0\u6cd5\u8bc6\u522b",  # 无法识别
        "N/A",
        "\u65e0\u3002",  # 无。
        "The value is not visible in this tile.",
        "I cannot read it.",
    ],
)
def test_prose_not_found_replies(text: str) -> None:
    reply = parse_tile_reply(text)
    assert (reply.status, reply.parse_mode) == ("not_found", "prose")


@pytest.mark.parametrize(
    "text",
    [
        "\u672a\u68c0\u51fa",  # 未检出
        "\u672a\u89c1\u5f02\u5e38",  # 未见异常
        "\u9634\u6027",  # 阴性
        "\u9634\u6027(-)",  # 阴性(-)
        "\u65e0\u5f02\u5e38",  # 无异常
        "-",
        "+",
        "<0.5",
        "Not detected",
    ],
)
def test_lab_results_stay_values(text: str) -> None:
    reply = parse_tile_reply(text)
    assert (reply.status, reply.parse_mode, reply.value) == ("found", "prose", text)


def test_long_prose_with_numbers_is_still_unparseable() -> None:
    text = "The table lists several values such as 5.6, 7.1 and 120, but none is the one asked for."
    assert parse_tile_reply(text).status == "unparseable"


def test_long_prose_is_unparseable_and_never_a_value() -> None:
    text = (
        "This tile shows part of the report header with the patient's name, the date of the "
        "visit and the name of the clinic."
    )
    reply = parse_tile_reply(text)
    assert (reply.status, reply.parse_mode) == ("unparseable", "none")


# ---------------------------------------------------------------------------
# 读数归一与相容
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("5.6", "5.6 mmol/L"),
        ("5.6", "5.60"),
        ("\uff15\uff0e\uff16", "5.6"),  # 全角数字
        ("12.3\u2191", "12.3"),
        ("3.5-5.1", "3.5\uff5e5.1"),
        ("3.5\uff5e5.1", "3.5 \u81f3 5.1"),  # 至
        ("\u9634\u6027", "(-)"),
        ("(-)", "negative"),
        ("4.5\u00d710^9/L", "4.5 10*9/L"),
        ("5 \u03bcmol/L", "5 umol/L"),
        ("5 \u00b5mol/L", "5 \u03bcmol/L"),
        ("12.3 H", "12.3\u2191"),
        ("1.2\u00d710^9", "1.2E9"),
        ("1,234", "1234"),
        ("\u9634\u6027(-)", "(-)"),  # 阴性(-)
    ],
)
def test_same_readings(a: str, b: str) -> None:
    ok, _ = compatible(parse_reading(a), parse_reading(b))
    assert ok, (a, b)


@pytest.mark.parametrize(
    ("a", "b", "reason"),
    [
        ("5.6", "5.9", ""),
        ("120 g/L", "12.0 g/dL", "units differ"),
        ("<0.5", "0.5", "band edge may have cut it off"),
        ("5\u2191", "5\u2193", "flagged high and the other low"),
        ("+", "++", ""),
        ("\u9634\u6027", "\u5f31\u9633\u6027", ""),  # 阴性 / 弱阳性
        ("1,234", "1.234", ""),
        ("5.6 g/L", "5.6 g/dL", "units differ"),
        # 不是千分位的逗号不去掉(可能是小数逗号):宁可报冲突。
        ("12,34", "1234", ""),
    ],
)
def test_conflicting_readings(a: str, b: str, reason: str) -> None:
    ok, why = compatible(parse_reading(a), parse_reading(b))
    assert not ok, (a, b)
    assert reason in why


def test_units_are_compared_from_the_separate_unit_field_too() -> None:
    ok, why = compatible(parse_reading("120", "g/L"), parse_reading("120", "g/dL"))
    assert not ok and "units differ" in why


# ---------------------------------------------------------------------------
# 汇总
# ---------------------------------------------------------------------------


def _results(*replies: str | None) -> list[TileResult]:
    return [
        TileResult(
            tile=tile,
            reply=parse_tile_reply(text) if text is not None else None,
            error=None if text is not None else "timed out",
        )
        for tile, text in zip(_TILES, replies, strict=True)
    ]


_NF = _json("not_found")


def test_all_not_found_says_so_and_invents_nothing() -> None:
    verdict = aggregate_tiles(_results(*[_NF] * 6))
    assert verdict.outcome == "none"
    assert "no band could see the complete answer (6 replied not_found)" in verdict.content
    assert "region=[" not in verdict.content


def test_partial_tiles_suggest_a_region_around_their_union() -> None:
    verdict = aggregate_tiles(_results(_NF, _NF, _json("partial"), _json("partial"), _NF, _NF))
    assert verdict.outcome == "none"
    third, fourth = _TILES[2].box, _TILES[3].box
    union = [
        min(third[0], fourth[0]),
        min(third[1], fourth[1]),
        max(third[2], fourth[2]),
        max(third[3], fourth[3]),
    ]
    assert f"region={union}" in verdict.content
    assert "band 3, band 4 saw it cut off" in verdict.content


def test_one_value_and_five_not_found_is_the_answer() -> None:
    verdict = aggregate_tiles(
        _results(_NF, _NF, _json("found", "5.6", unit="mmol/L", label="GLU"), _NF, _NF, _NF)
    )
    assert verdict.outcome == "single"
    assert verdict.content.startswith("5.6 mmol/L\n")
    assert 'label "GLU"' in verdict.content
    assert "read in band 3" in verdict.content
    assert "5 band(s) replied not_found" in verdict.content


def test_agreeing_tiles_show_the_most_complete_form_and_the_variants() -> None:
    verdict = aggregate_tiles(
        _results(
            _NF,
            _NF,
            _json("found", "5.6", label="GLU"),
            _json("found", "5.60\u2191", unit="mmol/L", label="GLU"),
            _NF,
            _NF,
        )
    )
    assert verdict.outcome == "single"
    assert verdict.content.startswith("5.60\u2191 mmol/L\n")
    assert 'written as "5.6" / "5.60\u2191 mmol/L"' in verdict.content


def test_two_agree_and_one_disagrees_is_a_conflict_not_a_vote() -> None:
    verdict = aggregate_tiles(
        _results(
            _json("found", "5.6", label="GLU"),
            _json("found", "5.6", label="GLU"),
            _json("found", "5.9", label="GLU"),
            _NF,
            _NF,
            _NF,
        )
    )
    assert verdict.outcome == "conflict"
    assert "same label, different readings" in verdict.content
    assert "no reading is preferred by count" in verdict.content
    first = list(_TILES[0].box)
    assert first == [0, 0, 1000, first[3]]
    assert f'- band 1 region={first} label "GLU": "5.6"' in verdict.content


def test_a_reading_must_agree_with_every_member_of_a_group() -> None:
    """「5.6」与「5.6 g/L」相容、也与「5.6 g/dL」相容,但后两个互不相容:不能经由第一个串成一组。"""
    verdict = aggregate_tiles(
        _results(
            _json("found", "5.6"),
            _json("found", "5.6", unit="g/L"),
            _json("found", "5.6", unit="g/dL"),
            _NF,
            _NF,
            _NF,
        )
    )
    assert verdict.outcome == "conflict"
    assert "the units differ" in verdict.content


def test_different_labels_with_different_values_point_at_different_rows() -> None:
    verdict = aggregate_tiles(
        _results(
            _json("found", "5.6", label="GLU"),
            _NF,
            _json("found", "7.1", label="HbA1c"),
            _NF,
            _NF,
            _NF,
        )
    )
    assert verdict.outcome == "conflict"
    assert "probably different rows or columns" in verdict.content


def test_a_comparator_conflict_says_the_edge_may_have_cut_it() -> None:
    verdict = aggregate_tiles(
        _results(_json("found", "<0.5"), _json("found", "0.5"), _NF, _NF, _NF, _NF)
    )
    assert verdict.outcome == "conflict"
    assert "a band edge may have cut it off" in verdict.content


def test_a_failed_tile_beside_a_value() -> None:
    verdict = aggregate_tiles(_results(None, _NF, _json("found", "5.6"), _NF, _NF, _NF))
    assert verdict.outcome == "single"
    assert verdict.content.startswith("5.6\n")
    assert "band 1 (timed out)" in verdict.content


def test_unparseable_tiles_are_named_and_never_values() -> None:
    long = "x" * 60
    verdict = aggregate_tiles(_results(*[long] * 6))
    assert verdict.outcome == "none"
    assert verdict.content.count("could not be read, so it was not counted") == 6


def test_an_unparseable_tile_does_not_create_a_conflict() -> None:
    long = "This tile mostly shows a table header and some unrelated small print near the edge."
    verdict = aggregate_tiles(_results(_json("found", "5.6"), long, _NF, _NF, _NF, _NF))
    assert verdict.outcome == "single"


def test_tile_meta_records_the_parsed_reply() -> None:
    text = "```json\n" + _json("found", "5.6", unit="mmol/L", label="GLU") + "\n```"
    (result,) = [TileResult(tile=_TILES[2], reply=parse_tile_reply(text))]
    entry = tile_meta(result)
    assert entry == {
        "tile": "band 3",
        "box": list(_TILES[2].box),
        "status": "found",
        "raw": text,
        "parse_mode": "json",
        "label": "GLU",
        "value": "5.6",
        "unit": "mmol/L",
    }
    long = "y" * 500
    assert (
        len(str(tile_meta(TileResult(tile=_TILES[0], reply=parse_tile_reply(long)))["raw"])) == 200
    )
    failed = tile_meta(TileResult(tile=_TILES[0], reply=None, error="timed out"))
    assert failed == {"tile": "band 1", "box": list(_TILES[0].box), "error": "timed out"}


# ---------------------------------------------------------------------------
# 重设计 §4 —— 横条值 / 退回整张读
# ---------------------------------------------------------------------------

_FULL = "The glucose value is 5.6 mmol/L."


def test_the_band_prompt_has_the_redesign_clauses() -> None:
    assert "full-width horizontal band" in TILE_INSTRUCTION
    assert "the requested value or text (number, name, date, word...) exactly as printed" in (
        TILE_INSTRUCTION
    )
    assert (
        "If this part of the image alone is not enough to answer - e.g. the question needs "
        "counting or comparing across the whole image, or an axis, legend or row/column header "
        'that is not visible here - use "not_found".'
    ) in TILE_INSTRUCTION


def test_a_single_band_value_is_the_answer() -> None:
    results = _results(_NF, _NF, _json("found", "5.6", unit="mmol/L"), _NF, _NF, _NF)
    decision = decide_auto(results, full_answer=_FULL, full_error=None)
    assert (decision.path, decision.reason, decision.bands) == ("bands", "single", "single")
    assert decision.content.startswith("5.6 mmol/L\n")
    assert _FULL not in decision.content


def test_a_band_conflict_falls_back_to_the_whole_image_answer_with_band_details() -> None:
    results = _results(_json("found", "5.6"), _NF, _json("found", "5.9"), _NF, _NF, _NF)
    decision = decide_auto(results, full_answer=_FULL, full_error=None)
    assert (decision.path, decision.reason, decision.bands) == (
        "full_fallback",
        "conflict",
        "conflict",
    )
    assert decision.content.startswith(_FULL + "\n\n")
    assert "whole-image reading; small print may be unreliable" in decision.content
    assert "the bands disagree" in decision.content
    assert f"- band 1 region={list(_TILES[0].box)}" in decision.content
    assert f"- band 3 region={list(_TILES[2].box)}" in decision.content


def test_no_band_value_falls_back_to_the_whole_image_answer() -> None:
    decision = decide_auto(_results(*[_NF] * 6), full_answer=_FULL, full_error=None)
    assert (decision.path, decision.reason) == ("full_fallback", "none")
    assert decision.content.startswith(_FULL)
    assert "no band read the value" in decision.content


def test_a_partial_band_beats_a_single_found_value() -> None:
    """规则 4c:有一条报「被边缘截断」,哪怕别的条给出唯一一个值,也不采用横条的值。"""
    results = _results(_NF, _json("partial"), _json("found", "3"), _NF, _NF, _NF)
    decision = decide_auto(results, full_answer="There are 5 bars.", full_error=None)
    assert (decision.path, decision.reason) == ("full_fallback", "partial")
    assert decision.content.startswith("There are 5 bars.")
    assert "a band saw the value cut off at its edge" in decision.content


def test_all_bands_failed_falls_back_to_the_whole_image_answer() -> None:
    decision = decide_auto(_results(*[None] * 6), full_answer=_FULL, full_error=None)
    assert (decision.path, decision.reason) == ("full_fallback", "bands_failed")


def test_a_failed_whole_image_read_leaves_the_band_summary() -> None:
    results = _results(_json("found", "5.6"), _NF, _json("found", "5.9"), _NF, _NF, _NF)
    decision = decide_auto(results, full_answer=None, full_error="timed out")
    assert (decision.path, decision.reason) == ("bands", "conflict+full_failed")
    assert "the bands gave conflicting readings" in decision.content
    assert decision.content.startswith("region='auto': no reliable reading - the bands disagree")
    assert "the whole-image read failed too (timed out)" in decision.content
