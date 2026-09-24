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
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, Protocol
from uuid import UUID

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage

from expert_work.common.observability import ExpertWorkComponent, expert_work_span
from expert_work.protocol.multimodal import (
    WORKSPACE_REF_PREFIX,
    parse_image_ref,
    parse_workspace_image_ref,
)
from orchestrator.multimodal import (
    ImageResolver,
    image_ref_block,
    is_missing_file,
    parse_rendered_figure_ref,
    unreadable_workspace_image_text,
)
from orchestrator.tools._guards import usage_total
from orchestrator.tools.figure_lookup import resolve_rendered_figure
from orchestrator.tools.file_ops import _require_path
from orchestrator.tools.registry import ToolBlockedError, ToolContext, ToolResult, ToolSpec
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

AskImageDepth = Literal["quick", "deep"]
_DEPTHS: tuple[AskImageDepth, ...] = ("quick", "deep")

_DEPTH_DESCRIPTION = (
    "Optional, default 'quick': the vision model answers with thinking off (or its lowest "
    "thinking tier) — right for reading text, recognising content, finding numbers. Pass "
    "'deep' only when the answer needs reasoning, e.g. inferring a chart trend or "
    "understanding a complex layout; it is much slower."
)


class VLUsageMeter(Protocol):
    """B-64 Task 9 —— 一次 VL 调用的记账回调(实现见 ``orchestrator.vl_metering``)。"""

    async def __call__(
        self, response: AIMessage, *, tenant_id: UUID, user_id: UUID | None
    ) -> None: ...


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
    #: B-64 Task 10 —— 单次 VL 调用的整体上限;只有测试会改它。
    timeout_s: float = ASK_IMAGE_TIMEOUT_S

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
        ref_str, resolved_from = await self._image_ref_from_args(args, ctx=ctx)
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
        caller = self.vl_caller
        if depth == "quick" and self.quick_vl_caller is not None:
            caller = self.quick_vl_caller
        with expert_work_span(ExpertWorkComponent.ORCHESTRATOR, "vision"):
            response = await self._call_with_limit(caller, messages, depth=depth, ctx=ctx)
        # B-64 Task 9 —— VL 的开销与主模型同样算数:先扣全树共享 token 池(B3),再落
        # ``token_usage``。与 agent 节点处理主模型那次调用同序;VL 调用抛错 / 被取消时
        # 两样都不做,也与主模型一致。
        if ctx.token_budget is not None:
            ctx.token_budget.add(usage_total(response.usage_metadata))
        if self.usage_meter is not None:
            await self.usage_meter(response, tenant_id=ctx.tenant_id, user_id=ctx.user_id)
        answer = _stringify(response.content) or "[VL model returned no text]"
        # Surface VL provenance in the ToolMessage artifact (event stream /
        # audit): the image ref plus the VL call's token usage — otherwise the
        # separate VL round-trip's cost is invisible (the tool only returns text).
        meta: dict[str, Any] = {"image_ref": ref_str, "depth": depth}
        if resolved_from is not None:
            meta["resolved_from"] = resolved_from
        if response.usage_metadata:
            meta["vl_usage"] = dict(response.usage_metadata)
        return ToolResult(content=answer, meta=meta)

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
        底层流随之关闭,不是只停止等待。超时抛 ``TimeoutError``:``tools`` 节点把它包成
        ``status="error"`` 的 ToolMessage、错误分类记成 ``transient``。调用没有返回,
        所以调用方不扣池也不记账(与 Task 9「失败不记账」一致)。
        """
        limit_s = self.timeout_s
        by_run_deadline = False
        if ctx.deadline_at is not None:
            remaining_s = ctx.deadline_at - time.monotonic()
            if remaining_s < limit_s:
                limit_s, by_run_deadline = remaining_s, True
        if limit_s <= 0:
            msg = "ask_image was not run: this run has no time left before its deadline."
            raise TimeoutError(msg)
        limit = asyncio.timeout(limit_s)
        try:
            async with limit:
                return await caller(messages=messages, tools=[])
        except TimeoutError as exc:
            # 只改写本层限时触发的那一种;调用自己抛出的 TimeoutError 原样上抛。
            if not limit.expired():
                raise
            raise TimeoutError(_timeout_message(limit_s, depth, by_run_deadline)) from exc

    async def _image_ref_from_args(
        self, args: Mapping[str, Any], *, ctx: ToolContext
    ) -> tuple[str, dict[str, Any] | None]:
        """两种用法二选一,返回 ``(image_ref, resolved_from)``。

        B-64 Task 8 —— ``path`` + ``unit`` 只负责**找到** ref;找到之后回到调用方,
        走 ``image_ref`` 那条路的全部校验(tenant / user / agent_key 三项、
        :meth:`_require_readable_workspace_image`),不为短形态另写一套。
        ``resolved_from`` 只在短形态下非 ``None``,记进 meta 便于真栈比对。
        """
        has_ref = _given(args, "image_ref")
        has_path = _given(args, "path")
        has_unit = _given(args, "unit")
        if self.workspace_store is None:
            if has_path or has_unit:
                msg = "ask_image here only accepts 'image_ref'; 'path' / 'unit' are not available"
                raise ValueError(msg)
            return _require_string(args, "image_ref"), None
        if has_ref and (has_path or has_unit):
            msg = (
                "ask_image takes either 'image_ref' or 'path' + 'unit', not both — "
                "for a document page you rendered with read_page, pass only 'path' + 'unit'"
            )
            raise ValueError(msg)
        if has_ref:
            return _require_string(args, "image_ref"), None
        if not has_path and not has_unit:
            msg = (
                "ask_image needs either 'image_ref' (an uploaded image) or 'path' + 'unit' "
                "(a document page you rendered with read_page)"
            )
            raise ValueError(msg)
        if not (has_path and has_unit):
            msg = (
                "ask_image 'path' and 'unit' go together: 'path' is the document path you "
                "gave read_page, 'unit' is the page number you rendered"
            )
            raise ValueError(msg)
        unit = _require_unit(args)
        path = _require_path(args, tool="ask_image", agent_key=ctx.agent_key)
        if ctx.tenant_id is None or ctx.user_id is None:
            msg = "ask_image 'path' + 'unit' requires a tenant and user binding"
            raise ToolBlockedError(msg)
        ref = await resolve_rendered_figure(
            self.workspace_store,
            tenant_id=ctx.tenant_id,
            user_id=ctx.user_id,
            agent_key=ctx.agent_key,
            path=path,
            unit=unit,
            run_id=ctx.run_id,
        )
        return ref, {"path": path, "unit": unit}

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
            "in exactly one of two ways: (1) a document page you rendered with read_page "
            "— pass 'path' (the same document path you gave read_page) and 'unit' (the "
            "page number you rendered; for .docx the figure number read_page used). "
            "Prefer this for document pages: do not copy long references by hand. "
            "(2) 'image_ref' — an ``expert_work://image/...`` reference attached to the "
            "user message, for images the user uploaded. Ask narrow, specific questions; "
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
                        "Document page form: the document path exactly as you passed it "
                        "to read_page. Use together with 'unit'."
                    ),
                },
                "unit": {
                    "type": "integer",
                    "minimum": 1,
                    "description": (
                        "Document page form: one of the 'units' you passed to read_page "
                        "(page/slide number; for .docx the figure number)."
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


def _timeout_message(limit_s: float, depth: AskImageDepth, by_run_deadline: bool) -> str:
    seconds = f"{limit_s:.0f} seconds"
    if by_run_deadline:
        seconds += ", the time this run had left"
    msg = (
        f"ask_image timed out ({seconds}) and got no answer from the vision model. "
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
