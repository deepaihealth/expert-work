"""B-332 —— ``ask_image`` 宿主裁图的内存有界:非 JPEG(PNG / WebP / GIF)解码前估峰值、超预算拒绝,
裁图本身不再为整张图叠几份拷贝。

钉住的事(设计 ``docs/superpowers/specs/2026-11-21-b332-bounded-png-crop.md``):

* 整张框、与不用缩的框:和 origin/main 的 ``crop_image`` 逐字节相同(进程内比对
  ``_b332_main_region_zoom``,那是 main 上该文件的原样拷贝;同一进程里 JPEG 编码字节相同,
  不依赖平台的哈希);
* 局部框、要缩:RGB / 灰度走一步 ``resize(box=)``,尺寸与方向不变、像素差有界;
  别的颜色模式仍逐字节相同;
* 估算不小于新进程里量到的峰值;超预算在解码之前被拒、如实告诉模型、计入指标。
"""

from __future__ import annotations

import importlib.util
import io
import json
import struct
import subprocess
import sys
import zlib
from pathlib import Path
from types import ModuleType

import pytest
from PIL import Image, ImageChops, ImageDraw, ImageStat

from orchestrator import image_views
from orchestrator.image_views import FULL_COPY_PEAK_BYTES, crop_slots, slots_for
from orchestrator.tools import region_zoom, vision
from orchestrator.tools.error_classifier import GuidedToolError
from orchestrator.tools.region_zoom import (
    MAX_BAND_PIXELS,
    PEAK_SAFETY,
    auto_bands,
    crop_image,
    crop_peak_bytes,
)


def _load_main() -> ModuleType:
    path = Path(__file__).with_name("_b332_main_region_zoom.py")
    spec = importlib.util.spec_from_file_location("_b332_main_region_zoom", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["_b332_main_region_zoom"] = module
    spec.loader.exec_module(module)
    return module


main = _load_main()

_W, _H = 360, 240
#: 小到会缩的上限(默认 150 万像素在测试用的小图上永远不会缩)。
_SMALL_CAP = 12_000
_WHOLE = (0, 0, 1000, 1000)
#: 局部框:中间一块、贴左上(origin)、贴右下、整行、整列。
_PARTIAL = [
    (100, 200, 800, 900),
    (0, 0, 600, 1000),
    (400, 300, 1000, 1000),
    (0, 400, 1000, 900),
    (300, 0, 700, 1000),
]
_MODES = ["RGB", "RGBA", "L", "LA", "P", "PT", "1", "I;16"]


def _screenshot(size: tuple[int, int] = (_W, _H)) -> Image.Image:
    """像截图的合成图:白底、渐变条、几行字、几块色块(与白底、渐变、细线的组合)。"""
    width, height = size
    image = Image.new("RGB", size, (250, 250, 250))
    image.paste(Image.linear_gradient("L").resize((width, height // 6)).convert("RGB"), (0, 0))
    draw = ImageDraw.Draw(image)
    for row in range(height // 24):
        top = height // 5 + row * 20
        draw.text((10 + (row % 3) * 17, top), f"line {row} 0123456789 abcdef", fill=(10, 10, 10))
    draw.rectangle((width // 2, height // 3, width - 20, height - 20), fill=(200, 60, 40))
    draw.line((0, height - 2, width, 8), fill=(0, 0, 255), width=1)
    return image


def _make(mode: str) -> Image.Image:
    rgb = _screenshot()
    alpha = Image.linear_gradient("L").resize(rgb.size).rotate(90)
    if mode == "RGB":
        return rgb
    if mode == "RGBA":
        out = rgb.convert("RGBA")
        out.putalpha(alpha)
        return out
    if mode == "L":
        return rgb.convert("L")
    if mode == "LA":
        out = rgb.convert("LA")
        out.putalpha(alpha)
        return out
    if mode in ("P", "PT"):
        out = rgb.convert("P", palette=Image.Palette.ADAPTIVE)
        if mode == "PT":
            out.info["transparency"] = 3
        return out
    if mode == "1":
        return rgb.convert("L").convert("1")
    if mode == "I;16":
        return _gray16(rgb)
    raise AssertionError(mode)


def _gray16(rgb: Image.Image) -> Image.Image:
    gray = rgb.convert("L")
    raw = b"".join(struct.pack("<H", v * 257) for v in gray.tobytes())
    return Image.frombytes("I;16", gray.size, raw)


def _png(image: Image.Image, orientation: int = 1) -> bytes:
    buffer = io.BytesIO()
    exif = Image.Exif()
    if orientation != 1:
        exif[0x0112] = orientation
        image.save(buffer, format="PNG", exif=exif)
    else:
        image.save(buffer, format="PNG")
    return buffer.getvalue()


def _jpeg(mode: str, orientation: int = 1) -> bytes:
    image = _screenshot().convert(mode)
    exif = Image.Exif()
    exif[0x0112] = orientation
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=90, exif=exif)
    return buffer.getvalue()


def _plain(pieces: list[object]) -> list[tuple[bytes, int, int]]:
    """``CroppedImage``(两份模块里各有一个类,不能直接比)→ (字节, 宽, 高)。"""
    return [(p.data, p.width, p.height) for p in pieces]  # type: ignore[attr-defined]


def _decode(data: bytes) -> Image.Image:
    return Image.open(io.BytesIO(data)).convert("RGB")


def _diff(a: bytes, b: bytes) -> tuple[float, int]:
    delta = ImageChops.difference(_decode(a), _decode(b))
    mean = sum(ImageStat.Stat(delta).mean) / 3
    return mean, max(hi for _, hi in delta.getextrema())


# ---------------------------------------------------------------------------
# 逐字节与 main 相同
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", _MODES)
@pytest.mark.parametrize("orientation", range(1, 9))
def test_whole_frame_is_byte_identical_to_main(mode: str, orientation: int) -> None:
    """整张框(要缩的、不用缩的)与 main 逐字节相同:8 种方向标记 x 4 个角度 x 各颜色模式。"""
    if mode in ("LA", "I;16") and orientation != 1:
        pytest.skip("PNG 里的 LA / I;16 不带方向标记也覆盖了颜色模式")
    data = _png(_make(mode), orientation)
    for degrees in (0, 90, 180, 270):
        for cap in (_SMALL_CAP, region_zoom.REGION_TARGET_PIXELS):
            ours = crop_image(data, [_WHOLE], degrees, cap)
            theirs = main.crop_image(data, [_WHOLE], degrees, cap)
            assert _plain(ours) == _plain(theirs), (mode, orientation, degrees, cap)


@pytest.mark.parametrize("mode", _MODES)
def test_partial_box_is_byte_identical_to_main_except_rgb_and_gray_downscale(mode: str) -> None:
    """局部框:不用缩的,与 main 逐字节相同;要缩的,除 RGB / 灰度走 ``resize(box=)`` 外也逐字节相同
    (带透明、调色板、1 位、16 位仍是「分条转 RGB、分条横向缩」,结果与先裁再缩一样)。"""
    for orientation in (1, 6, 3):
        data = _png(_make(mode), orientation) if mode not in ("LA", "I;16") else _png(_make(mode))
        for degrees in (0, 90, 270):
            for box in _PARTIAL:
                for cap in (_SMALL_CAP, region_zoom.REGION_TARGET_PIXELS):
                    ours = crop_image(data, [box], degrees, cap)[0]
                    theirs = main.crop_image(data, [box], degrees, cap)[0]
                    assert (ours.width, ours.height) == (theirs.width, theirs.height)
                    scaled = (theirs.width, theirs.height) != _unscaled(data, box, degrees)
                    if mode in ("RGB", "L") and scaled:
                        continue
                    assert ours.data == theirs.data, (mode, orientation, degrees, box, cap)


def _unscaled(data: bytes, box: region_zoom.Box, degrees: int) -> tuple[int, int]:
    """这个框不缩时的像素尺寸(转正之后)。"""
    size = region_zoom.image_size(data, degrees)
    assert size is not None
    left, top, right, bottom = region_zoom.pixel_box(box, size)
    return right - left, bottom - top


@pytest.mark.parametrize("mode", ["L", "RGB", "CMYK"])
@pytest.mark.parametrize("orientation", range(1, 9))
def test_jpeg_candidate_path_is_byte_identical_to_main(mode: str, orientation: int) -> None:
    """B-322 的问方向候选 / 小副本(``low_res_copy`` -> 整张框的 ``crop_image``,JPEG):与 main
    逐字节相同。"""
    data = _jpeg(mode, orientation)
    for degrees in (0, 90, 180, 270):
        assert (
            image_views.low_res_copy(data, degrees)[1]
            == main.crop_image(data, [_WHOLE], degrees)[0].data
        )
        assert _plain(crop_image(data, [_WHOLE], degrees, _SMALL_CAP)) == _plain(
            main.crop_image(data, [_WHOLE], degrees, _SMALL_CAP)
        )


@pytest.mark.parametrize("mode", ["L", "RGB", "CMYK"])
def test_jpeg_estimate_and_slots_are_unchanged_from_main(mode: str) -> None:
    sizes = [(640, 480), (4032, 3024), (5184, 3888), (8064, 6048), (9487, 6000), (7001, 5003)]
    for width, height in sizes:
        assert crop_peak_bytes(width, height, mode) == main.crop_peak_bytes(width, height, mode)


# ---------------------------------------------------------------------------
# 局部框、要缩:几何相同,像素差有界
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["RGB", "L"])
@pytest.mark.parametrize("orientation", [1, 3, 6, 8])
def test_partial_downscale_keeps_geometry_with_bounded_pixel_difference(
    mode: str, orientation: int
) -> None:
    image = _screenshot((1600, 1100))
    data = _png(image.convert(mode), orientation)
    for degrees in (0, 90):
        for box in _PARTIAL:
            ours = crop_image(data, [box], degrees, 90_000)[0]
            theirs = main.crop_image(data, [box], degrees, 90_000)[0]
            assert (ours.width, ours.height) == (theirs.width, theirs.height)
            mean, worst = _diff(ours.data, theirs.data)
            assert mean < 0.5, (mode, orientation, degrees, box, mean)
            assert worst <= 32, (mode, orientation, degrees, box, worst)


def test_seam_band_union_box_matches_main_within_bound() -> None:
    """B-301 接缝重看:框是相邻两条横条的并集,按横条分辨率(``MAX_BAND_PIXELS``)裁。"""
    image = _screenshot((1900, 1700))  # 约 323 万像素:并集要缩到 300 万以内
    data = _png(image)
    bands = auto_bands(4)
    union = (0, bands[1].box[1], 1000, bands[2].box[3])
    ours = crop_image(data, [union], 0, MAX_BAND_PIXELS)[0]
    theirs = main.crop_image(data, [union], 0, MAX_BAND_PIXELS)[0]
    full = _unscaled(data, union, 0)
    assert (theirs.width, theirs.height) != full or full[0] * full[1] <= MAX_BAND_PIXELS
    assert (ours.width, ours.height) == (theirs.width, theirs.height)
    mean, worst = _diff(ours.data, theirs.data)
    assert mean < 0.5
    assert worst <= 32
    # 小一点的上限逼出真正的缩小,并集框同样有界。
    small_cap = 400_000
    ours = crop_image(data, [union], 0, small_cap)[0]
    theirs = main.crop_image(data, [union], 0, small_cap)[0]
    assert (ours.width, ours.height) == (theirs.width, theirs.height)
    assert ours.width * ours.height <= small_cap
    mean, worst = _diff(ours.data, theirs.data)
    assert mean < 0.5
    assert worst <= 32


# ---------------------------------------------------------------------------
# 透明铺白底
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["RGBA", "LA", "PT"])
def test_transparent_pixels_are_flattened_onto_white(mode: str) -> None:
    transparent = Image.new("RGBA", (400, 300), (0, 0, 0, 0))
    ImageDraw.Draw(transparent).rectangle((0, 0, 199, 299), fill=(255, 0, 0, 255))
    if mode == "RGBA":
        source: Image.Image = transparent
    elif mode == "LA":
        source = transparent.convert("LA")
    else:
        source = transparent.convert("RGB").convert("P", palette=Image.Palette.ADAPTIVE)
        solid = source.getpixel((0, 0))
        source.info["transparency"] = source.getpixel((399, 0))
        assert solid != source.info["transparency"]
    data = _png(source)
    for cap in (_SMALL_CAP, region_zoom.REGION_TARGET_PIXELS):
        ours = crop_image(data, [_WHOLE], 0, cap)[0]
        assert _plain([ours]) == _plain(main.crop_image(data, [_WHOLE], 0, cap))
        pixels = _decode(ours.data)
        assert min(pixels.getpixel((pixels.width - 3, pixels.height // 2))) >= 250  # 透明处是白


# ---------------------------------------------------------------------------
# 估算、名额、拒绝
# ---------------------------------------------------------------------------


def _fake_png(width: int, height: int, color_type: int = 2, bit_depth: int = 8) -> bytes:
    """只有文件头的 PNG(``Image.open`` 读到 IDAT 就停,不解码):用来在不真分配几百 MB 的前提下
    测「解码之前」的判断。解码它会失败 —— 所以也钉住了「没解码」。"""

    def chunk(kind: bytes, body: bytes) -> bytes:
        crc = zlib.crc32(kind + body) & 0xFFFFFFFF
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", crc)

    header = struct.pack(">IIBBBBB", width, height, bit_depth, color_type, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(b""))
        + chunk(b"IEND", b"")
    )


def test_slots_for_png_sizes() -> None:
    def png_slots(w: int, h: int, color_type: int) -> int | None:
        return crop_slots(_fake_png(w, h, color_type), 0, region_zoom.REGION_TARGET_PIXELS)

    assert png_slots(2000, 1500, 2) == 1  # 3 MP RGB
    assert png_slots(4000, 3000, 2) == 2  # 12 MP RGB
    assert png_slots(4000, 3000, 6) == 2  # 12 MP RGBA
    assert png_slots(8000, 6000, 2) is None  # 48 MP RGB
    assert png_slots(8000, 6000, 6) is None
    assert png_slots(8000, 6000, 3) is None  # 48 MP 调色板:仍按估算拒
    assert png_slots(4000, 3000, 0) == 1  # 12 MP 灰度
    assert png_slots(2000, 1500, 0) == 1
    assert png_slots(8000, 6000, 0) is None  # 48 MP 灰度


def test_rotation_and_exif_raise_the_png_estimate() -> None:
    upright = crop_peak_bytes(4000, 3000, "RGB", draftable=False)
    rotated = crop_peak_bytes(4000, 3000, "RGB", draftable=False, rotated=True)
    assert rotated > upright
    assert crop_slots(_fake_png(4000, 3000), 0, region_zoom.REGION_TARGET_PIXELS) == 2
    assert crop_slots(_fake_png(4000, 3000), 90, region_zoom.REGION_TARGET_PIXELS) is None


def test_jpeg_crop_keeps_one_slot_whatever_the_size() -> None:
    data = _jpeg("RGB")
    assert crop_slots(data, 0, region_zoom.REGION_TARGET_PIXELS) == 1
    assert crop_slots(b"not an image", 0, region_zoom.REGION_TARGET_PIXELS) == 1


def test_slots_for_defaults_are_unchanged() -> None:
    assert [slots_for(4032, 3024, m) for m in ("L", "RGB", "CMYK")] == [1, 1, 1]
    assert slots_for(9487, 6000, "RGB") is None
    assert FULL_COPY_PEAK_BYTES == 110 * 1024 * 1024


@pytest.mark.anyio
async def test_over_budget_png_is_refused_before_decoding(monkeypatch: pytest.MonkeyPatch) -> None:
    def must_not_run(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("crop_image must not run for an over-budget image")

    monkeypatch.setattr(vision, "crop_image", must_not_run)
    before = _refused()
    with pytest.raises(GuidedToolError) as raised:
        await vision._crop_in_thread(_fake_png(8000, 6000), [_WHOLE], 0)
    assert "too large to crop at its original resolution" in str(raised.value)
    assert "Nothing was cropped" in str(raised.value)
    assert "without region" in raised.value.advice
    assert _refused() == before + 1


def _refused() -> float:
    return vision.ask_image_crop_refused_total.labels(reason="peak_over_budget")._value.get()


@pytest.mark.anyio
async def test_crop_takes_the_slots_the_estimate_says(monkeypatch: pytest.MonkeyPatch) -> None:
    taken: list[int] = []

    async def fake_slots(slots: int, call: object) -> list[object]:
        taken.append(slots)
        return []

    async def fake_slot(*_args: object, **_kwargs: object) -> list[object]:
        taken.append(1)
        return []

    monkeypatch.setattr(vision, "in_host_crop_slots", fake_slots)
    monkeypatch.setattr(vision, "in_host_crop_slot", fake_slot)
    await vision._crop_in_thread(_fake_png(2000, 1500), [_WHOLE], 0)
    await vision._crop_in_thread(_fake_png(4000, 3000), [_WHOLE], 0)
    await vision._crop_in_thread(_jpeg("RGB"), [_WHOLE], 0)
    assert taken == [1, 2, 1]


@pytest.mark.anyio
async def test_real_png_crop_through_the_gate_matches_direct_call() -> None:
    data = _png(_make("RGBA"))
    pieces = await vision._crop_in_thread(data, [_WHOLE], 0, _SMALL_CAP)
    assert pieces == crop_image(data, [_WHOLE], 0, _SMALL_CAP)


# ---------------------------------------------------------------------------
# 现量峰值对估算(新进程,与 test_b322_peak_measured 同一方法)
# ---------------------------------------------------------------------------

_MEASURE = """
import gc, json, resource, sys
path, degrees = sys.argv[1], int(sys.argv[2])
data = open(path, "rb").read()
from orchestrator.tools.region_zoom import crop_image
# 先用一张小图把编解码器、延迟导入走一遍:量的是这一次解码本身(生产进程早就热了)。
crop_image(open(sys.argv[3], "rb").read(), [(0, 0, 1000, 1000)], degrees)
gc.collect()
base = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
crop_image(data, [(0, 0, 1000, 1000)], degrees)
grown = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss - base
print(json.dumps(grown * (1 if sys.platform == "darwin" else 1024)))
"""

_MP_SIZES = {"6MP": (3000, 2000), "12MP": (4000, 3000), "24MP": (6000, 4000)}


def _flat_png(size: tuple[int, int], mode: str) -> bytes:
    """纯色底 + 几块字的合成图(压得小、生成得快)。"""
    image = Image.new("RGB", size, (250, 250, 250))
    draw = ImageDraw.Draw(image)
    for top in range(10, size[1] - 20, 60):
        draw.text((10, top), "measured peak 0123456789", fill=(0, 0, 0))
    if mode == "RGBA":
        image = image.convert("RGBA")
    elif mode == "L":
        image = image.convert("L")
    elif mode == "P":
        image = image.convert("P", palette=Image.Palette.ADAPTIVE)
    return _png(image)


@pytest.mark.parametrize("degrees", [0, 90])
@pytest.mark.parametrize("mode", ["RGB", "RGBA", "L", "P"])
@pytest.mark.parametrize("name", list(_MP_SIZES))
def test_measured_png_peak_stays_within_the_estimate(
    tmp_path: Path, name: str, mode: str, degrees: int
) -> None:
    size = _MP_SIZES[name]
    path = tmp_path / f"{name}-{mode}.png"
    path.write_bytes(_flat_png(size, mode))
    warm = tmp_path / f"warm-{mode}.png"
    warm.write_bytes(_flat_png((64, 48), mode))
    out = subprocess.run(  # noqa: S603 - 固定的解释器与脚本
        [sys.executable, "-c", _MEASURE, str(path), str(degrees), str(warm)],
        check=True,
        capture_output=True,
        text=True,
        timeout=180,
    )
    real = int(json.loads(out.stdout.strip().splitlines()[-1]))
    estimate = crop_peak_bytes(*size, mode, draftable=False, rotated=degrees != 0)
    mib = 1024 * 1024
    assert real <= estimate, (real / mib, estimate / mib)
    # 估算在乘安全系数之前就不低于实测(校准的方法)。
    assert real <= estimate / PEAK_SAFETY, (real / mib, estimate / mib)
    if estimate <= FULL_COPY_PEAK_BYTES:
        assert real <= FULL_COPY_PEAK_BYTES


def _solid(size: tuple[int, int], fmt: str, mode: str = "RGB") -> bytes:
    buffer = io.BytesIO()
    kwargs = {"lossless": True} if fmt == "WEBP" else {}
    Image.new(mode, size, 200 if mode == "P" else (200,) * len(mode)).save(
        buffer, format=fmt, **kwargs
    )
    return buffer.getvalue()


def test_webp_costs_more_than_png_because_the_decoder_keeps_extra_copies() -> None:
    cap = region_zoom.REGION_TARGET_PIXELS
    assert crop_slots(_solid((4000, 3000), "PNG"), 0, cap) == 2
    assert crop_slots(_solid((4000, 3000), "WEBP"), 0, cap) is None
    assert crop_slots(_solid((2000, 1500), "WEBP"), 0, cap) == 2
    # GIF 是调色板、1 字节一像素,与 PNG 的 P 同一档。
    assert crop_slots(_solid((2000, 1500), "GIF", "P"), 0, cap) == 1
    assert crop_slots(_solid((4000, 3000), "GIF", "P"), 0, cap) == 2


def test_measured_webp_peak_stays_within_the_estimate(tmp_path: Path) -> None:
    size = (3000, 2000)
    path = tmp_path / "wide.webp"
    path.write_bytes(_solid(size, "WEBP"))
    warm = tmp_path / "warm.webp"
    warm.write_bytes(_solid((64, 48), "WEBP"))
    out = subprocess.run(  # noqa: S603 - 固定的解释器与脚本
        [sys.executable, "-c", _MEASURE, str(path), "0", str(warm)],
        check=True,
        capture_output=True,
        text=True,
        timeout=180,
    )
    real = int(json.loads(out.stdout.strip().splitlines()[-1]))
    estimate = crop_peak_bytes(
        *size,
        "RGB",
        draftable=False,
        decoder_bytes_per_pixel=region_zoom.WEBP_DECODE_BYTES_PER_PIXEL,
    )
    assert real <= estimate / PEAK_SAFETY, (real, estimate)
