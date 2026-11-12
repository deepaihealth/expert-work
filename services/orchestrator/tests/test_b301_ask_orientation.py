"""B-301 PR 1 —— ``ask_image`` 先问方向再读(设计 ``2026-11-11-b301-image-orientation-design.md``)。

看图模型替身只认像素:合成图四个象限是红 / 绿 / 蓝 / 白,替身只在收到「红在左上、绿在右上、
蓝在左下」的那一张时答 ``yes``、读出正确内容;别的方向一律答 ``no``、读错。工具选对角度、读对内容、
meta 里记下角度,才说明它真的把图转正了。
"""

from __future__ import annotations

import asyncio
import io
import json
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from langchain_core.messages import AIMessage, BaseMessage
from PIL import Image, ImageOps

from expert_work.persistence import is_rendered_crop_rel
from expert_work.protocol import ModelSpec, VisionSpec
from expert_work.protocol.multimodal import ImageRef
from expert_work.runtime.middleware.llm_error_handling import LLMClientError
from orchestrator.multimodal import (
    DispatchingImageResolver,
    ImageResolver,
    InMemoryImageResolver,
    NasWorkspaceImageResolver,
    ResolvedImage,
    is_cacheable_image_ref,
    parse_rendered_figure_ref,
)
from orchestrator.tools import image_orientation, region_zoom
from orchestrator.tools.assembly import ToolEnv, build_tool_registry
from orchestrator.tools.image_orientation import (
    ORIENTATION_ENV,
    ORIENTATION_QUESTION,
    ask_image_orientation_off_topic_total,
    ask_image_orientation_total,
)
from orchestrator.tools.nas_workspace_store import NasWorkspaceStore
from orchestrator.tools.read_page import workspace_figure_ref
from orchestrator.tools.registry import ToolContext
from orchestrator.tools.vision import AskImageTool

_KEY = "orient-probe-1a2b3c4d"
_CONV = UUID("55555555-5555-5555-5555-555555555555")
_UPRIGHT_SIZE = (600, 400)
SECRET = "the label reads 69.1"
WRONG = "the label reads 0.0"
_ROTATED_NOTE = "it was rotated {n} degrees clockwise before reading"
_UNDETERMINED_NOTE = "its orientation could not be determined"
_FULL = "0-0-1000-1000.jpg"

# ---------------------------------------------------------------------------
# 合成图与看图模型替身
# ---------------------------------------------------------------------------

_COLOURS = {
    "red": (255, 0, 0),
    "green": (0, 160, 0),
    "blue": (0, 0, 255),
    "white": (255, 255, 255),
    "grey": (128, 128, 128),
}


def _upright() -> Image.Image:
    """正放的合成图:左上红、右上绿、左下蓝、右下白。"""
    width, height = _UPRIGHT_SIZE
    image = Image.new("RGB", _UPRIGHT_SIZE, _COLOURS["white"])
    image.paste(_COLOURS["red"], (0, 0, width // 2, height // 2))
    image.paste(_COLOURS["green"], (width // 2, 0, width, height // 2))
    image.paste(_COLOURS["blue"], (0, height // 2, width // 2, height))
    return image


def _stored(turn: int) -> Image.Image:
    """存下来的图:正放的图**逆时针**转了 ``turn`` 度 —— 要顺时针转 ``turn`` 度才正。"""
    return _upright().rotate(turn, expand=True)


def _encode(image: Image.Image, fmt: str = "PNG", **kwargs: Any) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format=fmt, **kwargs)
    return buffer.getvalue()


def _decode(data: bytes) -> Image.Image:
    image = Image.open(io.BytesIO(data))
    image.load()
    return image.convert("RGB")


def _colour(pixel: tuple[int, ...]) -> str | None:
    for name, ref in _COLOURS.items():
        if all(abs(a - b) <= 60 for a, b in zip(pixel[:3], ref, strict=True)):
            return name
    return None


def _quadrants(image: Image.Image) -> tuple[str | None, ...]:
    width, height = image.size
    points = ((0.25, 0.25), (0.75, 0.25), (0.25, 0.75), (0.75, 0.75))
    return tuple(_colour(image.getpixel((int(fx * width), int(fy * height)))) for fx, fy in points)


def _is_upright(image: Image.Image) -> bool:
    return _quadrants(image) == ("red", "green", "blue", "white")


def _is_blank(image: Image.Image) -> bool:
    return set(_quadrants(image)) == {"grey"}


def _sent(messages: Sequence[BaseMessage]) -> tuple[str, str]:
    content = messages[1].content
    assert isinstance(content, list)
    text, block = content[0], content[1]
    assert isinstance(text, dict) and isinstance(block, dict)
    return str(text["text"]), str(block["ref"])


Orient = Callable[[str, Image.Image], Awaitable[str]]


async def _default_orient(_ref: str, image: Image.Image) -> str:
    if _is_blank(image):
        return "none"
    return "Yes." if _is_upright(image) else "**No**"


@dataclass
class _Vision:
    """看图模型替身:按收到的像素回答。``orient`` 换掉「正不正」的回答(可以睡、可以抛)。"""

    resolver: ImageResolver
    orient: Orient = _default_orient
    calls: list[tuple[str, str]] = field(default_factory=list)

    async def __call__(self, *, messages: Sequence[BaseMessage], tools: Sequence[Any]) -> AIMessage:
        question, ref = _sent(messages)
        self.calls.append((question, ref))
        image = _decode((await self.resolver.resolve(ref)).data)
        if question == ORIENTATION_QUESTION:
            reply = await self.orient(ref, image)
        else:
            reply = SECRET if _is_upright(image) else WRONG
        usage = {"input_tokens": 10, "output_tokens": 1, "total_tokens": 11}
        return AIMessage(content=reply, usage_metadata=usage)  # type: ignore[arg-type]

    @property
    def orientation_refs(self) -> list[str]:
        return [ref for question, ref in self.calls if question == ORIENTATION_QUESTION]

    @property
    def read_refs(self) -> list[str]:
        return [ref for question, ref in self.calls if question != ORIENTATION_QUESTION]


@dataclass
class _Setup:
    tool: AskImageTool
    vision: _Vision
    ref: str
    ctx: ToolContext
    root: Path
    resolver: DispatchingImageResolver

    def path_of(self, ref: str) -> Path:
        prefix = f"expert_work://workspace/{self.ctx.tenant_id}/{self.ctx.user_id}/"
        assert ref.startswith(prefix), ref
        return self.root / str(self.ctx.tenant_id) / str(self.ctx.user_id) / ref[len(prefix) :]

    def image_of(self, ref: str) -> Image.Image:
        return _decode(self.path_of(ref).read_bytes())

    async def look(self, **args: Any) -> Any:
        return await self.tool.call({"question": "What does the label say?", **args}, ctx=self.ctx)


def _ctx(tenant: UUID, user: UUID) -> ToolContext:
    return ToolContext(
        tenant_id=tenant,
        run_id=uuid4(),
        user_id=user,
        thread_id=_CONV,
        agent_key=_KEY,
        workspace_layout="conversation",
    )


def _setup(
    tmp_path: Path,
    image: Image.Image | bytes,
    *,
    orient: Orient = _default_orient,
    on: bool = True,
    **ask: Any,
) -> _Setup:
    tenant, user = uuid4(), uuid4()
    root = tmp_path / "nas"
    (root / str(tenant) / str(user)).mkdir(parents=True)
    ref = ImageRef(tenant_id=tenant, thread_id=_CONV, image_id=uuid4(), ext=".png").to_uri()
    data = image if isinstance(image, bytes) else _encode(image)
    resolver = DispatchingImageResolver(
        uploads=InMemoryImageResolver(images={ref: ResolvedImage("image/png", data)}),
        workspace=NasWorkspaceImageResolver(root=root),
    )
    vision = _Vision(resolver=resolver, orient=orient)
    ask.setdefault("orientation_timeout_s", 0.3)
    tool = AskImageTool(
        vl_caller=vision,
        image_resolver=resolver,
        workspace_store=NasWorkspaceStore(root=str(root)),
        ask_orientation=on,
        **ask,
    )
    return _Setup(tool, vision, ref, _ctx(tenant, user), root, resolver)


def _counter(conclusion: str, degrees: int) -> float:
    return ask_image_orientation_total.labels(
        conclusion=conclusion, degrees=str(degrees)
    )._value.get()


# ---------------------------------------------------------------------------
# 必须变红的对照 + 四个角度
# ---------------------------------------------------------------------------


def _vision_spec(name: str = "doubao-seed-2-1-pro-260628") -> VisionSpec:
    return VisionSpec(model=ModelSpec(provider="doubao", name=name))


async def _registered(up: _Setup, vision: VisionSpec | None = None) -> AskImageTool:
    """按生产的装配路径建 ``ask_image``(``ask_orientation`` 由运维紧急开关填)。"""
    env = ToolEnv(image_resolver=up.resolver, workspace_store=up.tool.workspace_store)
    registry = await build_tool_registry(
        [], tool_env=env, vision=vision or _vision_spec(), vl_caller=up.vision
    )
    tool = registry.get("ask_image")
    assert isinstance(tool, AskImageTool)
    return tool


async def test_control_with_the_switch_off_a_turned_image_is_read_wrong(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """对照:运维紧急开关关掉,替身收到的是横着的原图,读错 —— 替身确实只在正放时读对。"""
    monkeypatch.setenv(ORIENTATION_ENV, "0")
    up = _setup(tmp_path, _stored(90), on=False)
    tool = await _registered(up)

    result = await tool.call({"image_ref": up.ref, "question": "q"}, ctx=up.ctx)

    assert tool.ask_orientation is False
    assert result.content == WRONG
    assert up.vision.orientation_refs == []
    assert up.vision.read_refs == [up.ref]
    assert "orientation" not in result.meta


async def test_by_default_assembly_asks_and_reads_the_turned_image_right(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """同一张 90 度的图,开关不设(默认开):按生产装配建的工具转正后读对。"""
    monkeypatch.delenv(ORIENTATION_ENV, raising=False)
    up = _setup(tmp_path, _stored(90), on=False)
    tool = await _registered(up)

    result = await tool.call({"image_ref": up.ref, "question": "q"}, ctx=up.ctx)

    assert tool.ask_orientation is True
    assert result.content.startswith(SECRET)
    assert result.meta["orientation"]["degrees"] == 90


@pytest.mark.parametrize("turn", [0, 90, 180, 270])
async def test_the_tool_picks_the_turn_reads_right_and_records_it(
    tmp_path: Path, turn: int
) -> None:
    up = _setup(tmp_path, _stored(turn))
    before = _counter("upright" if turn == 0 else "rotated", turn)

    result = await up.look(image_ref=up.ref)

    assert result.content.startswith(SECRET)
    orientation = result.meta["orientation"]
    assert orientation["degrees"] == turn
    assert orientation["conclusion"] == ("upright" if turn == 0 else "rotated")
    assert orientation["from_cache"] is False
    assert orientation["extra_calls"] == (1 if turn == 0 else 4)
    assert _counter(orientation["conclusion"], turn) == before + 1
    (read_ref,) = up.vision.read_refs
    if turn == 0:
        assert orientation["answers"] == {"0": "yes"}
        assert read_ref == up.ref  # 正放、无方向标记:原图引用原样交给模型
        assert result.content == SECRET  # 不加任何说明
    else:
        assert orientation["answers"] == {
            str(d): ("yes" if d == turn else "no") for d in (0, 90, 180, 270)
        }
        assert read_ref.endswith(f"/crops/{_sha_dir(up, read_ref)}/r{turn}/{_FULL}")
        assert _ROTATED_NOTE.format(n=turn) in result.content
        assert "Any region refers to the rotated image" in result.content


def _sha_dir(up: _Setup, ref: str) -> str:
    return ref.split("/crops/", 1)[1].split("/", 1)[0]


async def test_step_one_looks_at_the_zero_degree_copy_and_step_two_at_three_turns(
    tmp_path: Path,
) -> None:
    up = _setup(tmp_path, _stored(90))

    await up.look(image_ref=up.ref)

    first, *turned = up.vision.orientation_refs
    assert first.endswith(f"/{_FULL}") and "/r" not in first.rsplit("/crops/", 1)[1]
    assert sorted(ref.rsplit("/", 2)[1] for ref in turned) == ["r180", "r270", "r90"]
    for ref in turned:
        degrees = int(ref.rsplit("/", 2)[1][1:])
        expected = _stored(90).transpose(_CLOCKWISE[degrees])
        assert _quadrants(up.image_of(ref)) == _quadrants(expected)


_CLOCKWISE = {
    90: Image.Transpose.ROTATE_270,
    180: Image.Transpose.ROTATE_180,
    270: Image.Transpose.ROTATE_90,
}


async def test_orientation_calls_are_metered_like_other_looks(tmp_path: Path) -> None:
    metered: list[AIMessage] = []

    async def meter(response: AIMessage, *, tenant_id: UUID, user_id: UUID | None) -> None:
        metered.append(response)

    up = _setup(tmp_path, _stored(90), usage_meter=meter)

    await up.look(image_ref=up.ref)

    assert len(metered) == 5  # 1 + 3 次问方向 + 1 次读


async def test_no_text_is_upright(tmp_path: Path) -> None:
    up = _setup(tmp_path, Image.new("RGB", _UPRIGHT_SIZE, _COLOURS["grey"]))

    result = await up.look(image_ref=up.ref)

    assert result.meta["orientation"]["conclusion"] == "no_text"
    assert result.meta["orientation"]["degrees"] == 0
    assert up.vision.read_refs == [up.ref]
    assert result.content == WRONG  # 替身对无标记的图照常回话;没有追加说明


# ---------------------------------------------------------------------------
# 判不出 / 没问成
# ---------------------------------------------------------------------------


def _orient_by_dir(replies: dict[str, str | float | Exception]) -> Orient:
    """按候选所在子目录(``0`` / ``r90`` / ``r180`` / ``r270``)回答;数字 = 睡这么久再答 no
    (没有限时的话,那一个方向就成了「答成了的 no」,结论会变 —— 测试因此能红)。"""

    async def orient(ref: str, _image: Image.Image) -> str:
        parent = ref.rsplit("/", 2)[1]
        key = parent if parent.startswith("r") else "0"
        reply = replies.get(key, "no")
        if isinstance(reply, Exception):
            raise reply
        if isinstance(reply, float):
            await asyncio.sleep(reply)
            return "no"
        return reply

    return orient


@pytest.mark.parametrize(
    "replies",
    [
        {},
        {"r90": "yes", "r180": "yes"},
        {"r90": "yes", "r180": 5.0},
        {"r90": "yes", "r270": "The picture shows a label."},
        {"r90": "yes", "r180": RuntimeError("boom")},
    ],
    ids=["all-no", "two-yes", "one-timeout", "one-off-topic", "one-error"],
)
async def test_anything_but_exactly_one_yes_is_undetermined(
    tmp_path: Path, replies: dict[str, str | float | Exception]
) -> None:
    up = _setup(tmp_path, _stored(90), orient=_orient_by_dir(replies))

    result = await up.look(image_ref=up.ref)

    orientation = result.meta["orientation"]
    assert orientation["conclusion"] == "undetermined"
    assert orientation["degrees"] == 0
    assert orientation["extra_calls"] == 4
    assert up.vision.read_refs == [up.ref]  # 按 0 度读
    assert result.content.startswith(WRONG)
    assert _UNDETERMINED_NOTE in result.content
    assert "Tell the user it could not be read reliably" in result.content


async def test_an_off_topic_answer_is_counted(tmp_path: Path) -> None:
    counter = ask_image_orientation_off_topic_total.labels(model="vl-x")
    before = counter._value.get()
    up = _setup(
        tmp_path,
        _stored(90),
        orient=_orient_by_dir({"0": "The image is a photo."}),
        vl_model_name="vl-x",
    )

    result = await up.look(image_ref=up.ref)

    assert counter._value.get() == before + 1
    assert result.meta["orientation"]["conclusion"] == "not_asked"
    assert result.meta["orientation"]["answers"] == {"0": "off_topic"}


@pytest.mark.parametrize(
    "failure",
    [5.0, RuntimeError("boom"), LLMClientError("400 image too small")],
    ids=["timeout", "error", "vendor-400"],
)
async def test_a_failed_first_question_is_not_asked_and_the_read_still_succeeds(
    tmp_path: Path, failure: float | Exception
) -> None:
    if isinstance(failure, LLMClientError):
        failure.status = 400
    up = _setup(tmp_path, _stored(90), orient=_orient_by_dir({"0": failure}))

    result = await up.look(image_ref=up.ref)
    again = await up.look(image_ref=up.ref)

    for outcome in (result, again):
        assert outcome.content == WRONG  # 读成功(按原图),不对主模型说什么
        assert outcome.meta["orientation"]["conclusion"] == "not_asked"
        assert outcome.meta["orientation"]["degrees"] == 0
    assert result.meta["orientation"]["extra_calls"] == 1
    assert (
        again.meta["orientation"] | {"from_cache": False, "extra_calls": 1}
        == (result.meta["orientation"])
    )
    assert again.meta["orientation"]["from_cache"] is True
    assert again.meta["orientation"]["extra_calls"] == 0
    assert len(up.vision.orientation_refs) == 1  # 第二次看:0 次问方向
    assert up.vision.read_refs == [up.ref, up.ref]


@dataclass
class _Plain:
    """配置原样那条路由:被调到就记下来(问方向被拒时不许退回它)。"""

    calls: list[str] = field(default_factory=list)

    async def __call__(self, *, messages: Sequence[BaseMessage], tools: Sequence[Any]) -> AIMessage:
        self.calls.append(_sent(messages)[0])
        return AIMessage(content=WRONG)


async def test_a_vendor_rejection_does_not_take_the_normal_vision_route(tmp_path: Path) -> None:
    rejected = LLMClientError("400 invalid image")
    rejected.status = 400
    plain, deep = _Plain(), _Plain()
    up = _setup(tmp_path, _stored(90), orient=_orient_by_dir({"0": rejected}))
    tool = AskImageTool(
        vl_caller=deep,
        quick_vl_caller=up.vision,
        plain_vl_caller=plain,
        image_resolver=up.resolver,
        workspace_store=up.tool.workspace_store,
        ask_orientation=True,
    )

    result = await tool.call({"image_ref": up.ref, "question": "?"}, ctx=up.ctx)

    assert result.meta["orientation"]["conclusion"] == "not_asked"
    assert ORIENTATION_QUESTION not in plain.calls + deep.calls
    assert len(up.vision.orientation_refs) == 1


async def test_an_image_that_cannot_be_decoded_is_not_asked_and_reads_as_today(
    tmp_path: Path,
) -> None:
    up = _setup(tmp_path, b"\x89PNG not really")
    up.vision.calls.clear()

    async def answer(*, messages: Sequence[BaseMessage], tools: Sequence[Any]) -> AIMessage:
        up.vision.calls.append(_sent(messages))
        return AIMessage(content="cannot see")

    tool = AskImageTool(
        vl_caller=answer,
        image_resolver=up.resolver,
        workspace_store=up.tool.workspace_store,
        ask_orientation=True,
    )

    result = await tool.call({"image_ref": up.ref, "question": "?"}, ctx=up.ctx)

    assert result.content == "cannot see"
    assert result.meta["orientation"]["conclusion"] == "not_asked"
    assert up.vision.calls == [("?", up.ref)]


class _ListingFails(NasWorkspaceStore):
    """列目录就坏(读缓存的那一步会抛);写文件照常。"""

    async def list_dir(self, **kwargs: Any) -> Any:
        msg = "listing is down"
        raise RuntimeError(msg)


async def test_an_unexpected_failure_in_the_orientation_step_never_fails_the_read(
    tmp_path: Path,
) -> None:
    up = _setup(tmp_path, _stored(90))
    tool = AskImageTool(
        vl_caller=up.vision,
        image_resolver=up.resolver,
        workspace_store=_ListingFails(root=str(up.root)),
        ask_orientation=True,
    )
    before = _counter("not_asked", 0)

    result = await tool.call({"image_ref": up.ref, "question": "q"}, ctx=up.ctx)

    assert result.content == WRONG  # 按原图读成功,不对主模型说什么
    assert result.meta["orientation"]["conclusion"] == "not_asked"
    assert up.vision.read_refs == [up.ref]
    assert _counter("not_asked", 0) == before + 1


# ---------------------------------------------------------------------------
# 缓存:同一 run 只问一次
# ---------------------------------------------------------------------------


async def test_a_second_look_in_the_same_run_asks_nothing_and_keeps_the_turn(
    tmp_path: Path,
) -> None:
    up = _setup(tmp_path, _stored(270))

    first = await up.look(image_ref=up.ref)
    second = await up.look(image_ref=up.ref, region=[0, 0, 500, 500])

    assert len(up.vision.orientation_refs) == 4
    assert second.meta["orientation"]["degrees"] == 270
    assert second.meta["orientation"]["from_cache"] is True
    assert second.meta["orientation"]["extra_calls"] == 0
    assert second.meta["orientation"]["answers"] == first.meta["orientation"]["answers"]
    crop_ref = up.vision.read_refs[-1]
    assert "/r270/0-0-500-500.jpg" in crop_ref
    assert _quadrants(up.image_of(crop_ref)) == ("red",) * 4  # 转正后画面的左上角
    assert _ROTATED_NOTE.format(n=270) in second.content


async def test_the_cache_file_records_the_conclusion_and_all_answers(tmp_path: Path) -> None:
    up = _setup(tmp_path, _stored(180))

    await up.look(image_ref=up.ref)

    first = up.vision.orientation_refs[0]
    cache = up.path_of(first).parent / "orientation.json"
    stored = json.loads(cache.read_text())
    assert stored["conclusion"] == "rotated"
    assert stored["degrees"] == 180
    assert stored["answers"] == {"0": "no", "90": "no", "180": "yes", "270": "no"}


async def test_parallel_looks_at_one_image_ask_once(tmp_path: Path) -> None:
    up = _setup(tmp_path, _stored(90))

    results = await asyncio.gather(*(up.look(image_ref=up.ref) for _ in range(3)))

    assert len(up.vision.orientation_refs) == 4
    assert sorted(r.meta["orientation"]["from_cache"] for r in results) == [False, True, True]
    assert all(r.content.startswith(SECRET) for r in results)


async def test_a_new_run_asks_again(tmp_path: Path) -> None:
    up = _setup(tmp_path, _stored(90))

    await up.look(image_ref=up.ref)
    up.ctx = _ctx(up.ctx.tenant_id, up.ctx.user_id)  # type: ignore[arg-type]
    result = await up.look(image_ref=up.ref)

    assert len(up.vision.orientation_refs) == 8
    assert result.meta["orientation"]["from_cache"] is False


async def test_the_same_box_at_zero_and_ninety_degrees_is_two_files(tmp_path: Path) -> None:
    up = _setup(tmp_path, _stored(0), on=False)
    box = (0, 0, 500, 500)

    zero = (await up.tool._crop_image(up.ref, [box], ctx=up.ctx)).crops[0]
    turned = (await up.tool._crop_image(up.ref, [box], ctx=up.ctx, degrees=90)).crops[0]
    turned_again = (await up.tool._crop_image(up.ref, [box], ctx=up.ctx, degrees=90)).crops[0]

    assert zero.ref != turned.ref
    assert "/r90/" in turned.ref and "/r90/" not in zero.ref
    assert up.path_of(zero.ref).exists() and up.path_of(turned.ref).exists()
    assert (zero.reused, turned.reused, turned_again.reused) == (False, False, True)
    assert _quadrants(up.image_of(zero.ref)) == ("red",) * 4
    assert _quadrants(up.image_of(turned.ref)) == ("blue",) * 4  # 顺时针 90 后左上角是原左下


# ---------------------------------------------------------------------------
# 区域与横条:原像素裁,坐标在转正后的画面上
# ---------------------------------------------------------------------------


def _big_stored(turn: int, size: tuple[int, int] = (3000, 1500)) -> Image.Image:
    """转正后 ``size``(默认 3000x1500,450 万像素,过 auto 门槛)。"""
    image = _upright().resize(size, Image.Resampling.NEAREST)
    return image.rotate(turn, expand=True)


async def test_auto_plans_bands_on_the_turned_frame_and_reads_the_whole_candidate(
    tmp_path: Path,
) -> None:
    """转正后 5000x1000(宽:高 5,超过 4:1)该切竖条;存下来的是 1000x5000,按它规划会切横条。"""
    up = _setup(tmp_path, _big_stored(90, (5000, 1000)))

    result = await up.look(image_ref=up.ref, region="auto")

    assert result.meta["orientation"]["degrees"] == 90
    assert result.meta["auto_pixels"] == 5000 * 1000
    bands = result.meta["bands"]
    assert len(bands) >= 3
    for band in bands:
        _x1, y1, _x2, y2 = band["box"]
        assert (y1, y2) == (0, 1000)  # 整高的竖条
        width, height = band["pixels"]
        assert height == 1000 and width < 5000
    reads = up.vision.read_refs
    assert all("/r90/" in ref for ref in reads)
    assert any(ref.endswith(f"/r90/{_FULL}") for ref in reads)  # 并行那次整张读用候选副本


async def test_upright_without_exif_keeps_every_reference_as_today(tmp_path: Path) -> None:
    on = _setup(tmp_path / "on", _stored(0))
    off = _setup(tmp_path / "off", _stored(0), on=False)

    for up in (on, off):
        await up.look(image_ref=up.ref)
        await up.look(image_ref=up.ref, region=[0, 0, 500, 500])

    def tail(up: _Setup, ref: str) -> str:
        return "<original>" if ref == up.ref else ref.split(f"/{up.ctx.run_id}/", 1)[1]

    assert [tail(on, r) for r in on.vision.read_refs] == [
        tail(off, r) for r in off.vision.read_refs
    ]
    assert on.vision.read_refs[0] == on.ref


# ---------------------------------------------------------------------------
# 带方向标记的工作区图片
# ---------------------------------------------------------------------------


def _exif_jpeg(upright_view: Image.Image, orientation: int) -> bytes:
    """存成 JPEG,方向标记 ``orientation``;按标记转正后看到的是 ``upright_view``。"""
    undo = {6: Image.Transpose.ROTATE_90, 8: Image.Transpose.ROTATE_270}[orientation]
    exif = Image.Exif()
    exif[0x0112] = orientation
    return _encode(upright_view.transpose(undo), "JPEG", quality=95, exif=exif.tobytes())


def _as_crop(image: Image.Image) -> Image.Image:
    """与宿主裁图同一条编码路径(RGB、JPEG 质量 92)之后的像素。"""
    return _decode(_encode(image.convert("RGB"), "JPEG", quality=92))


async def test_an_exif_tagged_workspace_image_is_asked_upright_by_its_tag_then_turned(
    tmp_path: Path,
) -> None:
    raw = _exif_jpeg(_stored(90), orientation=6)
    up = _setup(tmp_path, _stored(0))
    rel = f"agents/{_KEY}/threads/{_CONV}/work/photo.jpg"
    target = up.root / str(up.ctx.tenant_id) / str(up.ctx.user_id) / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(raw)

    result = await up.look(path="/workspace/photo.jpg")

    first = up.vision.orientation_refs[0]
    tag_upright = ImageOps.exif_transpose(Image.open(io.BytesIO(raw)))
    assert up.image_of(first).tobytes() == _as_crop(tag_upright).tobytes()
    assert result.meta["orientation"]["degrees"] == 90
    assert result.content.startswith(SECRET)
    (read_ref,) = up.vision.read_refs
    assert read_ref.endswith(f"/r90/{_FULL}")
    expected = _as_crop(tag_upright.transpose(Image.Transpose.ROTATE_270))
    assert up.image_of(read_ref).tobytes() == expected.tobytes()


async def test_an_upright_exif_tagged_image_is_read_from_its_upright_copy(tmp_path: Path) -> None:
    raw = _exif_jpeg(_stored(0), orientation=8)
    up = _setup(tmp_path, _stored(0))
    rel = f"agents/{_KEY}/threads/{_CONV}/work/photo.jpg"
    target = up.root / str(up.ctx.tenant_id) / str(up.ctx.user_id) / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(raw)

    result = await up.look(path="/workspace/photo.jpg")

    assert result.meta["orientation"]["conclusion"] == "upright"
    (read_ref,) = up.vision.read_refs
    assert read_ref == up.vision.orientation_refs[0]  # 0 度候选(已按标记转正)
    assert result.content == SECRET


# ---------------------------------------------------------------------------
# 不问的情形
# ---------------------------------------------------------------------------


async def test_without_a_workspace_nothing_is_asked(tmp_path: Path) -> None:
    up = _setup(tmp_path, _stored(90))
    tool = AskImageTool(vl_caller=up.vision, image_resolver=up.resolver, ask_orientation=True)

    result = await tool.call({"image_ref": up.ref, "question": "?"}, ctx=up.ctx)

    assert up.vision.orientation_refs == []
    assert "orientation" not in result.meta


async def test_a_crop_reference_is_not_asked_about(tmp_path: Path) -> None:
    up = _setup(tmp_path, _stored(90), on=False)
    crop = (await up.tool._crop_image(up.ref, [(0, 0, 1000, 1000)], ctx=up.ctx)).crops[0]
    tool = AskImageTool(
        vl_caller=up.vision,
        image_resolver=up.resolver,
        workspace_store=up.tool.workspace_store,
        ask_orientation=True,
    )

    result = await tool.call({"image_ref": crop.ref, "question": "?"}, ctx=up.ctx)

    assert up.vision.orientation_refs == []
    assert "orientation" not in result.meta


# ---------------------------------------------------------------------------
# 运维紧急开关(装配):不看模型,只看开关
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("switch", "expected"),
    [(None, True), ("", True), ("1", True), ("0", False), ("off", False), (" FALSE ", False)],
)
@pytest.mark.parametrize(
    "vision",
    [
        _vision_spec(),
        _vision_spec("doubao-seed-2-1-pro-260915"),
        VisionSpec(model=ModelSpec(provider="glm", name="glm-4.6v")),
        VisionSpec(model=ModelSpec(provider="self-hosted", name="not-in-the-catalog")),
    ],
    ids=["doubao-260628", "doubao-260915", "glm-4.6v", "off-catalog"],
)
async def test_assembly_asks_for_every_vision_model_unless_the_switch_is_off(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    switch: str | None,
    expected: bool,
    vision: VisionSpec,
) -> None:
    if switch is None:
        monkeypatch.delenv(ORIENTATION_ENV, raising=False)
    else:
        monkeypatch.setenv(ORIENTATION_ENV, switch)
    up = _setup(tmp_path, _stored(90), on=False)

    assert (await _registered(up, vision)).ask_orientation is expected


async def _always_yes(_ref: str, _image: Image.Image) -> str:
    return "yes"


@pytest.mark.parametrize("turn", [0, 90])
@pytest.mark.parametrize("region", [None, [0, 0, 500, 500], "auto"])
async def test_a_model_that_always_says_yes_changes_nothing_but_one_call_and_the_meta(
    tmp_path: Path, turn: int, region: Any
) -> None:
    """一个对什么都答 yes 的看图模型无害:与开关关掉相比,只多 1 次调用和 meta 里的
    ``orientation``;交给模型读的 ref、回答、其余 meta 全都一样。"""
    image = _big_stored(turn) if region == "auto" else _stored(turn)
    on = _setup(tmp_path / "on", image, orient=_always_yes)
    off = _setup(tmp_path / "off", image, on=False)
    args: dict[str, Any] = {} if region is None else {"region": region}

    with_check = await on.look(image_ref=on.ref, **args)
    without = await off.look(image_ref=off.ref, **args)

    def normalise(up: _Setup, text: Any) -> Any:
        dumped = json.dumps(text, sort_keys=True).replace(up.ref, "<ref>")
        for value, mark in (
            (up.ctx.run_id, "<run>"),
            (up.ctx.tenant_id, "<tenant>"),
            (up.ctx.user_id, "<user>"),
        ):
            dumped = dumped.replace(str(value), mark)
        return json.loads(dumped)

    assert with_check.content == without.content
    assert len(on.vision.calls) == len(off.vision.calls) + 1
    assert len(on.vision.orientation_refs) == 1
    assert [normalise(on, r) for r in on.vision.read_refs] == [
        normalise(off, r) for r in off.vision.read_refs
    ]
    meta = dict(with_check.meta)
    assert meta.pop("orientation")["conclusion"] == "upright"
    assert normalise(on, meta) == normalise(off, dict(without.meta))


# ---------------------------------------------------------------------------
# 纯计算:方向值合成、尺寸、解析、结论、缓存格式、落点形状
# ---------------------------------------------------------------------------


def _patterned(size: tuple[int, int]) -> Image.Image:
    """每个像素都不一样的小图:任何一种翻转 / 旋转错了都看得出来。"""
    image = Image.new("RGB", size)
    width, height = size
    image.putdata(
        [
            (x * 37 % 256, y * 59 % 256, (x + y) * 13 % 256)
            for y in range(height)
            for x in range(width)
        ]
    )
    return image


@pytest.mark.parametrize("orientation", range(1, 9))
@pytest.mark.parametrize("degrees", [0, 90, 180, 270])
@pytest.mark.parametrize("box", [(0, 0, 1000, 1000), (100, 250, 700, 900)])
def test_a_turned_crop_equals_exif_upright_then_turned_then_cropped(
    orientation: int, degrees: int, box: tuple[int, int, int, int]
) -> None:
    exif = Image.Exif()
    exif[0x0112] = orientation
    tagged = Image.open(io.BytesIO(_encode(_patterned((7, 5)), "PNG", exif=exif.tobytes())))
    expected = ImageOps.exif_transpose(tagged)
    if degrees:
        expected = expected.transpose(_CLOCKWISE[degrees])
    expected = expected.crop(region_zoom.pixel_box(box, expected.size))

    turned = region_zoom.turned_orientation(region_zoom._orientation(tagged), degrees)
    got = region_zoom.crop_upright(tagged, box, turned)

    assert region_zoom._orientation(tagged) == orientation
    assert got.size == expected.size
    assert got.convert("RGB").tobytes() == expected.convert("RGB").tobytes()


def test_image_size_follows_the_turn() -> None:
    data = _encode(_patterned((7, 5)))
    assert [region_zoom.image_size(data, d) for d in (0, 90, 180, 270)] == [
        (7, 5),
        (5, 7),
        (7, 5),
        (5, 7),
    ]
    with pytest.raises(ValueError, match="degrees"):
        region_zoom.turned_orientation(1, 45)


def test_exif_orientation_reads_the_tag_or_one() -> None:
    assert region_zoom.exif_orientation(_exif_jpeg(_upright(), 6)) == 6
    assert region_zoom.exif_orientation(_encode(_upright())) == 1
    assert region_zoom.exif_orientation(b"not an image") == 1


@pytest.mark.parametrize(
    ("text", "word"),
    [
        ("yes", "yes"),
        ("Yes.", "yes"),
        ("**NO**", "no"),
        ("none", "none"),
        ("None (no text)", "none"),
        ("<think>it looks upright, yes</think>\nno", "no"),
        ("thinking about it... </think> yes", "yes"),
        ("- yes", "yes"),
        ("The text is upright.", None),
        ("", None),
        ("nope", None),
    ],
)
def test_the_reply_parser_takes_the_first_word(text: str, word: str | None) -> None:
    assert image_orientation.parse_orientation_reply(text) == word


@pytest.mark.parametrize(
    ("answer", "conclusion"),
    [
        ("yes", "upright"),
        ("none", "no_text"),
        ("no", None),
        ("off_topic", "not_asked"),
        ("timeout", "not_asked"),
        ("error", "not_asked"),
    ],
)
def test_the_first_step(answer: Any, conclusion: str | None) -> None:
    assert image_orientation.first_conclusion(answer) == conclusion


@pytest.mark.parametrize(
    ("answers", "decision"),
    [
        ({90: "yes", 180: "no", 270: "no"}, ("rotated", 90)),
        ({90: "no", 180: "no", 270: "yes"}, ("rotated", 270)),
        ({90: "no", 180: "none", 270: "yes"}, ("rotated", 270)),
        ({90: "no", 180: "no", 270: "no"}, ("undetermined", 0)),
        ({90: "yes", 180: "yes", 270: "no"}, ("undetermined", 0)),
        ({90: "yes", 180: "timeout", 270: "no"}, ("undetermined", 0)),
        ({90: "yes", 180: "off_topic", 270: "no"}, ("undetermined", 0)),
        ({90: "yes", 270: "no"}, ("undetermined", 0)),
    ],
)
def test_the_second_step_needs_three_answers_and_exactly_one_yes(
    answers: dict[int, Any], decision: tuple[str, int]
) -> None:
    assert image_orientation.turned_conclusion(answers) == decision


def test_the_cache_round_trips_and_refuses_anything_malformed() -> None:
    orientation = image_orientation.Orientation(
        "rotated", 90, {"0": "no", "90": "yes", "180": "no", "270": "no"}, "vl", extra_calls=4
    )
    back = image_orientation.Orientation.from_cache_bytes(orientation.cache_bytes())
    assert back is not None
    assert back.meta() == orientation.meta() | {"from_cache": True, "extra_calls": 0}
    for raw in (
        b"not json",
        b"[]",
        b'{"conclusion": "sideways", "degrees": 0, "answers": {}, "model": null}',
        b'{"conclusion": "rotated", "degrees": 0, "answers": {}, "model": null}',
        b'{"conclusion": "upright", "degrees": 90, "answers": {}, "model": null}',
        b'{"conclusion": "upright", "degrees": 45, "answers": {}, "model": null}',
        b'{"conclusion": "upright", "degrees": 0, "answers": {"0": 1}, "model": null}',
        b'{"conclusion": "upright", "degrees": 0, "answers": {"0": "maybe"}, "model": null}',
        b'{"conclusion": "upright", "degrees": 0, "answers": {}, "model": 3}',
    ):
        assert image_orientation.Orientation.from_cache_bytes(raw) is None, raw


def test_the_notes_say_only_what_happened() -> None:
    assert image_orientation.Orientation("upright").note() is None
    assert image_orientation.Orientation("no_text").note() is None
    assert image_orientation.Orientation("not_asked").note() is None
    rotated = image_orientation.Orientation("rotated", 180).note()
    assert rotated is not None and "rotated 180 degrees clockwise" in rotated
    assert image_orientation.Orientation("undetermined").note() == (
        image_orientation.UNDETERMINED_NOTE
    )


_RUN = "99999999-9999-4999-8999-999999999999"
_SHA = "a" * 16


@pytest.mark.parametrize(
    ("tail", "is_crop"),
    [
        (f".tool_results/{_RUN}/crops/{_SHA}/0-0-500-500.jpg", True),
        (f".tool_results/{_RUN}/crops/{_SHA}/r90/0-0-1000-1000.jpg", True),
        (f".tool_results/{_RUN}/crops/{_SHA}/r180/0-0-500-500.jpg", True),
        (f".tool_results/{_RUN}/crops/{_SHA}/r270/0-0-500-500.jpg", True),
        (f".tool_results/{_RUN}/crops/{_SHA}/r0/0-0-500-500.jpg", False),
        (f".tool_results/{_RUN}/crops/{_SHA}/r45/0-0-500-500.jpg", False),
        (f".tool_results/{_RUN}/crops/{_SHA}/orientation.json", False),
        (f".tool_results/{_RUN}/crops/{_SHA}/r90/orientation.json", False),
    ],
)
def test_turned_crops_keep_the_crop_shape(tail: str, is_crop: bool) -> None:
    assert is_rendered_crop_rel(tail) is is_crop
    if tail.endswith(".jpg"):  # 图片 ref 只能是图片扩展名
        ref = workspace_figure_ref(uuid4(), uuid4(), f"agents/{_KEY}/{tail}")
        assert is_cacheable_image_ref(ref) is is_crop
        assert parse_rendered_figure_ref(ref) is None
