"""Stream CM-0 PR2b — workspace state ingest node (``file → DB``).

The entry-chain counterpart of the tools-node projection (PR2a): once per run
(START → … → ``workspace_ingest`` → agent, so it fires at run start / resume,
Mini-ADR CM-A4), read the human-/agent-editable ``PLAN.md`` back from the
workspace and — when it genuinely changed and passes a strict prompt-injection
scan — apply it to ``AgentState.plan``. The DB stays authoritative (Mini-ADR
CM-A2/A8): a missing / unparseable / unchanged / injection-bearing file is a
no-op that leaves ``state.plan`` untouched.
"""

from __future__ import annotations

import logging
from typing import Any

from langchain_core.runnables import RunnableConfig

from expert_work.common.observability import (
    ExpertWorkComponent,
    expert_work_counter,
    expert_work_span,
)
from expert_work.common.threat_patterns import scan_for_threats
from expert_work.protocol import AuditAction, AuditEntry, AuditResult, Plan
from expert_work.runtime.audit.logger import AuditLogger
from orchestrator.context import WorkspaceIngester
from orchestrator.graph_builder._config import (
    audit_logger_from_config,
    cancellation_token,
    configurable_uuid,
)
from orchestrator.graph_builder.memory import MemoryNode
from orchestrator.state import AgentState
from orchestrator.tools.file_ops import SandboxWorkspaceReader
from orchestrator.tools.registry import ToolContext
from orchestrator.tools.sandbox import SupervisorClient

logger = logging.getLogger(__name__)

#: Stream CM-0 — file→DB ingests at run start, by outcome
#: (applied = plan replaced / rejected = injection scan blocked).
_cm_ingest_total = expert_work_counter(
    "expert_work_cm_ingest_total",
    "Workspace state ingests at run start (Stream CM-0).",
    ("outcome",),
)


def _plan_scan_text(plan: Plan) -> str:
    """The plan text that would reach the model (goal + step descriptions) —
    what the strict injection scan must vet before ingest."""
    return plan.goal + "\n" + "\n".join(step.description for step in plan.steps)


async def _emit_state_ingested_audit(
    audit_logger: AuditLogger | None, ctx: ToolContext, *, steps: int
) -> None:
    """Audit one ``file→DB`` ingest (Stream CM-0). Best-effort. ``resource_type``
    reuses ``user_workspace`` (Mini-ADR CM-A6)."""
    if audit_logger is None or ctx.tenant_id is None:
        return
    try:
        details: dict[str, Any] = {"file": "PLAN.md", "steps": steps}
        if ctx.run_id is not None:
            details["run_id"] = str(ctx.run_id)
        await audit_logger.write(
            AuditEntry(
                tenant_id=ctx.tenant_id,
                actor_type="agent",
                actor_id=str(ctx.run_id) if ctx.run_id is not None else "agent",
                action=AuditAction.STATE_INGESTED,
                resource_type="user_workspace",
                result=AuditResult.SUCCESS,
                details=details,
            )
        )
    except Exception:
        logger.exception("workspace_ingest.audit_failed")


def make_workspace_ingest_node(*, client: SupervisorClient) -> MemoryNode:
    """Build the entry-chain ingest node. Reads ``PLAN.md`` via the warm
    sandbox, parses it, and returns ``{"plan": ...}`` only on a genuine,
    injection-clean edit; otherwise ``{}`` (DB authoritative).

    The node's *existence* stays gated by the manifest ``persistent_workspace``
    flag in ``agent_factory`` (CM-0 plan-projection opt-in); the workspace mount
    itself follows ``ctx.user_id`` (durability automatic)."""

    async def workspace_ingest_node(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
        token = cancellation_token(config)
        token.raise_if_cancelled()
        tenant_id = configurable_uuid(config, "tenant_id")
        if tenant_id is None:
            return {}
        with expert_work_span(ExpertWorkComponent.ORCHESTRATOR, "workspace_ingest"):
            ctx = ToolContext(
                tenant_id=tenant_id,
                run_id=configurable_uuid(config, "run_id"),
                user_id=configurable_uuid(config, "user_id"),
                cancellation_token=token,
            )
            reader = SandboxWorkspaceReader(client=client, ctx=ctx)
            current = state.get("plan")
            try:
                candidate = await token.run_cancellable(
                    WorkspaceIngester(reader=reader).ingest_plan(current=current)
                )
            except Exception:
                logger.warning("workspace_ingest.failed", exc_info=True)
                return {}
            if candidate is None:
                return {}
            # Strict injection scan on the human-edited content before it can land
            # in the plan the model executes against (Mini-ADR CM-A8). On a hit,
            # discard the edit — DB stays authoritative — and keep the file for
            # the user to review.
            if scan_for_threats(_plan_scan_text(candidate), scope="strict"):
                logger.warning("workspace_ingest.blocked_injection")
                _cm_ingest_total.labels(outcome="rejected").inc()
                return {}
            _cm_ingest_total.labels(outcome="applied").inc()
            await _emit_state_ingested_audit(
                audit_logger_from_config(config), ctx, steps=len(candidate.steps)
            )
            return {"plan": candidate}

    return workspace_ingest_node
