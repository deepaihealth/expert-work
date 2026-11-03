"""Tests for :mod:`control_plane.api._image_sanitize` — J.6.补强-4 / Mini-ADR J-34."""

from __future__ import annotations

import struct
import warnings
import zlib
from io import BytesIO

import pytest
from PIL import Image, JpegImagePlugin

from control_plane.api import _image_sanitize
from control_plane.api._image_sanitize import ImageSanitizeError, ImageTooLargeError, strip_exif
from control_plane.knowledge.recognition.figure_images import MAX_DECODE_PIXELS


def _png_with_metadata() -> bytes:
    """A PNG carrying ancillary text chunks (the kind that exfil
    arbitrary strings through ``image.info``)."""
    buf = BytesIO()
    image = Image.new("RGB", (2, 2), color=(255, 0, 0))
    image.info["Comment"] = "tenant-secret-leak"  # Pillow lifts this into a tEXt chunk
    image.save(buf, format="PNG", pnginfo=_pnginfo({"Comment": "tenant-secret-leak"}))
    return buf.getvalue()


def _pnginfo(items: dict[str, str]) -> object:
    from PIL.PngImagePlugin import PngInfo

    info = PngInfo()
    for k, v in items.items():
        info.add_text(k, v)
    return info


def _jpeg_with_exif() -> bytes:
    """A JPEG carrying a synthetic EXIF block — sentinel bytes the
    sanitiser must remove."""
    buf = BytesIO()
    image = Image.new("RGB", (4, 4), color=(0, 255, 0))
    # EXIF bytes 0xFFE1 marker + payload starting with the sentinel.
    image.save(buf, format="JPEG", exif=b"expert-work-test-exif-secret")
    return buf.getvalue()


def test_strip_exif_removes_png_text_chunks() -> None:
    raw = _png_with_metadata()
    assert b"tenant-secret-leak" in raw  # PNG carries the text chunk

    sanitised = strip_exif(raw, mime_type="image/png")
    assert b"tenant-secret-leak" not in sanitised
    # Still a valid PNG.
    with Image.open(BytesIO(sanitised)) as image:
        image.load()
        assert image.size == (2, 2)


def test_strip_exif_removes_jpeg_exif() -> None:
    raw = _jpeg_with_exif()
    assert b"expert-work-test-exif-secret" in raw

    sanitised = strip_exif(raw, mime_type="image/jpeg")
    assert b"expert-work-test-exif-secret" not in sanitised
    with Image.open(BytesIO(sanitised)) as image:
        image.load()
        assert image.size == (4, 4)


def test_strip_exif_rejects_unknown_mime() -> None:
    with pytest.raises(ImageSanitizeError, match="unsupported mime_type"):
        strip_exif(b"any", mime_type="application/octet-stream")


def test_strip_exif_rejects_unparseable_bytes() -> None:
    with pytest.raises(ImageSanitizeError):
        strip_exif(b"not-an-image", mime_type="image/png")


# ---------------------------------------------------------------------------
# B-263 —— 拍照方向转正 / 重编码质量 / 解码像素上限 / GIF 只留首帧
# ---------------------------------------------------------------------------

_ORIENTATION_TAG = 0x0112
_GPS_IFD_TAG = 0x8825

#: 竖拍的「正」图:上半红、下半蓝,左上角一块绿 —— 任何旋转 / 翻转都会改变取样点的颜色。
_UPRIGHT_SIZE = (60, 90)
_RED, _BLUE, _GREEN = (220, 20, 20), (20, 20, 220), (20, 200, 20)


def _upright_reference() -> Image.Image:
    width, height = _UPRIGHT_SIZE
    image = Image.new("RGB", _UPRIGHT_SIZE, _RED)
    image.paste(_BLUE, (0, height // 2, width, height))
    image.paste(_GREEN, (0, 0, width // 3, height // 6))
    return image


def _assert_upright(image: Image.Image) -> None:
    """尺寸与三个取样点(左上绿 / 右上红 / 底部蓝)都与正图一致(JPEG 有损,留容差)。"""
    width, height = _UPRIGHT_SIZE
    assert image.size == _UPRIGHT_SIZE
    rgb = image.convert("RGB")
    for point, expected in (
        ((width // 6, height // 12), _GREEN),
        ((width * 5 // 6, height // 4), _RED),
        ((width // 2, height * 3 // 4), _BLUE),
    ):
        actual = rgb.getpixel(point)
        assert isinstance(actual, tuple)
        assert all(abs(a - e) <= 40 for a, e in zip(actual, expected, strict=True)), (
            point,
            actual,
            expected,
        )


#: 相机写入的方向标签 → 把正图变成「相机实际存下的像素」的变换(``exif_transpose`` 的逆变换)。
_STORED_TRANSPOSE_BY_ORIENTATION = {
    3: Image.Transpose.ROTATE_180,
    6: Image.Transpose.ROTATE_90,
    8: Image.Transpose.ROTATE_270,
}


def _camera_jpeg(orientation: int | None, *, with_gps: bool = False) -> bytes:
    """模拟手机照片:像素按传感器方向存、方向写在 EXIF 里,可选带 GPS 与机型。"""
    upright = _upright_reference()
    stored = (
        upright
        if orientation is None
        else upright.transpose(_STORED_TRANSPOSE_BY_ORIENTATION[orientation])
    )
    exif = Image.Exif()
    exif[0x010F] = "expert-work-test-camera-make"  # Make
    if orientation is not None:
        exif[_ORIENTATION_TAG] = orientation
    if with_gps:
        exif[_GPS_IFD_TAG] = {
            1: "N",  # GPSLatitudeRef
            2: (31.0, 13.0, 0.0),  # GPSLatitude
            3: "E",  # GPSLongitudeRef
            4: (121.0, 28.0, 0.0),  # GPSLongitude
        }
    buf = BytesIO()
    stored.save(buf, format="JPEG", quality=95, exif=exif)
    return buf.getvalue()


@pytest.mark.parametrize("orientation", [3, 6, 8])
def test_strip_exif_rotates_pixels_upright_and_drops_orientation_tag(orientation: int) -> None:
    raw = _camera_jpeg(orientation)
    with Image.open(BytesIO(raw)) as before:
        assert before.getexif().get(_ORIENTATION_TAG) == orientation

    sanitised = strip_exif(raw, mime_type="image/jpeg")

    with Image.open(BytesIO(sanitised)) as image:
        image.load()
        # 像素已转正,且不再带方向标签(否则看图端会再转一次)。
        assert image.getexif().get(_ORIENTATION_TAG) is None
        _assert_upright(image)


def test_strip_exif_keeps_unrotated_photo_upright() -> None:
    sanitised = strip_exif(_camera_jpeg(None), mime_type="image/jpeg")
    with Image.open(BytesIO(sanitised)) as image:
        image.load()
        _assert_upright(image)


def test_strip_exif_removes_gps_and_camera_tags_from_rotated_photo() -> None:
    raw = _camera_jpeg(6, with_gps=True)
    with Image.open(BytesIO(raw)) as before:
        assert before.getexif().get_ifd(_GPS_IFD_TAG)
    assert b"expert-work-test-camera-make" in raw

    sanitised = strip_exif(raw, mime_type="image/jpeg")

    assert b"expert-work-test-camera-make" not in sanitised
    assert b"Exif\x00\x00" not in sanitised
    with Image.open(BytesIO(sanitised)) as image:
        image.load()
        assert "exif" not in image.info
        exif = image.getexif()
        assert not exif.get_ifd(_GPS_IFD_TAG)
        assert len(exif) == 0


def _noisy_rgb(size: tuple[int, int] = (256, 256)) -> Image.Image:
    """有细节的图 —— 纯色图在任何质量下都编成同样的字节,区分不出质量档。"""
    return Image.merge(
        "RGB",
        [Image.effect_noise(size, sigma) for sigma in (60, 80, 100)],
    )


def test_strip_exif_reencodes_jpeg_at_high_quality() -> None:
    """量化表越小质量越高:输出的亮度 / 色度量化表逐项不大于 Pillow 按 quality=90 编出的表。

    旧实现走 Pillow 默认的 75 —— 小字号的化验单、处方会糊。"""
    buf = BytesIO()
    _noisy_rgb().save(buf, format="JPEG", quality=98)

    sanitised = strip_exif(buf.getvalue(), mime_type="image/jpeg")

    reference = BytesIO()
    _noisy_rgb().save(reference, format="JPEG", quality=90)
    with (
        Image.open(BytesIO(sanitised)) as image,
        Image.open(BytesIO(reference.getvalue())) as q90,
    ):
        tables = image.quantization  # type: ignore[attr-defined]
        q90_tables = q90.quantization  # type: ignore[attr-defined]
    assert tables.keys() == q90_tables.keys()
    for key, table in tables.items():
        assert all(ours <= ref for ours, ref in zip(table, q90_tables[key], strict=True)), key


def test_strip_exif_reencodes_webp_at_quality_90() -> None:
    """libwebp 同输入同参数输出逐字节确定:与 quality=90 自己编一遍的结果相同(旧实现是默认 80)。"""
    buf = BytesIO()
    _noisy_rgb().save(buf, format="WEBP", quality=100)
    raw = buf.getvalue()

    sanitised = strip_exif(raw, mime_type="image/webp")

    with Image.open(BytesIO(raw)) as decoded:
        decoded.load()
        expected = BytesIO()
        decoded.save(expected, format="WEBP", quality=90)
    assert sanitised == expected.getvalue()


def _png_header_only(width: int, height: int) -> bytes:
    """只有文件头的 PNG:声明的尺寸很大、字节很少 —— 解压炸弹的形状。"""

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
        )

    ihdr = struct.pack(">IIBBBBB", width, height, 1, 0, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(b""))
        + chunk(b"IEND", b"")
    )


def test_strip_exif_refuses_real_image_over_pixel_cap() -> None:
    """8000 x 8000 = 6400 万像素,超过 6000 万上限:真能解码的图也不解(旧实现照解不误)。"""
    buf = BytesIO()
    Image.new("1", (8000, 8000)).save(buf, format="PNG")

    with pytest.raises(ImageTooLargeError, match="pixel"):
        strip_exif(buf.getvalue(), mime_type="image/png")


def test_strip_exif_refuses_decompression_bomb_header() -> None:
    """20000 x 20000 = 4 亿像素:Pillow 在 ``Image.open`` 就抛 ``DecompressionBombError``。"""
    with pytest.raises(ImageTooLargeError):
        strip_exif(_png_header_only(20_000, 20_000), mime_type="image/png")


def test_strip_exif_refuses_bomb_warning_range_even_when_warnings_are_errors() -> None:
    """10000 x 10000 = 1 亿像素落在 Pillow 只告警的区间;告警被升级成异常时也归入「太大」。"""
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        with pytest.raises(ImageTooLargeError):
            strip_exif(_png_header_only(10_000, 10_000), mime_type="image/png")


def test_image_too_large_is_a_sanitize_error() -> None:
    """调用方只认 ``ImageSanitizeError`` 也不会漏接(太大是坏图的一种)。"""
    assert issubclass(ImageTooLargeError, ImageSanitizeError)


def test_strip_exif_keeps_only_first_gif_frame() -> None:
    """动图只留第一帧(看图模型只看静帧);钉住这个行为,改成保留全部帧要有意为之。"""
    colours = [(255, 0, 0), (0, 255, 0), (0, 0, 255)]
    frames = [Image.new("RGB", (8, 8), colour) for colour in colours]
    buf = BytesIO()
    frames[0].save(buf, format="GIF", save_all=True, append_images=frames[1:], duration=100)
    with Image.open(BytesIO(buf.getvalue())) as source:
        assert source.n_frames == 3  # type: ignore[attr-defined]

    sanitised = strip_exif(buf.getvalue(), mime_type="image/gif")

    with Image.open(BytesIO(sanitised)) as image:
        assert getattr(image, "n_frames", 1) == 1
        image.load()
        assert image.convert("RGB").getpixel((4, 4)) == colours[0]


def test_strip_exif_removes_gif_comment() -> None:
    """GIF 的注释块是 Pillow 默认会原样写回的元数据(JPEG / WebP 的 EXIF、XMP 默认不写回)——
    咬住 ``info.clear()``。"""
    buf = BytesIO()
    Image.new("RGB", (4, 4), (0, 0, 255)).save(
        buf, format="GIF", comment=b"expert-work-test-gif-comment"
    )
    assert b"expert-work-test-gif-comment" in buf.getvalue()

    sanitised = strip_exif(buf.getvalue(), mime_type="image/gif")

    assert b"expert-work-test-gif-comment" not in sanitised


# ---------------------------------------------------------------------------
# B-263 —— 超过像素上限的 JPEG 用 ``draft`` 按 1/2、1/4、1/8 缩小解码,不整张解开
# ---------------------------------------------------------------------------

#: 竖拍的正图 6000 x 10800 = 6480 万像素,超过上限;缩到 1/2 后 1620 万。
_BIG_UPRIGHT = (6000, 10800)
_BIG_DARK, _BIG_MID, _BIG_LIGHT = 0, 60, 200


def _big_camera_jpeg(orientation: int | None) -> bytes:
    """超上限的灰度 JPEG:正图上半浅、下半深,左上角一块黑;按方向标签存成相机的样子。

    先画 60 x 108 的小图再最近邻放大,内存只占一份大图(灰度 6480 万字节)。"""
    small = Image.new("L", (60, 108), _BIG_LIGHT)
    small.paste(_BIG_MID, (0, 54, 60, 108))
    small.paste(_BIG_DARK, (0, 0, 20, 18))
    if orientation is not None:
        small = small.transpose(_STORED_TRANSPOSE_BY_ORIENTATION[orientation])
    width, height = _BIG_UPRIGHT
    stored_size = (height, width) if orientation in (6, 8) else _BIG_UPRIGHT
    big = small.resize(stored_size, Image.Resampling.NEAREST)
    exif = Image.Exif()
    if orientation is not None:
        exif[_ORIENTATION_TAG] = orientation
    buf = BytesIO()
    big.save(buf, format="JPEG", quality=75, exif=exif)
    return buf.getvalue()


def _assert_big_upright(image: Image.Image, size: tuple[int, int]) -> None:
    width, height = size
    assert image.size == size
    gray = image.convert("L")
    for point, expected in (
        ((width // 6, height // 12), _BIG_DARK),
        ((width * 5 // 6, height // 4), _BIG_LIGHT),
        ((width // 2, height * 3 // 4), _BIG_MID),
    ):
        actual = gray.getpixel(point)
        assert isinstance(actual, (int, float))
        assert abs(actual - expected) <= 40, (point, actual, expected)


def _record_jpeg_decode_sizes(monkeypatch: pytest.MonkeyPatch) -> list[tuple[int, int]]:
    """记下每次 JPEG 真正解码出来的像素尺寸 —— 先整张解开再缩小的实现在这里现形。"""
    decoded: list[tuple[int, int]] = []
    original_load = JpegImagePlugin.JpegImageFile.load

    def spy_load(self: JpegImagePlugin.JpegImageFile) -> object:
        # ``tile`` 非空 = 这次真去解码;已解过的再调 ``load()`` 是空操作,不记。
        decoding = bool(self.tile)
        result = original_load(self)
        if decoding:
            decoded.append(self.im.size)
        return result

    monkeypatch.setattr(JpegImagePlugin.JpegImageFile, "load", spy_load)
    return decoded


def test_strip_exif_shrinks_over_cap_jpeg_instead_of_refusing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    decoded = _record_jpeg_decode_sizes(monkeypatch)
    width, height = _BIG_UPRIGHT
    assert width * height > MAX_DECODE_PIXELS

    sanitised = strip_exif(_big_camera_jpeg(None), mime_type="image/jpeg")

    # 解码就是 1/2 尺寸,从没整张解开。
    assert decoded == [(width // 2, height // 2)]
    decoded.clear()
    with Image.open(BytesIO(sanitised)) as image:
        image.load()
        assert image.format == "JPEG"
        assert image.width * image.height <= MAX_DECODE_PIXELS
        _assert_big_upright(image, (width // 2, height // 2))


def test_strip_exif_shrinks_over_cap_jpeg_and_still_rotates_it_upright(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """方向 6:相机存的是 10800 x 6000;先缩小解码再转正,得到 3000 x 5400 的正图。"""
    decoded = _record_jpeg_decode_sizes(monkeypatch)
    width, height = _BIG_UPRIGHT

    sanitised = strip_exif(_big_camera_jpeg(6), mime_type="image/jpeg")

    assert decoded == [(height // 2, width // 2)]
    with Image.open(BytesIO(sanitised)) as image:
        image.load()
        assert image.getexif().get(_ORIENTATION_TAG) is None
        _assert_big_upright(image, (width // 2, height // 2))


def _jpeg_header_only(width: int, height: int) -> bytes:
    """只有 SOF / SOS 段的 JPEG:声明尺寸,没有像素数据。"""

    def segment(marker: bytes, data: bytes) -> bytes:
        return b"\xff" + marker + struct.pack(">H", len(data) + 2) + data

    sof = struct.pack(">BHHB", 8, height, width, 3) + b"".join(
        bytes([component, 0x11, 0]) for component in (1, 2, 3)
    )
    sos = (
        bytes([3]) + b"".join(bytes([component, 0]) for component in (1, 2, 3)) + bytes([0, 63, 0])
    )
    return b"\xff\xd8" + segment(b"\xc0", sof) + segment(b"\xda", sos) + b"\xff\xd9"


def test_strip_exif_picks_smallest_draft_scale_that_fits(monkeypatch: pytest.MonkeyPatch) -> None:
    """2 万 x 2 万:1/2 还有 1 亿像素,1/4 才进上限 —— 选 1/4,不多缩。

    默认的 Pillow 炸弹阈值(约 1.79 亿)会在 ``Image.open`` 就拦下这张图,这里放开阈值单测选档。"""
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", None)
    drafted: list[tuple[int, int]] = []
    original_draft = JpegImagePlugin.JpegImageFile.draft

    def spy_draft(
        self: JpegImagePlugin.JpegImageFile, mode: str | None, size: tuple[int, int] | None
    ) -> object:
        result = original_draft(self, mode, size)
        drafted.append(self.size)
        return result

    monkeypatch.setattr(JpegImagePlugin.JpegImageFile, "draft", spy_draft)

    # 只有文件头、没有像素数据:缩小解码这一步会失败,这里只看选了哪一档。
    with pytest.raises(ImageSanitizeError):
        strip_exif(_jpeg_header_only(20_000, 20_000), mime_type="image/jpeg")
    assert drafted == [(5_000, 5_000)]


def test_strip_exif_refuses_jpeg_still_over_cap_at_one_eighth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """65535 x 65535 缩到 1/8 仍有 6700 万像素:拒收(只有放开炸弹阈值才走得到这里)。"""
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", None)
    with pytest.raises(ImageTooLargeError, match="pixel"):
        strip_exif(_jpeg_header_only(65_535, 65_535), mime_type="image/jpeg")


def test_strip_exif_refuses_jpeg_over_pillow_bomb_threshold() -> None:
    """超过 Pillow 炸弹阈值的 JPEG 在 ``Image.open`` 就抛错,轮不到缩小解码:拒收。"""
    with pytest.raises(ImageTooLargeError):
        strip_exif(_jpeg_header_only(20_000, 20_000), mime_type="image/jpeg")


def test_strip_exif_still_refuses_over_cap_non_jpeg_headers() -> None:
    """PNG 没有缩小解码,超上限照旧拒收(不解码)。"""
    with pytest.raises(ImageTooLargeError, match="pixel"):
        strip_exif(_png_header_only(8_000, 8_000), mime_type="image/png")


# ---------------------------------------------------------------------------
# B-266 review M4 —— 重编码后超过字节上限:先降质量再存,仍超就是「太大」。
# ---------------------------------------------------------------------------


def _q85_noisy_jpeg() -> bytes:
    buf = BytesIO()
    _noisy_rgb().save(buf, format="JPEG", quality=85)
    return buf.getvalue()


def test_strip_exif_falls_back_to_lower_quality_to_stay_within_the_byte_cap() -> None:
    raw = _q85_noisy_jpeg()
    with Image.open(BytesIO(raw)) as decoded:
        decoded.load()
        at_92 = BytesIO()
        decoded.save(at_92, format="JPEG", quality=92)
    assert len(at_92.getvalue()) > len(raw)  # 前提:q85 的原图按 q92 重存会变大

    sanitised = strip_exif(raw, mime_type="image/jpeg", max_bytes=len(raw))

    assert len(sanitised) <= len(raw)
    with Image.open(BytesIO(sanitised)) as image:
        assert image.format == "JPEG"


def test_strip_exif_refuses_when_even_the_lowest_quality_is_over_the_byte_cap() -> None:
    with pytest.raises(ImageTooLargeError, match="byte"):
        strip_exif(_q85_noisy_jpeg(), mime_type="image/jpeg", max_bytes=1000)


def test_strip_exif_refuses_a_lossless_reencode_over_the_byte_cap() -> None:
    buf = BytesIO()
    _noisy_rgb((64, 64)).save(buf, format="PNG")
    with pytest.raises(ImageTooLargeError, match="byte"):
        strip_exif(buf.getvalue(), mime_type="image/png", max_bytes=100)


def test_strip_exif_without_a_cap_keeps_quality_92() -> None:
    raw = _q85_noisy_jpeg()
    with Image.open(BytesIO(raw)) as decoded:
        decoded.load()
        at_92 = BytesIO()
        decoded.save(at_92, format="JPEG", quality=92, exif=b"")
    assert strip_exif(raw, mime_type="image/jpeg") == at_92.getvalue()


def test_strip_exif_shrinks_an_over_cap_mpo_like_a_jpeg(monkeypatch: pytest.MonkeyPatch) -> None:
    """B-266 review —— 手机的多图 JPEG(MPO)``format`` 是 "MPO",也要走缩小解码,不能拒收。"""
    monkeypatch.setattr(_image_sanitize, "MAX_DECODE_PIXELS", 20_000)
    first, second = _noisy_rgb((200, 200)), _noisy_rgb((200, 200))
    buf = BytesIO()
    first.save(buf, format="MPO", save_all=True, append_images=[second])
    with Image.open(BytesIO(buf.getvalue())) as probe:
        assert probe.format == "MPO"

    sanitised = strip_exif(buf.getvalue(), mime_type="image/jpeg")

    with Image.open(BytesIO(sanitised)) as image:
        assert image.width * image.height <= 20_000
