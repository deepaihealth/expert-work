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

只转 JPEG(L / RGB / CMYK;设计 §8.1 第 5 条五细化,2026-11-20 用户):别的格式不判方向、按原样发。
字节规则:转正图**不比原图大**;同一输入每次生成的字节完全相同(固定编码参数、不写元数据)——
厂商前缀缓存按字节比。

本模块是叶子:只依赖 Pillow 与 :mod:`orchestrator.tools.region_zoom` 的纯几何函数(函数里延迟导入,
免得适配器层一导入就拉起整个 ``orchestrator.tools``)。
"""

from __future__ import annotations

import asyncio
import functools
import hashlib
import io
import math
import time
import weakref
from collections import OrderedDict
from collections.abc import Callable, Mapping, Sequence
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
#: 一张全分辨率转正图允许的峰值内存(审查 H-new)。取 12 MP 照片那一档:pod 实测 4032 x 3024 转 90 度
#: +96 MiB(2026-11-20,测试环境控制面);12.6 MP(qwen4-max 的单图预算)加编码约 106 MiB。解码**之前**按
#: :func:`full_copy_peak` 估,超过它的图不出全分辨率转正图,改用小副本(第 2 级那条路),计
#: ``peak_over_budget``。控制面主进程的内存常数按它 + 两个缓存区算
#: (``tools/deploy/test_parse_worker_memory_budget.py``)。
FULL_COPY_PEAK_BYTES: Final = 110 * 1024 * 1024
#: 适配器当场重算(``adapter_miss``)最多等解图名额多久(秒)。它发生在 ``_prepare_request`` 里、
#: 算在路由的首 token 计时里(审查 N3):等不到就这一次按原图发,不让首 token 跟着解图排队。
#: 2 秒:一次普通裁图约 0.1~0.3 秒、一张 12 MP 转正图约 0.1~0.2 秒(本机),等两轮还够。
ADAPTER_MISS_SLOT_WAIT_S: Final = 2.0

#: B-322 只转 JPEG(第六轮,2026-11-20 用户:设计 §8.1 第 5 条五细化)—— 内存估得准、实测校准过的
#: 只有它。别的(PNG 各种模式、WebP、GIF、JPEG 的其它颜色模式)不判方向、按原样发、不加说明
#: (计 ``format_unsupported``)。只看文件头判断,不解码。改了它就改
#: ``graph_builder.image_upright.SKIP_RULES_VERSION``(测试钉住)。
UPRIGHT_MODES: Final[Mapping[str, frozenset[str]]] = {
    "JPEG": frozenset({"L", "RGB", "CMYK"}),
    "MPO": frozenset({"L", "RGB", "CMYK"}),
}
#: 问方向时借给适配器的候选图,全进程合计最多这么多字节(复审第四轮):超过就这张图这一步不判、
#: 什么都不记(计 ``candidates_over_cap``,复审第五轮 M2)。算进控制面主进程的内存常数。
LENT_CANDIDATES_CAP_BYTES: Final = 16 * 1024 * 1024
#: 依次用的 JPEG 质量(与上传转码同档:92,超了退 85、75)。
_QUALITIES: Final = (92, 85, 75)
#: 每次缩像素到上一次的 0.8 倍(像素数),最低到小副本的像素数。
_SHRINK_FACTOR: Final = 0.8

ViewOutcome = Literal[
    "hit", "precomputed", "adapter_miss", "adapter_busy", "low_res_copy", "peak_over_budget"
]
main_model_image_view_cache_total = expert_work_counter(
    "expert_work_main_model_image_view_cache_total",
    "B-322 upright copies of Path A images by outcome: hit (the adapter read a precomputed "
    "copy), precomputed (agent_node made one before the model call), adapter_miss (the adapter "
    "had to make it), adapter_busy (the adapter could not get a decode slot within "
    "ADAPTER_MISS_SLOT_WAIT_S and sent the stored image for that one call; the prompt note made "
    "before the call still says the image is shown rotated), low_res_copy (the full copy did "
    "not fit; a ~1.5 MP copy was used) and peak_over_budget (making the full copy would peak "
    "over FULL_COPY_PEAK_BYTES, estimated before decoding; the ~1.5 MP copy was used).",
    ("outcome",),
)


@dataclass(frozen=True)
class ImageView:
    """``image_ref`` 块上的 ``view``:顺时针转多少度、用哪一份(全分辨率 / 小副本 / 问方向的候选),
    以及(只有原图超过主模型单图预算时)缩到的预算 ``max_pixels``。"""

    degrees: int
    variant: ViewVariant = "full"
    max_pixels: int | None = None

    def as_dict(self) -> dict[str, object]:
        out: dict[str, object] = {"degrees": self.degrees, "variant": self.variant}
        if self.max_pixels is not None:
            out["max_pixels"] = self.max_pixels
        return out


def copy_digest(digest: str, max_pixels: int | None) -> str:
    """全分辨率转正图在缓存里的键:按预算缩过的另记一份(不同预算的模型不共用)。"""
    return digest if max_pixels is None else f"{digest}~{max_pixels}"


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
    max_pixels = raw.get("max_pixels")
    if max_pixels is not None and (
        isinstance(max_pixels, bool) or not isinstance(max_pixels, int) or max_pixels <= 0
    ):
        return None
    return ImageView(int(degrees), variant, max_pixels if variant == "full" else None)


def image_digest(data: bytes) -> str:
    """图字节的 sha256(十六进制)。结论、缓存都按它记 —— 方向是图自己的属性。"""
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# 生成
# ---------------------------------------------------------------------------


def upright_copy(
    data: bytes, degrees: int, max_pixels: int | None = None
) -> tuple[str, bytes] | None:
    """原图按方向标记转正、再顺时针转 ``degrees`` 度的整张图 → ``(媒体类型, 字节)``。

    只做 :data:`UPRIGHT_MODES` 里的 JPEG(别的抛 ``RegionImageError``,调用方之前就不会走到这里)。
    像素:不超过单图预算就不缩(设计 §8.1 第 5 条)。存成 JPEG 92;字节超过原图就按 85、75 再存,
    仍超就每次缩到 0.8 倍像素再试、最低到小副本的像素数。还超(实际上碰不到)→ ``None``:
    放弃转正,调用方按原样发并计 ``copy_too_large``。

    内存(审查 H1):解开的原图转完立刻放掉(:func:`~orchestrator.tools.region_zoom.upright_whole`),
    编完就关掉转好的那份。认不出 / 解不开的图抛 ``RegionImageError``。同步、吃 CPU:调用方用
    :func:`in_wide_host_crop_slot` 放到线程里(一张全分辨率转正图占两个解图名额)。"""
    from orchestrator.tools.region_zoom import (  # 延迟导入,见模块说明
        OPEN_FORMATS,
        RegionImageError,
        upright_whole,
    )

    try:
        image = Image.open(io.BytesIO(data), formats=OPEN_FORMATS)
    except (OSError, ValueError, Image.DecompressionBombError) as exc:
        msg = f"the image could not be decoded for turning upright ({type(exc).__name__})"
        raise RegionImageError(msg) from exc
    try:
        source_format = image.format or ""
        if image.mode not in UPRIGHT_MODES.get(source_format, frozenset()):
            msg = f"{source_format} {image.mode} images are not turned upright (B-322)"
            raise RegionImageError(msg)
        whole = upright_whole(image, degrees, max_pixels)
    except RegionImageError:
        image.close()
        raise
    except (OSError, ValueError, Image.DecompressionBombError) as exc:
        image.close()
        msg = f"the image could not be decoded for turning upright ({type(exc).__name__})"
        raise RegionImageError(msg) from exc
    try:
        return _encode_within(whole, len(data))
    finally:
        whole.close()


def _header(data: bytes) -> tuple[int, int, str] | None:
    """只读文件头:``(宽, 高, 颜色模式)``;认不出、或不是 :data:`UPRIGHT_MODES` 里的格式 / 颜色模式
    → ``None``。"""
    from orchestrator.tools.region_zoom import OPEN_FORMATS  # 延迟导入,见模块说明

    try:
        with Image.open(io.BytesIO(data), formats=OPEN_FORMATS) as image:
            (width, height), mode, fmt = image.size, image.mode, image.format or ""
    except (OSError, ValueError, Image.DecompressionBombError):
        return None
    if mode not in UPRIGHT_MODES.get(fmt, frozenset()):
        return None
    return width, height, mode


def skip_reason(data: bytes) -> str | None:
    """这张图 B-322 做不做(只读文件头,不解码):不是 :data:`UPRIGHT_MODES` 里的 JPEG →
    ``"format_unsupported"``;问方向的候选都要超过每张的内存预算 → ``"too_large"``;
    能做 → ``None``。只看图本身(与主模型、单图预算无关)。"""
    header = _header(data)
    if header is None:
        return "format_unsupported"
    if slots_for(*header) is None:
        return "too_large"
    return None


def full_copy_peak(data: bytes, max_pixels: int | None) -> int | None:
    """解码之前估这张图的全分辨率转正图峰值内存(只读文件头;审查 H-new)。认不出 → ``None``。"""
    from orchestrator.tools.region_zoom import upright_peak_bytes  # 延迟导入,见模块说明

    header = _header(data)
    if header is None:
        return None
    width, height, mode = header
    return upright_peak_bytes(width, height, mode, max_pixels, encoded_bytes=len(data))


def full_copy_fits_memory(data: bytes, max_pixels: int | None) -> bool:
    peak = full_copy_peak(data, max_pixels)
    return peak is not None and peak <= FULL_COPY_PEAK_BYTES


def small_copy_slots(data: bytes) -> int | None:
    """问方向的候选 / 小副本(:func:`low_res_copy`,整张框的 ``crop_image``)该占几个解图名额:见
    :func:`slots_for`。不支持的格式 → ``None``。"""
    header = _header(data)
    return None if header is None else slots_for(*header)


def slots_for(width: int, height: int, mode: str) -> int | None:
    """按解码之前估的峰值(:func:`~orchestrator.tools.region_zoom.crop_peak_bytes`,已乘安全系数)
    定占几个解图名额(复审 H-new / 第五轮 M1):不到 :data:`FULL_COPY_PEAK_BYTES` 的一半占 1 个 ——
    两个这样的同时在算,加起来也不超过它;不到它占 2 个(单独在算);更大的 → ``None``:不做,这张图
    不判方向(``too_large``)。``ask_image`` 自己的裁图不走这道估算,不受它约束。"""
    from orchestrator.tools.region_zoom import crop_peak_bytes  # 延迟导入,见模块说明

    peak = crop_peak_bytes(width, height, mode)
    if peak <= FULL_COPY_PEAK_BYTES // 2:
        return 1
    if peak <= FULL_COPY_PEAK_BYTES:
        return 2
    return None


def _encode_within(whole: Image.Image, original_bytes: int) -> tuple[str, bytes] | None:
    """按字节规则编码(见 :func:`upright_copy`):不超过原图字节。"""
    from orchestrator.tools.region_zoom import REGION_TARGET_PIXELS  # 延迟导入,见模块说明

    for quality in _QUALITIES:
        encoded = _encode(whole, quality)
        if len(encoded) <= original_bytes:
            return "image/jpeg", encoded
    pixels = whole.width * whole.height
    while pixels > REGION_TARGET_PIXELS:
        pixels = max(REGION_TARGET_PIXELS, math.floor(pixels * _SHRINK_FACTOR))
        encoded = _encode(_resized(whole, pixels), _QUALITIES[-1])
        if len(encoded) <= original_bytes:
            return "image/jpeg", encoded
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


def _encode(image: Image.Image, quality: int) -> bytes:
    """固定参数编码(同一输入字节每次相同):JPEG 4:2:0、不带元数据;CMYK 先转 RGB。"""
    buffer = io.BytesIO()
    rgb = image if image.mode in ("L", "RGB") else image.convert("RGB")
    rgb.save(buffer, format="JPEG", quality=quality, subsampling=2)
    return buffer.getvalue()


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
        #: (run, 图哈希) → 这张图这个 run 里降到哪一级(小副本 / 按原样)与过期的单调时刻;
        #: run 收尾时清,委派子代(``sse`` 收不到它的尾)靠过期清(审查 L2)。
        self._run_modes: dict[tuple[str, str], tuple[RunMode, float]] = {}

    # -- 读(适配器)--------------------------------------------------------

    def get(self, digest: str, view: ImageView) -> tuple[str, bytes] | None:
        key = (digest, view.degrees)
        if view.variant == "candidate":
            lent = self._lent.get(key)
            return (lent[0], lent[1]) if lent is not None else None
        if view.variant == "low":
            low = self._low.get(key)
            return (low.media_type, low.data) if low is not None else None
        key = (copy_digest(digest, view.max_pixels), view.degrees)
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

    @property
    def lent_bytes(self) -> int:
        """正借给适配器的问方向候选图(有自己的上限 :data:`LENT_CANDIDATES_CAP_BYTES`)。"""
        return sum(len(held[1]) for held in self._lent.values())

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
        self._evict_unpinned(keep=wanted)
        for key in wanted:
            self._full[key].pins += 1
            self._full.move_to_end(key)
        return True

    def offer_full(self, prepared: PreparedView) -> bool:
        """把算好的全分辨率转正图放进来、不钉住(放不下就不放);已有就什么都不做。"""
        if prepared.key in self._full:
            return True
        pinned = sum(len(e.data) for e in self._full.values() if e.pins > 0)
        if pinned + len(prepared.data) > self.full_cap_bytes:
            return False
        self._full[prepared.key] = _FullEntry(prepared.media_type, prepared.data)
        self._evict_unpinned(keep={prepared.key})
        return True

    def fits_full(self, extra_bytes: int) -> bool:
        """再钉住 ``extra_bytes`` 字节放不放得下(只看已钉住的,没钉住的可以挤)。"""
        pinned = sum(len(e.data) for e in self._full.values() if e.pins > 0)
        return pinned + extra_bytes <= self.full_cap_bytes

    def _evict_unpinned(self, keep: set[ViewKey] | frozenset[ViewKey] = frozenset()) -> None:
        """挤掉没钉住的旧条目(最久没用的先挤),直到全分辨率区不超过上限。"""
        total = self.full_bytes
        for key in list(self._full):
            if total <= self.full_cap_bytes:
                break
            entry = self._full[key]
            if entry.pins == 0 and key not in keep:
                total -= len(entry.data)
                del self._full[key]

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
        held = self._run_modes.get((run_id, digest))
        if held is None:
            return None
        if held[1] <= time.monotonic():
            del self._run_modes[(run_id, digest)]
            return None
        return held[0]

    def set_run_mode(self, run_id: str, digest: str, mode: RunMode, expires_at: float) -> None:
        now = time.monotonic()
        for key in [k for k, (_mode, at) in self._run_modes.items() if at <= now]:
            del self._run_modes[key]
        self._run_modes[(run_id, digest)] = (mode, expires_at)

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

    def lend(self, key: ViewKey, media_type: str, data: bytes) -> bool:
        """候选图在问的那一会儿借给适配器(一直借到这张图判完);同一张图被两个对话同时问时按次数借还。
        全进程借出的合计不超过 :data:`LENT_CANDIDATES_CAP_BYTES`:放不下就不借、返回 ``False``
        (调用方让这张图这一步不判)。"""
        held = self._lent.get(key)
        if held is None and self.lent_bytes + len(data) > LENT_CANDIDATES_CAP_BYTES:
            return False
        self._lent[key] = (media_type, data, (held[2] if held is not None else 0) + 1)
        return True

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


async def view_bytes(digest: str, view: ImageView, data: bytes) -> tuple[str, bytes] | None:
    """适配器取一张带 ``view`` 的图:先读缓存(计 ``hit``);没读到就当场算一次(计
    ``adapter_miss``,这一步照样成功)—— 也在解图名额里、在线程里算,不占事件循环(审查 H1);
    算不出来(放弃转正 / 解不开)→ ``None``,按原图发。"""
    cached = UPRIGHT_VIEWS.get(digest, view)
    # 问方向的候选只借不存,不进主模型转正图的缓存计数。
    if view.variant != "candidate":
        outcome: ViewOutcome = "hit" if cached is not None else "adapter_miss"
        main_model_image_view_cache_total.labels(outcome=outcome).inc()
    if cached is not None:
        return cached
    try:
        if view.variant == "full":
            if not full_copy_fits_memory(data, view.max_pixels):
                main_model_image_view_cache_total.labels(outcome="peak_over_budget").inc()
                return None
            return await full_copy_in_slots(
                data,
                view.degrees,
                digest,
                max_pixels=view.max_pixels,
                wait_s=ADAPTER_MISS_SLOT_WAIT_S,
            )
        slots = small_copy_slots(data)
        if slots is None:
            main_model_image_view_cache_total.labels(outcome="peak_over_budget").inc()
            return None
        return await _run_in_slots(
            functools.partial(low_res_copy, data, view.degrees),
            slots=slots,
            wait_s=ADAPTER_MISS_SLOT_WAIT_S,
        )
    except SlotBusyError:
        # 解图名额被别的解图占着:这次按原图发(这一步模型看到的是没转的图),不让首 token 一直等。
        main_model_image_view_cache_total.labels(outcome="adapter_busy").inc()
        return None
    except ValueError:  # RegionImageError:解不开 —— 按原图发
        return None


# ---------------------------------------------------------------------------
# 宿主解图名额
# ---------------------------------------------------------------------------

#: B-266 review round 2 g —— 宿主裁图的进程内名额(``region_zoom.HOST_CROP_SLOTS``),在**进线程
#: 之前**用 ``asyncio.Semaphore`` 拿:线程池里的线程从不阻塞着等。信号量绑定在创建它的循环上,按循环
#: 各一个。``ask_image`` 裁图、B-322 的候选图 / 小副本 / 转正图都共用它。
_HOST_CROP_GATES: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore] = (
    weakref.WeakKeyDictionary()
)
#: 要一次拿两个名额的任务先排这把锁:同一时间只有一个任务在凑两个名额,不会两个任务各拿一个互等。
_WIDE_GATES: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock] = (
    weakref.WeakKeyDictionary()
)


def _slot_count() -> int:
    from orchestrator.tools.region_zoom import HOST_CROP_SLOTS  # 延迟导入,见模块说明

    return HOST_CROP_SLOTS


def _gate() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    gate = _HOST_CROP_GATES.get(loop)
    if gate is None:
        gate = _HOST_CROP_GATES[loop] = asyncio.Semaphore(_slot_count())
    return gate


class SlotBusyError(Exception):
    """在限定时间里没拿到解图名额(只有适配器当场重算会限时,审查 N3)。"""


async def _acquire(gate: asyncio.Semaphore, slots: int, wait_s: float | None) -> int:
    """拿 ``slots`` 个名额(最多 ``HOST_CROP_SLOTS`` 个,审查 N6);``wait_s`` 秒内拿不齐就把拿到的
    还回去、抛 :class:`SlotBusyError`。要一个以上时先排 :data:`_WIDE_GATES`,不会两个任务各拿一个
    互等。出任何错(含取消)都把拿到的还回去。"""
    wanted = max(1, min(slots, _slot_count()))
    held = 0
    try:
        async with asyncio.timeout(wait_s):
            if wanted == 1:
                await gate.acquire()
                return 1
            loop = asyncio.get_running_loop()
            wide = _WIDE_GATES.get(loop)
            if wide is None:
                wide = _WIDE_GATES[loop] = asyncio.Lock()
            async with wide:
                while held < wanted:
                    await gate.acquire()
                    held += 1
            return held
    except TimeoutError as exc:
        for _ in range(held):
            gate.release()
        raise SlotBusyError(f"no host decode slot within {wait_s} s") from exc
    except BaseException:
        for _ in range(held):
            gate.release()
        raise


async def _run_in_slots[T](
    call: Callable[[], T],
    *,
    slots: int,
    wait_s: float | None = None,
    on_result: Callable[[T], None] | None = None,
) -> T:
    """拿名额、在线程里跑 ``call``。线程一旦开跑就跑完(线程停不下来):等它的一方被取消时线程
    照跑,**名额等线程真结束才还**(审查 N1)—— 否则取消之后另一个全分辨率转正图又能开算,两张同时在
    内存里。``on_result`` 在线程跑完、成功时在事件循环里调(取消了也调),用来把算好的结果收进缓存。"""
    gate = _gate()
    held = await _acquire(gate, slots, wait_s)
    task = asyncio.ensure_future(asyncio.to_thread(call))

    def finished(done: asyncio.Future[T]) -> None:
        for _ in range(held):
            gate.release()
        if done.cancelled() or done.exception() is not None:
            return
        if on_result is not None:
            on_result(done.result())

    task.add_done_callback(finished)
    return await asyncio.shield(task)


async def in_host_crop_slots[T](slots: int, call: Callable[[], T]) -> T:
    """在 ``slots`` 个解图名额里、线程中跑 ``call``(按估的峰值内存定占几个,
    见 :func:`small_copy_slots`)。"""
    return await _run_in_slots(call, slots=slots)


async def in_host_crop_slot[**P, T](func: Callable[P, T], *args: P.args, **kwargs: P.kwargs) -> T:
    """在一个宿主解图名额里、线程中跑一次解图 / 裁图(不新增解图并发)。"""
    return await _run_in_slots(functools.partial(func, *args, **kwargs), slots=1)


async def in_wide_host_crop_slot[**P, T](
    func: Callable[P, T], *args: P.args, **kwargs: P.kwargs
) -> T:
    """B-322 —— 占**两个**解图名额跑(全分辨率转正图:一张 12 MP 照片解开 + 转一份,峰值约是一次
    普通裁图的两倍,审查 H1)。所以同一时刻最多一张全分辨率转正图在算。"""
    return await _run_in_slots(functools.partial(func, *args, **kwargs), slots=2)


async def full_copy_in_slots(
    data: bytes,
    degrees: int,
    digest: str,
    *,
    max_pixels: int | None = None,
    wait_s: float | None = None,
) -> tuple[str, bytes] | None:
    """全分辨率转正图(:func:`upright_copy`,占两个名额)。算好了就顺手收进缓存(不钉住)——
    等它的一方中途被取消(首 token 超时换备用、用户取消)也收,下一个要它的不必再算一遍(审查 N1)。"""

    key = (copy_digest(digest, max_pixels), degrees)

    def keep(made: tuple[str, bytes] | None) -> None:
        if made is not None:
            UPRIGHT_VIEWS.offer_full(PreparedView(key, *made))

    return await _run_in_slots(
        functools.partial(upright_copy, data, degrees, max_pixels),
        slots=2,
        wait_s=wait_s,
        on_result=keep,
    )
