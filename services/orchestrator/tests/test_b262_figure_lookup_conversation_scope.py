"""B-262 —— ``ask_image(path, unit)`` 只用本对话渲染过的页。

渲染页落在 agent 级的 ``.tool_results/<run>/figures/<doc-sha>/…``,同一智能体的所有对话
共用这一层;``<doc-sha>`` 只认路径,两个对话里同名的上传算出来是同一个。原来短形态扫
``.tool_results`` 下**全部** run,于是对话 A 会拿到对话 B 在同名文档上渲出来的页。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from langchain_core.messages import AIMessage

from orchestrator.graph_builder.builder import _build_tool_context
from orchestrator.tools._child_run import _child_config
from orchestrator.tools.figure_lookup import figure_run_ids
from orchestrator.tools.read_page import ReadPageTool, workspace_figure_ref
from orchestrator.tools.registry import ToolContext, ToolRegistry, ToolResult, ToolSpec

from .test_figure_block_injection import _graph, _invoke, _RecordingLLM, _ref, _tool_turn
from .test_read_page import _install_office_stubs
from .test_vision_tool import (
    _AGENT,
    _DOC,
    _FakeVLCaller,
    _figure_rel,
    _LocalRenderRuntime,
    _put,
    _sent_ref,
    _short_form_tool,
)

_THIS_CONV_EARLIER = "11111111-1111-4111-8111-111111111111"
_OTHER_CONV = "22222222-2222-4222-8222-222222222222"


def _ctx(tenant: UUID, user: UUID, *, runs: frozenset[UUID]) -> ToolContext:
    return ToolContext(
        tenant_id=tenant, user_id=user, agent_key=_AGENT, run_id=uuid4(), figure_run_ids=runs
    )


@pytest.mark.anyio
async def test_another_conversations_render_of_a_same_named_upload_is_not_used(
    tmp_path: Path,
) -> None:
    """另一个对话渲过同名文档的这一页、本对话没渲过 → 说「还没渲染」,不拿别人的。"""
    tenant, user = uuid4(), uuid4()
    _put(tmp_path, tenant, user, f"agents/{_AGENT}/{_DOC}", mtime=1_000)
    _put(tmp_path, tenant, user, f"agents/{_AGENT}/{_figure_rel(run=_OTHER_CONV)}", mtime=2_000)
    vl = _FakeVLCaller()

    with pytest.raises(FileNotFoundError, match="还没渲染"):
        await _short_form_tool(tmp_path, vl).call(
            {"path": _DOC, "unit": 10, "question": "?"},
            ctx=_ctx(tenant, user, runs=frozenset({UUID(_THIS_CONV_EARLIER)})),
        )

    assert vl.calls == []


@pytest.mark.anyio
async def test_own_render_from_an_earlier_run_wins_over_a_newer_one_from_another_conversation(
    tmp_path: Path,
) -> None:
    """本对话上一轮渲过 → 用它;另一个对话的那张更新也不用(原来按修改时间取最新,会挑中它)。"""
    tenant, user = uuid4(), uuid4()
    own = _figure_rel(run=_THIS_CONV_EARLIER, render="1" * 16)
    other = _figure_rel(run=_OTHER_CONV, render="2" * 16)
    _put(tmp_path, tenant, user, f"agents/{_AGENT}/{_DOC}", mtime=1_000)
    _put(tmp_path, tenant, user, f"agents/{_AGENT}/{own}", mtime=2_000)
    _put(tmp_path, tenant, user, f"agents/{_AGENT}/{other}", mtime=3_000)
    vl = _FakeVLCaller(response=AIMessage(content="ok"))

    await _short_form_tool(tmp_path, vl).call(
        {"path": _DOC, "unit": 10, "question": "?"},
        ctx=_ctx(tenant, user, runs=frozenset({UUID(_THIS_CONV_EARLIER)})),
    )

    assert _sent_ref(vl) == workspace_figure_ref(tenant, user, f"agents/{_AGENT}/{own}")


@pytest.mark.anyio
async def test_a_conversation_run_whose_directory_is_gone_counts_as_empty(tmp_path: Path) -> None:
    """检查点里记着的 run,目录已被留存清理删掉 —— 当空,不报错。"""
    tenant, user = uuid4(), uuid4()
    own = _figure_rel(run=_THIS_CONV_EARLIER)
    _put(tmp_path, tenant, user, f"agents/{_AGENT}/{_DOC}", mtime=1_000)
    _put(tmp_path, tenant, user, f"agents/{_AGENT}/{own}", mtime=2_000)
    vl = _FakeVLCaller()

    await _short_form_tool(tmp_path, vl).call(
        {"path": _DOC, "unit": 10, "question": "?"},
        ctx=_ctx(tenant, user, runs=frozenset({UUID(_THIS_CONV_EARLIER), uuid4()})),
    )

    assert _sent_ref(vl) == workspace_figure_ref(tenant, user, f"agents/{_AGENT}/{own}")


def test_figure_run_ids_reads_the_run_segment_of_each_rendered_ref() -> None:
    tenant, user = uuid4(), uuid4()
    scoped = workspace_figure_ref(tenant, user, f"agents/{_AGENT}/{_figure_rel(run=_OTHER_CONV)}")
    flat = workspace_figure_ref(tenant, user, _figure_rel(run=_THIS_CONV_EARLIER))

    got = figure_run_ids([scoped, flat, "expert_work://image/not-a-figure", "junk", ""])

    assert got == frozenset({UUID(_OTHER_CONV), UUID(_THIS_CONV_EARLIER)})


def test_tool_context_takes_the_conversation_runs_from_viewed_figures() -> None:
    tenant, user = uuid4(), uuid4()
    ref = workspace_figure_ref(tenant, user, f"agents/{_AGENT}/{_figure_rel(run=_OTHER_CONV)}")
    config = {"configurable": {"tenant_id": str(tenant), "user_id": str(user)}}

    ctx = _build_tool_context(config, viewed_figures=[ref])  # type: ignore[arg-type]

    assert ctx.figure_run_ids == frozenset({UUID(_OTHER_CONV)})
    assert _build_tool_context(config).figure_run_ids == frozenset()  # type: ignore[arg-type]


def test_a_child_run_inherits_the_parents_conversation_runs_and_the_parent_run() -> None:
    """子代的检查点是新的:父对话的 run 与父这一个 run 要传下去,父刚渲好的页子代才找得到。"""
    parent_run = uuid4()
    parent = ToolContext(
        tenant_id=uuid4(),
        user_id=uuid4(),
        agent_key=_AGENT,
        run_id=parent_run,
        thread_id=uuid4(),
        figure_run_ids=frozenset({UUID(_THIS_CONV_EARLIER)}),
    )

    child = _build_tool_context(_child_config(parent, sub_thread_id=uuid4(), sub_run_id=uuid4()))

    assert child.figure_run_ids == frozenset({UUID(_THIS_CONV_EARLIER), parent_run})


@pytest.mark.anyio
async def test_seam_a_real_render_is_found_by_its_own_conversation_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """接缝:真 read_page 渲出来的 ref → ``figure_run_ids`` → 本对话的下一轮找得到;
    同一 agent 的另一个对话(检查点里没有这条 ref)找不到。"""
    _install_office_stubs(tmp_path, monkeypatch)
    root = tmp_path / "nas"
    tenant, user = uuid4(), uuid4()
    agent_dir = root / str(tenant) / str(user) / "agents" / _AGENT
    (agent_dir / "uploads").mkdir(parents=True)
    (agent_dir / _DOC).write_text("source")
    first_turn = ToolContext(tenant_id=tenant, user_id=user, agent_key=_AGENT, run_id=uuid4())
    rendered = await ReadPageTool(
        client=_LocalRenderRuntime(ws=agent_dir), figure_delivery="ask_image"
    ).call({"path": _DOC, "units": [3]}, ctx=first_turn)
    refs = list(rendered.state_updates["viewed_figures"])
    assert len(refs) == 1, f"真渲染没产出一页,下面什么也没验:{rendered.content}"

    vl = _FakeVLCaller()
    next_turn = _ctx(tenant, user, runs=figure_run_ids(refs))
    await _short_form_tool(root, vl).call({"path": _DOC, "unit": 3, "question": "?"}, ctx=next_turn)
    assert _sent_ref(vl) == refs[0]

    other_vl = _FakeVLCaller()
    with pytest.raises(FileNotFoundError, match="还没渲染"):
        await _short_form_tool(root, other_vl).call(
            {"path": _DOC, "unit": 3, "question": "?"}, ctx=_ctx(tenant, user, runs=frozenset())
        )
    assert other_vl.calls == []


@dataclass
class _SeesContext:
    """记下工具层真正收到的 ``ToolContext``。"""

    seen: list[ToolContext] = field(default_factory=list)
    name: str = "sees_context"

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(name=self.name, description="stub")

    async def call(self, args: Mapping[str, Any], *, ctx: ToolContext) -> ToolResult:
        del args
        self.seen.append(ctx)
        return ToolResult(content="ok")


@pytest.mark.anyio
async def test_the_real_tools_node_hands_the_conversation_runs_to_the_tool() -> None:
    """接线点:检查点里的 ``viewed_figures`` 经真 tools 节点进到工具收到的 ``ToolContext``。"""
    earlier = uuid4()
    tool = _SeesContext()
    registry = ToolRegistry()
    registry.register(tool)
    llm = _RecordingLLM(responses=[_tool_turn(tool.name)])

    async with _graph(llm, registry=registry) as compiled:
        await _invoke(compiled, viewed_figures=[_ref(3, run=earlier)])

    assert len(tool.seen) == 1, "工具没被调用,下面什么也没验"
    assert tool.seen[0].figure_run_ids == frozenset({earlier})
