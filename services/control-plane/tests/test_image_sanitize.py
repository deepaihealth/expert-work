"""Tests for :mod:`control_plane.api._image_sanitize` — J.6.补强-4 / Mini-ADR J-34."""

from __future__ import annotations

import struct
import warnings
import zlib
from io import BytesIO

import pytest
from PIL import Image

from control_plane.api._image_sanitize import ImageSanitizeError, ImageTooLargeError, strip_exif


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
