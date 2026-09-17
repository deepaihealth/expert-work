"""J.8 HITL / 审批 eval — Stream J.13a (M0 baseline) closeout.

Mini-ADR J-24 + STREAM-J-DESIGN § 14 end-to-end behaviour lock. Drives
the J.8 pieces against scripted, fully-deterministic cases:

* **detect** — the approval gate finds a declarative-gate / agent-
  initiated call (or correctly finds nothing).
* **request** — :class:`ApprovalRequest` is built with the right
  ``reason_kind`` (policy_gate for the gate, agent value otherwise,
  conservative fallback on a bogus value).
* **resume** — :func:`apply_resume_decision` dispatches on approve,
  rewrites args on modify, and rejects with the gate / ask terminal
  split.
* **store** — :class:`InMemoryApprovalStore` create / get / list_expired
  / mark_decided behaviour, including the cross-tenant hiding rule.
* **decision** — :class:`ApprovalDecision` modify-args validator.

Per Mini-ADR J-37 J.8 metric is deterministic ``pass_rate``; the
baseline threshold is ≥ 0.95 (§ 18.3) — achievable = 1.00 on these
all-deterministic scripted cases.
"""

from __future__ import annotations

import sys as _sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from pathlib import Path as _Path
from typing import Any
from uuid import uuid4

import yaml
from pydantic import ValidationError

from expert_work.persistence import InMemoryApprovalStore
from expert_work.protocol import (
    ApprovalDecision,
    ApprovalRecord,
    ApprovalStatus,
    canonical_args_digest,
)
from orchestrator.graph_builder._approval import (
    apply_resume_decision,
    build_approval_request,
    find_approval_target,
)
from orchestrator.tools.approval import ASK_FOR_APPROVAL_TOOL

_EVAL_DIR = _Path(__file__).resolve().parent
if str(_EVAL_DIR) not in _sys.path:
    _sys.path.insert(0, str(_EVAL_DIR))

from _capability import (  # type: ignore[import-not-found]  # noqa: E402
    CapabilityCaseResult,
    CapabilityReport,
)

CAPABILITY = "J.8_hitl"
METRIC_TYPE = "pass-rate"
THRESHOLD = {"pass_rate": 0.95}

_TENANT = uuid4()


@dataclass(frozen=True)
class HitlCase:
    """One scripted HITL behaviour case."""

    case_id: str
    scenario: str
    args: dict[str, Any] = field(default_factory=dict)


def _tc(name: str, args: dict[str, Any]) -> dict[str, Any]:
    return {"name": name, "args": args, "id": f"tc-{name}", "type": "tool_call"}


# ---------------------------------------------------------------------------
# detect
# ---------------------------------------------------------------------------


async def _run_detect_declarative_gate() -> tuple[bool, str]:
    calls = [_tc("safe", {}), _tc("send_email", {"to": "x"})]
    target = find_approval_target(calls, frozenset({"send_email"}))
    if target is None or target.index != 1 or target.is_agent_initiated:
        return False, f"expected gate hit at idx 1, got {target}"
    return True, ""


async def _run_detect_ask_for_approval() -> tuple[bool, str]:
    calls = [_tc(ASK_FOR_APPROVAL_TOOL, {"action_summary": "ok?"})]
    target = find_approval_target(calls, frozenset())
    if target is None or not target.is_agent_initiated:
        return False, "expected agent-initiated detection"
    return True, ""


async def _run_detect_nothing_gated() -> tuple[bool, str]:
    calls = [_tc("safe", {})]
    if find_approval_target(calls, frozenset({"send_email"})) is not None:
        return False, "ungated turn should not detect a target"
    return True, ""


async def _run_detect_first_hit() -> tuple[bool, str]:
    calls = [_tc("send_email", {}), _tc(ASK_FOR_APPROVAL_TOOL, {})]
    target = find_approval_target(calls, frozenset({"send_email"}))
    if target is None or target.index != 0:
        return False, "M0 should pause on the first gated call"
    return True, ""


# ---------------------------------------------------------------------------
# request
# ---------------------------------------------------------------------------


async def _run_request_policy_gate() -> tuple[bool, str]:
    calls = [_tc("send_email", {"to": "x"})]
    target = find_approval_target(calls, frozenset({"send_email"}))
    if target is None:
        return False, "gated call not detected"
    req = build_approval_request(target, thread_id="r-1", timeout_s=3600)
    if req.reason_kind != "policy_gate":
        return False, f"expected policy_gate, got {req.reason_kind}"
    if (req.timeout_at - req.requested_at) != timedelta(seconds=3600):
        return False, "timeout_at != requested_at + timeout_s"
    return True, ""


async def _run_request_agent_reason_kind() -> tuple[bool, str]:
    calls = [_tc(ASK_FOR_APPROVAL_TOOL, {"reason_kind": "approach_choice", "action_summary": "?"})]
    target = find_approval_target(calls, frozenset())
    if target is None:
        return False, "ask_for_approval call not detected"
    req = build_approval_request(target, thread_id="r-1", timeout_s=86400)
    if req.reason_kind != "approach_choice":
        return False, f"expected approach_choice, got {req.reason_kind}"
    return True, ""


async def _run_request_bogus_reason_kind_falls_back() -> tuple[bool, str]:
    calls = [_tc(ASK_FOR_APPROVAL_TOOL, {"reason_kind": "nonsense"})]
    target = find_approval_target(calls, frozenset())
    if target is None:
        return False, "ask_for_approval call not detected"
    req = build_approval_request(target, thread_id="r-1", timeout_s=86400)
    if req.reason_kind != "risk_confirmation":
        return False, f"bogus reason_kind should fall back, got {req.reason_kind}"
    return True, ""


# ---------------------------------------------------------------------------
# resume
# ---------------------------------------------------------------------------


def _verdict(
    calls: list[dict[str, Any]],
    gated: frozenset[str],
    decision: str,
    modified_args: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The ``approval_resume`` the decide endpoint writes for this paused turn.

    B-76 —— 续跑只放行裁定里那一条调用(``tool_call_index``);控制面总会把被批请求的
    身份抄进来,``modify`` 的绑定摘要是改后参数的哈希。缺这些项的裁定按「无法证明来源」
    什么都不放行,所以评测必须用端点实际写出的形状。
    """
    target = find_approval_target(calls, gated)
    if target is None:
        raise ValueError("the scripted turn has no gated call")
    request = build_approval_request(target, thread_id="run-eval", timeout_s=60)
    return {
        "decision": decision,
        "modified_args": modified_args,
        "reason": None,
        "binding_digest": (
            canonical_args_digest(modified_args or {})
            if decision == "modify"
            else request.binding_digest
        ),
        "request_id": request.request_id,
        "action_summary": request.action_summary,
        "tool_call_id": request.tool_call_id,
        "tool_call_index": request.tool_call_index,
    }


async def _run_resume_approve_dispatches() -> tuple[bool, str]:
    calls = [_tc("send_email", {"to": "x"})]
    gated = frozenset({"send_email"})
    outcome = apply_resume_decision(calls, gated, _verdict(calls, gated, "approve"))
    if outcome.reject_messages or outcome.withheld or outcome.answered:
        return False, "approve should release the approved call, nothing withheld"
    if [call["args"] for call in outcome.tool_calls] != [{"to": "x"}]:
        return False, f"approve should dispatch the call unchanged: {outcome.tool_calls}"
    return True, ""


async def _run_resume_modify_rewrites_args() -> tuple[bool, str]:
    calls = [_tc("send_email", {"to": "danger@evil.com"})]
    gated = frozenset({"send_email"})
    outcome = apply_resume_decision(
        calls, gated, _verdict(calls, gated, "modify", {"to": "safe@example.com"})
    )
    if outcome.reject_messages or outcome.withheld:
        return False, "modify should release the approved call, nothing withheld"
    if outcome.tool_calls[0]["args"] != {"to": "safe@example.com"}:
        return False, f"modify did not rewrite args: {outcome.tool_calls[0]['args']}"
    return True, ""


async def _run_resume_reject_gate_is_terminal() -> tuple[bool, str]:
    calls = [_tc("send_email", {"to": "x"})]
    outcome = apply_resume_decision(
        calls, frozenset({"send_email"}), {"decision": "reject", "modified_args": None}
    )
    if not outcome.reject_messages or not outcome.terminal:
        return False, "declarative-gate reject must be terminal with a rejection message"
    if outcome.tool_calls:
        return False, "reject dispatches nothing"
    return True, ""


async def _run_resume_reject_ask_not_terminal() -> tuple[bool, str]:
    calls = [_tc(ASK_FOR_APPROVAL_TOOL, {"action_summary": "?"})]
    outcome = apply_resume_decision(
        calls, frozenset(), {"decision": "reject", "modified_args": None}
    )
    if not outcome.reject_messages or outcome.terminal:
        return False, "ask_for_approval reject must NOT be terminal"
    return True, ""


# ---------------------------------------------------------------------------
# store
# ---------------------------------------------------------------------------


def _record(*, run_id: Any, timeout_at: datetime, status: ApprovalStatus) -> ApprovalRecord:
    now = datetime.now(UTC)
    return ApprovalRecord(
        id=uuid4(),
        tenant_id=_TENANT,
        run_id=run_id,
        thread_id=uuid4(),
        request_id="approval:x",
        node="tools",
        reason_kind="policy_gate",
        action_summary="gated tool",
        requested_at=now,
        timeout_at=timeout_at,
        status=status,
    )


async def _run_store_create_get() -> tuple[bool, str]:
    store = InMemoryApprovalStore()
    run_id = uuid4()
    now = datetime.now(UTC)
    await store.create(
        _record(run_id=run_id, timeout_at=now + timedelta(hours=24), status=ApprovalStatus.PENDING)
    )
    got = await store.get_by_run(run_id=run_id, tenant_id=_TENANT)
    if got is None or got.status is not ApprovalStatus.PENDING:
        return False, "create→get round-trip failed"
    return True, ""


async def _run_store_cross_tenant_hidden() -> tuple[bool, str]:
    store = InMemoryApprovalStore()
    run_id = uuid4()
    now = datetime.now(UTC)
    await store.create(
        _record(run_id=run_id, timeout_at=now + timedelta(hours=24), status=ApprovalStatus.PENDING)
    )
    if await store.get_by_run(run_id=run_id, tenant_id=uuid4()) is not None:
        return False, "cross-tenant get must return None"
    return True, ""


async def _run_store_list_expired() -> tuple[bool, str]:
    store = InMemoryApprovalStore()
    now = datetime.now(UTC)
    await store.create(
        _record(run_id=uuid4(), timeout_at=now - timedelta(hours=1), status=ApprovalStatus.PENDING)
    )
    await store.create(
        _record(run_id=uuid4(), timeout_at=now + timedelta(hours=1), status=ApprovalStatus.PENDING)
    )
    expired = await store.list_expired(before=now)
    if len(expired) != 1:
        return False, f"expected 1 expired row, got {len(expired)}"
    return True, ""


async def _run_store_mark_decided_once() -> tuple[bool, str]:
    store = InMemoryApprovalStore()
    run_id = uuid4()
    now = datetime.now(UTC)
    await store.create(
        _record(run_id=run_id, timeout_at=now + timedelta(hours=24), status=ApprovalStatus.PENDING)
    )
    first = await store.mark_decided(
        run_id=run_id,
        tenant_id=_TENANT,
        status=ApprovalStatus.APPROVED,
        decided_by="user-a",
        decided_at=now,
    )
    second = await store.mark_decided(
        run_id=run_id,
        tenant_id=_TENANT,
        status=ApprovalStatus.REJECTED,
        decided_by="user-b",
        decided_at=now,
    )
    if not first or second:
        return False, f"mark_decided should be idempotent-once, got ({first}, {second})"
    return True, ""


# ---------------------------------------------------------------------------
# decision validator
# ---------------------------------------------------------------------------


async def _run_decision_modify_requires_args() -> tuple[bool, str]:
    try:
        ApprovalDecision(decision="modify", decided_by="user-a")
    except ValidationError:
        return True, ""
    return False, "decision=modify without modified_args should fail validation"


async def _run_decision_approve_rejects_args() -> tuple[bool, str]:
    try:
        ApprovalDecision(decision="approve", modified_args={"x": 1}, decided_by="user-a")
    except ValidationError:
        return True, ""
    return False, "modified_args on a non-modify decision should fail validation"


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


_SCENARIOS: dict[str, Any] = {
    "detect_declarative_gate": _run_detect_declarative_gate,
    "detect_ask_for_approval": _run_detect_ask_for_approval,
    "detect_nothing_gated": _run_detect_nothing_gated,
    "detect_first_hit": _run_detect_first_hit,
    "request_policy_gate": _run_request_policy_gate,
    "request_agent_reason_kind": _run_request_agent_reason_kind,
    "request_bogus_reason_kind_falls_back": _run_request_bogus_reason_kind_falls_back,
    "resume_approve_dispatches": _run_resume_approve_dispatches,
    "resume_modify_rewrites_args": _run_resume_modify_rewrites_args,
    "resume_reject_gate_is_terminal": _run_resume_reject_gate_is_terminal,
    "resume_reject_ask_not_terminal": _run_resume_reject_ask_not_terminal,
    "store_create_get": _run_store_create_get,
    "store_cross_tenant_hidden": _run_store_cross_tenant_hidden,
    "store_list_expired": _run_store_list_expired,
    "store_mark_decided_once": _run_store_mark_decided_once,
    "decision_modify_requires_args": _run_decision_modify_requires_args,
    "decision_approve_rejects_args": _run_decision_approve_rejects_args,
}


async def _run_case(case: HitlCase) -> CapabilityCaseResult:
    runner = _SCENARIOS.get(case.scenario)
    if runner is None:
        return CapabilityCaseResult(
            case_id=case.case_id,
            passed=False,
            notes=(f"unknown scenario {case.scenario!r}",),
        )
    passed, note = await runner()
    return CapabilityCaseResult(
        case_id=case.case_id,
        passed=passed,
        notes=(note,) if note else (),
    )


# ---------------------------------------------------------------------------
# Public surface — matches sibling eval modules (load_cases / evaluate_set).
# ---------------------------------------------------------------------------


def load_cases(path: Path) -> tuple[HitlCase, ...]:
    """Parse the YAML dataset into :class:`HitlCase` tuples."""
    with path.open("r", encoding="utf-8") as fh:
        payload = yaml.safe_load(fh)
    raw_cases = payload.get("cases", [])
    out: list[HitlCase] = []
    for raw in raw_cases:
        out.append(
            HitlCase(
                case_id=str(raw["id"]),
                scenario=str(raw["scenario"]),
                args=dict(raw.get("args", {})),
            )
        )
    return tuple(out)


async def evaluate_set(cases: Sequence[HitlCase]) -> CapabilityReport:
    """Run all cases sequentially; produce the capability report."""
    per_case: list[CapabilityCaseResult] = []
    for case in cases:
        per_case.append(await _run_case(case))
    sample_size = len(per_case)
    passed = sum(1 for r in per_case if r.passed)
    pass_rate = passed / sample_size if sample_size else 0.0
    threshold = THRESHOLD["pass_rate"]
    status = "PASS" if pass_rate >= threshold and sample_size > 0 else "FAIL"
    return CapabilityReport(
        capability=CAPABILITY,
        metric_type=METRIC_TYPE,
        sample_size=sample_size,
        threshold=dict(THRESHOLD),
        aggregate_score={"pass_rate": pass_rate},
        status=status,
        per_case=tuple(per_case),
    )


__all__ = [
    "CAPABILITY",
    "METRIC_TYPE",
    "THRESHOLD",
    "HitlCase",
    "evaluate_set",
    "load_cases",
]
