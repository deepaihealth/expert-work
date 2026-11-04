"""B-273 —— whole-image questions skip the bands (V1); the band rules that stay as fallbacks:
R2 (several bands with the same value count only as one row seen twice where two neighbouring
bands overlap) and V2 (on a counting question a band's 0 means "not found")."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from orchestrator.tools.region_zoom import auto_bands
from orchestrator.tools.tile_reading import TileResult, decide_auto, parse_tile_reply
from orchestrator.tools.vision import ask_image_region_auto_total

from .test_b266_region_zoom import _VL, NOT_FOUND, _found, _is_band, _question_of, _upload

_TILES = auto_bands(6)
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
# V2 —— on a counting question a band's 0 is "not found"
# ---------------------------------------------------------------------------


def test_a_zero_from_one_band_does_not_answer_a_counting_question() -> None:
    results = _results(_json("found", "0"), _NF, _NF, _NF, _NF, _NF)
    counted = decide_auto(results, full_answer=_FULL, full_error=None, count_question=True)
    assert (counted.path, counted.reason) == ("full_fallback", "none")
    plain = decide_auto(results, full_answer=_FULL, full_error=None)
    assert (plain.path, plain.reason) == ("bands", "single")


def test_a_non_zero_count_from_one_band_is_kept() -> None:
    results = _results(_NF, _json("found", "4"), _NF, _NF, _NF, _NF)
    decision = decide_auto(results, full_answer=_FULL, full_error=None, count_question=True)
    assert (decision.path, decision.reason) == ("bands", "single")


# ---------------------------------------------------------------------------
# V1 —— the question decides before any band is cut
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_a_whole_image_question_is_read_whole_without_bands(tmp_path: Path) -> None:
    vl = _VL(answer=lambda ref: _found("3") if _is_band(ref) else "7 rows")
    up = _upload(tmp_path, vl, size=(3000, 2000))  # 6 MP: would be cut into bands
    before = ask_image_region_auto_total.labels(path="gated", reason="whole_question")._value.get()

    result = await up.tool.call(
        {"image_ref": up.ref, "question": "How many rows are marked pending?", "region": "auto"},
        ctx=up.ctx,
    )

    assert len(vl.calls) == 1
    assert not _is_band(str(vl.calls[0][1].content[1]["ref"]))  # type: ignore[index]
    assert _question_of(vl.calls[0]) == "How many rows are marked pending?"
    assert result.content.startswith("7 rows\n\n(region='auto': the question is about the whole")
    assert (result.meta["auto"], result.meta["auto_reason"]) == ("gated", "whole_question")
    assert result.meta["vl_calls"] == 1
    after = ask_image_region_auto_total.labels(path="gated", reason="whole_question")._value.get()
    assert after == before + 1


@pytest.mark.anyio
async def test_a_lookup_question_is_still_read_in_bands(tmp_path: Path) -> None:
    vl = _VL(answer=lambda ref: NOT_FOUND if _is_band(ref) else "whole")
    up = _upload(tmp_path, vl, size=(3000, 2000))

    result = await up.tool.call(
        {"image_ref": up.ref, "question": "What does the 'Total' row say?", "region": "auto"},
        ctx=up.ctx,
    )

    assert len(vl.calls) == 5  # 4 bands + the whole image in parallel
    assert result.meta["auto"] != "gated"
