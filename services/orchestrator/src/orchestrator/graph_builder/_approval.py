"""Approval-gate helpers for ``tools_node`` — Stream J.8 (Mini-ADR J-24).

expert_work's ``tools_node`` dispatches a turn's ``tool_calls`` in parallel
stages (Stream L.L6 — ``plan_stages`` + ``asyncio.gather``). LangGraph's
native ``interrupt()`` re-runs the whole node on resume, which does not
compose cleanly with an in-flight ``gather``. After comparing with
deer-flow (whose ``ClarificationMiddleware`` returns ``Command(goto=END)``
on a serial tool loop), J.8 adopts the **end-and-resume** model:

* ``tools_node`` checks the turn's ``tool_calls`` *before* staging. If a
  call is approval-gated — either its name is in
  ``policies.approval_required_tools`` (the platform-enforced declarative
  gate) or it is the agent-initiated ``ask_for_approval`` builtin — the
  node writes an :class:`ApprovalRequest` to ``AgentState.pending_approval``
  and dispatches nothing. The graph then routes to ``END`` and the run
  ends as ``RunStatus.PAUSED`` with its checkpoint intact.
* ``POST /v1/runs/{id}/resume`` (J.8-step3) writes the human verdict
  back into the checkpoint and re-invokes the graph.

This module owns the *detection* + *request construction* — pure
functions, no graph imports, easily unit-tested.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from langchain_core.messages import ToolMessage

from expert_work.protocol import (
    ApprovalReasonKind,
    ApprovalRequest,
    approval_kind_class,
    canonical_args_digest,
)
from orchestrator.tools.approval import ASK_FOR_APPROVAL_TOOL

__all__ = [
    "APPROVED_QUESTION_CONTENT",
    "WITHHELD_CALL_CONTENT",
    "ApprovalTarget",
    "ResumeOutcome",
    "apply_resume_decision",
    "build_approval_request",
    "find_approval_target",
]

#: B-76 — the result every call of an approved turn gets, except the one the
#: approval showed: it was not run, and the model may issue it again (a gated
#: call then asks for its own approval).
WITHHELD_CALL_CONTENT = (
    "[not run] this turn paused for human approval of another call; only the "
    "approved call ran. Call this tool again if it is still needed."
)

#: B-79 — the result an approved ``ask_for_approval`` call gets. The call is a
#: question, not an action: nothing is dispatched for it (its tool body is a
#: defensive stub that reports a wiring error). A ``modify`` appends the
#: reviewer's arguments (:data:`_APPROVED_WITH_ARGS`).
APPROVED_QUESTION_CONTENT = "[approved] a human approved this request."
_APPROVED_WITH_ARGS = " The human set these arguments: {}"
#: 审核人填的参数拼进提示词前的上限(与裁定 ``reason`` 的 2048 同量级)。它不经工具输出
#: 预算,端点对 ``modified_args`` 也没有长度上限。
APPROVED_ARGS_MAX_CHARS = 2048
_TRUNCATED_SUFFIX = "…(truncated)"

#: ``reason_kind`` values an ``ask_for_approval`` call may carry. A call
#: with anything else (or nothing) falls back to ``risk_confirmation``.
_AGENT_REASON_KINDS: frozenset[str] = frozenset(
    {
        "missing_info",
        "ambiguous_requirement",
        "approach_choice",
        "risk_confirmation",
    }
)


class ApprovalTarget:
    """The first approval-gated ``tool_call`` found in a turn.

    ``index`` is the call's position in the turn's ``tool_calls`` list;
    ``is_agent_initiated`` distinguishes an ``ask_for_approval`` call
    (the agent asked) from a declarative-gate hit (the platform
    intervened) — they differ only in resume *reject* semantics
    (STREAM-J-DESIGN § 14.5).
    """

    __slots__ = ("index", "is_agent_initiated", "tool_call")

    def __init__(
        self,
        *,
        index: int,
        tool_call: Mapping[str, Any],
        is_agent_initiated: bool,
    ) -> None:
        self.index = index
        self.tool_call = tool_call
        self.is_agent_initiated = is_agent_initiated


def find_approval_target(
    tool_calls: list[dict[str, Any]],
    approval_required_tools: frozenset[str],
) -> ApprovalTarget | None:
    """Return the first approval-gated call in ``tool_calls``, or ``None``.

    A call is gated when it is the ``ask_for_approval`` builtin, or its
    name is in ``approval_required_tools``. M0 pauses on the *first*
    such call — the rest of the turn's calls are simply not dispatched
    this round; a resume re-runs the agent which re-decides.
    """
    for index, call in enumerate(tool_calls):
        name = call.get("name")
        if name == ASK_FOR_APPROVAL_TOOL:
            return ApprovalTarget(index=index, tool_call=call, is_agent_initiated=True)
        if name in approval_required_tools:
            return ApprovalTarget(index=index, tool_call=call, is_agent_initiated=False)
    return None


def _stable_request_id(thread_id: str, node: str, action_summary: str) -> str:
    """Deterministic id for an :class:`ApprovalRequest`.

    A retried turn (same thread, same node, same action) produces the
    same id, so a UI keying approvals by ``request_id`` never shows a
    duplicate — the same防重试 trick deer-flow uses for its
    clarification message ids.
    """
    digest = hashlib.sha256(f"{thread_id}\x00{node}\x00{action_summary}".encode()).hexdigest()
    return f"approval:{digest[:16]}"


def _coerce_reason_kind(raw: object) -> ApprovalReasonKind:
    """Map an ``ask_for_approval`` call's ``reason_kind`` arg to the enum.

    An unknown / missing value is not a hard error — the agent's
    free-form arg should never crash the run. Fall back to the most
    conservative kind (``risk_confirmation``).
    """
    if isinstance(raw, str) and raw in _AGENT_REASON_KINDS:
        return raw  # type: ignore[return-value]  # membership-checked
    return "risk_confirmation"


def _action_summary(target: ApprovalTarget) -> str:
    """The ``action_summary`` an :class:`ApprovalRequest` for ``target`` shows.

    Declarative-gate hits get an auto-built summary; ``ask_for_approval``
    calls carry the agent's own. Shared by :func:`build_approval_request` and
    the legacy-verdict check in :func:`_approved_target`.
    """
    call = target.tool_call
    if target.is_agent_initiated:
        args = call.get("args") or {}
        return str(args.get("action_summary") or "agent requested human approval")
    return f"approval-gated tool '{call.get('name') or 'tool'}'"


def build_approval_request(
    target: ApprovalTarget,
    *,
    thread_id: str,
    timeout_s: int,
    clarification_timeout_s: int | None = None,
    now: datetime | None = None,
    bind: bool = True,
) -> ApprovalRequest:
    """Construct the :class:`ApprovalRequest` for a gated ``tool_call``.

    Declarative-gate hits get ``reason_kind="policy_gate"`` and an
    auto-built summary. ``ask_for_approval`` calls carry the agent's own
    ``reason_kind`` / ``action_summary`` / ``proposed_args``.

    RT-6 Tier A (RT-ADR-19) — ``bind`` controls whether the args digest is
    minted. It must be ``True`` only when the resume path re-finds *this
    exact* target: the declarative gate re-scans with the same
    ``find_approval_target(_gated_tools)`` on the same checkpointed
    ``tool_calls``, so mint target == resume target. The action-screen path
    (PI-3b) selects its target by a judge verdict the resume re-scan cannot
    reproduce; it mints ``bind=False`` (unbound) so verification is skipped
    rather than risk vetoing the wrong call — action-screen overrides are out
    of RT-6's gated-artifact scope.
    """
    moment = now or datetime.now(UTC)
    call = target.tool_call
    args = call.get("args") or {}
    action_summary = _action_summary(target)
    if target.is_agent_initiated:
        reason_kind: ApprovalReasonKind = _coerce_reason_kind(args.get("reason_kind"))
        proposed_args = dict(args.get("proposed_args") or {})
    else:
        reason_kind = "policy_gate"
        proposed_args = dict(args)
    # B-20 approval triage — a clarification question (missing_info /
    # ambiguous_requirement / approach_choice) gets its own, typically much
    # shorter, deadline; safety rows (policy_gate / risk_confirmation) keep
    # ``policies.approval_timeout_s``.
    effective_timeout_s = timeout_s
    if clarification_timeout_s is not None and approval_kind_class(reason_kind) == "clarification":
        effective_timeout_s = clarification_timeout_s
    return ApprovalRequest(
        request_id=_stable_request_id(thread_id, "tools", action_summary),
        node="tools",
        reason_kind=reason_kind,
        action_summary=action_summary,
        proposed_args=proposed_args,
        requested_at=moment,
        timeout_at=moment + timedelta(seconds=effective_timeout_s),
        # RT-6 Tier A (RT-ADR-19) — bind the args at mint (declarative gate);
        # ``bind=False`` (action-screen) leaves it unbound. Agent-initiated
        # ``ask_for_approval`` gets a digest but verification skips it (no
        # downstream execution).
        binding_digest=canonical_args_digest(proposed_args) if bind else "",
        tool_call_id=str(call.get("id") or ""),
        tool_call_index=target.index,
    )


class ResumeOutcome:
    """The result of applying a human verdict to a paused turn's tool_calls.

    Exactly one of two shapes:

    * **dispatch** — ``reject_messages`` empty. ``tool_calls`` is the whole
      turn, same length and order as the checkpointed calls (the approved
      one possibly arg-rewritten), so an index still means the same call.
      B-76 — only the approved call (the one the approval showed) may run:
      ``withheld`` maps the index of every *other* call to the synthetic
      ``ToolMessage`` that answers it instead of dispatching. So at most one
      index is absent from ``withheld``; none when no call of this turn is
      the approved one. B-79 — when the approved call is ``ask_for_approval``
      it is not dispatched either: ``answered`` maps its index to the
      ``[approved]`` result. The two maps never share an index.
    * **reject** — ``reject_messages`` carries one synthetic
      ``ToolMessage`` per call (so no orphan tool_call is left), and
      ``tool_calls`` / ``withheld`` / ``answered`` are empty (nothing runs).
      ``terminal`` is ``True`` for a declarative-gate reject (the platform
      vetoed the run → route to END) and ``False`` for an
      ``ask_for_approval`` reject (the agent just loops back, sees the
      rejection, re-plans).

    ``binding_drift`` (RT-6 Tier A, RT-ADR-19) marks the reject as an
    *integrity veto*: the dispatched args no longer match what was
    approved (checkpoint tamper / replay / bug). It is always terminal
    and lets the caller emit the ``APPROVAL_BINDING_DRIFT`` audit.
    """

    __slots__ = (
        "answered",
        "binding_drift",
        "reject_messages",
        "terminal",
        "tool_calls",
        "withheld",
    )

    def __init__(
        self,
        *,
        tool_calls: list[dict[str, Any]],
        reject_messages: list[ToolMessage],
        terminal: bool,
        binding_drift: bool = False,
        withheld: dict[int, ToolMessage] | None = None,
        answered: dict[int, ToolMessage] | None = None,
    ) -> None:
        self.tool_calls = tool_calls
        self.reject_messages = reject_messages
        self.terminal = terminal
        self.binding_drift = binding_drift
        self.withheld: dict[int, ToolMessage] = withheld or {}
        self.answered: dict[int, ToolMessage] = answered or {}


def _binding_drift_reject(tool_calls: list[dict[str, Any]]) -> ResumeOutcome:
    """Build the terminal integrity-veto outcome for a binding-drift hit.

    One rejection ``ToolMessage`` per call (no orphan tool_call left);
    nothing dispatches; ``terminal`` + ``binding_drift`` set so the caller
    routes to END and emits ``APPROVAL_BINDING_DRIFT``.
    """
    messages = [
        ToolMessage(
            content=(
                "[approval binding drift] the tool arguments changed after "
                "approval and were not executed"
            ),
            tool_call_id=str(call.get("id") or ""),
            status="error",
            name=call.get("name"),
        )
        for call in tool_calls
    ]
    return ResumeOutcome(tool_calls=[], reject_messages=messages, terminal=True, binding_drift=True)


def _has_binding_drift(
    target: ApprovalTarget,
    dispatched_args: Mapping[str, Any],
    resume: Mapping[str, Any],
) -> bool:
    """True iff the dispatched args diverge from the approved binding (RT-ADR-19).

    Verification applies only to a declarative-gate target (a real gated
    tool with downstream execution) and only when the resume carries a
    non-empty ``binding_digest`` — an ``ask_for_approval`` self-pause or a
    legacy / pre-feature row (empty digest) is left unverified.
    """
    expected = str(resume.get("binding_digest") or "")
    if not expected or target.is_agent_initiated:
        return False
    return canonical_args_digest(dispatched_args) != expected


def _verdict_is_for_this_turn(
    tool_calls: list[dict[str, Any]], resume: Mapping[str, Any], turn_run_id: str | None
) -> bool:
    """班车 2 —— 这条裁定是不是批给**这一轮**的。

    续跑写检查点之前控制面已经核对过一次,但「核对 → 写入」不是原子的:两者之间
    新一轮可以跑到自己的审批门,裁定随后落在新一轮的检查点上。所以图里再核对一次,
    两项都用写裁定时从被批请求上抄来的身份:

    * **请求**:``request_id`` 是按「暂停时的 run id + 摘要」算的;这一轮尾巴那条
      助手消息盖的 run 戳,换算出来必须是同一个 ``request_id``。模型给的调用 id
      可能不唯一(有的兼容厂商每轮都从 ``call_0`` 数起),这一项不依赖它。
    * **调用**:``tool_call_index`` 处的调用 id 必须是 ``tool_call_id``。

    裁定里缺某一项(滚动发布期间旧副本写的裁定、旧请求、没有 run 戳的旧消息、模型
    没给 id)就跳过那一项,行为与以前一致。
    """
    request_id = resume.get("request_id")
    summary = resume.get("action_summary")
    if request_id and isinstance(summary, str) and turn_run_id is not None:
        if _stable_request_id(turn_run_id, "tools", summary) != request_id:
            return False
    call_id = resume.get("tool_call_id")
    if call_id:
        index = resume.get("tool_call_index")
        if not isinstance(index, int) or not 0 <= index < len(tool_calls):
            return False
        if str(tool_calls[index].get("id") or "") != call_id:
            return False
    return True


def _approved_target(
    tool_calls: list[dict[str, Any]],
    approval_required_tools: frozenset[str],
    resume: Mapping[str, Any],
) -> ApprovalTarget | None:
    """B-76 — the call the approval showed: the only one a verdict may release.

    A verdict carries the request's ``tool_call_index`` (班车 2; when the call
    id is known, :func:`_verdict_is_for_this_turn` has already matched it
    against this turn) → that call. This matters for action screening, whose
    request targets the judge's pick, not the declarative scan's. An index
    that points at no call of this turn → ``None``: nothing is released.

    A verdict without it answers a request minted before 班车 2 (still pending
    across the deploy) → :func:`_legacy_target`.
    """
    index = resume.get("tool_call_index")
    if index is None:
        return _legacy_target(tool_calls, approval_required_tools, resume)
    if not isinstance(index, int) or not 0 <= index < len(tool_calls):
        return None
    call = tool_calls[index]
    return ApprovalTarget(
        index=index,
        tool_call=call,
        is_agent_initiated=call.get("name") == ASK_FOR_APPROVAL_TOOL,
    )


def _legacy_target(
    tool_calls: list[dict[str, Any]],
    approval_required_tools: frozenset[str],
    resume: Mapping[str, Any],
) -> ApprovalTarget | None:
    """The approved call for a verdict with no ``tool_call_index`` — or ``None``.

    The declarative scan re-finds the call only when the request was built from
    that scan; an action-screen request (judge's pick) looks the same apart from
    its binding. So the scan's target is trusted only when:

    * it is ``ask_for_approval`` — the request showed that very call; or
    * it is a gated call and the verdict proves a bound mint: an ``approve``
      carries the mint's ``binding_digest``, and only the declarative gate
      mints bound (action screening mints ``bind=False``; pre-RT-6 rows are
      empty too). The digest must also not match more than one call of the
      step — the gate set may have been edited during the pause, and a
      same-args call picked by the new scan would otherwise pass the drift
      check. (No match at all is left to that check: it is drift, a terminal
      veto.) A ``modify`` carries the digest of the reviewer's args whatever
      the mint was, so it proves nothing — there the carried
      ``action_summary`` must be present;

    and, whenever the verdict carries the request's ``action_summary``, it must
    be the summary a request for that target shows. Anything else releases
    nothing.
    """
    target = find_approval_target(tool_calls, approval_required_tools)
    if target is None:
        return None
    summary = resume.get("action_summary")
    if summary is not None and summary != _action_summary(target):
        return None
    if target.is_agent_initiated:
        return target
    if str(resume.get("decision", "approve")) == "modify":
        return target if summary is not None else None
    digest = resume.get("binding_digest")
    if not digest:
        return None
    matching = sum(canonical_args_digest(call.get("args") or {}) == digest for call in tool_calls)
    return None if matching > 1 else target


def _withhold_all_but(
    tool_calls: list[dict[str, Any]], released: int | None
) -> dict[int, ToolMessage]:
    """B-76 — one "not run" answer per call except ``released``, keyed by index.

    ``status="error"`` like every other synthetic "was not run" answer (reject,
    block, retype guard): the call produced no result, and neither the model
    (a provider's ``is_error``) nor an API client (``tool_result.status``)
    should read it as a success. ``tools_node`` keeps it out of the failure
    classification — not running is not a tool failure.
    """
    return {
        index: ToolMessage(
            content=WITHHELD_CALL_CONTENT,
            tool_call_id=str(call.get("id") or ""),
            status="error",
            name=call.get("name"),
        )
        for index, call in enumerate(tool_calls)
        if index != released
    }


def apply_resume_decision(
    tool_calls: list[dict[str, Any]],
    approval_required_tools: frozenset[str],
    resume: Mapping[str, Any],
    *,
    turn_run_id: str | None = None,
) -> ResumeOutcome:
    """Apply a resume ``{decision, modified_args, binding_digest}`` to a paused turn.

    班车 2 —— 裁定不属于这一轮(见 :func:`_verdict_is_for_this_turn`,``turn_run_id``
    是这一轮尾巴那条助手消息的 run 戳)时,不论批的是什么都按绑定漂移处理:什么都
    不执行,整轮终止。

    ``approve`` → run only the approved call (:func:`_approved_target`),
    unchanged. ``modify`` → run only the approved call, with its args
    replaced by ``modified_args``. Either way every other call of the turn is
    withheld (B-76): the approval showed one call, so it releases one call;
    the model is told to issue the others again if still needed. An approved
    ``ask_for_approval`` is answered, not dispatched (B-79). No approved
    call on this turn → nothing runs. ``reject`` → dispatch nothing; return a
    rejection ``ToolMessage`` per call. ``terminal`` is set for a
    declarative-gate reject.

    RT-6 Tier A (RT-ADR-19) — before an approve / modify dispatches, the
    approved call's args are re-hashed and matched against the approved
    ``binding_digest``. A mismatch (the checkpointed tool_call drifted from
    what was approved) is a terminal integrity veto: nothing runs.
    """
    if not _verdict_is_for_this_turn(tool_calls, resume, turn_run_id):
        return _binding_drift_reject(tool_calls)
    decision = str(resume.get("decision", "approve"))
    if decision == "reject":
        target = find_approval_target(tool_calls, approval_required_tools)
        reason = str(resume.get("reason") or "approval rejected by reviewer")
        messages = [
            ToolMessage(
                content=f"[approval rejected] {reason}",
                tool_call_id=str(call.get("id") or ""),
                status="error",
                name=call.get("name"),
            )
            for call in tool_calls
        ]
        # A declarative-gate reject vetoes the whole run; an
        # agent-initiated ask_for_approval reject just informs the agent.
        terminal = target is not None and not target.is_agent_initiated
        return ResumeOutcome(tool_calls=[], reject_messages=messages, terminal=terminal)
    # approve / modify (any other decision value reads as approve, as before).
    target = _approved_target(tool_calls, approval_required_tools, resume)
    dispatched = [dict(call) for call in tool_calls]
    if target is None:
        return ResumeOutcome(
            tool_calls=dispatched,
            reject_messages=[],
            terminal=False,
            withheld=_withhold_all_but(tool_calls, None),
        )
    args: Mapping[str, Any] = target.tool_call.get("args") or {}
    if decision == "modify":
        # The bound reference for a modify is the modified args (the resume
        # endpoint re-hashes them into ``binding_digest`` atomically with the
        # decision), so verify the rewritten args against it.
        args = dict(resume.get("modified_args") or {})
        dispatched[target.index] = {**dispatched[target.index], "args": args}
    if _has_binding_drift(target, args, resume):
        return _binding_drift_reject(tool_calls)
    answered: dict[int, ToolMessage] = {}
    if target.is_agent_initiated:
        answered[target.index] = _approved_question(target.tool_call, decision, args)
    return ResumeOutcome(
        tool_calls=dispatched,
        reject_messages=[],
        terminal=False,
        withheld=_withhold_all_but(tool_calls, target.index),
        answered=answered,
    )


def _approved_question(
    call: Mapping[str, Any], decision: str, args: Mapping[str, Any]
) -> ToolMessage:
    """B-79 — the result of an approved ``ask_for_approval`` call.

    A ``modify`` hands the reviewer's arguments back as compact JSON, exactly
    as the verdict carries them — the agent asked for them, and the call itself
    has nothing to run them through.
    """
    content = APPROVED_QUESTION_CONTENT
    if decision == "modify":
        # ``default=str`` 与 ``canonical_args_digest`` 同口径:这里抛错会让续跑卡在这一步。
        dumped = json.dumps(args, ensure_ascii=False, separators=(",", ":"), default=str)
        if len(dumped) > APPROVED_ARGS_MAX_CHARS:
            dumped = dumped[:APPROVED_ARGS_MAX_CHARS] + _TRUNCATED_SUFFIX
        content += _APPROVED_WITH_ARGS.format(dumped)
    return ToolMessage(
        content=content,
        tool_call_id=str(call.get("id") or ""),
        status="success",
        name=call.get("name"),
    )
