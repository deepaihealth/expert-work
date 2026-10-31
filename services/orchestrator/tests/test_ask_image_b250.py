"""B-250 / B-251 / B-252 —— ``ask_image`` 少绕路、少一轮。

10-31 测试环境一次报告解读跑了 47 分钟(对话 099d9b9b):模型裁出来的 png 看不了,只好拼
成 PDF → ``read_page`` → ``ask_image``;按页看时那一页还没渲染,报一次错再去补;``deep``
超时三次,每次白等 120 秒再多一轮模型思考。
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from langchain_core.messages import AIMessage, BaseMessage

from orchestrator.multimodal import IMAGE_REF_BLOCK_TYPE, NasWorkspaceImageResolver
from orchestrator.tools.error_classifier import GuidedTimeoutError
from orchestrator.tools.nas_workspace_store import NasWorkspaceStore
from orchestrator.tools.read_page import document_sha, workspace_figure_ref
from orchestrator.tools.registry import ToolContext, ToolResult
from orchestrator.tools.vision import AskImageTool

_TENANT = UUID("11111111-1111-1111-1111-111111111111")
_USER = UUID("33333333-3333-3333-3333-333333333333")
_CONV = UUID("44444444-4444-4444-4444-444444444444")
_AGENT = "report-aaaaaaaa"
_DOC = "uploads/report.pdf"
_RUN = UUID("00000000-0000-4000-8000-000000000000")


@dataclass
class _VL:
    answer: str = "ok"
    hang: bool = False
    calls: list[list[BaseMessage]] = field(default_factory=list)

    async def __call__(self, *, messages: Sequence[BaseMessage], tools: Sequence[Any]) -> AIMessage:
        self.calls.append(list(messages))
        if self.hang:
            await asyncio.sleep(3600)
        return AIMessage(content=self.answer)


def _ctx(*, conversation: bool = False) -> ToolContext:
    extra: dict[str, Any] = {}
    if conversation:
        extra = {"thread_id": _CONV, "workspace_layout": "conversation"}
    return ToolContext(tenant_id=_TENANT, user_id=_USER, agent_key=_AGENT, run_id=_RUN, **extra)


def _put(root: Path, rel: str, *, mtime: float = 1_000.0, data: bytes = b"\x89PNG") -> None:
    target = root / str(_TENANT) / str(_USER) / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    os.utime(target, (mtime, mtime))


def _sent_ref(vl: _VL) -> str:
    content = vl.calls[-1][1].content
    assert isinstance(content, list)
    block = content[1]
    assert block["type"] == IMAGE_REF_BLOCK_TYPE
    return str(block["ref"])


def _tool(root: Path, vl: _VL, **kwargs: Any) -> AskImageTool:
    return AskImageTool(
        vl_caller=vl,
        image_resolver=NasWorkspaceImageResolver(root=root),
        workspace_store=NasWorkspaceStore(root=str(root)),
        **kwargs,
    )


# ---------------------------------------------------------------------------
# B-250 —— 工作区里的图片文件直接看
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_a_workspace_png_is_looked_at_directly(tmp_path: Path) -> None:
    _put(tmp_path, f"agents/{_AGENT}/crops/a.png")
    vl = _VL(answer="blood sugar 9.1")

    result = await _tool(tmp_path, vl).call({"path": "crops/a.png", "question": "?"}, ctx=_ctx())

    assert result.content == "blood sugar 9.1"
    assert _sent_ref(vl) == workspace_figure_ref(_TENANT, _USER, f"agents/{_AGENT}/crops/a.png")
    assert result.meta["resolved_from"] == {"path": "crops/a.png"}


@pytest.mark.anyio
async def test_a_conversation_view_path_lands_in_its_own_work_dir(tmp_path: Path) -> None:
    rel = f"agents/{_AGENT}/threads/{_CONV}/work/crops/a.jpg"
    _put(tmp_path, rel)
    vl = _VL()

    await _tool(tmp_path, vl).call(
        {"path": "/workspace/crops/a.jpg", "question": "?"}, ctx=_ctx(conversation=True)
    )

    assert _sent_ref(vl) == workspace_figure_ref(_TENANT, _USER, rel)


@pytest.mark.anyio
async def test_a_workspace_path_given_as_image_ref_is_taken_as_a_path(tmp_path: Path) -> None:
    """10-31 那次:模型把 ``work/x.png`` 填进 ``image_ref``,报一次 ValueError 才改对。"""
    _put(tmp_path, f"agents/{_AGENT}/work/x.png")
    vl = _VL()

    result = await _tool(tmp_path, vl).call(
        {"image_ref": "work/x.png", "question": "?"}, ctx=_ctx()
    )

    assert _sent_ref(vl) == workspace_figure_ref(_TENANT, _USER, f"agents/{_AGENT}/work/x.png")
    assert result.meta["resolved_from"] == {"path": "work/x.png"}


@pytest.mark.anyio
async def test_a_missing_workspace_image_fails_without_calling_the_vl_model(
    tmp_path: Path,
) -> None:
    vl = _VL()
    with pytest.raises(FileNotFoundError):
        await _tool(tmp_path, vl).call({"path": "crops/none.png", "question": "?"}, ctx=_ctx())
    assert vl.calls == []


@pytest.mark.anyio
@pytest.mark.parametrize("path", ["../other/a.png", "/etc/a.png", "shared/a.png"])
async def test_a_path_outside_the_workspace_is_refused(tmp_path: Path, path: str) -> None:
    vl = _VL()
    with pytest.raises((ValueError, PermissionError)):
        await _tool(tmp_path, vl).call({"path": path, "question": "?"}, ctx=_ctx())
    assert vl.calls == []


@pytest.mark.anyio
async def test_a_document_path_without_unit_still_needs_a_unit(tmp_path: Path) -> None:
    vl = _VL()
    with pytest.raises(ValueError, match="unit"):
        await _tool(tmp_path, vl).call({"path": _DOC, "question": "?"}, ctx=_ctx())
    assert vl.calls == []


# ---------------------------------------------------------------------------
# B-251 —— 按页看,没渲染就先渲染
# ---------------------------------------------------------------------------


def _render_rel(unit: int) -> str:
    doc_sha = document_sha(_DOC, view=_ctx().workspace_view)
    assert doc_sha is not None
    return (
        f"agents/{_AGENT}/.tool_results/{_RUN}/figures/{doc_sha}/{'c' * 16}/_u{unit}/"
        f"page-{unit:02d}.jpg"
    )


@dataclass
class _Renderer:
    """替身 ``read_page``:按调用落一张渲染页(或什么都不落),记下参数。"""

    root: Path
    writes: bool = True
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def call(self, args: Mapping[str, Any], *, ctx: ToolContext) -> ToolResult:
        self.calls.append(dict(args))
        if not self.writes:
            return ToolResult(content="无法渲染 uploads/report.pdf:第 4 页超出文档页数。")
        for unit in args["units"]:
            _put(self.root, _render_rel(unit), mtime=3_000.0, data=b"\xff\xd8\xff\xe0")
        return ToolResult(content="已渲染", state_updates={"figure_documents": {"sha": _DOC}})


@pytest.mark.anyio
async def test_an_unrendered_page_is_rendered_then_looked_at(tmp_path: Path) -> None:
    _put(tmp_path, f"agents/{_AGENT}/{_DOC}", mtime=2_000.0)
    renderer = _Renderer(tmp_path)
    vl = _VL()

    result = await _tool(tmp_path, vl, page_renderer=renderer).call(
        {"path": _DOC, "unit": 4, "question": "?"}, ctx=_ctx()
    )

    assert renderer.calls == [{"path": _DOC, "units": [4]}]
    assert _sent_ref(vl) == workspace_figure_ref(_TENANT, _USER, _render_rel(4))
    # read_page 自己调时会写进 state 的东西,代它调也照样带回去。
    assert result.state_updates == {"figure_documents": {"sha": _DOC}}


@pytest.mark.anyio
async def test_a_stale_render_is_rendered_again(tmp_path: Path) -> None:
    _put(tmp_path, _render_rel(4), mtime=1_000.0)
    _put(tmp_path, f"agents/{_AGENT}/{_DOC}", mtime=2_000.0)  # 文档比渲染新
    renderer = _Renderer(tmp_path)
    vl = _VL()

    await _tool(tmp_path, vl, page_renderer=renderer).call(
        {"path": _DOC, "unit": 4, "question": "?"}, ctx=_ctx()
    )

    assert len(renderer.calls) == 1
    assert vl.calls


@pytest.mark.anyio
async def test_a_fresh_render_is_not_rendered_again(tmp_path: Path) -> None:
    _put(tmp_path, f"agents/{_AGENT}/{_DOC}", mtime=1_000.0)
    _put(tmp_path, _render_rel(4), mtime=2_000.0)
    renderer = _Renderer(tmp_path)

    await _tool(tmp_path, _VL(), page_renderer=renderer).call(
        {"path": _DOC, "unit": 4, "question": "?"}, ctx=_ctx()
    )

    assert renderer.calls == []


@pytest.mark.anyio
async def test_a_missing_document_is_not_rendered(tmp_path: Path) -> None:
    renderer = _Renderer(tmp_path)
    vl = _VL()
    with pytest.raises(FileNotFoundError, match="找不到文档"):
        await _tool(tmp_path, vl, page_renderer=renderer).call(
            {"path": _DOC, "unit": 4, "question": "?"}, ctx=_ctx()
        )
    assert renderer.calls == []
    assert vl.calls == []


@pytest.mark.anyio
async def test_a_failed_render_reports_why(tmp_path: Path) -> None:
    _put(tmp_path, f"agents/{_AGENT}/{_DOC}", mtime=2_000.0)
    renderer = _Renderer(tmp_path, writes=False)
    vl = _VL()
    with pytest.raises(FileNotFoundError, match="超出文档页数"):
        await _tool(tmp_path, vl, page_renderer=renderer).call(
            {"path": _DOC, "unit": 4, "question": "?"}, ctx=_ctx()
        )
    assert len(renderer.calls) == 1
    assert vl.calls == []


@pytest.mark.anyio
async def test_without_read_page_the_old_advice_stands(tmp_path: Path) -> None:
    _put(tmp_path, f"agents/{_AGENT}/{_DOC}", mtime=2_000.0)
    with pytest.raises(FileNotFoundError, match=r"read_page\(path="):
        await _tool(tmp_path, _VL()).call({"path": _DOC, "unit": 4, "question": "?"}, ctx=_ctx())


# ---------------------------------------------------------------------------
# B-252 —— deep 超时自动换 quick 再看一次
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_a_deep_timeout_retries_once_with_the_quick_look(tmp_path: Path) -> None:
    _put(tmp_path, f"agents/{_AGENT}/a.png")
    deep, quick = _VL(hang=True), _VL(answer="quick answer")
    tool = _tool(tmp_path, deep, quick_vl_caller=quick, timeout_s=0.05)

    result = await tool.call({"path": "a.png", "question": "?", "depth": "deep"}, ctx=_ctx())

    assert len(deep.calls) == 1 and len(quick.calls) == 1
    assert "quick answer" in str(result.content)
    assert "quick" in str(result.content).split("quick answer")[0]  # 先说明这是快速看的回答
    assert result.meta["depth"] == "quick"
    assert result.meta["deep_timed_out"] is True


@pytest.mark.anyio
async def test_a_quick_retry_that_also_times_out_is_reported(tmp_path: Path) -> None:
    _put(tmp_path, f"agents/{_AGENT}/a.png")
    deep, quick = _VL(hang=True), _VL(hang=True)
    tool = _tool(tmp_path, deep, quick_vl_caller=quick, timeout_s=0.05)

    with pytest.raises(GuidedTimeoutError):
        await tool.call({"path": "a.png", "question": "?", "depth": "deep"}, ctx=_ctx())
    assert len(quick.calls) == 1


@pytest.mark.anyio
async def test_a_quick_timeout_is_not_retried(tmp_path: Path) -> None:
    _put(tmp_path, f"agents/{_AGENT}/a.png")
    vl = _VL(hang=True)
    tool = _tool(tmp_path, vl, timeout_s=0.05)

    with pytest.raises(GuidedTimeoutError):
        await tool.call({"path": "a.png", "question": "?"}, ctx=_ctx())
    assert len(vl.calls) == 1


@pytest.mark.anyio
async def test_no_quick_retry_when_the_run_has_no_time_left(tmp_path: Path) -> None:
    _put(tmp_path, f"agents/{_AGENT}/a.png")
    deep, quick = _VL(hang=True), _VL()
    tool = _tool(tmp_path, deep, quick_vl_caller=quick, timeout_s=30)
    ctx = ToolContext(
        tenant_id=_TENANT,
        user_id=_USER,
        agent_key=_AGENT,
        run_id=_RUN,
        deadline_at=time.monotonic() + 0.05,
    )

    with pytest.raises(GuidedTimeoutError):
        await tool.call({"path": "a.png", "question": "?", "depth": "deep"}, ctx=ctx)
    assert quick.calls == []


# ---------------------------------------------------------------------------
# 审查回修
# ---------------------------------------------------------------------------


@dataclass
class _SlowRenderer(_Renderer):
    """渲染要花一点时间:两个并发的看图请求会同时撞上「没渲染」。"""

    async def call(self, args: Mapping[str, Any], *, ctx: ToolContext) -> ToolResult:
        await asyncio.sleep(0.05)
        return await super().call(args, ctx=ctx)


@pytest.mark.anyio
async def test_parallel_looks_at_one_unrendered_page_render_it_once(tmp_path: Path) -> None:
    """``ask_image`` 是只读工具,调度器会并行跑两次;两次都去渲,会在同一个渲染目录里
    互删(read_page 自己靠「非只读 + 同路径」串行,代调绕开了它)。同一 run 同一文档排队,
    排到时先看别人是不是已经渲好了。"""
    _put(tmp_path, f"agents/{_AGENT}/{_DOC}", mtime=2_000.0)
    renderer = _SlowRenderer(tmp_path)
    tool = _tool(tmp_path, _VL(), page_renderer=renderer)

    await asyncio.gather(
        tool.call({"path": _DOC, "unit": 4, "question": "a"}, ctx=_ctx()),
        tool.call({"path": _DOC, "unit": 4, "question": "b"}, ctx=_ctx()),
    )

    assert len(renderer.calls) == 1


@pytest.mark.anyio
async def test_a_raw_ref_into_another_conversation_is_refused(tmp_path: Path) -> None:
    """B-236 —— 别的对话的 ``threads/<id>/`` 永远看不到;手写的工作区 ref 也一样。"""
    from orchestrator.tools.registry import ToolBlockedError

    other = UUID("55555555-5555-5555-5555-555555555555")
    rel = f"agents/{_AGENT}/threads/{other}/work/x.png"
    _put(tmp_path, rel)
    vl = _VL()

    with pytest.raises(ToolBlockedError, match="another conversation"):
        await _tool(tmp_path, vl).call(
            {"image_ref": workspace_figure_ref(_TENANT, _USER, rel), "question": "?"},
            ctx=_ctx(conversation=True),
        )
    assert vl.calls == []

    own = f"agents/{_AGENT}/threads/{_CONV}/work/x.png"
    _put(tmp_path, own)
    await _tool(tmp_path, vl).call(
        {"image_ref": workspace_figure_ref(_TENANT, _USER, own), "question": "?"},
        ctx=_ctx(conversation=True),
    )
    assert len(vl.calls) == 1


@pytest.mark.anyio
async def test_the_quick_retry_never_falls_back_to_the_deep_route(tmp_path: Path) -> None:
    """快看被厂商拒时平时会退回正常看图 —— 但 deep 刚超时,退回去就是原样再等一整个窗口。"""
    from expert_work.runtime.middleware.llm_error_handling import LLMClientError

    @dataclass
    class _Rejects:
        calls: int = 0

        async def __call__(
            self, *, messages: Sequence[BaseMessage], tools: Sequence[Any]
        ) -> AIMessage:
            self.calls += 1
            raise LLMClientError("400 invalid parameter: thinking")

    _put(tmp_path, f"agents/{_AGENT}/a.png")
    deep, quick = _VL(hang=True), _Rejects()
    tool = _tool(tmp_path, deep, quick_vl_caller=quick, timeout_s=0.05)

    with pytest.raises(GuidedTimeoutError):
        await tool.call({"path": "a.png", "question": "?", "depth": "deep"}, ctx=_ctx())
    assert len(deep.calls) == 1
    assert quick.calls == 1


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("crops/a.png", True), ("A.JPEG", True), ("png", False), ("dir/jpg", False), ("x.pdf", False)],
)
def test_only_names_with_an_image_extension_count_as_images(raw: str, expected: bool) -> None:
    from orchestrator.tools.vision import _is_image_path

    assert _is_image_path(raw) is expected
