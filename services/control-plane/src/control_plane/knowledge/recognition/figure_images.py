"""B-209 PR-2 §5.4 —— 图片字节的处理(Pillow,在解析进程池里跑;纯函数、可 pickle)。

* **规整**:每张图转成送模型、也存对象存储的同一份 JPEG —— RGB(透明处垫白)、长边不超过
  :data:`FIGURE_MAX_EDGE_PX`、质量 :data:`FIGURE_JPEG_QUALITY`、不带 EXIF(拍照图里的 GPS /
  设备信息不进对象存储)。原始容器格式(TIFF / JPX / 带透明的 PNG)浏览器不一定能显示,规整后
  的 JPEG 都能。
* **像素哈希**:解码后的 RGB 像素的 sha256 —— 同一张图换个压缩 / 容器还是同一个哈希
  (同租户 ≥ 3 份文档出现的过滤按它数)。
* **解不开的格式**(WMF / EMF / SVG 等矢量格式):Pillow 只在 Windows 上能栅格化 WMF / EMF,
  镜像里也没有其它栅格化工具 —— 跳过、计数(``unsupported``),不报错。
"""

from __future__ import annotations

import hashlib
import io
from dataclasses import dataclass

from PIL import Image, ImageOps, UnidentifiedImageError

FIGURE_MAX_EDGE_PX = 2000
FIGURE_JPEG_QUALITY = 85
#: Images decoded at most this large (pixels); bigger ones are skipped as
#: unsupported rather than decoded (a decompression bomb, or a poster scan).
MAX_DECODE_PIXELS = 60_000_000

#: Vector formats no rasteriser in the image can draw (by magic bytes).
_WMF_PLACEABLE = b"\xd7\xcd\xc6\x9a"
_EMF_SIGNATURE = b" EMF"


def render_params() -> str:
    """Part of every figure cache key: an image normalized differently is a
    different image."""
    return f"figure;edge={FIGURE_MAX_EDGE_PX};jpeg={FIGURE_JPEG_QUALITY}"


@dataclass(frozen=True)
class NormalizedImage:
    """``jpeg``: the bytes sent to the models and stored; ``width`` /
    ``height``: the source image's pixel size (before any downscaling)."""

    jpeg: bytes
    width: int
    height: int
    pixel_hash: str


def is_vector_format(blob: bytes, mime: str | None = None) -> bool:
    """WMF / EMF / SVG — formats Pillow cannot rasterise here."""
    kind = (mime or "").lower()
    if any(tag in kind for tag in ("wmf", "emf", "svg")):
        return True
    head = blob[:64]
    return (
        head.startswith(_WMF_PLACEABLE)
        or (len(blob) > 44 and blob[40:44] == _EMF_SIGNATURE)
        or head.lstrip().startswith((b"<svg", b"<?xml"))
    )


def image_size(blob: bytes) -> tuple[int, int] | None:
    """The image's pixel size from its header (no full decode); ``None`` when
    Pillow cannot read it."""
    try:
        with Image.open(io.BytesIO(blob)) as image:
            width, height = image.size
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError):
        return None
    return int(width), int(height)


def normalize(blob: bytes) -> NormalizedImage | None:
    """``blob`` as the figure JPEG (see module doc); ``None`` when it cannot
    be decoded here (unsupported format, damaged, too large)."""
    try:
        with Image.open(io.BytesIO(blob)) as image:
            width, height = image.size
            if width <= 0 or height <= 0 or width * height > MAX_DECODE_PIXELS:
                return None
            image.load()
            upright = ImageOps.exif_transpose(image) or image
            rgb = _flatten(upright)
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError):
        return None
    return _encode(rgb, width, height)


def from_rgb(image: Image.Image) -> NormalizedImage:
    """An already-decoded RGB image (a crop of a rendered page) as a figure."""
    rgb = _flatten(image)
    return _encode(rgb, rgb.width, rgb.height)


def crop(jpeg: bytes, box: tuple[float, float, float, float]) -> NormalizedImage | None:
    """The ``box`` (pixel coordinates, clamped to the image) of a rendered
    page; ``None`` when the clamped box is empty."""
    with Image.open(io.BytesIO(jpeg)) as page:
        page.load()
        left, top = max(0, int(box[0])), max(0, int(box[1]))
        right, bottom = min(page.width, int(box[2])), min(page.height, int(box[3]))
        if right - left < 1 or bottom - top < 1:
            return None
        return from_rgb(page.crop((left, top, right, bottom)))


def _flatten(image: Image.Image) -> Image.Image:
    """RGB, transparency composited onto white."""
    if image.mode in ("RGBA", "LA") or (image.mode == "P" and "transparency" in image.info):
        rgba = image.convert("RGBA")
        canvas = Image.new("RGB", rgba.size, (255, 255, 255))
        canvas.paste(rgba, mask=rgba.getchannel("A"))
        return canvas
    return image.convert("RGB")


def _encode(rgb: Image.Image, width: int, height: int) -> NormalizedImage:
    pixel_hash = hashlib.sha256(f"{rgb.width}x{rgb.height};".encode() + rgb.tobytes()).hexdigest()
    longest = max(rgb.width, rgb.height)
    if longest > FIGURE_MAX_EDGE_PX:
        scale = FIGURE_MAX_EDGE_PX / longest
        size = (max(1, round(rgb.width * scale)), max(1, round(rgb.height * scale)))
        rgb = rgb.resize(size, Image.Resampling.LANCZOS)
    out = io.BytesIO()
    rgb.save(out, format="JPEG", quality=FIGURE_JPEG_QUALITY)
    return NormalizedImage(jpeg=out.getvalue(), width=width, height=height, pixel_hash=pixel_hash)
