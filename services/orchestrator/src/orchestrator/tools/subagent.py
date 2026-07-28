"""Sub-agent delegation — Stream J.4 (agent-as-tool).

A manifest's ``spec.subagents`` block declares deployed agents the parent
may delegate to. The assembler (Stream J.4 PR4) wraps each entry into a
named :class:`SubAgentTool` so the parent's LLM sees delegation as an
ordinary tool call.

This module holds:

* :data:`MAX_SUBAGENT_DEPTH` — the hard recursion cap.
* :class:`ChildAgentBuilder` — the callback the control-plane injects so
  the orchestrator can resolve an ``agent_ref`` and build the referenced
  sub-agent (``AgentSpecStore`` lives in the control-plane; the
  orchestrator only ever holds pre-built ``BuiltAgent``\\s).
* :class:`SubAgentTool` — the :class:`~orchestrator.tools.registry.Tool`
  adapter: ``call()`` builds the child agent and hands it to the shared
  :func:`~orchestrator.tools._child_run.run_child_to_result` core, which
  runs it to completion and returns the child's final answer.

See [STREAM-J-DESIGN §11 / Mini-ADR J-12](../../../../../docs/streams/STREAM-J-DESIGN.md).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final, Protocol, runtime_checkable
from uuid import UUID

from expert_work.protocol import SubAgentSpec, parse_agent_ref
from expert_work.runtime.cancellation import RunCancelledError
from orchestrator.tools._budget import DELEGATIONS_GATED
from orchestrator.tools._child_run import run_child_to_result
from orchestrator.tools.registry import ToolBlockedError, ToolContext, ToolResult, ToolSpec
from orchestrator.trajectory import TrajectoryRecorder

if TYPE_CHECKING:
    from orchestrator.agent_factory import BuiltAgent

logger = logging.getLogger(__name__)

#: Hard cap on recursive sub-agent delegation depth. The top-level agent
#: builds at depth 0; each delegation step builds the child at parent
#: depth + 1. An agent built at this depth gets **no** delegation tools
#: (``SubAgentTool`` / ``spawn_worker``) registered — structural recursion
#: termination, so a cross-manifest cycle (A->B->A) can never run away
#: (Mini-ADR J-12). This replaces a token-budget guard: expert_work has no
#: runtime token budget, so cost is bounded structurally by depth times
#: each agent's ``max_iterations``.
MAX_SUBAGENT_DEPTH: Final = 3


@runtime_checkable
class ChildAgentBuilder(Protocol):
    """Resolves an ``agent_ref`` and builds the referenced sub-agent.

    Injected into :class:`~orchestrator.tools.ToolEnv` by the
    control-plane — the orchestrator cannot resolve an ``agent_ref``
    itself (the ``AgentSpecStore`` is control-plane-only). :class:`SubAgentTool`
    calls this inside ``call()`` to obtain the child ``BuiltAgent`` it
    delegates to.

    ``depth`` is the child's build-time recursion depth (parent depth
    + 1). The builder keys its agent cache on ``depth`` because the same
    manifest builds a *different* graph at different depths — an agent
    built at :data:`MAX_SUBAGENT_DEPTH` carries no further delegation tools.
    """

    async def __call__(
        self,
        *,
        tenant_id: UUID,
        name: str,
        version: str,
        depth: int,
        oauth_user_id: str | None = None,
    ) -> BuiltAgent:
        """Build the sub-agent ``name@version`` for ``tenant_id`` at ``depth``.

        ``oauth_user_id`` (MCP-OAUTH OA-3b-后续) is the caller's OAuth subject
        so the child inherits the same per-user OAuth pool; ``None`` = no OAuth
        pool injected (the child still gets the tenant pool)."""


@dataclass(frozen=True)
class SubAgentTool:
    """A :class:`~orchestrator.tools.registry.Tool` that delegates to a
    deployed sub-agent — Stream J.4.

    One instance per ``SubAgentSpec`` declared in the parent manifest's
    ``spec.subagents`` block. ``call()`` builds the referenced child agent
    via :attr:`builder`, then hands it to the shared child-run core which
    runs it to completion on a fresh thread seeded with the delegated
    ``task`` and returns the child's final answer as the tool result.
    """

    subagent: SubAgentSpec
    builder: ChildAgentBuilder
    #: The child's build-time recursion depth (parent depth + 1). Passed
    #: straight to :attr:`builder` so the depth cap is enforced there.
    child_depth: int
    #: Mini-ADR J-21 — when set, the child run's trajectory writes to its
    #: own L7 ObjectStore key. ``None`` is a valid deployment (no recorder
    #: configured) — the dispatch is a no-op.
    trajectory_recorder: TrajectoryRecorder | None = None

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name=self.subagent.name,
            description=self.subagent.description,
            parameters={
                "type": "object",
                "properties": {
                    "task": {
                        "type": "string",
                        "description": (
                            "The subtask to delegate, described in full. The "
                            "sub-agent runs fresh with only this text as its "
                            "instruction — it sees none of this conversation."
                        ),
                    },
                },
                "required": ["task"],
            },
            # Mini-ADR J-40 — sibling delegations share neither ``thread_id``
            # nor sandbox session, so plan_stages may schedule them in the
            # same stage and run them concurrently via ``asyncio.gather``.
            is_parallel_safe=True,
        )

    async def call(self, args: Mapping[str, Any], *, ctx: ToolContext) -> ToolResult:
        if ctx.tenant_id is None:
            msg = (
                f"sub-agent {self.subagent.name!r} cannot be delegated to without a tenant binding"
            )
            raise ToolBlockedError(msg)
        # Mini-ADR J-40 — refuse the delegation up front when the global
        # deadline has already expired.
        if ctx.deadline_at is not None and ctx.deadline_at - time.monotonic() <= 0:
            raise RunCancelledError(
                f"sub-agent {self.subagent.name!r} declined: global deadline already expired"
            )
        task = self._require_task(args)
        name, version = parse_agent_ref(self.subagent.agent_ref)

        # 二期 PR3(spec P4)— process-wide delegation concurrency gate.
        # Acquired before the child build so a saturated gate doesn't pay for
        # resolving/building a child agent it can't run yet.
        gate = ctx.delegation_gate
        if gate is not None:
            # PR3 加固 — the gate wait is bounded by whichever is smaller:
            # the gate's own default or the run's remaining deadline (Mini-
            # ADR J-40), so a near-expired run never waits out the gate's
            # full default before degrading to a soft-fail refusal.
            remaining = ctx.deadline_at - time.monotonic() if ctx.deadline_at is not None else None
            if not await gate.acquire(timeout_s=remaining):
                DELEGATIONS_GATED.labels(tool="subagent").inc()
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
                name=name,
                version=version,
                depth=self.child_depth,
                oauth_user_id=ctx.oauth_user_id,
            )
            return await run_child_to_result(
                child=child,
                task=task,
                ctx=ctx,
                child_depth=self.child_depth,
                label=self.subagent.name,
                agent_ref=self.subagent.agent_ref,
                trajectory_recorder=self.trajectory_recorder,
                trajectory_metadata={
                    "subagent_name": self.subagent.name,
                    "subagent_ref": self.subagent.agent_ref,
                    "child_depth": self.child_depth,
                },
            )
        finally:
            if gate is not None:
                await gate.release()

    def _require_task(self, args: Mapping[str, Any]) -> str:
        raw = args.get("task")
        if not isinstance(raw, str) or not raw.strip():
            msg = f"sub-agent {self.subagent.name!r} requires a non-empty 'task' string"
            raise ValueError(msg)
        return raw.strip()
