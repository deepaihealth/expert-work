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
from orchestrator import (
    GraphRunner,
    ToolContext,
    ToolRegistry,
    ToolResult,
    ToolSpec,
    build_react_graph,
)
from orchestrator.graph_builder.builder import FIGURE_KEEP_RECENT, _figure_block_tail
from orchestrator.llm.providers._streaming import LLMDelta
from orchestrator.state import _merge_viewed_figures
from orchestrator.tools.read_page import workspace_figure_ref
from orchestrator.tools.sandbox_image_contract import EXEC_VIEW
from orchestrator.tools.skill_seed import sanitize_agent_key
from orchestrator.tools.workspace_scope import scoped_path, store_scope

from .test_read_page import _install_office_stubs, _real_out_rel, _run_render

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
    (pptx/pdf 的定义),docx 的场景显式给一个不同的值。``doc`` 决定 ``<doc-sha>``
    (路径派生),``content`` 决定 ``<render-sha>``(内容派生)。
    """
    unit_no = int(page) if unit is None else unit
    rel = "/".join(
        [
            WORKSPACE_OVERFLOW_DIR,
            str(run),
            RENDERED_FIGURE_DIR,
            _sha(doc),
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
    supports_vision: bool = True,
    tenant_id: UUID | None = _TENANT,
    user_id: UUID | None = _USER,
    agent_key: str = _AGENT_KEY,
) -> list[BaseMessage]:
    return _figure_block_tail(
        messages,
        viewed=viewed,
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
    assert "第 1 页 已退出上下文" in text
    assert "第 2 页 已退出上下文" in text
    assert "第 3 页 已退出上下文" not in text
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
    assert "第 1 页 已退出上下文" not in _block_text(out)


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
    assert "第 3 页(旧版本 —— 文档之后被改过)" in text


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
    ("page", "label"), [("3", "第 3 页"), ("03", "第 3 页"), ("005", "第 5 页")]
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
    """接线点必须拿**本次 run 的** config 去比,不是拿 ref 自己的身份。"""
    own, foreign = _ref(3), _ref(4, tenant=_OTHER_TENANT)
    registry = ToolRegistry()
    registry.register(_WritesFigures([own, foreign]))
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
