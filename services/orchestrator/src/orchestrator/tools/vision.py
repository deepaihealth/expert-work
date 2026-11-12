"""Image-question tool — Stream J.6 Path B.

When the agent's main model isn't multimodal (``ModelSpec.supports_vision``
is false), the manifest declares a ``vision:`` block carrying a separate
VL model. This tool routes image-understanding questions to that VL
model, leaving the main reasoning loop on the strong text model.

The agent calls ``ask_image(image_ref, question)``; the tool sends a
one-shot ``[SystemMessage, HumanMessage(content=[text, image_ref block])]``
to the VL caller (reusing the PR3 adapter's content-block translation —
no separate image encoding here). The text answer comes back as a
``ToolResult`` for the agent loop to consume.

The tool is **stateless and repeatable** — the agent can re-interrogate
the same image with sharper questions if the first answer is too vague.
See ``docs/streams/STREAM-J-DESIGN.md`` § 13.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
import weakref
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any, Literal, Protocol
from uuid import UUID

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage

from expert_work.common.observability import ExpertWorkComponent, expert_work_span
from expert_work.common.observability.metrics import expert_work_counter
from expert_work.persistence import (
    RENDERED_CROP_DIR,
    RENDERED_FIGURE_SHA_HEX_LEN,
    WORKSPACE_OVERFLOW_DIR,
    region_crop_stem,
)
from expert_work.protocol.multimodal import (
    WORKSPACE_REF_PREFIX,
    ImageRef,
    WorkspaceImageRef,
    parse_image_ref,
    parse_workspace_image_ref,
)
from expert_work.runtime.middleware.llm_error_handling import (
    LLMClientError,
    LLMUnauthorizedError,
)
from expert_work.runtime.storage.base import ObjectNotFoundError
from orchestrator.llm.truncation import (
    ServedOutputCap,
    is_unusable_truncation,
    output_truncated_total,
)
from orchestrator.multimodal import (
    UPLOAD_IMAGE_GONE_TEXT,
    WORKSPACE_IMAGE_EXTENSIONS,
    ImageResolver,
    image_ref_block,
    is_missing_file,
    parse_rendered_figure_ref,
    unreadable_workspace_image_text,
    upload_image_id_for_log,
)
from orchestrator.tools._guards import usage_total
from orchestrator.tools.error_classifier import GuidedTimeoutError, GuidedToolError
from orchestrator.tools.figure_lookup import (
    FigureNotRenderedError,
    _ScopeLister,
    resolve_rendered_figure,
)
from orchestrator.tools.file_ops import _require_path
from orchestrator.tools.image_orientation import (
    CACHE_NAME,
    ORIENTATION_MIN_BUDGET_S,
    ORIENTATION_QUESTION,
    ORIENTATION_TIMEOUT_S,
    TURNS,
    Answer,
    Orientation,
    ask_image_orientation_off_topic_total,
    ask_image_orientation_total,
    first_conclusion,
    parse_orientation_reply,
    turned_conclusion,
)
from orchestrator.tools.question_scope import is_count_question, is_whole_image_question
from orchestrator.tools.read_page import (
    RENDER_DPI,
    RegionCrop,
    RegionRender,
    RegionStaleError,
    figure_scope,
    workspace_figure_ref,
)
from orchestrator.tools.region_zoom import (
    AUTO_GATE_PIXELS,
    AUTO_REGION,
    DOCUMENT_ZOOM_DPI,
    HOST_CROP_SLOTS,
    LARGE_IMAGE_PIXELS,
    REGION_HINT,
    Box,
    CroppedImage,
    Region,
    Tile,
    band_axis,
    crop_image,
    crop_output_size,
    exif_orientation,
    image_size,
    parse_region,
    plan_bands,
)
from orchestrator.tools.registry import ToolBlockedError, ToolContext, ToolResult, ToolSpec
from orchestrator.tools.tile_reading import (
    RAW_META_CHARS,
    TileResult,
    decide_auto,
    decide_whole_question,
    parse_tile_reply,
    tile_meta,
    tile_question,
)
from orchestrator.tools.workspace_paths import (
    PLATFORM_DIRS,
    agent_relative,
    conversation_work_prefix,
    resolve_workspace_path,
)
from orchestrator.tools.workspace_scope import SCOPE_USER_ROOT, scoped_path
from orchestrator.tools.workspace_store import WorkspaceStore

if TYPE_CHECKING:
    # Imported under TYPE_CHECKING only — a runtime import of
    # ``orchestrator.llm`` here would cycle (llm → tools.registry → tools).
    from orchestrator.llm import LLMCaller

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = (
    "You are a vision assistant. Look at the image and answer the user's "
    "question precisely and concretely. Cite what you see; do not add "
    "caveats. If the image does not show what's asked, say so plainly."
)


#: B-64 Task 10 —— 一次 ``ask_image`` 的 VL 调用整体限时(秒)。数字取自 hermes-agent
#: ``vision_analyze`` 看图调用的默认超时,**不是自拍的数**。路由自己的首 token / 空闲
#: 超时对「一直在吐思考 token」的推理模型不触发(实测同类问题 11s 与 234s 两次),
#: 所以这里在工具层再加一道整体上限。
ASK_IMAGE_TIMEOUT_S = 120.0

#: B-257 —— 有单独的快看路由时,deep 最多等这么久就改用快看(B-252 的退路)。测试环境
#: 30 天:quick p95 15 s、最长 28 s;deep 中位 43 s、p90 107 s,约 23 % 等满 120 s。
#: 没有快看路由(deep 超时没有退路)时 deep 仍用 :data:`ASK_IMAGE_TIMEOUT_S`。
ASK_IMAGE_DEEP_TIMEOUT_S = 60.0

#: B-252 —— deep 超时后自动用快看再看一次的次数。
ask_image_deep_retry_total = expert_work_counter(
    "expert_work_ask_image_deep_timeout_quick_retry_total",
    "ask_image calls whose depth='deep' look timed out and were retried once with the "
    "quick look (B-252).",
)

#: B-105 —— 快看的「关思考」参数被厂商拒(4xx)、退回正常看图的次数。``model`` 是看图主模型名。
vision_quick_fallback_total = expert_work_counter(
    "expert_work_vision_quick_fallback_total",
    "ask_image quick looks rejected by the vendor and retried with the configured thinking.",
    ("model",),
)

#: B-257 —— deep 路由带的平台默认思考档位被厂商拒(4xx)、改用配置原样的路由再看的次数。
vision_deep_default_fallback_total = expert_work_counter(
    "expert_work_vision_deep_default_fallback_total",
    "ask_image deep looks whose platform-default thinking depth (B-257) was rejected by the "
    "vendor and retried with the configuration as written.",
    ("model",),
)

#: B-266 —— ``region="auto"`` 走了哪条路(横条 / 退回整张读 / 门槛挡回)与原因。
ask_image_region_auto_total = expert_work_counter(
    "expert_work_ask_image_region_auto_total",
    "ask_image region='auto' looks by the path taken: bands (a band value was used), "
    "full_fallback (the parallel whole-image answer was used), gated (the image was small "
    "enough to read whole) or whole_question (B-273: a whole-image question, answered from the "
    "whole image), with the reason (B-266).",
    ("path", "reason"),
)

#: B-266 review —— 进程内同时在飞的「切块」看图调用上限。8 个并行 ``ask_image`` 各切 6 块
#: 就是 48 个调用;超出的块排队,排队时间算在单次调用的上限里(等不到就按超时记)。
MAX_TILE_CALLS_IN_FLIGHT = 12
_TILE_SLOTS: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore] = (
    weakref.WeakKeyDictionary()
)


#: B-266 review round 2 g —— 宿主裁图的进程内名额(:data:`HOST_CROP_SLOTS`),在**进线程之前**
#: 用 ``asyncio.Semaphore`` 拿:线程池里的线程从不阻塞着等。信号量绑定在创建它的循环上,按循环各一个。
_HOST_CROP_GATES: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore] = (
    weakref.WeakKeyDictionary()
)


async def _crop_in_thread(data: bytes, boxes: Sequence[Box], degrees: int) -> list[CroppedImage]:
    loop = asyncio.get_running_loop()
    gate = _HOST_CROP_GATES.get(loop)
    if gate is None:
        gate = _HOST_CROP_GATES[loop] = asyncio.Semaphore(HOST_CROP_SLOTS)
    async with gate:
        return await asyncio.to_thread(crop_image, data, boxes, degrees)


def _tile_slots() -> asyncio.Semaphore:
    """本事件循环的切块调用闸(信号量绑定在创建它的循环上,所以按循环各一个)。"""
    loop = asyncio.get_running_loop()
    slots = _TILE_SLOTS.get(loop)
    if slots is None:
        slots = _TILE_SLOTS[loop] = asyncio.Semaphore(MAX_TILE_CALLS_IN_FLIGHT)
    return slots


#: B-301 —— 整张图的框:候选副本 = 对它带角度裁一次(自动缩到约 150 万像素)。
_FULL_FRAME: Box = (0, 0, 1000, 1000)

AskImageDepth = Literal["quick", "deep"]
_DEPTHS: tuple[AskImageDepth, ...] = ("quick", "deep")

_DEPTH_DESCRIPTION = (
    "Optional, default 'quick': the vision model answers with thinking off (or its lowest "
    "thinking tier) — right for reading text, recognising content, finding numbers. Pass "
    "'deep' only when the answer needs reasoning, e.g. inferring a chart trend or "
    "understanding a complex layout; it is much slower."
)


_REGION_DESCRIPTION = (
    "Optional: look at part of the image at higher resolution. 'auto' = read one specific "
    "small printed value, name or date in a large, dense image (always a quick look; depth "
    "is ignored; small images are simply read whole); not for counting, comparisons or "
    "questions about the whole image. [x1, y1, x2, y2] = one box on a 0-1000 scale "
    "(0 <= x1 < x2 <= 1000, 0 <= y1 < y2 <= 1000, at least 1% of the image)."
)


class VLUsageMeter(Protocol):
    """B-64 Task 9 —— 一次 VL 调用的记账回调(实现见 ``orchestrator.usage_metering``)。"""

    async def __call__(
        self, response: AIMessage, *, tenant_id: UUID, user_id: UUID | None
    ) -> None: ...


class PageRenderer(Protocol):
    """B-251 —— 代模型渲一页用的 ``read_page``(就是注册表里那个工具实例)。"""

    async def call(self, args: Mapping[str, Any], *, ctx: ToolContext) -> ToolResult:
        """渲 ``args["path"]`` 的 ``args["units"]``,与模型自己调 ``read_page`` 同一个入口。"""

    async def render_regions(
        self,
        *,
        path: str,
        figure_ref: str,
        boxes: Sequence[Box],
        ctx: ToolContext,
        bands: bool = False,
    ) -> RegionRender:
        """B-266 —— 这一页(``figure_ref`` 是它的整页渲染)按 ``boxes`` 各渲一张高分辨率局部。"""


class _AskImageTimeoutError(GuidedTimeoutError):
    """本层限时触发的超时;``by_run_deadline`` 为真表示是 run 的剩余时间用完了。"""

    def __init__(
        self, message: str, *, advice: str, by_run_deadline: bool, waited_s: float = 0.0
    ) -> None:
        super().__init__(message, advice=advice)
        self.by_run_deadline = by_run_deadline
        #: B-257 —— 这次实际等了多久(秒),改用快看时写进回答开头。
        self.waited_s = waited_s


@dataclass(frozen=True)
class _Look:
    """一次看图的结果:回答、实际用的 depth(deep 超时会改成 quick)、VL 用量。"""

    answer: str
    depth: AskImageDepth
    deep_timed_out: bool = False
    usage: dict[str, Any] | None = None


@dataclass(frozen=True)
class _Located:
    """``_image_ref_from_args`` 的结果:要看的 ref、它从哪来、代调 ``read_page`` 带回的 state。"""

    ref: str
    resolved_from: dict[str, Any] | None = None
    state_updates: Mapping[str, Any] | None = None
    #: B-301 —— 本 run 里这张图读之前顺时针转的角度:横条 / 区域按转正后的画面裁、规划。
    degrees: int = 0
    #: B-301 —— 整张看用的 ref(转过、或带方向标记时是候选副本);``None`` = 原图 ``ref``。
    whole_ref: str | None = None
    #: B-301 —— 方向结论;``None`` = 没做这一步(能力没开、不是上传图 / 工作区图片……)。
    orientation: Orientation | None = None

    @property
    def look_ref(self) -> str:
        """整张看交给模型的 ref。"""
        return self.whole_ref or self.ref


@dataclass(frozen=True)
class AskImageTool:
    """The ``ask_image`` tool — Stream J.6 Path B.

    Routes one image-understanding question to a separately-declared VL
    model so the main reasoning loop stays on the strong text model.
    Stateless — the agent can call it repeatedly with sharper questions
    to re-interrogate the same image.
    """

    vl_caller: LLMCaller
    #: B-64 —— 这是一个 ``DispatchingImageResolver``(生产环境),自己认得上传
    #: ref 与工作区 ref 两种 scheme;工具这一层不需要单独再接一根线。
    image_resolver: ImageResolver
    #: B-64 Task 8 —— ``path`` + ``unit`` 短形态在宿主侧找渲染页要读工作区。
    #: ``None`` 时短形态不存在:只收 ``image_ref``,描述里也不提短形态。
    workspace_store: WorkspaceStore | None = None
    #: B-64 Task 9 —— VL 调用落 ``token_usage`` 的记账回调。``None`` = 这次构建没接
    #: 用量存储(测试 / 无控制面),与主模型那边不装 ``TokenUsageMiddleware`` 同义。
    usage_meter: VLUsageMeter | None = None
    #: B-64 Task 10 —— ``depth="quick"``(默认)走的路由:看图模型按各自厂商的「关思考 /
    #: 最低档」发送。``None`` = 与 ``vl_caller`` 相同(整条链上没有可关的思考),
    #: quick 与 deep 走同一条路由。
    quick_vl_caller: LLMCaller | None = None
    #: B-257 —— 配置原样(不补平台默认档位)的路由:默认档位被厂商拒时、以及快看被拒时
    #: 退回它。``None`` = 默认档位什么都没改,与 ``vl_caller`` 相同。
    plain_vl_caller: LLMCaller | None = None
    #: B-64 Task 10 —— 单次 VL 调用的整体上限;只有测试会改它。
    timeout_s: float = ASK_IMAGE_TIMEOUT_S
    #: B-257 —— 有快看路由时 deep 的上限(不超过 ``timeout_s``);只有测试会改它。
    deep_timeout_s: float = ASK_IMAGE_DEEP_TIMEOUT_S
    #: B-105 —— 看图主模型名,只用作快看退回计数器的标签。
    vl_model_name: str = ""
    #: B-105 —— 看图模型(含备用树)的截断标签 / 上限解析器:实际应答的是谁、它的输出上限。
    #: ``None`` = 没接(测试),截断标签记 ``vl_model_name``、上限按厂商默认报。
    output_cap_resolver: ServedOutputCap | None = None
    #: B-301 —— 读上传图 / 工作区图片之前先问方向。装配处按运维紧急开关填(默认开,所有看图模型
    #: 都做,``image_orientation.orientation_switch_on``);测试 / 评测直接构造时设它。
    ask_orientation: bool = False
    #: B-301 —— 一次问方向的整体上限(含排队);只有测试会改它。
    orientation_timeout_s: float = ORIENTATION_TIMEOUT_S
    #: B-251 —— 按页看时那一页没渲染 / 已过期,用它先渲一次再看。``None`` = 这个 agent
    #: 没有 ``read_page``:照旧报错,让模型自己去渲。
    page_renderer: PageRenderer | None = None
    #: B-251 回修 —— 同一 run 同一文档的代渲染排队。``ask_image`` 是只读工具,调度器会并行
    #: 跑;两次同时渲同一份文档会在同一个渲染目录里互删(read_page 自己靠「非只读 + 同路径」
    #: 串行,代调绕开了它)。锁只在有人等时存活(弱引用),进程内即可:一个 run 的工具调用
    #: 都在同一个进程里。
    _render_locks: weakref.WeakValueDictionary[tuple[object, str], asyncio.Lock] = field(
        default_factory=weakref.WeakValueDictionary, compare=False, repr=False
    )

    @property
    def spec(self) -> ToolSpec:
        if self.workspace_store is not None:
            return _spec_with_short_form()
        return ToolSpec(
            name="ask_image",
            description=(
                "Look at an uploaded image and answer a specific question about "
                "it. ``image_ref`` must be a ``expert_work://image/...`` reference the "
                "user message attached. Ask narrow, specific questions; call "
                "ask_image repeatedly with sharper follow-ups if the first "
                "answer is too vague — the image stays accessible. By default it is "
                "a quick look (good for reading text and numbers); pass "
                "depth='deep' only for questions that need reasoning."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "image_ref": {
                        "type": "string",
                        "description": (
                            "A ``expert_work://image/...`` reference attached to the user message."
                        ),
                    },
                    "question": {
                        "type": "string",
                        "description": "What to ask about the image — be specific.",
                    },
                    "depth": {
                        "type": "string",
                        "enum": list(_DEPTHS),
                        "description": _DEPTH_DESCRIPTION,
                    },
                },
                "required": ["image_ref", "question"],
            },
            # Stream L.L6 — VL LLM call against an immutable image
            # reference; this variant has no ``region``, so it writes nothing.
            is_read_only=True,
        )

    async def call(self, args: Mapping[str, Any], *, ctx: ToolContext) -> ToolResult:
        if ctx.tenant_id is None:
            msg = "ask_image requires a tenant binding"
            raise ToolBlockedError(msg)
        region = self._require_region(args)
        located = await self._image_ref_from_args(args, ctx=ctx)
        ref_str, resolved_from = located.ref, located.resolved_from
        question = _require_string(args, "question")
        depth = _require_depth(args)
        # B-64 —— 工作区 ref(平台渲出来的文档页)与上传 ref 走两套互相独立的
        # 校验器:parse_workspace_image_ref 是 parse_image_ref 的**兄弟**,不是
        # 分支(见它的 docstring)。但租户校验必须对两条路都执行到 —— 新 scheme
        # 不能绕开它变成跨租户读洞,所以两支各自算出 tenant_of_ref 后走同一句
        # 检查,而不是各写各的判断。字节解析留给 self.image_resolver(生产环境
        # 是 DispatchingImageResolver,两种 scheme 它自己认得,见
        # orchestrator.multimodal 的 docstring)——工具这一层只做校验+分派,
        # 两种 ref 在它之下投递方式完全一样。
        #
        # 工作区 ref 还多带一层上传 ref 没有的边界:``{root}/{tenant}/{user}/``
        # 的 ``user`` 段。上传 ref(``ImageRef``)没有 user 概念,只按 tenant
        # 隔离;工作区 ref 是 per-user 的工作区目录,只查 tenant 只堵了一半 ——
        # 同租户、不同 user 的 ref 字符串不用任何路径穿越或 symlink,单纯换一个
        # UUID 就能读别人的工作区。所以工作区分支在算出 tenant_of_ref 的同时,
        # 立刻就地比对 user(不等共享的租户检查去做,那句检查两种 scheme 都要
        # 走,不该单独为 user 再加一次分支)。
        #
        # B-64 回修 C1 —— tenant/user 还不够:同租户同用户下,``rel`` 现在可能
        # 带 ``agents/<agent_key>/`` 前缀(见 ``parse_workspace_image_ref`` 的
        # C1 段落)。不比 agent_key 就是同租户同用户下**跨 agent** 读 ——一个
        # agent 能拿另一个 agent 渲染出来的页当自己的看。``ctx.agent_key`` 的
        # 空串 ↔ ``None`` 是两套代码分别表达"没绑 agent"的写法(见
        # ``ToolContext.agent_key`` 与 ``WorkspaceImageRef.agent_key`` 各自的
        # docstring),这里统一折成同一个值再比较。
        if ref_str.startswith(WORKSPACE_REF_PREFIX):
            workspace_ref = parse_workspace_image_ref(ref_str)
            tenant_of_ref = workspace_ref.tenant_id
            if workspace_ref.user_id != ctx.user_id:
                msg = "ask_image image_ref user does not match the run user"
                raise ToolBlockedError(msg)
            if workspace_ref.agent_key != (ctx.agent_key or None):
                msg = "ask_image image_ref agent scope does not match the run agent"
                raise ToolBlockedError(msg)
            _refuse_other_conversation(workspace_ref.rel, ctx)
        else:
            tenant_of_ref = parse_image_ref(ref_str).tenant_id  # raises ValueError on malformed
        if tenant_of_ref != ctx.tenant_id:
            msg = "ask_image image_ref tenant does not match the run tenant"
            raise ToolBlockedError(msg)
        if not ref_str.startswith(WORKSPACE_REF_PREFIX):
            _refuse_another_conversations_upload(parse_image_ref(ref_str), ctx)
            await self._require_present_upload_image(ref_str)
        if ref_str.startswith(WORKSPACE_REF_PREFIX):
            _refuse_another_conversations_run(parse_workspace_image_ref(ref_str), ctx)
            await self._require_readable_workspace_image(ref_str)
        # B-266 —— 安全检查都对**原图**做完了;``region`` 只是在本对话自己的目录里多裁一张。
        meta: dict[str, Any] = {"image_ref": ref_str}
        if resolved_from is not None:
            meta["resolved_from"] = resolved_from
        # B-301 —— 先问方向(只对上传图 / 工作区图片,能力开着时);之后整张、横条、区域都按它。
        # 留给读的时间 = 读那一次今天能拿到的上限(``auto`` 一律快看)。
        reserve_s = self._limit_s("quick" if region == AUTO_REGION else depth)
        located = await self._oriented(located, ctx=ctx, reserve_s=reserve_s)
        if located.orientation is not None:
            meta["orientation"] = located.orientation.meta()
        if region == AUTO_REGION:
            answer, auto_meta = await self._look_auto(
                located, question, requested_depth=depth, ctx=ctx
            )
            meta.update(auto_meta)
            return ToolResult(
                content=_with_orientation_note(answer, located),
                meta=meta,
                state_updates=located.state_updates or {},
            )
        look_ref = located.look_ref
        if region is not None:
            crop = (await self._crops(located, [region], ctx=ctx)).crops[0]
            look_ref = crop.ref
            meta["region"] = list(region)
            meta["crop"] = {
                "dpi": crop.dpi,
                "pixels": [crop.width, crop.height],
                "reused": crop.reused,
            }
        look = await self._look(look_ref, question, depth=depth, ctx=ctx)
        answer = look.answer
        if region is None and look.depth == "quick" and await self._wants_region_hint(located):
            answer = f"{answer}\n\n{REGION_HINT}"
        # Surface VL provenance in the ToolMessage artifact (event stream /
        # audit): the image ref plus the VL call's token usage — otherwise the
        # separate VL round-trip's cost is invisible (the tool only returns text).
        meta["depth"] = look.depth
        if look.deep_timed_out:
            meta["deep_timed_out"] = True
        if look.usage:
            meta["vl_usage"] = look.usage
        return ToolResult(
            content=_with_orientation_note(answer, located),
            meta=meta,
            state_updates=located.state_updates or {},
        )

    async def _look(
        self, ref_str: str, question: str, *, depth: AskImageDepth, ctx: ToolContext
    ) -> _Look:
        """一次看图:``depth`` 路由 + deep 超时改快看(B-252)+ 扣池 / 记账 + 截断判定。"""
        # Round-trip the image through the VL model. The provider adapter
        # resolves the ``image_ref`` content block to bytes via the same
        # shared resolver threaded into the VL caller (PR3 + PR4 + PR6).
        messages = _messages(ref_str, question)
        deep_timed_out, waited_s = False, 0.0
        try:
            response = await self._ask(messages, depth=depth, ctx=ctx)
        except _AskImageTimeoutError as exc:
            # B-252 —— deep 超时、run 还有时间、有单独的快看路由:用快看再看一次,而不是把
            # 「换 quick 再试」留给模型 —— 那要多一轮模型思考(10-31 那次一轮就是几十秒到
            # 几分钟)。没有单独的快看路由时两条是同一条,再调一次就是原样重试,不做。
            if depth != "deep" or exc.by_run_deadline or self.quick_vl_caller is None:
                raise
            logger.info("ask_image.deep_timeout_quick_retry")
            ask_image_deep_retry_total.inc()
            deep_timed_out, depth, waited_s = True, "quick", exc.waited_s
            try:
                response = await self._ask(messages, depth=depth, ctx=ctx, deep_fallback=False)
            except LLMClientError as rejected:
                # 快看被厂商拒:平时退回正常看图,这里正常看图刚超时,不再原样等一整个窗口。
                # 报错不再建议「先换快看」—— 快看刚被拒。
                msg = (
                    f"ask_image timed out ({exc.waited_s:.0f} seconds) with depth='deep', and "
                    "the quick look was rejected by the vision model's vendor, so there is no "
                    "answer from the vision model."
                )
                raise _AskImageTimeoutError(
                    msg,
                    advice=_DEEP_AND_QUICK_FAILED_ADVICE,
                    by_run_deadline=False,
                    waited_s=exc.waited_s,
                ) from rejected
        await self._meter(response, ctx=ctx)
        # B-105 —— 截断成空回答(思考吃满上限)不当成「看图模型没说话」:上面已照常扣池 / 记账,
        # 这里计数后给模型一个说清原因的工具错误,别让它原样再问一遍。
        if is_unusable_truncation(response):
            self._raise_truncated(response)
        answer = _stringify(response.content) or "[VL model returned no text]"
        if deep_timed_out:
            answer = (
                f"(depth='deep' timed out after {waited_s:.0f} seconds, so this is the "
                "quick look's answer — it may miss fine detail.)\n" + answer
            )
        usage = dict(response.usage_metadata) if response.usage_metadata else None
        return _Look(answer=answer, depth=depth, deep_timed_out=deep_timed_out, usage=usage)

    async def _meter(self, response: AIMessage, *, ctx: ToolContext) -> None:
        """B-64 Task 9 —— VL 的开销与主模型同样算数:先扣全树共享 token 池(B3),再落
        ``token_usage``。与 agent 节点处理主模型那次调用同序;VL 调用抛错 / 被取消时
        两样都不做,也与主模型一致。B-266:``region="auto"`` 的每一块各记一次。"""
        if ctx.token_budget is not None:
            ctx.token_budget.add(usage_total(response.usage_metadata))
        if self.usage_meter is not None and ctx.tenant_id is not None:
            await self.usage_meter(response, tenant_id=ctx.tenant_id, user_id=ctx.user_id)

    async def _look_auto(
        self,
        located: _Located,
        question: str,
        *,
        requested_depth: AskImageDepth,
        ctx: ToolContext,
    ) -> tuple[str, dict[str, Any]]:
        """B-266 ``region="auto"``(11-03 验证过的重设计)。一律快看(``depth`` 忽略、meta 注明)。

        1. 门槛:图(文档页按 :data:`DOCUMENT_ZOOM_DPI` 算)不到 :data:`AUTO_GATE_PIXELS` → 不切,
           整张快看一次(``gated``);
        2. 否则切 :func:`band_count` 条整宽横条,每条回一小段 JSON;**同时**并行一次整张快看
           (原问题、不加 JSON 要求),两者都计入在飞名额与记账;
        3. 最终答案见 :func:`~orchestrator.tools.tile_reading.decide_auto`;B-273 —— 问题像整体类
           问题(计数 / 合计 / 最高最低 ...)时见 ``decide_whole_question``(以整张读为准)。
           横条与整张读全失败才报错,与整页看失败同一种错误。
        """
        pixels, size, document = await self._zoom_pixels(located)
        meta: dict[str, Any] = {"region": AUTO_REGION, "depth": "quick", "auto_pixels": pixels}
        if requested_depth != "quick":
            meta["depth_ignored"] = requested_depth
        if pixels is not None and pixels < AUTO_GATE_PIXELS:
            look = await self._look(located.look_ref, question, depth="quick", ctx=ctx)
            ask_image_region_auto_total.labels(path="gated", reason="small_image").inc()
            meta.update({"auto": "gated", "auto_reason": "small_image", "vl_calls": 1})
            if look.usage:
                meta["vl_usage"] = look.usage
            content = (
                f"{look.answer}\n\n(region='auto': the image is small enough "
                f"({pixels / 1_000_000:.1f} MP) to read whole, so it was read whole.)"
            )
            return content, meta
        bands = plan_bands(size or (1, 1), pixels or 0)
        try:
            rendered = await self._crops(
                located, [band.box for band in bands], ctx=ctx, bands=document
            )
        except RegionStaleError:
            # 文档在整页渲染之后改过:整张读看的也是那张过期的页,不能拿来回答 —— 与指定框一样报错。
            raise
        except Exception as exc:
            # review round 2 d —— 横条渲不出来(渲染失败、图解不开……):仍整张读一次,明说不确定;
            # 整张读也失败才报错。
            return await self._auto_without_bands(located, question, exc, meta=meta, ctx=ctx)
        crop_by_box = {crop.box: crop for crop in rendered.crops}
        render_failed = dict(rendered.failed)
        looked = [band for band in bands if band.box in crop_by_box]
        prompt = tile_question(question, band_axis(bands))
        full_call = self._ask_tile(_messages(located.look_ref, question), ctx=ctx)
        outcomes = await asyncio.gather(
            full_call,
            *(
                self._ask_tile(_messages(crop_by_box[band.box].ref, prompt), ctx=ctx)
                for band in looked
            ),
            return_exceptions=True,
        )
        full_outcome, band_outcomes = outcomes[0], outcomes[1:]
        by_label: dict[str, TileResult] = {}
        crop_meta: dict[str, dict[str, Any]] = {}
        usages: list[Mapping[str, Any]] = []
        errors: list[Exception] = []
        for band, outcome in zip(looked, band_outcomes, strict=True):
            result, error = await self._tile_result(band, outcome, ctx=ctx)
            by_label[band.label] = result
            if error is not None:
                errors.append(error)
            if isinstance(outcome, AIMessage) and outcome.usage_metadata:
                usages.append(outcome.usage_metadata)
            crop = crop_by_box[band.box]
            crop_meta[band.label] = {"dpi": crop.dpi, "pixels": [crop.width, crop.height]}
        for band in bands:
            if band.label not in by_label:
                why = render_failed.get(band.box, "not rendered")
                by_label[band.label] = TileResult(tile=band, reply=None, error=why)
        results = [by_label[band.label] for band in bands]
        full_answer, full_error, full_exc = await self._full_result(full_outcome, ctx=ctx)
        if isinstance(full_outcome, AIMessage) and full_outcome.usage_metadata:
            usages.append(full_outcome.usage_metadata)
        if full_answer is None and all(result.reply is None for result in results):
            first = errors[0] if errors else full_exc
            if first is not None:
                raise first
            msg = "ask_image region='auto' could not look at this image at all"
            raise RuntimeError(msg)
        # B-273 V1 —— 整体类问题以整张读为准,横条只用来提示一句(见 decide_whole_question)。
        if is_whole_image_question(question):
            decision = decide_whole_question(
                results,
                full_answer=full_answer,
                full_error=full_error,
                count_question=is_count_question(question),
            )
        else:
            decision = decide_auto(results, full_answer=full_answer, full_error=full_error)
        ask_image_region_auto_total.labels(path=decision.path, reason=decision.reason).inc()
        meta.update(
            {
                "auto": decision.path,
                "auto_reason": decision.reason,
                "aggregate": decision.bands,
                "bands": [
                    {**tile_meta(result), **crop_meta.get(result.tile.label, {})}
                    for result in results
                ],
                "full": {"answer": full_answer[:RAW_META_CHARS]}
                if full_answer is not None
                else {"error": full_error},
                "vl_calls": len(looked) + 1,
            }
        )
        if usages:
            meta["vl_usage"] = _sum_usage(usages)
        return decision.content, meta

    async def _full_result(
        self, outcome: AIMessage | BaseException, *, ctx: ToolContext
    ) -> tuple[str | None, str | None, Exception | None]:
        """并行那次整张快看的结果:``(回答, 失败原因, 异常)``;成功的这里记账。"""
        if isinstance(outcome, BaseException) and not isinstance(outcome, Exception):
            raise outcome  # 取消等:照常往上走
        if isinstance(outcome, Exception):
            reason = "timed out" if isinstance(outcome, TimeoutError) else type(outcome).__name__
            return None, reason, outcome
        await self._meter(outcome, ctx=ctx)
        if is_unusable_truncation(outcome):
            try:
                self._raise_truncated(outcome)
            except GuidedToolError as exc:
                return None, "truncated", exc
        text = _stringify(outcome.content).strip()
        if not text:
            return None, "no text", None
        return text, None, None

    async def _auto_without_bands(
        self,
        located: _Located,
        question: str,
        error: Exception,
        *,
        meta: dict[str, Any],
        ctx: ToolContext,
    ) -> tuple[str, dict[str, Any]]:
        """横条准备不出来时:整张快看一次,标不确定并说原因;这一次也失败就报它(原因链上挂横条的错)。"""
        try:
            look = await self._look(located.look_ref, question, depth="quick", ctx=ctx)
        except Exception as full_error:
            raise full_error from error
        ask_image_region_auto_total.labels(path="full_fallback", reason="bands_unavailable").inc()
        # 异常的文字可能带 NAS 路径之类:给模型与 meta 的只有类型和一句固定的话,细节只进日志。
        logger.warning(
            "ask_image.auto_bands_unavailable err=%s", type(error).__name__, exc_info=error
        )
        reason = f"{type(error).__name__}: the region render failed"
        meta.update(
            {
                "auto": "full_fallback",
                "auto_reason": "bands_unavailable",
                "bands_error": type(error).__name__,
                "vl_calls": 1,
            }
        )
        if look.usage:
            meta["vl_usage"] = look.usage
        content = (
            f"{look.answer}\n\n(region='auto': whole-image reading; small print may be unreliable "
            f"- the bands could not be prepared ({reason}). Re-ask with an explicit region, or say "
            "the value is uncertain.)"
        )
        return content, meta

    async def _zoom_pixels(
        self, located: _Located
    ) -> tuple[int | None, tuple[int, int] | None, bool]:
        """``region="auto"`` 门槛用的像素数、图的(转正后)尺寸,以及这是不是一张文档页。

        图片:转正后的原像素。文档页(``path`` + ``unit``):按 :data:`DOCUMENT_ZOOM_DPI` 放大
        之后的页面像素 —— 已有的整页渲染是 100 dpi,乘 (300/100)^2;尺寸给 100 dpi 的(只用来
        看宽高比)。读不出尺寸就是 ``None``。"""
        source = located.resolved_from or {}
        document = (
            isinstance(source.get("path"), str)
            and source.get("unit") is not None
            and self.page_renderer is not None
        )
        try:
            size = image_size(
                (await self.image_resolver.resolve(located.ref)).data, located.degrees
            )
        except Exception:
            logger.debug("ask_image.zoom_pixels_unknown", exc_info=True)
            return None, None, document
        if size is None:
            return None, None, document
        scale = (DOCUMENT_ZOOM_DPI / RENDER_DPI) ** 2 if document else 1
        return round(size[0] * size[1] * scale), size, document

    async def _ask_tile(self, messages: Sequence[BaseMessage], *, ctx: ToolContext) -> AIMessage:
        """一块的快看:先排进 :data:`MAX_TILE_CALLS_IN_FLIGHT` 个名额里,排队不超过单次调用的上限
        (也不超过 run 的剩余时间)。"""
        slots = _tile_slots()
        wait_s = self.timeout_s
        if ctx.deadline_at is not None:
            wait_s = min(wait_s, ctx.deadline_at - time.monotonic())
        try:
            async with asyncio.timeout(max(wait_s, 0.0)):
                await slots.acquire()
        except TimeoutError as exc:
            msg = f"ask_image waited {wait_s:.0f} seconds for a free vision slot and gave up."
            raise _AskImageTimeoutError(
                msg, advice=_TIMEOUT_ADVICE, by_run_deadline=False, waited_s=wait_s
            ) from exc
        try:
            return await self._ask(messages, depth="quick", ctx=ctx)
        finally:
            slots.release()

    async def _tile_result(
        self, tile: Tile, outcome: AIMessage | BaseException, *, ctx: ToolContext
    ) -> tuple[TileResult, Exception | None]:
        """一块的调用结果 → :class:`TileResult`;成功的这里记账,失败的带回异常。"""
        if isinstance(outcome, BaseException) and not isinstance(outcome, Exception):
            raise outcome  # 取消等:照常往上走
        if isinstance(outcome, Exception):
            reason = "timed out" if isinstance(outcome, TimeoutError) else type(outcome).__name__
            return TileResult(tile=tile, reply=None, error=reason), outcome
        await self._meter(outcome, ctx=ctx)
        if is_unusable_truncation(outcome):
            try:
                self._raise_truncated(outcome)
            except GuidedToolError as exc:
                return TileResult(tile=tile, reply=None, error="truncated"), exc
        text = _stringify(outcome.content).strip()
        if not text:
            return TileResult(tile=tile, reply=None, error="no text"), None
        return TileResult(tile=tile, reply=parse_tile_reply(text)), None

    async def _crops(
        self,
        located: _Located,
        boxes: Sequence[Box],
        *,
        ctx: ToolContext,
        bands: bool = False,
    ) -> RegionRender:
        """B-266 —— 按 ``boxes`` 裁出局部:文档页(``path`` + ``unit``)在沙箱里按区域高分辨率渲,
        别的图(上传图、工作区图片)在宿主按原像素裁。都落本对话 ``.tool_results/<run>/crops/``。"""
        source = located.resolved_from or {}
        path, unit = source.get("path"), source.get("unit")
        if isinstance(path, str) and unit is not None:
            if self.page_renderer is None:
                msg = "ask_image 'region' on a document page needs this agent's read_page tool"
                raise ToolBlockedError(msg)
            async with self._render_lock((ctx.run_id, path)):
                return await self.page_renderer.render_regions(
                    path=path, figure_ref=located.ref, boxes=boxes, ctx=ctx, bands=bands
                )
        return await self._crop_image(located.ref, boxes, ctx=ctx, degrees=located.degrees)

    async def _crop_image(
        self, ref_str: str, boxes: Sequence[Box], *, ctx: ToolContext, degrees: int = 0
    ) -> RegionRender:
        """宿主侧裁图:字节经同一个 resolver 拿(上传图来自对象存储),原像素裁,写进本对话
        ``.tool_results/<run>/crops/<源图哈希>/<框>.jpg``;同一张图同一个框已经裁过就直接用。
        B-301 —— ``degrees`` 不是 0 时框在转正后的画面上,文件放进 ``r<degrees>/`` 子目录。"""
        self._require_crop_bindings(ctx)
        data = (await self.image_resolver.resolve(ref_str)).data
        out_dir = self._crop_dir(data, ctx=ctx)
        # 并行的两次 ask_image 看同一张图(常见:同一张化验单问两个值)会写同一批文件:排队,
        # 后来的直接复用 —— 不排队时两边同时写同一个名字,NAS 的写入会互相踩掉。
        async with self._render_lock((ctx.run_id, out_dir)):
            return await self._write_crops(data, out_dir, boxes, degrees=degrees, ctx=ctx)

    def _require_crop_bindings(self, ctx: ToolContext) -> None:
        if (
            self.workspace_store is None
            or ctx.tenant_id is None
            or ctx.user_id is None
            or ctx.run_id is None
        ):
            msg = "ask_image 'region' needs a workspace and a tenant, user and run binding"
            raise ToolBlockedError(msg)

    def _crop_dir(self, data: bytes, *, ctx: ToolContext) -> str:
        """这张图在本 run 的裁图目录(用户根相对):``.tool_results/<run>/crops/<源图哈希>``。"""
        self._require_crop_bindings(ctx)
        source_sha = hashlib.sha256(data).hexdigest()[:RENDERED_FIGURE_SHA_HEX_LEN]
        view = ctx.workspace_view
        return scoped_path(
            figure_scope(view),
            f"{conversation_work_prefix(view)}{WORKSPACE_OVERFLOW_DIR}/{ctx.run_id}/"
            f"{RENDERED_CROP_DIR}/{source_sha}",
        )

    def _crop_ref(self, out_dir: str, box: Box, *, degrees: int, ctx: ToolContext) -> str:
        """``_write_crops`` 给这个框、这个角度写的那个文件的 ref(不碰存储)。"""
        if ctx.tenant_id is None or ctx.user_id is None:
            msg = "ask_image 'region' needs a workspace and a tenant, user and run binding"
            raise ToolBlockedError(msg)
        rel = f"{_crop_target(out_dir, degrees)}/{region_crop_stem(box)}.jpg"
        return workspace_figure_ref(ctx.tenant_id, ctx.user_id, rel)

    async def _write_crops(
        self,
        data: bytes,
        out_dir: str,
        boxes: Sequence[Box],
        *,
        degrees: int,
        ctx: ToolContext,
    ) -> RegionRender:
        """裁 ``boxes`` 写进 ``out_dir``(0 度)或 ``out_dir/r<degrees>``;已有的同名文件直接用。
        调用方持有 ``(run, out_dir)`` 那把锁。"""
        store = self.workspace_store
        if store is None or ctx.tenant_id is None or ctx.user_id is None:
            msg = "ask_image 'region' needs a workspace and a tenant, user and run binding"
            raise ToolBlockedError(msg)
        target = _crop_target(out_dir, degrees)
        names = {region_crop_stem(box) + ".jpg" for box in boxes}
        lister = _ScopeLister(
            store=store, tenant_id=ctx.tenant_id, user_id=ctx.user_id, scope=SCOPE_USER_ROOT
        )
        crops: list[RegionCrop] = []
        existing = await lister.entries_if_present(target)
        reusable = {n for n in names if n in existing and not existing[n].is_dir}
        todo = [box for box in boxes if region_crop_stem(box) + ".jpg" not in reusable]
        pieces = (
            dict(zip(todo, await _crop_in_thread(data, todo, degrees), strict=True)) if todo else {}
        )
        size = image_size(data, degrees) if reusable else None
        for box in boxes:
            rel = f"{target}/{region_crop_stem(box)}.jpg"
            piece = pieces.get(box)
            if piece is not None:
                await store.write_file(
                    tenant_id=ctx.tenant_id, user_id=ctx.user_id, path=rel, data=piece.data
                )
                width, height = piece.width, piece.height
            else:
                width, height = crop_output_size(box, size or (0, 0))
            ref = workspace_figure_ref(ctx.tenant_id, ctx.user_id, rel)
            crops.append(
                RegionCrop(box=box, ref=ref, width=width, height=height, reused=piece is None)
            )
        return RegionRender(crops=tuple(crops))

    async def _oriented(self, located: _Located, *, ctx: ToolContext, reserve_s: float) -> _Located:
        """B-301 —— 读之前定方向(设计 §4.1)。不该问的原样返回;问方向出任何错都按「没问成」、
        按 0 度往下读 —— 看图绝不因为问方向而失败。``reserve_s`` 是留给读的那一份时间
        (见 :meth:`_orientation_budget`)。"""
        if not self._asks_orientation(located, ctx=ctx):
            return located
        try:
            return await self._orient(located, ctx=ctx, reserve_s=reserve_s)
        except Exception:
            logger.warning("ask_image.orientation_failed", exc_info=True)
            ask_image_orientation_total.labels(conclusion="not_asked", degrees="0").inc()
            return replace(located, orientation=Orientation("not_asked", reason="error"))

    def _asks_orientation(self, located: _Located, *, ctx: ToolContext) -> bool:
        """只问上传图与工作区图片文件:不问文档页(``path`` + ``unit``)、平台自己产出的图
        (``.tool_results/`` 下的渲染页 / 裁图 / 候选副本)、能力没开的、没接工作区的构建。"""
        if not self.ask_orientation or self.workspace_store is None:
            return False
        if ctx.tenant_id is None or ctx.user_id is None or ctx.run_id is None:
            return False
        if (located.resolved_from or {}).get("unit") is not None:
            return False
        if located.ref.startswith(WORKSPACE_REF_PREFIX):
            rel = parse_workspace_image_ref(located.ref).rel
            return WORKSPACE_OVERFLOW_DIR not in PurePosixPath(rel).parts
        return True

    async def _orient(self, located: _Located, *, ctx: ToolContext, reserve_s: float) -> _Located:
        """本 run 这张图的方向:有缓存用缓存,没有就问。

        缓存文件与候选副本都在这张图的裁图目录里,并行的 ``ask_image`` 在同一把目录锁上排队,
        后来的读缓存、不再问。整张看用的副本就是问方向时那张候选(问的时候已经写好),所以
        **先备好它、最后才写结论**;用缓存的那次不再写任何文件 —— 同一 run 里画面不会中途换。"""
        data = (await self.image_resolver.resolve(located.ref)).data
        out_dir = self._crop_dir(data, ctx=ctx)
        async with self._render_lock((ctx.run_id, out_dir)):
            orientation = await self._cached_orientation(out_dir, ctx=ctx)
            if orientation is None:
                orientation = await self._ask_orientation(
                    data, out_dir, ctx=ctx, reserve_s=reserve_s
                )
                ask_image_orientation_total.labels(
                    conclusion=orientation.conclusion, degrees=str(orientation.degrees)
                ).inc()
                await self._store_orientation(out_dir, orientation, ctx=ctx)
        whole_ref = None
        if _reads_a_copy(orientation, data):
            whole_ref = self._crop_ref(out_dir, _FULL_FRAME, degrees=orientation.degrees, ctx=ctx)
        return replace(
            located, degrees=orientation.degrees, whole_ref=whole_ref, orientation=orientation
        )

    def _orientation_budget(self, ctx: ToolContext, reserve_s: float) -> float | None:
        """这一轮问方向最多等多久:``orientation_timeout_s``,且 run 的剩余时间里先扣掉留给读的
        ``reserve_s``(读那一次今天能拿到的上限)。扣完不到 :data:`ORIENTATION_MIN_BUDGET_S`
        就是 ``None`` —— 不问,读拿到的时间与今天相同。"""
        budget = self.orientation_timeout_s
        if ctx.deadline_at is not None:
            spare = ctx.deadline_at - time.monotonic() - reserve_s
            if spare < ORIENTATION_MIN_BUDGET_S:
                return None
            budget = min(budget, spare)
        return budget

    async def _ask_orientation(
        self, data: bytes, out_dir: str, *, ctx: ToolContext, reserve_s: float
    ) -> Orientation:
        """问方向的两步(设计 §4.1)。没时间、候选副本裁不出来、第 1 步没答成 = 没问成(按今天
        的样子读);第 1 步答 no 之后没时间问第 2 步也是没问成,不对主模型说什么。"""
        budget = self._orientation_budget(ctx, reserve_s)
        if budget is None:
            return Orientation("not_asked", reason="deadline")
        try:
            zero = await self._write_crops(data, out_dir, [_FULL_FRAME], degrees=0, ctx=ctx)
        except Exception:
            logger.warning("ask_image.orientation_candidate_failed", exc_info=True)
            return Orientation("not_asked", reason="no_copy")
        first, first_model = await self._orientation_answer(zero.crops[0].ref, budget, ctx=ctx)
        answers: dict[str, Answer] = {"0": first}
        models: dict[str, str | None] = {"0": first_model}
        conclusion = first_conclusion(first)
        if conclusion == "not_asked":
            return Orientation(conclusion, 0, answers, models, "no_answer", extra_calls=1)
        if conclusion is not None:
            return Orientation(conclusion, 0, answers, models, extra_calls=1)
        budget = self._orientation_budget(ctx, reserve_s)
        if budget is None:
            return Orientation("not_asked", 0, answers, models, "deadline", extra_calls=1)
        try:
            turned = [
                (await self._write_crops(data, out_dir, [_FULL_FRAME], degrees=turn, ctx=ctx))
                .crops[0]
                .ref
                for turn in TURNS
            ]
        except Exception:
            logger.warning("ask_image.orientation_candidate_failed", exc_info=True)
            return Orientation("undetermined", 0, answers, models, extra_calls=1)
        replies = await asyncio.gather(
            *(self._orientation_answer(ref, budget, ctx=ctx) for ref in turned)
        )
        by_turn: dict[int, Answer] = {}
        for turn, (reply, model) in zip(TURNS, replies, strict=True):
            by_turn[turn] = reply
            answers[str(turn)] = reply
            models[str(turn)] = model
        conclusion, degrees = turned_conclusion(by_turn)
        return Orientation(conclusion, degrees, answers, models, extra_calls=1 + len(TURNS))

    async def _orientation_answer(
        self, ref: str, budget_s: float, *, ctx: ToolContext
    ) -> tuple[Answer, str | None]:
        """问一次「正不正」:快看那条链、被厂商拒不退回正常看图、最多等 ``budget_s``(含排队等
        切块名额)。回答照常扣池 / 记账。返回 ``(回答, 应答的模型)``;**任何** ``Exception``
        (含调用之后的记账)都变成 ``error``,从不往外抛 —— 第 2 步的 gather 因此不会半途抛出、
        留下还在跑的另外两个。"""
        try:
            async with asyncio.timeout(budget_s):
                async with _tile_slots():
                    response = await self._ask(
                        _messages(ref, ORIENTATION_QUESTION),
                        depth="quick",
                        ctx=ctx,
                        deep_fallback=False,
                    )
            await self._meter(response, ctx=ctx)
            model = self._served_model(response)
        except TimeoutError:
            return "timeout", None
        except Exception as exc:
            logger.info("ask_image.orientation_not_answered err=%s", type(exc).__name__)
            return "error", None
        word = parse_orientation_reply(_stringify(response.content))
        if word is None:
            ask_image_orientation_off_topic_total.labels(model=model or "").inc()
            return "off_topic", model
        return word, model

    def _served_model(self, response: AIMessage) -> str | None:
        """备用链上实际应答的模型名;没接解析器(测试)时记看图主模型名。"""
        if self.output_cap_resolver is not None:
            return self.output_cap_resolver(response)[1] or None
        return self.vl_model_name or None

    async def _cached_orientation(self, out_dir: str, *, ctx: ToolContext) -> Orientation | None:
        store = self.workspace_store
        if store is None or ctx.tenant_id is None or ctx.user_id is None:
            return None
        lister = _ScopeLister(
            store=store, tenant_id=ctx.tenant_id, user_id=ctx.user_id, scope=SCOPE_USER_ROOT
        )
        entry = (await lister.entries_if_present(out_dir)).get(CACHE_NAME)
        if entry is None or entry.is_dir:
            return None
        raw = await store.read_file(
            tenant_id=ctx.tenant_id, user_id=ctx.user_id, path=f"{out_dir}/{CACHE_NAME}"
        )
        return Orientation.from_cache_bytes(raw)

    async def _store_orientation(
        self, out_dir: str, orientation: Orientation, *, ctx: ToolContext
    ) -> None:
        """写缓存;写不进去只记日志 —— 这次照样按结论读,同一 run 下次看会再问一次。"""
        store = self.workspace_store
        if store is None or ctx.tenant_id is None or ctx.user_id is None:
            return
        try:
            await store.write_file(
                tenant_id=ctx.tenant_id,
                user_id=ctx.user_id,
                path=f"{out_dir}/{CACHE_NAME}",
                data=orientation.cache_bytes(),
            )
        except Exception:
            logger.warning("ask_image.orientation_cache_write_failed", exc_info=True)

    def _render_lock(self, key: tuple[object, str]) -> asyncio.Lock:
        """同一 run 同一文档的代渲染 / 渲局部排队(见 ``_render_locks``)。"""
        lock = self._render_locks.get(key)
        if lock is None:
            lock = self._render_locks[key] = asyncio.Lock()
        return lock

    async def _wants_region_hint(self, located: _Located) -> bool:
        """B-266 —— 整页快看之后,只有 ``region="auto"`` 不会被门槛挡回来的图才提示它。

        只在 ``region`` 可用(接了工作区)时提示。尺寸只读文件头;读不出来就不提示
        (这条路只决定加不加一句话,不许让看图失败)。"""
        if self.workspace_store is None:
            return False
        figure = parse_rendered_figure_ref(located.ref)
        if figure is not None and figure.region is not None:
            return False
        pixels, _size, _document = await self._zoom_pixels(located)
        return pixels is not None and pixels >= LARGE_IMAGE_PIXELS

    async def _ask(
        self,
        messages: Sequence[BaseMessage],
        *,
        depth: AskImageDepth,
        ctx: ToolContext,
        deep_fallback: bool = True,
    ) -> AIMessage:
        """按 ``depth`` 选路由调一次看图模型。``deep_fallback=False``:被厂商拒时不退回配置原样的
        路由(快看被拒、默认档位被拒都不退)。"""
        if depth == "quick" and self.quick_vl_caller is not None:
            try:
                with expert_work_span(ExpertWorkComponent.ORCHESTRATOR, "vision"):
                    return await self._call_with_limit(
                        self.quick_vl_caller, messages, depth=depth, ctx=ctx
                    )
            except LLMClientError as exc:
                if not _quick_rejected(exc) or not deep_fallback:
                    raise
                # B-105 —— 关思考的参数被这家拒了(目录没实调过的新模型最常见):退回 Agent
                # 原本的看图配置再看一次,而不是让看图整个失败。B-257:是配置原样那条路由,
                # 不带平台默认档位 —— 没实调过的厂商可能连默认档位也拒。
                vision_quick_fallback_total.labels(model=self.vl_model_name).inc()
                logger.warning("ask_image.quick_rejected_fallback err=%s", type(exc).__name__)
                plain = self.plain_vl_caller or self.vl_caller
                with expert_work_span(ExpertWorkComponent.ORCHESTRATOR, "vision"):
                    return await self._call_with_limit(plain, messages, depth=depth, ctx=ctx)
        try:
            with expert_work_span(ExpertWorkComponent.ORCHESTRATOR, "vision"):
                return await self._call_with_limit(self.vl_caller, messages, depth=depth, ctx=ctx)
        except LLMClientError as exc:
            if self.plain_vl_caller is None or not _quick_rejected(exc) or not deep_fallback:
                raise
            # B-257 —— 平台默认档位被这家拒了:用配置原样的路由再看一次(同 B-105 的判法)。
            vision_deep_default_fallback_total.labels(model=self.vl_model_name).inc()
            logger.warning("ask_image.deep_default_rejected_fallback err=%s", type(exc).__name__)
        with expert_work_span(ExpertWorkComponent.ORCHESTRATOR, "vision"):
            return await self._call_with_limit(self.plain_vl_caller, messages, depth=depth, ctx=ctx)

    def _raise_truncated(self, response: AIMessage) -> None:
        if self.output_cap_resolver is not None:
            provider, model, cap = self.output_cap_resolver(response)
        else:
            provider, model, cap = "", self.vl_model_name, None
        output_truncated_total.labels(provider=provider, model=model, usable="false").inc()
        logger.warning("ask_image.output_truncated model=%s", model)
        shown = str(cap) if cap is not None else "厂商默认值"
        msg = (
            f"看图模型输出被截断(已用满输出上限 {shown},含思考)。"
            "请在看图模型配置里调大『输出上限』,或者改用默认的快看。"
        )
        raise GuidedToolError(msg, advice=_TRUNCATED_ADVICE)

    def _limit_s(self, depth: AskImageDepth) -> float:
        """一次看图调用自己的上限(还没按 run 的剩余时间收紧)。"""
        if depth == "deep" and self.quick_vl_caller is not None:
            # B-257 —— deep 超时有快看兜底(B-252),不必等满整体上限。
            return min(self.deep_timeout_s, self.timeout_s)
        return self.timeout_s

    async def _call_with_limit(
        self,
        caller: LLMCaller,
        messages: Sequence[BaseMessage],
        *,
        depth: AskImageDepth,
        ctx: ToolContext,
    ) -> AIMessage:
        """B-64 Task 10 —— 一次 VL 调用,整体不超过 ``timeout_s``,也不超过 run 的剩余时间。

        超时由 ``asyncio.timeout`` 取消正在 await 的调用 —— 取消一路传进路由与厂商适配器,
        底层流随之关闭,不是只停止等待。超时抛 :class:`GuidedTimeoutError`:``tools`` 节点
        把它包成 ``status="error"`` 的 ToolMessage、错误分类记成 ``transient``,但恢复建议
        换成工具自己的(不许原样重试;run 没时间了就别再调),不是只读工具默认的「可安全
        重试一次」。调用没有返回,
        所以调用方不扣池也不记账(与 Task 9「失败不记账」一致)。
        """
        limit_s = self._limit_s(depth)
        by_run_deadline = False
        if ctx.deadline_at is not None:
            remaining_s = ctx.deadline_at - time.monotonic()
            if remaining_s < limit_s:
                limit_s, by_run_deadline = remaining_s, True
        if limit_s <= 0:
            msg = "ask_image was not run: this run has no time left before its deadline."
            raise _AskImageTimeoutError(msg, advice=_NO_TIME_LEFT_ADVICE, by_run_deadline=True)
        limit = asyncio.timeout(limit_s)
        try:
            async with limit:
                return await caller(messages=messages, tools=[])
        except TimeoutError as exc:
            # 只改写本层限时触发的那一种;调用自己抛出的 TimeoutError 原样上抛。
            if not limit.expired():
                raise
            advice = _NO_TIME_LEFT_ADVICE if by_run_deadline else _TIMEOUT_ADVICE
            message = _timeout_message(limit_s, depth, by_run_deadline)
            raise _AskImageTimeoutError(
                message, advice=advice, by_run_deadline=by_run_deadline, waited_s=limit_s
            ) from exc

    def _require_region(self, args: Mapping[str, Any]) -> Region | None:
        """B-266 —— ``region``:要往本对话目录里写裁图,只有接了工作区的那一版才有。"""
        if not _given(args, "region"):
            return None
        if self.workspace_store is None:
            msg = "ask_image here does not accept 'region'"
            raise ValueError(msg)
        return parse_region(args.get("region"), tool="ask_image")

    async def _image_ref_from_args(self, args: Mapping[str, Any], *, ctx: ToolContext) -> _Located:
        """三种用法,返回要看的 ref 与它的来历。

        B-64 Task 8 —— ``path`` + ``unit`` 只负责**找到** ref;找到之后回到调用方,
        走 ``image_ref`` 那条路的全部校验(tenant / user / agent_key 三项、
        :meth:`_require_readable_workspace_image`),不为短形态另写一套。B-250 的
        「``path`` 指向一张图」同样只是换算出工作区 ref,校验仍走那一条路。
        ``resolved_from`` 只在后两种用法下非 ``None``,记进 meta 便于真栈比对。
        """
        has_ref = _given(args, "image_ref")
        has_path = _given(args, "path")
        has_unit = _given(args, "unit")
        if self.workspace_store is None:
            if has_path or has_unit:
                msg = "ask_image here only accepts 'image_ref'; 'path' / 'unit' are not available"
                raise ValueError(msg)
            return _Located(_require_string(args, "image_ref"))
        if has_ref and (has_path or has_unit):
            msg = (
                "ask_image takes either 'image_ref' or 'path' (+ 'unit'), not both — "
                "for a document page or an image file in the workspace, pass only 'path'"
            )
            raise ValueError(msg)
        if has_ref:
            raw = _require_string(args, "image_ref")
            # B-250 —— 10-31 那次模型把工作区里的 ``work/x.png`` 填进了 ``image_ref``;
            # 不是平台 ref、又是图片路径,就当 ``path`` 处理,省一轮「参数不对」。
            if not raw.startswith("expert_work://") and _is_image_path(raw):
                return await self._workspace_image(raw, ctx=ctx)
            return _Located(raw)
        if has_path and not has_unit and _is_image_path(args.get("path")):
            return await self._workspace_image(args["path"], ctx=ctx)
        if not has_path and not has_unit:
            msg = (
                "ask_image needs either 'image_ref' (an uploaded image), 'path' (an image "
                "file in the workspace) or 'path' + 'unit' (a document page)"
            )
            raise ValueError(msg)
        if not (has_path and has_unit):
            msg = (
                "ask_image 'path' and 'unit' go together for a document: 'path' is the "
                "document path, 'unit' the page number (an image file — "
                f"{'/'.join(sorted(WORKSPACE_IMAGE_EXTENSIONS))} — needs only 'path')"
            )
            raise ValueError(msg)
        unit = _require_unit(args)
        path = _require_path(args, tool="ask_image", view=ctx.workspace_view).path
        if ctx.tenant_id is None or ctx.user_id is None:
            msg = "ask_image 'path' + 'unit' requires a tenant and user binding"
            raise ToolBlockedError(msg)
        store, tenant_id, user_id = self.workspace_store, ctx.tenant_id, ctx.user_id

        async def find() -> str:
            return await resolve_rendered_figure(
                store,
                tenant_id=tenant_id,
                user_id=user_id,
                view=ctx.workspace_view,
                path=path,
                unit=unit,
                run_id=ctx.run_id,
                conversation_run_ids=ctx.figure_run_ids,
            )

        try:
            ref = await find()
        except FigureNotRenderedError:
            if self.page_renderer is None:
                raise
            # B-251 —— 文档在、这一页没渲染或已过期:代模型调一次 read_page(同一个工具
            # 实例、同一套沙箱)再找。它回来的 state 原样带回去,与模型自己调一样。
            async with self._render_lock((ctx.run_id, path)):
                try:  # 排队时别人可能已经渲好了
                    return _Located(await find(), {"path": path, "unit": unit})
                except FigureNotRenderedError:
                    pass
                rendered = await self.page_renderer.call({"path": path, "units": [unit]}, ctx=ctx)
                try:
                    ref = await find()
                except FigureNotRenderedError as exc:
                    msg = f"ask_image 先调了 read_page 渲染这一页,没有成功:{rendered.content}"
                    raise FigureNotRenderedError(msg) from exc
            return _Located(
                ref,
                {"path": path, "unit": unit, "auto_rendered": True},
                rendered.state_updates,
            )
        return _Located(ref, {"path": path, "unit": unit})

    async def _workspace_image(self, raw_path: Any, *, ctx: ToolContext) -> _Located:
        """B-250 —— 工作区里的一张图 → 工作区 ref。路径按本 run 的视图解析(与文件工具同一套
        越界 / 保留目录校验),换算成用户根相对路径;读不读得到交给 :meth:`call` 里那道检查。"""
        path = _require_path({"path": raw_path}, tool="ask_image", view=ctx.workspace_view).path
        if ctx.tenant_id is None or ctx.user_id is None:
            msg = "ask_image 'path' requires a tenant and user binding"
            raise ToolBlockedError(msg)
        view = ctx.workspace_view
        target = resolve_workspace_path(path, view=view, tool="ask_image")
        rel = scoped_path(figure_scope(view), agent_relative(target, view))
        return _Located(workspace_figure_ref(ctx.tenant_id, ctx.user_id, rel), {"path": path})

    async def _require_readable_workspace_image(self, ref_str: str) -> None:
        """B-64 Task 7 回修第 2 轮 —— 工作区图读不出来就让**这一次工具调用**显式失败。

        适配器那一层(``multimodal.resolve_message_images``)对读不出来的工作区图
        降级成一段文字,因为另一个选择是整次 LLM 调用失败。到了 ``ask_image`` 这里,
        那段文字会被交给 VL 模型,VL 回一句「看不到图」,主模型会把这句**当成看图的
        结果** —— 该有失败信号的地方没有信号。所以这里先解析一次,失败就抛,文字与
        Path A 的降级同一句(:func:`~orchestrator.multimodal.unreadable_workspace_image_text`),
        VL 不被调用。文件真不在了(``ENOENT``)抛 ``FileNotFoundError``,``tools`` 节点
        把它包成 ``status="error"`` 的 ToolMessage、错误分类记成 ``resource_not_found``;
        其余原因(权限、安全拒绝、部署没接 NAS……)抛 ``RuntimeError``,文字明说
        「读不了、重渲也没用」—— 不许把「够不着」说成「不存在」。原始异常挂在
        ``__cause__`` 上。

        **这不保证 VL 永远拿不到降级文字。** 这次解析成功之后、VL caller 里的适配器
        再解析之前,文件仍可能被删(留存清理、并发 run、同一沙箱里的 ``rm``);那个
        窗口里命中,VL 收到的就是降级文字,不是崩溃。外层是
        :class:`~orchestrator.multimodal.CachingImageResolver` 且这条 ref 可缓存时,
        第二次解析多半命中本次填进去的缓存条目,但缓存有容量上限、可能被挤出,所以
        窗口只是变小,没有关掉。代价是不可缓存的工作区 ref 每次多读一遍盘。
        """
        try:
            await self.image_resolver.resolve(ref_str)
        except Exception as exc:
            figure = parse_rendered_figure_ref(ref_str)
            message = unreadable_workspace_image_text(figure, exc)
            # 类型决定 ``tools`` 节点给模型的错误分类与建议:只有文件真不在了才是
            # ``FileNotFoundError``(→ resource_not_found);「读不了」不能说成「不存在」。
            if is_missing_file(exc):
                raise FileNotFoundError(message) from exc
            raise RuntimeError(message) from exc

    async def _require_present_upload_image(self, ref_str: str) -> None:
        """B-283 —— 上传图的字节已经不在了(过了保留期被清理、或被删)就让这一次工具调用显式失败。

        适配器那一层(``multimodal.resolve_message_images``)对这种图降级成一段文字,因为另一
        个选择是这条会话以后每一轮都失败。到了 ``ask_image`` 这里,那段文字会被交给 VL 模型,
        VL 回一句「看不到图」,主模型会把它当成看图的结果 —— 与工作区图那道预检
        (:meth:`_require_readable_workspace_image`)同一个理由。这里抛 ``FileNotFoundError``
        (``tools`` 节点把它包成 ``status="error"``、错误分类 ``resource_not_found``),文字就是
        降级那一句,VL 不被调用。

        **只认「不在了」**:别的失败(对象存储暂时故障……)不在这里报,留给后面真正读图的
        那一次照旧报出来 —— 与加这道预检之前的行为一样。外层是
        :class:`~orchestrator.multimodal.CachingImageResolver` 时,这次读到的字节会被缓存,
        后面 VL 那一次不再多读一遍。
        """
        try:
            await self.image_resolver.resolve(ref_str)
        except ObjectNotFoundError as exc:
            raise FileNotFoundError(UPLOAD_IMAGE_GONE_TEXT) from exc
        except Exception as exc:
            # 只探「不在了」;别的失败留给后面真正读图的那一次报(那里会原样抛出)。
            logger.debug(
                "ask_image.upload_probe_failed image_id=%s error=%s",
                upload_image_id_for_log(ref_str),
                type(exc).__name__,
            )


def _spec_with_short_form() -> ToolSpec:
    """B-64 Task 8 —— 接了 ``workspace_store`` 时的描述:两种用法,文档页优先短形态。"""
    return ToolSpec(
        name="ask_image",
        description=(
            "Ask a vision model a specific question about an image. Point at the image "
            "in exactly one of three ways: (1) an image file in the workspace (png, jpg, "
            "jpeg, webp, gif — e.g. a crop you saved) — pass only 'path'; no need to put it "
            "into a PDF first. (2) a document page — pass 'path' (the document path) and "
            "'unit' (the page number; for .docx the figure number read_page used); if that "
            "page has not been rendered yet it is rendered for you. Do not copy long "
            "references by hand. (3) 'image_ref' — an ``expert_work://image/...`` reference "
            "attached to the user message, for images the user uploaded. Ask narrow, "
            "specific questions; "
            "call ask_image repeatedly with sharper follow-ups if the first answer is too "
            "vague — the image stays accessible. Looking at the whole image is the default. "
            "Use region='auto' only to read one specific small printed value, name or date in "
            "a large, dense page or photo; never use it for counting, comparisons, trends, "
            "'which is highest' or any question about the whole picture. If its result says it "
            "is uncertain, re-ask with an explicit region or tell the user the value is "
            "uncertain. When you already know where something is (e.g. the vision model told "
            "you), pass region=[x1, y1, x2, y2] on a 0-1000 scale to look at just that part. "
            "Pass depth='deep' only when the text is readable and the answer still needs "
            "reasoning, such as a chart's trend or a complex layout."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": (
                        "An image file in the workspace (alone), or a document path "
                        "(together with 'unit')."
                    ),
                },
                "unit": {
                    "type": "integer",
                    "minimum": 1,
                    "description": (
                        "Document page form only: the page/slide number (for .docx the "
                        "figure number read_page listed)."
                    ),
                },
                "image_ref": {
                    "type": "string",
                    "description": (
                        "Uploaded image form: a ``expert_work://image/...`` reference "
                        "attached to the user message. Do not combine with 'path'/'unit'."
                    ),
                },
                "question": {
                    "type": "string",
                    "description": "What to ask about the image — be specific.",
                },
                "depth": {
                    "type": "string",
                    "enum": list(_DEPTHS),
                    "description": _DEPTH_DESCRIPTION,
                },
                "region": {
                    "anyOf": [
                        {"type": "string", "enum": [AUTO_REGION]},
                        {
                            "type": "array",
                            "items": {"type": "integer", "minimum": 0, "maximum": 1000},
                            "minItems": 4,
                            "maxItems": 4,
                        },
                    ],
                    "description": _REGION_DESCRIPTION,
                },
            },
            "required": ["question"],
        },
        # Stream L.L6 —— 对源图只读。B-266 的 ``region`` 会往本 run 自己的
        # ``.tool_results/<run>/crops/`` 写裁图:文件名由源内容哈希 + 框决定、写同样的字节,
        # 文档页的局部在 ``(run, path)`` 锁里渲,图片的裁图在 ``(run, 裁图目录)`` 锁里写;
        # 不改任何别人读的东西,所以调度上仍按只读处理。
        is_read_only=True,
    )


#: B-64 Task 10 —— 超时后的恢复建议,替换 transient + 只读工具默认的「可安全重试一次」:
#: 原样重试只会再耗一整个窗口。
_TIMEOUT_ADVICE = (
    "Do not repeat the identical call. Retry only with a narrower, more specific "
    "question, or with the default quick depth if this call used depth='deep'; "
    "otherwise continue without this image answer."
)
_NO_TIME_LEFT_ADVICE = (
    "This run has no time left. Do not call ask_image again; finish with what you already have."
)
#: B-257 —— deep 超时、改用的快看又被厂商拒:两条路都走过了,不再建议换 quick。
_DEEP_AND_QUICK_FAILED_ADVICE = (
    "Do not repeat the identical call. Retry only with a narrower, more specific question; "
    "otherwise continue without this image answer."
)
#: B-105 —— 看图模型截断:原样再问只会再截一次(上限没变)。
_TRUNCATED_ADVICE = (
    "Do not repeat the identical call: the vision model will hit the same output cap again. "
    "Retry once with the default quick depth if this call used depth='deep', or with a "
    "narrower question; otherwise continue without this image answer and tell the user the "
    "vision model's output cap needs raising."
)


def _timeout_message(limit_s: float, depth: AskImageDepth, by_run_deadline: bool) -> str:
    if by_run_deadline:
        # 不写「0 seconds」:剩余时间不到 1 秒时如实说不到 1 秒。
        left = "less than 1 second" if limit_s < 1 else f"{limit_s:.0f} seconds"
        return (
            f"ask_image timed out after {left}, all the time this run had left, "
            "and got no answer from the vision model. This run has no time left."
        )
    msg = (
        f"ask_image timed out ({limit_s:.0f} seconds) and got no answer from the vision model. "
        "You can try again with a more specific, narrower question"
    )
    if depth == "deep":
        return msg + "; since this call used depth='deep', try the default quick look first."
    return msg + "."


def _require_depth(args: Mapping[str, Any]) -> AskImageDepth:
    raw = args.get("depth")
    if raw is None:
        return "quick"
    for depth in _DEPTHS:
        if raw == depth:
            return depth
    msg = f"ask_image 'depth' must be one of {list(_DEPTHS)}, got {raw!r}"
    raise ValueError(msg)


def _is_image_path(raw: object) -> bool:
    """B-250 —— 一个字符串路径,扩展名是看图模型认的图片格式。"""
    if not isinstance(raw, str) or not raw.strip():
        return False
    name = raw.rsplit("/", 1)[-1]
    stem, dot, suffix = name.rpartition(".")
    return bool(dot and stem) and f".{suffix.lower()}" in WORKSPACE_IMAGE_EXTENSIONS


def _refuse_other_conversation(rel: str, ctx: ToolContext) -> None:
    """B-236 —— 工作区 ref 落在 ``agents/<key>/threads/<id>/`` 底下时,``<id>`` 必须是本对话。

    路径形态(``path``)走 ``resolve_workspace_path``,本来就到不了别的对话;这里补的是模型
    手写 ``expert_work://workspace/...`` 的那条路(审查回修:B-236 之前就能这样写)。B-262
    review —— 分段与读侧同一个算法(``PurePosixPath``;``rel`` 已由解析器保证是规范写法)。"""
    parts = PurePosixPath(rel).parts
    if len(parts) >= 4 and parts[0] == "agents" and parts[2] == "threads":
        own = ctx.workspace_view.own_thread
        if own is None or parts[3] != str(own):
            msg = "ask_image image_ref points into another conversation's directory"
            raise ToolBlockedError(msg)


def _refuse_another_conversations_upload(ref: ImageRef, ctx: ToolContext) -> None:
    """B-266 review —— 上传图 ref 里带着上传时的会话(``thread_id``):只认本对话的。

    以前只比租户:同租户里换一条别的会话的上传 ref 就能看(带 ``region`` 时还会把它的像素
    裁进本对话的工作区)。本对话 = ``ctx.thread_id``;委派出去的子代另认根对话
    (``ctx.workspace_thread_id``)—— 父这一轮的图会原样交给子代。合法来源都是本会话的:
    控制台 / 对外接口建 run 时都要求 ref 的 thread 就是本会话(``runs._validate_image_refs``、
    ``agents.resolve_external_files``),``:edit`` / 重新生成也在同一会话里。

    run 没绑会话(两个都是 ``None``,只有单测 / 合成评测)时无从比较,照旧放行。"""
    own = {thread for thread in (ctx.thread_id, ctx.workspace_thread_id) if thread is not None}
    if own and ref.thread_id not in own:
        msg = (
            "ask_image image_ref is an image uploaded in another conversation; only this "
            "conversation's images can be read"
        )
        raise ToolBlockedError(msg)


def _refuse_another_conversations_run(ref: WorkspaceImageRef, ctx: ToolContext) -> None:
    """B-262 —— ref 落在 agent 根(没绑 agent 时是用户根)的平台目录下时:

    * ``.tool_results/<run>/``:``<run>`` 必须是本对话的 —— ``ctx.figure_run_ids``(本对话渲染
      过页的 run,子代含父链)或本 run。两种视图都是(迁移过来的老对话要用自己 run 的页)。
    * 对话视图里 ``uploads/`` / ``inputs/``(含预拉缓存)一律拒:exec 视图里它们被盖住,那是
      老视图对话的件。老视图里它们就是本对话的 ``/workspace``,照旧放行。

    那一层是同一 agent 各对话共用的,模型手写的 ref 原来能读到别的对话的件。本对话
    ``threads/<conv>/work/`` 下的由 :func:`_refuse_other_conversation` 管,这里不碰。"""
    parts = PurePosixPath(ref.rel).parts[2 if ref.agent_key is not None else 0 :]
    if not parts or parts[0] not in PLATFORM_DIRS:
        return
    if parts[0] == WORKSPACE_OVERFLOW_DIR:
        own = set(ctx.figure_run_ids) | ({ctx.run_id} if ctx.run_id is not None else set())
        try:
            run = UUID(parts[1]) if len(parts) > 1 else None
        except ValueError:
            run = None
        if run is not None and run in own:
            return
    elif not ctx.workspace_view.per_conversation:
        return
    msg = (
        "ask_image image_ref points into another conversation's files; only this "
        f"conversation's own files can be read ({parts[0]}/ under the agent directory is not)"
    )
    raise ToolBlockedError(msg)


def _given(args: Mapping[str, Any], key: str) -> bool:
    """参数算不算「给了」:缺席、``null``、空白字符串都算没给。"""
    value = args.get(key)
    if value is None:
        return False
    return not (isinstance(value, str) and not value.strip())


def _require_unit(args: Mapping[str, Any]) -> int:
    raw = args.get("unit")
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 1:
        msg = "ask_image 'unit' must be a positive integer (a page number you rendered)"
        raise ValueError(msg)
    return raw


def _require_string(args: Mapping[str, Any], key: str) -> str:
    raw = args.get(key)
    if not isinstance(raw, str) or not raw.strip():
        msg = f"ask_image requires a non-empty {key!r} string"
        raise ValueError(msg)
    return raw.strip()


def _quick_rejected(exc: LLMClientError) -> bool:
    """B-105 —— 快看被厂商拒、值得用正常看图再试一次:4xx 且不是 401 / 403。

    路由对 4xx 不走备用链、原样抛出(不包成 ``AllProvidersExhaustedError``),所以这里
    直接判异常本身。401 是凭据问题,换一套思考参数一样失败;真路由里它还是 key 级错误,
    轮完整条链后被包成 ``AllProvidersExhaustedError``、根本进不了这里。429
    (``LLMRateLimitError``)不继承 ``LLMClientError``,也进不了这里。403 是权限问题,同理
    不退回;它是普通 ``LLMClientError``,靠 ``classify_http_error`` 填的 ``status`` 认出来。
    """
    return not isinstance(exc, LLMUnauthorizedError) and exc.status != 403


def _stringify(content: Any) -> str:
    """Flatten an ``AIMessage.content`` into plain text — same convention
    as the provider adapters' ``_message_text``."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, Mapping):
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "".join(parts)
    return ""


def _messages(ref_str: str, text: str) -> list[BaseMessage]:
    """一次看图的消息:系统提示 + ``[问题, 图]``。"""
    return [
        SystemMessage(content=_SYSTEM_PROMPT),
        HumanMessage(content=[{"type": "text", "text": text}, image_ref_block(ref_str)]),
    ]


def _crop_target(out_dir: str, degrees: int) -> str:
    """裁图写在哪个目录:0 度就是 ``out_dir``,转过的放 ``r<角度>/`` 子目录(B-301)。"""
    return out_dir if degrees == 0 else f"{out_dir}/r{degrees}"


def _reads_a_copy(orientation: Orientation, data: bytes) -> bool:
    """B-301 —— 整张看要不要用候选副本:转过的、或带方向标记(副本已按标记转正)的用;
    没问成的一律按今天的样子读原图,正放且无标记的也读原图(引用与今天逐字节相同)。
    用副本的这几种结论,问方向时那张副本都已经写好了。"""
    if orientation.conclusion == "not_asked":
        return False
    return orientation.degrees != 0 or exif_orientation(data) != 1


def _with_orientation_note(content: str, located: _Located) -> str:
    """B-301 —— 转过 / 判不出方向时,在工具结果末尾如实说明(设计 §4.3)。"""
    note = located.orientation.note() if located.orientation is not None else None
    return content if note is None else f"{content}\n\n{note}"


def _sum_usage(usages: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    """几次 VL 调用的用量相加(只加三个总数;明细各块自己的回执里有)。"""
    total: dict[str, int] = {}
    for usage in usages:
        for key in ("input_tokens", "output_tokens", "total_tokens"):
            value = usage.get(key)
            if isinstance(value, int):
                total[key] = total.get(key, 0) + value
    return total
