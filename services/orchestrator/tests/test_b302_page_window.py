"""B-302 PR 2 第 2 层 —— 一份文档的页图在它最后一次 ``read_page`` 之后只带 N 次模型调用。

设计 ``docs/superpowers/specs/2026-11-11-b302-runaway-reverify-design.md`` §4.2.2-§4.2.6、
测试表 §4.2.10(T2-1 … T2-11)。

* 按**文档**计、不按每页计(还在往下读时已取的页都在);
* 判过期在 ``_window`` 里:过期的页从槽位整个拿掉 —— 不做新鲜度核对、不出 B-268 的退役行、
  不出「重取编号 N」行,只出一行可找回的过期占位;
* ``figure_view_steps = 0`` = 今天的挂图行为,逐字节相同;
* 措辞:占位 / 回执里不再说 number(s),一律 detail(s)。

ref 一律按 ``read_page`` 的真实产出形状拼(借 ``test_figure_block_injection`` 的夹具)。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any
from uuid import UUID, uuid4

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.runnables import RunnableConfig

from expert_work.runtime.checkpointer import make_checkpointer
from orchestrator import (
    GraphRunner,
    ToolContext,
    ToolRegistry,
    ToolResult,
    ToolSpec,
    build_react_graph,
)
from orchestrator.graph_builder.figure_block import (
    FigureExpiry,
    figure_block_tail,
    figure_freshness,
)
from orchestrator.tools.figure_reads import (
    FigureRead,
    ledger_entries,
    read_notice,
    stamp_step,
)
from orchestrator.tools.read_page import ReadPageTool
from orchestrator.tools.workspace_paths import WorkspaceView
from orchestrator.tools.workspace_store import RecordingWorkspaceStore

from .test_b268_figure_reads import _Renders
from .test_figure_block_injection import (
    _AGENT_KEY,
    _TENANT,
    _USER,
    _block_text,
    _documents,
    _EchoRenderRuntime,
    _marked,
    _pixels,
    _read_page_call,
    _RecordingLLM,
    _ref,
)
from .test_figure_block_injection import _RUN as _TURN

_VIEW = WorkspaceView(agent_key=_AGENT_KEY)
_N = 3


def _doc_sha(path: str) -> str:
    figure = _ref(1, doc=path)
    from orchestrator.multimodal import parse_rendered_figure_ref

    parsed = parse_rendered_figure_ref(figure)
    assert parsed is not None
    return parsed.doc_sha


def _reads(path: str, units: Sequence[int], *, step: int, run: UUID = _TURN) -> list[Any]:
    """``read_page`` 记的账 + tools 节点盖的步数章。"""
    entries = ledger_entries(run, _doc_sha(path), [FigureRead(u) for u in units], None)
    return stamp_step(entries, step)


def _expiry(reads: Sequence[object], *, step: int, n: int = _N, run: UUID = _TURN) -> FigureExpiry:
    return FigureExpiry(view_steps=n, step=step, reads=tuple(reads), run_id=run)


def _tail(
    viewed: Sequence[object],
    *,
    expiry: FigureExpiry | None,
    documents: Mapping[str, object] | None = None,
    freshness: Mapping[str, Any] | None = None,
) -> list[BaseMessage]:
    return figure_block_tail(
        [],
        viewed=viewed,
        documents=documents,
        supports_vision=True,
        tenant_id=_TENANT,
        user_id=_USER,
        view=_VIEW,
        freshness=freshness,
        expiry=expiry,
    )


# ---------------------------------------------------------------------------
# T2-1 / T2-2 —— 第 k 步取页,k+2 还在,k+3 换成占位
# ---------------------------------------------------------------------------


def test_t2_1_pages_leave_three_steps_after_the_last_read() -> None:
    viewed = [_ref(p, doc="a.pdf") for p in (1, 2, 3)]
    reads = _reads("a.pdf", [1, 2, 3], step=5)
    out = _tail(viewed, expiry=_expiry(reads, step=8), documents=_documents("a.pdf"))
    assert _pixels(out) == []
    text = _block_text(out)
    assert '[Document "a.pdf": you viewed units 1-3.' in text
    assert f"stay attached only for {_N} steps after you last called read_page" in text
    assert "If you need a specific detail you never wrote down, call read_page again" in text
    # 只从可见集合拿掉、没从槽位拿掉时,B-268 的退役行会同时出现。
    assert "left your context" not in text
    assert "下面依次附上" not in text


def test_t2_2_pages_are_still_attached_two_steps_after_the_read() -> None:
    viewed = [_ref(p, doc="a.pdf") for p in (1, 2, 3)]
    reads = _reads("a.pdf", [1, 2, 3], step=5)
    out = _tail(viewed, expiry=_expiry(reads, step=7), documents=_documents("a.pdf"))
    assert _pixels(out) == viewed
    assert "you viewed" not in _block_text(out)


# ---------------------------------------------------------------------------
# T2-3 —— 按文档计,不按每页计
# ---------------------------------------------------------------------------


def test_t2_3_a_document_still_being_read_keeps_all_its_pages() -> None:
    viewed = [_ref(p, doc="a.pdf") for p in range(1, 7)]
    reads = [*_reads("a.pdf", [1, 2, 3], step=5), *_reads("a.pdf", [4, 5, 6], step=7)]
    out = _tail(viewed, expiry=_expiry(reads, step=9), documents=_documents("a.pdf"))
    assert _pixels(out) == viewed


def test_documents_expire_independently() -> None:
    a = [_ref(p, doc="a.pdf") for p in (1, 2)]
    b = [_ref(p, doc="b.pdf") for p in (1, 2)]
    reads = [*_reads("a.pdf", [1, 2], step=2), *_reads("b.pdf", [1, 2], step=6)]
    out = _tail([*a, *b], expiry=_expiry(reads, step=7), documents=_documents("a.pdf", "b.pdf"))
    assert _pixels(out) == b
    text = _block_text(out)
    assert '[Document "a.pdf": you viewed units 1-2.' in text
    assert '"b.pdf"' in text and '[Document "b.pdf": you viewed' not in text


def test_a_zoomed_region_expires_with_its_document() -> None:
    page = _ref(2, doc="a.pdf")
    crop = _ref(2, doc="a.pdf", region=(0, 0, 500, 500))
    reads = stamp_step(
        ledger_entries(_TURN, _doc_sha("a.pdf"), [FigureRead(2, (0, 0, 500, 500))], None), 4
    )
    reads = [*_reads("a.pdf", [2], step=3), *reads]
    assert _pixels(_tail([page, crop], expiry=_expiry(reads, step=6))) == [page, crop]
    out = _tail([page, crop], expiry=_expiry(reads, step=7))
    assert _pixels(out) == []
    assert "[A document (path not recorded): you viewed unit 2." in _block_text(out)


# ---------------------------------------------------------------------------
# T2-4 —— 上一轮取的页,本轮第一次调用就是占位
# ---------------------------------------------------------------------------


def test_t2_4_pages_from_an_earlier_turn_are_expired_at_once() -> None:
    viewed = [_ref(p, doc="a.pdf") for p in (1, 2)]
    earlier = _reads("a.pdf", [1, 2], step=9, run=uuid4())
    out = _tail(viewed, expiry=_expiry(earlier, step=0), documents=_documents("a.pdf"))
    assert _pixels(out) == []
    assert '[Document "a.pdf": you viewed units 1-2.' in _block_text(out)


def test_an_unstamped_or_junk_ledger_entry_does_not_keep_pages() -> None:
    """``figure_reads`` 是工具可写的通道:没有步数章 / 形状不对的条目一律不算「读过」。"""
    viewed = [_ref(1, doc="a.pdf")]
    unstamped = ledger_entries(_TURN, _doc_sha("a.pdf"), [FigureRead(1)], None)
    junk: list[object] = [
        {**unstamped[0], "step": "7"},
        {**unstamped[0], "step": True},
        42,
    ]
    for reads in (unstamped, junk):
        assert _pixels(_tail(viewed, expiry=_expiry(reads, step=7))) == []


# ---------------------------------------------------------------------------
# T2-5 / T2-6 —— 过期的页不进新鲜度核对,也不出「重取」行
# ---------------------------------------------------------------------------


async def test_t2_5_expired_pages_are_not_checked_for_freshness() -> None:
    ref = _ref(3, doc="d.pptx")
    reads = _reads("d.pptx", [3], step=1)
    store = RecordingWorkspaceStore(workspace_list_error=RuntimeError("would judge it"))
    checked = await figure_freshness(
        store,
        viewed=[ref],
        documents=_documents("d.pptx"),
        tenant_id=_TENANT,
        user_id=_USER,
        view=_VIEW,
        expiry=_expiry(reads, step=4),
    )
    assert checked == {}
    # 同一张图还没过期时它照常被核对(否则上面的断言什么也没验)。
    live = await figure_freshness(
        store,
        viewed=[ref],
        documents=_documents("d.pptx"),
        tenant_id=_TENANT,
        user_id=_USER,
        view=_VIEW,
        expiry=_expiry(reads, step=3),
    )
    assert live == {ref: "unknown"}


def test_t2_5_an_expired_page_never_gets_the_refetch_line() -> None:
    ref = _ref(3, doc="d.pptx")
    reads = _reads("d.pptx", [3], step=1)
    out = _tail(
        [ref],
        expiry=_expiry(reads, step=4),
        documents=_documents("d.pptx"),
        freshness={ref: "stale"},
    )
    text = _block_text(out)
    assert "重新调用 read_page" not in text
    assert "被改过或替换了" not in text
    assert '[Document "d.pptx": you viewed unit 3.' in text


def test_t2_6_an_old_session_without_a_path_gets_the_expired_line_only() -> None:
    ref = _ref(3)
    out = _tail([ref], expiry=_expiry([], step=0), freshness={ref: "unknown"})
    text = _block_text(out)
    assert text.count("[A document (path not recorded): you viewed unit 3.") == 1
    assert "核对不了" not in text


# ---------------------------------------------------------------------------
# T2-7 —— 占位与回执里不说 number(s)
# ---------------------------------------------------------------------------


def test_t2_7_the_wording_says_detail_not_number() -> None:
    texts: list[str] = []
    # B-268 退役行(窗口满了退出)
    texts.append(_block_text(_tail([_ref(p) for p in range(1, 9)], expiry=None)))
    # 过期占位
    texts.append(_block_text(_tail([_ref(1)], expiry=_expiry([], step=0))))
    ledger = ledger_entries(_TURN, "doc", [FigureRead(1), FigureRead(2)], 9)
    for view_steps in (0, _N):
        # pages=None:不触发超上限预告 —— 它不在 §4.2.4 点名的两处里,措辞照旧(见下一条测试)。
        for reads, pages in (([FigureRead(1)], None), ([FigureRead(3)], None)):
            for prior in ([], ledger):
                notice = read_notice(
                    prior,
                    run_id=_TURN,
                    doc_sha="doc",
                    reads=reads,
                    pages=pages,
                    view_steps=view_steps,
                )
                if notice is not None:
                    texts.append(notice)
        rereads = read_notice(
            ledger, run_id=_TURN, doc_sha="doc", reads=[FigureRead(1)], view_steps=view_steps
        )
        assert rereads is not None
        texts.append(rereads)
    assert len(texts) >= 6
    for text in texts:
        assert "number" not in text.lower(), text
    joined = " ".join(texts)
    assert "a specific detail" in joined
    assert "details noted in your thinking are not kept to the next step" in joined


def test_the_over_cap_heads_up_keeps_its_wording() -> None:
    """§4.2.4 只改退役行与回执两处;超上限预告不在范围内,原文不动(独立审查 MEDIUM-4)。"""
    for view_steps in (0, _N):
        notice = read_notice(
            [], run_id=_TURN, doc_sha="doc", reads=[FigureRead(1)], pages=9, view_steps=view_steps
        )
        assert notice is not None
        assert notice.startswith(
            "This document has 9 pages, more than the 6 pages that can be in view at once. "
            "Write the numbers into a workspace file as you read, then calculate from the file."
        )


# ---------------------------------------------------------------------------
# 回执:开着第 2 层时,每次取页成功都说「只留 N 步」
# ---------------------------------------------------------------------------


_RECEIPT = (
    "Note: your thinking is not kept to the next step, and these pages stay attached only for "
    "the next 3 steps. Write what you need from them into your reply, or into a workspace file "
    "with write_file, now, and rely on that afterwards. If something is unclear, write it down "
    "as uncertain with the candidate readings."
)


def test_with_the_window_on_every_read_gets_the_receipt() -> None:
    first = read_notice([], run_id=_TURN, doc_sha="doc", reads=[FigureRead(1)], view_steps=_N)
    assert first == _RECEIPT
    ledger = ledger_entries(_TURN, "doc", [FigureRead(1)], None)
    again = read_notice(ledger, run_id=_TURN, doc_sha="doc", reads=[FigureRead(1)], view_steps=_N)
    assert again is not None
    assert again.startswith("You already viewed unit 1 in this task (read #2).")
    assert again.endswith(_RECEIPT)
    assert "details noted in your thinking" not in again


def test_with_the_window_off_a_first_read_gets_no_receipt() -> None:
    """``figure_view_steps = 0`` = 今天:第一次读没有回执。"""
    assert read_notice([], run_id=_TURN, doc_sha="doc", reads=[FigureRead(1)]) is None
    assert read_notice([], run_id=_TURN, doc_sha="doc", reads=[FigureRead(1)], view_steps=0) is None


async def test_read_page_reads_the_window_from_its_context() -> None:
    tool = ReadPageTool(client=_Renders(), figure_delivery="inline")
    ctx = ToolContext(
        tenant_id=_TENANT,
        user_id=_USER,
        run_id=_TURN,
        agent_key=_AGENT_KEY,
        figure_view_steps=_N,
    )
    on = await tool.call({"path": "a.pdf", "units": [1]}, ctx=ctx)
    assert on.notice == _RECEIPT
    off = await tool.call(
        {"path": "a.pdf", "units": [1]},
        ctx=ToolContext(tenant_id=_TENANT, user_id=_USER, run_id=_TURN, agent_key=_AGENT_KEY),
    )
    assert off.notice is None
    # 看不了图的投递路径不许说「留在你眼前」。
    ask = ReadPageTool(client=_Renders(), figure_delivery="ask_image")
    assert (await ask.call({"path": "a.pdf", "units": [1]}, ctx=ctx)).notice is None


# ---------------------------------------------------------------------------
# T2-9 —— 开关为 0:与今天逐字节相同
# ---------------------------------------------------------------------------


def test_t2_9_a_zero_window_is_byte_identical_to_today() -> None:
    viewed = [_ref(p, doc="a.pdf") for p in range(1, 9)] + [_ref(1, doc="b.pdf")]
    old = _reads("a.pdf", [1], step=0, run=uuid4())
    documents = _documents("a.pdf", "b.pdf")
    today = _tail(viewed, expiry=None, documents=documents)
    for expiry in (_expiry(old, step=50, n=0), FigureExpiry(view_steps=0, step=99)):
        zero = _tail(viewed, expiry=expiry, documents=documents)
        assert [m.content for m in zero] == [m.content for m in today]
        assert [m.additional_kwargs for m in zero] == [m.additional_kwargs for m in today]


# ---------------------------------------------------------------------------
# 图级:tools 节点盖步数章,agent_node 按它退役
# ---------------------------------------------------------------------------


class _Noop:
    """一次什么也不改的工具调用 —— 让模型多走几步。"""

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(name="noop", description="stub")

    async def call(self, args: Mapping[str, Any], *, ctx: ToolContext) -> ToolResult:
        del args, ctx
        return ToolResult(content="ok")


def _noop_call(call_id: str) -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": "noop", "args": {}, "id": call_id}])


_GRAPH_RUN = uuid4()


async def _run(view_steps: int | None) -> tuple[_RecordingLLM, dict[str, Any]]:
    registry = ToolRegistry()
    registry.register(ReadPageTool(client=_EchoRenderRuntime(), figure_delivery="inline"))
    registry.register(_Noop())
    llm = _RecordingLLM(
        responses=[
            _read_page_call("a.pptx", [1, 2], "c0"),
            _noop_call("c1"),
            _noop_call("c2"),
            _noop_call("c3"),
        ]
    )
    kwargs: dict[str, Any] = {} if view_steps is None else {"figure_view_steps": view_steps}
    config: RunnableConfig = {
        "configurable": {
            "thread_id": str(uuid4()),
            "run_id": str(_GRAPH_RUN),
            "tenant_id": str(_TENANT),
            "user_id": str(_USER),
            "agent_key": _AGENT_KEY,
        }
    }
    async with make_checkpointer("memory") as cp:
        graph = GraphRunner(checkpointer=cp).compile(
            build_react_graph(
                llm_caller=llm, tool_registry=registry, supports_vision=True, **kwargs
            )
        )
        final = await graph.ainvoke(
            {"messages": [HumanMessage(content="看图")], "step_count": 0, "max_steps": 8},
            config=config,
        )
    return llm, final


async def test_the_graph_stamps_each_read_and_retires_the_pages_after_n_calls() -> None:
    llm, final = await _run(_N)
    assert {e.get("step") for e in final["figure_reads"]} == {1}
    # 第 0 次调用发起 read_page;第 1、2、3 次调用带像素,第 4 次换成占位。
    assert [len(_pixels(p)) for p in llm.seen_prompts[:5]] == [0, 2, 2, 2, 0]
    fifth = _block_text(llm.seen_prompts[4])
    assert '[Document "a.pptx": you viewed units 1-2.' in fifth
    tool_texts = [str(m.content) for m in final["messages"] if m.type == "tool"]
    assert any("only for the next 3 steps" in t for t in tool_texts)


async def test_t2_9_the_graph_default_and_zero_are_today() -> None:
    default_llm, default_final = await _run(None)
    zero_llm, zero_final = await _run(0)
    assert [len(_pixels(p)) for p in default_llm.seen_prompts[:5]] == [0, 2, 2, 2, 2]
    for a, b in zip(default_llm.seen_prompts, zero_llm.seen_prompts, strict=True):
        assert [m.content for m in _marked(a)] == [m.content for m in _marked(b)]
    tools = [
        [m.content for m in f["messages"] if m.type == "tool"] for f in (default_final, zero_final)
    ]
    assert tools[0] == tools[1]


class _ForgesStep:
    """往 ``figure_reads`` 里自己写 ``step`` 的工具 —— 通道工具可写,章必须由平台盖。"""

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(name="forge", description="stub")

    async def call(self, args: Mapping[str, Any], *, ctx: ToolContext) -> ToolResult:
        del args
        entry = {"run": str(ctx.run_id), "doc": "d", "unit": 1, "region": None, "pages": None}
        return ToolResult(content="ok", state_updates={"figure_reads": [{**entry, "step": 999}]})


async def test_a_step_a_tool_writes_itself_is_overwritten_by_the_platform() -> None:
    registry = ToolRegistry()
    registry.register(_ForgesStep())
    llm = _RecordingLLM(
        responses=[AIMessage(content="", tool_calls=[{"name": "forge", "args": {}, "id": "f"}])]
    )
    config: RunnableConfig = {
        "configurable": {
            "thread_id": str(uuid4()),
            "run_id": str(_GRAPH_RUN),
            "tenant_id": str(_TENANT),
            "user_id": str(_USER),
            "agent_key": _AGENT_KEY,
        }
    }
    async with make_checkpointer("memory") as cp:
        graph = GraphRunner(checkpointer=cp).compile(
            build_react_graph(llm_caller=llm, tool_registry=registry, supports_vision=True)
        )
        final = await graph.ainvoke(
            {"messages": [HumanMessage(content="x")], "step_count": 0, "max_steps": 4},
            config=config,
        )
    # 第 0 次调用发起工具,tools 节点里 step_count = 1;开关关着(默认)也盖章(理由见 §112)。
    assert [e["step"] for e in final["figure_reads"]] == [1]
