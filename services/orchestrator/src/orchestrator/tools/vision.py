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
import logging
import time
import weakref
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, Protocol
from uuid import UUID

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage

from expert_work.common.observability import ExpertWorkComponent, expert_work_span
from expert_work.common.observability.metrics import expert_work_counter
from expert_work.protocol.multimodal import (
    WORKSPACE_REF_PREFIX,
    parse_image_ref,
    parse_workspace_image_ref,
)
from expert_work.runtime.middleware.llm_error_handling import (
    LLMClientError,
    LLMUnauthorizedError,
)
from orchestrator.llm.truncation import (
    ServedOutputCap,
    is_unusable_truncation,
    output_truncated_total,
)
from orchestrator.multimodal import (
    WORKSPACE_IMAGE_EXTENSIONS,
    ImageResolver,
    image_ref_block,
    is_missing_file,
    parse_rendered_figure_ref,
    unreadable_workspace_image_text,
)
from orchestrator.tools._guards import usage_total
from orchestrator.tools.error_classifier import GuidedTimeoutError, GuidedToolError
from orchestrator.tools.figure_lookup import FigureNotRenderedError, resolve_rendered_figure
from orchestrator.tools.file_ops import _require_path
from orchestrator.tools.read_page import figure_scope, workspace_figure_ref
from orchestrator.tools.registry import ToolBlockedError, ToolContext, ToolResult, ToolSpec
from orchestrator.tools.workspace_paths import agent_relative, resolve_workspace_path
from orchestrator.tools.workspace_scope import scoped_path
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

AskImageDepth = Literal["quick", "deep"]
_DEPTHS: tuple[AskImageDepth, ...] = ("quick", "deep")

_DEPTH_DESCRIPTION = (
    "Optional, default 'quick': the vision model answers with thinking off (or its lowest "
    "thinking tier) — right for reading text, recognising content, finding numbers. Pass "
    "'deep' only when the answer needs reasoning, e.g. inferring a chart trend or "
    "understanding a complex layout; it is much slower."
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
class _Located:
    """``_image_ref_from_args`` 的结果:要看的 ref、它从哪来、代调 ``read_page`` 带回的 state。"""

    ref: str
    resolved_from: dict[str, Any] | None = None
    state_updates: Mapping[str, Any] | None = None


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
            # reference. Pure read.
            is_read_only=True,
        )

    async def call(self, args: Mapping[str, Any], *, ctx: ToolContext) -> ToolResult:
        if ctx.tenant_id is None:
            msg = "ask_image requires a tenant binding"
            raise ToolBlockedError(msg)
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
        if ref_str.startswith(WORKSPACE_REF_PREFIX):
            await self._require_readable_workspace_image(ref_str)
        # Round-trip the image through the VL model. The provider adapter
        # resolves the ``image_ref`` content block to bytes via the same
        # shared resolver threaded into the VL caller (PR3 + PR4 + PR6).
        messages = [
            SystemMessage(content=_SYSTEM_PROMPT),
            HumanMessage(
                content=[
                    {"type": "text", "text": question},
                    image_ref_block(ref_str),
                ]
            ),
        ]
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
        # B-64 Task 9 —— VL 的开销与主模型同样算数:先扣全树共享 token 池(B3),再落
        # ``token_usage``。与 agent 节点处理主模型那次调用同序;VL 调用抛错 / 被取消时
        # 两样都不做,也与主模型一致。
        if ctx.token_budget is not None:
            ctx.token_budget.add(usage_total(response.usage_metadata))
        if self.usage_meter is not None:
            await self.usage_meter(response, tenant_id=ctx.tenant_id, user_id=ctx.user_id)
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
        # Surface VL provenance in the ToolMessage artifact (event stream /
        # audit): the image ref plus the VL call's token usage — otherwise the
        # separate VL round-trip's cost is invisible (the tool only returns text).
        meta: dict[str, Any] = {"image_ref": ref_str, "depth": depth}
        if resolved_from is not None:
            meta["resolved_from"] = resolved_from
        if deep_timed_out:
            meta["deep_timed_out"] = True
        if response.usage_metadata:
            meta["vl_usage"] = dict(response.usage_metadata)
        return ToolResult(content=answer, meta=meta, state_updates=located.state_updates or {})

    async def _ask(
        self,
        messages: Sequence[BaseMessage],
        *,
        depth: AskImageDepth,
        ctx: ToolContext,
        deep_fallback: bool = True,
    ) -> AIMessage:
        """按 ``depth`` 选路由调一次看图模型。``deep_fallback=False``:快看被拒时不退回正常看图。"""
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
            if self.plain_vl_caller is None or not _quick_rejected(exc):
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
        limit_s = self.timeout_s
        if depth == "deep" and self.quick_vl_caller is not None:
            # B-257 —— deep 超时有快看兜底(B-252),不必等满整体上限。
            limit_s = min(self.deep_timeout_s, limit_s)
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
            )

        try:
            ref = await find()
        except FigureNotRenderedError:
            if self.page_renderer is None:
                raise
            # B-251 —— 文档在、这一页没渲染或已过期:代模型调一次 read_page(同一个工具
            # 实例、同一套沙箱)再找。它回来的 state 原样带回去,与模型自己调一样。
            key = (ctx.run_id, path)
            lock = self._render_locks.get(key)
            if lock is None:
                lock = self._render_locks[key] = asyncio.Lock()
            async with lock:
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
            "vague — the image stays accessible. By default it is a quick look (good for "
            "reading text, recognising content, finding numbers); pass depth='deep' only "
            "for questions that need reasoning, such as a chart's trend or a complex layout."
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
            },
            "required": ["question"],
        },
        # Stream L.L6 — VL LLM call against an immutable image reference. Pure read.
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
    手写 ``expert_work://workspace/...`` 的那条路(审查回修:B-236 之前就能这样写)。"""
    parts = rel.split("/")
    if len(parts) >= 4 and parts[0] == "agents" and parts[2] == "threads":
        own = ctx.workspace_view.own_thread
        if own is None or parts[3] != str(own):
            msg = "ask_image image_ref points into another conversation's directory"
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
