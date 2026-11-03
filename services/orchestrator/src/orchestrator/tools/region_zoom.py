"""B-266 —— 看图「放大局部」:``region`` 参数的校验、切块几何、分辨率,以及图片的宿主侧裁剪。
每条横条回答的解析与汇总在 :mod:`orchestrator.tools.tile_reading`。

为什么要裁:看图模型不论图多大都压到约 1,400 输入 token,整页里 7-10 像素的小字读对率只有一成;
裁成一块再看,同样的预算看更小的面积(实测与数据见
``docs/superpowers/specs/2026-11-03-b266-region-zoom.md``)。

坐标一律是 0-1000 的整数 ``[x1, y1, x2, y2]``(看图模型自己报框就是这个尺度)。文档页的局部在
沙箱里按区域高分辨率渲(``tools.read_page``);图片的局部在宿主按原像素裁(本模块
:func:`crop_image`)—— 上传图的字节只在宿主拿得到(对象存储),工作区图片走同一条路省一套
沙箱片段。

本模块只做纯计算与 Pillow 裁剪,不碰工作区与沙箱。
"""

from __future__ import annotations

import io
import math
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final, Literal

from PIL import ExifTags, Image, UnidentifiedImageError
from PIL.JpegImagePlugin import JpegImageFile

#: 坐标尺度:``[x1, y1, x2, y2]`` 都在 0..1000。
REGION_SCALE: Final = 1000
#: 框的最小面积:整图的 1%(``(x2-x1)*(y2-y1) >= 10000``)。更小的框几乎只会是定位错。
MIN_REGION_AREA: Final = REGION_SCALE * REGION_SCALE // 100
AUTO_REGION: Final = "auto"
#: ``region="auto"`` 的门槛(B-266 重设计,11-03 验证):图(文档页按 :data:`DOCUMENT_ZOOM_DPI`
#: 算)不到 400 万像素就不切,整张快看一次。实测 300 万时图表会被切开、多出一个错答案。
AUTO_GATE_PIXELS: Final = 4_000_000
#: 切成整宽的横条:条数 = ceil(像素 / 150 万),夹在 3..8 之间;相邻两条重叠条高的 12%。
#: 整宽是为了让一行字、一张表的一行永远不被竖着切开(原来的 2x3 把图表、宽表切坏了)。
AUTO_BAND_PIXELS: Final = 1_500_000
AUTO_MIN_BANDS: Final = 3
AUTO_MAX_BANDS: Final = 8
AUTO_BAND_OVERLAP: Final = 0.12
#: 文档页「放大来看」用的分辨率:门槛按这一档的页面像素算,横条也按这一档渲。300 dpi 的 A4 是
#: 2480 x 3508(约 870 万像素)—— 验证用的合成化验单就是这个尺寸。文档页没有「原像素」(矢量),
#: 已有的整页渲染只有 100 dpi(A4 约 97 万像素),拿它比门槛会把每一页都当成小图。
DOCUMENT_ZOOM_DPI: Final = 300
#: 文档页的局部按这个像素数选分辨率(约 1.5 百万像素),上限 400 dpi。
REGION_TARGET_PIXELS: Final = 1_500_000
MAX_REGION_DPI: Final = 400
#: 图片解码上限(像素数)。与上传那一侧(control-plane ``figure_images.MAX_DECODE_PIXELS``,
#: B-263)同一个值;编排层不能 import control-plane,这里另记一份,
#: ``services/control-plane/tests/test_region_zoom_decode_cap.py`` 钉两边相等。
MAX_DECODE_PIXELS: Final = 60_000_000
#: 整页快看之后,只有 ``region="auto"`` 不会被门槛挡回来的图才提示它(与门槛同一个数)。
LARGE_IMAGE_PIXELS: Final = AUTO_GATE_PIXELS
#: 裁图存成 JPEG 的质量 —— 与 B-263 上传转码同一档。
CROP_JPEG_QUALITY: Final = 92

#: 整页快看一张文档页或大图之后追加的提示。
REGION_HINT: Final = (
    "(To read one specific small printed value, name or date in this large image precisely, "
    "you can call ask_image again with region='auto'; do not use it for counting, comparisons, "
    "trends or questions about the whole image.)"
)

Box = tuple[int, int, int, int]
Region = Box | Literal["auto"]


class RegionImageError(ValueError):
    """图片裁不了:不是能解码的图,或像素数超过 :data:`MAX_DECODE_PIXELS`。"""


_BOX_SHAPE: Final = f"a list of four integers [x1, y1, x2, y2] on a 0-{REGION_SCALE} scale"


def parse_region(raw: object, *, tool: str) -> Region | None:
    """``ask_image`` 的 ``region``:缺席 / ``null`` → ``None``;``"auto"``;或一个框(见
    :func:`parse_box`)。不合法当场抛。"""
    if raw == AUTO_REGION:
        return AUTO_REGION
    return parse_box(raw, tool=tool, expected=f'"auto" or {_BOX_SHAPE}')


def parse_box(raw: object, *, tool: str, expected: str = _BOX_SHAPE) -> Box | None:
    """四个整数的框:``0 <= x1 < x2 <= 1000``、``0 <= y1 < y2 <= 1000``、面积不小于整图 1%。

    缺席 / ``null`` → ``None``;其余不合法一律 ``ValueError``(文字说清哪条不满足)。"""
    if raw is None:
        return None
    if not isinstance(raw, list | tuple) or len(raw) != 4:
        raise ValueError(f"{tool} 'region' must be {expected}, got {raw!r}")
    if any(isinstance(v, bool) or not isinstance(v, int) for v in raw):
        raise ValueError(f"{tool} 'region' must be {expected}, got {raw!r}")
    x1, y1, x2, y2 = raw
    if not (0 <= x1 < x2 <= REGION_SCALE and 0 <= y1 < y2 <= REGION_SCALE):
        msg = (
            f"{tool} 'region' {list(raw)} is out of range: it needs "
            f"0 <= x1 < x2 <= {REGION_SCALE} and 0 <= y1 < y2 <= {REGION_SCALE}"
        )
        raise ValueError(msg)
    if (x2 - x1) * (y2 - y1) < MIN_REGION_AREA:
        msg = (
            f"{tool} 'region' {list(raw)} is too small: it must cover at least 1% of the image "
            f"((x2-x1)*(y2-y1) >= {MIN_REGION_AREA}); widen the box around what you need"
        )
        raise ValueError(msg)
    return (x1, y1, x2, y2)


@dataclass(frozen=True)
class Tile:
    """``region="auto"`` 的一条:名字(给模型看,``band 1`` 在最上面)+ 框。"""

    label: str
    box: Box


def band_count(pixels: int) -> int:
    """切几条:``ceil(像素 / 150 万)``,夹在 :data:`AUTO_MIN_BANDS`..:data:`AUTO_MAX_BANDS`。"""
    wanted = math.ceil(pixels / AUTO_BAND_PIXELS) if pixels > 0 else AUTO_MIN_BANDS
    return max(AUTO_MIN_BANDS, min(AUTO_MAX_BANDS, wanted))


def auto_bands(count: int) -> tuple[Tile, ...]:
    """``count`` 条整宽横条,从上到下,相邻两条重叠不少于条高的 :data:`AUTO_BAND_OVERLAP`
    (起点向下取整、终点向上取整),合起来盖满整个高度。"""
    height = REGION_SCALE / (count - (count - 1) * AUTO_BAND_OVERLAP)
    step = height * (1 - AUTO_BAND_OVERLAP)
    bands: list[Tile] = []
    for index in range(count):
        top = math.floor(index * step)
        bottom = min(REGION_SCALE, math.ceil(index * step + height))
        if index == count - 1:
            bottom = REGION_SCALE
        bands.append(Tile(label=f"band {index + 1}", box=(0, top, REGION_SCALE, bottom)))
    return tuple(bands)


def region_dpi(page_px: tuple[int, int], box: Box, *, base_dpi: int) -> int:
    """文档页的这一块按多少 dpi 渲,让它约 :data:`REGION_TARGET_PIXELS` 像素,上限 400。

    ``page_px`` 是这一页在 ``base_dpi`` 下渲出来的像素尺寸(已有的整页渲染),页面英寸数由它换算。
    """
    width, height = page_px
    x1, y1, x2, y2 = box
    area_at_base = width * height * (x2 - x1) * (y2 - y1) / (REGION_SCALE * REGION_SCALE)
    if area_at_base <= 0:
        return MAX_REGION_DPI
    dpi = base_dpi * math.sqrt(REGION_TARGET_PIXELS / area_at_base)
    return max(1, min(MAX_REGION_DPI, math.floor(dpi)))


def pixel_box(box: Box, size: tuple[float, float]) -> tuple[int, int, int, int]:
    """0-1000 的框 → ``size`` 像素图上的 ``(left, top, right, bottom)``(向外取整,夹在图内)。"""
    width, height = size
    x1, y1, x2, y2 = box
    max_x, max_y = math.ceil(width), math.ceil(height)
    left = min(max_x, math.floor(x1 * width / REGION_SCALE))
    top = min(max_y, math.floor(y1 * height / REGION_SCALE))
    right = min(max_x, math.ceil(x2 * width / REGION_SCALE))
    bottom = min(max_y, math.ceil(y2 * height / REGION_SCALE))
    return left, top, max(right, left + 1), max(bottom, top + 1)


#: EXIF 方向标记里「宽高互换」的那几个值(转 90 / 270 度)。
_SWAPPED_ORIENTATIONS: Final = frozenset({5, 6, 7, 8})
#: 方向标记 → 把原图转正要做的变换(与 ``ImageOps.exif_transpose`` 同一张表)。
_UPRIGHT_METHODS: Final[dict[int, Image.Transpose]] = {
    2: Image.Transpose.FLIP_LEFT_RIGHT,
    3: Image.Transpose.ROTATE_180,
    4: Image.Transpose.FLIP_TOP_BOTTOM,
    5: Image.Transpose.TRANSPOSE,
    6: Image.Transpose.ROTATE_270,
    7: Image.Transpose.TRANSVERSE,
    8: Image.Transpose.ROTATE_90,
}
#: 打开模型 / 用户给的字节时只认上传白名单这几种格式:别让 Pillow 按魔数去试 TIFF / PSD
#: 之类别的解码器。手机的多图 JPEG(MPO)由 "JPEG" 这一项打开(Pillow 没有单独的 "MPO"
#: 打开项,写进来反而 ``KeyError``)。
OPEN_FORMATS: Final = ("PNG", "JPEG", "WEBP", "GIF")
#: JPEG 缩小解码可选的倍数(libjpeg 的 DCT 缩放),从大往小试。
_JPEG_DRAFT_SCALES: Final = (8, 4, 2)
#: 进程内同时在做的宿主裁图上限:一张 6000 万像素的图解开就是几百 MB,
#: 并行的 ``ask_image`` 不许一起解。阻塞在线程里等(调用方本来就在线程里跑)。
HOST_CROP_SLOTS: Final = 2
_HOST_CROP_SEMAPHORE = threading.BoundedSemaphore(HOST_CROP_SLOTS)


def image_size(data: bytes) -> tuple[int, int] | None:
    """图片转正之后的像素尺寸(只读文件头,不解码);认不出来是 ``None``。"""
    try:
        with Image.open(io.BytesIO(data), formats=OPEN_FORMATS) as image:
            width, height = image.size
            if _orientation(image) in _SWAPPED_ORIENTATIONS:
                return height, width
            return width, height
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError):
        return None


@dataclass(frozen=True)
class CroppedImage:
    data: bytes
    width: int
    height: int


def crop_image(data: bytes, boxes: Sequence[Box]) -> list[CroppedImage]:
    """按原像素裁出每个框(不放大;超过约 1.5 百万像素的缩到那个量),存成 JPEG。

    省内存(一张 6000 万像素的 RGBA PNG 整张转一次就是几百 MB):
    * 进程内最多 :data:`HOST_CROP_SLOTS` 张同时在裁;
    * EXIF 方向不转整张图:把框换算到原图坐标、裁下来之后只转这一小块;
    * 转 RGB / 铺白底只对裁下来的那一小块做;
    * JPEG 在每一块都还够 :data:`REGION_TARGET_PIXELS` 的前提下用 ``draft`` 缩小解码。

    解码前先看像素数:超过 :data:`MAX_DECODE_PIXELS` 不解码,抛 :class:`RegionImageError`。
    同步、吃 CPU —— 调用方放到线程里跑。
    """
    with _HOST_CROP_SEMAPHORE:
        try:
            with Image.open(io.BytesIO(data), formats=OPEN_FORMATS) as image:
                return _crop_open_image(image, boxes)
        except RegionImageError:
            raise
        except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
            msg = f"the image could not be decoded for cropping ({type(exc).__name__})"
            raise RegionImageError(msg) from exc


def _crop_open_image(image: Image.Image, boxes: Sequence[Box]) -> list[CroppedImage]:
    width, height = image.size
    if width * height > MAX_DECODE_PIXELS:
        msg = (
            f"the image is {width}x{height} pixels, over the {MAX_DECODE_PIXELS}-pixel "
            "limit for cropping"
        )
        raise RegionImageError(msg)
    orientation = _orientation(image)
    if isinstance(image, JpegImageFile):
        scale = _draft_scale((width, height), boxes)
        if scale > 1:
            image.draft(image.mode, (-(-width // scale), -(-height // scale)))
    crops: list[CroppedImage] = []
    for box in boxes:
        piece = _flatten(crop_upright(image, box, orientation))
        piece = _cap_pixels(piece)
        buffer = io.BytesIO()
        piece.save(buffer, format="JPEG", quality=CROP_JPEG_QUALITY)
        crops.append(CroppedImage(data=buffer.getvalue(), width=piece.width, height=piece.height))
    return crops


def _orientation(image: Image.Image) -> int:
    value = image.getexif().get(ExifTags.Base.Orientation, 1)
    return value if isinstance(value, int) and value in _UPRIGHT_METHODS else 1


def _draft_scale(size: tuple[int, int], boxes: Sequence[Box]) -> int:
    """JPEG 能缩小解码多少倍:每一块缩小后仍不少于 :data:`REGION_TARGET_PIXELS`(不够就不缩)。"""
    width, height = size
    smallest = min(
        (x2 - x1) * (y2 - y1) * width * height / (REGION_SCALE * REGION_SCALE)
        for x1, y1, x2, y2 in boxes
    )
    for scale in _JPEG_DRAFT_SCALES:
        if smallest / (scale * scale) >= REGION_TARGET_PIXELS:
            return scale
    return 1


def crop_upright(image: Image.Image, box: Box, orientation: int) -> Image.Image:
    """转正之后那张图上的 ``box`` —— 不转整张图:框换算到原图坐标,裁下来再只转这一块。

    与 ``ImageOps.exif_transpose(image).crop(pixel_box(box, 转正后尺寸))`` 逐像素相同
    (测试对 8 种方向钉住)。"""
    width, height = image.size
    swapped = orientation in _SWAPPED_ORIENTATIONS
    upright_size = (height, width) if swapped else (width, height)
    left, top, right, bottom = pixel_box(box, upright_size)
    corners = [
        _raw_point(x, y, orientation, (width, height)) for x, y in ((left, top), (right, bottom))
    ]
    xs, ys = sorted(c[0] for c in corners), sorted(c[1] for c in corners)
    piece = image.crop((xs[0], ys[0], xs[1], ys[1]))
    method = _UPRIGHT_METHODS.get(orientation)
    return piece if method is None else piece.transpose(method)


def _raw_point(x: int, y: int, orientation: int, raw: tuple[int, int]) -> tuple[int, int]:
    """转正后坐标 ``(x, y)`` 在原图上的位置(像素网格的角点,不是像素中心)。"""
    width, height = raw
    return {
        1: (x, y),
        2: (width - x, y),
        3: (width - x, height - y),
        4: (x, height - y),
        5: (y, x),
        6: (y, height - x),
        7: (width - y, height - x),
        8: (width - y, x),
    }[orientation]


def _capped(width: int, height: int) -> tuple[int, int]:
    """超过约 1.5 百万像素就等比缩到那个量(看图模型反正会压);从不放大。"""
    pixels = width * height
    if pixels <= REGION_TARGET_PIXELS:
        return width, height
    factor = math.sqrt(REGION_TARGET_PIXELS / pixels)
    return max(1, math.floor(width * factor)), max(1, math.floor(height * factor))


def _cap_pixels(piece: Image.Image) -> Image.Image:
    size = _capped(piece.width, piece.height)
    return piece if size == piece.size else piece.resize(size, Image.Resampling.LANCZOS)


def crop_output_size(box: Box, upright_size: tuple[int, int]) -> tuple[int, int]:
    """:func:`crop_image` 对这个框出的图有多大(复用已裁好的图时报尺寸用)。"""
    left, top, right, bottom = pixel_box(box, upright_size)
    return _capped(right - left, bottom - top)


def _flatten(image: Image.Image) -> Image.Image:
    """转成 RGB;带透明的铺在白底上(直接 ``convert("RGB")`` 会把透明处变黑)。"""
    if image.mode in ("RGBA", "LA") or (image.mode == "P" and "transparency" in image.info):
        rgba = image.convert("RGBA")
        canvas = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
        return Image.alpha_composite(canvas, rgba).convert("RGB")
    return image.convert("RGB")
