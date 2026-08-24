"""Stream CM-0 — project authoritative agent state into workspace files.

The DB (LangGraph checkpoint + ``memory_item`` rows) is the **source of
truth**; these Markdown files are a *projection* of it into the per-user
``/workspace`` so the agent can ``read_file`` them and a human can open them
to see — and steer — the run. State flows one direction at a time
(Mini-ADR CM-A2): this module renders + writes ``DB → file`` at a turn
boundary; the inverse ``file → DB`` controlled ingest is a separate path.

**Pure core (this module)** — rendering is pure functions and
:class:`WorkspaceProjector` writes through an injected
:class:`WorkspaceFileWriter` seam, so the only-if-changed digest gate and
the best-effort contract are unit-testable without a live sandbox. The real
writer (a ``write_file`` snippet in the J.15 warm sandbox — the only channel
with workspace-volume write access, see CM-A1) is wired separately.

**Best-effort (Mini-ADR CM-A8)** — a failing write is logged and never
propagates (projection must never break a run), and the returned digest does
*not* advance past a partial failure so the next turn retries.
"""

from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from expert_work.protocol import MemoryItem, Plan, PlanStep
from expert_work.protocol.plan import PlanStepStatus

logger = logging.getLogger(__name__)

#: Projected file names — since BUG-10 (方案 a) they live under a per-thread
#: directory, never at the workspace root: the workspace is **user**-scoped
#: and persistent (#989), so a root-level ``PLAN.md`` was cross-thread shared
#: state — every new thread ingested the previous thread's plan at run start.
PLAN_FILE = "PLAN.md"
TODO_FILE = "TODO.md"
MEMORY_FILE = "MEMORY.md"

#: Workspace dir holding one subdirectory per thread's projected state.
THREADS_DIR = "threads"


def thread_projection_prefix(thread_id: object) -> str:
    """The workspace-relative directory prefix (with trailing ``/``) for one
    thread's projected state files, e.g. ``threads/<thread_id>/``."""
    return f"{THREADS_DIR}/{thread_id}/"


def safe_thread_projection_prefix(thread_id: object) -> str | None:
    """:func:`thread_projection_prefix` with validation: ``None`` for a
    missing/empty id or one carrying path separators / traversal (the id is
    spliced into a sandbox path — a hostile value must never escape the
    thread's own directory). Callers skip projection/ingest on ``None``."""
    if thread_id is None:
        return None
    raw = str(thread_id)
    if not raw or "/" in raw or "\\" in raw or ".." in raw:
        return None
    return thread_projection_prefix(raw)


#: PLAN.md is the single canonical, round-trippable ingest source (Mini-ADR
#: CM-A5b): edit it to steer the agent. TODO.md / MEMORY.md are read-only
#: views — edits there are ignored.
_PLAN_NOTE = (
    "<!-- Edit this file to steer the agent: change the goal, add/remove/"
    "reorder steps, or flip a checkbox ([ ] todo / [~] in progress / [x] "
    "done). Changes are ingested at the next run start; the database stays "
    "the source of truth. -->"
)
_READONLY_NOTE = (
    "<!-- Read-only projection (the database is the source of truth). Edits "
    "here are NOT ingested — edit PLAN.md to steer the agent. -->"
)

#: Status ↔ checkbox marker. PLAN.md round-trips through these (render ↔ parse).
_STATUS_BOX: dict[PlanStepStatus, str] = {"pending": " ", "in_progress": "~", "completed": "x"}
_BOX_STATUS: dict[str, PlanStepStatus] = {" ": "pending", "~": "in_progress", "x": "completed"}

#: One PLAN.md step line, e.g. ``- [x] 1. write tests``. ``id`` is non-greedy
#: up to the first ``. `` so dotted ids (``1.2``) parse correctly.
_PLAN_STEP_RE = re.compile(r"^- \[(.)\] (\S+?)\.\s+(.*)$", re.MULTILINE)
_PLAN_GOAL_RE = re.compile(r"^\*\*Goal:\*\*\s*(.+)$", re.MULTILINE)


# ---------------------------------------------------------------------------
# Render functions (pure)
# ---------------------------------------------------------------------------


def render_plan_md(plan: Plan | None) -> str:
    """Render ``PLAN.md`` — the canonical, round-trippable plan view (goal +
    ordered steps with a status checkbox). :func:`parse_plan_md` is its exact
    inverse. ``None`` → empty string (no plan, e.g. a plain ReAct run)."""
    if plan is None:
        return ""
    lines = [_PLAN_NOTE, "", "# Plan", "", f"**Goal:** {plan.goal}", "", "## Steps", ""]
    lines.extend(
        f"- [{_STATUS_BOX.get(step.status, ' ')}] {step.id}. {step.description}"
        for step in plan.steps
    )
    return "\n".join(lines) + "\n"


def render_todo_md(plan: Plan | None) -> str:
    """Render ``TODO.md`` — a read-only flat checklist: ``completed`` → ``[x]``,
    ``in_progress`` → ``[ ]`` + marker, ``pending`` → ``[ ]``. ``None`` → ``""``."""
    if plan is None:
        return ""
    lines = [_READONLY_NOTE, "", "# TODO", ""]
    for step in plan.steps:
        box = "x" if step.status == "completed" else " "
        suffix = " — in progress" if step.status == "in_progress" else ""
        lines.append(f"- [{box}] {step.id}. {step.description}{suffix}")
    return "\n".join(lines) + "\n"


def render_memory_md(memories: Sequence[MemoryItem]) -> str:
    """Render ``MEMORY.md`` — a read-only digest of recalled / consolidated
    long-term memories. Empty sequence → empty string."""
    if not memories:
        return ""
    lines = [_READONLY_NOTE, "", "# Memory", ""]
    lines.extend(f"- ({item.kind}) {item.content}" for item in memories)
    return "\n".join(lines) + "\n"


def parse_plan_md(text: str) -> Plan | None:
    """Parse a (possibly human-edited) ``PLAN.md`` back into a :class:`Plan` —
    the exact inverse of :func:`render_plan_md`. Tolerant of surrounding prose
    and the HTML-comment header. Returns ``None`` when the text is not a valid
    plan (no ``**Goal:**`` line or no step lines) so the caller keeps the DB
    authoritative rather than ingesting garbage (Mini-ADR CM-A8)."""
    goal_match = _PLAN_GOAL_RE.search(text)
    if goal_match is None:
        return None
    steps: list[PlanStep] = []
    for box, step_id, description in _PLAN_STEP_RE.findall(text):
        status = _BOX_STATUS.get(box, "pending")
        steps.append(PlanStep(id=step_id, description=description.strip(), status=status))
    if not steps:
        return None
    return Plan(goal=goal_match.group(1).strip(), steps=tuple(steps))


# ---------------------------------------------------------------------------
# Projector
# ---------------------------------------------------------------------------


class WorkspaceFileWriter(Protocol):
    """Writes one workspace file. The real implementation rides the warm
    sandbox ``write_file`` snippet (CM-A1); tests inject a fake."""

    async def write(self, *, rel: str, content: str) -> None:
        """Create/overwrite workspace-relative ``rel`` with ``content``."""


@dataclass(frozen=True)
class ProjectionResult:
    """Outcome of one :meth:`WorkspaceProjector.project` call.

    ``digest`` is the new content digest to persist as the projection cursor
    when every intended write succeeded; on a partial failure it stays at the
    caller's ``last_digest`` so the next turn retries. ``None`` means there
    was nothing to project.
    """

    written: tuple[str, ...]
    skipped: bool
    digest: str | None


def _digest(items: Sequence[tuple[str, str]]) -> str | None:
    """Content digest over the (path, content) pairs about to be projected.
    ``None`` when there is nothing to project."""
    if not items:
        return None
    hasher = hashlib.sha256()
    for rel, content in items:
        hasher.update(rel.encode("utf-8"))
        hasher.update(b"\0")
        hasher.update(content.encode("utf-8"))
        hasher.update(b"\0")
    return hasher.hexdigest()


@dataclass(frozen=True)
class WorkspaceProjector:
    """Projects agent state into workspace files through a writer seam."""

    writer: WorkspaceFileWriter

    async def project(
        self,
        *,
        plan: Plan | None,
        memories: Sequence[MemoryItem],
        last_digest: str | None,
        prefix: str,
    ) -> ProjectionResult:
        """Render the projected files, and — only when their content changed
        since ``last_digest`` — write each through the seam. Best-effort: a
        failing write is logged, excluded from ``written``, and prevents the
        digest from advancing (so the next turn retries) but never raises.

        ``prefix`` is the thread-scoped directory (BUG-10 方案 a, see
        :func:`thread_projection_prefix`) — required, no root default, so a
        call site can never silently fall back to the shared workspace root.
        The digest covers the prefixed rel, so identical content under a new
        thread still projects."""
        items: list[tuple[str, str]] = []
        plan_md = render_plan_md(plan)
        if plan_md:
            items.append((prefix + PLAN_FILE, plan_md))
        todo_md = render_todo_md(plan)
        if todo_md:
            items.append((prefix + TODO_FILE, todo_md))
        memory_md = render_memory_md(memories)
        if memory_md:
            items.append((prefix + MEMORY_FILE, memory_md))

        digest = _digest(items)
        if digest == last_digest:
            logger.debug("workspace_projection.unchanged")
            return ProjectionResult(written=(), skipped=True, digest=digest)

        written: list[str] = []
        all_ok = True
        for rel, content in items:
            try:
                await self.writer.write(rel=rel, content=content)
            # Best-effort (CM-A8): projection must never break a run. ``Exception``
            # (not ``BaseException``) so ``asyncio.CancelledError`` still propagates.
            except Exception:
                all_ok = False
                logger.warning(
                    "workspace_projection.write_failed", extra={"rel": rel}, exc_info=True
                )
                continue
            written.append(rel)

        out_digest = digest if all_ok else last_digest
        logger.info(
            "workspace_projection.projected",
            extra={"written": written, "all_ok": all_ok},
        )
        return ProjectionResult(written=tuple(written), skipped=False, digest=out_digest)


# ---------------------------------------------------------------------------
# Ingester (file → DB candidate)
# ---------------------------------------------------------------------------


class WorkspaceFileReader(Protocol):
    """Reads one workspace file, returning its text or ``None`` when absent.
    The real implementation rides the warm-sandbox ``read_file`` snippet;
    tests inject a fake."""

    async def read(self, rel: str) -> str | None:
        """Return the text of workspace-relative ``rel``, or ``None`` if absent."""


@dataclass(frozen=True)
class WorkspaceIngester:
    """Reads the human-/agent-editable ``PLAN.md`` back into a candidate
    :class:`Plan` (Mini-ADR CM-A2, the ``file → DB`` direction). The caller
    validates + applies it under DB authority; this layer only reads + parses.
    """

    reader: WorkspaceFileReader

    async def ingest_plan(self, *, current: Plan | None, prefix: str) -> Plan | None:
        """Read + parse the thread's ``PLAN.md`` (under ``prefix`` — BUG-10
        方案 a: never the workspace root, which is user-scoped cross-thread
        state). Returns the parsed plan **only** when it differs from
        ``current`` (a genuine edit); returns ``None`` when the file is
        absent, unparseable, or unchanged. Never raises — projection / ingest
        must not break a run (Mini-ADR CM-A8); a read failure is logged and
        treated as "no edit"."""
        try:
            text = await self.reader.read(prefix + PLAN_FILE)
        except Exception:
            logger.warning("workspace_ingest.read_failed", exc_info=True)
            return None
        if not text:
            return None
        parsed = parse_plan_md(text)
        if parsed is None:
            logger.warning("workspace_ingest.parse_failed")
            return None
        if parsed == current:
            return None
        return parsed
