"""Stream CM-0 — :mod:`orchestrator.context.workspace_projection` unit tests.

Pins the pure projection core (no live sandbox): plan/todo/memory rendering,
the ``status`` checkbox mapping, the only-if-changed digest gate, and the
best-effort write contract (a failing writer is logged and never propagates,
and the digest does not advance so the next turn retries).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from uuid import uuid4

import pytest

from expert_work.protocol import MemoryItem, Plan, PlanStep
from orchestrator.context import (
    WorkspaceProjector,
    render_memory_md,
    render_plan_md,
    render_todo_md,
)
from orchestrator.tools.file_ops import FileOpError, SandboxWorkspaceWriter
from orchestrator.tools.registry import ToolContext
from orchestrator.tools.sandbox import RecordingSandboxRuntime, SandboxOutcome

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


@dataclass
class _RecordingWriter:
    """Records every ``write`` call; optionally raises for chosen paths."""

    writes: dict[str, str] = field(default_factory=dict)
    fail_on: frozenset[str] = frozenset()

    async def write(self, *, rel: str, content: str) -> None:
        if rel in self.fail_on:
            msg = f"sandbox write failed: {rel}"
            raise RuntimeError(msg)
        self.writes[rel] = content


def _plan() -> Plan:
    return Plan(
        goal="ship the feature",
        steps=(
            PlanStep(id="1", description="write tests", status="completed"),
            PlanStep(id="2", description="implement", status="in_progress"),
            PlanStep(id="3", description="review"),  # status defaults to pending
        ),
    )


def _memories() -> list[MemoryItem]:
    return [
        MemoryItem(
            id=uuid4(),
            tenant_id=uuid4(),
            user_id=uuid4(),
            kind="fact",
            content="user prefers Go",
            embedding=(0.0,),
        ),
        MemoryItem(
            id=uuid4(),
            tenant_id=uuid4(),
            user_id=uuid4(),
            kind="episodic",
            content="last run refactored auth",
            embedding=(0.0,),
        ),
    ]


# ---------------------------------------------------------------------------
# Render functions (pure)
# ---------------------------------------------------------------------------


def test_render_plan_md_has_goal_and_steps() -> None:
    md = render_plan_md(_plan())
    assert "ship the feature" in md
    assert "write tests" in md
    assert "implement" in md
    assert "review" in md


def test_render_plan_md_none_is_empty() -> None:
    assert render_plan_md(None) == ""


def test_render_todo_md_checkbox_per_status() -> None:
    md = render_todo_md(_plan())
    lines = md.splitlines()
    done = next(line for line in lines if "write tests" in line)
    in_prog = next(line for line in lines if "implement" in line)
    pending = next(line for line in lines if "review" in line)
    assert "[x]" in done
    assert "[ ]" in in_prog  # in_progress renders unchecked...
    assert "in progress" in in_prog.lower()  # ...with an explicit marker
    assert "[ ]" in pending
    assert "in progress" not in pending.lower()


def test_render_todo_md_none_is_empty() -> None:
    assert render_todo_md(None) == ""


def test_render_memory_md_lists_kind_and_content() -> None:
    md = render_memory_md(_memories())
    assert "user prefers Go" in md
    assert "last run refactored auth" in md
    assert "fact" in md
    assert "episodic" in md


def test_render_memory_md_empty_is_empty() -> None:
    assert render_memory_md([]) == ""


# ---------------------------------------------------------------------------
# WorkspaceProjector
# ---------------------------------------------------------------------------


async def test_project_writes_all_three_files_when_present() -> None:
    writer = _RecordingWriter()
    result = await WorkspaceProjector(writer=writer).project(
        plan=_plan(), memories=_memories(), last_digest=None
    )
    assert set(writer.writes) == {"PLAN.md", "TODO.md", "MEMORY.md"}
    assert set(result.written) == {"PLAN.md", "TODO.md", "MEMORY.md"}
    assert result.skipped is False
    assert result.digest  # non-empty content digest


async def test_project_skips_when_unchanged() -> None:
    writer = _RecordingWriter()
    proj = WorkspaceProjector(writer=writer)
    first = await proj.project(plan=_plan(), memories=_memories(), last_digest=None)

    writer2 = _RecordingWriter()
    second = await WorkspaceProjector(writer=writer2).project(
        plan=_plan(), memories=_memories(), last_digest=first.digest
    )
    assert second.skipped is True
    assert second.written == ()
    assert writer2.writes == {}  # no sandbox round-trip when nothing changed
    assert second.digest == first.digest


async def test_project_omits_plan_files_when_plan_none() -> None:
    writer = _RecordingWriter()
    result = await WorkspaceProjector(writer=writer).project(
        plan=None, memories=_memories(), last_digest=None
    )
    assert set(writer.writes) == {"MEMORY.md"}
    assert set(result.written) == {"MEMORY.md"}


async def test_project_writes_nothing_when_empty() -> None:
    writer = _RecordingWriter()
    result = await WorkspaceProjector(writer=writer).project(
        plan=None, memories=[], last_digest=None
    )
    assert writer.writes == {}
    assert result.written == ()


async def test_project_best_effort_swallows_writer_failure() -> None:
    writer = _RecordingWriter(fail_on=frozenset({"TODO.md"}))
    # Must not raise even though one file write fails.
    result = await WorkspaceProjector(writer=writer).project(
        plan=_plan(), memories=_memories(), last_digest=None
    )
    # The two healthy files still landed; the failed one is excluded.
    assert "PLAN.md" in writer.writes
    assert "MEMORY.md" in writer.writes
    assert "TODO.md" not in writer.writes
    assert "TODO.md" not in result.written
    # Digest does NOT advance past a partial failure → next turn retries.
    assert result.digest is None


# ---------------------------------------------------------------------------
# SandboxWorkspaceWriter (real writer over the warm-sandbox snippet)
# ---------------------------------------------------------------------------


def _ctx() -> ToolContext:
    return ToolContext(tenant_id=uuid4(), run_id=uuid4(), user_id=uuid4())


def _ok_envelope(path: str) -> SandboxOutcome:
    return SandboxOutcome(
        stdout=json.dumps({"ok": True, "content_hash": "h", "size": 3, "path": path}),
        stderr="",
        exit_code=0,
        timed_out=False,
    )


async def test_sandbox_writer_writes_through_warm_sandbox() -> None:
    client = RecordingSandboxRuntime(outcome=_ok_envelope("PLAN.md"))
    writer = SandboxWorkspaceWriter(client=client, ctx=_ctx())
    await writer.write(rel="PLAN.md", content="abc")
    assert len(client.execs) == 1
    code = client.execs[0][1]
    assert "PLAN.md" in code  # path carried into the write snippet
    assert "abc" in code  # content carried via the json params
    assert client.released  # sandbox released after the write


async def test_sandbox_writer_raises_on_sandbox_failure() -> None:
    client = RecordingSandboxRuntime(
        outcome=SandboxOutcome(
            stdout=json.dumps({"ok": False, "error": "io_error", "detail": "disk full"}),
            stderr="",
            exit_code=0,
            timed_out=False,
        )
    )
    writer = SandboxWorkspaceWriter(client=client, ctx=_ctx())
    # The projector swallows this best-effort; the writer itself raises.
    with pytest.raises(FileOpError):
        await writer.write(rel="PLAN.md", content="abc")
