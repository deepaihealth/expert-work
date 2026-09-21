"""Tests for the ``ask_image`` tool — Stream J.6 Path B."""

from __future__ import annotations

import base64
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage

from expert_work.protocol.multimodal import ImageRef
from orchestrator.multimodal import (
    IMAGE_REF_BLOCK_TYPE,
    InMemoryImageResolver,
    NasWorkspaceImageResolver,
    ResolvedImage,
)
from orchestrator.tools.registry import ToolBlockedError, ToolContext
from orchestrator.tools.vision import AskImageTool

_TENANT = UUID("11111111-1111-1111-1111-111111111111")
_THREAD = UUID("22222222-2222-2222-2222-222222222222")


@dataclass
class _FakeVLCaller:
    """Records calls + returns a canned ``AIMessage``."""

    response: AIMessage = field(default_factory=lambda: AIMessage(content="ok"))
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def __call__(self, *, messages: Sequence[BaseMessage], tools: Sequence[Any]) -> AIMessage:
        self.calls.append({"messages": list(messages), "tools": list(tools)})
        return self.response


def _ref(tenant: UUID = _TENANT, ext: str = ".png") -> str:
    return ImageRef(tenant_id=tenant, thread_id=_THREAD, image_id=uuid4(), ext=ext).to_uri()


def _resolver() -> InMemoryImageResolver:
    return InMemoryImageResolver(images={"any": ResolvedImage(media_type="image/png", data=b"PNG")})


def _ctx(tenant_id: UUID | None = _TENANT, *, user_id: UUID | None = None) -> ToolContext:
    return ToolContext(tenant_id=tenant_id, user_id=user_id)


@pytest.mark.asyncio
async def test_ask_image_happy_path() -> None:
    vl = _FakeVLCaller(response=AIMessage(content="a red apple on a desk"))
    tool = AskImageTool(vl_caller=vl, image_resolver=_resolver())
    ref = _ref()

    result = await tool.call({"image_ref": ref, "question": "what is this?"}, ctx=_ctx())

    assert result.content == "a red apple on a desk"
    assert result.meta == {"image_ref": ref}
    # The VL caller saw a system prompt + a human message with the image_ref block.
    sent = vl.calls[0]["messages"]
    assert isinstance(sent[0], SystemMessage)
    assert isinstance(sent[1], HumanMessage)
    content = sent[1].content
    assert isinstance(content, list)
    assert content[0] == {"type": "text", "text": "what is this?"}
    assert content[1] == {"type": IMAGE_REF_BLOCK_TYPE, "ref": ref}


@pytest.mark.asyncio
async def test_ask_image_meta_carries_vl_usage_when_present() -> None:
    # The separate VL round-trip's token usage rides in meta → ToolMessage
    # artifact, so the VL call's cost is observable (it's otherwise invisible —
    # the tool only returns text).
    usage = {"input_tokens": 800, "output_tokens": 40, "total_tokens": 840}
    vl = _FakeVLCaller(response=AIMessage(content="a desk", usage_metadata=usage))
    tool = AskImageTool(vl_caller=vl, image_resolver=_resolver())
    ref = _ref()

    result = await tool.call({"image_ref": ref, "question": "what is this?"}, ctx=_ctx())

    assert result.meta == {"image_ref": ref, "vl_usage": usage}


@pytest.mark.asyncio
async def test_ask_image_empty_text_response_falls_back() -> None:
    vl = _FakeVLCaller(response=AIMessage(content=""))
    tool = AskImageTool(vl_caller=vl, image_resolver=_resolver())
    result = await tool.call({"image_ref": _ref(), "question": "what is this?"}, ctx=_ctx())
    assert result.content == "[VL model returned no text]"


@pytest.mark.asyncio
async def test_ask_image_flattens_block_list_response() -> None:
    blocks = [{"type": "text", "text": "a "}, {"type": "text", "text": "cat"}]
    vl = _FakeVLCaller(response=AIMessage(content=blocks))
    tool = AskImageTool(vl_caller=vl, image_resolver=_resolver())
    result = await tool.call({"image_ref": _ref(), "question": "what is this?"}, ctx=_ctx())
    assert result.content == "a cat"


@pytest.mark.asyncio
async def test_ask_image_requires_tenant_in_ctx() -> None:
    tool = AskImageTool(vl_caller=_FakeVLCaller(), image_resolver=_resolver())
    with pytest.raises(ToolBlockedError, match="tenant"):
        await tool.call({"image_ref": _ref(), "question": "q"}, ctx=_ctx(tenant_id=None))


@pytest.mark.asyncio
async def test_ask_image_rejects_cross_tenant_ref() -> None:
    other_tenant = uuid4()
    tool = AskImageTool(vl_caller=_FakeVLCaller(), image_resolver=_resolver())
    with pytest.raises(ToolBlockedError, match="tenant"):
        await tool.call({"image_ref": _ref(tenant=other_tenant), "question": "q"}, ctx=_ctx())


@pytest.mark.asyncio
async def test_ask_image_rejects_empty_question() -> None:
    tool = AskImageTool(vl_caller=_FakeVLCaller(), image_resolver=_resolver())
    with pytest.raises(ValueError, match="'question'"):
        await tool.call({"image_ref": _ref(), "question": "   "}, ctx=_ctx())


@pytest.mark.asyncio
async def test_ask_image_rejects_missing_image_ref() -> None:
    tool = AskImageTool(vl_caller=_FakeVLCaller(), image_resolver=_resolver())
    with pytest.raises(ValueError, match="'image_ref'"):
        await tool.call({"question": "q"}, ctx=_ctx())


@pytest.mark.asyncio
async def test_ask_image_rejects_malformed_image_ref() -> None:
    tool = AskImageTool(vl_caller=_FakeVLCaller(), image_resolver=_resolver())
    with pytest.raises(ValueError, match="image ref"):
        await tool.call({"image_ref": "not-a-expert-work-ref", "question": "q"}, ctx=_ctx())


def test_ask_image_spec_shape() -> None:
    spec = AskImageTool(vl_caller=_FakeVLCaller(), image_resolver=_resolver()).spec
    assert spec.name == "ask_image"
    assert "image_ref" in spec.parameters["properties"]
    assert "question" in spec.parameters["properties"]
    assert spec.parameters["required"] == ["image_ref", "question"]


# ---------------------------------------------------------------------------
# workspace ref dispatch (B-64) — the sibling scheme, tenant check on both paths
# ---------------------------------------------------------------------------


@dataclass
class _RecordingVLCaller:
    """假 LLMCaller —— 记录它被塞进消息里的真实图片字节。

    工作区 ref 这条路,``AskImageTool`` 自己现地把 NAS 上的字节解出来,内嵌成
    消息块里的 ``resolved_data_uri``(见 ``vision.py``——provider 适配器
    construction 时钉死的共享 resolver 不认工作区 scheme,所以字节不能像上传
    ref 那样留给它事后再解)。这里直接从消息里把字节抠出来,证明分派链路真把
    NAS 上的字节带到了 VL 调用这一步,而不仅仅是把 ref 字符串原样转发。
    """

    answer: str
    seen_bytes: bytes | None = field(default=None, init=False)

    async def __call__(self, *, messages: Sequence[BaseMessage], tools: Sequence[Any]) -> AIMessage:
        for msg in messages:
            content = msg.content
            if not isinstance(content, list):
                continue
            for block in content:
                if isinstance(block, Mapping) and "resolved_data_uri" in block:
                    _, _, encoded = str(block["resolved_data_uri"]).partition(",")
                    self.seen_bytes = base64.b64decode(encoded)
        return AIMessage(content=self.answer)


@pytest.mark.anyio
async def test_ask_image_accepts_a_workspace_ref(tmp_path: Path) -> None:
    """按 scheme 分派到 NAS resolver,VL 模型拿到的是真字节。"""
    tenant, user = uuid4(), uuid4()
    page = tmp_path / str(tenant) / str(user) / ".tool_results" / "r1" / "figures" / "abc"
    page.mkdir(parents=True)
    (page / "page-03.jpg").write_bytes(b"\xff\xd8\xff\xe0fake-jpeg")

    caller = _RecordingVLCaller(answer="曲线从 6.1 降到 5.5")
    tool = AskImageTool(
        vl_caller=caller,
        image_resolver=InMemoryImageResolver({}),
        workspace_image_resolver=NasWorkspaceImageResolver(root=tmp_path),
    )
    ref = f"expert_work://workspace/{tenant}/{user}/.tool_results/r1/figures/abc/page-03.jpg"
    result = await tool.call(
        {"image_ref": ref, "question": "走势如何"},
        ctx=_ctx(tenant_id=tenant, user_id=user),
    )
    assert "5.5" in result.content
    assert caller.seen_bytes == b"\xff\xd8\xff\xe0fake-jpeg"


@pytest.mark.anyio
async def test_ask_image_rejects_a_cross_tenant_workspace_ref(tmp_path: Path) -> None:
    """租户校验对两种 ref 都执行 —— 新 scheme 不是绕过它的后门。"""
    mine, theirs, user = uuid4(), uuid4(), uuid4()
    tool = AskImageTool(
        vl_caller=_RecordingVLCaller(answer="never"),
        image_resolver=InMemoryImageResolver({}),
        workspace_image_resolver=NasWorkspaceImageResolver(root=tmp_path),
    )
    ref = f"expert_work://workspace/{theirs}/{user}/.tool_results/r1/figures/a/page-01.jpg"
    with pytest.raises(ToolBlockedError, match="does not match the run tenant"):
        await tool.call({"image_ref": ref, "question": "?"}, ctx=_ctx(tenant_id=mine))


@pytest.mark.anyio
async def test_ask_image_rejects_workspace_ref_when_resolver_not_configured() -> None:
    """``workspace_image_resolver=None``(默认)——这条部署没装 Path B 的 NAS 直读。"""
    tenant, user = uuid4(), uuid4()
    tool = AskImageTool(vl_caller=_RecordingVLCaller(answer="never"), image_resolver=_resolver())
    ref = f"expert_work://workspace/{tenant}/{user}/.tool_results/r1/x.jpg"
    with pytest.raises(ToolBlockedError, match="not available"):
        await tool.call({"image_ref": ref, "question": "?"}, ctx=_ctx(tenant_id=tenant))
