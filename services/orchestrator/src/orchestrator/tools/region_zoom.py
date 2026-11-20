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
#: 横条的上限(review round 2 N2):文档页的横条按 300 dpi 渲,但大幅面的页(A0、海报)会因此出
#: 几千万像素的图 —— 每条不超过 300 万像素、长边不超过 4000 像素(厂商单边上限 8000 留一半余量),
#: 超了就降 dpi。门槛仍按 300 dpi 的页面像素算。
MAX_BAND_PIXELS: Final = 3_000_000
MAX_SIDE_PIXELS: Final = 4_000
#: 宽:高超过 4:1 的图改切竖条(整高、沿宽度切);每条长边:短边尽量不超过 8(条太细时少切几条)。
VERTICAL_BANDS_ASPECT: Final = 4.0
MAX_BAND_ASPECT: Final = 8.0
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


BandAxis = Literal["x", "y"]


def _band_spans(count: int) -> list[tuple[int, int]]:
    """沿一条边切 ``count`` 段,相邻两段重叠不少于段长的 :data:`AUTO_BAND_OVERLAP`
    (起点向下取整、终点向上取整),合起来盖满整条边。"""
    length = REGION_SCALE / (count - (count - 1) * AUTO_BAND_OVERLAP)
    step = length * (1 - AUTO_BAND_OVERLAP)
    spans: list[tuple[int, int]] = []
    for index in range(count):
        start = math.floor(index * step)
        end = (
            REGION_SCALE
            if index == count - 1
            else min(REGION_SCALE, math.ceil(index * step + length))
        )
        spans.append((start, end))
    return spans


def auto_bands(count: int, axis: BandAxis = "y") -> tuple[Tile, ...]:
    """``count`` 条横条(``axis="y"``:整宽、从上到下)或竖条(``axis="x"``:整高、从左到右)。"""
    bands: list[Tile] = []
    for index, (start, end) in enumerate(_band_spans(count)):
        box = (0, start, REGION_SCALE, end) if axis == "y" else (start, 0, end, REGION_SCALE)
        bands.append(Tile(label=f"band {index + 1}", box=box))
    return tuple(bands)


def band_axis(bands: tuple[Tile, ...]) -> BandAxis:
    """这组条是横条(``"y"``,整宽)还是竖条(``"x"``,整高)。"""
    if bands and (bands[0].box[0], bands[0].box[2]) != (0, REGION_SCALE):
        return "x"
    return "y"


def _band_aspect(size: tuple[int, int], count: int, axis: BandAxis) -> float:
    width, height = size
    share = 1 / (count - (count - 1) * AUTO_BAND_OVERLAP)
    band = (width, height * share) if axis == "y" else (width * share, height)
    return max(band) / max(min(band), 1e-9)


def plan_bands(size: tuple[int, int], pixels: int) -> tuple[Tile, ...]:
    """这张图怎么切:宽:高超过 4:1 切竖条,否则横条;条数按像素定,条太细(长:短 > 8)就少切几条,
    最少 :data:`AUTO_MIN_BANDS` 条。``size`` 只用来看比例(文档页给 100 dpi 的整页尺寸即可)。"""
    width, height = size
    axis: BandAxis = "x" if height > 0 and width > VERTICAL_BANDS_ASPECT * height else "y"
    count = band_count(pixels)
    while count > AUTO_MIN_BANDS and _band_aspect(size, count, axis) > MAX_BAND_ASPECT:
        count -= 1
    return auto_bands(count, axis)


def _box_size_at(page_px: tuple[int, int], box: Box, *, base_dpi: int) -> tuple[float, float]:
    """框在 ``base_dpi`` 下的像素宽高。"""
    x1, y1, x2, y2 = box
    return (
        page_px[0] * (x2 - x1) / REGION_SCALE,
        page_px[1] * (y2 - y1) / REGION_SCALE,
    )


def _capped_dpi(page_px: tuple[int, int], box: Box, *, base_dpi: int, pixels: int) -> float:
    """让这个框不超过 ``pixels`` 像素、长边不超过 :data:`MAX_SIDE_PIXELS` 的最大 dpi。

    像素框向外取整会多出一两行像素,所以两条线各留一点余量。"""
    width, height = _box_size_at(page_px, box, base_dpi=base_dpi)
    if width <= 0 or height <= 0:
        return float(MAX_REGION_DPI)
    by_area = base_dpi * math.sqrt(pixels * 0.99 / (width * height))
    by_side = base_dpi * (MAX_SIDE_PIXELS - 4) / max(width, height)
    return min(by_area, by_side)


def band_dpi(page_px: tuple[int, int], box: Box, *, base_dpi: int) -> int:
    """文档页的一条横条按多少 dpi 渲::data:`DOCUMENT_ZOOM_DPI`,超过每条 300 万像素或长边 4000
    像素就降下来。"""
    capped = _capped_dpi(page_px, box, base_dpi=base_dpi, pixels=MAX_BAND_PIXELS)
    return max(1, math.floor(min(DOCUMENT_ZOOM_DPI, capped)))


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
    # 又细又长的框(整宽的一窄条)按面积定会出上万像素宽的图:长边也不超过 4000 像素。
    side = _capped_dpi(page_px, box, base_dpi=base_dpi, pixels=MAX_BAND_PIXELS)
    return max(1, min(MAX_REGION_DPI, math.floor(min(dpi, side))))


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
#: B-301 —— 顺时针多转的角度(转正之后再转)。
ROTATIONS: Final = (0, 90, 180, 270)
#: B-301 —— 方向标记 + 转正之后再顺时针转 ``d`` 度 = 另一个方向标记:``_TURNED[标记][d // 90]``。
#: 方向标记的 8 个值连同四个角度的旋转构成同一个群(正方形的对称群),合成之后仍是这 8 个值之一;
#: 这张表由 Pillow 的 ``transpose`` 逐个对出来,测试对 8 x 4 种组合逐像素钉住。
_TURNED: Final[dict[int, tuple[int, int, int, int]]] = {
    1: (1, 6, 3, 8),
    2: (2, 7, 4, 5),
    3: (3, 8, 1, 6),
    4: (4, 5, 2, 7),
    5: (5, 2, 7, 4),
    6: (6, 3, 8, 1),
    7: (7, 4, 5, 2),
    8: (8, 1, 6, 3),
}
#: 打开模型 / 用户给的字节时只认上传白名单这几种格式:别让 Pillow 按魔数去试 TIFF / PSD
#: 之类别的解码器。手机的多图 JPEG(MPO)由 "JPEG" 这一项打开(Pillow 没有单独的 "MPO"
#: 打开项,写进来反而 ``KeyError``)。
OPEN_FORMATS: Final = ("PNG", "JPEG", "WEBP", "GIF")
#: JPEG 缩小解码可选的倍数(libjpeg 的 DCT 缩放),从大往小试。
_JPEG_DRAFT_SCALES: Final = (8, 4, 2)
#: 进程内同时在做的宿主裁图上限:一张 6000 万像素的图解开就是几百 MB,并行的 ``ask_image``
#: 不许一起解。闸在调用方(``tools.vision``)进线程**之前**用 ``asyncio.Semaphore`` 拿,
#: 线程池里的线程从不阻塞着等(review round 2 g)。
HOST_CROP_SLOTS: Final = 2


def turned_orientation(orientation: int, degrees: int) -> int:
    """B-301 —— 按方向标记 ``orientation`` 转正、再顺时针转 ``degrees`` 度,合成一个方向值。"""
    if degrees not in ROTATIONS:
        msg = f"degrees must be one of {ROTATIONS}, got {degrees!r}"
        raise ValueError(msg)
    return _TURNED[orientation][degrees // 90]


def image_size(data: bytes, degrees: int = 0) -> tuple[int, int] | None:
    """图片转正(B-301:再顺时针转 ``degrees`` 度)之后的像素尺寸(只读文件头,不解码);
    认不出来是 ``None``。"""
    try:
        with Image.open(io.BytesIO(data), formats=OPEN_FORMATS) as image:
            width, height = image.size
            if turned_orientation(_orientation(image), degrees) in _SWAPPED_ORIENTATIONS:
                return height, width
            return width, height
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError):
        return None


def exif_orientation(data: bytes) -> int:
    """B-301 —— 图片自带的方向标记(只读文件头);没有、认不出来都是 1(不用转)。"""
    try:
        with Image.open(io.BytesIO(data), formats=OPEN_FORMATS) as image:
            return _orientation(image)
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError):
        return 1


@dataclass(frozen=True)
class CroppedImage:
    data: bytes
    width: int
    height: int


def crop_image(
    data: bytes,
    boxes: Sequence[Box],
    degrees: int = 0,
    max_pixels: int = REGION_TARGET_PIXELS,
) -> list[CroppedImage]:
    """按原像素裁出每个框(不放大;超过 ``max_pixels``(默认约 1.5 百万像素)的缩到那个量),存成 JPEG。

    B-301 —— ``degrees``:按方向标记转正之后再顺时针转这么多度,框是转完之后那张图上的坐标。
    与方向标记合成一个方向值(:func:`turned_orientation`),所以仍然只转裁下来的那一块。
    B-301 PR 3 —— 接缝重看的框是两条横条的并集,按 :data:`MAX_BAND_PIXELS` 裁,不比横条更糊。

    省内存(一张 6000 万像素的 RGBA PNG 整张转一次就是几百 MB):
    * 进程内最多 :data:`HOST_CROP_SLOTS` 张同时在裁(闸在调用方);
    * EXIF 方向不转整张图:把框换算到原图坐标、裁下来之后只转这一小块;
    * 转 RGB / 铺白底只对裁下来的那一小块做;
    * JPEG 在每一块都还够 :data:`REGION_TARGET_PIXELS` 的前提下用 ``draft`` 缩小解码。

    解码前先看像素数:超过 :data:`MAX_DECODE_PIXELS` 不解码,抛 :class:`RegionImageError`。
    同步、吃 CPU —— 调用方放到线程里跑。
    """
    try:
        with Image.open(io.BytesIO(data), formats=OPEN_FORMATS) as image:
            return _crop_open_image(image, boxes, degrees, max_pixels)
    except RegionImageError:
        raise
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        msg = f"the image could not be decoded for cropping ({type(exc).__name__})"
        raise RegionImageError(msg) from exc


def _crop_open_image(
    image: Image.Image, boxes: Sequence[Box], degrees: int, max_pixels: int
) -> list[CroppedImage]:
    width, height = image.size
    if width * height > MAX_DECODE_PIXELS:
        msg = (
            f"the image is {width}x{height} pixels, over the {MAX_DECODE_PIXELS}-pixel "
            "limit for cropping"
        )
        raise RegionImageError(msg)
    orientation = turned_orientation(_orientation(image), degrees)
    scale = _draft_scale((width, height), boxes, max_pixels)
    if isinstance(image, JpegImageFile):
        if scale > 1:
            # B-322 复审:按下取整请求 —— Pillow 按「原尺寸 // 请求尺寸」选档,向上取整的请求
            # 在宽高不是倍数时会退一档(9487 宽按 1/4 请求实际只按 1/2 解码)。
            image.draft(image.mode, _draft_request((width, height), scale))
    elif scale > 1:
        # B-322 复审:不是 JPEG 的没有解码时缩;解开之后先整数倍缩一次(每一块缩后仍不少于
        # ``max_pixels``,与 JPEG 的 draft 同一判据),再裁 —— 不在整张原尺寸上裁 / 转。
        image.load()
        image = image.reduce(scale)
    crops: list[CroppedImage] = []
    for box in boxes:
        piece = _flatten(crop_upright(image, box, orientation))
        piece = _cap_pixels(piece, max_pixels)
        buffer = io.BytesIO()
        piece.save(buffer, format="JPEG", quality=CROP_JPEG_QUALITY)
        crops.append(CroppedImage(data=buffer.getvalue(), width=piece.width, height=piece.height))
    return crops


def upright_whole(image: Image.Image, degrees: int, max_pixels: int | None = None) -> Image.Image:
    """B-322 —— 整张图按方向标记转正、再顺时针转 ``degrees`` 度(不按框裁;方向表与
    :func:`crop_upright` 同一份,逐像素相同)。

    缩不缩、怎么缩只看 :func:`budget_size`(设计 §8.1 第 5 条再细化 / 三细化,2026-11-20):
    ``max_pixels`` 是这个模型的单图预算;原图像素**超过**它才缩,不超过的一律不缩。超过时优先用
    「解码时就缩」:JPEG 的 ``draft``(1/2、1/4、1/8)或别的格式的整数倍 ``reduce``,结果落在
    [95% 预算, 预算] 里就**直接用,不再缩一次**(48~50 MP 的照片按一半读);都落不进去才最后一次
    LANCZOS 缩到预算(内存贵,调用方先按 :func:`upright_peak_bytes` 估过才会走到这里)。都在转之前做。

    省内存(审查 H1):每做完一步就关掉上一份;不缩、不转时不复制,原样返回 ``image``(已解码)。
    所以调用方之后只能用返回值,不能再用 ``image``。

    解码前先看像素数:超过 :data:`MAX_DECODE_PIXELS` 抛 :class:`RegionImageError`。同步、吃 CPU。"""
    width, height = image.size
    if width * height > MAX_DECODE_PIXELS:
        msg = f"the image is {width}x{height} pixels, over the {MAX_DECODE_PIXELS}-pixel limit"
        raise RegionImageError(msg)
    # 方向标记在缩之前读:缩出来的新图不带 EXIF。
    method = _UPRIGHT_METHODS.get(turned_orientation(_orientation(image), degrees))
    plan = budget_size(width, height, max_pixels, jpeg=isinstance(image, JpegImageFile))
    current = image
    if plan is not None:
        if plan.draft > 1:
            current.draft(current.mode, _draft_request((width, height), plan.draft))
        current.load()
        if plan.reduce > 1:
            current = _replaced(current, current.reduce(plan.reduce))
        if plan.resize and current.width * current.height > plan.max_pixels:
            current = _replaced(current, current.resize(plan.target, Image.Resampling.LANCZOS))
    if method is None:
        current.load()
        return current
    return _replaced(current, current.transpose(method))


#: B-322 —— 按单图预算缩时,结果最低可以到预算的这么多(设计 §8.1 第 5 条三细化,2026-11-20):
#: 让 48~50 MP 的照片能按一半解码(省内存),不必先整张解开再缩。
BUDGET_FLOOR: Final = 0.95
_REDUCE_FACTORS: Final = (2, 3, 4, 5, 6, 7, 8)


@dataclass(frozen=True)
class BudgetPlan:
    """原图超过单图预算时怎么缩(:func:`budget_size` 定)。"""

    max_pixels: int
    #: 需要一次 LANCZOS 时缩到的尺寸(等比、向下取整,不超过预算)。
    target: tuple[int, int]
    #: JPEG 解码时就缩的倍数(1 = 不用 draft)。
    draft: int = 1
    #: 解码之后整数倍缩小的倍数(1 = 不用)。
    reduce: int = 1

    @property
    def resize(self) -> bool:
        """解码时缩 / 整数倍缩都落不进 [95%, 100%] 预算,还要一次 LANCZOS。"""
        return self.draft == 1 and self.reduce == 1


def budget_size(
    width: int, height: int, max_pixels: int | None, *, jpeg: bool = False
) -> BudgetPlan | None:
    """B-322 —— **转正图唯一的缩放判定点**。不超过单图预算(或预算未知)→ ``None``:不缩。超过时:
    JPEG 先找一档 ``draft``(1/2、1/4、1/8),别的格式找一个整数倍 ``reduce``,结果落在
    [:data:`BUDGET_FLOOR` x 预算, 预算] 里就用它、不再缩;都没有才 LANCZOS 缩到 ``target``。"""
    if max_pixels is None or width * height <= max_pixels:
        return None
    factor = math.sqrt(max_pixels / (width * height))
    target = max(1, math.floor(width * factor)), max(1, math.floor(height * factor))
    for scale in (8, 4, 2) if jpeg else ():
        if _in_window(_scaled((width, height), scale), max_pixels):
            return BudgetPlan(max_pixels, target, draft=scale)
    for scale in _REDUCE_FACTORS:
        if _in_window(_scaled((width, height), scale), max_pixels):
            return BudgetPlan(max_pixels, target, reduce=scale)
    return BudgetPlan(max_pixels, target)


def _scaled(size: tuple[int, int], scale: int) -> tuple[int, int]:
    """按 ``scale`` 缩之后的尺寸(JPEG ``draft`` 与 ``reduce`` 都向上取整)。"""
    return -(-size[0] // scale), -(-size[1] // scale)


def _draft_request(size: tuple[int, int], scale: int) -> tuple[int, int]:
    """让 Pillow 的 ``draft`` 正好选 ``scale`` 那一档的请求尺寸:它按 ``原尺寸 // 请求尺寸`` 选,
    所以请求要向下取整(B-322 复审)。"""
    return max(1, size[0] // scale), max(1, size[1] // scale)


def _in_window(size: tuple[int, int], max_pixels: int) -> bool:
    pixels = size[0] * size[1]
    return max_pixels * BUDGET_FLOOR <= pixels <= max_pixels


#: 每个像素在内存里占几个字节(Pillow 的存法:RGB / RGBA / CMYK 都是 4 字节一像素)。
_BYTES_PER_PIXEL: Final = {"1": 1, "L": 1, "P": 1, "I;16": 2, "LA": 4, "RGB": 4, "RGBA": 4}


def crop_peak_bytes(
    width: int,
    height: int,
    mode: str,
    *,
    jpeg: bool,
    encoded_bytes: int,
    max_pixels: int = REGION_TARGET_PIXELS,
) -> int:
    """B-322 复审 —— 解码之前估 :func:`crop_image` 对整张框(问方向的候选 / 小副本)的峰值内存:
    JPEG 按 ``draft`` 之后的那份算三份(解开、裁下、转),别的格式要先整张解开,再加整数倍缩后的三份。
    本机按审查同一方法实测校准:12 MP JPEG 约 41 MiB,20 MP JPEG 约 64 MiB,48.8 MP PNG 约 228 MiB。"""
    bpp = _BYTES_PER_PIXEL.get(mode, 4)
    scale = _draft_scale((width, height), [(0, 0, REGION_SCALE, REGION_SCALE)], max_pixels)
    sw, sh = _scaled((width, height), scale)
    reduced = sw * sh * bpp
    if jpeg:
        return 3 * reduced + encoded_bytes
    return width * height * bpp + 3 * reduced + encoded_bytes


def upright_peak_bytes(
    width: int, height: int, mode: str, max_pixels: int | None, *, jpeg: bool, encoded_bytes: int
) -> int:
    """B-322 审查 H-new —— 解码**之前**估一张全分辨率转正图的峰值内存:解开的那份 + 同时还在的另一份
    (转、整数倍缩或 LANCZOS 的两遍)+ 编码出来的字节。只看文件头(尺寸、颜色模式)与
    :func:`budget_size` 的缩法。按测试 / 本机实测校准:12 MP JPEG 转 90 度 ≈ 2 x 46.5 MiB + 编码。"""
    bpp = _BYTES_PER_PIXEL.get(mode, 4)
    plan = budget_size(width, height, max_pixels, jpeg=jpeg)
    if plan is None:
        return 2 * width * height * bpp + encoded_bytes
    dw, dh = _scaled((width, height), plan.draft)
    decoded = dw * dh * bpp
    if plan.reduce > 1:
        rw, rh = _scaled((dw, dh), plan.reduce)
        return decoded + rw * rh * bpp + encoded_bytes
    if plan.resize:
        tw, th = plan.target
        return decoded + tw * dh * bpp + tw * th * bpp + encoded_bytes
    return 2 * decoded + encoded_bytes


def _replaced(old: Image.Image, new: Image.Image) -> Image.Image:
    """换成新的一份,马上关掉旧的(放掉那份像素)。"""
    old.close()
    return new


def _orientation(image: Image.Image) -> int:
    value = image.getexif().get(ExifTags.Base.Orientation, 1)
    return value if isinstance(value, int) and value in _UPRIGHT_METHODS else 1


def _draft_scale(size: tuple[int, int], boxes: Sequence[Box], max_pixels: int) -> int:
    """JPEG 能缩小解码多少倍:每一块缩小后仍不少于 ``max_pixels``(不够就不缩)。"""
    width, height = size
    smallest = min(
        (x2 - x1) * (y2 - y1) * width * height / (REGION_SCALE * REGION_SCALE)
        for x1, y1, x2, y2 in boxes
    )
    for scale in _JPEG_DRAFT_SCALES:
        if smallest / (scale * scale) >= max_pixels:
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


def _capped(width: int, height: int, max_pixels: int = REGION_TARGET_PIXELS) -> tuple[int, int]:
    """超过 ``max_pixels``(默认约 1.5 百万像素)、或长边超过 :data:`MAX_SIDE_PIXELS`,就等比缩到
    线内(看图模型反正会压);从不放大。"""
    pixels = width * height
    factor = min(
        1.0,
        math.sqrt(max_pixels / pixels) if pixels else 1.0,
        MAX_SIDE_PIXELS / max(width, height, 1),
    )
    if factor >= 1.0:
        return width, height
    return max(1, math.floor(width * factor)), max(1, math.floor(height * factor))


def _cap_pixels(piece: Image.Image, max_pixels: int) -> Image.Image:
    size = _capped(piece.width, piece.height, max_pixels)
    return piece if size == piece.size else piece.resize(size, Image.Resampling.LANCZOS)


def crop_output_size(
    box: Box, upright_size: tuple[int, int], max_pixels: int = REGION_TARGET_PIXELS
) -> tuple[int, int]:
    """:func:`crop_image` 对这个框出的图有多大(复用已裁好的图时报尺寸用)。"""
    left, top, right, bottom = pixel_box(box, upright_size)
    return _capped(right - left, bottom - top, max_pixels)


def _flatten(image: Image.Image) -> Image.Image:
    """转成 RGB;带透明的铺在白底上(直接 ``convert("RGB")`` 会把透明处变黑)。"""
    if image.mode in ("RGBA", "LA") or (image.mode == "P" and "transparency" in image.info):
        rgba = image.convert("RGBA")
        canvas = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
        return Image.alpha_composite(canvas, rgba).convert("RGB")
    return image.convert("RGB")
