"""B-273 —— whole-image questions are answered from the whole image (V1); the band rule that stays
as a fallback: R2 (several bands with the same value count only as one row seen twice where two
neighbouring bands overlap)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from expert_work.persistence import region_crop_stem
from orchestrator.tools.region_zoom import auto_bands
from orchestrator.tools.tile_reading import (
    TileResult,
    decide_auto,
    decide_whole_question,
    parse_tile_reply,
)
from orchestrator.tools.vision import ask_image_region_auto_total

from .test_b266_region_zoom import (
    _VL,
    NOT_FOUND,
    _found,
    _image_ref_of,
    _is_band,
    _question_of,
    _upload,
)

_TILES = auto_bands(6)
_FIRST = region_crop_stem(auto_bands(4)[0].box)
_FULL = "There are 7 rows in total."


def _json(status: str, value: str = "", *, label: str = "") -> str:
    return json.dumps({"status": status, "label": label, "value": value, "unit": ""})


_NF = _json("not_found")


def _results(*replies: str) -> list[TileResult]:
    return [
        TileResult(tile=tile, reply=parse_tile_reply(text))
        for tile, text in zip(_TILES, replies, strict=True)
    ]


# ---------------------------------------------------------------------------
# R2 —— several bands with one value
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("labels"),
    [("GLU", "GLU"), ("GLU", ""), ("", "")],
    ids=["same-label", "one-empty", "both-empty"],
)
def test_two_neighbouring_bands_with_one_value_are_one_row_seen_twice(
    labels: tuple[str, str],
) -> None:
    results = _results(
        _NF,
        _NF,
        _json("found", "5.6", label=labels[0]),
        _json("found", "5.6", label=labels[1]),
        _NF,
        _NF,
    )
    decision = decide_auto(results, full_answer=_FULL, full_error=None)
    assert (decision.path, decision.reason) == ("bands", "single")


def test_two_bands_far_apart_with_one_value_fall_back_to_the_whole_image() -> None:
    """E1: equal partial counts from different parts of a long list looked like agreement."""
    results = _results(_json("found", "3"), _NF, _NF, _json("found", "3"), _NF, _NF)
    decision = decide_auto(results, full_answer=_FULL, full_error=None)
    assert (decision.path, decision.reason) == ("full_fallback", "multi_band")
    assert decision.content.startswith(_FULL)
    assert "more than one band reported this value" in decision.content


def test_three_bands_with_one_value_fall_back_to_the_whole_image() -> None:
    results = _results(_json("found", "3"), _json("found", "3"), _json("found", "3"), _NF, _NF, _NF)
    decision = decide_auto(results, full_answer=_FULL, full_error=None)
    assert (decision.path, decision.reason) == ("full_fallback", "multi_band")


def test_neighbouring_bands_with_different_labels_fall_back() -> None:
    results = _results(
        _NF, _json("found", "3", label="Mon"), _json("found", "3", label="Tue"), _NF, _NF, _NF
    )
    decision = decide_auto(results, full_answer=_FULL, full_error=None)
    assert (decision.path, decision.reason) == ("full_fallback", "multi_band")


# ---------------------------------------------------------------------------
# V1 —— whole-image questions: the whole-image answer is used, the bands only flag it
# ---------------------------------------------------------------------------


def _whole(results: list[TileResult], *, count: bool = False) -> Any:
    return decide_whole_question(results, full_answer=_FULL, full_error=None, count_question=count)


def test_the_whole_image_answer_is_used_when_no_band_contradicts_it() -> None:
    decision = _whole(_results(_NF, _NF, _NF, _NF, _NF, _NF))
    assert (decision.path, decision.reason) == ("whole_question", "agrees")
    assert decision.content.startswith(f"{_FULL}\n\n(region='auto': the question is about")


def test_a_band_value_the_whole_image_answer_also_gives_agrees() -> None:
    decision = _whole(_results(_NF, _json("found", "7", label="Rows"), _NF, _NF, _NF, _NF))
    assert (decision.path, decision.reason) == ("whole_question", "agrees")


def test_a_labelled_band_value_the_whole_image_answer_does_not_give_is_noted_neutrally() -> None:
    """A wrongly routed lookup ("platelet count"): the band that saw the labelled field is
    attached, without presenting either value as the likely answer."""
    decision = _whole(_results(_NF, _NF, _json("found", "245", label="PLT"), _NF, _NF, _NF))
    assert (decision.path, decision.reason) == ("whole_question", "bands_disagree")
    assert decision.content == (
        f"{_FULL}\n\n(region='auto': whole-image reading: the answer above. A band (which sees "
        f"only part of the image) read field 'PLT' as 245 at region={list(_TILES[2].box)}. If the "
        "question asks for a value printed in the image, check that field with a region zoom.)"
    )
    assert "is the answer" not in decision.content


def test_an_unlabelled_partial_value_does_not_flag() -> None:
    """E1: on whole-image questions one band reporting its own partial count is the common case."""
    decision = _whole(_results(_NF, _NF, _json("found", "3"), _NF, _NF, _NF))
    assert (decision.path, decision.reason) == ("whole_question", "agrees")


def test_a_counting_question_never_flags() -> None:
    decision = _whole(
        _results(_NF, _json("found", "3", label="Rows"), _NF, _NF, _NF, _NF), count=True
    )
    assert (decision.path, decision.reason) == ("whole_question", "agrees")


def test_labelled_bands_far_apart_do_not_flag() -> None:
    """R2: the same labelled value in two non-adjacent bands is not one field seen twice."""
    results = _results(
        _json("found", "3", label="A"), _NF, _NF, _json("found", "3", label="A"), _NF, _NF
    )
    assert _whole(results).reason == "agrees"


def test_one_field_seen_by_two_neighbouring_bands_flags() -> None:
    results = _results(
        _NF, _json("found", "245", label="PLT"), _json("found", "245"), _NF, _NF, _NF
    )
    assert _whole(results).reason == "bands_disagree"


def test_bands_that_disagree_with_each_other_do_not_flag_the_whole_image_answer() -> None:
    results = _results(
        _json("found", "2", label="A"), _NF, _NF, _json("found", "3", label="B"), _NF, _NF
    )
    assert _whole(results).reason == "agrees"


def test_without_a_whole_image_answer_the_band_rules_apply() -> None:
    results = _results(_NF, _NF, _json("found", "245"), _NF, _NF, _NF)
    decision = decide_whole_question(
        results, full_answer=None, full_error="timed out", count_question=False
    )
    assert (decision.path, decision.reason) == ("bands", "single")


@pytest.mark.anyio
async def test_a_whole_image_question_reads_bands_and_whole_and_prefers_whole(
    tmp_path: Path,
) -> None:
    def answer(ref: str) -> str:
        if not _is_band(ref):
            return "7 rows"
        return _found("3", label="Pending") if ref.endswith(f"/{_FIRST}.jpg") else NOT_FOUND

    vl = _VL(answer=answer)
    up = _upload(tmp_path, vl, size=(3000, 2000))  # 6 MP: 4 bands
    labels = {"path": "whole_question", "reason": "agrees"}
    before = ask_image_region_auto_total.labels(**labels)._value.get()

    result = await up.tool.call(
        {"image_ref": up.ref, "question": "How many rows are marked pending?", "region": "auto"},
        ctx=up.ctx,
    )

    assert len(vl.calls) == 5  # 4 bands + the whole image, as on the band path
    whole = [call for call in vl.calls if not _is_band(_image_ref_of(call))]
    assert [_question_of(call) for call in whole] == ["How many rows are marked pending?"]
    assert result.content.startswith("7 rows\n\n")
    # a counting question: the band's "3" is its own part of the image, not flagged
    assert (result.meta["auto"], result.meta["auto_reason"]) == ("whole_question", "agrees")
    assert result.meta["vl_calls"] == 5
    assert ask_image_region_auto_total.labels(**labels)._value.get() == before + 1


@pytest.mark.anyio
async def test_a_lookup_question_is_still_decided_by_the_bands(tmp_path: Path) -> None:
    vl = _VL(answer=lambda ref: _found("3") if _is_band(ref) else "whole")
    up = _upload(tmp_path, vl, size=(3000, 2000))

    result = await up.tool.call(
        {"image_ref": up.ref, "question": "What does the 'Total' row say?", "region": "auto"},
        ctx=up.ctx,
    )

    assert len(vl.calls) == 5
    assert result.meta["auto"] != "whole_question"


@pytest.mark.anyio
async def test_a_routed_non_counting_question_notes_a_labelled_band(tmp_path: Path) -> None:
    def answer(ref: str) -> str:
        if not _is_band(ref):
            return "The total is 95."
        return _found("120", label="Total") if ref.endswith(f"/{_FIRST}.jpg") else NOT_FOUND

    up = _upload(tmp_path, _VL(answer=answer), size=(3000, 2000))
    result = await up.tool.call(
        {"image_ref": up.ref, "question": "What is the total?", "region": "auto"}, ctx=up.ctx
    )
    assert (result.meta["auto"], result.meta["auto_reason"]) == ("whole_question", "bands_disagree")
    assert "read field 'Total' as 120" in result.content
