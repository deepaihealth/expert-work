"""B-322 PR ① —— 发给主模型的「转正图」:怎么生成、多大、放在哪(设计 §4.1.5)。

主模型自己看图(Path A)时,横 / 倒的上传图只在**提示词视图**里换成转正的图:块仍指向原图 ref,
只多一个 ``view``(``{"degrees": 90, "variant": "full"}``)。转正图不落盘、不进沙箱,只在本进程
内存里:

* ``agent_node`` 在调模型**之前**生成好、放进 :class:`UprightViewCache` 并「钉住」整步(适配器
  解析图片算在路由的首 token 超时里,不能在那里现算 12 MP 的图);适配器
  (``multimodal.resolve_message_images``)只读。没读到(只有钉住失效之类的异常情况)才当场算
  一次、计 ``adapter_miss``。
* 全分辨率区(:data:`FULL_VIEW_CACHE_BYTES`)按最近使用淘汰,钉住的也算在上限里,从不超过上限;
  一步要用的条目一次拿齐(全拿到或一个都不拿)。放不下就等最多 :data:`PIN_WAIT_S` 秒,再不行改用
  约 150 万像素的小副本(:data:`LOW_VIEW_CACHE_BYTES` 的单独小区,条目归属于 run);小区也满 →
  这张图在这个 run 里按原样发。
* 问方向的候选图(约 150 万像素)也在内存里生成,只在问的那一会儿「借」给适配器
  (:meth:`UprightViewCache.lend`),不缓存。

字节规则:转正图**不比原图大**(PNG / GIF 例外:不超过 ``min(原图 x 1.1, 4,900,000 字节)``);
同一输入每次生成的字节完全相同(固定编码参数、不写元数据)—— 厂商前缀缓存按字节比。

本模块是叶子:只依赖 Pillow 与 :mod:`orchestrator.tools.region_zoom` 的纯几何函数(函数里延迟导入,
免得适配器层一导入就拉起整个 ``orchestrator.tools``)。
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import math
import time
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final, Literal, get_args

from PIL import Image

from expert_work.common.observability import expert_work_counter

#: ``image_ref`` 块上表示「按多少度、用哪一份显示」的键。
VIEW_KEY: Final = "view"
ViewVariant = Literal["full", "low", "candidate"]
_VARIANTS: Final = frozenset(get_args(ViewVariant))
_DEGREES: Final = frozenset({90, 180, 270})

#: 全分辨率转正图区的上限(按「同时活跃的带横图对话数」估,§4.1.5;pod 实测后再调)。
FULL_VIEW_CACHE_BYTES: Final = 32 * 1024 * 1024
#: 小副本区的上限(第 2 级退路)。两个区加起来 36 MiB,是硬上限。
LOW_VIEW_CACHE_BYTES: Final = 4 * 1024 * 1024
#: 全分辨率区放不下时最多等多久(等别的步骤结束、钉住解除)。
PIN_WAIT_S: Final = 5.0
_PIN_POLL_S: Final = 0.05
#: 没有截止时刻的 run(单测 / 评测 / 不限时)小副本最多留多久;正常由 run 收尾释放。
LOW_COPY_FALLBACK_TTL_S: Final = 3600.0

#: 有损格式依次用的质量(与上传转码同档:JPEG 92 / WebP 90,超了退 85、75)。
_FIRST_QUALITY: Final = {"JPEG": 92, "WEBP": 90}
_FALLBACK_QUALITIES: Final = (85, 75)
#: 每次缩像素到上一次的 0.8 倍(像素数),最低到小副本的像素数。
_SHRINK_FACTOR: Final = 0.8
#: PNG / GIF 无损,转完字节只因压缩顺序略有出入:允许到 ``min(原图 x 1.1, 4,900,000)``(十进制
#: 4.9 MB,低于 Anthropic 的 5 MB;5 MiB 会超过它)。
LOSSLESS_GROWTH: Final = 1.1
LOSSLESS_MAX_BYTES: Final = 4_900_000
_MEDIA_TYPES: Final = {
    "JPEG": "image/jpeg",
    "MPO": "image/jpeg",
    "PNG": "image/png",
    "WEBP": "image/webp",
    "GIF": "image/gif",
}

ViewOutcome = Literal["hit", "precomputed", "adapter_miss", "low_res_copy"]
main_model_image_view_cache_total = expert_work_counter(
    "expert_work_main_model_image_view_cache_total",
    "B-322 upright copies of Path A images by outcome: hit (the adapter read a precomputed "
    "copy), precomputed (agent_node made one before the model call), adapter_miss (the adapter "
    "had to make it) and low_res_copy (the full copy did not fit; a ~1.5 MP copy was used).",
    ("outcome",),
)


@dataclass(frozen=True)
class ImageView:
    """``image_ref`` 块上的 ``view``:顺时针转多少度、用哪一份(全分辨率 / 小副本 / 问方向的候选)。"""

    degrees: int
    variant: ViewVariant = "full"

    def as_dict(self) -> dict[str, object]:
        return {"degrees": self.degrees, "variant": self.variant}


def parse_image_view(raw: object) -> ImageView | None:
    """块上的 ``view``;没有或不认识(被改坏)就是 ``None`` —— 按原图发。"""
    if not isinstance(raw, Mapping):
        return None
    degrees, variant = raw.get("degrees"), raw.get("variant", "full")
    if isinstance(degrees, bool) or variant not in _VARIANTS:
        return None
    # 0 度只对问方向的候选图有意义(约 150 万像素、按方向标记转正的那张)。
    if degrees not in _DEGREES and not (variant == "candidate" and degrees == 0):
        return None
    return ImageView(int(degrees), variant)


def image_digest(data: bytes) -> str:
    """图字节的 sha256(十六进制)。结论、缓存都按它记 —— 方向是图自己的属性。"""
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# 生成
# ---------------------------------------------------------------------------


def upright_copy(data: bytes, degrees: int) -> tuple[str, bytes] | None:
    """原图按方向标记转正、再顺时针转 ``degrees`` 度的整张图 → ``(媒体类型, 字节)``。

    像素:原像素,超过 1,200 万 / 长边 4,000 才缩。格式跟原图走(JPEG 92 / WebP 90 / PNG 最高压缩 /
    GIF)。字节:有损格式超过原图就按 85、75 再存,仍超就每次缩到 0.8 倍像素再试、最低到小副本的
    像素数;无损格式不超过 ``max(原图, min(原图 x 1.1, 4,900,000))`` 就照发,不缩像素,超了才缩。
    还超(实际上碰不到)→ ``None``:放弃转正,调用方按原样发并计 ``copy_too_large``。

    认不出 / 解不开的图抛 ``RegionImageError``。同步、吃 CPU,调用方放到线程里(占解图名额)。"""
    from orchestrator.tools.region_zoom import (  # 延迟导入,见模块说明
        OPEN_FORMATS,
        REGION_TARGET_PIXELS,
        UPRIGHT_COPY_MAX_PIXELS,
        RegionImageError,
        upright_whole,
    )

    try:
        with Image.open(io.BytesIO(data), formats=OPEN_FORMATS) as image:
            source_format = image.format or "PNG"
            whole = upright_whole(image, degrees, UPRIGHT_COPY_MAX_PIXELS)
    except RegionImageError:
        raise
    except (OSError, ValueError, Image.DecompressionBombError) as exc:
        msg = f"the image could not be decoded for turning upright ({type(exc).__name__})"
        raise RegionImageError(msg) from exc
    fmt = "JPEG" if source_format == "MPO" else source_format
    media_type = _MEDIA_TYPES.get(fmt, "image/png")
    if fmt in _FIRST_QUALITY:
        limit = len(data)
        qualities: tuple[int | None, ...] = (_FIRST_QUALITY[fmt], *_FALLBACK_QUALITIES)
    else:
        limit = max(len(data), min(math.floor(len(data) * LOSSLESS_GROWTH), LOSSLESS_MAX_BYTES))
        qualities = (None,)
    for quality in qualities:
        encoded = _encode(whole, fmt, quality)
        if len(encoded) <= limit:
            return media_type, encoded
    pixels = whole.width * whole.height
    while pixels > REGION_TARGET_PIXELS:
        pixels = max(REGION_TARGET_PIXELS, math.floor(pixels * _SHRINK_FACTOR))
        encoded = _encode(_resized(whole, pixels), fmt, qualities[-1])
        if len(encoded) <= limit:
            return media_type, encoded
    return None


def low_res_copy(data: bytes, degrees: int) -> tuple[str, bytes]:
    """约 150 万像素的转正图(JPEG 92)—— 与问方向的候选图同一个函数、同样大小。小副本(第 2 级
    退路)与候选图都用它。同步、吃 CPU。"""
    from orchestrator.tools.region_zoom import crop_image  # 延迟导入,见模块说明

    return "image/jpeg", crop_image(data, [(0, 0, 1000, 1000)], degrees)[0].data


def _resized(image: Image.Image, pixels: int) -> Image.Image:
    factor = math.sqrt(pixels / (image.width * image.height))
    size = (max(1, math.floor(image.width * factor)), max(1, math.floor(image.height * factor)))
    return image.resize(size, Image.Resampling.LANCZOS)


def _encode(image: Image.Image, fmt: str, quality: int | None) -> bytes:
    """固定参数编码(同一输入字节每次相同):不带元数据;JPEG 4:2:0、WebP 默认方法、PNG 最高压缩。"""
    buffer = io.BytesIO()
    if fmt == "JPEG":
        rgb = image if image.mode in ("L", "RGB") else _flattened(image)
        rgb.save(buffer, format="JPEG", quality=quality, subsampling=2)
    elif fmt == "WEBP":
        has_alpha = image.mode in ("RGBA", "LA") or "transparency" in image.info
        image.convert("RGBA" if has_alpha else "RGB").save(buffer, format="WEBP", quality=quality)
    elif fmt == "GIF":
        image.save(buffer, format="GIF")
    else:
        image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def _flattened(image: Image.Image) -> Image.Image:
    """转成 RGB;带透明的铺在白底上。"""
    if image.mode in ("RGBA", "LA") or (image.mode == "P" and "transparency" in image.info):
        rgba = image.convert("RGBA")
        canvas = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
        return Image.alpha_composite(canvas, rgba).convert("RGB")
    return image.convert("RGB")


# ---------------------------------------------------------------------------
# 缓存与钉住
# ---------------------------------------------------------------------------

ViewKey = tuple[str, int]


@dataclass
class _FullEntry:
    media_type: str
    data: bytes
    pins: int = 0


@dataclass
class _LowEntry:
    media_type: str
    data: bytes
    #: 拥有它的 run → 过期的单调时刻。没有拥有者了就删。
    owners: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class PreparedView:
    """一张要钉住的转正图:键 + 已生成好的字节。"""

    key: ViewKey
    media_type: str
    data: bytes


RunMode = Literal["low", "raw"]


class UprightViewCache:
    """进程内的转正图缓存(设计 §4.1.5)。只在一个事件循环里用;所有「查 + 改」之间没有 ``await``,
    所以不用锁:一步的条目要么全拿到、要么一个都不拿,不会两个步骤各拿一半互相等。"""

    def __init__(
        self,
        full_cap_bytes: int = FULL_VIEW_CACHE_BYTES,
        low_cap_bytes: int = LOW_VIEW_CACHE_BYTES,
    ) -> None:
        self.full_cap_bytes = full_cap_bytes
        self.low_cap_bytes = low_cap_bytes
        self._full: OrderedDict[ViewKey, _FullEntry] = OrderedDict()
        self._low: dict[ViewKey, _LowEntry] = {}
        self._lent: dict[ViewKey, tuple[str, bytes, int]] = {}
        #: (run, 图哈希) → 这张图这个 run 里降到哪一级(小副本 / 按原样);run 收尾时清。
        self._run_modes: dict[tuple[str, str], RunMode] = {}

    # -- 读(适配器)--------------------------------------------------------

    def get(self, digest: str, view: ImageView) -> tuple[str, bytes] | None:
        key = (digest, view.degrees)
        if view.variant == "candidate":
            lent = self._lent.get(key)
            return (lent[0], lent[1]) if lent is not None else None
        if view.variant == "low":
            low = self._low.get(key)
            return (low.media_type, low.data) if low is not None else None
        entry = self._full.get(key)
        if entry is None:
            return None
        self._full.move_to_end(key)
        return entry.media_type, entry.data

    def has_full(self, key: ViewKey) -> bool:
        return key in self._full

    def has_low(self, key: ViewKey) -> bool:
        return key in self._low

    # -- 全分辨率区 --------------------------------------------------------

    @property
    def full_bytes(self) -> int:
        return sum(len(e.data) for e in self._full.values())

    @property
    def low_bytes(self) -> int:
        return sum(len(e.data) for e in self._low.values())

    def try_pin_full(self, items: Sequence[PreparedView]) -> bool:
        """把 ``items`` 一次全部钉住(已在缓存里的只加钉,不在的放进去);放不下就一个都不钉、
        返回 ``False``。放得下时先挤掉没钉住的旧条目(最久没用的先挤),缓存永不超过上限。"""
        new = list(items)
        wanted = {p.key for p in new}
        needed = sum(len(p.data) for p in new if p.key not in self._full)
        needed += sum(len(e.data) for k, e in self._full.items() if e.pins > 0 or k in wanted)
        if needed > self.full_cap_bytes:
            return False
        for prepared in new:
            if prepared.key not in self._full:
                self._full[prepared.key] = _FullEntry(prepared.media_type, prepared.data)
        total = self.full_bytes
        for key in list(self._full):
            if total <= self.full_cap_bytes:
                break
            entry = self._full[key]
            if entry.pins == 0 and key not in wanted:
                total -= len(entry.data)
                del self._full[key]
        for key in wanted:
            self._full[key].pins += 1
            self._full.move_to_end(key)
        return True

    def unpin_full(self, keys: Sequence[ViewKey]) -> None:
        for key in keys:
            entry = self._full.get(key)
            if entry is not None and entry.pins > 0:
                entry.pins -= 1

    async def pin_full(self, items: Sequence[PreparedView], wait_s: float = PIN_WAIT_S) -> bool:
        """:meth:`try_pin_full`,放不下就等(最多 ``wait_s`` 秒)别的步骤解除钉住再试。"""
        give_up_at = time.monotonic() + wait_s
        while True:
            if self.try_pin_full(items):
                return True
            if time.monotonic() >= give_up_at:
                return False
            await asyncio.sleep(_PIN_POLL_S)

    # -- 小副本区(按 run 归属)-------------------------------------------

    def hold_low(self, run_id: str, new: Sequence[PreparedView], expires_at: float) -> bool:
        """``new`` 一次全部归给 ``run_id``(已有同键的只加拥有者);放不下就一个都不放。"""
        self._expire_low()
        needed = sum(len(p.data) for p in new if p.key not in self._low)
        if self.low_bytes + needed > self.low_cap_bytes:
            return False
        for prepared in new:
            entry = self._low.get(prepared.key)
            if entry is None:
                entry = self._low[prepared.key] = _LowEntry(prepared.media_type, prepared.data)
            entry.owners[run_id] = max(entry.owners.get(run_id, 0.0), expires_at)
        return True

    def _expire_low(self) -> None:
        now = time.monotonic()
        for key in list(self._low):
            entry = self._low[key]
            entry.owners = {run: at for run, at in entry.owners.items() if at > now}
            if not entry.owners:
                del self._low[key]

    # -- run 级的降级决定 ---------------------------------------------------

    def run_mode(self, run_id: str, digest: str) -> RunMode | None:
        return self._run_modes.get((run_id, digest))

    def set_run_mode(self, run_id: str, digest: str, mode: RunMode) -> None:
        self._run_modes[(run_id, digest)] = mode

    def release_run(self, run_id: str) -> None:
        """run 收尾(正常结束、出错、被取消都走):它的小副本与降级决定一并释放。"""
        for key in list(self._low):
            entry = self._low[key]
            entry.owners.pop(run_id, None)
            if not entry.owners:
                del self._low[key]
        for run_and_digest in [k for k in self._run_modes if k[0] == run_id]:
            del self._run_modes[run_and_digest]

    # -- 问方向的候选图(只借不存)-----------------------------------------

    def lend(self, key: ViewKey, media_type: str, data: bytes) -> None:
        """候选图在问的那一会儿借给适配器;同一张图被两个对话同时问时按次数借还。"""
        held = self._lent.get(key)
        self._lent[key] = (media_type, data, (held[2] if held is not None else 0) + 1)

    def give_back(self, key: ViewKey) -> None:
        held = self._lent.get(key)
        if held is None:
            return
        if held[2] <= 1:
            del self._lent[key]
        else:
            self._lent[key] = (held[0], held[1], held[2] - 1)

    def clear(self) -> None:
        """测试用:清空一切。"""
        self._full.clear()
        self._low.clear()
        self._lent.clear()
        self._run_modes.clear()


#: 进程内唯一的一份:``agent_node`` 写、两个适配器读、run 收尾释放。
UPRIGHT_VIEWS: Final = UprightViewCache()


def release_run_views(run_id: object) -> None:
    """``sse.run_agent`` 收尾时调用(B-322 §4.1.5 第 2 级)。"""
    UPRIGHT_VIEWS.release_run(str(run_id))


def view_bytes(digest: str, view: ImageView, data: bytes) -> tuple[str, bytes] | None:
    """适配器取一张带 ``view`` 的图:先读缓存(计 ``hit``);没读到就当场算一次(计
    ``adapter_miss``,这一步照样成功);算不出来(放弃转正 / 解不开)→ ``None``,按原图发。"""
    cached = UPRIGHT_VIEWS.get(digest, view)
    # 问方向的候选只借不存,不进主模型转正图的缓存计数。
    if view.variant != "candidate":
        outcome: ViewOutcome = "hit" if cached is not None else "adapter_miss"
        main_model_image_view_cache_total.labels(outcome=outcome).inc()
    if cached is not None:
        return cached
    try:
        if view.variant == "full":
            return upright_copy(data, view.degrees)
        return low_res_copy(data, view.degrees)
    except ValueError:  # RegionImageError:解不开 —— 按原图发
        return None
