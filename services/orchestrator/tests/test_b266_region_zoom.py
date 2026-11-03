"""B-266 —— 看图「放大局部」:``ask_image`` / ``read_page`` 的 ``region``。

分四层测:纯计算(校验、切块几何、分辨率、汇总)、落点形状(布局正则 / 解析 / 可缓存)、
沙箱片段真跑(桩 pdftoppm / soffice,片段在本地 exec)、工具端到端(真 NAS 存储 + 真片段)。
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import io
import itertools
import json
import math
import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from langchain_core.messages import AIMessage, BaseMessage
from PIL import Image

from expert_work.persistence import is_rendered_crop_rel, is_rendered_figure_rel, region_crop_stem
from expert_work.protocol.multimodal import ImageRef, parse_image_ref
from orchestrator.graph_builder.figure_block import figure_block_message
from orchestrator.multimodal import (
    DispatchingImageResolver,
    InMemoryImageResolver,
    NasWorkspaceImageResolver,
    ResolvedImage,
    is_cacheable_image_ref,
    parse_rendered_figure_ref,
)
from orchestrator.tools import region_zoom
from orchestrator.tools.figure_lookup import figure_run_ids
from orchestrator.tools.nas_workspace_store import NasWorkspaceStore
from orchestrator.tools.read_page import (
    _CONVERT_TIMEOUT_S,
    _REGION_MAIN,
    _REGION_RENDER_TIMEOUT_S,
    _RENDER_MAIN,
    ReadPageTool,
    RegionRenderError,
    RegionStaleError,
    document_sha,
    workspace_figure_ref,
)
from orchestrator.tools.region_zoom import (
    AUTO_MAX_BANDS,
    AUTO_MIN_BANDS,
    AUTO_REGION,
    MAX_REGION_DPI,
    MIN_REGION_AREA,
    REGION_HINT,
    REGION_TARGET_PIXELS,
    auto_bands,
    band_count,
    crop_image,
    parse_box,
    parse_region,
    pixel_box,
    region_dpi,
)
from orchestrator.tools.registry import ToolBlockedError, ToolContext
from orchestrator.tools.sandbox import RecordingSandboxRuntime, SandboxOutcome
from orchestrator.tools.tile_reading import tile_instruction
from orchestrator.tools.vision import AskImageTool, ask_image_region_auto_total
from orchestrator.tools.workspace_paths import WorkspaceView

from .test_read_page import _install_office_stubs, _snippet_params

_KEY = "pf-probe-33086dc0"
_CONV = UUID("55555555-5555-5555-5555-555555555555")
_A4_AT_100DPI = (827, 1169)

# ---------------------------------------------------------------------------
# 纯计算
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [[0, 0, 100, 100], [900, 900, 1000, 1000], (0, 0, 1000, 1000), [10, 20, 30, 520]],
)
def test_a_valid_box_parses(raw: Sequence[int]) -> None:
    assert parse_region(list(raw), tool="ask_image") == tuple(raw)


@pytest.mark.parametrize(
    ("raw", "needle"),
    [
        ([0, 0, 100], "four integers"),
        ([0, 0, 100, 100, 5], "four integers"),
        ([0.0, 0, 100, 100], "four integers"),
        ([True, 0, 100, 100], "four integers"),
        ("0,0,100,100", "four integers"),
        ("AUTO", "four integers"),
        ([100, 0, 100, 500], "out of range"),
        ([200, 0, 100, 500], "out of range"),
        ([0, 500, 100, 400], "out of range"),
        ([-1, 0, 100, 500], "out of range"),
        ([0, 0, 1001, 500], "out of range"),
        ([0, 0, 100, 99], "too small"),
    ],
)
def test_an_invalid_region_is_refused(raw: object, needle: str) -> None:
    with pytest.raises(ValueError, match=needle):
        parse_region(raw, tool="ask_image")


def test_the_area_floor_is_one_percent() -> None:
    assert MIN_REGION_AREA == 10_000
    assert parse_box([0, 0, 100, 100], tool="t") == (0, 0, 100, 100)
    with pytest.raises(ValueError, match="too small"):
        parse_box([0, 0, 100, 99], tool="t")


def test_auto_is_a_region_but_not_a_box() -> None:
    assert parse_region("auto", tool="ask_image") == AUTO_REGION
    assert parse_region(None, tool="ask_image") is None
    with pytest.raises(ValueError, match="four integers"):
        parse_box("auto", tool="read_page")


@pytest.mark.parametrize(
    ("pixels", "count"),
    [
        (0, 3),
        (3_999_999, 3),
        (4_500_000, 3),
        (4_500_001, 4),
        (8_700_000, 6),
        (12_000_000, 8),
        (60_000_000, 8),
    ],
)
def test_band_count_is_pixels_over_one_and_a_half_megapixels_clamped(
    pixels: int, count: int
) -> None:
    assert band_count(pixels) == count


@pytest.mark.parametrize("count", range(AUTO_MIN_BANDS, AUTO_MAX_BANDS + 1))
def test_bands_are_full_width_cover_the_height_and_overlap_twelve_percent(count: int) -> None:
    bands = auto_bands(count)
    assert [b.label for b in bands] == [f"band {i}" for i in range(1, count + 1)]
    for band in bands:
        x1, y1, x2, y2 = band.box
        assert (x1, x2) == (0, 1000)
        assert parse_box(list(band.box), tool="t") == band.box
        assert (x2 - x1) * (y2 - y1) >= MIN_REGION_AREA
    assert bands[0].box[1] == 0
    assert bands[-1].box[3] == 1000
    for upper, lower in itertools.pairwise(bands):
        overlap = upper.box[3] - lower.box[1]
        for band in (upper, lower):
            assert overlap >= 0.12 * (band.box[3] - band.box[1]), (count, overlap)


@pytest.mark.parametrize("count", range(AUTO_MIN_BANDS, AUTO_MAX_BANDS + 1))
def test_a_text_row_no_taller_than_the_overlap_is_never_split(count: int) -> None:
    """一行字(高度不超过重叠)放在任何位置,总有一条横条把它整个装下。"""
    bands = auto_bands(count)
    row = math.floor(min(b.box[3] - b.box[1] for b in bands) * 0.12)
    for top in range(0, 1000 - row + 1):
        assert any(b.box[1] <= top and top + row <= b.box[3] for b in bands), (count, top)


def test_region_dpi_targets_one_and_a_half_megapixels() -> None:
    whole = region_dpi(_A4_AT_100DPI, (0, 0, 1000, 1000), base_dpi=100)
    corner = (0, 0, 541, 371)
    tile = region_dpi(_A4_AT_100DPI, corner, base_dpi=100)
    for dpi, box in ((whole, (0, 0, 1000, 1000)), (tile, corner)):
        scale = dpi / 100
        left, top, right, bottom = pixel_box(
            box, (_A4_AT_100DPI[0] * scale, _A4_AT_100DPI[1] * scale)
        )
        pixels = (right - left) * (bottom - top)
        assert 0.95 * REGION_TARGET_PIXELS <= pixels <= 1.02 * REGION_TARGET_PIXELS, (dpi, pixels)
    assert 120 <= whole < tile < MAX_REGION_DPI


def test_region_dpi_is_capped_at_400() -> None:
    assert region_dpi(_A4_AT_100DPI, (0, 0, 100, 100), base_dpi=100) == MAX_REGION_DPI


def test_pixel_box_rounds_outwards_and_clamps_to_the_image() -> None:
    assert pixel_box((0, 0, 1000, 1000), (827, 1169)) == (0, 0, 827, 1169)
    assert pixel_box((500, 500, 1000, 1000), (101, 101)) == (50, 50, 101, 101)
    left, _top, right, _bottom = pixel_box((333, 0, 334, 1000), (1000.5, 10))
    assert (left, right) == (333, 335)


_TILES = auto_bands(6)
#: 一块按提示词回的 JSON(B-266 review M3)。
NOT_FOUND = '{"status": "not_found", "label": "", "value": "", "unit": ""}'


def _found(value: str, *, unit: str = "", label: str = "") -> str:
    return json.dumps({"status": "found", "label": label, "value": value, "unit": unit})


# ---------------------------------------------------------------------------
# 宿主裁图(Pillow)
# ---------------------------------------------------------------------------


def _image_bytes(
    size: tuple[int, int], *, fmt: str = "JPEG", mode: str = "RGB", orientation: int | None = None
) -> bytes:
    """左半红、右半蓝的图;``orientation`` 写进 EXIF 方向标记。"""
    image = Image.new(mode, size, (0, 0, 255, 255) if "A" in mode else (0, 0, 255))
    image.paste((255, 0, 0, 255) if "A" in mode else (255, 0, 0), (0, 0, size[0] // 2, size[1]))
    buffer = io.BytesIO()
    if orientation is not None:
        exif = Image.Exif()
        exif[0x0112] = orientation
        image.save(buffer, format=fmt, exif=exif.tobytes())
    else:
        image.save(buffer, format=fmt)
    return buffer.getvalue()


def _decode(data: bytes) -> Image.Image:
    image = Image.open(io.BytesIO(data))
    image.load()
    return image


def test_crop_image_cuts_native_pixels_without_upscaling() -> None:
    (piece,) = crop_image(_image_bytes((2000, 1000)), [(0, 0, 250, 500)])
    image = _decode(piece.data)
    assert (piece.width, piece.height) == image.size == (500, 500)
    assert image.format == "JPEG"
    red, _green, blue = image.getpixel((250, 250))
    assert red > 200 and blue < 60


def test_crop_image_turns_the_image_upright_first() -> None:
    """EXIF 方向 6(顺时针转 90 度):宽高互换后再按框裁,与看图模型看到的方向一致。"""
    data = _image_bytes((2000, 1000), orientation=6)
    assert region_zoom.image_size(data) == (1000, 2000)
    (piece,) = crop_image(data, [(0, 0, 1000, 500)])
    assert (piece.width, piece.height) == (1000, 1000)


def test_crop_image_puts_transparency_on_white() -> None:
    image = Image.new("RGBA", (400, 400), (0, 0, 0, 0))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    (piece,) = crop_image(buffer.getvalue(), [(0, 0, 1000, 1000)])
    assert _decode(piece.data).getpixel((200, 200))[0] > 240


def test_crop_image_refuses_over_the_decode_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(region_zoom, "MAX_DECODE_PIXELS", 1_000_000)
    with pytest.raises(region_zoom.RegionImageError, match="pixel limit"):
        crop_image(_image_bytes((2000, 1000)), [(0, 0, 500, 500)])


def test_crop_image_refuses_bytes_that_are_not_an_image() -> None:
    with pytest.raises(region_zoom.RegionImageError, match="could not be decoded"):
        crop_image(b"not an image", [(0, 0, 500, 500)])


# ---------------------------------------------------------------------------
# 落点形状
# ---------------------------------------------------------------------------

_RUN = "99999999-9999-4999-8999-999999999999"
_SHA_A, _SHA_B = "a" * 16, "b" * 16
_DOC_CROP = f".tool_results/{_RUN}/crops/{_SHA_A}/{_SHA_B}/_u5/page-2-100-200-600-500-d289.jpg"
_IMAGE_CROP = f".tool_results/{_RUN}/crops/{_SHA_A}/0-0-541-371.jpg"


@pytest.mark.parametrize("rel", [_DOC_CROP, _IMAGE_CROP])
def test_both_crop_shapes_are_platform_renders(rel: str) -> None:
    assert is_rendered_crop_rel(rel)
    assert not is_rendered_figure_rel(rel)


@pytest.mark.parametrize(
    "rel",
    [
        f".tool_results/{_RUN}/crops/evil.jpg",
        f".tool_results/{_RUN}/crops/{_SHA_A}/x.jpg",
        f".tool_results/{_RUN}/crops/{_SHA_A}/0-0-541.jpg",
        f".tool_results/{_RUN}/crops/{_SHA_A}/0-0-541-371.png",
        f".tool_results/{_RUN}/crops/{_SHA_A}/0-0-541-371.jpg\n",
        f".tool_results/{_RUN}/crops/{_SHA_A}/{_SHA_B}/_u5/page-2.jpg",
        # 文档页的局部必须带实际 dpi(review round 2 N2):没有 dpi 的老写法不认。
        f".tool_results/{_RUN}/crops/{_SHA_A}/{_SHA_B}/_u5/page-2-100-200-600-500.jpg",
        f".tool_results/{_RUN}/crops/{_SHA_A}/{_SHA_B}/_u5/page-2-100-200-600-500-d0.jpg",
        f".tool_results/{_RUN}/crops/{_SHA_A}/{_SHA_B}/_u5/page-2-100-200-600-500-d12345.jpg",
        f".tool_results/{_RUN}/figures/{_SHA_A}/0-0-541-371.jpg",
        f"x/.tool_results/{_RUN}/crops/{_SHA_A}/0-0-541-371.jpg",
    ],
)
def test_other_shapes_under_crops_are_not_platform_renders(rel: str) -> None:
    assert not is_rendered_crop_rel(rel)


def test_the_page_shape_is_unchanged() -> None:
    page = f".tool_results/{_RUN}/figures/{_SHA_A}/{_SHA_B}/_u5/page-02.jpg"
    assert is_rendered_figure_rel(page)
    assert not is_rendered_crop_rel(page)


def test_a_document_crop_ref_parses_like_a_page_with_its_box() -> None:
    tenant, user = uuid4(), uuid4()
    ref = workspace_figure_ref(tenant, user, f"agents/{_KEY}/threads/{_CONV}/work/{_DOC_CROP}")
    parsed = parse_rendered_figure_ref(ref)
    assert parsed is not None
    assert (parsed.doc_sha, parsed.render_sha, parsed.unit, parsed.page) == (_SHA_A, _SHA_B, 5, 2)
    assert parsed.region == (100, 200, 600, 500)
    assert is_cacheable_image_ref(ref)
    assert figure_run_ids([ref]) == {UUID(_RUN)}


def test_an_image_crop_ref_is_cacheable_but_not_a_page() -> None:
    ref = workspace_figure_ref(uuid4(), uuid4(), f"agents/{_KEY}/{_IMAGE_CROP}")
    assert parse_rendered_figure_ref(ref) is None
    assert is_cacheable_image_ref(ref)


def test_a_file_the_model_wrote_under_crops_is_not_cacheable() -> None:
    ref = workspace_figure_ref(uuid4(), uuid4(), f".tool_results/{_RUN}/crops/evil.jpg")
    assert not is_cacheable_image_ref(ref)


# ---------------------------------------------------------------------------
# 沙箱片段真跑 + 工具端到端
# ---------------------------------------------------------------------------

#: pdftoppm 桩:渲局部(``-singlefile``)时把收到的参数写进产出;渲整页时写一张真 JPEG
#: (宿主要从整页读页面像素尺寸)。
_PDFTOPPM_PAGE_OR_REGION = (
    'if "-singlefile" in args:\n'
    '    with open(prefix + ".jpg", "w") as fh:\n'
    '        fh.write(" ".join(args))\n'
    "else:\n"
    "    from PIL import Image\n"
    f'    page = Image.new("RGB", {_A4_AT_100DPI!r}, "white")\n'
    '    page.save(f"{prefix}-{unit:02d}.jpg", "JPEG")\n'
)


@dataclass
class _LocalRuntime(RecordingSandboxRuntime):
    """把片段真跑在本地:``ws`` 换成测试里的 agent 根(沙箱里的 ``/agent``)。"""

    agent_root: Path = field(default_factory=Path)

    async def exec(self, **kwargs: Any) -> SandboxOutcome:
        await super().exec(**kwargs)
        code = str(kwargs["code"])
        params = _snippet_params(code)
        params["ws"] = str(self.agent_root)
        code = f"_PARAMS = {json.dumps(params)!r}\n" + code.split("\n", 1)[1]
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            exec(compile(code, "<snippet>", "exec"), {})  # noqa: S102
        return SandboxOutcome(stdout=buffer.getvalue(), stderr="", exit_code=0, timed_out=False)


def _answer_ok(_ref: str) -> str:
    return "ok"


@dataclass
class _VL:
    """看图模型替身:按图 ref 回答(``answer(ref)`` 返回文字或抛异常),记下每次调用。"""

    answer: Callable[[str], str] | None = None
    usage: dict[str, int] | None = None
    calls: list[list[BaseMessage]] = field(default_factory=list)

    async def __call__(self, *, messages: Sequence[BaseMessage], tools: Sequence[Any]) -> AIMessage:
        self.calls.append(list(messages))
        await asyncio.sleep(0)
        reply = self.answer or _answer_ok
        return AIMessage(content=reply(_image_ref_of(messages)), usage_metadata=self.usage)  # type: ignore[arg-type]


def _image_ref_of(messages: Sequence[BaseMessage]) -> str:
    content = messages[1].content
    assert isinstance(content, list)
    block = content[1]
    assert isinstance(block, dict)
    return str(block["ref"])


def _question_of(messages: Sequence[BaseMessage]) -> str:
    content = messages[1].content
    assert isinstance(content, list) and isinstance(content[0], dict)
    return str(content[0]["text"])


@dataclass
class _Doc:
    """真 NAS 目录里的一份文档 + 它第 ``page`` 页(``unit``)已渲好的整页。"""

    root: Path
    tenant: UUID
    user: UUID
    ctx: ToolContext
    work: Path
    agent_root: Path
    path: str
    render_sha: str
    doc_sha: str
    page_ref: str


def _doc(
    tmp_path: Path,
    *,
    name: str = "report.pdf",
    content: str = "PDF-SOURCE",
    unit: int = 1,
    page: int = 1,
    with_pdf: bool = True,
    page_px: tuple[int, int] = _A4_AT_100DPI,
) -> _Doc:
    tenant, user = uuid4(), uuid4()
    root = tmp_path / "nas"
    agent_root = root / str(tenant) / str(user) / "agents" / _KEY
    work = agent_root / "threads" / str(_CONV) / "work"
    work.mkdir(parents=True)
    source = work / name
    source.write_text(content)
    os.utime(source, (1_000, 1_000))
    ctx = ToolContext(
        tenant_id=tenant,
        run_id=uuid4(),
        user_id=user,
        thread_id=_CONV,
        agent_key=_KEY,
        workspace_layout="conversation",
    )
    doc_sha = document_sha(name, view=ctx.workspace_view)
    assert doc_sha is not None
    render_sha = hashlib.sha256(b"dpi=100;" + content.encode()).hexdigest()[:16]
    render_dir = f".tool_results/{ctx.run_id}/figures/{doc_sha}/{render_sha}"
    page_file = work / render_dir / f"_u{unit}" / f"page-{page}.jpg"
    page_file.parent.mkdir(parents=True)
    Image.new("RGB", page_px, "white").save(page_file, "JPEG")
    os.utime(page_file, (2_000, 2_000))
    if with_pdf and not name.endswith(".pdf"):
        (work / render_dir / "_pdf").mkdir()
        (work / render_dir / "_pdf" / (Path(name).stem + ".pdf")).write_text("CONVERTED-PDF")
    page_ref = workspace_figure_ref(
        tenant, user, f"agents/{_KEY}/threads/{_CONV}/work/{render_dir}/_u{unit}/page-{page}.jpg"
    )
    return _Doc(root, tenant, user, ctx, work, agent_root, name, render_sha, doc_sha, page_ref)


def _tools(
    doc: _Doc, vl: _VL, *, delivery: str = "ask_image", **ask: Any
) -> tuple[AskImageTool, ReadPageTool, _LocalRuntime]:
    runtime = _LocalRuntime(agent_root=doc.agent_root)
    resolver = NasWorkspaceImageResolver(root=doc.root)
    read_page = ReadPageTool(
        client=runtime,
        figure_delivery=delivery,  # type: ignore[arg-type]
        image_resolver=resolver,
    )
    tool = AskImageTool(
        vl_caller=vl,
        image_resolver=resolver,
        workspace_store=NasWorkspaceStore(root=str(doc.root)),
        page_renderer=read_page,
        **ask,
    )
    return tool, read_page, runtime


def _calls(bin_dir: Path, binary: str) -> int:
    counter = bin_dir / f"{binary}_calls"
    return int(counter.read_text()) if counter.exists() else 0


def _crop_path(doc: _Doc, ref: str) -> Path:
    prefix = f"expert_work://workspace/{doc.tenant}/{doc.user}/"
    assert ref.startswith(prefix)
    return doc.root / str(doc.tenant) / str(doc.user) / ref[len(prefix) :]


@pytest.mark.anyio
async def test_an_explicit_box_on_a_pdf_page_renders_just_that_region(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = _install_office_stubs(tmp_path, monkeypatch, pdftoppm_body=_PDFTOPPM_PAGE_OR_REGION)
    doc = _doc(tmp_path)
    vl = _VL(answer=lambda _ref: "5.6 mmol/L")
    tool, _read_page, runtime = _tools(doc, vl)
    box = [100, 200, 600, 500]

    result = await tool.call(
        {"path": doc.path, "unit": 1, "question": "glucose?", "region": box}, ctx=doc.ctx
    )

    assert result.content == "5.6 mmol/L"
    (sent,) = [_image_ref_of(call) for call in vl.calls]
    crop = parse_rendered_figure_ref(sent)
    assert crop is not None and crop.region == (100, 200, 600, 500) and crop.page == 1
    # 落在本对话、本 run 的 crops 目录下。
    assert f"/agents/{_KEY}/threads/{_CONV}/work/.tool_results/{doc.ctx.run_id}/crops/" in sent
    dpi = region_dpi(_A4_AT_100DPI, (100, 200, 600, 500), base_dpi=100)
    args = _crop_path(doc, sent).read_text().split()
    assert args[args.index("-r") + 1] == str(dpi)
    assert args[args.index("-f") + 1] == args[args.index("-l") + 1] == "1"
    assert result.meta["region"] == box
    assert result.meta["crop"]["dpi"] == dpi
    assert result.meta["crop"]["reused"] is False
    assert REGION_HINT not in result.content
    assert len(runtime.execs) == 1
    assert _calls(bin_dir, "soffice") == 0


@pytest.mark.anyio
async def test_the_same_box_again_reuses_the_crop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = _install_office_stubs(tmp_path, monkeypatch, pdftoppm_body=_PDFTOPPM_PAGE_OR_REGION)
    doc = _doc(tmp_path)
    vl = _VL()
    tool, _read_page, _runtime = _tools(doc, vl)
    args = {"path": doc.path, "unit": 1, "question": "?", "region": [100, 200, 600, 500]}

    first = await tool.call(args, ctx=doc.ctx)
    rendered = _calls(bin_dir, "pdftoppm")
    second = await tool.call(args, ctx=doc.ctx)

    assert _calls(bin_dir, "pdftoppm") == rendered
    assert second.meta["crop"]["reused"] is True
    assert first.meta["crop"]["reused"] is False
    assert _image_ref_of(vl.calls[0]) == _image_ref_of(vl.calls[1])


@pytest.mark.anyio
async def test_a_docx_region_reuses_the_converted_pdf_on_the_real_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """docx 的 unit 是图编号:渲的是整页渲染里记的真实页号;转好的 PDF 直接用,不再转。"""
    bin_dir = _install_office_stubs(tmp_path, monkeypatch, pdftoppm_body=_PDFTOPPM_PAGE_OR_REGION)
    doc = _doc(tmp_path, name="report.docx", content="DOCX", unit=5, page=3)
    vl = _VL()
    tool, _read_page, _runtime = _tools(doc, vl)

    await tool.call(
        {"path": doc.path, "unit": 5, "question": "?", "region": [0, 0, 500, 500]}, ctx=doc.ctx
    )

    sent = _image_ref_of(vl.calls[0])
    args = _crop_path(doc, sent).read_text().split()
    assert args[args.index("-f") + 1] == "3"
    assert args[-2].endswith("/_pdf/report.pdf")
    assert f"/figures/{doc.doc_sha}/{doc.render_sha}/_pdf/" in args[-2]
    assert _calls(bin_dir, "soffice") == 0


@pytest.mark.anyio
async def test_a_docx_region_reconverts_when_the_pdf_is_gone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = _install_office_stubs(tmp_path, monkeypatch, pdftoppm_body=_PDFTOPPM_PAGE_OR_REGION)
    doc = _doc(tmp_path, name="report.pptx", content="PPTX", unit=2, page=2, with_pdf=False)
    vl = _VL()
    tool, _read_page, _runtime = _tools(doc, vl)

    await tool.call(
        {"path": doc.path, "unit": 2, "question": "?", "region": [0, 0, 500, 500]}, ctx=doc.ctx
    )

    assert _calls(bin_dir, "soffice") == 1
    args = _crop_path(doc, _image_ref_of(vl.calls[0])).read_text().split()
    assert f"/crops/{doc.doc_sha}/{doc.render_sha}/_u2/_pdf/report.pdf" in args[-2]


@pytest.mark.anyio
async def test_a_region_on_a_document_changed_since_the_render_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """mtime 倒退(``cp -p``)骗得过整页的新鲜度闸,骗不过内容哈希:不渲、说清原因。"""
    bin_dir = _install_office_stubs(tmp_path, monkeypatch, pdftoppm_body=_PDFTOPPM_PAGE_OR_REGION)
    doc = _doc(tmp_path)
    (doc.work / doc.path).write_text("SOMETHING ELSE")
    os.utime(doc.work / doc.path, (1_000, 1_000))
    vl = _VL()
    tool, _read_page, _runtime = _tools(doc, vl)

    with pytest.raises(RegionStaleError, match="changed after page 1 was rendered") as info:
        await tool.call(
            {"path": doc.path, "unit": 1, "question": "?", "region": [0, 0, 500, 500]},
            ctx=doc.ctx,
        )

    # B-266 review (f) —— 不是「原样重试一次」:先重新 read_page 这一页。
    assert f"read_page(path={doc.path!r}, units=[1])" in str(info.value)
    assert "call read_page for this page again first" in info.value.advice
    assert isinstance(info.value, RegionRenderError)
    assert vl.calls == []
    assert _calls(bin_dir, "pdftoppm") == 0


@pytest.mark.anyio
async def test_a_failed_region_render_leaves_no_half_written_crop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_office_stubs(
        tmp_path,
        monkeypatch,
        pdftoppm_body='open(prefix + ".jpg", "w").write("partial")\nsys.exit(1)\n',
    )
    doc = _doc(tmp_path)
    tool, _read_page, _runtime = _tools(doc, _VL())

    with pytest.raises(RegionRenderError, match="none of the regions"):
        await tool.call(
            {"path": doc.path, "unit": 1, "question": "?", "region": [0, 0, 500, 500]},
            ctx=doc.ctx,
        )

    crops = doc.work / ".tool_results" / str(doc.ctx.run_id) / "crops"
    assert list(crops.rglob("*.jpg")) == []


def _is_band(ref: str) -> bool:
    return "/crops/" in ref


@pytest.mark.anyio
async def test_auto_on_a_document_page_reads_bands_and_the_whole_page_in_parallel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A4 页按 300 dpi 约 870 万像素:6 条横条一次沙箱渲完(300 dpi),同时整页快看一次;
    横条给出唯一的值 → 用横条的值。7 次调用全部记账。"""
    _install_office_stubs(tmp_path, monkeypatch, pdftoppm_body=_PDFTOPPM_PAGE_OR_REGION)
    doc = _doc(tmp_path)
    third = region_crop_stem(_TILES[2].box)
    vl = _VL(
        answer=lambda ref: (
            "The glucose looks like 5.8."
            if not _is_band(ref)
            else _found("5.6", unit="mmol/L", label="GLU")
            if ref.endswith(f"-{third}-d300.jpg")
            else NOT_FOUND
        ),
        usage={"input_tokens": 1400, "output_tokens": 5, "total_tokens": 1405},
    )
    metered: list[AIMessage] = []

    async def meter(response: AIMessage, *, tenant_id: UUID, user_id: UUID | None) -> None:
        metered.append(response)

    tool, _read_page, runtime = _tools(doc, vl, usage_meter=meter)
    before = ask_image_region_auto_total.labels(path="bands", reason="single")._value.get()

    result = await tool.call(
        {"path": doc.path, "unit": 1, "question": "glucose?", "region": "auto", "depth": "deep"},
        ctx=doc.ctx,
    )

    assert len(runtime.execs) == 1  # 六条一次沙箱执行渲完
    assert len(vl.calls) == 7
    assert len(metered) == 7  # 每次调用都记账
    full = [call for call in vl.calls if not _is_band(_image_ref_of(call))]
    bands = [call for call in vl.calls if _is_band(_image_ref_of(call))]
    assert [_image_ref_of(call) for call in full] == [doc.page_ref]
    assert _question_of(full[0]) == "glucose?"  # 整张读用原问题,不加 JSON 要求
    for call in bands:
        assert _question_of(call) == f"glucose?\n\n{tile_instruction('y')}"
    assert result.content.startswith("5.6 mmol/L\n")
    assert "band 3" in result.content
    assert "5.8" not in result.content
    meta = result.meta
    assert (meta["region"], meta["auto"], meta["auto_reason"]) == ("auto", "bands", "single")
    assert meta["auto_pixels"] == _A4_AT_100DPI[0] * _A4_AT_100DPI[1] * 9
    assert (meta["aggregate"], meta["depth"], meta["depth_ignored"]) == ("single", "quick", "deep")
    assert meta["vl_calls"] == 7
    assert meta["vl_usage"] == {"input_tokens": 9800, "output_tokens": 35, "total_tokens": 9835}
    assert meta["full"] == {"answer": "The glucose looks like 5.8."}
    by_band = {t["tile"]: t for t in meta["bands"]}
    assert list(by_band) == [f"band {i}" for i in range(1, 7)]
    assert by_band["band 3"]["status"] == "found"
    assert (by_band["band 3"]["value"], by_band["band 3"]["unit"]) == ("5.6", "mmol/L")
    assert by_band["band 1"]["status"] == "not_found"
    assert all(
        t["dpi"] == 300 and t["box"][0] == 0 and t["box"][2] == 1000 for t in by_band.values()
    )
    args = _crop_path(doc, _image_ref_of(bands[0])).read_text().split()
    assert args[args.index("-r") + 1] == "300"
    assert ask_image_region_auto_total.labels(path="bands", reason="single")._value.get() == (
        before + 1
    )


@pytest.mark.anyio
async def test_auto_on_a_small_document_page_is_gated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """一页小卡片:100 dpi 渲出 400 x 500,按 300 dpi 是 180 万像素 —— 不切,整页快看一次。"""
    _install_office_stubs(tmp_path, monkeypatch, pdftoppm_body=_PDFTOPPM_PAGE_OR_REGION)
    doc = _doc(tmp_path, page_px=(400, 500))
    vl = _VL(answer=lambda _ref: "A small card.")
    tool, _read_page, runtime = _tools(doc, vl)

    result = await tool.call(
        {"path": doc.path, "unit": 1, "question": "what?", "region": "auto"}, ctx=doc.ctx
    )

    assert runtime.execs == []
    assert [_image_ref_of(call) for call in vl.calls] == [doc.page_ref]
    assert _question_of(vl.calls[0]) == "what?"
    assert result.content.startswith("A small card.\n\n(region='auto': the image is small enough")
    assert (result.meta["auto"], result.meta["auto_reason"]) == ("gated", "small_image")
    assert result.meta["auto_pixels"] == 400 * 500 * 9


# ---------------------------------------------------------------------------
# 上传图 / 工作区图片:宿主按原像素裁
# ---------------------------------------------------------------------------


@dataclass
class _Upload:
    tool: AskImageTool
    ref: str
    ctx: ToolContext
    root: Path


def _upload(
    tmp_path: Path, vl: _VL, *, size: tuple[int, int] = (2400, 1200), **ask: Any
) -> _Upload:
    tenant, user = uuid4(), uuid4()
    root = tmp_path / "nas"
    (root / str(tenant) / str(user)).mkdir(parents=True)
    ref = ImageRef(tenant_id=tenant, thread_id=_CONV, image_id=uuid4(), ext=".png").to_uri()
    resolver = DispatchingImageResolver(
        uploads=InMemoryImageResolver(
            images={ref: ResolvedImage(media_type="image/png", data=_image_bytes(size, fmt="PNG"))}
        ),
        workspace=NasWorkspaceImageResolver(root=root),
    )
    ctx = ToolContext(
        tenant_id=tenant,
        run_id=uuid4(),
        user_id=user,
        thread_id=_CONV,
        agent_key=_KEY,
        workspace_layout="conversation",
    )
    tool = AskImageTool(
        vl_caller=vl,
        image_resolver=resolver,
        workspace_store=NasWorkspaceStore(root=str(root)),
        **ask,
    )
    return _Upload(tool=tool, ref=ref, ctx=ctx, root=root)


@pytest.mark.anyio
async def test_an_explicit_box_on_an_upload_is_cropped_into_this_conversation(
    tmp_path: Path,
) -> None:
    vl = _VL(answer=lambda _ref: "42")
    up = _upload(tmp_path, vl)

    result = await up.tool.call(
        {"image_ref": up.ref, "question": "?", "region": [0, 0, 250, 500]}, ctx=up.ctx
    )

    assert result.content == "42"
    sent = _image_ref_of(vl.calls[0])
    assert f"/agents/{_KEY}/threads/{_CONV}/work/.tool_results/{up.ctx.run_id}/crops/" in sent
    assert sent.endswith("/0-0-250-500.jpg")
    assert is_cacheable_image_ref(sent)
    on_disk = up.root / str(up.ctx.tenant_id) / str(up.ctx.user_id) / sent.split("/", 5)[5]
    assert _decode(on_disk.read_bytes()).size == (600, 600)  # 原像素,不放大
    assert result.meta["image_ref"] == up.ref
    assert result.meta["crop"] == {"dpi": None, "pixels": [600, 600], "reused": False}


@pytest.mark.anyio
async def test_an_upload_crop_is_reused_on_the_same_box(tmp_path: Path) -> None:
    vl = _VL()
    up = _upload(tmp_path, vl)
    args = {"image_ref": up.ref, "question": "?", "region": [0, 0, 250, 500]}

    await up.tool.call(args, ctx=up.ctx)
    again = await up.tool.call(args, ctx=up.ctx)

    assert again.meta["crop"] == {"dpi": None, "pixels": [600, 600], "reused": True}


_BIG = (3000, 2000)  # 600 万像素 -> 4 条横条
_FOUR = auto_bands(4)


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("size", "gated"),
    [((1999, 2000), True), ((2000, 2000), False)],
    ids=["just-under-4MP", "exactly-4MP"],
)
async def test_the_auto_gate_is_four_megapixels(
    tmp_path: Path, size: tuple[int, int], gated: bool
) -> None:
    vl = _VL(answer=lambda ref: NOT_FOUND if _is_band(ref) else "whole")
    up = _upload(tmp_path, vl, size=size)

    result = await up.tool.call(
        {"image_ref": up.ref, "question": "?", "region": "auto"}, ctx=up.ctx
    )

    assert (result.meta["auto"] == "gated") is gated
    assert len(vl.calls) == (1 if gated else 4)  # 3 条横条 + 整张
    assert result.meta["auto_pixels"] == size[0] * size[1]


@pytest.mark.anyio
async def test_auto_on_an_upload_with_failing_bands_names_them(tmp_path: Path) -> None:
    second = region_crop_stem(_FOUR[1].box)
    last = region_crop_stem(_FOUR[3].box)

    def answer(ref: str) -> str:
        if not _is_band(ref):
            return "whole: 7"
        if ref.endswith(f"/{last}.jpg"):
            raise RuntimeError("vendor 500")
        return _found("7") if ref.endswith(f"/{second}.jpg") else NOT_FOUND

    vl = _VL(answer=answer)
    up = _upload(tmp_path, vl, size=_BIG)

    result = await up.tool.call(
        {"image_ref": up.ref, "question": "?", "region": "auto"}, ctx=up.ctx
    )

    assert len(vl.calls) == 5
    assert result.content.startswith("7\n")
    assert "band 4 (RuntimeError)" in result.content
    by_band = {t["tile"]: t for t in result.meta["bands"]}
    assert by_band["band 4"]["error"] == "RuntimeError"
    assert by_band["band 2"]["dpi"] is None
    assert result.meta["auto"] == "bands"


@pytest.mark.anyio
async def test_a_band_conflict_returns_the_whole_image_answer_marked_uncertain(
    tmp_path: Path,
) -> None:
    first, third = region_crop_stem(_FOUR[0].box), region_crop_stem(_FOUR[2].box)

    def answer(ref: str) -> str:
        if not _is_band(ref):
            return "The total is 42."
        if ref.endswith(f"/{first}.jpg"):
            return _found("40", label="Total")
        return _found("42", label="Total") if ref.endswith(f"/{third}.jpg") else NOT_FOUND

    up = _upload(tmp_path, _VL(answer=answer), size=_BIG)
    result = await up.tool.call(
        {"image_ref": up.ref, "question": "total?", "region": "auto"}, ctx=up.ctx
    )

    assert result.content.startswith("The total is 42.\n\n")
    assert "whole-image reading; small print may be unreliable" in result.content
    assert f"- band 1 region={list(_FOUR[0].box)}" in result.content
    assert f"- band 3 region={list(_FOUR[2].box)}" in result.content
    assert (result.meta["auto"], result.meta["auto_reason"]) == ("full_fallback", "conflict")


@pytest.mark.anyio
async def test_a_partial_band_falls_back_to_the_whole_image_count(tmp_path: Path) -> None:
    """规则 4c(验证里那一次漏判):一条只数到自己那一截、另一条报被截断 —— 用整张读的答案。"""
    first, second = region_crop_stem(_FOUR[0].box), region_crop_stem(_FOUR[1].box)

    def answer(ref: str) -> str:
        if not _is_band(ref):
            return "There are 9 people."
        if ref.endswith(f"/{first}.jpg"):
            return _found("4")
        partial = json.dumps({"status": "partial", "label": "", "value": "", "unit": ""})
        return partial if ref.endswith(f"/{second}.jpg") else NOT_FOUND

    up = _upload(tmp_path, _VL(answer=answer), size=_BIG)
    result = await up.tool.call(
        {"image_ref": up.ref, "question": "how many people?", "region": "auto"}, ctx=up.ctx
    )

    assert result.content.startswith("There are 9 people.")
    assert (result.meta["auto"], result.meta["auto_reason"]) == ("full_fallback", "partial")


@pytest.mark.anyio
async def test_auto_with_every_call_failing_is_an_error_like_a_whole_look(
    tmp_path: Path,
) -> None:
    def answer(_ref: str) -> str:
        raise RuntimeError("vendor 500")

    up = _upload(tmp_path, _VL(answer=answer), size=_BIG)
    with pytest.raises(RuntimeError, match="vendor 500"):
        await up.tool.call({"image_ref": up.ref, "question": "?", "region": "auto"}, ctx=up.ctx)


@pytest.mark.anyio
async def test_auto_bands_that_time_out_are_reported_as_timed_out(tmp_path: Path) -> None:
    first = region_crop_stem(_FOUR[0].box)

    @dataclass
    class _SlowOnOne(_VL):
        async def __call__(
            self, *, messages: Sequence[BaseMessage], tools: Sequence[Any]
        ) -> AIMessage:
            if _image_ref_of(messages).endswith(f"/{first}.jpg"):
                await asyncio.sleep(5)
            return await super().__call__(messages=messages, tools=tools)

    vl = _SlowOnOne(answer=lambda ref: NOT_FOUND if _is_band(ref) else "Nothing like that.")
    up = _upload(tmp_path, vl, size=_BIG, timeout_s=0.2)

    result = await up.tool.call(
        {"image_ref": up.ref, "question": "?", "region": "auto"}, ctx=up.ctx
    )

    assert (result.meta["aggregate"], result.meta["auto"]) == ("none", "full_fallback")
    assert result.content.startswith("Nothing like that.")
    assert "band 1 (timed out)" in result.content
    assert "no band could see the complete answer (3 replied not_found)" in result.content


@pytest.mark.anyio
async def test_a_region_on_another_conversations_file_is_still_refused(tmp_path: Path) -> None:
    """安全检查对原图做:别的对话目录里的图,带不带 region 都拒,而且一张图都不裁。"""
    vl = _VL()
    up = _upload(tmp_path, vl)
    other = UUID("66666666-6666-6666-6666-666666666666")
    foreign = workspace_figure_ref(
        up.ctx.tenant_id,  # type: ignore[arg-type]
        up.ctx.user_id,  # type: ignore[arg-type]
        f"agents/{_KEY}/threads/{other}/work/photo.png",
    )

    for region in ([0, 0, 500, 500], "auto"):
        with pytest.raises(ToolBlockedError, match="another conversation"):
            await up.tool.call(
                {"image_ref": foreign, "question": "?", "region": region}, ctx=up.ctx
            )

    assert vl.calls == []
    assert not list(up.root.rglob("crops"))


@pytest.mark.anyio
async def test_a_bad_region_is_refused_before_anything_is_looked_at(tmp_path: Path) -> None:
    vl = _VL()
    up = _upload(tmp_path, vl)
    with pytest.raises(ValueError, match="too small"):
        await up.tool.call(
            {"image_ref": up.ref, "question": "?", "region": [0, 0, 10, 10]}, ctx=up.ctx
        )
    assert vl.calls == []


@pytest.mark.anyio
async def test_region_is_not_accepted_without_a_workspace() -> None:
    tool = AskImageTool(
        vl_caller=_VL(),
        image_resolver=InMemoryImageResolver(),
    )
    with pytest.raises(ValueError, match="does not accept 'region'"):
        await tool.call(
            {"image_ref": "expert_work://image/x", "question": "?", "region": "auto"},
            ctx=ToolContext(tenant_id=uuid4()),
        )


# ---------------------------------------------------------------------------
# 结果提示(spec §4)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("size", "hinted"),
    [((2000, 2000), True), ((1999, 2000), False)],
    ids=["4MP", "under-4MP"],
)
async def test_a_whole_image_quick_look_on_a_large_upload_suggests_auto(
    tmp_path: Path, size: tuple[int, int], hinted: bool
) -> None:
    up = _upload(tmp_path, _VL(answer=lambda _ref: "a lab report"), size=size)
    result = await up.tool.call({"image_ref": up.ref, "question": "?"}, ctx=up.ctx)
    assert (REGION_HINT in result.content) is hinted
    assert result.content.startswith("a lab report")


@pytest.mark.anyio
async def test_a_deep_look_gets_no_hint(tmp_path: Path) -> None:
    up = _upload(tmp_path, _VL())
    result = await up.tool.call({"image_ref": up.ref, "question": "?", "depth": "deep"}, ctx=up.ctx)
    assert REGION_HINT not in result.content


@pytest.mark.anyio
async def test_a_quick_look_at_a_document_page_suggests_auto(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_office_stubs(tmp_path, monkeypatch, pdftoppm_body=_PDFTOPPM_PAGE_OR_REGION)
    doc = _doc(tmp_path)
    tool, _read_page, _runtime = _tools(doc, _VL(answer=lambda _ref: "a table"))
    result = await tool.call({"path": doc.path, "unit": 1, "question": "?"}, ctx=doc.ctx)
    assert result.content == f"a table\n\n{REGION_HINT}"


@pytest.mark.anyio
async def test_no_hint_where_region_is_not_offered() -> None:
    ref = ImageRef(tenant_id=uuid4(), thread_id=_CONV, image_id=uuid4(), ext=".png").to_uri()
    big = ResolvedImage(media_type="image/png", data=_image_bytes((3000, 2000), fmt="PNG"))
    tool = AskImageTool(vl_caller=_VL(), image_resolver=InMemoryImageResolver(images={ref: big}))
    result = await tool.call(
        {"image_ref": ref, "question": "?"},
        ctx=ToolContext(tenant_id=parse_image_ref(ref).tenant_id),
    )
    assert REGION_HINT not in result.content


# ---------------------------------------------------------------------------
# 工具描述
# ---------------------------------------------------------------------------


def test_ask_image_offers_region_with_the_order_of_use() -> None:
    spec = AskImageTool(
        vl_caller=_VL(),
        image_resolver=InMemoryImageResolver(),
        workspace_store=NasWorkspaceStore(root="/nonexistent"),
    ).spec
    region = spec.parameters["properties"]["region"]
    kinds = [option["type"] for option in region["anyOf"]]
    assert kinds == ["string", "array"]
    assert region["anyOf"][0]["enum"] == ["auto"]
    assert region["anyOf"][1]["minItems"] == region["anyOf"][1]["maxItems"] == 4
    description = spec.description
    assert "Looking at the whole image is the default." in description
    assert (
        "Use region='auto' only to read one specific small printed value, name or date in "
        "a large, dense page or photo"
    ) in description
    assert (
        "never use it for counting, comparisons, trends, 'which is highest' or any question "
        "about the whole picture"
    ) in description
    assert "re-ask with an explicit region or tell the user the value is uncertain" in description
    assert description.index("region='auto'") < description.index("depth='deep' only when")
    assert "0-1000" in description
    for word in ("lab", "report", "glucose", "patient", "health", "medical"):
        assert word not in description.lower()
    assert "not for counting, comparisons" in region["description"]


def test_the_hint_matches_the_description() -> None:
    assert "one specific small printed value, name or date" in REGION_HINT
    assert "region='auto'" in REGION_HINT
    assert "do not use it for counting, comparisons" in REGION_HINT


@pytest.mark.parametrize(
    ("delivery", "resolver", "offered"),
    [("inline", True, True), ("inline", False, False), ("ask_image", True, False)],
)
def test_read_page_offers_region_only_to_a_model_that_sees_images(
    delivery: str, resolver: bool, offered: bool
) -> None:
    spec = ReadPageTool(
        client=RecordingSandboxRuntime(),
        figure_delivery=delivery,  # type: ignore[arg-type]
        image_resolver=InMemoryImageResolver() if resolver else None,
    ).spec
    assert ("region" in spec.parameters["properties"]) is offered
    assert ("'region'" in spec.description) is offered


def test_both_snippets_share_one_render_hash() -> None:
    """渲局部核对的内容哈希必须与渲整页逐字同一个函数。"""
    start = _RENDER_MAIN.index("def _render_sha(full):")
    body = _RENDER_MAIN[start : _RENDER_MAIN.index("def _fresh_dir(path):")]
    assert body in _REGION_MAIN


def test_the_region_render_budget_fits_under_the_exec_timeout_cap() -> None:
    """最坏情形:重转一次 PDF + 8 条横条逐条渲满各自的超时,仍在沙箱 300 s 的兜底之下。"""
    assert _CONVERT_TIMEOUT_S + AUTO_MAX_BANDS * _REGION_RENDER_TIMEOUT_S < 300


# ---------------------------------------------------------------------------
# Path A:read_page(region=…) 把局部内联给主模型
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_read_page_region_puts_the_crop_in_viewed_figures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_office_stubs(tmp_path, monkeypatch, pdftoppm_body=_PDFTOPPM_PAGE_OR_REGION)
    doc = _doc(tmp_path)
    _tool, read_page, runtime = _tools(doc, _VL(), delivery="inline")

    result = await read_page.call(
        {"path": doc.path, "units": [1], "region": [100, 200, 600, 500]}, ctx=doc.ctx
    )

    (crop_ref,) = result.state_updates["viewed_figures"]
    crop = parse_rendered_figure_ref(crop_ref)
    assert crop is not None and crop.region == (100, 200, 600, 500)
    assert _crop_path(doc, crop_ref).exists()
    assert len(runtime.execs) == 2  # 整页一次、局部一次
    dpi = region_dpi(_A4_AT_100DPI, (100, 200, 600, 500), base_dpi=100)
    assert f"at {dpi} dpi" in result.content
    assert result.meta["region"] == [100, 200, 600, 500]

    block = figure_block_message(
        viewed=[crop_ref],
        documents=result.state_updates["figure_documents"],
        supports_vision=True,
        tenant_id=doc.tenant,
        user_id=doc.user,
        view=doc.ctx.workspace_view,
    )
    assert block is not None
    text = block.content[0]["text"]  # type: ignore[index]
    assert "第 1 页的局部 [100, 200, 600, 500]" in text
    assert block.content[1] == {"type": "image_ref", "ref": crop_ref}


def test_a_page_and_its_crop_take_separate_slots() -> None:
    tenant, user = uuid4(), uuid4()
    view = WorkspaceView(agent_key=_KEY)
    base = f"agents/{_KEY}/.tool_results/{_RUN}"
    page = workspace_figure_ref(tenant, user, f"{base}/figures/{_SHA_A}/{_SHA_B}/_u2/page-2.jpg")
    crop = workspace_figure_ref(
        tenant, user, f"{base}/crops/{_SHA_A}/{_SHA_B}/_u2/page-2-0-0-500-500-d200.jpg"
    )
    block = figure_block_message(
        viewed=[page, crop],
        documents={},
        supports_vision=True,
        tenant_id=tenant,
        user_id=user,
        view=view,
    )
    assert block is not None
    assert list(block.content[1:]) == [
        {"type": "image_ref", "ref": page},
        {"type": "image_ref", "ref": crop},
    ]


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("args", "needle"),
    [
        ({"units": [1, 2], "region": [0, 0, 500, 500]}, "exactly one unit"),
        ({"units": [1], "region": "auto"}, "four integers"),
    ],
)
async def test_read_page_region_argument_checks(args: dict[str, Any], needle: str) -> None:
    tool = ReadPageTool(
        client=RecordingSandboxRuntime(),
        figure_delivery="inline",
        image_resolver=InMemoryImageResolver(),
    )
    ctx = ToolContext(tenant_id=uuid4(), run_id=uuid4(), user_id=uuid4())
    try:
        result = await tool.call({"path": "d.pdf", **args}, ctx=ctx)
    except ValueError as exc:
        assert needle in str(exc)
    else:
        assert needle in str(result.content)


@pytest.mark.anyio
async def test_read_page_region_is_refused_when_the_model_cannot_see_images() -> None:
    runtime = RecordingSandboxRuntime()
    tool = ReadPageTool(
        client=runtime, figure_delivery="ask_image", image_resolver=InMemoryImageResolver()
    )
    result = await tool.call(
        {"path": "d.pdf", "units": [1], "region": [0, 0, 500, 500]},
        ctx=ToolContext(tenant_id=uuid4(), run_id=uuid4(), user_id=uuid4()),
    )
    assert "use ask_image" in str(result.content)
    assert runtime.execs == []
