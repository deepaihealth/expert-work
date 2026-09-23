"""B-64 Task 7 —— 渲染页怎么进提示词(Path A 的块注入)。

渲染本身归 ``test_read_page``;这一份钉的是**挂哪几张、怎么退役、给谁看**:

1. 滑窗:只有最近 :data:`FIGURE_KEEP_RECENT` 个槽带像素。
2. 退役是替换不是删除:超窗的换成可见文字,写明怎么拿回来。
3. 三项比对(tenant / user / agent_key)缺一不可 —— ``viewed_figures`` 是任何
   工具都能写的通道,而 resolver 的身份取自 ref 自身。
4. 同一页的新旧两版只占一个像素槽,旧版可见标注。
5. 页号从文件名取,不从 ``_u<n>`` 取(docx 的 unit 是图的编号)。
6. ``supports_vision`` 为假时一个像素都不挂,且 ``build_react_graph`` 的默认值就是假。
7. 块只进这一次的提示词视图,从不落检查点(CM-C4)。

ref 一律按 ``read_page`` 真实产出的形状拼(常量取自 ``expert_work.persistence``,
作用域翻译走 ``workspace_scope``),不写 ``"r0"`` 这种字符串 —— 三项比对会把它们
当成解析失败全部拒掉,那样写出来的滑窗测试在「一张都不挂」的实现下也是绿的。
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.runnables import RunnableConfig

from expert_work.common.conversation_channel import FIGURE_BLOCK_MARK, HIDE_FROM_UI
from expert_work.persistence import (
    RENDERED_FIGURE_DIR,
    RENDERED_FIGURE_PAGE_STEM,
    RENDERED_FIGURE_SHA_HEX_LEN,
    RENDERED_FIGURE_UNIT_PREFIX,
    WORKSPACE_OVERFLOW_DIR,
)
from expert_work.protocol import StructuredOutputSpec
from expert_work.runtime.checkpointer import make_checkpointer
from expert_work.runtime.tokens import CharTokenEstimator
from orchestrator import (
    GraphRunner,
    ToolContext,
    ToolRegistry,
    ToolResult,
    ToolSpec,
    build_react_graph,
)
from orchestrator.context import ContextCompressor
from orchestrator.graph_builder.figure_block import (
    FIGURE_KEEP_RECENT,
    figure_block_tail,
    figure_freshness,
)
from orchestrator.llm.providers._streaming import LLMDelta
from orchestrator.multimodal import parse_rendered_figure_ref
from orchestrator.state import _merge_viewed_figures
from orchestrator.tools.nas_workspace_store import NasWorkspaceStore
from orchestrator.tools.read_page import ReadPageTool, document_sha, workspace_figure_ref
from orchestrator.tools.sandbox import RecordingSandboxRuntime, SandboxOutcome
from orchestrator.tools.sandbox_image_contract import EXEC_VIEW
from orchestrator.tools.skill_seed import sanitize_agent_key
from orchestrator.tools.workspace_scope import scoped_path, store_scope
from orchestrator.tools.workspace_store import RecordingWorkspaceStore

from .test_read_page import _install_office_stubs, _real_out_rel, _run_render, _snippet_params

_TENANT = uuid4()
_USER = uuid4()
_AGENT_KEY = sanitize_agent_key("pf-probe")
_RUN = uuid4()


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------


def _sha(seed: str) -> str:
    return hashlib.sha256(seed.encode()).hexdigest()[:RENDERED_FIGURE_SHA_HEX_LEN]


def _ref(
    page: int | str,
    *,
    doc: str = "d.pptx",
    content: str = "v1",
    unit: int | None = None,
    run: UUID = _RUN,
    tenant: UUID = _TENANT,
    user: UUID = _USER,
    agent_key: str = _AGENT_KEY,
) -> str:
    """一条 ``read_page`` 形状的 ref。

    ``page`` 可以是带补零的字符串(pdftoppm 按总页数补零);``unit`` 缺省等于页号
    (pptx/pdf 的定义),docx 的场景显式给一个不同的值。``doc`` 是模型传给
    ``read_page`` 的路径,``<doc-sha>`` 用 ``read_page`` 自己那个函数由它算出来;
    ``content`` 决定 ``<render-sha>``(内容派生)。
    """
    unit_no = int(page) if unit is None else unit
    rel = "/".join(
        [
            WORKSPACE_OVERFLOW_DIR,
            str(run),
            RENDERED_FIGURE_DIR,
            str(document_sha(doc, agent_key=agent_key)),
            _sha(content),
            f"{RENDERED_FIGURE_UNIT_PREFIX}{unit_no}",
            f"{RENDERED_FIGURE_PAGE_STEM}-{page}.jpg",
        ]
    )
    scope = store_scope(EXEC_VIEW, agent_key=agent_key)
    return workspace_figure_ref(tenant, user, scoped_path(scope, rel))


def _tail(
    messages: list[BaseMessage],
    viewed: Sequence[object],
    *,
    documents: Mapping[str, object] | None = None,
    supports_vision: bool = True,
    tenant_id: UUID | None = _TENANT,
    user_id: UUID | None = _USER,
    agent_key: str = _AGENT_KEY,
) -> list[BaseMessage]:
    return figure_block_tail(
        messages,
        viewed=viewed,
        documents=documents,
        supports_vision=supports_vision,
        tenant_id=tenant_id,
        user_id=user_id,
        agent_key=agent_key,
    )


def _pixels(messages: Sequence[BaseMessage]) -> list[str]:
    """提示词里全部 ``image_ref`` 块的 ref,按出现次序。"""
    out: list[str] = []
    for m in messages:
        if not isinstance(m.content, list):
            continue
        for b in m.content:
            if isinstance(b, dict) and b.get("type") == "image_ref":
                out.append(b["ref"])
    return out


def _marked(messages: Sequence[BaseMessage]) -> list[BaseMessage]:
    return [m for m in messages if (m.additional_kwargs or {}).get(FIGURE_BLOCK_MARK)]


def _block_text(messages: Sequence[BaseMessage]) -> str:
    blocks = _marked(messages)
    assert len(blocks) == 1, f"期望恰好一段渲染页块,实际 {len(blocks)} 段"
    content = blocks[0].content
    assert isinstance(content, list)
    return "".join(b.get("text", "") for b in content if isinstance(b, dict))


# ---------------------------------------------------------------------------
# 1. 滑窗
# ---------------------------------------------------------------------------


def test_only_the_newest_three_figures_carry_pixels() -> None:
    viewed = [_ref(p) for p in range(1, 6)]
    out = _tail([], viewed)
    assert FIGURE_KEEP_RECENT == 3
    assert _pixels(out) == viewed[-3:]


# ---------------------------------------------------------------------------
# 2. 退役是替换不是删除
# ---------------------------------------------------------------------------


def test_retired_figures_leave_a_visible_placeholder() -> None:
    """删除是静默失效 —— 模型必须看得见这里原来有张图,以及怎么拿回来。"""
    out = _tail([], [_ref(p) for p in range(1, 6)])
    text = _block_text(out)
    # 1、2 两页退役,按文档折叠成一行、编号压成区间;3 仍在窗口里。
    assert "编号 1-2 的图已退出上下文" in text
    assert "read_page" in text


def test_a_re_read_retired_page_comes_back_into_the_window() -> None:
    """占位文字说「再调一次 read_page」—— 那句话得是真的。

    同一个 run 里重读同一页拿到的是逐字相同的 ref,经过真 reducer 合并之后,它必须
    重新带像素。按「首次看到」排的 reducer 下它永远回不来。
    """
    first = [_ref(p) for p in range(1, 6)]
    again = _merge_viewed_figures(first, [_ref(1)])
    out = _tail([], again)
    assert _ref(1) in _pixels(out)
    assert "编号 2-3 的图已退出上下文" in _block_text(out)


# ---------------------------------------------------------------------------
# 3. 三项比对
# ---------------------------------------------------------------------------


_OTHER_TENANT = uuid4()
_OTHER_USER = uuid4()


@pytest.mark.parametrize(
    "foreign",
    [
        pytest.param(_ref(9, tenant=_OTHER_TENANT), id="other-tenant"),
        pytest.param(_ref(9, user=_OTHER_USER), id="other-user"),
        pytest.param(_ref(9, agent_key=sanitize_agent_key("someone-else")), id="other-agent"),
        # 本 run 绑了 agent,ref 却落在用户根(没绑 agent 的形状)。
        pytest.param(_ref(9, agent_key=""), id="unbound-ref-in-bound-run"),
        pytest.param("expert_work://workspace/not-a-uuid/x/y.jpg", id="unparseable"),
        # 三项全对、但不是 read_page 的产出形状 —— 模型自己写进 .tool_results 的文件。
        pytest.param(
            workspace_figure_ref(
                _TENANT, _USER, scoped_path(store_scope(EXEC_VIEW, agent_key=_AGENT_KEY), "a.jpg")
            ),
            id="own-scope-not-a-render",
        ),
        # 同上,但尾巴刻意凑成 ``_u<n>/page-NN.jpg`` —— 只有形状判据挡得住它,
        # 光拆最后几段是能拆出一个像样的页号的。
        pytest.param(
            workspace_figure_ref(
                _TENANT,
                _USER,
                scoped_path(
                    store_scope(EXEC_VIEW, agent_key=_AGENT_KEY),
                    f"notes/a/b/{RENDERED_FIGURE_UNIT_PREFIX}3/{RENDERED_FIGURE_PAGE_STEM}-3.jpg",
                ),
            ),
            id="own-scope-render-lookalike",
        ),
        pytest.param(f"expert_work://image/{_TENANT}/{uuid4()}/{uuid4()}.png", id="upload-scheme"),
    ],
)
def test_a_ref_that_is_not_this_runs_own_render_never_gets_pixels(foreign: str) -> None:
    """``viewed_figures`` 是任何工具都能写的通道;resolver 的身份取自 ref 自身。

    同一份输入里放一条合法的 —— 否则「一张都不挂」的实现也能让这条绿。
    """
    own = _ref(1)
    out = _tail([], [own, foreign])
    assert _pixels(out) == [own]
    assert foreign not in _block_text(out)


def test_a_bound_ref_is_refused_in_an_unbound_run() -> None:
    """反方向:本 run 没绑 agent(``agent_key=""``),ref 却带 ``agents/<key>/``。"""
    own = _ref(1, agent_key="")
    out = _tail([], [own, _ref(2)], agent_key="")
    assert _pixels(out) == [own]


def test_non_string_entries_are_ignored() -> None:
    own = _ref(1)
    out = _tail([], [own, 42, None, {"ref": _ref(2)}])
    assert _pixels(out) == [own]


def test_no_run_identity_means_no_pixels() -> None:
    """config 里拿不到 tenant / user 时没有可比对的身份 —— 一张都不挂。"""
    assert _pixels(_tail([], [_ref(1)], tenant_id=None)) == []
    assert _pixels(_tail([], [_ref(1)], user_id=None)) == []


# ---------------------------------------------------------------------------
# 4. 同一页的新旧两版
# ---------------------------------------------------------------------------


def test_two_versions_of_one_page_share_one_pixel_slot() -> None:
    """次序刻意排成「新旧两版之间夹着一页」:不归槽的话,最近 3 条是旧版 + 两页,
    会把最早那一页挤掉 —— 这条测试才区分得出有没有归槽。"""
    old, new = _ref(3, content="v1"), _ref(3, content="v2")
    p5, p7 = _ref(5), _ref(7)
    out = _tail([], [p5, old, p7, new])
    assert _pixels(out) == [p5, p7, new]
    text = _block_text(out)
    assert "编号 3 有旧版本(文档之后被改过)" in text


def test_an_edit_reverted_page_shows_the_reverted_version_as_current() -> None:
    """A → B → A:第三次读拿到的 ref 与第一次逐字相同。当前版本必须是 A。"""
    a, b = _ref(3, content="A"), _ref(3, content="B")
    viewed = _merge_viewed_figures(_merge_viewed_figures([a], [b]), [a])
    out = _tail([], viewed)
    assert _pixels(out) == [a]
    assert "旧版本" in _block_text(out)


def test_the_same_render_from_another_run_is_not_called_an_old_version() -> None:
    """跨 run 重读同一份没改过的文档:ref 不同(run_id 段),像素相同。只挂一张,
    也不许说「文档之后被改过」—— 它没被改过。"""
    earlier, later = _ref(3, run=uuid4()), _ref(3, run=uuid4())
    out = _tail([], [earlier, later])
    assert _pixels(out) == [later]
    assert "旧版本" not in _block_text(out)


def test_different_documents_do_not_share_a_slot() -> None:
    a3, b3 = _ref(3, doc="a.pptx"), _ref(3, doc="b.pptx")
    assert _pixels(_tail([], [a3, b3])) == [a3, b3]


# ---------------------------------------------------------------------------
# 5. 页号从文件名取
# ---------------------------------------------------------------------------


def test_page_label_comes_from_the_file_name_not_the_unit() -> None:
    """docx 的 unit 是图的编号:「第 7 处图」可能在第 3 页。"""
    text = _block_text(_tail([], [_ref(3, unit=7, doc="r.docx")]))
    assert "第 3 页" in text
    assert "第 7 页" not in text


@pytest.mark.parametrize(
    ("page", "label"),
    [("3", "第 3 页"), ("03", "第 3 页"), ("005", "第 5 页"), ("105", "第 105 页")],
)
def test_every_padding_width_is_read(page: str, label: str) -> None:
    """pdftoppm 按总页数补零:``page-3`` / ``page-03`` / ``page-005`` 都会出现。"""
    text = _block_text(_tail([], [_ref(page)]))
    assert label in text


async def test_a_real_read_page_ref_gets_pixels_and_its_page_label(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """不手写样本路径:out_rel 取自宿主真跑 ``ReadPageTool.call``,尾巴取自片段真跑,
    作用域翻译走 ``read_page`` 同一对函数。形状判据严过头时,这里是唯一会红的地方。"""
    _install_office_stubs(tmp_path, monkeypatch)
    (tmp_path / "d.pptx").write_text("source")
    out_rel = await _real_out_rel()
    env = _run_render(tmp_path, "d.pptx", units=[3], out_rel=out_rel)
    assert env["ok"] is True
    rel = env["rendered"][0]["rel"]
    ref = workspace_figure_ref(
        _TENANT, _USER, scoped_path(store_scope(EXEC_VIEW, agent_key=_AGENT_KEY), rel)
    )
    out = _tail([], [ref])
    assert _pixels(out) == [ref]
    assert "第 3 页" in _block_text(out)


# ---------------------------------------------------------------------------
# 6. 看不见就不挂
# ---------------------------------------------------------------------------


def test_no_pixels_when_the_model_cannot_see() -> None:
    """非视觉主模型不挂 image 块 —— 那条路由走 ask_image(Path B)。"""
    prior = _tail([], [_ref(1)])
    assert _pixels(prior), "前置没挂上,下面的断言什么也没验"
    out = _tail(prior, [_ref(1), _ref(2)], supports_vision=False)
    assert _pixels(out) == []
    assert _marked(out) == []


# ---------------------------------------------------------------------------
# 7. 生命周期:每轮重建,不回写入参
# ---------------------------------------------------------------------------


def test_the_block_is_appended_to_a_new_list() -> None:
    original: list[BaseMessage] = [HumanMessage(content="看看第三页")]
    prompt_view = _tail(original, [_ref(3)])
    assert len(prompt_view) == 2
    assert len(original) == 1
    assert _marked(original) == []
    assert (prompt_view[-1].additional_kwargs or {}).get(HIDE_FROM_UI) is True


def test_previous_blocks_are_dropped_before_appending() -> None:
    prior = _tail([], [_ref(1)])
    again = _tail(prior, [_ref(1), _ref(2)])
    assert len(_marked(again)) == 1
    assert _pixels(again) == [_ref(1), _ref(2)]


def test_a_stale_block_goes_even_when_nothing_is_left_to_show() -> None:
    prior = _tail([], [_ref(1)])
    assert _marked(_tail(prior, [])) == []


# ---------------------------------------------------------------------------
# 图级:接线点、默认值、检查点
# ---------------------------------------------------------------------------


@dataclass
class _RecordingLLM:
    """记下每一次收到的提示词;脚本用完就不带工具地答一句, run 于是收尾。"""

    responses: list[AIMessage] = field(default_factory=list)
    seen_prompts: list[list[BaseMessage]] = field(default_factory=list)
    calls: int = 0

    async def __call__(
        self,
        *,
        messages: Sequence[BaseMessage],
        tools: Sequence[ToolSpec],
        output_schema: StructuredOutputSpec | None = None,
        on_delta: Callable[[LLMDelta], Awaitable[None]] | None = None,
    ) -> AIMessage:
        del tools, output_schema, on_delta
        self.seen_prompts.append(list(messages))
        idx = self.calls
        self.calls += 1
        if idx < len(self.responses):
            return self.responses[idx]
        return AIMessage(content="done")


@dataclass
class _WritesFigures:
    """替 ``read_page`` 往 ``viewed_figures`` 写 ref —— 与真工具走同一条 state_updates 通道。"""

    refs: list[str]
    name: str = "fake_read_page"

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(name=self.name, description="stub")

    async def call(self, args: Mapping[str, Any], *, ctx: ToolContext) -> ToolResult:
        del args, ctx
        return ToolResult(content="ok", state_updates={"viewed_figures": list(self.refs)})


def _configurable() -> dict[str, Any]:
    return {
        "thread_id": str(uuid4()),
        "tenant_id": str(_TENANT),
        "user_id": str(_USER),
        "agent_key": _AGENT_KEY,
    }


@asynccontextmanager
async def _graph(
    llm: _RecordingLLM, *, registry: ToolRegistry | None = None, **kwargs: Any
) -> AsyncIterator[Any]:
    async with make_checkpointer("memory") as cp:
        yield GraphRunner(checkpointer=cp).compile(
            build_react_graph(llm_caller=llm, tool_registry=registry or ToolRegistry(), **kwargs)
        )


async def _invoke(compiled: Any, **state: Any) -> RunnableConfig:
    config: RunnableConfig = {"configurable": _configurable()}
    await compiled.ainvoke(
        {
            "messages": [HumanMessage(content="看看第三页")],
            "step_count": 0,
            "max_steps": 5,
            **state,
        },
        config=config,
    )
    return config


def _tool_turn(name: str) -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": name, "args": {}, "id": "c1"}])


async def test_a_figure_a_tool_wrote_reaches_the_next_prompt() -> None:
    """真 reducer + 真 ``agent_node`` 接线点:工具这一步写进去,下一步提示词里就有。"""
    ref = _ref(3)
    registry = ToolRegistry()
    registry.register(_WritesFigures([ref]))
    llm = _RecordingLLM(responses=[_tool_turn("fake_read_page")])
    async with _graph(llm, registry=registry, supports_vision=True) as compiled:
        await _invoke(compiled)
    assert _pixels(llm.seen_prompts[0]) == []
    assert _pixels(llm.seen_prompts[1]) == [ref]


async def test_a_foreign_ref_a_tool_wrote_never_reaches_the_model() -> None:
    """接线点必须拿**本次 run 的** config 去比,不是拿 ref 自己的身份。

    外来的那条排在前面:一个「拿列表里第一条 ref 的身份当基准」的接线在这个次序下
    会放行它、拒掉自己的那条。
    """
    own, foreign = _ref(3), _ref(4, tenant=_OTHER_TENANT)
    registry = ToolRegistry()
    registry.register(_WritesFigures([foreign, own]))
    llm = _RecordingLLM(responses=[_tool_turn("fake_read_page")])
    async with _graph(llm, registry=registry, supports_vision=True) as compiled:
        await _invoke(compiled)
    assert _pixels(llm.seen_prompts[1]) == [own]


async def test_the_graph_hangs_no_pixels_unless_told_the_model_can_see() -> None:
    """``build_react_graph`` 不传 ``supports_vision`` —— 失败方向必须是安全那一侧。"""
    llm = _RecordingLLM()
    async with _graph(llm) as compiled:
        await _invoke(compiled, viewed_figures=[_ref(3)])
    assert _pixels(llm.seen_prompts[0]) == []
    assert _marked(llm.seen_prompts[0]) == []


async def test_the_block_never_lands_in_the_checkpoint() -> None:
    """与工作区块同一口径 —— 块只进这一次的提示词视图,不进 state["messages"]。"""
    llm = _RecordingLLM()
    async with _graph(llm, supports_vision=True) as compiled:
        config = await _invoke(compiled, viewed_figures=[_ref(3)])
        snapshot = await compiled.aget_state(config)
    assert len(_marked(llm.seen_prompts[0])) == 1, "块没注入, 这条测试就什么也没验"
    assert _marked(snapshot.values["messages"]) == []
    assert _pixels(snapshot.values["messages"]) == []


# ---------------------------------------------------------------------------
# 回修第 3 轮 (a) —— 占位按文档折叠、可恢复
# ---------------------------------------------------------------------------


def _documents(*paths: str) -> dict[str, str]:
    """``read_page`` 写进 ``figure_documents`` 的那个形状:``<doc-sha>`` → 路径。"""
    return {str(document_sha(p, agent_key=_AGENT_KEY)): p for p in paths}


def test_a_retired_page_says_which_path_to_pass_back() -> None:
    """ref 里只有路径哈希;退役占位必须给出模型能原样再传给 read_page 的路径。"""
    out = _tail(
        [],
        [_ref(p, doc="uploads/a.pptx") for p in (1, 2)]
        + [_ref(p, doc="uploads/b.pdf") for p in (1, 2, 3)],
        documents=_documents("uploads/a.pptx", "uploads/b.pdf"),
    )
    text = _block_text(out)
    assert '文档 "uploads/a.pptx":编号 1-2 的图已退出上下文' in text
    assert "path 填这个路径" in text
    assert '"uploads/b.pdf" 第 1 页' in text


def test_without_a_recorded_path_the_placeholder_says_so() -> None:
    text = _block_text(_tail([], [_ref(p) for p in range(1, 6)]))
    assert "路径没记下来" in text
    assert "你当初调用 read_page 时传的那个路径" in text


def test_a_path_that_does_not_hash_to_the_key_is_not_shown() -> None:
    """``figure_documents`` 是工具可写的通道:键对不上路径就不采信。"""
    forged = {str(document_sha("d.pptx", agent_key=_AGENT_KEY)): "别的文件.pptx"}
    text = _block_text(_tail([], [_ref(p) for p in range(1, 6)], documents=forged))
    assert "别的文件" not in text
    assert "路径没记下来" in text


def test_placeholders_grow_with_documents_not_with_pages() -> None:
    """同一份文档看 40 页、每页改 3 版:仍然只有「退役」「旧版本」两行,编号压成区间。"""

    def text_for(pages: int) -> str:
        viewed = [
            _ref(p, doc="uploads/a.pptx", content=f"v{v}")
            for v in range(3)
            for p in range(1, pages + 1)
        ]
        return _block_text(_tail([], viewed, documents=_documents("uploads/a.pptx")))

    few, many = text_for(5), text_for(40)
    assert few.count("\n") == many.count("\n")
    assert "编号 1-37 的图已退出上下文" in many
    assert "编号 1-40 有旧版本" in many


# ---------------------------------------------------------------------------
# 回修第 3 轮 (b) —— 压缩判定看得见图块
# ---------------------------------------------------------------------------


@dataclass
class _CountingSummariser:
    calls: int = 0

    async def __call__(
        self, *, messages: Sequence[BaseMessage], tools: Sequence[ToolSpec]
    ) -> AIMessage:
        del messages, tools
        self.calls += 1
        return AIMessage(content="- 摘要")


def _history(total_tokens: int, count: int = 6) -> list[BaseMessage]:
    """``count`` 条消息,按 ``CharTokenEstimator``(4 字符 1 token)合计约 ``total_tokens``。"""
    per = total_tokens * 4 // count
    out: list[BaseMessage] = []
    for i in range(count):
        body = f"第{i}条" + "x" * per
        out.append(HumanMessage(content=body) if i % 2 == 0 else AIMessage(content=body))
    return out


async def _run_with_compressor(
    *, history_tokens: int, context_window: int, supports_vision: bool, count: int = 6
) -> tuple[_CountingSummariser, _RecordingLLM]:
    summariser = _CountingSummariser()
    compressor = ContextCompressor(
        llm_caller=summariser,
        context_window=context_window,
        head_keep=1,
        tail_keep=1,
        estimator=CharTokenEstimator(),
    )
    llm = _RecordingLLM()
    async with _graph(
        llm, context_compressor=compressor, supports_vision=supports_vision
    ) as compiled:
        config: RunnableConfig = {"configurable": _configurable()}
        await compiled.ainvoke(
            {
                "messages": _history(history_tokens, count),
                "step_count": 0,
                "max_steps": 5,
                "viewed_figures": [_ref(p) for p in (1, 2, 3)],
            },
            config=config,
        )
    return summariser, llm


async def test_the_figure_block_counts_toward_the_compression_threshold() -> None:
    """历史卡在阈值下(13k < 0.7 * 20k = 14k),三张图(3 * 1300)一加就超。

    图块挂在压缩**之后**;不把它的估算交给压缩器,这里一次都不会压。
    """
    summariser, llm = await _run_with_compressor(
        history_tokens=13_000, context_window=20_000, supports_vision=True
    )
    assert summariser.calls >= 1
    prompt = llm.seen_prompts[0]
    # 块本身不进被总结的那段:压缩之后它仍然完整地挂在尾部。
    assert len(_marked(prompt)) == 1
    assert len(_pixels(prompt)) == 3


async def test_without_a_figure_block_the_same_history_is_not_compressed() -> None:
    """对照组:同一段历史,主模型看不了图(没有块)—— 不压。"""
    summariser, _ = await _run_with_compressor(
        history_tokens=13_000, context_window=20_000, supports_vision=False
    )
    assert summariser.calls == 0


async def test_a_small_window_compresses_hard_but_does_not_fail() -> None:
    """8K 那一档:预留本身就接近或超过阈值。压到头也不许因为预留抛
    ``ContextOverflowError`` —— 失败判据维持「不算预留」的口径。"""
    summariser, llm = await _run_with_compressor(
        history_tokens=1_500, context_window=4_000, supports_vision=True
    )
    assert summariser.calls >= 1
    assert len(_pixels(llm.seen_prompts[0])) == 3


async def test_a_small_window_with_nothing_left_to_summarise_does_not_fail() -> None:
    """同上,但历史只有两条:头尾各留一条,中段一开始就是空的 —— 压缩器的
    「中段已空」失败判据。不算预留时这段历史远在阈值下,不许因为预留抛
    ``ContextOverflowError``(上一条测试走的是「次数用完」那条路,够不到这里)。"""
    summariser, llm = await _run_with_compressor(
        history_tokens=500, context_window=4_000, supports_vision=True, count=2
    )
    assert summariser.calls == 0
    assert len(_pixels(llm.seen_prompts[0])) == 3


# ---------------------------------------------------------------------------
# 回修第 4 轮 I-1 —— 预留不许让压缩器白跑空的总结更新
# ---------------------------------------------------------------------------


@dataclass
class _ModeRecordingSummariser:
    """记下每次总结调用是「全新总结」还是「更新」,以及更新时 NEW EVENTS 是否为空。"""

    calls: list[tuple[str, bool]] = field(default_factory=list)

    async def __call__(
        self, *, messages: Sequence[BaseMessage], tools: Sequence[ToolSpec]
    ) -> AIMessage:
        del tools
        body = str(messages[-1].content)
        update = body.startswith("PREVIOUS SUMMARY")
        self.calls.append(
            ("update" if update else "fresh", update and body.rstrip().endswith("NEW EVENTS:"))
        )
        return AIMessage(content="- 摘要")


async def test_the_reserve_does_not_trigger_empty_summary_updates() -> None:
    """8K 窗口:压完一遍后「头 + 摘要 + 尾」不算预留在阈值下、加上预留仍超。

    修前:第 2、3 遍中段只剩上一轮摘要,照样调一次「更新」而 NEW EVENTS 是空的,
    每一遍还把中段(就是那份摘要)交给 ``on_pre_compaction`` 冲进长期记忆 ——
    一步 3 次总结、3 次冲记忆,而压缩结果不落检查点,下一步从完整历史再来一遍。
    修后:一步 1 次总结、1 次冲记忆。
    """
    summariser = _ModeRecordingSummariser()
    flushed: list[list[BaseMessage]] = []

    async def _flush(middle: Sequence[BaseMessage], config: RunnableConfig, token: Any) -> int:
        del config, token
        flushed.append(list(middle))
        return 0

    compressor = ContextCompressor(
        llm_caller=summariser, context_window=8_000, estimator=CharTokenEstimator()
    )
    llm = _RecordingLLM()
    async with _graph(
        llm, context_compressor=compressor, supports_vision=True, pre_compaction_flush=_flush
    ) as compiled:
        config: RunnableConfig = {"configurable": _configurable()}
        await compiled.ainvoke(
            {
                # 20 条、每条约 400 token:远超 5.6k 阈值;压完后头 4 + 尾 6 ≈ 4k,
                # 不算预留在阈值下,加上三张图的预留就超。
                "messages": _history(8_000, 20),
                "step_count": 0,
                "max_steps": 5,
                "viewed_figures": [_ref(p) for p in (1, 2, 3)],
            },
            config=config,
        )
    assert summariser.calls == [("fresh", False)]
    assert len(flushed) == 1
    assert len(_pixels(llm.seen_prompts[0])) == 3


# ---------------------------------------------------------------------------
# 回修第 4 轮 I-2 —— 接缝:真 read_page → tools 节点 → reducer → 检查点 → 下一轮块
# ---------------------------------------------------------------------------


class _EchoRenderRuntime(RecordingSandboxRuntime):
    """沙箱替身:按宿主真喂给片段的 ``out_rel`` / ``units`` 回一份「渲染成功」。

    不跑片段 —— 这条测试管的是 state 的接线,不是渲染。``out_rel`` 从片段参数里
    取(``_snippet_params``),不自己拼,所以 ``<doc-sha>`` 就是 read_page 真算的那个。
    """

    async def exec(
        self,
        *,
        sandbox_id: UUID,
        code: str,
        timeout_s: int | None,
        agent_key: str = "",
        run_id: UUID | None = None,
    ) -> SandboxOutcome:
        await super().exec(
            sandbox_id=sandbox_id,
            code=code,
            timeout_s=timeout_s,
            agent_key=agent_key,
            run_id=run_id,
        )
        params = _snippet_params(code)
        rendered = [
            {
                "unit": u,
                "rel": f"{params['out_rel']}/{'b' * 16}/{RENDERED_FIGURE_UNIT_PREFIX}{u}/"
                f"{RENDERED_FIGURE_PAGE_STEM}-{u:02d}.jpg",
                "bytes": 100,
            }
            for u in params["units"]
        ]
        return SandboxOutcome(
            stdout=json.dumps({"ok": True, "rendered": rendered}),
            stderr="",
            exit_code=0,
            timed_out=False,
        )


def _read_page_call(path: str, units: list[int], call_id: str) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{"name": "read_page", "args": {"path": path, "units": units}, "id": call_id}],
    )


async def test_the_path_read_page_recorded_reaches_later_prompts_and_runs() -> None:
    """两次 read_page(共 4 页)→ 第 3 次模型调用时第 1 页已退役,占位里必须是
    read_page 自己记下的路径;同一会话的下一个 run 里也还在。

    ``agent_node`` 若不把 ``state["figure_documents"]`` 交给建块函数,占位会退化成
    「路径没记下来」—— 那句话与旧会话的合法兜底逐字相同,看输出分不出是 bug 还是
    旧数据,所以这里要直接断言路径本身。
    """
    registry = ToolRegistry()
    registry.register(ReadPageTool(client=_EchoRenderRuntime(), figure_delivery="inline"))
    thread = str(uuid4())

    def _config() -> RunnableConfig:
        return {"configurable": {**_configurable(), "thread_id": thread, "run_id": str(uuid4())}}

    async with make_checkpointer("memory") as cp:
        llm = _RecordingLLM(
            responses=[
                _read_page_call("a.pptx", [1, 2], "c1"),
                _read_page_call("a.pptx", [3, 4], "c2"),
            ]
        )
        graph = GraphRunner(checkpointer=cp).compile(
            build_react_graph(llm_caller=llm, tool_registry=registry, supports_vision=True)
        )
        await graph.ainvoke(
            {"messages": [HumanMessage(content="看图")], "step_count": 0, "max_steps": 8},
            config=_config(),
        )
        third = _block_text(llm.seen_prompts[2])
        assert '文档 "a.pptx":编号 1 的图已退出上下文' in third

        llm2 = _RecordingLLM()
        graph2 = GraphRunner(checkpointer=cp).compile(
            build_react_graph(llm_caller=llm2, tool_registry=registry, supports_vision=True)
        )
        await graph2.ainvoke(
            {"messages": [HumanMessage(content="继续")], "step_count": 0, "max_steps": 4},
            config=_config(),
        )
        assert '文档 "a.pptx":编号 1 的图已退出上下文' in _block_text(llm2.seen_prompts[0])


# ---------------------------------------------------------------------------
# 回修第 4 轮 M-2 —— 路径每份文档只写一次;行分隔符转义
# ---------------------------------------------------------------------------


def test_each_path_is_written_once_and_pixels_follow_the_labels() -> None:
    """窗口里 a、b、a 交错:按文档归到一起,路径各写一次;图片次序与文字次序一致。"""
    a1, b1, a2 = _ref(1, doc="a.pptx"), _ref(1, doc="b.pdf"), _ref(2, doc="a.pptx")
    out = _tail([], [a1, b1, a2], documents=_documents("a.pptx", "b.pdf"))
    text = _block_text(out)
    assert text.count('"a.pptx"') == 1
    assert text.count('"b.pdf"') == 1
    assert '"a.pptx" 第 1 页、第 2 页;"b.pdf" 第 1 页' in text
    assert _pixels(out) == [a1, a2, b1]


def test_a_line_separator_in_a_path_is_escaped() -> None:
    """U+2028 / U+2029 在 ``json.dumps(ensure_ascii=False)`` 下原样放行,模型可能当换行读。"""
    path = "uploads/报告\u2028第二行\u2029.pptx"
    out = _tail(
        [],
        [_ref(p, doc=path) for p in range(1, 5)],
        documents=_documents(path),
    )
    text = _block_text(out)
    assert "\u2028" not in text and "\u2029" not in text
    assert "报告\\u2028第二行\\u2029.pptx" in text


# ---------------------------------------------------------------------------
# 终审 #1 —— 文档在渲染之后被改过/替换了:这一页不附图,换成可见文字
# ---------------------------------------------------------------------------


def _put_file(root: Path, rel: str, *, mtime: float, data: bytes = b"x") -> None:
    target = root / str(_TENANT) / str(_USER) / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    os.utime(target, (mtime, mtime))


def _render_rel(ref: str) -> str:
    figure = parse_rendered_figure_ref(ref)
    assert figure is not None
    return figure.workspace.rel


async def _prompt_with_store(root: Path, ref: str) -> list[BaseMessage]:
    """真 ``agent_node`` + 真 NAS store:把 ``ref`` 放进 state,取第一次模型调用的提示词。"""
    llm = _RecordingLLM()
    async with _graph(
        llm, supports_vision=True, workspace_store=NasWorkspaceStore(root=str(root))
    ) as compiled:
        await _invoke(compiled, viewed_figures=[ref], figure_documents=_documents("d.pptx"))
    return llm.seen_prompts[0]


async def test_a_page_whose_document_was_replaced_is_not_shown(tmp_path: Path) -> None:
    """同名上传覆盖了原文档之后,不能拿旧文档的页顶着当前路径的名字给模型看。"""
    ref = _ref(3)
    doc = f"agents/{_AGENT_KEY}/d.pptx"
    _put_file(tmp_path, doc, mtime=1_000, data=b"customer A")
    _put_file(tmp_path, _render_rel(ref), mtime=2_000)

    fresh = await _prompt_with_store(tmp_path, ref)
    assert _pixels(fresh) == [ref], "新鲜的页都没挂上,下面的断言什么也没验"

    _put_file(tmp_path, doc, mtime=3_000, data=b"customer B")
    stale = await _prompt_with_store(tmp_path, ref)

    assert _pixels(stale) == []
    text = _block_text(stale)
    assert "文档「d.pptx」在你看过之后被改过或替换了,编号 3 的渲染页已过期" in text
    assert "重新调用 read_page,path 填「d.pptx」,units 填 3" in text
    assert "下面依次附上" not in text


@pytest.mark.parametrize(
    ("state", "phrase"),
    [
        ("missing", "已不在工作区里"),
        ("unknown", "核对不了在渲染之后有没有被改过"),
    ],
)
def test_a_page_that_cannot_be_verified_is_withheld_visibly(state: str, phrase: str) -> None:
    ref = _ref(3)
    messages = figure_block_tail(
        [],
        viewed=[ref],
        documents=_documents("d.pptx"),
        supports_vision=True,
        tenant_id=_TENANT,
        user_id=_USER,
        agent_key=_AGENT_KEY,
        freshness={ref: state},  # type: ignore[dict-item]
    )
    assert _pixels(messages) == []
    assert phrase in _block_text(messages)


async def test_a_failing_freshness_check_withholds_the_page_and_does_not_raise() -> None:
    """列目录失败:这一轮不许挂掉,也不许悄悄当成「新鲜」照挂。"""
    ref = _ref(3)
    store = RecordingWorkspaceStore(workspace_list_error=RuntimeError("nas down"))
    freshness = await figure_freshness(
        store,
        viewed=[ref],
        documents=_documents("d.pptx"),
        tenant_id=_TENANT,
        user_id=_USER,
        agent_key=_AGENT_KEY,
    )
    assert freshness == {ref: "unknown"}
