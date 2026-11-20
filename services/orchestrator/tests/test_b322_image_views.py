"""B-322 PR ① —— 转正图的生成、字节规则与进程内缓存(设计 §4.1.5、§6.1 字节 / 钉住各条)。"""

from __future__ import annotations

import asyncio
import io
import time
import weakref
from typing import Any

import pytest
from langchain_core.messages import HumanMessage
from PIL import Image, ImageOps

from orchestrator import image_views
from orchestrator.image_views import (
    ImageView,
    PreparedView,
    UprightViewCache,
    image_digest,
    low_res_copy,
    parse_image_view,
    upright_copy,
)
from orchestrator.llm.providers import anthropic as anthropic_adapter
from orchestrator.llm.providers import openai as openai_adapter
from orchestrator.multimodal import (
    InMemoryImageResolver,
    ResolvedImage,
    resolve_message_images,
    split_human_views,
)

_REF = "expert_work://image/00000000-0000-4000-8000-000000000001/00000000-0000-4000-8000-000000000002/00000000-0000-4000-8000-000000000003.png"


def _quadrants() -> Image.Image:
    image = Image.new("RGB", (600, 400), (255, 255, 255))
    image.paste((255, 0, 0), (0, 0, 300, 200))
    image.paste((0, 160, 0), (300, 0, 600, 200))
    image.paste((0, 0, 255), (0, 200, 300, 400))
    return image


def _encode(image: Image.Image, fmt: str, **kw: Any) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format=fmt, **kw)
    return buffer.getvalue()


def _quad_jpeg() -> bytes:
    """B-322 只转 JPEG(第六轮):测试图存成 JPEG 100(4:4:4)(转正图按 92 存,不比原图大)。"""
    return _encode(_quadrants(), "JPEG", quality=100, subsampling=0)


def _decode(data: bytes) -> Image.Image:
    image = Image.open(io.BytesIO(data))
    image.load()
    return image


@pytest.fixture(autouse=True)
def _fresh_cache() -> Any:
    image_views.UPRIGHT_VIEWS.clear()
    yield
    image_views.UPRIGHT_VIEWS.clear()


# ---------------------------------------------------------------------------
# 生成
# ---------------------------------------------------------------------------


def _as_copy(image: Image.Image) -> bytes:
    """转正图的编码参数(JPEG 92、4:2:0、不带元数据)。"""
    return _encode(image, "JPEG", quality=92, subsampling=2)


@pytest.mark.parametrize("degrees", [90, 180, 270])
def test_jpeg_copy_is_the_turned_original(degrees: int) -> None:
    raw = _quad_jpeg()
    media, data = upright_copy(raw, degrees) or ("", b"")
    assert media == "image/jpeg"
    # 顺时针转 N 度 = PIL 的 rotate(-N);同样的编码参数,字节一样。
    assert data == _as_copy(_decode(raw).rotate(-degrees, expand=True))


@pytest.mark.parametrize("exif", [3, 6, 8])
def test_exif_orientation_is_applied_before_the_turn(exif: int) -> None:
    """方向标记先转正,再顺时针转 N 度(与 ``crop_upright`` 整张框的结果、与 B-301 候选同一口径)。"""
    tags = Image.Exif()
    tags[0x0112] = exif
    raw = _encode(_quadrants(), "JPEG", quality=100, subsampling=0, exif=tags.tobytes())
    _media, data = upright_copy(raw, 90) or ("", b"")
    expected = ImageOps.exif_transpose(_decode(raw)).rotate(-90, expand=True)
    assert _decode(data).tobytes() == _decode(_as_copy(expected)).tobytes()


def test_same_input_gives_the_same_bytes_every_time() -> None:
    raw = _encode(_photo(1200, 900), "JPEG", quality=95)
    assert upright_copy(raw, 90) == upright_copy(raw, 90)
    assert low_res_copy(raw, 90) == low_res_copy(raw, 90)


def _photo(width: int, height: int) -> Image.Image:
    """像照片的合成图:渐变 + 噪点(Pillow 的 C 实现,12 MP 也快)。"""
    gradient = Image.linear_gradient("L").resize((width, height))
    noise = Image.effect_noise((width, height), 40)
    return Image.merge("RGB", (gradient, noise, Image.blend(gradient, noise, 0.5)))


def test_jpeg_copy_never_larger_than_the_original() -> None:
    """高压缩比的 JPEG:按 92 重存会变大 → 退质量 / 缩像素,字节 ≤ 原图。"""
    raw = _encode(_photo(3000, 2000), "JPEG", quality=40)
    assert len(_encode(_photo(3000, 2000), "JPEG", quality=92)) > len(raw)
    found = upright_copy(raw, 90)
    assert found is not None
    media, data = found
    assert media == "image/jpeg"
    assert len(data) <= len(raw)


def test_copy_is_never_downscaled_below_the_originals_size() -> None:
    """设计 §8.1 第 5 条细化(2026-11-20,用户;审查 H1):转正图与原图一样多像素,不缩 —— 原图今天
    就是这么大发给厂商的。4032 x 3024 的手机照片转 90 度是 3024 x 4032,不是 3000 x 4000。"""
    raw = _encode(Image.new("RGB", (4032, 3024), (10, 20, 30)), "JPEG", quality=92)
    found = upright_copy(raw, 90)
    assert found is not None
    assert _decode(found[1]).size == (3024, 4032)


def test_a_copy_that_cannot_fit_gives_up(monkeypatch: pytest.MonkeyPatch) -> None:
    """怎么退都超 → ``None``(调用方按原样发、计 ``copy_too_large``)。"""
    raw = _encode(_photo(1600, 1200), "JPEG", quality=40)
    monkeypatch.setattr(image_views, "_encode", lambda image, q: b"x" * (len(raw) + 1))
    assert upright_copy(raw, 90) is None


def test_parse_image_view() -> None:
    assert parse_image_view({"degrees": 90, "variant": "full"}) == ImageView(90, "full")
    assert parse_image_view({"degrees": 180}) == ImageView(180, "full")
    assert parse_image_view({"degrees": 45}) is None
    assert parse_image_view({"degrees": True}) is None
    assert parse_image_view({"degrees": 90, "variant": "x"}) is None
    assert parse_image_view(None) is None
    assert parse_image_view({"degrees": 0, "variant": "candidate"}) == ImageView(0, "candidate")
    assert parse_image_view({"degrees": 0, "variant": "full"}) is None


# ---------------------------------------------------------------------------
# 适配器只读;没读到当场算
# ---------------------------------------------------------------------------


def _resolver(data: bytes) -> InMemoryImageResolver:
    return InMemoryImageResolver({_REF: ResolvedImage(media_type="image/png", data=data)})


def _block(view: dict[str, Any] | None) -> dict[str, Any]:
    block: dict[str, Any] = {"type": "image_ref", "ref": _REF}
    if view is not None:
        block["view"] = view
    return block


def _count(outcome: str) -> float:
    return image_views.main_model_image_view_cache_total.labels(outcome=outcome)._value.get()


async def test_adapter_reads_the_precomputed_copy() -> None:
    raw = _quad_jpeg()
    media, copy = upright_copy(raw, 90) or ("", b"")
    key = (image_digest(raw), 90)
    assert image_views.UPRIGHT_VIEWS.try_pin_full([PreparedView(key, media, copy)])
    before_hit, before_miss = _count("hit"), _count("adapter_miss")
    out = await resolve_message_images(
        [_REF], _resolver(raw), split_human_views([_block({"degrees": 90, "variant": "full"})])
    )
    assert isinstance(out[0], ResolvedImage)
    assert out[0].data == copy
    assert _count("hit") == before_hit + 1
    assert _count("adapter_miss") == before_miss


async def test_adapter_miss_computes_and_still_succeeds() -> None:
    raw = _quad_jpeg()
    before = _count("adapter_miss")
    out = await resolve_message_images([_REF], _resolver(raw), [ImageView(90)])
    assert isinstance(out[0], ResolvedImage)
    assert out[0].data == (upright_copy(raw, 90) or ("", b""))[1]
    assert _count("adapter_miss") == before + 1


async def test_low_variant_is_read_as_low_even_when_full_exists() -> None:
    raw = _quad_jpeg()
    key = (image_digest(raw), 90)
    full = upright_copy(raw, 90) or ("", b"")
    low = low_res_copy(raw, 90)
    cache = image_views.UPRIGHT_VIEWS
    assert cache.try_pin_full([PreparedView(key, *full)])
    assert cache.hold_low("run-1", [PreparedView(key, *low)], time.monotonic() + 60)
    out = await resolve_message_images([_REF], _resolver(raw), [ImageView(90, "low")])
    assert isinstance(out[0], ResolvedImage)
    assert out[0].data == low[1]


async def test_block_without_view_is_the_original() -> None:
    raw = _quad_jpeg()
    out = await resolve_message_images([_REF], _resolver(raw), split_human_views([_block(None)]))
    assert isinstance(out[0], ResolvedImage)
    assert out[0].data == raw


@pytest.mark.parametrize("adapter", [openai_adapter, anthropic_adapter])
async def test_both_adapters_pass_the_view(adapter: Any) -> None:
    raw = _quad_jpeg()
    msg = HumanMessage(content=[{"type": "text", "text": "hi"}, _block({"degrees": 180})])
    blocks = await adapter._human_content(msg, _resolver(raw))
    rotated = (upright_copy(raw, 180) or ("", b""))[1]
    wire = str(blocks)
    expected = ResolvedImage(media_type="image/png", data=rotated).base64_data
    assert expected in wire
    assert ResolvedImage(media_type="image/png", data=raw).base64_data not in wire


# ---------------------------------------------------------------------------
# 缓存:钉住、上限、小副本区
# ---------------------------------------------------------------------------


def _prepared(name: str, size: int) -> PreparedView:
    return PreparedView((name, 90), "image/jpeg", b"x" * size)


def test_pin_is_all_or_nothing_and_never_over_the_cap() -> None:
    cache = UprightViewCache(full_cap_bytes=100, low_cap_bytes=10)
    assert cache.try_pin_full([_prepared("a", 60)])
    # 第二步要 b + c 共 70:加上钉住的 a 超了 → 一个都不拿。
    assert not cache.try_pin_full([_prepared("b", 35), _prepared("c", 35)])
    assert not cache.has_full(("b", 90)) and not cache.has_full(("c", 90))
    assert cache.full_bytes <= 100
    cache.unpin_full([("a", 90)])
    # a 解除钉住后可以被挤掉:b + c 一次拿齐。
    assert cache.try_pin_full([_prepared("b", 35), _prepared("c", 35)])
    assert not cache.has_full(("a", 90))
    assert cache.full_bytes <= 100


async def test_waiting_step_gets_in_when_the_other_releases() -> None:
    cache = UprightViewCache(full_cap_bytes=100, low_cap_bytes=10)
    assert cache.try_pin_full([_prepared("a", 80)])

    async def release_soon() -> None:
        await asyncio.sleep(0.1)
        cache.unpin_full([("a", 90)])

    task = asyncio.create_task(release_soon())
    assert await cache.pin_full([_prepared("b", 80)], wait_s=2.0)
    await task


async def test_waiting_gives_up_after_the_wait() -> None:
    cache = UprightViewCache(full_cap_bytes=100, low_cap_bytes=10)
    assert cache.try_pin_full([_prepared("a", 80)])
    started = time.monotonic()
    assert not await cache.pin_full([_prepared("b", 80)], wait_s=0.2)
    assert time.monotonic() - started >= 0.2


def test_low_region_belongs_to_runs() -> None:
    cache = UprightViewCache(full_cap_bytes=10, low_cap_bytes=100)
    far = time.monotonic() + 600
    assert cache.hold_low("run-1", [_prepared("a", 60)], far)
    assert not cache.hold_low("run-2", [_prepared("b", 60)], far)  # 小区也满
    cache.set_run_mode("run-1", "a", "low", far)
    cache.release_run("run-1")  # 正常结束、出错、被取消都走这里
    assert cache.low_bytes == 0
    assert cache.run_mode("run-1", "a") is None
    assert cache.hold_low("run-2", [_prepared("b", 60)], far)


def test_low_copy_expires_at_the_run_deadline() -> None:
    cache = UprightViewCache(full_cap_bytes=10, low_cap_bytes=100)
    assert cache.hold_low("run-1", [_prepared("a", 60)], time.monotonic() - 1)
    assert cache.hold_low("run-2", [_prepared("b", 60)], time.monotonic() + 60)
    assert not cache.has_low(("a", 90))


def test_run_modes_expire_for_runs_that_never_reach_sse() -> None:
    """审查 L2 —— 委派子代的 run 不经 ``sse.run_agent`` 收尾:降级决定按过期清掉,不泄漏。"""
    cache = UprightViewCache(full_cap_bytes=10, low_cap_bytes=10)
    cache.set_run_mode("child-run", "a", "raw", time.monotonic() - 1)
    assert cache.run_mode("child-run", "a") is None
    cache.set_run_mode("child-run", "z", "raw", time.monotonic() - 1)
    cache.set_run_mode("child-run", "b", "raw", time.monotonic() + 60)
    assert ("child-run", "z") not in cache._run_modes  # 写新的时顺手清掉过期的
    assert cache.run_mode("child-run", "b") == "raw"


async def test_a_full_copy_takes_both_decode_slots() -> None:
    """审查 H1 —— 全分辨率转正图占两个解图名额:它在跑时别的解图排队;几个全分辨率的同时来,
    一次只跑一个,不会各拿一个名额互等。"""
    import threading

    active = 0
    peak = 0
    lock = threading.Lock()

    def work(_: int) -> int:
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.05)
        with lock:
            active -= 1
        return 0

    await asyncio.gather(*(image_views.in_wide_host_crop_slot(work, n) for n in range(3)))
    assert peak == 1
    active = peak = 0
    await asyncio.gather(*(image_views.in_host_crop_slot(work, n) for n in range(4)))
    assert peak == 2


async def test_adapter_miss_runs_off_the_event_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    """审查 H1 —— 适配器当场重算走解图名额、在线程里,不占事件循环。"""
    import threading

    raw = _quad_jpeg()
    seen: list[str] = []
    real = image_views.upright_copy

    def spy(data: bytes, degrees: int, *rest: Any) -> Any:
        seen.append(threading.current_thread().name)
        return real(data, degrees, *rest)

    monkeypatch.setattr(image_views, "upright_copy", spy)
    await resolve_message_images([_REF], _resolver(raw), [ImageView(90)])
    assert seen
    assert seen[0] != threading.main_thread().name


def test_upright_whole_closes_the_original_once_turned() -> None:
    """审查 H1 —— 转完马上关掉原图(解开的那份像素放掉);不用转时不复制、原样返回。"""
    from orchestrator.tools.region_zoom import upright_whole

    image = Image.open(io.BytesIO(_quad_jpeg()))
    turned = upright_whole(image, 90)
    assert turned is not image
    with pytest.raises(ValueError):
        image.load()  # 已关
    assert turned.size == (400, 600)

    tags = Image.Exif()
    tags[0x0112] = 8  # 方向标记 8 再顺时针转 90 度 = 正好不用转
    plain = Image.open(io.BytesIO(_encode(_quadrants(), "JPEG", quality=95, exif=tags.tobytes())))
    assert upright_whole(plain, 90) is plain


# ---------------------------------------------------------------------------
# 复审:N1 / N3 / N6 与单图预算
# ---------------------------------------------------------------------------


async def _wait_for(predicate: Any, timeout_s: float = 5.0) -> None:
    deadline = time.monotonic() + timeout_s
    while not predicate():
        assert time.monotonic() < deadline, "timed out"
        await asyncio.sleep(0.01)


@pytest.mark.parametrize(
    ("runner", "held"), [("in_host_crop_slot", 1), ("in_wide_host_crop_slot", 2)]
)
async def test_cancel_keeps_the_slots_until_the_thread_finishes(runner: str, held: int) -> None:
    """审查 N1 —— 等的一方被取消,线程照跑;名额要等线程真结束才还,不然另一张全分辨率图又能开算。"""
    import threading

    started, release = threading.Event(), threading.Event()

    def work() -> int:
        started.set()
        release.wait(5)
        return 1

    task = asyncio.create_task(getattr(image_views, runner)(work))
    await asyncio.to_thread(started.wait, 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    gate = image_views._gate()
    assert gate._value == 2 - held  # 线程还在跑:名额没还
    release.set()
    await _wait_for(lambda: gate._value == 2)


async def test_a_cancelled_full_copy_still_lands_in_the_cache() -> None:
    """审查 N1 —— 首 token 超时换备用 / 用户取消时,已经开算的全分辨率图算完照样收进缓存。"""
    raw = _quad_jpeg()
    digest = image_digest(raw)
    task = asyncio.create_task(image_views.full_copy_in_slots(raw, 90, digest))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await _wait_for(lambda: image_views.UPRIGHT_VIEWS.has_full((digest, 90)))


async def test_adapter_miss_does_not_wait_long_for_a_slot(monkeypatch: pytest.MonkeyPatch) -> None:
    """审查 N3 —— 解图名额被占着时,适配器当场重算最多等 ``ADAPTER_MISS_SLOT_WAIT_S``,等不到就这次
    按原图发(计 ``adapter_busy``),不让首 token 跟着排队。"""
    monkeypatch.setattr(image_views, "ADAPTER_MISS_SLOT_WAIT_S", 0.1)
    raw = _quad_jpeg()
    gate = image_views._gate()
    await gate.acquire()
    await gate.acquire()
    before = _count("adapter_busy")
    started = time.monotonic()
    try:
        out = await resolve_message_images([_REF], _resolver(raw), [ImageView(90)])
    finally:
        gate.release()
        gate.release()
    assert time.monotonic() - started < 1.0
    assert isinstance(out[0], ResolvedImage)
    assert out[0].data == raw
    assert _count("adapter_busy") == before + 1


async def test_a_wide_slot_works_with_a_single_host_slot(monkeypatch: pytest.MonkeyPatch) -> None:
    """审查 N6 —— ``HOST_CROP_SLOTS`` 只有 1 时,占两个名额的退成占一个(不会永远等第二个)。"""
    from orchestrator.tools import region_zoom

    monkeypatch.setattr(region_zoom, "HOST_CROP_SLOTS", 1)
    monkeypatch.setattr(image_views, "_HOST_CROP_GATES", weakref.WeakKeyDictionary())
    assert await asyncio.wait_for(image_views.in_wide_host_crop_slot(lambda: 7), 2.0) == 7


def test_a_copy_within_the_budget_is_byte_identical() -> None:
    """设计 §8.1 第 5 条再细化 —— 不超过单图预算的原图一律不缩:有预算与没预算出的字节一样。"""
    raw = _encode(_photo(1200, 900), "JPEG", quality=92)
    unscaled = upright_copy(raw, 90)
    assert upright_copy(raw, 90, max_pixels=1200 * 900) == unscaled
    # qwen4-max 的情形:12.2 MP 的手机照片,预算 12.6 MP(约多 3%)—— 也不缩(更不放大)。
    assert upright_copy(raw, 90, max_pixels=int(1200 * 900 * 12_595_200 / 12_192_768)) == unscaled


def test_a_jpeg_over_the_budget_is_drafted_then_fitted(monkeypatch: pytest.MonkeyPatch) -> None:
    """超过单图预算的 JPEG:解码时就按 DCT 缩(draft),再缩到预算;结果不超过预算、方向正确。"""
    from PIL.JpegImagePlugin import JpegImageFile

    drafts: list[tuple[int, int]] = []
    real = JpegImageFile.draft

    def spy(self: Any, mode: Any, size: Any) -> Any:
        drafts.append(size)
        return real(self, mode, size)

    monkeypatch.setattr(JpegImageFile, "draft", spy)
    stored = _quadrants().resize((2400, 1600))
    raw = _encode(stored, "JPEG", quality=92)
    found = upright_copy(raw, 90, max_pixels=240_000)
    assert found is not None
    image = _decode(found[1])
    assert image.width * image.height <= 240_000
    assert image.size == (400, 600)  # 600x400 的预算尺寸,转了 90 度
    # 1/4 那一档 600 x 400 = 24 万正好落在 [95% 预算, 预算] 里:按它解码,不再缩。
    assert drafts == [(600, 400)]


def test_a_view_carries_the_budget_only_for_full_copies() -> None:
    assert parse_image_view({"degrees": 90, "max_pixels": 1000}) == ImageView(90, "full", 1000)
    assert parse_image_view({"degrees": 90, "variant": "low", "max_pixels": 1000}) == ImageView(
        90, "low"
    )
    assert parse_image_view({"degrees": 90, "max_pixels": 0}) is None
    assert ImageView(90).as_dict() == {"degrees": 90, "variant": "full"}


# ---------------------------------------------------------------------------
# 第 5 条三细化(2026-11-20):缩放结果落在 [95% 预算, 预算]
# ---------------------------------------------------------------------------


def _drafts(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    from PIL.JpegImagePlugin import JpegImageFile

    seen: list[Any] = []
    real = JpegImageFile.draft

    def spy(self: Any, mode: Any, size: Any) -> Any:
        result = real(self, mode, size)
        seen.append((size, self.size))
        return result

    monkeypatch.setattr(JpegImageFile, "draft", spy)
    return seen


@pytest.mark.parametrize("budget", [1_000_000, 990_000])
def test_a_half_draft_just_under_the_budget_is_kept(
    monkeypatch: pytest.MonkeyPatch, budget: int
) -> None:
    """48~50 MP 照片的情形(缩小版):按一半解码得到 1200 x 800 = 96 万,在 [95% 预算, 预算] 里,
    就用它,不再整张解开、也不再缩一次。"""
    seen = _drafts(monkeypatch)
    raw = _encode(_quadrants().resize((2400, 1600)), "JPEG", quality=92)
    found = upright_copy(raw, 90, max_pixels=budget)
    assert found is not None
    image = _decode(found[1])
    assert image.size == (800, 1200)
    assert budget * 0.95 <= image.width * image.height <= budget
    assert [decoded for _, decoded in seen] == [(1200, 800)]  # 确实按一半读的


def test_no_draft_lands_in_the_window_so_none_is_used(monkeypatch: pytest.MonkeyPatch) -> None:
    """按一半读是 96 万 > 预算 90 万,按四分之一是 24 万 < 95%:没有一档落在 [95%, 100%] 里 ——
    不用 draft,整张解开后一次 LANCZOS 缩到预算(内存贵;``_pin`` 先估过才会走到这里)。"""
    seen = _drafts(monkeypatch)
    raw = _encode(_quadrants().resize((2400, 1600)), "JPEG", quality=92)
    found = upright_copy(raw, 90, max_pixels=900_000)
    assert found is not None
    image = _decode(found[1])
    assert 900_000 * 0.95 <= image.width * image.height <= 900_000
    assert seen == []


def test_the_budget_plan_lands_in_the_window() -> None:
    """``budget_size`` 是唯一的判定点:解码时缩只在结果落进 [95%, 100%] 预算时才用。"""
    from orchestrator.tools.region_zoom import BUDGET_FLOOR, budget_size

    qwen = 12_595_200
    assert budget_size(4032, 3024, qwen) is None  # 12.2 MP:不超过预算,不缩
    phone48 = budget_size(8064, 6048, qwen)
    assert phone48 is not None
    assert (phone48.draft, phone48.resize) == (2, False)  # 按一半读,直接用
    twenty = budget_size(5184, 3888, qwen)  # 1/2 只有 504 万(40%):不用 draft
    assert twenty is not None
    assert (twenty.draft, twenty.resize) == (1, True)
    tw, th = twenty.target
    assert qwen * BUDGET_FLOOR <= tw * th <= qwen


def test_the_peak_estimate_matches_the_measured_classes() -> None:
    """解码之前的峰值估算:12 MP 照片与按一半读的 48 MP 照片在每张转正图的内存预算以内;20 MP 照片
    (要整张解开再缩)、按一半读的 48 MP CMYK、6000 万像素的 JPEG 超过 —— 它们改用小副本。"""
    from orchestrator.image_views import FULL_COPY_PEAK_BYTES
    from orchestrator.tools.region_zoom import upright_peak_bytes

    qwen, mib = 12_595_200, 1024 * 1024

    def peak(w: int, h: int, *, mode: str = "RGB", size: int = 4 * mib) -> int:
        return upright_peak_bytes(w, h, mode, qwen, encoded_bytes=size)

    assert peak(4032, 3024) <= FULL_COPY_PEAK_BYTES
    assert peak(4096, 3072, size=3 * mib) <= FULL_COPY_PEAK_BYTES  # 12.58 MP、3 MiB:最贵的不缩图
    assert peak(8064, 6048) <= FULL_COPY_PEAK_BYTES
    assert peak(5184, 3888) > FULL_COPY_PEAK_BYTES
    assert peak(8064, 6048, mode="CMYK", size=20 * mib) > FULL_COPY_PEAK_BYTES
    assert peak(9487, 6324) > FULL_COPY_PEAK_BYTES  # 6000 万像素(上传解码上限)
    assert peak(5184, 3888, mode="L") <= FULL_COPY_PEAK_BYTES  # 灰度:每像素 1 字节


async def test_the_adapter_does_not_make_a_full_copy_over_the_peak_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(image_views, "FULL_COPY_PEAK_BYTES", 1000)
    raw = _quad_jpeg()
    before = _count("peak_over_budget")
    out = await resolve_message_images([_REF], _resolver(raw), [ImageView(90)])
    assert isinstance(out[0], ResolvedImage)
    assert out[0].data == raw
    assert _count("peak_over_budget") == before + 1


def test_small_copies_take_one_or_two_slots_by_their_estimated_peak(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """候选 / 小副本按估的峰值占解图名额:不到每张预算的一半占 1 个、不到预算占 2 个(两个名额加起来
    不超过预算),再大不做。"""
    from orchestrator.tools import region_zoom

    raw = _quad_jpeg()
    assert image_views.small_copy_slots(raw) == 1
    peak = region_zoom.crop_peak_bytes(600, 400, "RGB")
    monkeypatch.setattr(image_views, "FULL_COPY_PEAK_BYTES", peak + 1)
    assert image_views.small_copy_slots(raw) == 2
    monkeypatch.setattr(image_views, "FULL_COPY_PEAK_BYTES", peak - 1)
    assert image_views.small_copy_slots(raw) is None


def test_the_jpeg_draft_request_picks_the_intended_scale() -> None:
    """复审 —— Pillow 按「原尺寸 // 请求尺寸」选 draft 档:宽高不是倍数时(9487)也要真按 1/4 读。"""
    raw = _encode(Image.new("RGB", (9487, 6324), (40, 80, 120)), "JPEG", quality=50)
    from orchestrator.tools.region_zoom import _draft_request

    image = Image.open(io.BytesIO(raw))
    image.draft("RGB", _draft_request(image.size, 4))
    assert image.size == (2372, 1581)


def test_lent_candidates_have_their_own_process_wide_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    """复审第四轮 —— 借给适配器的候选图全进程合计有上限:到了就不借(调用方让这张图这一步不判);
    还回去之后又能借。它们不再挤全分辨率区的转正图。"""
    monkeypatch.setattr(image_views, "LENT_CANDIDATES_CAP_BYTES", 100)
    cache = UprightViewCache(full_cap_bytes=100, low_cap_bytes=10)
    assert cache.try_pin_full([_prepared("a", 60)])
    cache.unpin_full([("a", 90)])
    assert cache.lend(("cand", 0), "image/jpeg", b"x" * 60)
    assert cache.has_full(("a", 90))  # 不挤转正图
    assert not cache.lend(("cand", 90), "image/jpeg", b"x" * 60)  # 60 + 60 > 100
    cache.give_back(("cand", 0))
    assert cache.lend(("cand", 90), "image/jpeg", b"x" * 60)


#: 第六轮实测(审查方法:新进程、读文件、``crop_image`` 整张框转 90 度一次、``ru_maxrss`` 增量;
#: 本机 macOS 与控制面镜像的 Linux 取大的,MiB)。``test_b322_peak_measured`` 每次现量一遍。
_MEASURED_CROP_MIB: dict[tuple[int, int, str], float] = {
    (4032, 3024, "L"): 35.0,
    (4032, 3024, "RGB"): 41.3,
    (4032, 3024, "CMYK"): 41.5,
    (5184, 3888, "L"): 47.3,
    (5184, 3888, "RGB"): 64.2,
    (5184, 3888, "CMYK"): 64.2,
    (8064, 6048, "L"): 35.0,
    (8064, 6048, "RGB"): 41.3,
    (8064, 6048, "CMYK"): 41.5,
    (9487, 6000, "RGB"): 165.3,
    (3000, 2000, "L"): 10.7,
}


@pytest.mark.parametrize(("size", "measured"), list(_MEASURED_CROP_MIB.items()))
def test_the_crop_estimate_covers_the_measured_peak(
    size: tuple[int, int, str], measured: float
) -> None:
    """复审第五轮 M1 —— 估算(含 LANCZOS 中间那份、转 RGB 的几份、校准余量,再乘 1.2)不低于实测,
    也不离谱地高(不到实测的两倍)。"""
    from orchestrator.tools.region_zoom import PEAK_SAFETY, crop_peak_bytes

    estimate = crop_peak_bytes(*size) / (1024 * 1024)
    assert estimate / PEAK_SAFETY >= measured  # 校准值本身就不低于实测
    assert estimate < 2 * measured


def test_one_slot_crops_fit_twice_in_the_per_copy_budget() -> None:
    """复审第五轮 M1 —— 占 1 个名额的候选 / 小副本,两个同时在算也不超过 110 MiB(按估算,估算不低于
    实测);12 MP 照片占 1 个,20 MP 占 2 个,6000 万像素的不做。"""
    from orchestrator.image_views import FULL_COPY_PEAK_BYTES, slots_for
    from orchestrator.tools.region_zoom import crop_peak_bytes

    for (w, h, mode), measured in _MEASURED_CROP_MIB.items():
        if slots_for(w, h, mode) == 1:
            assert 2 * crop_peak_bytes(w, h, mode) <= FULL_COPY_PEAK_BYTES
            assert 2 * measured * 1024 * 1024 <= FULL_COPY_PEAK_BYTES
    assert [slots_for(4032, 3024, m) for m in ("L", "RGB", "CMYK")] == [1, 1, 1]
    assert [slots_for(5184, 3888, m) for m in ("L", "RGB", "CMYK")] == [2, 2, 2]
    assert [slots_for(8064, 6048, m) for m in ("L", "RGB", "CMYK")] == [1, 1, 1]
    assert slots_for(9487, 6000, "RGB") is None


@pytest.mark.parametrize(
    ("fmt", "mode"),
    [
        ("WEBP", "RGB"),
        ("PNG", "L"),
        ("PNG", "LA"),
        ("PNG", "RGB"),
        ("PNG", "RGBA"),
        ("PNG", "P"),
        ("PNG", "1"),
        ("PNG", "I;16"),
        ("GIF", "P"),
    ],
)
def test_unsupported_formats_and_modes_are_skipped_by_header(fmt: str, mode: str) -> None:
    """第六轮(用户)—— 只做 JPEG(L / RGB / CMYK);PNG 各种模式、WebP、GIF 按文件头就判定不做
    (``format_unsupported``),转正图也做不出来。"""
    image = _quadrants().convert(mode) if mode not in ("P",) else _quadrants().convert("P")
    raw = _encode(image, fmt)
    assert image_views.skip_reason(raw) == "format_unsupported"
    assert image_views.small_copy_slots(raw) is None
    with pytest.raises(ValueError):
        upright_copy(raw, 90)


@pytest.mark.parametrize(
    ("fmt", "mode"),
    [
        ("JPEG", "L"),
        ("JPEG", "RGB"),
        ("JPEG", "CMYK"),
    ],
)
def test_supported_formats_and_modes_are_turned(fmt: str, mode: str) -> None:
    raw = _encode(_quadrants().convert(mode), fmt, quality=95)
    assert image_views.skip_reason(raw) is None
    found = upright_copy(raw, 90)
    assert found is not None
    assert _decode(found[1]).size == (400, 600)


def test_a_12mp_webp_is_skipped_without_decoding(monkeypatch: pytest.MonkeyPatch) -> None:
    """不支持的格式只读文件头,不解码。"""
    raw = _encode(Image.new("RGB", (4000, 3000), (10, 20, 30)), "WEBP", quality=50)
    loads: list[str] = []
    real = Image.Image.load

    def spy(self: Image.Image) -> Any:
        loads.append(self.format or "")
        return real(self)

    monkeypatch.setattr(Image.Image, "load", spy)
    assert image_views.skip_reason(raw) == "format_unsupported"
    assert loads == []


def test_a_progressive_jpeg_is_not_supported() -> None:
    """复审第六轮 L3 —— 渐进式 JPEG 的解码缓冲没量过:按文件头不做(纵深防御)。"""
    buffer = io.BytesIO()
    _quadrants().save(buffer, format="JPEG", quality=95, progressive=True)
    raw = buffer.getvalue()
    assert image_views.skip_reason(raw) == "format_unsupported"
    assert image_views.small_copy_slots(raw) is None
    baseline = _encode(_quadrants(), "JPEG", quality=95)
    assert image_views.skip_reason(baseline) is None


def test_the_full_copy_estimate_covers_the_reviewers_measurements() -> None:
    """复审第六轮 L1 —— 奇数尺寸 12 MP、方向标记 6、灰度:估算(含 x1.05)不低于审查量到的数。"""
    from orchestrator.tools.region_zoom import upright_peak_bytes

    mib = 1024 * 1024
    qwen = 12_595_200
    assert upright_peak_bytes(4031, 3023, "RGB", qwen, encoded_bytes=4 * mib) >= 103.7 * mib
    assert upright_peak_bytes(4032, 3024, "RGB", qwen, encoded_bytes=4 * mib) >= 101.1 * mib
    assert upright_peak_bytes(4032, 3024, "L", qwen, encoded_bytes=3 * mib) >= 31.3 * mib
