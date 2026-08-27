"""Dynamic worker spawning — 1.3 Orchestrator-Worker (``spawn_worker`` tool).

Where :class:`~orchestrator.tools.subagent.SubAgentTool` delegates to a
*statically declared* deployed agent (``spec.subagents``), this tool lets the
orchestrator **create an ephemeral worker at run time** from a generated
task + focus — the 2026-mainstream Orchestrator-Worker shape (Anthropic
multi-agent research / Claude Code Task tool / hermes ``delegate_task``).

The worker:

* is built from a *synthesized* spec (the control-plane's
  :class:`WorkerAgentBuilder` derives it from the parent — inheriting the
  parent's model + sandbox isolation, with a generated worker system
  prompt), **not** resolved from a deployed ``agent_ref``;
* runs to completion via the shared child-run core
  (:func:`~orchestrator.tools._child_run.run_child_to_result`), reusing the
  depth cap, cancellation/deadline propagation, L7 trajectory, and
  final-answer extraction;
* is discarded when done — the parent synthesizes its result.

Bounds are platform-global (see ``control_plane.settings``): a per-run spawn
count + a per-run concurrency semaphore live on :class:`WorkerSpawnBudget`,
created once per run and threaded through :class:`ToolContext`. When no
budget is wired (tests / eval), the worker still runs — depth, iteration cap,
deadline, and the per-tenant quota engine bound cost structurally.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable
from uuid import UUID

from expert_work.common.observability import expert_work_counter
from expert_work.runtime.cancellation import RunCancelledError
from orchestrator.tools._budget import DELEGATIONS_GATED, WorkerSpawnBudget
from orchestrator.tools._child_run import run_child_to_result
from orchestrator.tools.registry import ToolBlockedError, ToolContext, ToolResult, ToolSpec
from orchestrator.trajectory import TrajectoryRecorder

# Re-exported for back-compat: callers import WorkerSpawnBudget from here.
__all__ = [
    "SPAWN_WORKER_TOOL_NAME",
    "SpawnWorkerTool",
    "WorkerAgentBuilder",
    "WorkerBuildFn",
    "WorkerSpawnBudget",
]

if TYPE_CHECKING:
    from expert_work.protocol import AgentSpec
    from orchestrator.built_agent import BuiltAgent

_workers_spawned = expert_work_counter(
    "expert_work_dynamic_worker_spawned_total",
    "Dynamic workers spawned via the spawn_worker tool.",
)
_workers_blocked = expert_work_counter(
    "expert_work_dynamic_worker_blocked_total",
    "spawn_worker calls refused (per-run budget exhausted).",
)

#: The spawn_worker tool name handed to the parent LLM.
SPAWN_WORKER_TOOL_NAME = "spawn_worker"


@runtime_checkable
class WorkerAgentBuilder(Protocol):
    """Builds an ephemeral worker :class:`BuiltAgent` from a generated role.

    Injected into :class:`~orchestrator.tools.ToolEnv` by the control-plane
    (it owns the worker-spec synthesis + ``build_agent`` path). Unlike
    :class:`~orchestrator.tools.subagent.ChildAgentBuilder` there is no
    ``agent_ref`` — the worker spec is synthesized from the parent at
    ``depth``. ``role`` (the LLM's ``focus`` argument) shapes the worker's
    generated system prompt.
    """

    async def __call__(
        self,
        *,
        tenant_id: UUID,
        role: str | None,
        depth: int,
        oauth_user_id: str | None = None,
    ) -> BuiltAgent:
        """Build an ephemeral worker for ``tenant_id`` at ``depth``.

        ``oauth_user_id`` (MCP-OAUTH OA-3b-后续) lets the worker inherit the
        caller's per-user OAuth pool; ``None`` = tenant pool only."""


@runtime_checkable
class WorkerBuildFn(Protocol):
    """Control-plane callable that synthesizes + builds a worker from a parent.

    Carried on :class:`~orchestrator.tools.ToolEnv` (injected by the
    control-plane, which owns ``build_agent`` + the worker-spec synthesis).
    ``build_tool_registry`` binds the parent ``AgentSpec`` to produce the
    per-build :class:`WorkerAgentBuilder` the ``SpawnWorkerTool`` holds.
    ``None`` on the env means the feature is unwired (no ``spawn_worker``
    tool registered) — also how the platform ``enable_dynamic_workers=False``
    switch is expressed.
    """

    async def __call__(
        self,
        parent_spec: AgentSpec,
        *,
        tenant_id: UUID,
        role: str | None,
        depth: int,
        oauth_user_id: str | None = None,
        token_usage_kind: str = "conversation",  # noqa: S107 — usage label, not a secret
    ) -> BuiltAgent:
        """Synthesize a worker spec from ``parent_spec`` + ``role`` and build it.

        ``token_usage_kind`` (B-26) labels the worker build's LLM spend in
        token_usage; ``build_agent`` injects the parent's own kind (see
        ``_bind_delegation_usage_kind``)."""


@dataclass(frozen=True)
class SpawnWorkerTool:
    """The ``spawn_worker`` tool — 1.3 dynamic Orchestrator-Worker."""

    builder: WorkerAgentBuilder
    #: The worker's build-time recursion depth (parent depth + 1).
    child_depth: int
    trajectory_recorder: TrajectoryRecorder | None = None

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name=SPAWN_WORKER_TOOL_NAME,
            # 委派率增强(层 0)— the delegation judgment lives HERE, at the
            # decision site: domain-free *shape* criteria for when to
            # delegate, mirrored by the system-prompt scale rubric (层 2).
            description=(
                "Spawn an ephemeral worker sub-agent to complete a focused subtask "
                "in isolation, then return its result. Workers are lightweight, "
                "fast, and cheap; several can run in parallel; each starts with a "
                "fresh context — it sees none of this conversation, only 'task' — "
                "and is discarded when done. They excel at reading, extracting, "
                "and organizing work, keeping bulk material out of this "
                "conversation's context.\n"
                "USE this tool proactively — do not wait to be asked — whenever "
                "the work has one of these shapes, regardless of domain: "
                "(1) three or more similar, mutually independent sub-items "
                "(process each one, then aggregate); (2) several long materials "
                "must be read in full while only the conclusions matter here; "
                "(3) exploratory search — finding a small amount of relevant "
                "information in a large body of content.\n"
                "Do NOT use it for: small work a single step can finish; work "
                "involving writes or the final decision (those stay here); work "
                "so dependent on this conversation that the task cannot be "
                "written self-contained.\n"
                "Write 'task' fully self-contained: spell out identifiers, "
                "scope, and the expected output format, and never reference "
                "'above' or 'earlier'. Treat worker results as raw material — "
                "verify key conclusions here before relying on them."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "task": {
                        "type": "string",
                        "description": (
                            "The subtask to delegate, described in full and "
                            "self-contained (the worker sees nothing else)."
                        ),
                    },
                    "focus": {
                        "type": "string",
                        "description": (
                            "Optional role / specialty for the worker (e.g. "
                            "'code reviewer', 'researcher') — shapes its system prompt."
                        ),
                    },
                },
                "required": ["task"],
            },
            # Sibling workers share neither thread nor sandbox session, so the
            # scheduler may run them concurrently (bounded by the budget).
            is_parallel_safe=True,
        )

    async def call(self, args: Mapping[str, Any], *, ctx: ToolContext) -> ToolResult:
        if ctx.tenant_id is None:
            msg = "spawn_worker cannot run without a tenant binding"
            raise ToolBlockedError(msg)
        if ctx.deadline_at is not None and ctx.deadline_at - time.monotonic() <= 0:
            raise RunCancelledError("spawn_worker declined: global deadline already expired")

        task = self._require_task(args)
        focus = args.get("focus")
        role = focus.strip() if isinstance(focus, str) and focus.strip() else None

        budget = ctx.worker_spawn_budget
        if budget is not None and not budget.try_reserve():
            _workers_blocked.inc()
            return ToolResult(
                content=(
                    "[spawn_worker refused: this run reached its worker budget "
                    f"({budget.max_per_run}); complete the work with the results you have]"
                ),
                meta={"spawn_worker_blocked": True, "reason": "per_run_budget"},
            )

        # Minor — if the gate below refuses this delegation, the per-run
        # spawn budget slot reserved just above is intentionally NOT rolled
        # back: WorkerSpawnBudget exposes no reverse/refund API, a gate
        # refusal is transient (retry later), and the budget is a defense
        # against runaway spawning — erring toward spending it down is the
        # conservative direction here.
        # 二期 PR3(spec P4)— process-wide delegation concurrency gate, layered
        # on top of the per-run budget above. Acquired before the child build
        # so a saturated gate doesn't pay for building a worker it can't run.
        gate = ctx.delegation_gate
        if gate is not None:
            # PR3 加固 — the gate wait is bounded by whichever is smaller:
            # the gate's own default or the run's remaining deadline (Mini-
            # ADR J-40), so a near-expired run never waits out the gate's
            # full default before degrading to a soft-fail refusal.
            remaining = ctx.deadline_at - time.monotonic() if ctx.deadline_at is not None else None
            if not await gate.acquire(timeout_s=remaining):
                DELEGATIONS_GATED.labels(tool="spawn_worker").inc()
                return ToolResult(
                    content=(
                        "[delegation refused: platform-wide delegation concurrency is "
                        "saturated; retry later or complete the work without delegating]"
                    ),
                    meta={"delegation_gated": True, "reason": "global_gate_timeout"},
                )
        try:
            child = await self.builder(
                tenant_id=ctx.tenant_id,
                role=role,
                depth=self.child_depth,
                oauth_user_id=ctx.oauth_user_id,
            )
            _workers_spawned.inc()
            async with _maybe_concurrency(budget):
                return await run_child_to_result(
                    child=child,
                    task=task,
                    ctx=ctx,
                    child_depth=self.child_depth,
                    label=SPAWN_WORKER_TOOL_NAME,
                    agent_ref=f"dynamic:{role or 'general'}",
                    trajectory_recorder=self.trajectory_recorder,
                    trajectory_metadata={
                        "subagent_name": SPAWN_WORKER_TOOL_NAME,
                        "dynamic": True,
                        "role": role,
                        "child_depth": self.child_depth,
                    },
                    extra_meta={"dynamic": True, "role": role},
                )
        finally:
            if gate is not None:
                await gate.release()

    def _require_task(self, args: Mapping[str, Any]) -> str:
        raw = args.get("task")
        if not isinstance(raw, str) or not raw.strip():
            msg = "spawn_worker requires a non-empty 'task' string"
            raise ValueError(msg)
        return raw.strip()


@asynccontextmanager
async def _maybe_concurrency(budget: WorkerSpawnBudget | None) -> AsyncIterator[None]:
    if budget is None:
        yield
        return
    async with budget.concurrency():
        yield
