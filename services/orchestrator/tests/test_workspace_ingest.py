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
    WorkspaceIngester,
    parse_plan_md,
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


async def test_ingest_returns_none_when_unchanged() -> None:
    plan = _plan()
    reader = _StubReader(contents={"PLAN.md": render_plan_md(plan)})
    # The projected file matches the DB plan → no edit → no-op.
    assert await WorkspaceIngester(reader=reader).ingest_plan(current=plan) is None


async def test_ingest_returns_candidate_on_edit() -> None:
    plan = _plan()
    edited = render_plan_md(plan).replace("- [ ] 3. review", "- [x] 3. review")
    reader = _StubReader(contents={"PLAN.md": edited})
    candidate = await WorkspaceIngester(reader=reader).ingest_plan(current=plan)
    assert candidate is not None
    assert candidate.steps[2].status == "completed"
    assert candidate != plan


async def test_ingest_returns_none_when_file_absent() -> None:
    reader = _StubReader(contents={})
    assert await WorkspaceIngester(reader=reader).ingest_plan(current=_plan()) is None


async def test_ingest_returns_none_on_unparseable_file() -> None:
    reader = _StubReader(contents={"PLAN.md": "garbage that is not a plan"})
    assert await WorkspaceIngester(reader=reader).ingest_plan(current=_plan()) is None


async def test_ingest_swallows_reader_failure() -> None:
    reader = _StubReader(contents={}, raise_on_read=True)
    # A read failure must not raise — projection/ingest never breaks a run.
    assert await WorkspaceIngester(reader=reader).ingest_plan(current=_plan()) is None


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
