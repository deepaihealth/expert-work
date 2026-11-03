"""B-266 —— 看图「放大局部」:``region`` 参数的校验、切块几何、分辨率、汇总,以及图片的宿主侧裁剪。

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
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final, Literal

from PIL import ExifTags, Image, ImageOps, UnidentifiedImageError

#: 坐标尺度:``[x1, y1, x2, y2]`` 都在 0..1000。
REGION_SCALE: Final = 1000
#: 框的最小面积:整图的 1%(``(x2-x1)*(y2-y1) >= 10000``)。更小的框几乎只会是定位错。
MIN_REGION_AREA: Final = REGION_SCALE * REGION_SCALE // 100
#: ``region="auto"``:切 2 列 x 3 行;相邻块重叠 15%(实测 8% 时有目标贴边没认出)。
AUTO_REGION: Final = "auto"
AUTO_COLUMNS: Final = 2
AUTO_ROWS: Final = 3
AUTO_OVERLAP: Final = 0.15
#: 文档页的局部按这个像素数选分辨率(约 1.5 百万像素),上限 400 dpi。
REGION_TARGET_PIXELS: Final = 1_500_000
MAX_REGION_DPI: Final = 400
#: 图片解码上限(像素数)。与上传那一侧(control-plane ``figure_images.MAX_DECODE_PIXELS``,
#: B-263)同一个值;编排层不能 import control-plane,这里另记一份,
#: ``services/control-plane/tests/test_region_zoom_decode_cap.py`` 钉两边相等。
MAX_DECODE_PIXELS: Final = 60_000_000
#: 整页快看一张不小于这么多像素的图时,结果末尾提示可以 ``region="auto"`` 再看(spec §4)。
LARGE_IMAGE_PIXELS: Final = 2_000_000
#: 裁图存成 JPEG 的质量 —— 与 B-263 上传转码同一档。
CROP_JPEG_QUALITY: Final = 92

#: 每块看不到完整答案时要回的那个词。
NOT_FOUND: Final = "NOT_FOUND"
#: 每块的提示词在用户原问题后面追加这一段;问题本身原样不动。
TILE_INSTRUCTION: Final = (
    "This image is one tile cut from a larger image, so the answer may not be in it. "
    f"If the complete answer is not fully visible in this tile, reply with exactly {NOT_FOUND} "
    "and nothing else. Do not guess."
)
#: 整页快看一张文档页或大图之后追加的提示。
REGION_HINT: Final = (
    "(To read small text or exact numbers precisely, call ask_image again on the same image "
    "with region='auto'.)"
)

Box = tuple[int, int, int, int]
Region = Box | Literal["auto"]
AutoOutcome = Literal["single", "conflict", "none"]

_ROW_NAMES: Final = ("top", "middle", "bottom")
_COLUMN_NAMES: Final = ("left", "right")


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
    """``region="auto"`` 的一块:位置名(给模型看)+ 框。"""

    label: str
    box: Box


def _spans(count: int) -> list[tuple[int, int]]:
    """一条边切 ``count`` 段,相邻段重叠不少于 :data:`AUTO_OVERLAP`(起点向下取整、终点向上取整)。"""
    size = REGION_SCALE / (count - (count - 1) * AUTO_OVERLAP)
    step = size * (1 - AUTO_OVERLAP)
    return [
        (math.floor(i * step), min(REGION_SCALE, math.ceil(i * step + size))) for i in range(count)
    ]


def auto_tiles() -> tuple[Tile, ...]:
    """2 列 x 3 行,按行从上到下、行内从左到右。"""
    tiles: list[Tile] = []
    for row, (y1, y2) in zip(_ROW_NAMES, _spans(AUTO_ROWS), strict=True):
        for col, (x1, x2) in zip(_COLUMN_NAMES, _spans(AUTO_COLUMNS), strict=True):
            tiles.append(Tile(label=f"{row}-{col}", box=(x1, y1, x2, y2)))
    return tuple(tiles)


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


def tile_question(question: str) -> str:
    """一块的提示词:用户的问题原样,后面加 :data:`TILE_INSTRUCTION`。"""
    return f"{question}\n\n{TILE_INSTRUCTION}"


def is_not_found(answer: str) -> bool:
    """这一块的回答是不是「没看到」:去掉引号 / 星号后以 ``NOT_FOUND``(或 ``NOT FOUND``)开头。"""
    head = answer.strip().strip("`*_\"'").upper()
    return head.startswith((NOT_FOUND, "NOT FOUND"))


@dataclass(frozen=True)
class TileResult:
    """一块看下来的结果:``answer`` 为 ``None`` 表示这一块的调用失败了(``error`` 是原因)。"""

    tile: Tile
    answer: str | None
    error: str | None = None


@dataclass(frozen=True)
class AutoVerdict:
    outcome: AutoOutcome
    content: str


def _normalize(answer: str) -> str:
    return " ".join(answer.split()).strip().rstrip(".\u3002").casefold()


def aggregate_tiles(results: Sequence[TileResult]) -> AutoVerdict:
    """把各块的回答汇成一个结论(spec §1):单值 / 冲突 / 全无。至少要有一块没失败。"""
    answered = [r for r in results if r.answer is not None]
    failed = [r for r in results if r.answer is None]
    values: dict[str, list[TileResult]] = {}
    for result in answered:
        if result.answer is not None and not is_not_found(result.answer):
            values.setdefault(_normalize(result.answer), []).append(result)
    if not values:
        scope = "every tile" if not failed else "every tile that answered"
        content = (
            f"region='auto': {scope} replied {NOT_FOUND} - none of them could see the complete "
            "answer. The text may be too small to read, or it is not in this image."
        )
        outcome: AutoOutcome = "none"
    elif len(values) == 1:
        group = next(iter(values.values()))
        labels = ", ".join(r.tile.label for r in group)
        others = len(answered) - len(group)
        rest = f"; the other {others} replied {NOT_FOUND}" if others else ""
        content = (
            f"{(group[0].answer or '').strip()}\n\n(region='auto': read in tile {labels}{rest}.)"
        )
        outcome = "single"
    else:
        lines = [
            f"- {', '.join(r.tile.label for r in group)}: {(group[0].answer or '').strip()}"
            for group in values.values()
        ]
        content = (
            "region='auto': the tiles gave conflicting answers:\n"
            + "\n".join(lines)
            + "\nDecide which one is right, or look again with an explicit region around the "
            "part you need."
        )
        outcome = "conflict"
    if failed:
        names = ", ".join(f"{r.tile.label} ({r.error})" for r in failed)
        content += f"\n(These tiles failed and were not read: {names}.)"
    return AutoVerdict(outcome=outcome, content=content)


#: EXIF 方向标记里「宽高互换」的那几个值(转 90 / 270 度)。
_SWAPPED_ORIENTATIONS: Final = frozenset({5, 6, 7, 8})


def image_size(data: bytes) -> tuple[int, int] | None:
    """图片转正之后的像素尺寸(只读文件头,不解码);认不出来是 ``None``。"""
    try:
        with Image.open(io.BytesIO(data)) as image:
            width, height = image.size
            if image.getexif().get(ExifTags.Base.Orientation) in _SWAPPED_ORIENTATIONS:
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
    """按原像素裁出每个框(不放大),存成 JPEG。先按 EXIF 方向转正,与 B-263 上传的处理一致。

    解码前先看像素数:超过 :data:`MAX_DECODE_PIXELS` 不解码,抛 :class:`RegionImageError`。
    同步、吃 CPU —— 调用方放到线程里跑。
    """
    try:
        with Image.open(io.BytesIO(data)) as image:
            width, height = image.size
            if width * height > MAX_DECODE_PIXELS:
                msg = (
                    f"the image is {width}x{height} pixels, over the {MAX_DECODE_PIXELS}-pixel "
                    "limit for cropping"
                )
                raise RegionImageError(msg)
            upright = ImageOps.exif_transpose(image)
            rgb = _flatten(upright)
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        msg = f"the image could not be decoded for cropping ({type(exc).__name__})"
        raise RegionImageError(msg) from exc
    crops: list[CroppedImage] = []
    for box in boxes:
        piece = rgb.crop(pixel_box(box, rgb.size))
        buffer = io.BytesIO()
        piece.save(buffer, format="JPEG", quality=CROP_JPEG_QUALITY)
        crops.append(CroppedImage(data=buffer.getvalue(), width=piece.width, height=piece.height))
    return crops


def _flatten(image: Image.Image) -> Image.Image:
    """转成 RGB;带透明的铺在白底上(直接 ``convert("RGB")`` 会把透明处变黑)。"""
    if image.mode in ("RGBA", "LA") or (image.mode == "P" and "transparency" in image.info):
        rgba = image.convert("RGBA")
        canvas = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
        return Image.alpha_composite(canvas, rgba).convert("RGB")
    return image.convert("RGB")
