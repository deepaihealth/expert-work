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

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from langchain_core.messages import HumanMessage, SystemMessage

from expert_work.common.observability import ExpertWorkComponent, expert_work_span
from expert_work.protocol.multimodal import (
    WORKSPACE_REF_PREFIX,
    parse_image_ref,
    parse_workspace_image_ref,
)
from orchestrator.multimodal import ImageResolver, image_ref_block
from orchestrator.tools.registry import ToolBlockedError, ToolContext, ToolResult, ToolSpec

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

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="ask_image",
            description=(
                "Look at an uploaded image and answer a specific question about "
                "it. ``image_ref`` must be a ``expert_work://image/...`` reference the "
                "user message attached. Ask narrow, specific questions; call "
                "ask_image repeatedly with sharper follow-ups if the first "
                "answer is too vague — the image stays accessible."
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
        ref_str = _require_string(args, "image_ref")
        question = _require_string(args, "question")
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
        with expert_work_span(ExpertWorkComponent.ORCHESTRATOR, "vision"):
            response = await self.vl_caller(messages=messages, tools=[])
        answer = _stringify(response.content) or "[VL model returned no text]"
        # Surface VL provenance in the ToolMessage artifact (event stream /
        # audit): the image ref plus the VL call's token usage — otherwise the
        # separate VL round-trip's cost is invisible (the tool only returns text).
        meta: dict[str, Any] = {"image_ref": ref_str}
        if response.usage_metadata:
            meta["vl_usage"] = dict(response.usage_metadata)
        return ToolResult(content=answer, meta=meta)


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
