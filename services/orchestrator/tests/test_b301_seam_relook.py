"""B-301 PR 3 —— ``region="auto"`` 有横条报 ``partial`` 时,工具自己把接缝再快看一次(设计 §4.2)。

今天的输出(``_TODAY_*``)是改动之前在同一个场景里跑出来的原文,逐字节钉住:重看失败 / 超时 /
没时间 / 没有横条报 ``partial`` 时,输出必须与它完全相同。
"""

from __future__ import annotations

import asyncio
import dataclasses
import io
import json
import re
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest
from langchain_core.messages import AIMessage, BaseMessage
from PIL import Image, ImageChops, ImageStat

from expert_work.persistence import is_rendered_crop_rel, region_crop_stem
from orchestrator.tools import vision
from orchestrator.tools._guards import TokenBudget
from orchestrator.tools.image_orientation import ORIENTATION_QUESTION
from orchestrator.tools.read_page import RENDER_DPI
from orchestrator.tools.region_zoom import (
    REGION_TARGET_PIXELS,
    Tile,
    auto_bands,
    band_dpi,
    crop_output_size,
    pixel_box,
    plan_bands,
    region_dpi,
)
from orchestrator.tools.seam_relook import (
    SEAM_RELOOK_MIN_RUN_LEFT_S,
    ask_image_seam_relook_total,
    seam_agrees,
    seam_box,
)
from orchestrator.tools.tile_reading import TileReply, TileResult, tile_question
from orchestrator.tools.vision import AskImageTool

from .test_b266_region_zoom import (
    _A4_AT_100DPI,
    _BIG,
    _FOUR,
    _PDFTOPPM_PAGE_OR_REGION,
    _VL,
    NOT_FOUND,
    _doc,
    _found,
    _image_ref_of,
    _is_band,
    _question_of,
    _tools,
    _upload,
)
from .test_b301_ask_orientation import _big_stored, _default_orient, _sent, _setup, _upright
from .test_read_page import _install_office_stubs

QUESTION = "What is the reading?"
WHOLE = "The reading is 68.1."
PARTIAL = json.dumps({"status": "partial", "label": "", "value": "", "unit": ""})
#: 横条 2、3 都报 ``partial``:并集框就是今天建议里那个框。
SEAM = (0, 241, 1000, 759)
#: 接缝按横条分辨率裁,文件名带 ``-b``(与同一个框的一般局部分开)。
SEAM_STEM = region_crop_stem(SEAM, band=True)

#: 改动之前跑出来的原文(横条 2、3 报 partial,其余 not_found,整张读 68.1)。
_TODAY_PARTIAL = (
    "The reading is 68.1.\n\n(region='auto': whole-image reading; small print may be unreliable "
    "- a band saw the value cut off at its edge, so no band value was taken. Re-ask with an "
    "explicit region from the band readings below, or say the value is uncertain.)\nBand "
    "readings: no band could see the complete answer (2 replied not_found). The text may be too "
    "small to read, or it is not in this image. band 2, band 3 saw it cut off at their edge, so "
    "it probably sits on a seam between bands: look again with region=[0, 241, 1000, 759]."
)
#: 改动之前跑出来的原文(只有横条 2 读到 7,没有 partial)。
_TODAY_SINGLE = "7\n\n(region='auto', read in bands: read in band 2; 3 band(s) replied not_found.)"
_USAGE = {"input_tokens": 10, "output_tokens": 1, "total_tokens": 11}
_ASK_AGAIN = "Re-ask with an explicit region"
_LOOK_AGAIN = "look again with region="
_UNCERTAIN = "small print may be unreliable"


def _is_seam(ref: str) -> bool:
    return ref.endswith(f"/{SEAM_STEM}.jpg")


def _partial_scene(
    seam: Callable[[str], str], *, whole: str = WHOLE, partial: Sequence[int] = (1, 2)
) -> Callable[[str], str]:
    """``_FOUR`` 里第 ``partial`` 条(从 0 数)报 partial、其余 not_found、整张读 ``whole``;
    接缝那一块由 ``seam`` 回答。"""
    partial_stems = {region_crop_stem(_FOUR[i].box) for i in partial}

    def answer(ref: str) -> str:
        if not _is_band(ref):
            return whole
        if _is_seam(ref):
            return seam(ref)
        stem = ref.rsplit("/", 1)[1].removesuffix(".jpg")
        return PARTIAL if stem in partial_stems else NOT_FOUND

    return answer


def _seam_says(reply: str) -> Callable[[str], str]:
    return lambda _ref: reply


def _seam_raises(_ref: str) -> str:
    raise RuntimeError("vendor 500")


async def _auto(vl: _VL, tmp_path: Path, **ask: Any) -> Any:
    up = _upload(tmp_path, vl, size=_BIG, **ask)
    return await up.tool.call(
        {"image_ref": up.ref, "question": QUESTION, "region": "auto"}, ctx=up.ctx
    )


def _relook_count(outcome: str) -> float:
    return ask_image_seam_relook_total.labels(outcome=outcome)._value.get()


# ---------------------------------------------------------------------------
# 接缝框
# ---------------------------------------------------------------------------


def _result(tile: Tile, status: str) -> TileResult:
    return TileResult(tile=tile, reply=TileReply(status=status, raw="", parse_mode="json"))  # type: ignore[arg-type]


def test_the_seam_is_the_union_of_the_partial_bands() -> None:
    results = [_result(t, "partial" if i in (1, 2) else "not_found") for i, t in enumerate(_FOUR)]
    assert seam_box(results, _FOUR) == SEAM


def test_one_partial_band_is_widened_by_half_a_band_each_way() -> None:
    """只有一条报 partial 时并集就是它自己:原样再裁一次看到的还是那几个被切开的像素。"""
    results = [_result(t, "partial" if i == 1 else "not_found") for i, t in enumerate(_FOUR)]
    x1, y1, x2, y2 = _FOUR[1].box
    half = (y2 - y1) // 2
    assert seam_box(results, _FOUR) == (x1, y1 - half, x2, y2 + half)


def test_a_widened_edge_band_stops_at_the_image_edge() -> None:
    results = [_result(t, "partial" if i == 0 else "not_found") for i, t in enumerate(_FOUR)]
    assert seam_box(results, _FOUR) == (0, 0, 1000, 275 + 275 // 2)


def test_vertical_strips_widen_along_x() -> None:
    strips = auto_bands(3, "x")
    results = [_result(t, "partial" if i == 1 else "not_found") for i, t in enumerate(strips)]
    x1, y1, x2, y2 = strips[1].box
    half = (x2 - x1) // 2
    assert seam_box(results, strips) == (x1 - half, y1, x2 + half, y2)


def test_no_partial_band_means_no_seam() -> None:
    results = [_result(t, "not_found") for t in _FOUR]
    assert seam_box(results, _FOUR) is None


@pytest.mark.parametrize("partial", [(0, 3), (0, 2), (0, 1, 3)], ids=str)
def test_partial_bands_that_are_not_neighbours_are_not_one_seam(partial: tuple[int, ...]) -> None:
    results = [_result(t, "partial" if i in partial else "not_found") for i, t in enumerate(_FOUR)]
    assert seam_box(results, _FOUR) is None


def test_three_neighbouring_partial_bands_are_one_seam() -> None:
    results = [_result(t, "partial" if i in (1, 2, 3) else "found") for i, t in enumerate(_FOUR)]
    assert seam_box(results, _FOUR) == (0, 241, 1000, 1000)


# ---------------------------------------------------------------------------
# 两次读数一致吗(宁严勿松)
# ---------------------------------------------------------------------------

#: 第 1 轮审查复现的「误判一致」:都必须判不一致。原有的第 6 个(``68.1 lb`` 对 ``68.1``)在第 2 轮
#: 改为一致 —— 只有一边写了单位藏不住一个不同的数,而且一致的那句话把 ``68.1 lb`` 原样写了出来
#: (见 :func:`test_a_unit_on_one_side_only_agrees_and_shows_the_reading`)。
_REVIEW_CASES = [
    ("Blood pressure 120/80 mmHg.", "120/85"),
    ("12:30", "12:35"),
    ("The value is 5", "<5"),
    ("Glucose 5.6 mmol/L (reference 3.9-6.1)", "6.1"),
    ("Name: Zhang San", "Zhang"),
]


@pytest.mark.parametrize(("whole", "seen"), _REVIEW_CASES, ids=[c[1] for c in _REVIEW_CASES])
def test_a_near_match_is_not_agreement(whole: str, seen: str) -> None:
    assert seam_agrees(whole, seen) is False


@pytest.mark.parametrize(
    ("whole", "seen"),
    [
        ("The value is 5", "-5"),
        ("Order 915", "0915"),
        ("It is 68.1 kg.", "68.1 lb"),
        ("Code HT-2026-0915-07", "HT-2026-0915-08"),
        ("Shelf B12", "A12"),  # 数一样、字母不一样
        ("Value: 7, then 68.1", "68.1"),
        ("", "68.1"),
    ],
    ids=str,
)
def test_more_near_matches_are_not_agreement(whole: str, seen: str) -> None:
    assert seam_agrees(whole, seen) is False


#: 第 2 轮审查:只对上了一个复合值的一段、或对上的是标签 —— 都必须判不一致。
_PART_OF_A_VALUE = [
    ("Blood pressure 120/80 mmHg.", "120"),
    ("The time is 12:30.", "12"),
    ("Date: 2026-11-12", "2026"),
    ("Date: 2026-11-12", "2026-11"),
    ("日期 2026年11月12日", "2026"),
    ("Reference 3.9-6.1", "3.9"),
    ("Version 1.2.3", "1.2"),
    ("Step 1: the reading is 68.1", "1"),
    ("There are 2 values: 5.6 and 7.1", "2"),
    ("There are 2 different values: 5.6 and 7.1", "2"),
    ("HBsAg: negative", "HBsAg"),
    ("HBsAg\uff1a negative", "HBsAg"),
]


@pytest.mark.parametrize(
    ("whole", "seen"), _PART_OF_A_VALUE, ids=[f"{w}|{s}" for w, s in _PART_OF_A_VALUE]
)
def test_part_of_a_value_or_a_label_is_not_agreement(whole: str, seen: str) -> None:
    assert seam_agrees(whole, seen) is False


#: 第 2 轮审查:重看读到 ``68.1 kg`` 时,这些平常的写法都该一致(单位只在两边都有且不同时才冲突;
#: Markdown 的 ``*`` ``_`` 反引号不算单位的一部分)。
_ORDINARY_PHRASINGS = [
    "68.1 kg",
    "68.1kg",
    "68.1 KG",
    "68.1",
    "The reading is 68.1.",
    "The reading is 68.1 kg.",
    "The scale displays **68.1 kg**.",
    "The scale displays **68.1** kg.",
    "**Weight:** 68.1 kg",
    "Weight: 68.1 kg",
    "It reads `68.1 kg`.",
    "_68.1 kg_",
    "The display shows 68.1 (kg).",
    "The value is 68.1 kg, within the normal range.",
    "68.1 kg is shown on the screen.",
    "体重 68.1 kg。",
]


@pytest.mark.parametrize("whole", _ORDINARY_PHRASINGS, ids=str)
def test_ordinary_phrasings_of_the_same_reading_agree(whole: str) -> None:
    assert seam_agrees(whole, "68.1 kg") is True


def test_two_different_units_still_disagree() -> None:
    assert seam_agrees("68.1 kg", "68.1 lb") is False
    assert seam_agrees("The scale displays **68.1 kg**.", "68.1 lb") is False


@pytest.mark.parametrize(
    ("whole", "seen"),
    [
        ("The reading is 68.1.", "68.1"),
        ("The reading is 68.1 kg.", "68.1"),
        ("Weight: 68.1kg", "68.1 kg"),
        ("Blood pressure 120/80 mmHg.", "120/80 mmHg"),
        ("Glucose 5.6 mmol/L (reference 3.9-6.1)", "5.6 mmol/L"),
        ("It shows <5 here", "<5"),
        ("The value is 36.80", "36.8"),
        ("Name: Zhang San", "Zhang San"),
        ('The label reads "Zhang San".', "zhang san"),
        ("Code HT-2026-0915-07 printed", "HT-2026-0915-07"),
        ("Date: 2026-11-12", "2026-11-12"),
        ("The time is 12:30.", "12:30"),
        ("HBsAg: negative", "negative"),
        ("68.1", "68.1 lb"),  # 只有一边写了单位:不冲突(值原样写进一致的那句话)
    ],
    ids=str,
)
def test_a_reading_that_is_there_agrees(whole: str, seen: str) -> None:
    assert seam_agrees(whole, seen) is True


# ---------------------------------------------------------------------------
# 工具端到端
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_a_partial_band_makes_exactly_one_extra_look_at_the_seam(tmp_path: Path) -> None:
    vl = _VL(answer=_partial_scene(_seam_says(_found("68.1"))))

    result = await _auto(vl, tmp_path)

    assert len(vl.calls) == 4 + 1 + 1  # 4 条横条 + 整张 + 接缝
    seam_calls = [c for c in vl.calls if _is_seam(_image_ref_of(c))]
    assert len(seam_calls) == 1
    assert _question_of(seam_calls[0]) == tile_question(QUESTION, "y")
    assert result.meta["vl_calls"] == 6
    assert result.meta["seam_relook"]["region"] == list(SEAM)


@pytest.mark.anyio
async def test_the_same_value_keeps_the_uncertainty_and_drops_the_ask_again(
    tmp_path: Path,
) -> None:
    before = _relook_count("agree")
    vl = _VL(answer=_partial_scene(_seam_says(_found("68.1"))), usage=_USAGE)
    metered: list[AIMessage] = []

    async def meter(response: AIMessage, *, tenant_id: UUID, user_id: UUID | None) -> None:
        metered.append(response)

    up = _upload(tmp_path, vl, size=_BIG, usage_meter=meter)
    budget = TokenBudget(limit=1_000_000)
    ctx = dataclasses.replace(up.ctx, token_budget=budget)

    result = await up.tool.call(
        {"image_ref": up.ref, "question": QUESTION, "region": "auto"}, ctx=ctx
    )

    content = result.content
    assert content.startswith(WHOLE + "\n\n")  # 值不变:仍是整张读
    assert _UNCERTAIN in content
    assert "say the value is uncertain" in content
    assert _ASK_AGAIN not in content
    assert _LOOK_AGAIN not in content
    assert (
        'looked at again (region=[0, 241, 1000, 759]) and read "68.1", which also appears in '
        "the whole-image reading above" in content
    )
    assert "same value" not in content
    assert re.search(r"(?<!un)reliable|confirm", content, re.IGNORECASE) is None
    assert result.meta["seam_relook"]["outcome"] == "agree"
    assert result.meta["seam_relook"]["value"] == "68.1"
    assert _relook_count("agree") == before + 1
    # 接缝那一次与横条一样计用量、扣池、记账:6 次调用(4 条 + 整张 + 接缝)。
    assert result.meta["vl_usage"] == {k: 6 * v for k, v in _USAGE.items()}
    assert len(metered) == 6
    assert budget.spent == 6 * _USAGE["total_tokens"]


@pytest.mark.anyio
async def test_a_different_value_lists_both_and_picks_neither(tmp_path: Path) -> None:
    before = _relook_count("disagree")
    vl = _VL(answer=_partial_scene(_seam_says(_found("0.0"))))

    result = await _auto(vl, tmp_path)

    content = result.content
    assert content.startswith(WHOLE + "\n\n")  # 整张读原样列在上面
    assert 'disagree: the whole-image reading above / "0.0"' in content
    assert "Neither is preferred; say the value is uncertain." in content
    assert "gave the same value" not in content
    assert _ASK_AGAIN not in content
    assert result.meta["seam_relook"]["outcome"] == "disagree"
    assert _relook_count("disagree") == before + 1


@pytest.mark.anyio
@pytest.mark.parametrize("reply", [NOT_FOUND, PARTIAL, "I cannot tell."], ids=str)
async def test_a_seam_look_that_reads_nothing_says_the_value_is_uncertain(
    tmp_path: Path, reply: str
) -> None:
    before = _relook_count("no_reading")
    vl = _VL(answer=_partial_scene(_seam_says(reply)))

    result = await _auto(vl, tmp_path)

    content = result.content
    assert content.startswith(WHOLE + "\n\n")
    assert "and nothing was read there" in content
    assert "say the value is uncertain" in content
    assert "gave the same value" not in content
    assert _ASK_AGAIN not in content
    assert result.meta["seam_relook"]["outcome"] == "no_reading"
    assert _relook_count("no_reading") == before + 1


@pytest.mark.anyio
async def test_a_failed_seam_look_returns_todays_output_byte_for_byte(tmp_path: Path) -> None:
    before = _relook_count("error")
    vl = _VL(answer=_partial_scene(_seam_raises))

    result = await _auto(vl, tmp_path)

    assert result.content == _TODAY_PARTIAL
    assert result.meta["seam_relook"] == {
        "outcome": "error",
        "region": list(SEAM),
        "error": "RuntimeError",
    }
    assert result.meta["vl_calls"] == 6  # 试过了
    assert _relook_count("error") == before + 1


@dataclass
class _SlowSeam(_VL):
    async def __call__(self, *, messages: Sequence[BaseMessage], tools: Sequence[Any]) -> AIMessage:
        if _is_seam(_image_ref_of(messages)):
            await asyncio.sleep(5)
        return await super().__call__(messages=messages, tools=tools)


@pytest.mark.anyio
async def test_a_timed_out_seam_look_returns_todays_output_byte_for_byte(tmp_path: Path) -> None:
    vl = _SlowSeam(answer=_partial_scene(_seam_says(_found("68.1"))))

    started = time.monotonic()
    result = await _auto(vl, tmp_path, seam_relook_timeout_s=0.2)

    assert time.monotonic() - started < 4
    assert result.content == _TODAY_PARTIAL
    assert result.meta["seam_relook"]["outcome"] == "error"
    assert result.meta["seam_relook"]["error"] == "timed out"


@pytest.mark.anyio
async def test_no_partial_band_makes_no_extra_look_and_changes_nothing(tmp_path: Path) -> None:
    def answer(ref: str) -> str:
        if not _is_band(ref):
            return "whole: 7"
        return _found("7") if ref.endswith(f"/{region_crop_stem(_FOUR[1].box)}.jpg") else NOT_FOUND

    vl = _VL(answer=answer)

    result = await _auto(vl, tmp_path)

    assert len(vl.calls) == 5
    assert result.content == _TODAY_SINGLE
    assert "seam_relook" not in result.meta
    assert result.meta["vl_calls"] == 5


@pytest.mark.anyio
async def test_a_band_conflict_without_partial_is_not_looked_at_again(tmp_path: Path) -> None:
    first, third = region_crop_stem(_FOUR[0].box), region_crop_stem(_FOUR[2].box)

    def answer(ref: str) -> str:
        if not _is_band(ref):
            return "The total is 42."
        if ref.endswith(f"/{first}.jpg"):
            return _found("40", label="Total")
        return _found("42", label="Total") if ref.endswith(f"/{third}.jpg") else NOT_FOUND

    vl = _VL(answer=answer)

    result = await _auto(vl, tmp_path)

    assert len(vl.calls) == 5
    assert result.meta["auto_reason"] == "conflict"
    assert "seam_relook" not in result.meta


@pytest.mark.anyio
async def test_a_whole_image_question_is_not_looked_at_again(tmp_path: Path) -> None:
    """B-273 整体类问题以整张读为准,横条报 partial 是常态:不重看。"""
    vl = _VL(answer=_partial_scene(_seam_says(_found("68.1"))))
    up = _upload(tmp_path, vl, size=_BIG)

    result = await up.tool.call(
        {"image_ref": up.ref, "question": "How many rows are there?", "region": "auto"},
        ctx=up.ctx,
    )

    assert result.meta["auto"] == "whole_question"
    assert len(vl.calls) == 5
    assert "seam_relook" not in result.meta


@pytest.mark.anyio
async def test_no_whole_image_reading_means_nothing_to_compare(tmp_path: Path) -> None:
    scene = _partial_scene(_seam_says(_found("68.1")))

    def answer(ref: str) -> str:
        if not _is_band(ref):
            raise RuntimeError("vendor 500")
        return scene(ref)

    vl = _VL(answer=answer)

    result = await _auto(vl, tmp_path)

    assert result.meta["auto_reason"] == "partial+full_failed"
    assert len(vl.calls) == 5
    assert "seam_relook" not in result.meta


@pytest.mark.anyio
async def test_too_little_run_time_left_skips_the_seam_look(tmp_path: Path) -> None:
    before = _relook_count("skipped_deadline")
    vl = _VL(answer=_partial_scene(_seam_says(_found("68.1"))))
    up = _upload(tmp_path, vl, size=_BIG)
    ctx = dataclasses.replace(
        up.ctx, deadline_at=time.monotonic() + SEAM_RELOOK_MIN_RUN_LEFT_S - 15
    )

    result = await up.tool.call(
        {"image_ref": up.ref, "question": QUESTION, "region": "auto"}, ctx=ctx
    )

    assert len(vl.calls) == 5
    assert result.content == _TODAY_PARTIAL
    assert result.meta["seam_relook"] == {"outcome": "skipped_deadline", "region": list(SEAM)}
    assert result.meta["vl_calls"] == 5
    assert _relook_count("skipped_deadline") == before + 1


@pytest.mark.anyio
async def test_enough_run_time_left_still_looks(tmp_path: Path) -> None:
    vl = _VL(answer=_partial_scene(_seam_says(_found("68.1"))))
    up = _upload(tmp_path, vl, size=_BIG)
    ctx = dataclasses.replace(
        up.ctx, deadline_at=time.monotonic() + SEAM_RELOOK_MIN_RUN_LEFT_S + 30
    )

    result = await up.tool.call(
        {"image_ref": up.ref, "question": QUESTION, "region": "auto"}, ctx=ctx
    )

    assert result.meta["seam_relook"]["outcome"] == "agree"


@pytest.mark.anyio
async def test_the_seam_look_takes_a_tile_slot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """进程内同时在飞的切块调用上限也管接缝那一次。"""
    taken: list[int] = []
    real = vision._tile_slots

    def counting() -> asyncio.Semaphore:
        taken.append(1)
        return real()

    monkeypatch.setattr(vision, "_tile_slots", counting)
    vl = _VL(answer=_partial_scene(_seam_says(_found("68.1"))))

    await _auto(vl, tmp_path)

    assert len(taken) == 4 + 1 + 1


# ---------------------------------------------------------------------------
# 与 PR 1 的方向:接缝从转正后的画面上裁
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_on_a_rotated_image_the_seam_is_cut_from_the_upright_frame(
    tmp_path: Path,
) -> None:
    """存下来的图要顺时针转 90 度才正;横条按转正后的 3000x1500 规划(3 条),条 1、2 报 partial,
    接缝框 [0, 0, 1000, 682] 必须在转正后的画面上裁(``r90/``),像素与「先转正再裁」一致。"""
    size = (3000, 1500)
    up = _setup(tmp_path, _big_stored(90, size))
    seam = (0, 0, 1000, 682)
    seam_stem = region_crop_stem(seam, band=True)

    async def answer(*, messages: Sequence[BaseMessage], tools: Sequence[Any]) -> AIMessage:
        question, ref = _sent(messages)
        up.vision.calls.append((question, ref))
        if question == ORIENTATION_QUESTION:
            image = Image.open(io.BytesIO((await up.resolver.resolve(ref)).data)).convert("RGB")
            return AIMessage(content=await _default_orient(ref, image))
        if "/crops/" in ref and ref.endswith(f"/{seam_stem}.jpg"):
            return AIMessage(content=_found("69.1"))
        if "/crops/" in ref and not ref.endswith("/0-0-1000-1000.jpg"):
            stem = ref.rsplit("/", 1)[1]
            return AIMessage(content=PARTIAL if stem.split("-")[1] != "637" else NOT_FOUND)
        return AIMessage(content="It reads 69.1.")

    tool = dataclasses.replace(up.tool, vl_caller=answer)
    result = await tool.call(
        {"image_ref": up.ref, "question": QUESTION, "region": "auto"}, ctx=up.ctx
    )

    assert result.meta["orientation"]["degrees"] == 90
    assert result.meta["seam_relook"]["region"] == list(seam)
    assert result.meta["seam_relook"]["outcome"] == "agree"
    (seam_ref,) = [ref for _q, ref in up.vision.calls if ref.endswith(f"/{seam_stem}.jpg")]
    assert f"/r90/{seam_stem}.jpg" in seam_ref
    got = up.image_of(seam_ref)
    upright = _upright().resize(size, Image.Resampling.NEAREST)
    want = upright.crop(pixel_box(seam, size)).resize(got.size, Image.Resampling.NEAREST)
    diff = ImageStat.Stat(ImageChops.difference(got, want)).mean
    assert max(diff) < 8, diff


# ---------------------------------------------------------------------------
# 审查回修(11-12)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.parametrize(("whole", "seen"), _REVIEW_CASES, ids=[c[1] for c in _REVIEW_CASES])
async def test_a_near_match_through_the_tool_lists_both(
    tmp_path: Path, whole: str, seen: str
) -> None:
    """审查复现的 6 个:经过工具也判不一致,两个读数都列出,不挑。"""
    value, _, unit = seen.partition(" ")
    vl = _VL(answer=_partial_scene(_seam_says(_found(value, unit=unit)), whole=whole))

    result = await _auto(vl, tmp_path)

    assert result.meta["seam_relook"]["outcome"] == "disagree"
    assert result.content.startswith(whole + "\n\n")
    assert f'disagree: the whole-image reading above / "{seen}"' in result.content
    assert "say the value is uncertain" in result.content


@pytest.mark.anyio
async def test_partial_bands_far_apart_are_not_looked_at_again(tmp_path: Path) -> None:
    """条 1 与条 4 报 partial:并集差不多是整张图,不是一条接缝 —— 不看,输出与今天逐字节相同。"""
    vl = _VL(answer=_partial_scene(_seam_says(_found("68.1")), partial=(0, 3)))

    result = await _auto(vl, tmp_path)

    today = _TODAY_PARTIAL.replace("band 2, band 3", "band 1, band 4").replace(
        "[0, 241, 1000, 759]", "[0, 0, 1000, 1000]"
    )
    assert result.content == today
    assert len(vl.calls) == 5
    assert "seam_relook" not in result.meta


@pytest.mark.anyio
async def test_run_time_running_out_during_the_crop_skips_the_look(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """裁接缝时 run 剩的时间掉到线下(这里让 vision 的时钟在裁完之后跳 40 秒):裁完那一次检查
    挡住它,不发调用,输出与今天相同。"""
    real_crops = AskImageTool._crops
    real_time = vision.time

    async def crops_then_jump(self: AskImageTool, located: Any, boxes: Any, **kw: Any) -> Any:
        render = await real_crops(self, located, boxes, **kw)
        if list(boxes) == [SEAM]:
            monkeypatch.setattr(
                vision, "time", SimpleNamespace(monotonic=lambda: real_time.monotonic() + 40)
            )
        return render

    monkeypatch.setattr(AskImageTool, "_crops", crops_then_jump)
    vl = _VL(answer=_partial_scene(_seam_says(_found("68.1"))))
    up = _upload(tmp_path, vl, size=_BIG)
    ctx = dataclasses.replace(
        up.ctx, deadline_at=time.monotonic() + SEAM_RELOOK_MIN_RUN_LEFT_S + 20
    )

    result = await up.tool.call(
        {"image_ref": up.ref, "question": QUESTION, "region": "auto"}, ctx=ctx
    )

    assert result.meta["seam_relook"] == {"outcome": "skipped_deadline", "region": list(SEAM)}
    assert len(vl.calls) == 5
    assert result.content == _TODAY_PARTIAL


@pytest.mark.anyio
async def test_vertical_strips_look_at_the_seam_with_the_strip_prompt(tmp_path: Path) -> None:
    """宽:高超过 4:1 切竖条;接缝那一次用竖条的提示(full-height vertical strip)。"""
    size = (5000, 1000)
    strips = plan_bands(size, size[0] * size[1])
    assert strips == auto_bands(len(strips), "x")
    seam = (strips[1].box[0], 0, strips[2].box[2], 1000)
    partial_stems = {region_crop_stem(strips[1].box), region_crop_stem(strips[2].box)}

    def answer(ref: str) -> str:
        if not _is_band(ref):
            return WHOLE
        stem = ref.rsplit("/", 1)[1].removesuffix(".jpg")
        if stem == region_crop_stem(seam, band=True):
            return _found("68.1")
        return PARTIAL if stem in partial_stems else NOT_FOUND

    vl = _VL(answer=answer)
    up = _upload(tmp_path, vl, size=size)

    result = await up.tool.call(
        {"image_ref": up.ref, "question": QUESTION, "region": "auto"}, ctx=up.ctx
    )

    assert result.meta["seam_relook"]["region"] == list(seam)
    (seam_call,) = [
        c
        for c in vl.calls
        if _image_ref_of(c).endswith(f"/{region_crop_stem(seam, band=True)}.jpg")
    ]
    assert _question_of(seam_call) == tile_question(QUESTION, "x")
    assert "one full-height vertical strip" in _question_of(seam_call)


@pytest.mark.anyio
async def test_the_seam_crop_is_as_sharp_as_the_bands(tmp_path: Path) -> None:
    """接缝框是两条横条的并集(约 310 万像素):按横条的上限(300 万)裁,不按一般局部的
    150 万 —— 否则比横条还糊。横条本身仍是 150 万。"""
    vl = _VL(answer=_partial_scene(_seam_says(_found("68.1"))))
    up = _upload(tmp_path, vl, size=_BIG)

    await up.tool.call({"image_ref": up.ref, "question": QUESTION, "region": "auto"}, ctx=up.ctx)

    def pixels(ref: str) -> int:
        on_disk = up.root / str(up.ctx.tenant_id) / str(up.ctx.user_id) / ref.split("/", 5)[5]
        width, height = Image.open(on_disk).size
        return width * height

    crops = [_image_ref_of(c) for c in vl.calls if _is_band(_image_ref_of(c))]
    seam_ref = next(ref for ref in crops if _is_seam(ref))
    assert pixels(seam_ref) > 1.9 * REGION_TARGET_PIXELS
    assert all(pixels(ref) <= REGION_TARGET_PIXELS for ref in crops if ref != seam_ref)


@pytest.mark.anyio
async def test_a_document_page_seam_renders_at_the_band_resolution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """文档页:接缝按横条的分辨率(``band_dpi``)渲,不按一般局部的(``region_dpi``,更低)。"""
    _install_office_stubs(tmp_path, monkeypatch, pdftoppm_body=_PDFTOPPM_PAGE_OR_REGION)
    doc = _doc(tmp_path)
    tiles = auto_bands(6)
    seam = (0, tiles[1].box[1], 1000, tiles[2].box[3])
    want_dpi = band_dpi(_A4_AT_100DPI, seam, base_dpi=RENDER_DPI)
    assert want_dpi > region_dpi(_A4_AT_100DPI, seam, base_dpi=RENDER_DPI)
    partial = {region_crop_stem(tiles[1].box), region_crop_stem(tiles[2].box)}

    def answer(ref: str) -> str:
        if not _is_band(ref):
            return WHOLE
        if f"-{region_crop_stem(seam)}-d" in ref:
            return _found("68.1")
        return PARTIAL if any(f"-{stem}-d" in ref for stem in partial) else NOT_FOUND

    vl = _VL(answer=answer)
    tool, _read_page, _runtime = _tools(doc, vl)

    result = await tool.call(
        {"path": doc.path, "unit": 1, "question": QUESTION, "region": "auto"}, ctx=doc.ctx
    )

    assert result.meta["seam_relook"]["outcome"] == "agree"
    (seam_ref,) = [
        _image_ref_of(c) for c in vl.calls if f"-{region_crop_stem(seam)}-d" in _image_ref_of(c)
    ]
    assert seam_ref.endswith(f"-{region_crop_stem(seam)}-d{want_dpi}.jpg")


@pytest.mark.anyio
async def test_a_metering_failure_on_the_seam_look_fails_the_tool_like_a_band(
    tmp_path: Path,
) -> None:
    """接缝那一次之后记账出错:与横条一样让工具失败,不悄悄当成「没看成」。"""
    metered: list[AIMessage] = []

    async def meter(response: AIMessage, *, tenant_id: UUID, user_id: UUID | None) -> None:
        metered.append(response)
        if len(metered) == 6:  # 4 条 + 整张都记过了,第 6 次是接缝
            raise RuntimeError("meter down")

    vl = _VL(answer=_partial_scene(_seam_says(_found("68.1"))))
    up = _upload(tmp_path, vl, size=_BIG, usage_meter=meter)

    with pytest.raises(RuntimeError, match="meter down"):
        await up.tool.call(
            {"image_ref": up.ref, "question": QUESTION, "region": "auto"}, ctx=up.ctx
        )


# ---------------------------------------------------------------------------
# 第 2 轮审查回修(11-12)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("whole", "seen"), _PART_OF_A_VALUE[:4], ids=[f"{w}|{s}" for w, s in _PART_OF_A_VALUE[:4]]
)
async def test_part_of_a_value_through_the_tool_lists_both(
    tmp_path: Path, whole: str, seen: str
) -> None:
    vl = _VL(answer=_partial_scene(_seam_says(_found(seen)), whole=whole))

    result = await _auto(vl, tmp_path)

    assert result.meta["seam_relook"]["outcome"] == "disagree"
    assert f'disagree: the whole-image reading above / "{seen}"' in result.content


@pytest.mark.anyio
async def test_a_unit_on_one_side_only_agrees_and_shows_the_reading(tmp_path: Path) -> None:
    """整张读「68.1」、重看读到「68.1 lb」:不冲突,一致的那句话把「68.1 lb」原样写出来。"""
    vl = _VL(answer=_partial_scene(_seam_says(_found("68.1", unit="lb")), whole="68.1"))

    result = await _auto(vl, tmp_path)

    assert result.meta["seam_relook"]["outcome"] == "agree"
    assert 'and read "68.1 lb", which also appears in the whole-image reading' in result.content


@pytest.mark.parametrize("tail", [f"{SEAM_STEM}.jpg", f"r90/{SEAM_STEM}.jpg"])
def test_band_resolution_crops_are_platform_renders(tail: str) -> None:
    run, sha = "99999999-9999-4999-8999-999999999999", "a" * 16
    assert is_rendered_crop_rel(f".tool_results/{run}/crops/{sha}/{tail}")
    assert not is_rendered_crop_rel(f".tool_results/{run}/crops/{sha}/0-241-1000-759-c.jpg")
    assert not is_rendered_crop_rel(f".tool_results/{run}/crops/{sha}/0-241-1000-759-bb.jpg")


def _on_disk(up: Any, ref: str) -> tuple[int, int]:
    path = up.root / str(up.ctx.tenant_id) / str(up.ctx.user_id) / ref.split("/", 5)[5]
    return Image.open(path).size


#: 一般局部(约 150 万像素)裁这个框的尺寸。
_REGION_SIZE = crop_output_size(SEAM, _BIG)


@pytest.mark.anyio
@pytest.mark.parametrize(
    "relook_first", [True, False], ids=["relook-then-region", "region-then-relook"]
)
async def test_the_seam_crop_and_an_explicit_region_on_the_same_box_are_two_files(
    tmp_path: Path, relook_first: bool
) -> None:
    """接缝(横条分辨率)与同一个框的显式 ``region``(一般分辨率)各写各的文件,各按各的尺寸;
    meta 里报的是这一次真正交给模型的那张。两种先后顺序都一样。"""
    scene = _partial_scene(_seam_says(_found("68.1")))
    vl = _VL(
        answer=lambda ref: (
            "explicit" if ref.endswith(f"/{region_crop_stem(SEAM)}.jpg") else scene(ref)
        )
    )
    up = _upload(tmp_path, vl, size=_BIG)
    auto = {"image_ref": up.ref, "question": QUESTION, "region": "auto"}
    explicit = {"image_ref": up.ref, "question": QUESTION, "region": list(SEAM)}

    if relook_first:
        await up.tool.call(auto, ctx=up.ctx)
        region = await up.tool.call(explicit, ctx=up.ctx)
    else:
        region = await up.tool.call(explicit, ctx=up.ctx)
        await up.tool.call(auto, ctx=up.ctx)
    again = await up.tool.call(explicit, ctx=up.ctx)

    refs = [_image_ref_of(c) for c in vl.calls]
    (seam_ref,) = [ref for ref in refs if _is_seam(ref)]
    region_refs = [ref for ref in refs if ref.endswith(f"/{region_crop_stem(SEAM)}.jpg")]
    assert len(region_refs) == 2 and seam_ref not in region_refs
    assert _on_disk(up, region_refs[0]) == _REGION_SIZE
    width, height = _on_disk(up, seam_ref)
    assert width * height > 1.9 * REGION_TARGET_PIXELS
    assert region.content == "explicit"
    assert region.meta["crop"] == {"dpi": None, "pixels": list(_REGION_SIZE), "reused": False}
    assert again.meta["crop"] == {"dpi": None, "pixels": list(_REGION_SIZE), "reused": True}
