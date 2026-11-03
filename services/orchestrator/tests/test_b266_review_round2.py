"""B-266 review round 2:解析有界且线性、横条 dpi 有上限且写进文件名、超大指数 / 上标单位、
单字「无」只在散文里算没看到、横条准备失败与被截断时的退路、极端宽高比改切竖条。"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from orchestrator.multimodal import parse_rendered_figure_ref
from orchestrator.tools.read_page import RENDER_DPI, _region_job
from orchestrator.tools.region_zoom import (
    AUTO_MIN_BANDS,
    MAX_BAND_ASPECT,
    MAX_BAND_PIXELS,
    MAX_SIDE_PIXELS,
    REGION_SCALE,
    auto_bands,
    band_count,
    band_dpi,
    plan_bands,
    region_dpi,
)
from orchestrator.tools.tile_reading import (
    MAX_REPLY_CHARS,
    TileResult,
    compatible,
    decide_auto,
    parse_reading,
    parse_tile_reply,
)

from .test_b266_region_zoom import (
    _A4_AT_100DPI,
    _PDFTOPPM_PAGE_OR_REGION,
    _VL,
    _calls,
    _crop_path,
    _doc,
    _image_ref_of,
    _tools,
)
from .test_read_page import _install_office_stubs

# ---------------------------------------------------------------------------
# N1 —— 回答先截断,配平扫描是线性的
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("unit", ["{", '{"a'], ids=["braces", "open-strings"])
def test_a_huge_hostile_reply_parses_fast(unit: str) -> None:
    text = unit * (100_000 // len(unit))
    started = time.perf_counter()
    reply = parse_tile_reply(text)
    elapsed = time.perf_counter() - started
    assert elapsed < 0.05, elapsed
    assert reply.status == "unparseable"
    assert len(reply.raw) == MAX_REPLY_CHARS


@pytest.mark.parametrize(
    ("text", "value"),
    [
        ('noise {"status": "found", "value": "1"} tail', "1"),
        ('{ {"status": "found", "value": "2"} never closed', "2"),
        ('}} stray {"status": "found", "value": "3"}', "3"),
        ('{"status": "found", "value": "a } b", "unit": ""}', "a } b"),
        ('{"status": "found", "value": "q\\"}x"}', 'q"}x'),
    ],
    ids=["embedded", "inner-of-unclosed", "stray-closers", "brace-in-string", "escaped-quote"],
)
def test_the_linear_scan_finds_the_first_balanced_object(text: str, value: str) -> None:
    reply = parse_tile_reply(text)
    assert (reply.status, reply.value) == ("found", value)


# ---------------------------------------------------------------------------
# N2 —— 横条 / 局部的 dpi 有上限,dpi 写进文件名
# ---------------------------------------------------------------------------

#: 100 dpi 下的整页像素:A0(33.1 x 46.8 英寸)与 200 x 200 英寸的大幅面。
_A0_AT_100DPI = (3311, 4681)
_HUGE_AT_100DPI = (20_000, 20_000)


@pytest.mark.parametrize(
    "page_px", [_A4_AT_100DPI, _A0_AT_100DPI, _HUGE_AT_100DPI], ids=["A4", "A0", "200in"]
)
def test_band_renders_are_bounded(page_px: tuple[int, int]) -> None:
    pixels = page_px[0] * page_px[1] * 9
    for band in plan_bands(page_px, pixels):
        job = _region_job(band.box, page=1, page_px=page_px, bands=True)
        assert job["dpi"] <= 300
        assert job["w"] * job["h"] <= MAX_BAND_PIXELS, (page_px, job)
        assert max(job["w"], job["h"]) <= MAX_SIDE_PIXELS, (page_px, job)
        assert job["name"].endswith(f"-d{job['dpi']}.jpg")


def test_an_a4_band_keeps_the_full_300_dpi() -> None:
    band = auto_bands(6)[0]
    assert band_dpi(_A4_AT_100DPI, band.box, base_dpi=RENDER_DPI) == 300


def test_an_a0_band_is_rendered_below_300_dpi() -> None:
    band = plan_bands(_A0_AT_100DPI, _A0_AT_100DPI[0] * _A0_AT_100DPI[1] * 9)[0]
    assert band_dpi(_A0_AT_100DPI, band.box, base_dpi=RENDER_DPI) < 300


def test_a_thin_explicit_region_on_a_wide_page_is_bounded_on_its_long_side() -> None:
    page = (8000, 500)  # 80 x 5 英寸的横幅
    box = (0, 0, 1000, 100)
    job = _region_job(box, page=1, page_px=page)
    assert max(job["w"], job["h"]) <= MAX_SIDE_PIXELS
    assert job["dpi"] == region_dpi(page, box, base_dpi=RENDER_DPI)


@pytest.mark.anyio
async def test_an_explicit_region_equal_to_a_band_box_renders_at_its_own_dpi(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = _install_office_stubs(tmp_path, monkeypatch, pdftoppm_body=_PDFTOPPM_PAGE_OR_REGION)
    doc = _doc(tmp_path)
    tool, _read_page, _runtime = _tools(doc, _VL(answer=lambda _ref: "NOT_FOUND"))
    await tool.call({"path": doc.path, "unit": 1, "question": "?", "region": "auto"}, ctx=doc.ctx)
    after_bands = _calls(bin_dir, "pdftoppm")
    band = auto_bands(6)[0].box

    vl = _VL(answer=lambda _ref: "ok")
    tool, _read_page, _runtime = _tools(doc, vl)
    result = await tool.call(
        {"path": doc.path, "unit": 1, "question": "?", "region": list(band)}, ctx=doc.ctx
    )

    own = region_dpi(_A4_AT_100DPI, band, base_dpi=RENDER_DPI)
    assert own != 300
    assert _calls(bin_dir, "pdftoppm") == after_bands + 1  # 重新渲了,没复用 300 dpi 的横条
    assert result.meta["crop"]["dpi"] == own
    assert result.meta["crop"]["reused"] is False
    assert _image_ref_of(vl.calls[0]).endswith(f"-d{own}.jpg")
    args = _crop_path(doc, _image_ref_of(vl.calls[0])).read_text().split()
    assert args[args.index("-r") + 1] == str(own)


#: pdftoppm 桩:整页渲成 8000 x 500(80 x 5 英寸的横幅);局部照旧写下收到的参数。
_PDFTOPPM_WIDE_PAGE = _PDFTOPPM_PAGE_OR_REGION.replace(repr(_A4_AT_100DPI), repr((8000, 500)))


@pytest.mark.anyio
async def test_path_a_never_inlines_a_region_above_the_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert _PDFTOPPM_WIDE_PAGE != _PDFTOPPM_PAGE_OR_REGION
    _install_office_stubs(tmp_path, monkeypatch, pdftoppm_body=_PDFTOPPM_WIDE_PAGE)
    doc = _doc(tmp_path)
    _tool, read_page, _runtime = _tools(doc, _VL(), delivery="inline")

    result = await read_page.call(
        {"path": doc.path, "units": [1], "region": [0, 0, 1000, 100]}, ctx=doc.ctx
    )

    width, height = result.meta["pixels"]
    assert max(width, height) <= MAX_SIDE_PIXELS
    assert width * height <= MAX_BAND_PIXELS
    (crop_ref,) = result.state_updates["viewed_figures"]
    parsed = parse_rendered_figure_ref(crop_ref)
    assert parsed is not None and parsed.region == (0, 0, 1000, 100)
    assert crop_ref.endswith(f"-d{result.meta['dpi']}.jpg")


# ---------------------------------------------------------------------------
# (a) 超大指数不炸,按文字比
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value", ["1e999999999", "5.6\u00d710^999999999", "5e" + "9" * 5000, "1e51"]
)
def test_absurd_exponents_fall_back_to_text(value: str) -> None:
    assert parse_reading(value).kind == "text"


def test_a_sane_exponent_is_still_a_number() -> None:
    reading = parse_reading("1.2E9")
    assert reading.kind == "number"
    ok, _ = compatible(reading, parse_reading("1.2\u00d710^9"))
    assert ok


# ---------------------------------------------------------------------------
# (b) 单独一个「无」「没有」「N/A」只在散文里算没看到
# ---------------------------------------------------------------------------


def _json(status: str, value: str = "") -> str:
    return json.dumps({"status": status, "label": "", "value": value, "unit": ""})


@pytest.mark.parametrize("value", ["\u65e0", "\u6ca1\u6709", "N/A"])  # 无 / 没有
def test_a_json_found_bare_word_stays_a_value(value: str) -> None:
    reply = parse_tile_reply(_json("found", value))
    assert (reply.status, reply.value) == ("found", value)


def test_a_json_found_not_found_phrase_is_still_not_found() -> None:
    assert parse_tile_reply(_json("found", "\u672a\u627e\u5230")).status == "not_found"  # 未找到


@pytest.mark.parametrize("text", ["\u65e0", "\u6ca1\u6709", "N/A", "NA"])
def test_a_prose_bare_word_is_not_found(text: str) -> None:
    assert parse_tile_reply(text).status == "not_found"


# ---------------------------------------------------------------------------
# (c) 上标单位
# ---------------------------------------------------------------------------


def test_superscript_units_match_the_caret_and_star_spellings() -> None:
    sup = parse_reading("4.5\u00d710\u2079/L")  # 乘 10 的 9 次方(上标)/L
    for other in ("4.5 10^9/L", "4.5 10*9/L", "4.5\u00d710^9/L"):
        ok, why = compatible(sup, parse_reading(other))
        assert ok, (other, why)
    assert sup.unit == "10^9/l"
    ok, _ = compatible(parse_reading("4.5\u00d710\u00b9\u00b2/L"), parse_reading("4.5 10^9/L"))
    assert not ok  # 10¹² ≠ 10⁹


# ---------------------------------------------------------------------------
# (d) 横条准备失败:仍整张读,标不确定;整张读也失败才报错
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_a_band_render_failure_still_returns_the_whole_image_reading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_office_stubs(tmp_path, monkeypatch, pdftoppm_body=_PDFTOPPM_PAGE_OR_REGION)
    doc = _doc(tmp_path)
    (doc.work / doc.path).write_text("CHANGED")  # 内容哈希对不上 -> 横条报 stale
    os.utime(doc.work / doc.path, (1_000, 1_000))
    vl = _VL(answer=lambda _ref: "The glucose is 5.6.")
    tool, _read_page, _runtime = _tools(doc, vl)

    result = await tool.call(
        {"path": doc.path, "unit": 1, "question": "glucose?", "region": "auto"}, ctx=doc.ctx
    )

    assert [_image_ref_of(call) for call in vl.calls] == [doc.page_ref]
    assert result.content.startswith("The glucose is 5.6.\n\n")
    assert "whole-image reading; small print may be unreliable" in result.content
    assert "the bands could not be prepared (RegionStaleError" in result.content
    assert (result.meta["auto"], result.meta["auto_reason"]) == (
        "full_fallback",
        "bands_unavailable",
    )
    assert "RegionStaleError" in result.meta["bands_error"]


@pytest.mark.anyio
async def test_a_band_render_failure_and_a_failed_whole_read_is_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_office_stubs(tmp_path, monkeypatch, pdftoppm_body=_PDFTOPPM_PAGE_OR_REGION)
    doc = _doc(tmp_path)
    (doc.work / doc.path).write_text("CHANGED")
    os.utime(doc.work / doc.path, (1_000, 1_000))

    def fail(_ref: str) -> str:
        raise RuntimeError("vendor 500")

    tool, _read_page, _runtime = _tools(doc, _VL(answer=fail))
    with pytest.raises(RuntimeError, match="vendor 500") as info:
        await tool.call(
            {"path": doc.path, "unit": 1, "question": "?", "region": "auto"}, ctx=doc.ctx
        )
    assert type(info.value.__cause__).__name__ == "RegionStaleError"


# ---------------------------------------------------------------------------
# (e) 有横条被截断、整张读又失败:不拿任何一条的值当答案
# ---------------------------------------------------------------------------


def test_a_partial_band_with_a_failed_whole_read_has_no_reliable_reading() -> None:
    bands = auto_bands(4)
    replies = [_json("not_found"), _json("partial"), _json("found", "4"), _json("not_found")]
    results = [
        TileResult(tile=band, reply=parse_tile_reply(text))
        for band, text in zip(bands, replies, strict=True)
    ]
    decision = decide_auto(results, full_answer=None, full_error="timed out")
    assert decision.content.startswith("region='auto': no reliable reading - a band saw the value")
    assert not decision.content.startswith("4")
    assert (decision.path, decision.reason) == ("bands", "partial+full_failed")
    assert "region=[" in decision.content or "band 3" in decision.content


# ---------------------------------------------------------------------------
# (f) 极端宽高比
# ---------------------------------------------------------------------------


def _band_px(size: tuple[int, int], box: tuple[int, int, int, int]) -> tuple[float, float]:
    return (
        size[0] * (box[2] - box[0]) / REGION_SCALE,
        size[1] * (box[3] - box[1]) / REGION_SCALE,
    )


@pytest.mark.parametrize(
    ("size", "vertical"),
    [((15_000, 1_000), True), ((1_000, 15_000), False), ((4_000, 1_000), False)],
    ids=["very-wide", "very-tall", "exactly-4-to-1"],
)
def test_extreme_aspect_ratios_cut_along_the_long_side(
    size: tuple[int, int], vertical: bool
) -> None:
    bands = plan_bands(size, size[0] * size[1])
    assert len(bands) >= AUTO_MIN_BANDS
    for band in bands:
        x1, y1, x2, y2 = band.box
        if vertical:
            assert (y1, y2) == (0, 1000)  # 整高
        else:
            assert (x1, x2) == (0, 1000)  # 整宽
    if size != (4_000, 1_000):
        for band in bands:
            width, height = _band_px(size, band.box)
            assert max(width, height) / min(width, height) <= MAX_BAND_ASPECT, band
    edges = [band.box[0] if vertical else band.box[1] for band in bands]
    ends = [band.box[2] if vertical else band.box[3] for band in bands]
    assert edges[0] == 0 and ends[-1] == 1000


def test_a_moderately_wide_image_uses_fewer_bands_to_stay_near_the_aspect_limit() -> None:
    size = (3_000, 1_000)  # 3:1,按像素该切 3 条以上;横条太细时少切
    bands = plan_bands(size, 12_000_000)
    assert len(bands) < band_count(12_000_000)
    assert len(bands) >= AUTO_MIN_BANDS
