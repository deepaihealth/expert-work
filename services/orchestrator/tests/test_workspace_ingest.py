"""Stream CM-0 PR2b-i — ``PLAN.md`` parse / round-trip + WorkspaceIngester.

Pins the ``file → DB`` ingest core (no live sandbox): ``parse_plan_md`` is the
exact inverse of ``render_plan_md`` (an unedited projection round-trips to an
equal Plan), malformed input yields ``None`` (the caller keeps the DB
authoritative), and :class:`WorkspaceIngester` returns a candidate only on a
genuine edit.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from uuid import uuid4

import pytest

from expert_work.protocol import Plan, PlanStep
from orchestrator.context import (
    PlanIngest,
    WorkspaceIngester,
    parse_plan_md,
    plan_md_digest,
    render_plan_md,
)
from orchestrator.tools.file_ops import FileOpError, SandboxWorkspaceReader
from orchestrator.tools.registry import ToolContext
from orchestrator.tools.sandbox import RecordingSandboxRuntime, SandboxOutcome


@dataclass
class _StubReader:
    """In-memory ``WorkspaceFileReader``: returns ``contents[rel]`` or None."""

    contents: dict[str, str]
    raise_on_read: bool = False

    async def read(self, rel: str) -> str | None:
        if self.raise_on_read:
            msg = "sandbox read failed"
            raise RuntimeError(msg)
        return self.contents.get(rel)


def _plan() -> Plan:
    return Plan(
        goal="ship the feature",
        steps=(
            PlanStep(id="1", description="write tests", status="completed"),
            PlanStep(id="2", description="implement", status="in_progress"),
            PlanStep(id="3", description="review"),  # pending
        ),
    )


# ---------------------------------------------------------------------------
# parse_plan_md / round-trip
# ---------------------------------------------------------------------------


def test_render_parse_round_trips_exactly() -> None:
    plan = _plan()
    assert parse_plan_md(render_plan_md(plan)) == plan


def test_parse_preserves_status_checkboxes() -> None:
    parsed = parse_plan_md(render_plan_md(_plan()))
    assert parsed is not None
    assert [s.status for s in parsed.steps] == ["completed", "in_progress", "pending"]


def test_parse_handles_dotted_step_ids() -> None:
    plan = Plan(goal="g", steps=(PlanStep(id="1.2", description="nested step"),))
    parsed = parse_plan_md(render_plan_md(plan))
    assert parsed is not None
    assert parsed.steps[0].id == "1.2"
    assert parsed.steps[0].description == "nested step"


def test_parse_reflects_a_human_checkbox_edit() -> None:
    # Human flips step 3 from pending to done.
    edited = render_plan_md(_plan()).replace("- [ ] 3. review", "- [x] 3. review")
    parsed = parse_plan_md(edited)
    assert parsed is not None
    assert parsed.steps[2].status == "completed"


def test_parse_without_goal_returns_none() -> None:
    assert parse_plan_md("# Plan\n\n## Steps\n\n- [ ] 1. do it\n") is None


def test_parse_without_steps_returns_none() -> None:
    assert parse_plan_md("# Plan\n\n**Goal:** g\n\n## Steps\n") is None


def test_parse_empty_returns_none() -> None:
    assert parse_plan_md("") is None


# ---------------------------------------------------------------------------
# WorkspaceIngester
# ---------------------------------------------------------------------------


#: The digest of some earlier projection — "we wrote something else than what
#: the file holds now", i.e. the file was edited after our last write.
_OTHER_DIGEST = plan_md_digest("an earlier projection")


async def test_ingest_returns_none_when_unchanged() -> None:
    plan = _plan()
    text = render_plan_md(plan)
    reader = _StubReader(contents={"PLAN.md": text})
    # The file is exactly what we last projected → no edit → no-op.
    assert (
        await WorkspaceIngester(reader=reader).ingest_plan(
            current=plan, prefix="", last_written_digest=plan_md_digest(text)
        )
        is None
    )


async def test_ingest_returns_candidate_on_edit() -> None:
    plan = _plan()
    edited = render_plan_md(plan).replace("- [ ] 3. review", "- [x] 3. review")
    reader = _StubReader(contents={"PLAN.md": edited})
    edit = await WorkspaceIngester(reader=reader).ingest_plan(
        current=plan, prefix="", last_written_digest=plan_md_digest(render_plan_md(plan))
    )
    assert edit is not None and edit.plan is not None
    assert edit.plan.steps[2].status == "completed"
    assert edit.plan != plan
    # The edit's digest is handed back so the caller consumes it exactly once.
    assert edit.digest == plan_md_digest(edited)


async def test_ingest_returns_none_when_file_absent() -> None:
    reader = _StubReader(contents={})
    assert (
        await WorkspaceIngester(reader=reader).ingest_plan(
            current=_plan(), prefix="", last_written_digest=_OTHER_DIGEST
        )
        is None
    )


async def test_ingest_returns_none_on_unparseable_file() -> None:
    reader = _StubReader(contents={"PLAN.md": "garbage that is not a plan"})
    assert (
        await WorkspaceIngester(reader=reader).ingest_plan(
            current=_plan(), prefix="", last_written_digest=_OTHER_DIGEST
        )
        is None
    )


async def test_ingest_swallows_reader_failure() -> None:
    reader = _StubReader(contents={}, raise_on_read=True)
    # A read failure must not raise — projection/ingest never breaks a run.
    assert (
        await WorkspaceIngester(reader=reader).ingest_plan(
            current=_plan(), prefix="", last_written_digest=_OTHER_DIGEST
        )
        is None
    )


# ---------------------------------------------------------------------------
# PLAN.md 完整性 —— 只导入真被改过的文件(与上次投影写入的那份比,不与 state.plan 比)
# ---------------------------------------------------------------------------


def _planner_turn2_plan() -> Plan:
    """The plan the planner just made for turn 2's (different) question."""
    return Plan(
        goal="answer the follow-up",
        steps=(
            PlanStep(id="1", description="look up the new data", execution="delegate"),
            PlanStep(id="2", description="reply"),
        ),
    )


async def test_turn2_untouched_projection_does_not_clobber_the_planner_plan() -> None:
    """生产实证:第 2 轮起,PLAN.md 里是上一轮投影的计划,与 planner 本轮新做的计划必然
    不同 —— 旧规则(``parsed != current`` 就导入)每轮都拿旧计划盖掉新计划。文件就是
    我们上次写的那份(摘要相等)→ 不导入。"""
    turn1_text = render_plan_md(_plan())
    reader = _StubReader(contents={"PLAN.md": turn1_text})
    edit = await WorkspaceIngester(reader=reader).ingest_plan(
        current=_planner_turn2_plan(), prefix="", last_written_digest=plan_md_digest(turn1_text)
    )
    assert edit is None


async def test_genuine_edit_is_still_ingested_over_the_planner_plan() -> None:
    """文件与上次写入的那份不同 = 人 / agent 改过 → 照旧解析并作为候选返回。"""
    turn1_text = render_plan_md(_plan())
    edited = turn1_text.replace("- [ ] 3. review", "- [ ] 3. review twice")
    reader = _StubReader(contents={"PLAN.md": edited})
    edit = await WorkspaceIngester(reader=reader).ingest_plan(
        current=_planner_turn2_plan(), prefix="", last_written_digest=plan_md_digest(turn1_text)
    )
    assert edit is not None and edit.plan is not None
    assert edit.plan.steps[2].description == "review twice"


async def test_no_stored_digest_means_no_ingest() -> None:
    """上线前就投影过 / 从没投影过的会话:分不清人改与自己的旧投影,一律不导入。"""
    edited = render_plan_md(_plan()).replace("- [ ] 3. review", "- [ ] 3. review twice")
    reader = _StubReader(contents={"PLAN.md": edited})
    edit = await WorkspaceIngester(reader=reader).ingest_plan(
        current=_planner_turn2_plan(), prefix="", last_written_digest=None
    )
    assert edit is None


async def test_edit_equal_to_current_is_consumed_without_a_plan() -> None:
    """改过但解析出来就是当前计划:没东西可应用,但要把摘要交回去记下,
    否则下一轮 planner 换了新计划,这份旧文件又会被当成「改动」盖上去。"""
    plan = _plan()
    noisy = render_plan_md(plan) + "\n<!-- a note the human left -->\n"
    reader = _StubReader(contents={"PLAN.md": noisy})
    edit = await WorkspaceIngester(reader=reader).ingest_plan(
        current=plan, prefix="", last_written_digest=plan_md_digest(render_plan_md(plan))
    )
    assert edit == PlanIngest(plan=None, digest=plan_md_digest(noisy))


# ---------------------------------------------------------------------------
# SandboxWorkspaceReader (real reader over the warm-sandbox snippet)
# ---------------------------------------------------------------------------


def _reader_ctx() -> ToolContext:
    return ToolContext(tenant_id=uuid4(), run_id=uuid4(), user_id=uuid4())


def _read_envelope(content: str) -> SandboxOutcome:
    return SandboxOutcome(
        stdout=json.dumps(
            {"ok": True, "content": content, "content_hash": "h", "size": len(content)}
        ),
        stderr="",
        exit_code=0,
        timed_out=False,
    )


async def test_sandbox_reader_returns_content() -> None:
    client = RecordingSandboxRuntime(outcome=_read_envelope("# Plan\n"))
    reader = SandboxWorkspaceReader(client=client, ctx=_reader_ctx())
    assert await reader.read("PLAN.md") == "# Plan\n"
    assert client.execs and "PLAN.md" in client.execs[0][1]


async def test_sandbox_reader_returns_none_when_absent() -> None:
    client = RecordingSandboxRuntime(
        outcome=SandboxOutcome(
            stdout=json.dumps({"ok": False, "error": "not_found"}),
            stderr="",
            exit_code=0,
            timed_out=False,
        )
    )
    reader = SandboxWorkspaceReader(client=client, ctx=_reader_ctx())
    assert await reader.read("PLAN.md") is None


async def test_sandbox_reader_raises_on_io_error() -> None:
    client = RecordingSandboxRuntime(
        outcome=SandboxOutcome(
            stdout=json.dumps({"ok": False, "error": "io_error", "detail": "x"}),
            stderr="",
            exit_code=0,
            timed_out=False,
        )
    )
    reader = SandboxWorkspaceReader(client=client, ctx=_reader_ctx())
    with pytest.raises(FileOpError):
        await reader.read("PLAN.md")


# ---------------------------------------------------------------------------
# BUG-10 (方案 a) — thread-scoped ingest path
# ---------------------------------------------------------------------------


async def test_ingest_reads_only_the_thread_scoped_plan() -> None:
    plan = _plan()
    edited = render_plan_md(plan).replace("- [ ] 3. review", "- [x] 3. review")
    # A stale legacy root PLAN.md sits next to the thread's own file — only
    # the thread-scoped path may be read (BUG-10: user-scoped workspaces made
    # the root file cross-thread shared state).
    reader = _StubReader(
        contents={
            "PLAN.md": "**Goal:** stale cross-thread plan\n\n- [ ] 1. old step",
            "threads/t-1/PLAN.md": edited,
        }
    )
    edit = await WorkspaceIngester(reader=reader).ingest_plan(
        current=plan,
        prefix="threads/t-1/",
        last_written_digest=plan_md_digest(render_plan_md(plan)),
    )
    assert edit is not None and edit.plan is not None
    assert edit.plan.steps[2].status == "completed"


async def test_ingest_ignores_legacy_root_plan_for_a_fresh_thread() -> None:
    # Fresh thread: no thread-dir PLAN.md. The legacy root file (another
    # thread's plan) must NOT seed this thread's state.
    reader = _StubReader(contents={"PLAN.md": render_plan_md(_plan())})
    assert (
        await WorkspaceIngester(reader=reader).ingest_plan(
            current=None, prefix="threads/t-2/", last_written_digest=_OTHER_DIGEST
        )
        is None
    )
