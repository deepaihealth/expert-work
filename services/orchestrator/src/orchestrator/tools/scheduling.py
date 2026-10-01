"""Stream L.L6 — adaptive tool-call scheduling.

Walks a batch of LLM-emitted tool calls and assigns each to a *stage*:
a group of calls that may safely run concurrently. The ``tools`` node
executes one stage at a time with :func:`asyncio.gather`, capping
concurrency at :data:`MAX_TOOL_WORKERS`.

Conflict rules (Mini-ADR L-6, conservative defaults):

* Two ``is_read_only=True`` tools NEVER conflict — even if they touch
  the same path. Pure reads can race safely (no torn writes).
* A read + a write that BOTH declare ``path_args`` conflict iff the
  resolved path sets intersect. Disjoint paths run in parallel.
* A read + a write where at least one has empty ``path_args``
  conflict — the side without a declared path could touch anything,
  so we cannot prove safety.
* Two writes conflict on path overlap; both with empty ``path_args``
  also conflict (they write shared state — ``update_plan`` /
  ``subagent`` style).
* Stream TE-4 — an ``irreversible`` tool (``resolved_side_effect ==
  "irreversible"``: bash, destructive ops) conflicts with every other
  call, so it always runs alone in its own stage (forced serial).

The scheduler is greedy: each tool call lands in the earliest stage
that has no conflict with anyone already there. This keeps the total
stage count small while preserving the LLM's intended ordering of
state-mutating calls (``update_plan`` always lands alone, after
prior reads, before any subsequent reads).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from orchestrator.tools.registry import ToolSpec

#: Stream L.L6 — cap on per-stage parallel dispatches. Mini-ADR L-6:
#: matches Hermes ``_MAX_TOOL_WORKERS=8``. Larger stages run in
#: ``ceil(stage_size / MAX_TOOL_WORKERS)`` batches inside the same
#: stage to avoid event-loop pressure.
MAX_TOOL_WORKERS: int = 8


@dataclass(frozen=True)
class _ScheduledCall:
    """One tool call annotated with its original batch index and the
    paths the scheduler resolved from its args."""

    index: int
    name: str
    args: Mapping[str, Any]
    spec: ToolSpec | None
    paths: frozenset[str]


def _resolve_paths(spec: ToolSpec | None, args: Mapping[str, Any]) -> frozenset[str]:
    """Pull the values of ``spec.path_args`` out of ``args``.

    ``None`` spec (unknown tool — the dispatch wrapper will already
    raise) is treated as "no declared paths" so the conflict rules
    safely serialise it against other writes.
    """
    if spec is None or not spec.path_args:
        return frozenset()
    out: set[str] = set()
    for key in spec.path_args:
        value = args.get(key)
        if isinstance(value, str) and value:
            # Normalise so ``./report.md`` and ``report.md`` collide.
            # PurePath.as_posix() drops the redundant ``./`` prefix; we
            # avoid Path.resolve() because the agent may name files
            # the filesystem hasn't created yet.
            from pathlib import PurePath

            out.add(PurePath(value).as_posix())
    return frozenset(out)


def conflicts(a: _ScheduledCall, b: _ScheduledCall) -> bool:
    """Return True if calls ``a`` and ``b`` must not run concurrently.

    See module docstring for the full rule set.
    """
    # Stream TE-4 — an ``irreversible`` tool (bash, destructive ops) is
    # forced serial: it conflicts with EVERY other call so it always lands
    # alone in its own stage. Checked FIRST — before the read-read fast-path
    # and the parallel-safe / path rules — so nothing (not even a pure read,
    # a parallel-safe sibling, or a contradictory ``read_only + irreversible``
    # spec) can share a stage with it.
    a_irrev = a.spec is not None and a.spec.resolved_side_effect == "irreversible"
    b_irrev = b.spec is not None and b.spec.resolved_side_effect == "irreversible"
    if a_irrev or b_irrev:
        return True
    a_read = a.spec is not None and a.spec.is_read_only
    b_read = b.spec is not None and b.spec.is_read_only
    # Rule 1 — two reads never conflict.
    if a_read and b_read:
        return False
    # Mini-ADR J-40 (J.4-补强-2) — two ``is_parallel_safe`` calls don't
    # conflict either. The canonical case is SubAgentTool: each
    # delegation gets its own ``thread_id`` / checkpoint, so multiple
    # sibling sub-agents can run via ``asyncio.gather``. (They share the
    # per-(tenant, user) sandbox session and workspace; writes there
    # serialize on the user-level workspace lock.) This
    # is checked *before* the empty-paths rule below — a SubAgentTool
    # has no declared paths but is explicitly safe.
    a_par = a.spec is not None and a.spec.is_parallel_safe
    b_par = b.spec is not None and b.spec.is_parallel_safe
    if a_par and b_par:
        return False
    # Both empty path sets and at least one is a write → conservative
    # conflict (e.g., ``update_plan`` writes AgentState; reads of state
    # might be affected by a sibling write).
    if not a.paths or not b.paths:
        return True
    # Mixed read/write or both-write: conflict iff path sets intersect.
    return bool(a.paths & b.paths)


def plan_stages(
    tool_calls: Sequence[Mapping[str, Any]],
    specs_by_name: Mapping[str, ToolSpec],
) -> list[list[_ScheduledCall]]:
    """Group tool calls into stages of mutually-non-conflicting calls.

    Greedy assignment: each call lands in the earliest stage where it
    has no conflict with anyone already there. Within a stage, ordering
    is preserved by ``_ScheduledCall.index`` so the caller can collate
    results back into the LLM's original tool_call order.
    """
    stages: list[list[_ScheduledCall]] = []
    for index, raw in enumerate(tool_calls):
        name = str(raw.get("name", ""))
        args_obj = raw.get("args") or {}
        args: Mapping[str, Any] = args_obj if isinstance(args_obj, Mapping) else {}
        spec = specs_by_name.get(name)
        call = _ScheduledCall(
            index=index,
            name=name,
            args=args,
            spec=spec,
            paths=_resolve_paths(spec, args),
        )
        placed = False
        for stage in stages:
            if not any(conflicts(call, other) for other in stage):
                stage.append(call)
                placed = True
                break
        if not placed:
            stages.append([call])
    return stages
