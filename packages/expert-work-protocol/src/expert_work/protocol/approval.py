"""J.8 人在回路 / 审批 — DTOs.

Mini-ADR J-15 + J-24 (STREAM-J-DESIGN § 14). M0 = LangGraph
``interrupt()`` 审批节点 + 声明式 ``PolicySpec`` 门控 + agent 主动
``ask_for_approval`` 工具 + 24h 超时 fallback + audit trail.

These DTOs are the wire shape between orchestrator (the ``approval``
graph node writes :class:`ApprovalRequest` into ``AgentState``) and
control-plane (the resume endpoint takes :class:`ApprovalDecision`).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import datetime
from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

__all__ = [
    "CLARIFICATION_REASON_KINDS",
    "SAFETY_REASON_KINDS",
    "ApprovalDecision",
    "ApprovalKindClass",
    "ApprovalReasonKind",
    "ApprovalRecord",
    "ApprovalRequest",
    "ApprovalStatus",
    "approval_kind_class",
    "canonical_args_digest",
]


def canonical_args_digest(args: Mapping[str, object]) -> str:
    """Stable SHA-256 over a tool call's args — RT-6 Tier A binding (RT-ADR-19).

    Canonical form = sorted keys (all levels), tight separators, non-ASCII
    preserved, and a ``str`` fallback for anything not JSON-native (UUID /
    datetime / etc.). So the digest is deterministic across processes /
    replicas and independent of dict insertion order — two argument dicts
    that differ only in key order hash identically.

    Bound at approval mint (over ``proposed_args``) and re-computed before
    dispatch: a mismatch means the checkpointed ``tool_call`` drifted from
    what the human approved (checkpoint tamper / replay / bug), so the
    approval-resume path rejects it. Lives in ``expert-work-protocol`` so both the
    orchestrator (mint + verify) and control-plane (modify re-bind) import it
    without a cross-service dependency.
    """
    canonical = json.dumps(
        dict(args),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


#: Why a run is paused for approval — borrowed from deer-flow's
#: ``ask_clarification`` taxonomy (STREAM-J-DESIGN § 14.6). Admin UI
#: (H.3) filters / sorts the approval queue by this; audit analysis
#: groups "which kind of approval is most frequent" by it.
#:
#: ``policy_gate`` is fixed for the declarative path
#: (``PolicySpec.approval_required_tools``); the other four are what
#: the agent itself passes to the ``ask_for_approval`` builtin.
ApprovalReasonKind = Literal[
    "policy_gate",
    "missing_info",
    "ambiguous_requirement",
    "approach_choice",
    "risk_confirmation",
]

#: B-20 approval triage — the five reason kinds split into two classes with
#: different audiences and different timeout semantics. *Safety* rows are
#: platform / risk sign-offs (an approver role decides; timeout = plain
#: reject). *Clarification* rows are the agent asking a person a question
#: (missing input, ambiguous ask, fork between approaches; timeout = tell the
#: agent to proceed on its own conservative default). Single source for the
#: orchestrator mint (per-class timeout), the timeout sweep (per-class reject
#: reason) and the ``GET /v1/approvals`` ``kind_class`` filter — keep the two
#: sets an exact partition of :data:`ApprovalReasonKind`.
SAFETY_REASON_KINDS: frozenset[str] = frozenset({"policy_gate", "risk_confirmation"})
CLARIFICATION_REASON_KINDS: frozenset[str] = frozenset(
    {"missing_info", "ambiguous_requirement", "approach_choice"}
)

#: The two triage classes — wire value of the ``kind_class`` query filter.
ApprovalKindClass = Literal["safety", "clarification"]


def approval_kind_class(reason_kind: str) -> ApprovalKindClass:
    """Map a reason kind onto its triage class (unknown values → safety).

    Unknowns fail *closed*: a reason kind this build does not recognise is
    treated as a safety row (human sign-off semantics), never as a
    clarification that could auto-continue on timeout.
    """
    return "clarification" if reason_kind in CLARIFICATION_REASON_KINDS else "safety"


class ApprovalRequest(BaseModel):
    """A run paused for human approval — lives in ``AgentState.pending_approval``.

    Produced by the ``approval`` graph node (declarative gate) or the
    ``ask_for_approval`` builtin (agent-initiated). Persisted with the
    rest of ``AgentState`` via the checkpointer, so a resume after a
    process restart still sees the pending request.
    """

    model_config = ConfigDict(frozen=True)

    request_id: str = Field(
        min_length=1,
        description=(
            "Stable id — a hash of (thread_id, node, action_summary). "
            "Deterministic so a retried interrupt does not surface the "
            "same approval twice."
        ),
    )
    node: str = Field(
        min_length=1,
        description="Graph node that raised the interrupt (e.g. 'tools', 'ask_for_approval').",
    )
    reason_kind: ApprovalReasonKind
    action_summary: str = Field(
        min_length=1,
        description="Human-readable one-liner of what is awaiting approval.",
    )
    proposed_args: dict[str, object] = Field(
        default_factory=dict,
        description="The tool-call arguments the human is approving / may modify.",
    )
    requested_at: datetime
    timeout_at: datetime = Field(
        description=(
            "``requested_at + policies.approval_timeout_s``. The "
            "approval-timeout job auto-rejects requests past this."
        ),
    )
    binding_digest: str = Field(
        default="",
        description=(
            "RT-6 Tier A (RT-ADR-19) — ``canonical_args_digest(proposed_args)`` "
            "at mint. Re-computed before dispatch to detect checkpoint drift; a "
            "mismatch rejects the resume. Empty = unbound (legacy / pre-feature "
            "row), verification skipped."
        ),
    )


class ApprovalDecision(BaseModel):
    """A human's verdict on a pending approval — the resume API's body.

    ``modify`` replaces the tool-call arguments with ``modified_args``
    and resumes; ``modified_args`` is required in that case and
    ignored otherwise.
    """

    model_config = ConfigDict(frozen=True)

    decision: Literal["approve", "reject", "modify"]
    modified_args: dict[str, object] | None = Field(
        default=None,
        description="Replacement tool-call args — required iff decision == 'modify'.",
    )
    decided_by: str = Field(
        min_length=1,
        description="Subject id of the human (or 'system' for the timeout job).",
    )
    reason: str | None = Field(
        default=None,
        description="Optional free-text note; the timeout job sets reason='timeout'.",
    )

    @model_validator(mode="after")
    def _check_modified_args(self) -> ApprovalDecision:
        """``modified_args`` is required for — and only for — ``modify``."""
        if self.decision == "modify" and self.modified_args is None:
            msg = "decision='modify' requires modified_args"
            raise ValueError(msg)
        if self.decision != "modify" and self.modified_args is not None:
            msg = f"modified_args is only valid with decision='modify', not {self.decision!r}"
            raise ValueError(msg)
        return self


class ApprovalStatus(StrEnum):
    """Lifecycle status of an :class:`ApprovalRecord` row.

    ``PENDING`` is the only non-terminal state — the run is paused
    waiting on a human. The four terminal states correspond to the
    resume endpoint's verdict (``APPROVED`` / ``REJECTED`` /
    ``MODIFIED``) or the timeout job's auto-reject (``TIMEOUT``).
    """

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    MODIFIED = "modified"
    TIMEOUT = "timeout"


class ApprovalRecord(BaseModel):
    """One row of ``agent_approval`` — a paused run's persistent registry entry.

    expert_work's ``RunManager`` is in-memory only, so this table is the
    durable home of a paused run (Mini-ADR J-24). It is what the 24h
    timeout job scans, what ``GET`` reads to surface ``pending_approval``
    across a control-plane restart, and what ``POST .../resume`` looks
    up. Carries the :class:`ApprovalRequest` fields plus the run /
    tenant binding and the eventual verdict.
    """

    model_config = ConfigDict(frozen=True)

    id: UUID
    tenant_id: UUID
    user_id: UUID | None = None
    run_id: UUID
    thread_id: UUID
    request_id: str = Field(min_length=1)
    node: str = Field(min_length=1)
    reason_kind: ApprovalReasonKind
    action_summary: str = Field(min_length=1)
    proposed_args: dict[str, object] = Field(default_factory=dict)
    requested_at: datetime
    timeout_at: datetime
    # RT-6 Tier A (RT-ADR-19) — the mint-time args digest, persisted on the
    # ``agent_approval`` row (an integrity domain independent of the checkpoint,
    # so tampering the checkpoint's tool_call cannot also forge the digest). On
    # ``modify`` the resume path rewrites this to ``canonical_args_digest(
    # modified_args)`` atomically with the ``mark_decided`` CAS. Empty = unbound.
    binding_digest: str = ""
    status: ApprovalStatus = ApprovalStatus.PENDING
    decided_by: str | None = None
    decided_at: datetime | None = None
    modified_args: dict[str, object] | None = None
    # Stream 13.2 — written atomically with the decision (the ``mark_decided``
    # CAS). A retry / concurrent decide carrying the same ``idempotency_key``
    # reads back ``continuation_run_id`` and replays it instead of 409'ing, so
    # resume is deterministically recoverable. Both ``None`` for an
    # unconditioned (keyless) decide and for still-pending rows.
    idempotency_key: str | None = None
    continuation_run_id: UUID | None = None
