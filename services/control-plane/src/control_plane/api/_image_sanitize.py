"""J.6.补强-4 (Mini-ADR J-34) — EXIF strip on upload.

Camera images carry an EXIF block with GPS coordinates, device
identifiers, timestamps, and software fingerprints — a low-cost
metadata exfiltration channel if uploads sit in tenant object stores
that get shared, exported, or backed up. Strip the block before the
bytes leave the request handler.

The mime allowlist already excludes SVG (no XML / script surface), so
we only worry about the raster formats Pillow understands. PNG / JPEG /
WebP / GIF all flow through ``Image.open`` round-tripping cleanly. Any
parse error short-circuits to ``ValueError`` — the uploads handler
maps that to ``400 invalid image``.

B-263 —— 重编码不再丢方向、不再压糊:

* 先按 EXIF 方向标签把像素转正(``ImageOps.exif_transpose``),再剥元数据 —— 否则手机竖拍的
  照片存下来是横的,看图模型看到的也是横的;
* JPEG 用质量 :data:`JPEG_QUALITY`、WebP 用 :data:`WEBP_QUALITY` 重编码(Pillow 默认 75 / 80
  会把化验单、处方上的小字压糊);色度仍是 4:2:0 —— 手机原图本来就是 4:2:0,改存 4:4:4 补不回
  丢掉的色度,只让文件变大(3000 x 4000 合成照片实测比原图大 1.1~1.4 倍);PNG 无损不变;
* 解码前按像素数设上限(与知识库图片同一个 :data:`MAX_DECODE_PIXELS`)—— 字节上限挡不住
  「几 KB 声明几亿像素」的解压炸弹。超过上限的 JPEG 用 Pillow 的 ``draft`` 按 1/2、1/4、1/8
  缩小解码(DCT 阶段直接出小图,整张图从不在内存里展开),取能进上限的最小缩小倍数;其它格式
  没有缩小解码,超过就不解码、抛 :class:`ImageTooLargeError`(上传接口回 413)。超过 Pillow
  炸弹阈值(约 1.79 亿像素)的图在 ``Image.open`` 读文件头时就被拦下,JPEG 也一样拒收;
* GIF 只保留第一帧(``save`` 不带 ``save_all``):图片是给看图模型看的,看的是静帧。
"""

from __future__ import annotations

from io import BytesIO
from typing import Final

from PIL import Image, ImageOps, UnidentifiedImageError

from control_plane.knowledge.recognition.figure_images import MAX_DECODE_PIXELS

#: JPEG 缩小解码可选的倍数(libjpeg 的 DCT 缩放只支持这几档),从小往大试。
_JPEG_DRAFT_SCALES: Final = (2, 4, 8)

#: 有损格式的重编码质量(B-263)。
JPEG_QUALITY: Final = 92
WEBP_QUALITY: Final = 90

#: Pillow format name keyed by the upload's MIME type — keeps the
#: round-trip in the same format the user uploaded so the bytes match
#: their original perceptually.
_PIL_FORMAT_BY_MIME: Final[dict[str, str]] = {
    "image/png": "PNG",
    "image/jpeg": "JPEG",
    "image/webp": "WEBP",
    "image/gif": "GIF",
}

#: 各格式额外的 ``save`` 参数;PNG / GIF 无损,不需要。
_SAVE_OPTIONS_BY_FORMAT: Final[dict[str, dict[str, int]]] = {
    "JPEG": {"quality": JPEG_QUALITY},
    "WEBP": {"quality": WEBP_QUALITY},
}


class ImageSanitizeError(ValueError):
    """The uploaded bytes failed to parse / re-encode through Pillow.

    Raised by :func:`strip_exif` so the uploads handler can map to
    ``400 invalid image`` rather than a 500 (Pillow exceptions vary by
    format).
    """


class ImageTooLargeError(ImageSanitizeError):
    """图片像素数超过 :data:`MAX_DECODE_PIXELS`(B-263),不解码。

    是 :class:`ImageSanitizeError` 的子类:只认父类的调用方照样接得住;上传接口单独接它回
    ``413``(与超过字节上限同一个码)。
    """


def strip_exif(raw: bytes, *, mime_type: str) -> bytes:
    """Return ``raw`` re-encoded **without** EXIF / metadata.

    Mini-ADR J-34. Pillow's ``Image.save(..., exif=b"")`` drops the
    EXIF block in formats that carry one (JPEG / WebP); for PNG / GIF
    we strip the ``info`` metadata dict before re-encoding. Either
    way the output bytes carry zero ancillary metadata.

    :param raw: the uploaded bytes (already size-validated upstream).
    :param mime_type: the upload's claimed MIME (from the allowlist —
        anything outside ``_PIL_FORMAT_BY_MIME`` is a programmer error
        because the uploads handler validates against the allowlist
        first).
    :raises ImageTooLargeError: the image declares more than
        :data:`MAX_DECODE_PIXELS` pixels (B-263).
    :raises ImageSanitizeError: bytes don't parse, or the re-encode
        path the format requires isn't available.
    """
    pil_format = _PIL_FORMAT_BY_MIME.get(mime_type)
    if pil_format is None:
        msg = f"strip_exif: unsupported mime_type {mime_type!r}"
        raise ImageSanitizeError(msg)
    try:
        with Image.open(BytesIO(raw)) as image:
            # 只读了文件头:先看声明的像素数,超限的 JPEG 缩小解码、其它格式不解码(B-263)。
            _fit_pixel_cap(image)
            # Force a full decode so partially-read streams surface here
            # as ``ImageSanitizeError`` rather than later in ``save``.
            image.load()
            # 按方向标签把像素转正,并去掉标签本身(B-263);没有标签时原样不动。
            ImageOps.exif_transpose(image, in_place=True)
            out = BytesIO()
            # ``info`` carries PNG text chunks + GIF transparency etc.;
            # wiping it removes ancillary metadata that ``exif=b""``
            # doesn't reach.
            image.info.clear()
            # The ``save`` call mirrors Pillow's documented EXIF-strip
            # idiom — passing an empty ``exif`` bytes object overrides
            # the source's block.
            image.save(
                out, format=pil_format, exif=b"", **_SAVE_OPTIONS_BY_FORMAT.get(pil_format, {})
            )
            return out.getvalue()
    except ImageTooLargeError:
        # 上面自己抛的;它也是 ``ValueError``,别让最后一条 except 改写成「解不开」。
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        # 超过 Pillow 自己的上限(约 1.79 亿像素)时 ``Image.open`` 就抛错;告警区间
        # (约 0.89 亿起)只有在告警被升级成异常时才会到这里 —— 都比上面的上限大,归入「太大」。
        msg = f"image exceeds {MAX_DECODE_PIXELS}-pixel limit"
        raise ImageTooLargeError(msg) from exc
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        msg = f"strip_exif: cannot re-encode image as {pil_format}"
        raise ImageSanitizeError(msg) from exc


def _fit_pixel_cap(image: Image.Image) -> None:
    """让接下来的 ``load()`` 解出不超过 :data:`MAX_DECODE_PIXELS` 的像素,做不到就抛
    :class:`ImageTooLargeError`。只在 ``Image.open`` 之后、``load()`` 之前调用。

    JPEG 用 ``draft`` 选能进上限的最小缩小倍数;``draft`` 之后 ``image.size`` 就是将要解出的
    尺寸,所以最后按它再核一次 —— ``draft`` 没生效(返回 ``None``)或 1/8 仍超限都在这里拒收。
    """
    width, height = image.size
    if width * height <= MAX_DECODE_PIXELS:
        return
    if image.format == "JPEG":
        for scale in _JPEG_DRAFT_SCALES:
            if -(-width // scale) * -(-height // scale) <= MAX_DECODE_PIXELS:
                image.draft(image.mode, (max(1, width // scale), max(1, height // scale)))
                break
    if image.width * image.height > MAX_DECODE_PIXELS:
        msg = f"image exceeds {MAX_DECODE_PIXELS}-pixel limit"
        raise ImageTooLargeError(msg)
