"""Unit tests for J.8 approval DTOs — Stream J.8 (Mini-ADR J-24)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from expert_work.protocol import (
    CLARIFICATION_REASON_KINDS,
    SAFETY_REASON_KINDS,
    ApprovalDecision,
    ApprovalReasonKind,
    ApprovalRequest,
    approval_kind_class,
    canonical_args_digest,
)


def _now() -> datetime:
    return datetime(2026, 5, 22, 12, 0, 0, tzinfo=UTC)


def test_approval_request_round_trips() -> None:
    req = ApprovalRequest(
        request_id="abc123",
        node="tools",
        reason_kind="policy_gate",
        action_summary="send_email to ops@example.com",
        proposed_args={"to": "ops@example.com", "body": "hi"},
        requested_at=_now(),
        timeout_at=_now() + timedelta(hours=24),
    )
    dumped = req.model_dump()
    restored = ApprovalRequest.model_validate(dumped)
    assert restored == req
    assert restored.reason_kind == "policy_gate"


def test_approval_request_is_frozen() -> None:
    req = ApprovalRequest(
        request_id="x",
        node="tools",
        reason_kind="risk_confirmation",
        action_summary="rm -rf /tmp/work",
        requested_at=_now(),
        timeout_at=_now() + timedelta(hours=24),
    )
    with pytest.raises(ValidationError):
        req.action_summary = "changed"


def test_approval_request_rejects_unknown_reason_kind() -> None:
    with pytest.raises(ValidationError):
        ApprovalRequest(
            request_id="x",
            node="tools",
            reason_kind="not_a_kind",  # type: ignore[arg-type]
            action_summary="x",
            requested_at=_now(),
            timeout_at=_now(),
        )


def test_approval_request_rejects_empty_action_summary() -> None:
    with pytest.raises(ValidationError):
        ApprovalRequest(
            request_id="x",
            node="tools",
            reason_kind="missing_info",
            action_summary="",
            requested_at=_now(),
            timeout_at=_now(),
        )


@pytest.mark.parametrize("decision", ["approve", "reject"])
def test_approval_decision_simple_verdicts(decision: str) -> None:
    dec = ApprovalDecision(decision=decision, decided_by="user-a")  # type: ignore[arg-type]
    assert dec.decision == decision
    assert dec.modified_args is None


def test_approval_decision_modify_requires_modified_args() -> None:
    with pytest.raises(ValidationError, match="modified_args"):
        ApprovalDecision(decision="modify", decided_by="user-a")


def test_approval_decision_modify_round_trips() -> None:
    dec = ApprovalDecision(
        decision="modify",
        modified_args={"to": "safe@example.com"},
        decided_by="user-a",
    )
    assert dec.modified_args == {"to": "safe@example.com"}


def test_approval_decision_rejects_modified_args_on_non_modify() -> None:
    """``modified_args`` only makes sense for ``modify`` — reject it elsewhere."""
    with pytest.raises(ValidationError, match="modified_args"):
        ApprovalDecision(
            decision="approve",
            modified_args={"to": "x"},
            decided_by="user-a",
        )


def test_approval_decision_timeout_reason() -> None:
    """The timeout job emits a reject with ``decided_by='system'``."""
    dec = ApprovalDecision(decision="reject", decided_by="system", reason="timeout")
    assert dec.reason == "timeout"


# ---------------------------------------------------------------------------
# RT-6 Tier A — canonical_args_digest + binding_digest field (RT-ADR-19)
# ---------------------------------------------------------------------------


def test_canonical_args_digest_is_deterministic() -> None:
    """Same args → same digest across calls (stable receipt)."""
    args = {"command": "./deploy.sh prod", "timeout_s": 30}
    assert canonical_args_digest(args) == canonical_args_digest(dict(args))


def test_canonical_args_digest_ignores_key_order() -> None:
    """Dict insertion order must not change the digest (canonical = sorted)."""
    a = {"command": "ls", "timeout_s": 5}
    b = {"timeout_s": 5, "command": "ls"}
    assert canonical_args_digest(a) == canonical_args_digest(b)


def test_canonical_args_digest_detects_value_change() -> None:
    """A changed argument value flips the digest — the whole point of binding."""
    approved = canonical_args_digest({"command": "./deploy.sh"})
    tampered = canonical_args_digest({"command": "rm -rf /"})
    assert approved != tampered


def test_canonical_args_digest_handles_non_json_native_values() -> None:
    """Non-JSON values (UUID / datetime) fall back to ``str`` without raising."""
    from uuid import UUID

    digest = canonical_args_digest({"id": UUID("00000000-0000-0000-0000-0000000000ff")})
    assert isinstance(digest, str) and len(digest) == 64


def test_approval_request_binding_digest_defaults_empty() -> None:
    """Legacy / pre-feature construction leaves ``binding_digest`` empty (unbound)."""
    req = ApprovalRequest(
        request_id="abc123",
        node="tools",
        reason_kind="policy_gate",
        action_summary="approval-gated tool 'bash'",
        requested_at=_now(),
        timeout_at=_now() + timedelta(hours=24),
    )
    assert req.binding_digest == ""


def test_approval_request_carries_binding_digest() -> None:
    req = ApprovalRequest(
        request_id="abc123",
        node="tools",
        reason_kind="policy_gate",
        action_summary="approval-gated tool 'bash'",
        proposed_args={"command": "ls"},
        requested_at=_now(),
        timeout_at=_now() + timedelta(hours=24),
        binding_digest=canonical_args_digest({"command": "ls"}),
    )
    assert req.binding_digest == canonical_args_digest({"command": "ls"})


# ---------------------------------------------------------------------------
# B-20 approval triage — the safety / clarification partition
# ---------------------------------------------------------------------------


def test_kind_class_sets_partition_all_reason_kinds() -> None:
    """SAFETY ∪ CLARIFICATION covers every reason kind, with no overlap."""
    from typing import get_args

    all_kinds = set(get_args(ApprovalReasonKind))
    assert SAFETY_REASON_KINDS | CLARIFICATION_REASON_KINDS == all_kinds
    assert not (SAFETY_REASON_KINDS & CLARIFICATION_REASON_KINDS)


def test_approval_kind_class_mapping() -> None:
    assert approval_kind_class("policy_gate") == "safety"
    assert approval_kind_class("risk_confirmation") == "safety"
    assert approval_kind_class("missing_info") == "clarification"
    assert approval_kind_class("ambiguous_requirement") == "clarification"
    assert approval_kind_class("approach_choice") == "clarification"


def test_approval_kind_class_unknown_fails_closed_to_safety() -> None:
    """A reason kind this build does not know must get sign-off semantics."""
    assert approval_kind_class("some_future_kind") == "safety"
    assert approval_kind_class("") == "safety"
