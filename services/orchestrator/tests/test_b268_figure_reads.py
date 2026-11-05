"""B-268 —— the per-run ``read_page`` ledger: re-read receipt, over-cap heads-up, breaker.

Measured on the test environment (E2, 11-03): with a 3-slot window a 4-page document was
read in strict rotation until the step cap, and pages that had left view were invented.
The window itself is pinned in ``test_figure_block_injection``; this file pins what
``read_page`` says and refuses, counted per run.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.runnables import RunnableConfig

from expert_work.persistence import RENDERED_FIGURE_PAGE_STEM, RENDERED_FIGURE_UNIT_PREFIX
from expert_work.runtime.checkpointer import make_checkpointer
from orchestrator import GraphRunner, ToolContext, ToolRegistry, build_react_graph
from orchestrator.graph_builder.builder import _build_tool_context
from orchestrator.state import _merge_figure_reads
from orchestrator.tools._guards import LOOP_TRIP_META_KEY
from orchestrator.tools.figure_reads import (
    FIGURE_CROPS_PER_DOCUMENT,
    FIGURE_PAGES_IN_VIEW,
    FIGURE_READS_PER_ITEM,
    Box,
    FigureRead,
    ReadPlan,
    ledger_entries,
    plan_reads,
    read_notice,
)
from orchestrator.tools.read_page import ReadPageTool, document_sha
from orchestrator.tools.registry import TOOL_ALLOWED_STATE_KEYS, TURN_ROOT_RUN_KEY
from orchestrator.tools.sandbox import EgressContext, RecordingSandboxRuntime, SandboxOutcome
from orchestrator.tools.skill_seed import sanitize_agent_key
from orchestrator.tools.workspace_paths import WorkspaceView

from .test_figure_block_injection import _RecordingLLM
from .test_read_page import _install_office_stubs, _run_render, _snippet_params

_RUN = uuid4()
_DOC = "doc-sha-a"
_TENANT = uuid4()
_USER = uuid4()
_AGENT_KEY = sanitize_agent_key("b268-probe")


def _ledger(*reads: FigureRead, run: UUID = _RUN, doc: str = _DOC, pages: int | None = 4) -> list:
    return ledger_entries(run, doc, reads, pages)


def _page(n: int) -> FigureRead:
    return FigureRead(unit=n)


def _plan(ledger: list, units: list[int], region: Box | None = None) -> ReadPlan:
    return plan_reads(ledger, run_id=_RUN, doc_sha=_DOC, units=units, region=region)


# ---------------------------------------------------------------------------
# breaker
# ---------------------------------------------------------------------------


def test_the_fourth_read_of_one_unit_is_refused_and_the_third_is_not() -> None:
    assert FIGURE_READS_PER_ITEM == 3
    assert _plan(_ledger(_page(1), _page(1)), [1]) == ReadPlan((1,))
    plan = _plan(_ledger(_page(1), _page(1), _page(1)), [1])
    assert plan.units == ()
    assert plan.refused is not None
    assert plan.refused.startswith("Not rendered: unit 1 - already read 3 times in this task.")
    assert "say in your answer what is missing" in plan.refused


def test_only_the_units_over_the_cap_are_refused() -> None:
    """Review M3 — the other requested units are still rendered; a note names the refused one."""
    plan = _plan(_ledger(_page(2), _page(2), _page(2)), [1, 2, 3])
    assert plan.units == (1, 3)
    assert plan.refused is not None and plan.refused.startswith("Not rendered: unit 2 - ")


def test_reads_from_another_turn_do_not_count() -> None:
    """Point 7 — a later turn may read the same unit normally again."""
    earlier = _ledger(_page(1), _page(1), _page(1), run=uuid4())
    assert _plan(earlier, [1]) == ReadPlan((1,))


def test_a_region_is_counted_apart_from_its_whole_page() -> None:
    box = (0, 0, 500, 500)
    crops = _ledger(*(FigureRead(unit=1, region=box) for _ in range(3)))
    assert _plan(crops, [1]) == ReadPlan((1,))
    plan = _plan(crops, [1], box)
    assert plan.units == ()
    assert plan.refused is not None
    assert "Not rendered: the region [0, 0, 500, 500] of unit 1 - already read 3" in plan.refused


def test_a_first_read_is_never_refused_by_the_document_cap() -> None:
    """Review round 2 M-A —— a 1-page docx whose figure map lists many figures: every figure's
    first read must render (``pages`` is the converted PDF's page count, not the units)."""
    ledger = _ledger(*[_page(u) for u in (3, 7, 12, 15, 20, 24)], pages=1)
    assert _plan(ledger, [30, 33]) == ReadPlan((30, 33))


def test_re_reads_stop_at_one_per_unit_read_plus_three() -> None:
    """Six units read once, nine re-reads used (6 + 3): the next re-read is refused, a new
    unit is not; crops do not count."""
    first = [_page(u) for u in range(1, 7)]
    again = [_page(u) for u in (1, 2, 3, 4, 5, 6, 1, 2, 3)]
    crops = [FigureRead(unit=1, region=(0, 0, 9 + i, 9)) for i in range(5)]
    ledger = _ledger(*first, *again, *crops, pages=2)
    plan = _plan(ledger, [4, 7])
    assert plan.units == (7,)
    assert plan.refused is not None
    assert plan.refused.startswith(
        "Not rendered: unit 4 - this document was already re-read 9 times in this task, the "
        "most allowed after reading 6 of its units (one re-read per unit read, plus 3)."
    )
    assert _plan(ledger[:-6], [4]) == ReadPlan((4,))  # 8 re-reads so far: one more is allowed
    assert _plan(ledger[:-6], [7, 4]) == ReadPlan((7, 4))  # a first read does not use the budget


def test_a_one_page_scan_can_be_zoomed_several_times() -> None:
    """Review M3 — whole page + 4 zooms must not end the run (B-266 region zoom)."""
    zooms = [FigureRead(unit=1, region=(0, 100 * i, 1000, 100 * i + 100)) for i in range(4)]
    ledger = _ledger(_page(1), *zooms, pages=1)
    assert _plan(ledger, [1], (0, 500, 1000, 600)) == ReadPlan((1,))
    assert _plan(ledger, [1]) == ReadPlan((1,))


def test_zoomed_regions_have_their_own_budget_per_document() -> None:
    assert FIGURE_CROPS_PER_DOCUMENT == 12
    zooms = [FigureRead(unit=1, region=(0, i, 1000, i + 50)) for i in range(12)]
    ledger = _ledger(*zooms, pages=1)
    plan = _plan(ledger, [1], (0, 900, 1000, 950))
    assert plan.units == ()
    assert plan.refused is not None
    assert "12 zoomed regions of this document were already rendered in this task" in (plan.refused)
    assert _plan(ledger[:11], [1], (0, 900, 1000, 950)) == ReadPlan((1,))
    # whole-page renders do not use up the zoom budget
    pages = _ledger(*[_page(p) for p in range(1, 13)], pages=None)
    assert _plan(pages, [1], (0, 900, 1000, 950)) == ReadPlan((1,))


def test_without_a_page_count_only_the_per_unit_rule_applies() -> None:
    ledger = _ledger(*[_page(p) for p in range(1, 10)], pages=None)
    assert _plan(ledger, [10]) == ReadPlan((10,))


def test_a_malformed_ledger_entry_is_ignored() -> None:
    """``figure_reads`` is a tool-writable channel: junk never trips the breaker."""
    junk: list[object] = [
        42,
        {"run": str(_RUN), "doc": _DOC, "unit": "1"},
        {"run": str(_RUN), "doc": _DOC, "unit": True},
    ] * 3
    assert _plan(junk, [1]) == ReadPlan((1,))


# ---------------------------------------------------------------------------
# receipt + heads-up
# ---------------------------------------------------------------------------


def test_a_re_read_gets_a_receipt_naming_the_read_count_and_what_is_in_view() -> None:
    ledger = _ledger(_page(1), _page(2), _page(3), _page(4), _page(1))
    notice = read_notice(ledger, run_id=_RUN, doc_sha=_DOC, reads=[_page(1)], pages=4)
    assert notice is not None
    assert "You already viewed unit 1 in this task (read #3)." in notice
    assert "Units 1-4 of this document are all in view now; no need to fetch them again." in (
        notice
    )
    assert "write_file" in notice
    assert "numbers noted in your thinking are not kept to the next step" in notice


def test_the_receipt_counts_by_unit_not_by_page() -> None:
    """Review L5 — docx units are figure entries: two entries on one page are two units."""
    ledger = _ledger(FigureRead(unit=7), FigureRead(unit=8))
    notice = read_notice(ledger, run_id=_RUN, doc_sha=_DOC, reads=[FigureRead(unit=8)])
    assert notice is not None
    assert "You already viewed unit 8 in this task (read #2)." in notice
    assert "Units 7-8 of this document" in notice


def test_a_first_read_gets_no_receipt() -> None:
    assert read_notice([], run_id=_RUN, doc_sha=_DOC, reads=[_page(1)], pages=4) is None


def test_in_view_units_are_capped_at_the_window() -> None:
    ledger = _ledger(*[_page(p) for p in range(1, 9)], pages=8)
    notice = read_notice(ledger, run_id=_RUN, doc_sha=_DOC, reads=[_page(8)], pages=8)
    assert FIGURE_PAGES_IN_VIEW == 6
    assert notice is not None and "Units 3-8 of this document" in notice


def test_a_document_over_the_cap_gets_a_heads_up_on_its_first_read_only() -> None:
    first = read_notice([], run_id=_RUN, doc_sha=_DOC, reads=[_page(1)], pages=9)
    assert first == (
        "This document has 9 pages, more than the 6 pages that can be in view at once. "
        "Write the numbers into a workspace file as you read, then calculate from the file."
    )
    later = read_notice(_ledger(_page(1), pages=9), run_id=_RUN, doc_sha=_DOC, reads=[_page(2)])
    assert later is None
    small = read_notice([], run_id=_RUN, doc_sha=_DOC, reads=[_page(1)], pages=6)
    assert small is None


# ---------------------------------------------------------------------------
# state channel
# ---------------------------------------------------------------------------


def test_the_ledger_keeps_only_the_current_runs_entries() -> None:
    old = _ledger(_page(1), run=uuid4())
    mine = _ledger(_page(2))
    merged = _merge_figure_reads(old + mine, _ledger(_page(3)))
    assert merged == mine + _ledger(_page(3))
    assert _merge_figure_reads(merged, []) == merged
    assert "figure_reads" in TOOL_ALLOWED_STATE_KEYS


# ---------------------------------------------------------------------------
# read_page wiring
# ---------------------------------------------------------------------------


class _Renders(RecordingSandboxRuntime):
    """Sandbox double: answers every render with success for the units the host asked for."""

    pages: int | None = None

    async def exec(
        self,
        *,
        sandbox_id: UUID,
        code: str,
        timeout_s: int | None,
        agent_key: str = "",
        run_id: UUID | None = None,
        has_skills: bool = False,
        egress: EgressContext | None = None,
        conv_id: UUID | None = None,
        workspace_thread_id: UUID | None = None,
    ) -> SandboxOutcome:
        await super().exec(
            sandbox_id=sandbox_id,
            code=code,
            timeout_s=timeout_s,
            agent_key=agent_key,
            run_id=run_id,
            has_skills=has_skills,
            egress=egress,
        )
        params = _snippet_params(code)
        rendered = [
            {
                "unit": u,
                "page": u,
                "rel": f"{params['out_rel']}/{'b' * 16}/{RENDERED_FIGURE_UNIT_PREFIX}{u}/"
                f"{RENDERED_FIGURE_PAGE_STEM}-{u:02d}.jpg",
                "bytes": 100,
            }
            for u in params["units"]
        ]
        envelope: dict[str, Any] = {"ok": True, "rendered": rendered}
        if self.pages is not None:
            envelope["pages"] = self.pages
        return SandboxOutcome(stdout=json.dumps(envelope), stderr="", exit_code=0, timed_out=False)


def _ctx(
    ledger: Sequence[Mapping[str, Any]] = (),
    *,
    run_id: UUID = _RUN,
    turn_root_run_id: UUID | None = None,
) -> ToolContext:
    return ToolContext(
        tenant_id=_TENANT,
        user_id=_USER,
        run_id=run_id,
        agent_key=_AGENT_KEY,
        figure_reads=tuple(ledger),
        turn_root_run_id=turn_root_run_id,
    )


def _doc_sha(path: str) -> str:
    sha = document_sha(path, view=WorkspaceView(agent_key=_AGENT_KEY))
    assert sha is not None
    return sha


async def test_read_page_records_each_render_in_the_ledger() -> None:
    runtime = _Renders()
    runtime.pages = 4
    tool = ReadPageTool(client=runtime, figure_delivery="inline")
    result = await tool.call({"path": "a.pdf", "units": [1, 2]}, ctx=_ctx())
    assert result.state_updates["figure_reads"] == ledger_entries(
        _RUN, _doc_sha("a.pdf"), [_page(1), _page(2)], 4
    )
    assert result.notice is None


async def test_read_page_refuses_the_fourth_read_without_rendering() -> None:
    runtime = _Renders()
    tool = ReadPageTool(client=runtime, figure_delivery="inline")
    ledger = ledger_entries(_RUN, _doc_sha("a.pdf"), [_page(1)] * 3, 4)
    result = await tool.call({"path": "a.pdf", "units": [1]}, ctx=_ctx(ledger))
    # Review L3 — the instruction is platform text (notice), outside the untrusted-data fence.
    assert result.content == "Nothing was rendered."
    assert result.notice is not None
    assert result.notice.startswith("Not rendered: unit 1 - already read 3 times")
    assert runtime.execs == []
    assert result.meta.get(LOOP_TRIP_META_KEY) is True
    assert "figure_reads" not in result.state_updates


async def test_read_page_attaches_the_receipt_to_a_re_read() -> None:
    tool = ReadPageTool(client=_Renders(), figure_delivery="inline")
    ledger = ledger_entries(_RUN, _doc_sha("a.pdf"), [_page(1), _page(2)], None)
    result = await tool.call({"path": "a.pdf", "units": [2]}, ctx=_ctx(ledger))
    assert result.notice is not None
    assert "You already viewed unit 2 in this task (read #2)." in result.notice


async def test_read_page_renders_the_units_under_the_cap_and_notes_the_others() -> None:
    runtime = _Renders()
    tool = ReadPageTool(client=runtime, figure_delivery="inline")
    ledger = ledger_entries(_RUN, _doc_sha("a.pdf"), [_page(2)] * 3, None)
    result = await tool.call({"path": "a.pdf", "units": [1, 2, 3]}, ctx=_ctx(ledger))
    assert _snippet_params(runtime.execs[0][1])["units"] == [1, 3]
    assert result.notice is not None and result.notice.startswith("Not rendered: unit 2 - ")
    assert LOOP_TRIP_META_KEY not in result.meta  # something was rendered: not a loop trip
    assert [e["unit"] for e in result.state_updates["figure_reads"]] == [1, 3]


async def test_an_approval_continuation_keeps_the_turns_counts() -> None:
    """Review L1 — the continuation segment has a new run_id; the ledger is keyed by the turn."""
    root, continuation = uuid4(), uuid4()
    runtime = _Renders()
    tool = ReadPageTool(client=runtime, figure_delivery="inline")
    ledger = ledger_entries(root, _doc_sha("a.pdf"), [_page(1)] * 3, None)
    ctx = _ctx(ledger, run_id=continuation, turn_root_run_id=root)
    refused = await tool.call({"path": "a.pdf", "units": [1]}, ctx=ctx)
    assert runtime.execs == []
    assert refused.meta.get(LOOP_TRIP_META_KEY) is True
    rendered = await tool.call({"path": "a.pdf", "units": [2]}, ctx=ctx)
    assert {e["run"] for e in rendered.state_updates["figure_reads"]} == {str(root)}


async def test_read_page_gives_the_heads_up_for_a_long_document() -> None:
    runtime = _Renders()
    runtime.pages = 12
    tool = ReadPageTool(client=runtime, figure_delivery="inline")
    result = await tool.call({"path": "a.pdf", "units": [1]}, ctx=_ctx())
    assert result.notice is not None and "This document has 12 pages" in result.notice


async def test_no_in_view_texts_when_pages_go_through_ask_image() -> None:
    """Gated: a model that cannot see the pages must not be told they are in view."""
    runtime = _Renders()
    runtime.pages = 12
    tool = ReadPageTool(client=runtime, figure_delivery="ask_image")
    ledger = ledger_entries(_RUN, _doc_sha("a.pdf"), [_page(1)], 12)
    result = await tool.call({"path": "a.pdf", "units": [1]}, ctx=_ctx(ledger))
    assert result.notice is None
    assert result.state_updates["figure_reads"]  # still counted for the breaker


# ---------------------------------------------------------------------------
# a refusal counts as a loop trip, so the run wraps up
# ---------------------------------------------------------------------------


def _read_call(call_id: str) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{"name": "read_page", "args": {"path": "a.pdf", "units": [1]}, "id": call_id}],
    )


async def test_two_refusals_end_the_run_with_a_wrap_up() -> None:
    runtime = _Renders()
    registry = ToolRegistry()
    registry.register(ReadPageTool(client=runtime, figure_delivery="inline"))
    llm = _RecordingLLM(responses=[_read_call(f"c{i}") for i in range(8)])
    config: RunnableConfig = {
        "configurable": {
            "thread_id": str(uuid4()),
            "run_id": str(_RUN),
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
            {"messages": [HumanMessage(content="read")], "step_count": 0, "max_steps": 20},
            config=config,
        )
    messages: list[BaseMessage] = final["messages"]
    refusals = [
        m
        for m in messages
        if isinstance(m, ToolMessage) and "Nothing was rendered" in str(m.content)
    ]
    assert len(runtime.execs) == 3
    assert len(refusals) == 2
    assert final["loop_trip_count"] == 2
    # Reads 1-3 rendered, 4-5 refused, then the tool-less wrap-up turn: 6 LLM calls, not 9.
    assert llm.calls == 6


async def test_parallel_refusals_in_one_batch_count_as_one_trip() -> None:
    """Two refused reads in one parallel batch are one loop trip, like a loop-detection
    stop (once per step) — the run gets one more step before the wrap-up."""
    runtime = _Renders()
    registry = ToolRegistry()
    registry.register(ReadPageTool(client=runtime, figure_delivery="inline"))
    pair = AIMessage(
        content="",
        tool_calls=[
            {"name": "read_page", "args": {"path": "a.pdf", "units": [1]}, "id": "p1"},
            {"name": "read_page", "args": {"path": "a.pdf", "units": [1]}, "id": "p2"},
        ],
    )
    responses = [_read_call("c0"), _read_call("c1"), _read_call("c2"), pair]
    llm = _RecordingLLM(responses=responses + [_read_call(f"d{i}") for i in range(6)])
    config: RunnableConfig = {
        "configurable": {
            "thread_id": str(uuid4()),
            "run_id": str(_RUN),
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
            {"messages": [HumanMessage(content="read")], "step_count": 0, "max_steps": 20},
            config=config,
        )
    refusals = [
        m
        for m in final["messages"]
        if isinstance(m, ToolMessage) and "Nothing was rendered" in str(m.content)
    ]
    assert len(runtime.execs) == 3
    # The pair (2 refusals, 1 trip), one more refused read (2nd trip), then the wrap-up.
    assert len(refusals) == 3
    assert final["loop_trip_count"] == 2
    assert llm.calls == 6


def test_the_tool_context_reads_the_turns_root_run() -> None:
    root = uuid4()
    ctx = _build_tool_context({"configurable": {TURN_ROOT_RUN_KEY: str(root)}})
    assert ctx.turn_root_run_id == root
    assert _build_tool_context({"configurable": {TURN_ROOT_RUN_KEY: "x"}}).turn_root_run_id is None


@pytest.mark.parametrize("delivery", ["inline", "ask_image"])
async def test_the_breaker_applies_whatever_the_delivery(delivery: Any) -> None:
    runtime = _Renders()
    tool = ReadPageTool(client=runtime, figure_delivery=delivery)
    ledger = ledger_entries(_RUN, _doc_sha("a.pdf"), [_page(1)] * 3, None)
    result = await tool.call({"path": "a.pdf", "units": [1]}, ctx=_ctx(ledger))
    assert runtime.execs == []
    assert result.meta.get(LOOP_TRIP_META_KEY) is True


# ---------------------------------------------------------------------------
# the render snippet reports the page count (pdfinfo), best effort
# ---------------------------------------------------------------------------


def test_the_render_snippet_reports_the_page_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_office_stubs(
        tmp_path, monkeypatch, pdfinfo_body='print("Title: x\\nPages:          7\\n")\n'
    )
    (tmp_path / "a.pdf").write_text("pdf source")
    env = _run_render(tmp_path, "a.pdf", units=[1], out_rel=".tool_results/r1/figures/aaa")
    assert env["ok"] is True
    assert env["pages"] == 7


def test_without_pdfinfo_the_envelope_has_no_page_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_office_stubs(tmp_path, monkeypatch)  # default pdfinfo stub fails
    (tmp_path / "a.pdf").write_text("pdf source")
    env = _run_render(tmp_path, "a.pdf", units=[1], out_rel=".tool_results/r1/figures/aaa")
    assert env["ok"] is True
    assert "pages" not in env
