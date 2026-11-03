"""B-266 review 回修:宿主裁图省内存 / 限并发 / 只认几种格式、上传图只认本对话、切块调用限流,
以及片段与宿主校验那几道闸的回归钉。夹具复用 ``test_b266_region_zoom``。"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import io
import json
import os
import random
import threading
import time
import weakref
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from langchain_core.messages import AIMessage, BaseMessage
from PIL import Image, ImageChops, ImageOps, JpegImagePlugin

from expert_work.persistence import region_crop_stem
from expert_work.protocol.multimodal import ImageRef
from orchestrator.multimodal import InMemoryImageResolver, ResolvedImage, parse_rendered_figure_ref
from orchestrator.tools import region_zoom, vision
from orchestrator.tools.error_classifier import classify_tool_error
from orchestrator.tools.nas_workspace_store import NasWorkspaceStore
from orchestrator.tools.read_page import (
    ReadPageTool,
    RegionRenderError,
    RegionStaleError,
    _figure_pdf_rel,
    build_region_wrapper,
    document_sha,
    workspace_figure_ref,
)
from orchestrator.tools.region_zoom import (
    REGION_TARGET_PIXELS,
    auto_bands,
    crop_image,
    crop_upright,
    image_size,
    pixel_box,
)
from orchestrator.tools.registry import ToolBlockedError, ToolContext
from orchestrator.tools.sandbox import RecordingSandboxRuntime, SandboxOutcome

from .test_b266_region_zoom import (
    _A4_AT_100DPI,
    _CONV,
    _KEY,
    _PDFTOPPM_PAGE_OR_REGION,
    _VL,
    _calls,
    _doc,
    _image_bytes,
    _image_ref_of,
    _tools,
    _upload,
)
from .test_read_page import _install_office_stubs, _snippet_params

# ---------------------------------------------------------------------------
# M1 —— 宿主裁图:只认几种格式、不转整张图、JPEG 缩小解码、限并发
# ---------------------------------------------------------------------------


def _gradient(size: tuple[int, int]) -> Image.Image:
    image = Image.new("RGB", size)
    width, height = size
    image.putdata([(i % 251, (i * 7) % 253, (i * 13) % 255) for i in range(width * height)])
    return image


@pytest.mark.parametrize("orientation", range(1, 9))
def test_crop_upright_matches_transposing_the_whole_image(orientation: int) -> None:
    """只转裁下来的那一块,与「整张转正再裁」逐像素相同 —— 8 种方向、200 个随机框。"""
    raw = _gradient((37, 23))
    tagged = raw.copy()
    exif = tagged.getexif()
    exif[0x0112] = orientation
    tagged.info["exif"] = exif.tobytes()
    upright = ImageOps.exif_transpose(tagged)
    rng = random.Random(orientation)  # noqa: S311 —— 测试数据
    for _ in range(200):
        x1, y1 = rng.randint(0, 990), rng.randint(0, 990)
        box = (x1, y1, rng.randint(x1 + 1, 1000), rng.randint(y1 + 1, 1000))
        ours = crop_upright(raw, box, orientation)
        reference = upright.crop(pixel_box(box, upright.size))
        assert ours.size == reference.size, box
        assert ImageChops.difference(ours, reference).getbbox() is None, box


def test_crop_never_converts_or_transposes_the_full_image(monkeypatch: pytest.MonkeyPatch) -> None:
    """一张带方向标记、带透明的大 PNG:转 RGB / 铺白底 / 转正都只发生在裁下来的那一块上。"""
    size = (1600, 1200)
    image = Image.new("RGBA", size, (0, 0, 255, 128))
    exif = image.getexif()
    exif[0x0112] = 6
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", exif=exif.tobytes())
    full_sizes = {size, (size[1], size[0])}
    seen: list[tuple[str, tuple[int, int]]] = []
    convert, transpose = Image.Image.convert, Image.Image.transpose

    def spy_convert(self: Image.Image, *args: Any, **kwargs: Any) -> Image.Image:
        seen.append(("convert", self.size))
        return convert(self, *args, **kwargs)

    def spy_transpose(self: Image.Image, *args: Any, **kwargs: Any) -> Image.Image:
        seen.append(("transpose", self.size))
        return transpose(self, *args, **kwargs)

    monkeypatch.setattr(Image.Image, "convert", spy_convert)
    monkeypatch.setattr(Image.Image, "transpose", spy_transpose)

    (piece,) = crop_image(buffer.getvalue(), [(0, 0, 500, 500)])

    assert (piece.width, piece.height) == (600, 800)
    assert {kind for kind, _ in seen} == {"convert", "transpose"}
    assert not [entry for entry in seen if entry[1] in full_sizes], seen


def test_a_large_jpeg_is_decoded_reduced_and_crops_stay_near_the_pixel_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """每块缩小后仍有 ≥1.5 百万像素才缩小解码;输出不超过约 1.5 百万像素,也从不放大。"""
    decoded: list[tuple[int, int]] = []
    load = JpegImagePlugin.JpegImageFile.load

    def spy_load(self: JpegImagePlugin.JpegImageFile) -> Any:
        decoding = bool(self.tile)
        result = load(self)
        if decoding:
            decoded.append(self.im.size)
        return result

    monkeypatch.setattr(JpegImagePlugin.JpegImageFile, "load", spy_load)
    data = _image_bytes((6000, 4000))

    big, small = crop_image(data, [(0, 0, 1000, 1000), (0, 0, 100, 100)])

    assert decoded == [(6000, 4000)]  # 小框缩一半就不够 1.5 百万像素了:不缩
    assert big.width * big.height <= REGION_TARGET_PIXELS
    assert big.width * big.height > 0.98 * REGION_TARGET_PIXELS
    assert (small.width, small.height) == (600, 400)  # 原像素,不放大

    decoded.clear()
    (whole,) = crop_image(data, [(0, 0, 1000, 1000)])
    # 1/4 解码正好 1.5 百万像素,还够;1/8 就不够了。
    assert decoded == [(1500, 1000)]
    assert (whole.width, whole.height) == (1500, 1000)


def test_a_multi_picture_jpeg_is_still_opened() -> None:
    """手机的 MPO 走 "JPEG" 那一项打开,不被格式白名单挡掉。"""
    first = Image.new("RGB", (200, 100), "red")
    buffer = io.BytesIO()
    first.save(buffer, format="MPO", save_all=True, append_images=[first.copy()])
    assert image_size(buffer.getvalue()) == (200, 100)
    (piece,) = crop_image(buffer.getvalue(), [(0, 0, 500, 1000)])
    assert (piece.width, piece.height) == (100, 100)


@pytest.mark.parametrize("fmt", ["BMP", "TIFF"])
def test_only_upload_formats_are_opened(fmt: str) -> None:
    buffer = io.BytesIO()
    Image.new("RGB", (64, 64)).save(buffer, format=fmt)
    assert image_size(buffer.getvalue()) is None
    with pytest.raises(region_zoom.RegionImageError, match="could not be decoded"):
        crop_image(buffer.getvalue(), [(0, 0, 500, 500)])


def _crop_tool(tmp_path: Path, images: dict[str, bytes]) -> tuple[vision.AskImageTool, ToolContext]:
    tenant, user = uuid4(), uuid4()
    root = tmp_path / "nas"
    (root / str(tenant) / str(user)).mkdir(parents=True)
    resolver = InMemoryImageResolver(
        images={
            ref: ResolvedImage(media_type="image/png", data=data) for ref, data in images.items()
        }
    )
    tool = vision.AskImageTool(
        vl_caller=_VL(), image_resolver=resolver, workspace_store=NasWorkspaceStore(root=str(root))
    )
    ctx = ToolContext(
        tenant_id=tenant,
        user_id=user,
        run_id=uuid4(),
        thread_id=_CONV,
        agent_key=_KEY,
        workspace_layout="conversation",
    )
    return tool, ctx


@dataclass
class _CropSpy:
    """替换 ``vision.crop_image``:记下同时在线程里跑的个数和总调用数。"""

    active: int = 0
    peak: int = 0
    calls: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)

    def __call__(self, data: bytes, boxes: Sequence[region_zoom.Box]) -> Any:
        with self.lock:
            self.active += 1
            self.calls += 1
            self.peak = max(self.peak, self.active)
        time.sleep(0.05)
        try:
            return crop_image(data, boxes)
        finally:
            with self.lock:
                self.active -= 1


@pytest.mark.anyio
async def test_at_most_two_host_crops_run_at_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """名额在进线程之前用 asyncio 拿:6 张不同的图同时裁,线程里同时最多 2 张,没有线程在里面等。"""
    spy = _CropSpy()
    monkeypatch.setattr(vision, "crop_image", spy)
    monkeypatch.setattr(vision, "_HOST_CROP_GATES", weakref.WeakKeyDictionary())
    images = {f"img-{i}": _image_bytes((200 + i, 200), fmt="PNG") for i in range(6)}
    tool, ctx = _crop_tool(tmp_path, images)

    await asyncio.gather(*(tool._crop_image(ref, [(0, 0, 500, 500)], ctx=ctx) for ref in images))

    assert spy.calls == 6
    assert spy.peak == region_zoom.HOST_CROP_SLOTS == 2


@pytest.mark.anyio
async def test_two_concurrent_crops_of_one_image_are_serialised_and_the_second_reuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """同一 run、同一张图、同一个框并发裁两次:按 (run, 裁图目录) 排队,只裁一次,第二次复用。"""
    spy = _CropSpy()
    monkeypatch.setattr(vision, "crop_image", spy)
    tool, ctx = _crop_tool(tmp_path, {"img": _image_bytes((400, 300), fmt="PNG")})

    first, second = await asyncio.gather(
        tool._crop_image("img", [(0, 0, 500, 500)], ctx=ctx),
        tool._crop_image("img", [(0, 0, 500, 500)], ctx=ctx),
    )

    assert spy.calls == 1
    assert sorted([first.crops[0].reused, second.crops[0].reused]) == [False, True]
    assert first.crops[0].ref == second.crops[0].ref


# ---------------------------------------------------------------------------
# M2 —— 上传图只认本对话(子代另认根对话)
# ---------------------------------------------------------------------------


def _upload_ref(tenant: UUID, thread: UUID) -> str:
    return ImageRef(tenant_id=tenant, thread_id=thread, image_id=uuid4(), ext=".png").to_uri()


@pytest.mark.anyio
@pytest.mark.parametrize("region", [None, [0, 0, 500, 500], "auto"])
async def test_an_upload_from_another_conversation_is_refused(
    tmp_path: Path, region: object
) -> None:
    vl = _VL()
    up = _upload(tmp_path, vl)
    assert up.ctx.tenant_id is not None
    foreign = _upload_ref(up.ctx.tenant_id, UUID("66666666-6666-6666-6666-666666666666"))
    args: dict[str, Any] = {"image_ref": foreign, "question": "?"}
    if region is not None:
        args["region"] = region

    with pytest.raises(ToolBlockedError, match="another conversation"):
        await up.tool.call(args, ctx=up.ctx)

    assert vl.calls == []
    assert not list(up.root.rglob("crops"))


@pytest.mark.anyio
async def test_a_child_may_look_at_its_root_conversations_upload() -> None:
    """委派出去的子代:``thread_id`` 是子线程,父这一轮的图属于根对话(``workspace_thread_id``)。"""
    tenant = uuid4()
    ref = _upload_ref(tenant, _CONV)
    resolver = InMemoryImageResolver(
        images={ref: ResolvedImage(media_type="image/png", data=_image_bytes((64, 64), fmt="PNG"))}
    )
    vl = _VL(answer=lambda _ref: "a cat")
    tool = vision.AskImageTool(vl_caller=vl, image_resolver=resolver)
    child = ToolContext(tenant_id=tenant, thread_id=uuid4(), workspace_thread_id=_CONV)

    result = await tool.call({"image_ref": ref, "question": "?"}, ctx=child)

    assert result.content == "a cat"
    other_child = ToolContext(tenant_id=tenant, thread_id=uuid4(), workspace_thread_id=uuid4())
    with pytest.raises(ToolBlockedError, match="another conversation"):
        await tool.call({"image_ref": ref, "question": "?"}, ctx=other_child)


# ---------------------------------------------------------------------------
# (e) —— 切块调用限流
# ---------------------------------------------------------------------------


@dataclass
class _CountingVL(_VL):
    in_flight: int = 0
    peak: int = 0
    #: 给了就把每个调用挡住,直到同时在飞的达到这个数才一起放行(最多等 10 s),峰值不再取决于
    #: 机器快慢:CI 上 0.05 s 的固定停顿不够让 15 个调用叠满 12 个名额。
    release_at: int | None = None
    _go: asyncio.Event | None = None

    async def __call__(self, *, messages: Sequence[BaseMessage], tools: Sequence[Any]) -> AIMessage:
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        try:
            if self.release_at is None:
                await asyncio.sleep(0.05)
            else:
                if self._go is None:
                    self._go = asyncio.Event()
                if self.in_flight >= self.release_at:
                    self._go.set()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._go.wait(), timeout=10)
            return await super().__call__(messages=messages, tools=tools)
        finally:
            self.in_flight -= 1


#: 600 万像素:4 条横条 + 1 次整张读 = 每次 5 个调用。
_BIG = (3000, 2000)
_FOUR = auto_bands(4)


@pytest.mark.anyio
async def test_the_whole_image_read_runs_in_parallel_with_the_bands(tmp_path: Path) -> None:
    vl = _CountingVL(answer=lambda _ref: "NOT_FOUND")
    up = _upload(tmp_path, vl, size=_BIG)

    result = await up.tool.call(
        {"image_ref": up.ref, "question": "?", "region": "auto"}, ctx=up.ctx
    )

    assert len(vl.calls) == 5
    assert vl.peak == 5  # 整张读与 4 条横条同时在飞
    assert result.meta["vl_calls"] == 5


@pytest.mark.anyio
async def test_parallel_auto_looks_keep_at_most_twelve_calls_in_flight(
    tmp_path: Path,
) -> None:
    """每次 N+1 个调用(4 条横条 + 整张读),3 次并行共 15 个:同时在飞的不超过 12 个。"""
    vl = _CountingVL(answer=lambda _ref: "NOT_FOUND", release_at=vision.MAX_TILE_CALLS_IN_FLIGHT)
    up = _upload(tmp_path, vl, size=_BIG)
    args = {"image_ref": up.ref, "question": "?", "region": "auto"}

    await asyncio.gather(*(up.tool.call(args, ctx=up.ctx) for _ in range(3)))

    assert len(vl.calls) == 15
    assert vl.peak == vision.MAX_TILE_CALLS_IN_FLIGHT == 12


@pytest.mark.anyio
async def test_a_tile_that_cannot_get_a_slot_in_time_counts_as_timed_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """一个名额、上限 0.3 s:整张读(先排)与第 1 条各占 0.2 s;第 1 条等 0.2 s 拿到名额,
    第 2 条起要等 0.4 s,等不到。"""
    monkeypatch.setattr(vision, "MAX_TILE_CALLS_IN_FLIGHT", 1)
    monkeypatch.setattr(vision, "_TILE_SLOTS", weakref.WeakKeyDictionary())
    first = region_crop_stem(_FOUR[0].box)

    @dataclass
    class _SlowFirstTwo(_VL):
        async def __call__(
            self, *, messages: Sequence[BaseMessage], tools: Sequence[Any]
        ) -> AIMessage:
            ref = _image_ref_of(messages)
            if "/crops/" not in ref or ref.endswith(f"/{first}.jpg"):
                await asyncio.sleep(0.2)
            return await super().__call__(messages=messages, tools=tools)

    vl = _SlowFirstTwo(answer=lambda _ref: "7")
    up = _upload(tmp_path, vl, size=_BIG, timeout_s=0.3)

    result = await up.tool.call(
        {"image_ref": up.ref, "question": "?", "region": "auto"}, ctx=up.ctx
    )

    bands = {t["tile"]: t for t in result.meta["bands"]}
    assert bands["band 1"]["value"] == "7"
    assert result.meta["full"] == {"answer": "7"}
    late = [bands[label] for label in ("band 2", "band 3", "band 4")]
    assert all(t["error"] == "timed out" for t in late)
    assert len(vl.calls) == 2


# ---------------------------------------------------------------------------
# 片段 / 宿主校验那几道闸
# ---------------------------------------------------------------------------


def _jpeg(size: tuple[int, int]) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, "white").save(buffer, format="JPEG")
    return buffer.getvalue()


_RUN = UUID("99999999-9999-4999-8999-999999999999")


def _page_ref(tenant: UUID, user: UUID, *, crop: bool = False) -> str:
    doc_sha = document_sha("d.pdf", view=ToolContext(agent_key=_KEY).workspace_view)
    tail = "crops" if crop else "figures"
    name = "page-1-0-0-500-500-d200.jpg" if crop else "page-1.jpg"
    rel = f"agents/{_KEY}/.tool_results/{_RUN}/{tail}/{doc_sha}/{'b' * 16}/_u1/{name}"
    return workspace_figure_ref(tenant, user, rel)


def _ctx(tenant: UUID, user: UUID) -> ToolContext:
    return ToolContext(tenant_id=tenant, user_id=user, run_id=uuid4(), agent_key=_KEY)


def _canned(reply: dict[str, Any]) -> RecordingSandboxRuntime:
    return RecordingSandboxRuntime(
        SandboxOutcome(stdout=json.dumps(reply), stderr="", exit_code=0, timed_out=False)
    )


@dataclass
class _EchoRuntime(RecordingSandboxRuntime):
    """回一个「渲好了」的包,``rel`` 由 ``forge(out_rel, name)`` 决定 —— 模拟被篡改的回包。"""

    forge: Any = None
    params: list[dict[str, Any]] = field(default_factory=list)

    async def exec(self, **kwargs: Any) -> SandboxOutcome:
        await super().exec(**kwargs)
        params = _snippet_params(str(kwargs["code"]))
        self.params.append(params)
        name = params["jobs"][0]["name"]
        reply = {"ok": True, "crops": [{"name": name, "rel": self.forge(params["out_rel"], name)}]}
        return SandboxOutcome(stdout=json.dumps(reply), stderr="", exit_code=0, timed_out=False)


def _region_tool(runtime: RecordingSandboxRuntime, page_ref: str) -> ReadPageTool:
    resolver = InMemoryImageResolver(
        images={page_ref: ResolvedImage(media_type="image/jpeg", data=_jpeg(_A4_AT_100DPI))}
    )
    return ReadPageTool(client=runtime, figure_delivery="inline", image_resolver=resolver)


@pytest.mark.anyio
async def test_an_honest_region_reply_is_accepted() -> None:
    tenant, user = uuid4(), uuid4()
    page = _page_ref(tenant, user)
    runtime = _EchoRuntime(forge=lambda out_rel, name: f"{out_rel}/{name}")
    rendered = await _region_tool(runtime, page).render_regions(
        path="d.pdf", figure_ref=page, boxes=[(0, 0, 500, 500)], ctx=_ctx(tenant, user)
    )
    crop = parse_rendered_figure_ref(rendered.crops[0].ref)
    assert crop is not None and crop.region == (0, 0, 500, 500)


@pytest.mark.anyio
@pytest.mark.parametrize(
    "forge",
    [
        lambda out_rel, name: f"{out_rel.replace('/crops/', '/figures/')}/{name}",
        lambda out_rel, name: f"{out_rel}/../../{name}",
        lambda out_rel, name: f"{out_rel}/evil.jpg",
        lambda out_rel, name: f"/{out_rel}/{name}",
        lambda out_rel, name: f"{out_rel}/page-1-0-0-1000-1000.jpg",
    ],
    ids=["outside-out-rel", "dot-dot", "not-crop-shaped", "absolute", "another-box"],
)
async def test_a_tampered_region_reply_is_rejected(forge: Any) -> None:
    tenant, user = uuid4(), uuid4()
    page = _page_ref(tenant, user)
    with pytest.raises(RegionRenderError, match="none of the regions"):
        await _region_tool(_EchoRuntime(forge=forge), page).render_regions(
            path="d.pdf", figure_ref=page, boxes=[(0, 0, 500, 500)], ctx=_ctx(tenant, user)
        )


@pytest.mark.anyio
async def test_zooming_into_a_crop_is_refused() -> None:
    tenant, user = uuid4(), uuid4()
    crop = _page_ref(tenant, user, crop=True)
    runtime = _canned({"ok": True, "crops": []})
    with pytest.raises(RegionRenderError, match="needs a rendered page"):
        await _region_tool(runtime, crop).render_regions(
            path="d.pdf", figure_ref=crop, boxes=[(0, 0, 500, 500)], ctx=_ctx(tenant, user)
        )
    assert runtime.execs == []


def test_figure_pdf_rel_without_an_agent_binding() -> None:
    """没绑 agent:ref 的 rel 就是用户根相对(没有 ``agents/<key>/`` 两段可剥)。"""
    tenant, user = uuid4(), uuid4()
    rel = f".tool_results/{_RUN}/figures/{'a' * 16}/{'b' * 16}/_u3/page-3.jpg"
    figure = parse_rendered_figure_ref(workspace_figure_ref(tenant, user, rel))
    assert figure is not None and figure.workspace.agent_key is None
    assert _figure_pdf_rel(figure) == f".tool_results/{_RUN}/figures/{'a' * 16}/{'b' * 16}/_pdf"

    bound = parse_rendered_figure_ref(workspace_figure_ref(tenant, user, f"agents/{_KEY}/{rel}"))
    assert bound is not None
    assert _figure_pdf_rel(bound) == _figure_pdf_rel(figure)


@pytest.mark.anyio
async def test_a_truncated_tile_is_reported_as_truncated(tmp_path: Path) -> None:
    truncated = region_crop_stem(_FOUR[3].box)

    @dataclass
    class _TruncatesOne(_VL):
        async def __call__(
            self, *, messages: Sequence[BaseMessage], tools: Sequence[Any]
        ) -> AIMessage:
            self.calls.append(list(messages))
            if _image_ref_of(messages).endswith(f"/{truncated}.jpg"):
                return AIMessage(content="", response_metadata={"finish_reason": "length"})
            return AIMessage(content="9")

    up = _upload(tmp_path, _TruncatesOne(), size=_BIG)
    result = await up.tool.call(
        {"image_ref": up.ref, "question": "?", "region": "auto"}, ctx=up.ctx
    )
    bands = {t["tile"]: t for t in result.meta["bands"]}
    assert bands["band 4"]["error"] == "truncated"
    assert "band 4 (truncated)" in result.content
    assert result.meta["aggregate"] == "single"


# ---------------------------------------------------------------------------
# 片段真跑:复用闸不认符号链接、复用刷新修改时间、失败不留半张
# ---------------------------------------------------------------------------


def _run_region(
    ws: Path,
    *,
    jobs: list[dict[str, Any]],
    render_sha: str,
    out_rel: str,
    render_timeout_s: int = 30,
) -> dict:
    code = build_region_wrapper(
        "d.pdf",
        page=1,
        render_sha=render_sha,
        jobs=jobs,
        out_rel=out_rel,
        ws=str(ws),
        render_timeout_s=render_timeout_s,
    )
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        exec(compile(code, "<snippet>", "exec"), {})  # noqa: S102
    return json.loads(buffer.getvalue())


def _region_setup(tmp_path: Path) -> tuple[Path, str, list[dict[str, Any]], Path]:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "d.pdf").write_text("PDF")
    render_sha = hashlib.sha256(b"dpi=100;PDF").hexdigest()[:16]
    jobs = [{"name": "page-1-0-0-500-500-d200.jpg", "dpi": 200, "x": 0, "y": 0, "w": 10, "h": 10}]
    return ws, render_sha, jobs, ws / "out" / jobs[0]["name"]


def test_the_reuse_check_does_not_follow_a_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = _install_office_stubs(tmp_path, monkeypatch, pdftoppm_body=_PDFTOPPM_PAGE_OR_REGION)
    ws, render_sha, jobs, target = _region_setup(tmp_path)
    elsewhere = tmp_path / "elsewhere.jpg"
    elsewhere.write_text("NOT A RENDER")
    target.parent.mkdir(parents=True)
    target.symlink_to(elsewhere)

    env = _run_region(ws, jobs=jobs, render_sha=render_sha, out_rel="out")

    assert env["crops"][0]["reused"] is False
    assert _calls(bin_dir, "pdftoppm") == 1
    assert not target.is_symlink()
    assert elsewhere.read_text() == "NOT A RENDER"


def test_reusing_a_crop_refreshes_its_mtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_office_stubs(tmp_path, monkeypatch, pdftoppm_body=_PDFTOPPM_PAGE_OR_REGION)
    ws, render_sha, jobs, target = _region_setup(tmp_path)
    _run_region(ws, jobs=jobs, render_sha=render_sha, out_rel="out")
    os.utime(target, (1_000, 1_000))

    env = _run_region(ws, jobs=jobs, render_sha=render_sha, out_rel="out")

    assert env["crops"][0]["reused"] is True
    assert target.stat().st_mtime > 1_000_000


def test_a_timed_out_render_leaves_no_partial_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """pdftoppm 写了半张之后超时被杀:``.part.jpg`` 不留下。"""
    _install_office_stubs(
        tmp_path,
        monkeypatch,
        pdftoppm_body='open(prefix + ".jpg", "w").write("partial")\nimport time\ntime.sleep(5)\n',
    )
    ws, render_sha, jobs, target = _region_setup(tmp_path)

    env = _run_region(ws, jobs=jobs, render_sha=render_sha, out_rel="out", render_timeout_s=1)

    assert env["ok"] is False
    assert env["failed"][0]["why"] == "TimeoutExpired"
    assert list((ws / "out").glob("*")) == []
    assert not target.exists()


# ---------------------------------------------------------------------------
# (f) —— 文档改过:给「先重新 read_page」的建议,不是只读工具默认的「可安全重试」
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_a_stale_region_does_not_get_the_generic_retry_advice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_office_stubs(tmp_path, monkeypatch, pdftoppm_body=_PDFTOPPM_PAGE_OR_REGION)
    doc = _doc(tmp_path)
    (doc.work / doc.path).write_text("CHANGED")
    os.utime(doc.work / doc.path, (1_000, 1_000))
    tool, _read_page, _runtime = _tools(doc, _VL())
    with pytest.raises(RegionStaleError) as info:
        await tool.call(
            {"path": doc.path, "unit": 1, "question": "?", "region": [0, 0, 500, 500]},
            ctx=doc.ctx,
        )
    classified = classify_tool_error(tool_name="ask_image", error=info.value, spec=tool.spec)
    assert classified.retryable is False
    assert "read_page" in classified.advice
