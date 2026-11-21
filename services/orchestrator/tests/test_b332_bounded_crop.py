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
from typing import Any

import pytest
from PIL import Image, ImageChops, ImageDraw, ImageStat

from orchestrator import image_views
from orchestrator.image_views import FULL_COPY_PEAK_BYTES, crop_slots, slots_for
from orchestrator.tools import region_zoom, vision
from orchestrator.tools.error_classifier import (
    ExpectedRefusalError,
    classify_tool_error,
)
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
    # 含宽高不是 8 的倍数的常见尺寸:JPEG ``draft`` 按「原尺寸 // 请求尺寸」选档,不整除时档位变小,
    # 估算(B-322 既有,B-332 没动)随之不单调 —— 钉在 main 上,不许这一轮顺手改。
    sizes = [
        (640, 480),
        (4032, 3024),
        (5184, 3888),
        (8064, 6048),
        (9487, 6000),
        (7001, 5003),
        (5163, 3873),
        (5656, 4243),
        (9018, 6764),
        (6000, 4000),
        (8160, 6120),
    ]
    for width, height in sizes:
        assert crop_peak_bytes(width, height, mode) == main.crop_peak_bytes(width, height, mode)


# ---------------------------------------------------------------------------
# 局部框、要缩:几何相同,像素差有界
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["RGB", "L"])
@pytest.mark.parametrize("orientation", range(1, 9))
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
    # 并集约 1.67 MP,在横条上限(300 万)以内,不用缩:与 main 逐字节相同。
    assert (ours.width, ours.height) == _unscaled(data, union, 0)
    assert _plain([ours]) == _plain([theirs])
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
    # 同样 24 MP 的 PNG 会被拒;JPEG 按缩小解码裁,老样子占 1 个。
    big = _solid((6000, 4000), "JPEG")
    assert crop_slots(big, 0, region_zoom.REGION_TARGET_PIXELS) == 1
    assert crop_slots(_solid((6000, 4000), "PNG"), 0, region_zoom.REGION_TARGET_PIXELS) is None


def test_png_exif_orientation_in_the_header_counts_as_rotated() -> None:
    cap = region_zoom.REGION_TARGET_PIXELS
    plain = _png(Image.new("RGB", (4000, 3000), (200, 200, 200)))
    tagged = _png(Image.new("RGB", (4000, 3000), (200, 200, 200)), orientation=6)
    assert crop_slots(plain, 0, cap) == 2
    assert crop_slots(tagged, 0, cap) is None
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
    with pytest.raises(ExpectedRefusalError) as raised:
        await vision._crop_in_thread(_fake_png(8000, 6000), [_WHOLE], 0)
    assert "too large to crop at its original resolution" in str(raised.value)
    assert "Nothing was cropped" in str(raised.value)
    assert "without region" in raised.value.advice
    assert _refused() == before + 1


def _refused(purpose: str = "region") -> float:
    return vision.ask_image_crop_refused_total.labels(
        reason="peak_over_budget", purpose=purpose
    )._value.get()


@pytest.mark.anyio
async def test_crop_takes_the_slots_the_estimate_says(monkeypatch: pytest.MonkeyPatch) -> None:
    taken: list[int | str] = []

    async def fake_slots(slots: int, call: object) -> list[object]:
        taken.append(slots)
        return []

    async def fake_slot(*_args: object, **_kwargs: object) -> list[object]:
        taken.append("single")
        return []

    monkeypatch.setattr(vision, "in_host_crop_slots", fake_slots)
    monkeypatch.setattr(vision, "in_host_crop_slot", fake_slot)
    await vision._crop_in_thread(_fake_png(2000, 1500), [_WHOLE], 0)
    await vision._crop_in_thread(_fake_png(4000, 3000), [_WHOLE], 0)
    await vision._crop_in_thread(_jpeg("RGB"), [_WHOLE], 0)
    assert taken == ["single", 2, "single"]


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

_MP_SIZES = {
    "6MP": (3000, 2000),
    "12MP": (4000, 3000),
    "24MP": (6000, 4000),
    "pano12MP": (12000, 1000),
}


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
    elif mode == "I;16":
        image = Image.frombytes("I;16", size, b"\x00\x80" * (size[0] * size[1]))
    return _png(image)


@pytest.mark.parametrize("degrees", [0, 90])
@pytest.mark.parametrize("mode", ["RGB", "RGBA", "L", "P", "I;16"])
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


# ---------------------------------------------------------------------------
# 独立审查(2026-11-21)返工
# ---------------------------------------------------------------------------

_BANDS = 3_000_000


@pytest.mark.parametrize("mode", ["RGBA", "LA", "PT", "P", "1", "I;16"])
def test_multi_strip_flatten_is_byte_identical_to_main(
    mode: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """条宽压到 7 行(最后一条不满):分条转 RGB、分条横向缩拼起来,与 main 逐字节相同。"""
    monkeypatch.setattr(region_zoom, "_FLATTEN_STRIP_PIXELS", _W * 7)
    for orientation in (1, 6):
        data = _png(_make(mode), orientation) if mode not in ("LA", "I;16") else _png(_make(mode))
        for degrees in (0, 90):
            for box in [_WHOLE, *_PARTIAL]:
                for cap in (_SMALL_CAP, region_zoom.REGION_TARGET_PIXELS):
                    ours = crop_image(data, [box], degrees, cap)
                    theirs = main.crop_image(data, [box], degrees, cap)
                    assert _plain(ours) == _plain(theirs), (mode, orientation, degrees, box, cap)


def test_header_only_helpers_never_decode_a_png() -> None:
    """``Image.getexif()`` 对没有 ``exif`` 头的 PNG 会整张解码;``image_size`` / ``exif_orientation``
    只读文件头:对只有文件头的假 PNG(解码就会失败)照样答得出。"""
    fake = _fake_png(8000, 6000, 6)
    assert region_zoom.image_size(fake) == (8000, 6000)
    assert region_zoom.image_size(fake, 90) == (6000, 8000)
    assert region_zoom.exif_orientation(fake) == 1
    with pytest.raises(OSError):  # 前提:真解码这张假图会失败
        Image.open(io.BytesIO(fake)).load()


def test_png_with_a_header_exif_still_reports_its_orientation() -> None:
    tagged = _png(Image.new("RGB", (40, 20)), orientation=6)
    assert region_zoom.exif_orientation(tagged) == 6
    assert region_zoom.image_size(tagged) == (20, 40)


def _trailing_exif_png(size: tuple[int, int], orientation: int) -> bytes:
    """``eXIf`` 块在 IDAT 之后(文件头里看不到方向)的 PNG。"""
    raw = _png(Image.new("RGB", size, (200, 200, 200)))
    exif = Image.Exif()
    exif[0x0112] = orientation
    body = exif.tobytes()[6:]
    crc = zlib.crc32(b"eXIf" + body) & 0xFFFFFFFF
    chunk = struct.pack(">I", len(body)) + b"eXIf" + body + struct.pack(">I", crc)
    at = raw.rindex(b"IEND") - 4
    return raw[:at] + chunk + raw[at:]


def test_trailing_exif_png_is_estimated_as_rotated() -> None:
    cap = region_zoom.REGION_TARGET_PIXELS
    trailing = _trailing_exif_png((4000, 3000), 6)
    assert crop_slots(_png(Image.new("RGB", (4000, 3000))), 0, cap) == 2
    assert crop_slots(trailing, 0, cap) is None
    # 真裁时照它转(整张已解开):输出是竖的。
    piece = crop_image(_trailing_exif_png((600, 400), 6), [_WHOLE], 0, _SMALL_CAP)[0]
    assert piece.width < piece.height


def test_harmless_header_exif_does_not_count_as_rotated() -> None:
    cap = region_zoom.REGION_TARGET_PIXELS
    flat = Image.new("RGB", (4000, 3000), (200, 200, 200))
    assert crop_slots(_png(flat, orientation=1), 0, cap) == 2
    one = io.BytesIO()
    exif = Image.Exif()
    exif[0x0112] = 1
    flat.save(one, format="PNG", exif=exif)
    assert crop_slots(one.getvalue(), 0, cap) == 2
    # 方向 6 再转 270 度合成回 1:不转;不再转就是转了。
    assert crop_slots(_png(flat, orientation=6), 270, cap) == 2
    assert crop_slots(_png(flat, orientation=6), 0, cap) is None


@pytest.mark.parametrize(
    ("size", "color_type"),
    [((10_000_000, 1), 3), ((40_000_000, 1), 3), ((10_000_000, 1), 2), ((1, 10_000_000), 2)],
)
def test_extreme_aspect_png_is_refused_before_decoding(
    size: tuple[int, int], color_type: int
) -> None:
    assert crop_slots(_fake_png(*size, color_type), 0, region_zoom.REGION_TARGET_PIXELS) is None


_EXTREME = {
    "P+tRNS 2Mx3": ((2_000_000, 3), "PT"),
    "RGB 3Mx1": ((3_000_000, 1), "RGB"),
    "RGB 1x3M": ((1, 3_000_000), "RGB"),
    "P+tRNS 3Mx1": ((3_000_000, 1), "PT"),
}


def _extreme_png(size: tuple[int, int], mode: str) -> bytes:
    if mode == "RGB":
        return _png(Image.new("RGB", size, (10, 200, 30)))
    image = Image.new("P", size, 5)
    image.putpalette([i % 256 for i in range(768)])
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", transparency=3)
    return buffer.getvalue()


_MEASURE_BOX = """
import gc, json, resource, sys
path, box, cap = sys.argv[1], json.loads(sys.argv[2]), int(sys.argv[3])
degrees = int(sys.argv[5])
data = open(path, "rb").read()
from orchestrator.tools.region_zoom import crop_image
crop_image(open(sys.argv[4], "rb").read(), [(0, 0, 1000, 1000)], 0)
gc.collect()
base = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
crop_image(data, [tuple(box)], degrees, cap)
grown = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss - base
print(json.dumps(grown * (1 if sys.platform == "darwin" else 1024)))
"""


def _measure_box(
    path: Path, warm: Path, box: tuple[int, int, int, int], cap: int, degrees: int = 0
) -> int:
    out = subprocess.run(  # noqa: S603 - 固定的解释器与脚本
        [
            sys.executable,
            "-c",
            _MEASURE_BOX,
            str(path),
            json.dumps(box),
            str(cap),
            str(warm),
            str(degrees),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=180,
    )
    return int(json.loads(out.stdout.strip().splitlines()[-1]))


@pytest.mark.parametrize("name", list(_EXTREME))
def test_measured_extreme_aspect_peak_stays_within_the_estimate(tmp_path: Path, name: str) -> None:
    """一像素高 / 宽的 PNG:滤波系数表与分条的整行临时空间,按像素数估会差一个数量级。"""
    size, mode = _EXTREME[name]
    path = tmp_path / "extreme.png"
    path.write_bytes(_extreme_png(size, mode))
    warm = tmp_path / "warm.png"
    warm.write_bytes(_extreme_png((64, 48), mode))
    real = _measure_box(path, warm, _WHOLE, region_zoom.REGION_TARGET_PIXELS)
    estimate = crop_peak_bytes(*size, "P" if mode == "PT" else mode, draftable=False)
    assert real <= estimate / PEAK_SAFETY, (real / 2**20, estimate / 2**20)


@pytest.mark.parametrize("mode", ["RGBA", "PT"])
@pytest.mark.parametrize("size", [(3600, 2700), (4000, 2700)])
def test_measured_band_crop_without_downscale_stays_within_the_estimate(
    tmp_path: Path, mode: str, size: tuple[int, int]
) -> None:
    """B-301 横条(上限 300 万像素)里不用缩的框:带透明的图在老路上同时有约 5 份拷贝。"""
    if mode == "RGBA":
        data = _png(Image.new("RGBA", size, (10, 200, 30, 120)))
        warm_bytes = _png(Image.new("RGBA", (64, 48), (1, 2, 3, 4)))
    else:
        data = _extreme_png(size, "PT")
        warm_bytes = _extreme_png((64, 48), "PT")
    path, warm = tmp_path / "band.png", tmp_path / "warm.png"
    path.write_bytes(data)
    warm.write_bytes(warm_bytes)
    real = _measure_box(path, warm, (0, 0, 1000, 277), _BANDS)
    estimate = crop_peak_bytes(*size, "P" if mode == "PT" else mode, _BANDS, draftable=False)
    assert real <= estimate / PEAK_SAFETY, (real / 2**20, estimate / 2**20)


# ---------------------------------------------------------------------------
# 预期之内的拒绝:不是 run 的失败;提示不再把模型引回必被拒的裁图
# ---------------------------------------------------------------------------


def _tools_node_view(error: BaseException) -> tuple[str, bool, list[object]]:
    """这个异常在 tools 节点里的真实去向(与生产同一串函数、同一份 ``ask_image`` 工具规格):
    ``(错误类, 会不会触发深想升档, run 级失败账本里剩什么)``。``ask_image`` 是只读工具,B-164 的
    ``_is_read_only_failure`` 让它所有非瞬态失败本来就不进账本。"""
    from langchain_core.messages import ToolMessage

    from orchestrator.graph_builder import builder as builder_mod

    spec = vision._spec_with_short_form()
    classified = classify_tool_error(tool_name="ask_image", error=error, spec=spec)
    call = {"name": "ask_image", "args": {}, "id": "c1", "type": "tool_call"}
    message = ToolMessage(content="x", tool_call_id="c1", status="error", name="ask_image")
    failure = builder_mod._classify_tool_failure(call, message, classified)
    ledger_failure = builder_mod._ledger_failure(failure, classified)
    outcomes = []
    if not builder_mod._is_read_only_failure(spec, ledger_failure):
        outcomes.append((builder_mod._ledger_key(call, message, failure), ledger_failure))
    ledger = list(builder_mod._apply_failure_ledger([], outcomes))
    # tools 节点:有建议的失败进 ``tool_failures``,其中非 transient 的触发深想升档。
    escalates = failure is not None and bool(failure.advice) and failure.error_class != "transient"
    return classified.error_class, escalates, ledger


def test_refusal_is_transient_without_escalation_and_outside_the_ledger() -> None:
    error_class, escalates, ledger = _tools_node_view(
        ExpectedRefusalError("too large", advice="read it whole")
    )
    assert (error_class, escalates, ledger) == ("transient", False, [])
    classified = classify_tool_error(
        tool_name="ask_image", error=ExpectedRefusalError("x", advice="read it whole")
    )
    assert (classified.retryable, classified.advice) == (False, "read it whole")


def test_a_genuine_crop_error_is_not_transient_and_escalates_but_stays_out_of_the_ledger() -> None:
    """``ask_image`` 只读:B-164 本来就不让它的失败进账本(与拒绝一样);区别是真错误不是 transient ——
    计 ``cm_tool_error_total{error_class="unknown"}``、会触发深想升档。"""
    error_class, escalates, ledger = _tools_node_view(
        region_zoom.RegionImageError("the image could not be decoded for cropping")
    )
    assert (error_class, escalates, ledger) == ("unknown", True, [])


@pytest.mark.anyio
async def test_orientation_candidate_refusal_is_counted_apart_and_logs_no_stack(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from orchestrator.tools.orientation_judge import judge_orientation

    async def candidate(_degrees: int) -> str:
        await vision._crop_in_thread(_fake_png(8000, 6000), [_WHOLE], 0, purpose="orientation")
        return "never"

    async def ask(_item: str, _budget: float) -> tuple[object, str | None]:
        raise AssertionError("no candidate, nothing to ask")

    before_region, before_orientation = _refused("region"), _refused("orientation")
    with caplog.at_level("INFO"):
        outcome = await judge_orientation(
            candidate=candidate,
            ask=ask,  # type: ignore[arg-type]
            budget=lambda _extra: 5.0,
        )
    assert (outcome.conclusion, outcome.reason) == ("not_asked", "no_copy")
    assert _refused("orientation") == before_orientation + 1
    assert _refused("region") == before_region
    assert not any(record.exc_info for record in caplog.records)


def _large_upload(tmp_path: Path, header_png: bytes) -> tuple[Any, Any]:
    from orchestrator.multimodal import ResolvedImage

    from .test_b266_region_zoom import _VL, _upload

    vl = _VL(answer=lambda _ref: "a lab report")
    up = _upload(tmp_path, vl, size=(64, 64))
    images = up.tool.image_resolver.uploads.images
    images[up.ref] = ResolvedImage(media_type="image/png", data=header_png)
    return up, vl


@pytest.mark.anyio
async def test_no_region_hint_for_an_image_the_host_cannot_crop(tmp_path: Path) -> None:
    up, _vl = _large_upload(tmp_path, _fake_png(8000, 6000))
    result = await up.tool.call({"image_ref": up.ref, "question": "?"}, ctx=up.ctx)
    assert region_zoom.REGION_HINT not in result.content
    assert result.content.startswith("a lab report")
    # 对照:裁得动的 12 MP 仍然提示。
    up, _vl = _large_upload(tmp_path, _fake_png(4000, 3000))
    result = await up.tool.call({"image_ref": up.ref, "question": "?"}, ctx=up.ctx)
    assert region_zoom.REGION_HINT in result.content


@pytest.mark.anyio
async def test_region_auto_on_an_uncroppable_image_reads_whole_and_does_not_say_reask(
    tmp_path: Path,
) -> None:
    up, vl = _large_upload(tmp_path, _fake_png(8000, 6000))
    result = await up.tool.call(
        {"image_ref": up.ref, "question": "?", "region": "auto"}, ctx=up.ctx
    )
    assert "too large to zoom into" in result.content
    assert "Re-ask with an explicit region" not in result.content
    assert len(vl.calls) == 1  # 只整张读了一次


@pytest.mark.anyio
async def test_explicit_region_on_an_uncroppable_image_raises_the_refusal(tmp_path: Path) -> None:
    up, _vl = _large_upload(tmp_path, _fake_png(8000, 6000))
    with pytest.raises(ExpectedRefusalError):
        await up.tool.call(
            {"image_ref": up.ref, "question": "?", "region": [0, 0, 500, 500]}, ctx=up.ctx
        )


@pytest.mark.anyio
async def test_write_crops_passes_its_purpose_to_the_refusal_count(tmp_path: Path) -> None:
    """问方向的候选副本经 ``_write_crops``:拒绝按 ``purpose="orientation"`` 计,不混进局部。"""
    data = _fake_png(8000, 6000)
    up, _vl = _large_upload(tmp_path, data)
    out_dir = up.tool._crop_dir(data, ctx=up.ctx)
    before = _refused("orientation"), _refused("region")
    with pytest.raises(ExpectedRefusalError):
        await up.tool._write_crops(
            data, out_dir, [_WHOLE], degrees=0, ctx=up.ctx, purpose="orientation"
        )
    assert (_refused("orientation"), _refused("region")) == (before[0] + 1, before[1])


@pytest.mark.parametrize("name", ["P+tRNS 2Mx3", "RGB 3Mx1", "RGB 1x3M", "P+tRNS 3Mx1"])
def test_measured_extreme_aspect_rotated_peak_stays_within_the_estimate(
    tmp_path: Path, name: str
) -> None:
    """转 90 度:缩的横向那一遍作用在转过的图上,中间图是 输出宽(对调) x 原宽。"""
    size, mode = _EXTREME[name]
    path, warm = tmp_path / "rot.png", tmp_path / "warm.png"
    path.write_bytes(_extreme_png(size, mode))
    warm.write_bytes(_extreme_png((64, 48), mode))
    real = _measure_box(path, warm, _WHOLE, region_zoom.REGION_TARGET_PIXELS, 90)
    estimate = crop_peak_bytes(*size, "P" if mode == "PT" else mode, draftable=False, rotated=True)
    assert real <= estimate / PEAK_SAFETY, (real / 2**20, estimate / 2**20)


def test_rotated_estimate_of_a_one_pixel_high_image_is_not_absurd() -> None:
    """900000x1 的 P 转 90 度实际约 67 MiB;估算不能是几个 GB(中间图按转后的形状算)。"""
    estimate = crop_peak_bytes(900_000, 1, "P", draftable=False, rotated=True)
    assert estimate < 400 * 2**20


def test_reads_a_copy_when_a_png_may_carry_a_trailing_orientation() -> None:
    from orchestrator.tools.image_orientation import Orientation

    upright = Orientation("upright")
    plain = _png(Image.new("RGB", (40, 20)))
    assert vision._reads_a_copy(upright, plain) is False
    assert vision._reads_a_copy(upright, _trailing_exif_png((40, 20), 6)) is True
    assert vision._reads_a_copy(Orientation("not_asked"), _trailing_exif_png((40, 20), 6)) is False


@pytest.mark.parametrize("mode", ["RGB", "RGBA", "L", "P"])
def test_png_estimate_is_monotone_in_size_with_no_multiple_of_eight_jumps(mode: str) -> None:
    """不能 ``draft`` 的格式没有 JPEG 那种「整除才缩」的档位:估算随尺寸单调,宽高不是 8 的倍数的
    常见尺寸与相邻的 8 的倍数估得差不多(JPEG 的估算有跳变,见设计 §10,B-322 既有)。"""
    previous = 0
    for width in range(1000, 9000, 37):
        estimate = crop_peak_bytes(width, width * 3 // 4, mode, draftable=False)
        assert estimate >= previous, (width, estimate, previous)
        previous = estimate
    for odd, nearby in [((5163, 3873), (5160, 3872)), ((5656, 4243), (5656, 4240))]:
        a = crop_peak_bytes(*odd, mode, draftable=False)
        b = crop_peak_bytes(*nearby, mode, draftable=False)
        assert abs(a - b) / b < 0.02, (odd, nearby, a, b)
