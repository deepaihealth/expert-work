"""B-322 PR ① —— 转正图的生成、字节规则与进程内缓存(设计 §4.1.5、§6.1 字节 / 钉住各条)。"""

from __future__ import annotations

import asyncio
import io
import random
import time
from typing import Any

import pytest
from langchain_core.messages import HumanMessage
from PIL import Image, ImageDraw, ImageOps

from orchestrator import image_views
from orchestrator.image_views import (
    LOSSLESS_MAX_BYTES,
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


@pytest.mark.parametrize("degrees", [90, 180, 270])
def test_png_copy_is_pixel_identical_to_turning_first(degrees: int) -> None:
    stored = _quadrants()
    media, data = upright_copy(_encode(stored, "PNG"), degrees) or ("", b"")
    assert media == "image/png"
    # 顺时针转 N 度 = PIL 的 rotate(-N)。
    expected = stored.rotate(-degrees, expand=True)
    assert _decode(data).convert("RGB").tobytes() == expected.tobytes()


@pytest.mark.parametrize("exif", [3, 6, 8])
def test_exif_orientation_is_applied_before_the_turn(exif: int) -> None:
    """方向标记先转正,再顺时针转 N 度(与 ``crop_upright`` 整张框的结果、与 B-301 候选同一口径)。"""
    stored = _quadrants()
    tags = Image.Exif()
    tags[0x0112] = exif
    raw = _encode(stored, "PNG", exif=tags.tobytes())
    _media, data = upright_copy(raw, 90) or ("", b"")
    expected = ImageOps.exif_transpose(_decode(raw)).rotate(-90, expand=True)
    assert _decode(data).convert("RGB").tobytes() == expected.convert("RGB").tobytes()


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


def test_webp_copy_never_larger_than_the_original() -> None:
    raw = _encode(_photo(3000, 2000), "WEBP", quality=30)
    found = upright_copy(raw, 270)
    assert found is not None
    assert found[0] == "image/webp"
    assert len(found[1]) <= len(raw)


def _small_text_png() -> bytes:
    image = Image.new("RGB", (1800, 1200), (255, 255, 255))
    draw = ImageDraw.Draw(image)
    rng = random.Random(3)  # noqa: S311 - 合成测试图,不是密码学用途
    for row in range(0, 1200, 12):
        line = "".join(rng.choice("0123456789.-mgdL ") for _ in range(250))
        draw.text((4, row), line, fill=(0, 0, 0))  # 默认字体约 9-10 像素高
    return _encode(image, "PNG")


def test_png_screenshot_is_not_shrunk_and_stays_within_the_png_allowance() -> None:
    raw = _small_text_png()
    found = upright_copy(raw, 90)
    assert found is not None
    media, data = found
    assert media == "image/png"
    assert _decode(data).size == (1200, 1800)  # 不缩像素
    assert len(data) <= max(len(raw), min(int(len(raw) * 1.1), LOSSLESS_MAX_BYTES))


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
    monkeypatch.setattr(image_views, "_encode", lambda image, fmt, q: b"x" * (len(raw) + 1))
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
    raw = _encode(_quadrants(), "PNG")
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
    raw = _encode(_quadrants(), "PNG")
    before = _count("adapter_miss")
    out = await resolve_message_images([_REF], _resolver(raw), [ImageView(90)])
    assert isinstance(out[0], ResolvedImage)
    assert out[0].data == (upright_copy(raw, 90) or ("", b""))[1]
    assert _count("adapter_miss") == before + 1


async def test_low_variant_is_read_as_low_even_when_full_exists() -> None:
    raw = _encode(_quadrants(), "PNG")
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
    raw = _encode(_quadrants(), "PNG")
    out = await resolve_message_images([_REF], _resolver(raw), split_human_views([_block(None)]))
    assert isinstance(out[0], ResolvedImage)
    assert out[0].data == raw


@pytest.mark.parametrize("adapter", [openai_adapter, anthropic_adapter])
async def test_both_adapters_pass_the_view(adapter: Any) -> None:
    raw = _encode(_quadrants(), "PNG")
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


def test_lent_candidates_count_toward_the_full_cap() -> None:
    """审查 L6 —— 借出的问方向候选图算进全分辨率区的上限:挤掉没钉住的转正图,钉不下就不钉。"""
    cache = UprightViewCache(full_cap_bytes=100, low_cap_bytes=10)
    assert cache.try_pin_full([_prepared("a", 60)])
    cache.unpin_full([("a", 90)])
    cache.lend(("cand", 0), "image/jpeg", b"x" * 50)
    assert not cache.has_full(("a", 90))  # 没钉住的被挤掉
    assert not cache.try_pin_full([_prepared("b", 60)])  # 50 + 60 > 100
    cache.give_back(("cand", 0))
    assert cache.try_pin_full([_prepared("b", 60)])


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

    raw = _encode(_quadrants(), "PNG")
    seen: list[str] = []
    real = image_views.upright_copy

    def spy(data: bytes, degrees: int) -> Any:
        seen.append(threading.current_thread().name)
        return real(data, degrees)

    monkeypatch.setattr(image_views, "upright_copy", spy)
    await resolve_message_images([_REF], _resolver(raw), [ImageView(90)])
    assert seen
    assert seen[0] != threading.main_thread().name


def test_upright_whole_closes_the_original_once_turned() -> None:
    """审查 H1 —— 转完马上关掉原图(解开的那份像素放掉);不用转时不复制、原样返回。"""
    from orchestrator.tools.region_zoom import upright_whole

    image = Image.open(io.BytesIO(_encode(_quadrants(), "PNG")))
    turned = upright_whole(image, 90)
    assert turned is not image
    with pytest.raises(ValueError):
        image.load()  # 已关
    assert turned.size == (400, 600)

    tags = Image.Exif()
    tags[0x0112] = 8  # 方向标记 8 再顺时针转 90 度 = 正好不用转
    plain = Image.open(io.BytesIO(_encode(_quadrants(), "PNG", exif=tags.tobytes())))
    assert upright_whole(plain, 90) is plain
