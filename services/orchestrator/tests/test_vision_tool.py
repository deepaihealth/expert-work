"""Tests for the ``ask_image`` tool — Stream J.6 Path B."""

from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage

from expert_work.protocol.multimodal import ImageRef
from orchestrator.multimodal import (
    IMAGE_REF_BLOCK_TYPE,
    DispatchingImageResolver,
    InMemoryImageResolver,
    NasWorkspaceImageResolver,
    ResolvedImage,
)
from orchestrator.tools.nas_workspace_store import NasWorkspaceStore
from orchestrator.tools.read_page import document_sha, workspace_figure_ref
from orchestrator.tools.registry import ToolBlockedError, ToolContext
from orchestrator.tools.sandbox import WorkspacePermissionError
from orchestrator.tools.vision import AskImageTool
from orchestrator.tools.workspace_store import RecordingWorkspaceStore, WorkspaceFileEntry

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


def _page_rel(page: int = 3) -> str:
    return f".tool_results/{uuid4()}/figures/{'a' * 16}/{'b' * 16}/_u{page}/page-{page:02d}.jpg"


@pytest.mark.anyio
async def test_ask_image_fails_explicitly_on_a_deleted_workspace_image(tmp_path: Path) -> None:
    """文件被留存清理删了(真 ``ENOENT``):这一次工具调用显式失败,VL 一次都不被调用。

    不预检的话,VL 收到的是适配器的降级文字、回一句「看不到图」,主模型会把它当成
    看图的结果。
    """
    tenant, user = uuid4(), uuid4()
    (tmp_path / str(tenant) / str(user)).mkdir(parents=True)
    vl = _FakeVLCaller()
    tool = AskImageTool(vl_caller=vl, image_resolver=NasWorkspaceImageResolver(root=tmp_path))
    ref = f"expert_work://workspace/{tenant}/{user}/{_page_rel()}"

    with pytest.raises(FileNotFoundError) as info:
        await tool.call(
            {"image_ref": ref, "question": "走势如何"}, ctx=_ctx(tenant_id=tenant, user_id=user)
        )

    assert "第 3 页" in str(info.value)
    assert "已不存在" in str(info.value)
    assert "重新调用 read_page" in str(info.value)
    assert vl.calls == []


@pytest.mark.anyio
async def test_ask_image_calls_an_unreachable_image_unreadable_not_missing(tmp_path: Path) -> None:
    """回修第 3 轮 —— 「够不着」不是「不存在」:符号链接越界是**安全拒绝**,重渲也读
    不到。不许说「已被清理」、不许引导模型去重渲,也不许报成 FileNotFoundError(那会让
    错误分类给出「目标不存在」的建议)。"""
    tenant, user = uuid4(), uuid4()
    rel = _page_rel()
    leaf = tmp_path / str(tenant) / str(user) / rel
    leaf.parent.mkdir(parents=True)
    outside = tmp_path / "outside.jpg"
    outside.write_bytes(b"\xff\xd8\xff\xe0")
    leaf.symlink_to(outside)
    vl = _FakeVLCaller()
    tool = AskImageTool(vl_caller=vl, image_resolver=NasWorkspaceImageResolver(root=tmp_path))
    ref = f"expert_work://workspace/{tenant}/{user}/{rel}"

    with pytest.raises(RuntimeError) as info:
        await tool.call(
            {"image_ref": ref, "question": "?"}, ctx=_ctx(tenant_id=tenant, user_id=user)
        )

    assert not isinstance(info.value, FileNotFoundError)
    assert "读不了" in str(info.value)
    assert "已被清理" not in str(info.value)
    assert "需要的话重新调用 read_page" not in str(info.value)
    assert vl.calls == []


@pytest.mark.anyio
async def test_ask_image_on_a_deployment_without_workspace_says_unreadable() -> None:
    """部署没接 NAS:引导模型「重新 read_page」只会再失败、空转到步数上限。"""
    tenant, user = uuid4(), uuid4()
    vl = _FakeVLCaller()
    resolver = DispatchingImageResolver(uploads=InMemoryImageResolver(), workspace=None)
    tool = AskImageTool(vl_caller=vl, image_resolver=resolver)
    ref = f"expert_work://workspace/{tenant}/{user}/{_page_rel()}"

    with pytest.raises(RuntimeError) as info:
        await tool.call(
            {"image_ref": ref, "question": "?"}, ctx=_ctx(tenant_id=tenant, user_id=user)
        )

    assert "读不了" in str(info.value)
    assert "已被清理" not in str(info.value)
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


# ---------------------------------------------------------------------------
# B-64 Task 8 —— 短形态 path + unit:模型不再手抄约 200 字符的 ref
# ---------------------------------------------------------------------------

_AGENT = "pf-probe-33086dc0"
_DOC = "uploads/report.pdf"
_OLD_RUN = "ffffffff-ffff-4fff-bfff-ffffffffffff"
_NEW_RUN = "00000000-0000-4000-8000-000000000000"


def _figure_rel(
    *, run: str = _NEW_RUN, render: str = "b" * 16, unit: int = 10, agent_key: str = _AGENT
) -> str:
    """一张 ``read_page`` 渲染页的**作用域相对**落点(形状与片段产出一致)。"""
    doc_sha = document_sha(_DOC, agent_key=agent_key)
    assert doc_sha is not None
    return f".tool_results/{run}/figures/{doc_sha}/{render}/_u{unit}/page-{unit:02d}.jpg"


def _put(root: Path, tenant: UUID, user: UUID, rel: str, *, mtime: float) -> None:
    target = root / str(tenant) / str(user) / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"\xff\xd8\xff\xe0")
    os.utime(target, (mtime, mtime))


def _short_form_tool(root: Path, vl: _FakeVLCaller) -> AskImageTool:
    return AskImageTool(
        vl_caller=vl,
        image_resolver=NasWorkspaceImageResolver(root=root),
        workspace_store=NasWorkspaceStore(root=str(root)),
    )


def _sent_ref(vl: _FakeVLCaller) -> str:
    content = vl.calls[0]["messages"][1].content
    assert isinstance(content, list)
    block = content[1]
    assert block["type"] == IMAGE_REF_BLOCK_TYPE
    ref = block["ref"]
    assert isinstance(ref, str)
    return ref


@pytest.mark.anyio
async def test_ask_image_short_form_resolves_the_rendered_page(tmp_path: Path) -> None:
    """path + unit → 本 agent 作用域里那张 ``_u10`` 渲染页;VL 收到的 ref 与
    ``workspace_figure_ref`` 对它拼出来的逐字相同,meta 记下解析来源。"""
    tenant, user = uuid4(), uuid4()
    _put(tmp_path, tenant, user, f"agents/{_AGENT}/{_DOC}", mtime=1_000)
    rel = _figure_rel()
    _put(tmp_path, tenant, user, f"agents/{_AGENT}/{rel}", mtime=2_000)
    vl = _FakeVLCaller(response=AIMessage(content="血糖曲线"))

    result = await _short_form_tool(tmp_path, vl).call(
        {"path": _DOC, "unit": 10, "question": "图里是什么"},
        ctx=_ctx(tenant_id=tenant, user_id=user, agent_key=_AGENT),
    )

    expected = workspace_figure_ref(tenant, user, f"agents/{_AGENT}/{rel}")
    assert _sent_ref(vl) == expected
    assert result.content == "血糖曲线"
    assert result.meta["image_ref"] == expected
    assert result.meta["resolved_from"] == {"path": _DOC, "unit": 10}


@pytest.mark.anyio
async def test_ask_image_short_form_works_on_the_in_memory_store() -> None:
    """同一套走法在内存替身上答案一样 —— 它对不存在的目录回空列表,NAS 抛异常,
    解析只往确认过的目录里走,所以两边同义。"""
    tenant, user = uuid4(), uuid4()
    rel = _figure_rel()
    stamp = datetime(2026, 9, 23, tzinfo=UTC)
    store = RecordingWorkspaceStore(
        workspace_files=[
            WorkspaceFileEntry(path=f"agents/{_AGENT}/{_DOC}", size=1, mtime=stamp),
            WorkspaceFileEntry(path=f"agents/{_AGENT}/{rel}", size=1, mtime=stamp),
        ]
    )
    vl = _FakeVLCaller()
    tool = AskImageTool(vl_caller=vl, image_resolver=_AnyRefResolver(), workspace_store=store)

    await tool.call(
        {"path": _DOC, "unit": 10, "question": "?"},
        ctx=_ctx(tenant_id=tenant, user_id=user, agent_key=_AGENT),
    )

    assert _sent_ref(vl) == workspace_figure_ref(tenant, user, f"agents/{_AGENT}/{rel}")


@pytest.mark.anyio
async def test_ask_image_short_form_picks_the_newest_render(tmp_path: Path) -> None:
    """多次渲染取修改时间最新的那张 —— 目录名排序在前的那个 run 反而是旧的,
    按名字挑就会挑错。"""
    tenant, user = uuid4(), uuid4()
    _put(tmp_path, tenant, user, f"agents/{_AGENT}/{_DOC}", mtime=1_000)
    old = _figure_rel(run=_NEW_RUN, render="1" * 16)
    new = _figure_rel(run=_OLD_RUN, render="2" * 16)
    _put(tmp_path, tenant, user, f"agents/{_AGENT}/{old}", mtime=2_000)
    _put(tmp_path, tenant, user, f"agents/{_AGENT}/{new}", mtime=3_000)
    vl = _FakeVLCaller()

    await _short_form_tool(tmp_path, vl).call(
        {"path": _DOC, "unit": 10, "question": "?"},
        ctx=_ctx(tenant_id=tenant, user_id=user, agent_key=_AGENT),
    )

    assert _sent_ref(vl) == workspace_figure_ref(tenant, user, f"agents/{_AGENT}/{new}")


@pytest.mark.anyio
async def test_ask_image_short_form_refuses_a_render_older_than_the_document(
    tmp_path: Path,
) -> None:
    """新鲜度闸:渲完之后文档又改过 —— 最新那张渲染也是旧内容,显式失败,VL 不被调用。"""
    tenant, user = uuid4(), uuid4()
    _put(tmp_path, tenant, user, f"agents/{_AGENT}/{_figure_rel()}", mtime=2_000)
    _put(tmp_path, tenant, user, f"agents/{_AGENT}/{_DOC}", mtime=3_000)
    vl = _FakeVLCaller()

    with pytest.raises(FileNotFoundError) as info:
        await _short_form_tool(tmp_path, vl).call(
            {"path": _DOC, "unit": 10, "question": "?"},
            ctx=_ctx(tenant_id=tenant, user_id=user, agent_key=_AGENT),
        )

    assert "改过" in str(info.value)
    assert f"read_page(path={_DOC!r}, units=[10])" in str(info.value)
    assert vl.calls == []


@pytest.mark.anyio
async def test_ask_image_short_form_without_a_render_says_call_read_page(tmp_path: Path) -> None:
    tenant, user = uuid4(), uuid4()
    _put(tmp_path, tenant, user, f"agents/{_AGENT}/{_DOC}", mtime=1_000)
    # 同一份文档渲过别的页 —— 不能拿来顶第 10 页。
    _put(tmp_path, tenant, user, f"agents/{_AGENT}/{_figure_rel(unit=3)}", mtime=2_000)
    vl = _FakeVLCaller()

    with pytest.raises(FileNotFoundError) as info:
        await _short_form_tool(tmp_path, vl).call(
            {"path": _DOC, "unit": 10, "question": "?"},
            ctx=_ctx(tenant_id=tenant, user_id=user, agent_key=_AGENT),
        )

    assert "还没渲染" in str(info.value)
    assert f"read_page(path={_DOC!r}, units=[10])" in str(info.value)
    assert vl.calls == []


@pytest.mark.anyio
async def test_ask_image_short_form_only_searches_the_run_own_scope(tmp_path: Path) -> None:
    """短形态绕不过身份边界:``<doc-sha>`` 只由路径决定,别的 agent、没绑 agent 的用户
    根、同 agent 的另一个 user 下都能有同一个 ``<doc-sha>`` 的渲染页 —— 一张都不许命中。"""
    tenant, user, other_user = uuid4(), uuid4(), uuid4()
    other_agent = "someone-else-11111111"
    _put(tmp_path, tenant, user, f"agents/{_AGENT}/{_DOC}", mtime=1_000)
    _put(tmp_path, tenant, user, f"agents/{other_agent}/{_figure_rel()}", mtime=2_000)
    _put(tmp_path, tenant, user, _figure_rel(), mtime=2_000)
    _put(tmp_path, tenant, other_user, f"agents/{_AGENT}/{_DOC}", mtime=1_000)
    _put(tmp_path, tenant, other_user, f"agents/{_AGENT}/{_figure_rel()}", mtime=2_000)
    vl = _FakeVLCaller()

    with pytest.raises(FileNotFoundError, match="还没渲染"):
        await _short_form_tool(tmp_path, vl).call(
            {"path": _DOC, "unit": 10, "question": "?"},
            ctx=_ctx(tenant_id=tenant, user_id=user, agent_key=_AGENT),
        )

    assert vl.calls == []


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("args", "needle"),
    [
        ({"image_ref": "expert_work://image/x", "path": _DOC, "unit": 10}, "not both"),
        ({}, "needs either"),
        ({"path": _DOC}, "go together"),
        ({"unit": 10}, "go together"),
    ],
    ids=["both", "neither", "path-only", "unit-only"],
)
async def test_ask_image_rejects_a_bad_argument_combination(
    tmp_path: Path, args: dict[str, Any], needle: str
) -> None:
    vl = _FakeVLCaller()
    with pytest.raises(ValueError, match=needle):
        await _short_form_tool(tmp_path, vl).call(
            {**args, "question": "?"}, ctx=_ctx(user_id=uuid4(), agent_key=_AGENT)
        )
    assert vl.calls == []


@pytest.mark.anyio
async def test_ask_image_without_a_workspace_store_has_no_short_form() -> None:
    """没接工作区存储:短形态不存在 —— 调用被拒,描述与参数里也不许提它。"""
    tool = AskImageTool(vl_caller=_FakeVLCaller(), image_resolver=_resolver())
    spec = tool.spec
    assert set(spec.parameters["properties"]) == {"image_ref", "question"}
    for word in ("path", "unit", "read_page"):
        assert word not in spec.description
        assert word not in str(spec.parameters)

    with pytest.raises(ValueError, match="only accepts 'image_ref'"):
        await tool.call({"path": _DOC, "unit": 10, "question": "?"}, ctx=_ctx(user_id=uuid4()))


def test_ask_image_with_a_workspace_store_offers_both_forms() -> None:
    spec = AskImageTool(
        vl_caller=_FakeVLCaller(),
        image_resolver=_resolver(),
        workspace_store=RecordingWorkspaceStore(),
    ).spec
    assert {"path", "unit", "image_ref", "question"} <= set(spec.parameters["properties"])
    assert spec.parameters["required"] == ["question"]
    assert "Prefer this for document pages" in spec.description


@pytest.mark.anyio
async def test_ask_image_short_form_in_an_agent_workspace_that_does_not_exist_yet(
    tmp_path: Path,
) -> None:
    """一个还没写过任何文件的 agent:NAS 列作用域根就是「不存在」。要落成与「还没渲染」
    同一类的失败,并指出下一步,而不是一句 ``workspace path not found: '.'``。"""
    tenant, user = uuid4(), uuid4()
    (tmp_path / str(tenant) / str(user)).mkdir(parents=True)
    vl = _FakeVLCaller()

    with pytest.raises(FileNotFoundError) as info:
        await _short_form_tool(tmp_path, vl).call(
            {"path": _DOC, "unit": 10, "question": "?"},
            ctx=_ctx(tenant_id=tenant, user_id=user, agent_key=_AGENT),
        )

    assert f"read_page(path={_DOC!r}, units=[10])" in str(info.value)
    assert vl.calls == []


@pytest.mark.anyio
async def test_ask_image_short_form_does_not_hide_a_permission_error() -> None:
    """只把「不存在」当空;读不动不是不存在,原样抛。"""
    store = RecordingWorkspaceStore(workspace_list_error=WorkspacePermissionError("nope"))
    vl = _FakeVLCaller()
    tool = AskImageTool(vl_caller=vl, image_resolver=_AnyRefResolver(), workspace_store=store)

    with pytest.raises(WorkspacePermissionError):
        await tool.call(
            {"path": _DOC, "unit": 10, "question": "?"},
            ctx=_ctx(tenant_id=uuid4(), user_id=uuid4(), agent_key=_AGENT),
        )

    assert vl.calls == []


@pytest.mark.anyio
async def test_ask_image_short_form_finds_this_run_page_past_the_listing_cap() -> None:
    """``.tool_results`` 的列表按名字截到 2000 条;本 run 的目录名排在最后、被截掉时,
    它刚渲出来的页仍要找得到。"""
    tenant, user = uuid4(), uuid4()
    stamp = datetime(2026, 9, 23, tzinfo=UTC)
    rel = _figure_rel(run=_OLD_RUN)  # 全 f,名字排在最后
    decoys = [
        WorkspaceFileEntry(
            path=f"agents/{_AGENT}/.tool_results/00000000-0000-4000-8000-{i:012d}/x.json",
            size=1,
            mtime=stamp,
        )
        for i in range(2000)
    ]
    store = RecordingWorkspaceStore(
        workspace_files=[
            WorkspaceFileEntry(path=f"agents/{_AGENT}/{_DOC}", size=1, mtime=stamp),
            WorkspaceFileEntry(path=f"agents/{_AGENT}/{rel}", size=1, mtime=stamp),
            *decoys,
        ]
    )
    listing = await store.list_dir(
        tenant_id=tenant, user_id=user, scope=f"agent:{_AGENT}", path=".tool_results"
    )
    assert listing.truncated
    assert _OLD_RUN not in {entry.name for entry in listing.entries}, "没截到本 run,验不到"
    vl = _FakeVLCaller()
    tool = AskImageTool(vl_caller=vl, image_resolver=_AnyRefResolver(), workspace_store=store)

    await tool.call(
        {"path": _DOC, "unit": 10, "question": "?"},
        ctx=ToolContext(tenant_id=tenant, user_id=user, agent_key=_AGENT, run_id=UUID(_OLD_RUN)),
    )

    assert _sent_ref(vl) == workspace_figure_ref(tenant, user, f"agents/{_AGENT}/{rel}")
