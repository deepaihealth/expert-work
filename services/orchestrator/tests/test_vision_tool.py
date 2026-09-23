"""Tests for the ``ask_image`` tool — Stream J.6 Path B."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage

from expert_work.protocol.multimodal import ImageRef
from orchestrator.multimodal import IMAGE_REF_BLOCK_TYPE, InMemoryImageResolver, ResolvedImage
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


@dataclass
class _AnyRefResolver:
    """任何 ref 都解析成一张图,并记下被解析过哪些 ref。"""

    resolved: list[str] = field(default_factory=list)

    async def resolve(self, ref: str) -> ResolvedImage:
        self.resolved.append(ref)
        return ResolvedImage(media_type="image/jpeg", data=b"JPG")


def _ctx(
    tenant_id: UUID | None = _TENANT, *, user_id: UUID | None = None, agent_key: str = ""
) -> ToolContext:
    return ToolContext(tenant_id=tenant_id, user_id=user_id, agent_key=agent_key)


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
#
# Fix round 1: byte resolution is no longer the tool's job. A single
# DispatchingImageResolver (orchestrator.multimodal) shared by both provider
# adapters and this tool now understands both ref schemes, so the workspace
# branch emits the exact same ``image_ref_block(ref)`` shape as the upload
# branch — the tool only picks which parser to use for the tenant check.
# The "does the resolver actually fetch the right bytes" question moved to
# test_multimodal.py (DispatchingImageResolver's own tests) and to
# test_llm_provider_openai.py / test_llm_provider_anthropic.py (proving a
# real adapter resolves a workspace ref end to end).


@pytest.mark.anyio
async def test_ask_image_accepts_a_workspace_ref() -> None:
    """按 scheme 分派租户校验,并把 ref 原样转发 —— 工作区 ref 与上传 ref 在
    工具这一层之下走同一条投递路径(``image_ref_block``),字节由共享的
    ``DispatchingImageResolver`` 按 scheme 解析,不是本工具的活。"""
    tenant, user = uuid4(), uuid4()
    vl = _FakeVLCaller(response=AIMessage(content="曲线从 6.1 降到 5.5"))
    # Task 7 回修第 2 轮起工具会先解析一次工作区 ref,文件得「在」。
    tool = AskImageTool(vl_caller=vl, image_resolver=_AnyRefResolver())
    ref = f"expert_work://workspace/{tenant}/{user}/.tool_results/r1/figures/abc/page-03.jpg"

    result = await tool.call(
        {"image_ref": ref, "question": "走势如何"},
        ctx=_ctx(tenant_id=tenant, user_id=user),
    )

    assert "5.5" in result.content
    sent = vl.calls[0]["messages"]
    content = sent[1].content
    assert isinstance(content, list)
    assert content[1] == {"type": IMAGE_REF_BLOCK_TYPE, "ref": ref}


@pytest.mark.anyio
async def test_ask_image_rejects_a_cross_tenant_workspace_ref() -> None:
    """租户校验对两种 ref 都执行 —— 新 scheme 不是绕过它的后门。

    ``user`` 在 ref 与 ctx 里保持一致,只让 tenant 不同 —— 否则(``ctx`` 默认
    ``user_id=None``)会先撞上 user 检查,这条测试就验不到它名字里说的那条
    规则了(fix round 2 review 抓到的一次真实误配)。
    """
    mine, theirs, user = uuid4(), uuid4(), uuid4()
    tool = AskImageTool(vl_caller=_FakeVLCaller(), image_resolver=_resolver())
    ref = f"expert_work://workspace/{theirs}/{user}/.tool_results/r1/figures/a/page-01.jpg"
    with pytest.raises(ToolBlockedError, match="does not match the run tenant"):
        await tool.call({"image_ref": ref, "question": "?"}, ctx=_ctx(tenant_id=mine, user_id=user))


@pytest.mark.anyio
async def test_ask_image_rejects_a_same_tenant_different_user_workspace_ref() -> None:
    """Critical finding 2 —— ``{root}/{tenant}/{user}/`` 的 ``user`` 段也是隔离
    边界的一半;只查 tenant 只堵了一半。ref 换一个 user UUID,tenant 不变,不
    需要任何路径穿越或 symlink 就能读到别人的工作区。"""
    tenant, mine, theirs = uuid4(), uuid4(), uuid4()
    tool = AskImageTool(vl_caller=_FakeVLCaller(), image_resolver=_resolver())
    ref = f"expert_work://workspace/{tenant}/{theirs}/.tool_results/r1/figures/a/page-01.jpg"
    with pytest.raises(ToolBlockedError, match="does not match the run user"):
        await tool.call(
            {"image_ref": ref, "question": "?"}, ctx=_ctx(tenant_id=tenant, user_id=mine)
        )


@pytest.mark.anyio
async def test_ask_image_accepts_a_ref_matching_the_run_own_agent_scope() -> None:
    """B-64 回修 C1 —— 绑了 agent 的 run 读自己 agent 渲染出来的页(``agents/<own
    key>/...``)必须放行,不能因为多了这层前缀就误判成跨 agent。"""
    tenant, user = uuid4(), uuid4()
    vl = _FakeVLCaller(response=AIMessage(content="ok"))
    tool = AskImageTool(vl_caller=vl, image_resolver=_AnyRefResolver())
    ref = (
        f"expert_work://workspace/{tenant}/{user}/agents/pf-probe-33086dc0"
        "/.tool_results/r1/figures/abc/page-03.jpg"
    )

    result = await tool.call(
        {"image_ref": ref, "question": "?"},
        ctx=_ctx(tenant_id=tenant, user_id=user, agent_key="pf-probe-33086dc0"),
    )

    assert result.content == "ok"


@pytest.mark.anyio
async def test_ask_image_rejects_a_cross_agent_workspace_ref() -> None:
    """Critical finding (B-64 回修 C1)——同租户同用户下,绑了 agent 的 run 不许
    读**另一个** agent 渲染出来的页(``agents/other-key/...``),否则一个 agent
    能拿别的 agent 渲染出来的页当自己的看。"""
    tenant, user = uuid4(), uuid4()
    tool = AskImageTool(vl_caller=_FakeVLCaller(), image_resolver=_resolver())
    ref = (
        f"expert_work://workspace/{tenant}/{user}/agents/other-agent-11111111"
        "/.tool_results/r1/figures/abc/page-03.jpg"
    )
    with pytest.raises(ToolBlockedError, match="does not match the run agent"):
        await tool.call(
            {"image_ref": ref, "question": "?"},
            ctx=_ctx(tenant_id=tenant, user_id=user, agent_key="my-own-agent-22222222"),
        )


@pytest.mark.anyio
async def test_ask_image_rejects_an_agent_scoped_ref_from_an_unbound_run() -> None:
    """反方向:没绑 agent 的 run(``ctx.agent_key == ""``)不许读绑了 agent 的
    ref —— 空串必须折成 ``None`` 才能与 ``workspace_ref.agent_key`` 比对,
    不能被当成"随便哪个 agent 都行"的通配符。"""
    tenant, user = uuid4(), uuid4()
    tool = AskImageTool(vl_caller=_FakeVLCaller(), image_resolver=_resolver())
    ref = (
        f"expert_work://workspace/{tenant}/{user}/agents/some-agent-33086dc0"
        "/.tool_results/r1/figures/abc/page-03.jpg"
    )
    with pytest.raises(ToolBlockedError, match="does not match the run agent"):
        await tool.call(
            {"image_ref": ref, "question": "?"}, ctx=_ctx(tenant_id=tenant, user_id=user)
        )


# ---------------------------------------------------------------------------
# B-64 Task 7 回修第 2 轮 —— 读不出来的工作区图:显式失败,不交给 VL
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_ask_image_fails_explicitly_on_a_deleted_workspace_image() -> None:
    """文件被留存清理删了:这一次工具调用显式失败,VL 一次都不被调用。

    不预检的话,VL 收到的是适配器的降级文字、回一句「看不到图」,主模型会把它当成
    看图的结果。
    """
    tenant, user = uuid4(), uuid4()
    vl = _FakeVLCaller()
    tool = AskImageTool(vl_caller=vl, image_resolver=InMemoryImageResolver())
    ref = (
        f"expert_work://workspace/{tenant}/{user}/.tool_results/{uuid4()}/figures/"
        f"{'a' * 16}/{'b' * 16}/_u3/page-03.jpg"
    )

    with pytest.raises(FileNotFoundError) as info:
        await tool.call(
            {"image_ref": ref, "question": "走势如何"}, ctx=_ctx(tenant_id=tenant, user_id=user)
        )

    assert "第 3 页" in str(info.value)
    assert "read_page" in str(info.value)
    assert vl.calls == []


@pytest.mark.anyio
async def test_ask_image_does_not_pre_resolve_an_upload_ref() -> None:
    """回归钉:上传 ref 的行为不变 —— 工具这一层不解析它,直接交给 VL caller。"""
    resolver = _AnyRefResolver()
    vl = _FakeVLCaller()
    tool = AskImageTool(vl_caller=vl, image_resolver=resolver)

    result = await tool.call({"image_ref": _ref(), "question": "?"}, ctx=_ctx())

    assert result.content == "ok"
    assert resolver.resolved == []
    assert len(vl.calls) == 1
