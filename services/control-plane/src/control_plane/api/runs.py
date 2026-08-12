"""``POST /v1/sessions/{thread_id}/runs`` — SSE run trigger.

Stream B.7 shipped a *fake* stream; the control-plane cutover replaces
it with the real path. In-process monolith (STREAM-E-DESIGN § 2.6): the
endpoint loads the thread's agent manifest, builds (or cache-hits) a
runnable agent via the orchestrator's :func:`build_agent`, spawns the
E.14 ``run_agent`` worker as a background task, and streams the worker's
events back through E.14 ``sse_consumer``.

SSE event vocabulary is ``metadata`` / ``updates`` / ``end`` / ``error``
plus ``: heartbeat`` comment frames — see the amended ADR B-4. The old
``token`` / ``done`` words were fake-stream placeholders.

Cancellation: ``sse_consumer`` polls ``request.is_disconnected`` and, on
disconnect, cancels the run through the :class:`RunManager` (E.15
cooperative cancellation surfaces it inside the graph).

Audit: a ``session:write`` row lands at run start; the ``run_agent``
worker writes the run-completion row (``run:completed`` / ``run:failed``)
at run end.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime
from typing import Annotated, Any, Final, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from pydantic import BaseModel, ConfigDict, Field, field_validator

from control_plane.agent_disable_status import AgentDisableService
from control_plane.api._authz import console_only, require_key_scope
from control_plane.api._quota_admission import check_admission
from control_plane.api._run_event_stream import build_event_producer
from control_plane.api._session_title import title_from_text
from control_plane.api._user_scope import (
    caller_owns_thread,
    ensure_member_active,
    get_user_repo,
    resolve_caller_user_id,
)
from control_plane.api.trace_facade import fetch_and_normalize, fetch_span_raw
from control_plane.audit import emit
from control_plane.kill_switch import run_block_reason
from control_plane.prompt_render import (
    PromptRenderError,
    render_system_prompt,
    validate_prompt_inputs,
)
from control_plane.quota.base import QuotaService
from control_plane.runtime import AgentRuntime
from control_plane.settings import Settings
from control_plane.tenant_scope import (
    CrossTenant,
    applied_scope,
    cross_tenant_query_enabled,
    ensure_single_tenant_scope,
    ensure_tenant_scope,
)
from control_plane.tenant_status import TenantStatusService
from control_plane.transcript import read_turns
from expert_work.common.observability import (
    current_trace_id_hex,
    expert_work_counter,
    expert_work_histogram,
)
from expert_work.common.spotlight import spotlight_untrusted
from expert_work.persistence import ApprovalStore
from expert_work.persistence.agent_spec import AgentSpecStore
from expert_work.persistence.rls import current_user_id_var
from expert_work.persistence.tenant_user import TenantUserStore
from expert_work.persistence.thread_meta import ThreadMetaStore
from expert_work.persistence.token_usage_store import TokenTotals, TokenUsageStore
from expert_work.persistence.workspace import UserWorkspaceStore
from expert_work.protocol import (
    AgentSpec,
    AgentSpecStatus,
    ApprovalStatus,
    AuditAction,
    AuditResult,
    ThreadStatus,
    canonical_args_digest,
)
from expert_work.protocol.multimodal import parse_image_ref
from expert_work.runtime.audit.logger import AuditLogger
from expert_work.runtime.runs import RunEventStore, RunStore
from expert_work.runtime.runs.schemas import TERMINAL_RUN_STATUSES, RunStatus
from expert_work.runtime.runs.store import MAX_LIST_LIMIT, _clamp_limit
from orchestrator import AgentFactoryError, BuiltAgent, run_agent, sse_consumer
from orchestrator.multimodal import image_ref_block

logger = logging.getLogger("expert_work.control_plane.runs")

#: Char cap for the free-text ``input`` message field (this endpoint and the
#: external ``ExternalRunRequest``). This is the prompt the user types/pastes —
#: a long email, spec, or article should fit, so it is far larger than the 8192
#: cap that still guards the *structured* fields (``untrusted_content`` blocks,
#: jinja ``inputs`` values). Genuinely large content (a book, a report) rides
#: the document-upload path (``document_max_bytes``, read on demand in the
#: sandbox), not this field. Kept as a generous DoS guardrail, not a hard UX
#: limit — there is no global request-body middleware behind it.
MAX_RUN_INPUT_CHARS: Final[int] = 65536


class RunRequest(BaseModel):
    """POST body. ``input`` is the user's prompt for this run;
    ``image_refs`` is the list of J.6 ``expert_work://image/...`` references
    uploaded via ``POST /v1/sessions/{thread_id}/uploads``."""

    model_config = ConfigDict(extra="forbid")

    input: str | None = Field(default=None, max_length=MAX_RUN_INPUT_CHARS)
    #: Stream 9.5 — execution mode. ``stream`` (default) runs the agent inside
    #: this request and streams the result (SSE) — unchanged behaviour. ``queue``
    #: enqueues the run for the distributed run queue and returns ``202`` with
    #: the ``run_id`` immediately; a ``RunQueueWorker`` on any instance executes
    #: it, and the client reads the output over ``GET .../runs/{id}/events``.
    mode: Literal["stream", "queue"] = "stream"
    image_refs: list[str] = Field(default_factory=list, max_length=64)
    #: Stream PI-1c — structured untrusted input. A business system passes
    #: the data to act on (a ticket / email / document) here instead of
    #: concatenating it into ``input``, so expert_work knows which span is
    #: attacker-controllable and fences it with spotlighting before the
    #: model sees it. The matching system-prompt clause tells the model to
    #: treat fenced content as DATA, never instructions — the root fix for
    #: inline prompt injection. Empty / omitted → today's behaviour.
    untrusted_content: list[str] = Field(default_factory=list, max_length=16)
    #: Stream Dynamic-Prompt — run-time Jinja variables. Substituted into the
    #: agent's ``system_prompt`` template (when the agent opts into jinja mode)
    #: against its declared ``variables``. Keys not declared → 422; declared
    #: ``required`` keys missing → 422. Empty / omitted → today's behaviour.
    inputs: dict[str, Any] = Field(default_factory=dict)

    @field_validator("inputs")
    @classmethod
    def _bound_inputs(cls, value: dict[str, Any]) -> dict[str, Any]:
        if len(value) > 64:
            msg = "too many input variables (max 64)"
            raise ValueError(msg)
        for key, val in value.items():
            if isinstance(val, str) and len(val) > 8192:
                msg = f"input '{key}' exceeds 8192 chars"
                raise ValueError(msg)
        return value

    @field_validator("untrusted_content")
    @classmethod
    def _bound_untrusted_blocks(cls, value: list[str]) -> list[str]:
        for block in value:
            if len(block) > 8192:
                msg = "each untrusted_content block must be <= 8192 chars"
                raise ValueError(msg)
        return value

    @field_validator("image_refs")
    @classmethod
    def _parse_image_refs(cls, value: list[str]) -> list[str]:
        for ref in value:
            parse_image_ref(ref)  # raises ValueError if malformed → 422
        return value


class ResumeRequest(BaseModel):
    """POST body for the J.8 resume endpoint — a human's approval verdict.

    ``decided_by`` is *not* a body field — it is taken from the
    authenticated caller so a client cannot spoof the reviewer
    identity. ``modified_args`` is required for — and only for —
    ``decision == "modify"``.
    """

    model_config = ConfigDict(extra="forbid")

    decision: Literal["approve", "reject", "modify"]
    modified_args: dict[str, Any] | None = None
    reason: str | None = Field(default=None, max_length=2048)
    # Stream 13.2 — optional client-supplied key for deterministic recovery. A
    # retry / concurrent decide carrying the same key replays the same
    # continuation run instead of 409'ing. Omitted → today's exactly-once
    # behaviour (a duplicate decide 409s).
    idempotency_key: str | None = Field(default=None, max_length=255)


def _get_thread_repo(request: Request) -> ThreadMetaStore:
    repo: ThreadMetaStore = request.app.state.thread_meta_repo
    return repo


def _get_settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def _validate_image_refs(
    refs: list[str],
    *,
    tenant_id: UUID,
    thread_id: UUID,
    supports_vision: bool,
    has_vision_block: bool,
    max_per_run: int,
) -> None:
    """Enforce the J.6 run-time image-ref constraints.

    Raises :class:`HTTPException` with the right status:
    * **422** when the agent is image-incapable and no ``vision:`` block
      is declared, or when the count exceeds ``max_per_run``;
    * **404** when a ref belongs to a different tenant or thread —
      hides cross-scope existence per the J.14 pattern.
    """
    if not refs:
        return
    if not supports_vision and not has_vision_block:
        raise HTTPException(
            status_code=422,
            detail=(
                "agent does not accept image input: model.supports_vision is "
                "false and no 'vision' block is declared"
            ),
        )
    if len(refs) > max_per_run:
        raise HTTPException(
            status_code=422,
            detail=f"too many images: max {max_per_run} per run",
        )
    for ref_str in refs:
        ref = parse_image_ref(ref_str)
        if ref.tenant_id != tenant_id or ref.thread_id != thread_id:
            raise HTTPException(status_code=404, detail="image ref not found")


def _fence_untrusted(blocks: list[str], *, spotlight_nonce: str | None) -> str:
    """Render structured ``untrusted_content`` as a trailing text section.

    Stream PI-1c — each block is fenced with :func:`spotlight_untrusted`
    using the build's nonce (shared with the model-side tool/RAG channels)
    so the model treats it as DATA per the spotlight system clause. When
    the agent has spotlighting off (``spotlight_nonce is None``) the blocks
    are appended verbatim under a plain marker — degrades to today's
    behaviour, with the 7.4 output screen as the backstop.
    """
    if spotlight_nonce:
        fenced = [spotlight_untrusted(b, nonce=spotlight_nonce) for b in blocks]
    else:
        fenced = [f"[untrusted content]\n{b}" for b in blocks]
    return "\n\n".join(fenced)


def _build_human_message(
    *,
    input_text: str | None,
    image_refs: list[str],
    supports_vision: bool,
    untrusted_content: list[str] | None = None,
    spotlight_nonce: str | None = None,
) -> HumanMessage:
    """Assemble the ``HumanMessage`` for a J.6 multimodal run input.

    Path A (``supports_vision=True``) — emit a content-block list with
    the text followed by one ``image_ref`` block per upload, so the
    provider adapter resolves them to native multimodal payloads.

    Path B (``supports_vision=False`` with images) — emit plain text
    with each ref mentioned as ``[image attached: expert_work://...]``. The
    agent has the ``ask_image`` tool in its catalogue and uses these
    refs to call it.

    No-images case — emit plain text unchanged.

    Stream PI-1c — when ``untrusted_content`` is supplied, the fenced
    blocks are appended after the trusted instruction text (as a trailing
    text segment in both the content-block and plain paths) so the model
    can separate the user's instruction from attacker-controllable data.
    """
    text = input_text or ""
    untrusted = (
        _fence_untrusted(untrusted_content, spotlight_nonce=spotlight_nonce)
        if untrusted_content
        else ""
    )
    if not image_refs:
        body = f"{text}\n\n{untrusted}" if untrusted and text else (untrusted or text)
        return HumanMessage(content=body)
    if supports_vision:
        content: list[dict[str, Any]] = []
        if text:
            content.append({"type": "text", "text": text})
        for ref in image_refs:
            content.append(image_ref_block(ref))
        if untrusted:
            content.append({"type": "text", "text": untrusted})
        return HumanMessage(content=content)
    mentions = "\n".join(f"[image attached: {ref}]" for ref in image_refs)
    parts = [p for p in (text, mentions, untrusted) if p]
    return HumanMessage(content="\n\n".join(parts))


def build_run_graph_input(
    built: Any,
    *,
    input_text: str | None,
    image_refs: list[str],
    untrusted_content: list[str] | None,
    inputs: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Assemble the graph input for a run from a built agent + user input.

    Stream 9.5 — the single source both the synchronous POST handler and the
    distributed-queue worker (``RunQueueWorker``) use, so an enqueued run is
    executed byte-for-byte the same as a streamed one. ``built.*`` is rebuilt
    by the worker via ``runtime.get_agent`` (like the orphan-sweep respawn);
    only the user input is carried through the persisted ``enqueued_input``.

    Stream Dynamic-Prompt — ``inputs`` carries the run's Jinja variables; the
    system prompt is rendered here so stream and queue render identically.
    """
    return {
        "messages": [
            SystemMessage(content=render_system_prompt(built, inputs or {})),
            _build_human_message(
                input_text=input_text,
                image_refs=image_refs,
                supports_vision=built.supports_vision,
                untrusted_content=untrusted_content,
                spotlight_nonce=built.spotlight_nonce,
            ),
        ],
        "step_count": 0,
        "max_steps": built.max_steps,
        "max_no_progress": built.max_no_progress,
    }


def _get_audit(request: Request) -> AuditLogger:
    return request.app.state.audit_logger  # type: ignore[no-any-return]


def _get_quota(request: Request) -> QuotaService:
    return request.app.state.quota_service  # type: ignore[no-any-return]


def _get_agent_repo(request: Request) -> AgentSpecStore:
    return request.app.state.agent_spec_repo  # type: ignore[no-any-return]


def _get_agent_runtime(request: Request) -> AgentRuntime:
    return request.app.state.agent_runtime  # type: ignore[no-any-return]


def _get_approval_store(request: Request) -> ApprovalStore:
    return request.app.state.approval_store  # type: ignore[no-any-return]


def _get_run_store(request: Request) -> RunStore:
    return request.app.state.run_store  # type: ignore[no-any-return]


def _get_token_usage_store(request: Request) -> TokenUsageStore:
    return request.app.state.token_usage_store  # type: ignore[no-any-return]


def _get_run_event_store(request: Request) -> RunEventStore | None:
    """Stream H.3 PR 4 — the durable SSE event store wired by ``app.py``.

    ``None`` when the deployment opted out (no SSE replay; the
    ``/events`` endpoint then live-attaches only)."""
    store: RunEventStore | None = getattr(request.app.state, "run_event_store", None)
    return store


def _idempotent_continuation(approval: Any | None, idempotency_key: str | None) -> UUID | None:
    """Stream 13.2 — the continuation to replay for an idempotent retry, or None.

    Returns the stored ``continuation_run_id`` only when the caller supplied a
    non-empty ``idempotency_key`` that matches the one persisted with the
    original decision AND a continuation was recorded. Any mismatch (no key,
    different key, keyless original, no continuation) → ``None`` ⇒ the caller
    raises 409. Keyless decisions never replay — exactly-once stays the default.
    """
    if not idempotency_key or approval is None:
        return None
    if approval.idempotency_key != idempotency_key:
        return None
    return approval.continuation_run_id


async def apply_approval_decision(
    *,
    request: Request,
    thread_id: UUID,
    run_id: UUID,
    decision: Literal["approve", "reject", "modify"],
    modified_args: dict[str, Any] | None,
    reason: str | None,
    threads: Any,
    users: TenantUserStore,
    audit: AuditLogger,
    agent_repo: AgentSpecStore,
    runtime: AgentRuntime,
    approvals: ApprovalStore,
    idempotency_key: str | None = None,
) -> tuple[Any, UUID, bool]:
    """Apply one human verdict + spawn the continuation worker (J.8 core).

    Stream HX-7 — extracted from the resume endpoint so the batch
    ``POST /v1/approvals:decide`` shares the exact same path: verdict
    validation, the ``mark_decided`` CAS, the APPROVAL_DECIDED audit,
    the checkpoint ``aupdate_state``, and the detached worker spawn.
    The worker is independent of any SSE consumer — the resume endpoint
    streams it, the batch endpoint just returns its ``run_id``.

    Returns ``(run_record, continuation_run_id, replayed)``. ``replayed`` is
    ``True`` when an idempotent key matched an already-decided approval — the
    caller returns the stored ``continuation_run_id`` WITHOUT spawning a worker
    (``run_record`` is ``None`` then). Raises :class:`HTTPException`
    (404 / 409 / 422) — the batch caller maps those onto per-item results.
    """
    tenant_id: UUID = request.state.tenant_id
    actor_id: str = request.state.actor_id

    meta = await threads.get(thread_id, tenant_id=tenant_id)
    if meta is None:
        raise HTTPException(status_code=404, detail="session not found")
    caller_user_id = await resolve_caller_user_id(request, users)
    if not caller_owns_thread(
        meta=meta, caller_user_id=caller_user_id, principal=request.state.principal
    ):
        raise HTTPException(status_code=404, detail="session not found")
    # ``modify`` carries replacement args; the other verdicts must not.
    if decision == "modify" and modified_args is None:
        raise HTTPException(status_code=422, detail="decision 'modify' requires modified_args")
    if decision != "modify" and modified_args is not None:
        raise HTTPException(
            status_code=422, detail="modified_args is only valid with decision 'modify'"
        )

    _status_for: dict[str, ApprovalStatus] = {
        "approve": ApprovalStatus.APPROVED,
        "reject": ApprovalStatus.REJECTED,
        "modify": ApprovalStatus.MODIFIED,
    }
    return await resolve_approval_decision(
        tenant_id=tenant_id,
        actor_id=actor_id,
        caller_user_id=caller_user_id,
        # Stream MCP-OAUTH (OA-3b) — per-user OAuth MCP pool key.
        oauth_user_id=request.state.principal.subject_id,
        thread_id=thread_id,
        run_id=run_id,
        graph_decision=decision,
        db_status=_status_for[decision],
        modified_args=modified_args,
        reason=reason,
        threads=threads,
        audit=audit,
        agent_repo=agent_repo,
        runtime=runtime,
        approvals=approvals,
        idempotency_key=idempotency_key,
        agent_disable_service=getattr(request.app.state, "agent_disable_service", None),
        tenant_status_service=getattr(request.app.state, "tenant_status_service", None),
        # RT-6 Tier B (RT-ADR-20) — workspace drift signal on the decision audit.
        workspace_store=getattr(request.app.state, "user_workspace_store", None),
    )


async def _workspace_drift(
    workspace_store: UserWorkspaceStore | None,
    *,
    tenant_id: UUID,
    user_id: UUID | None,
    reason_kind: str,
    requested_at: datetime,
) -> bool:
    """RT-6 Tier B (RT-ADR-20) — did a workspace-write-capable tool run since the
    approval was requested? (approve-then-swap-script).

    Audit-only / forensic — every failure is swallowed to ``False`` so a purely
    informational read can never block a resume or a status fetch. Scoped to
    declarative-gate (``policy_gate``) approvals bound to a user workspace; a
    ``NullWorkspaceLock`` deployment never bumps ``last_write_at`` so it's False.
    The signal is a conservative over-approximation — a read-only bash also bumps
    the lock — so it means "a mutating-capable tool ran", not a proven change.
    """
    if workspace_store is None or reason_kind != "policy_gate" or user_id is None:
        return False
    try:
        ws = await workspace_store.get(tenant_id=tenant_id, user_id=user_id)
        # The comparison stays INSIDE the try: a naive/aware datetime mismatch
        # raises TypeError, and this read must never 500 a status poll or wedge
        # a post-CAS resume — swallow everything to False (RT-6 audit-only).
        return ws is not None and ws.last_write_at is not None and ws.last_write_at > requested_at
    except Exception:
        # No request-derived value in the message (CodeQL py/log-injection).
        logger.warning("approval.workspace_drift_check_failed", exc_info=True)
        return False


async def resolve_approval_decision(
    *,
    tenant_id: UUID,
    actor_id: str,
    caller_user_id: UUID | None,
    oauth_user_id: str | None,
    thread_id: UUID,
    run_id: UUID,
    graph_decision: Literal["approve", "reject", "modify"],
    db_status: ApprovalStatus,
    modified_args: dict[str, Any] | None,
    reason: str | None,
    threads: Any,
    audit: AuditLogger,
    agent_repo: AgentSpecStore,
    runtime: AgentRuntime,
    approvals: ApprovalStore,
    idempotency_key: str | None = None,
    agent_disable_service: AgentDisableService | None = None,
    tenant_status_service: TenantStatusService | None = None,
    workspace_store: UserWorkspaceStore | None = None,
) -> tuple[Any, UUID, bool]:
    """Request-free core of a J.8 approval verdict — CAS + checkpoint + spawn.

    Stream 9.5 — extracted from :func:`apply_approval_decision` so the
    ``ApprovalTimeoutSweep`` worker shares the exact same continuation path as
    the human endpoints. The caller is responsible for *authorising* the verdict
    (the HTTP wrapper checks thread ownership; the timeout sweep is a trusted
    system actor); this core does the ``mark_decided`` CAS (exactly-once across
    instances), the ``APPROVAL_DECIDED`` audit, the checkpoint ``aupdate_state``,
    and the detached continuation worker.

    ``graph_decision`` is what the graph applies (a timeout maps to ``reject``);
    ``db_status`` is the row's terminal status (``TIMEOUT`` for the sweep) — they
    differ only for the auto-timeout path. Returns ``(run_record,
    continuation_run_id, replayed)`` with the same semantics as the wrapper.
    """
    trace_id = current_trace_id_hex()
    approval = await approvals.get_by_run(run_id=run_id, tenant_id=tenant_id)
    if approval is None:
        raise HTTPException(status_code=404, detail="run not found")
    if approval.status is not ApprovalStatus.PENDING:
        # Stream 13.2 — already decided. Replay idempotently iff the caller's
        # key matches the one stored with the original decision; otherwise it
        # is a genuine conflict (409).
        replay = _idempotent_continuation(approval, idempotency_key)
        if replay is not None:
            return None, replay, True
        raise HTTPException(
            status_code=409,
            detail=f"approval already decided ({approval.status.value})",
        )

    meta = await threads.get(thread_id, tenant_id=tenant_id)
    if meta is None or meta.agent_name is None or meta.agent_version is None:
        raise HTTPException(status_code=409, detail="session is not bound to an agent")
    # Stream RT-4 (RT-ADR-16) — THE spawn choke point for every approval
    # continuation: HTTP resume, batch decide, AND the timeout sweep all route
    # here. Gate BEFORE the ``mark_decided`` CAS so a disabled agent / suspended
    # tenant leaves the approval PENDING (fully reversible) — never consumes the
    # decision only to 403 the spawn. Callers run in the tenant RLS scope.
    blocked = await run_block_reason(
        tenant_status=tenant_status_service,
        agent_disable=agent_disable_service,
        tenant_id=tenant_id,
        agent_name=meta.agent_name,
    )
    if blocked is not None:
        raise HTTPException(status_code=403, detail=blocked.upper())

    # RT-6 Tier A (RT-ADR-19) — the binding the graph re-verifies before
    # dispatch. A ``modify`` re-binds to the digest of the modified args (stored
    # atomically with the CAS below); ``approve`` / ``reject`` keep the mint-time
    # digest already on the row. Threaded into ``approval_resume`` so the graph
    # compares it against the about-to-dispatch tool_call.
    if graph_decision == "modify" and modified_args is not None:
        expected_digest = canonical_args_digest(modified_args)
        rebind_digest: str | None = expected_digest
    else:
        expected_digest = approval.binding_digest
        rebind_digest = None

    # Stream 13.2 — generate the continuation id BEFORE the CAS so it is bound
    # atomically to the winning decision; a retry / lost-race caller reads it
    # back to replay the same continuation.
    continuation_run_id = uuid4()
    decided = await approvals.mark_decided(
        run_id=run_id,
        tenant_id=tenant_id,
        status=db_status,
        decided_by=actor_id,
        decided_at=datetime.now(UTC),
        modified_args=modified_args,
        idempotency_key=idempotency_key,
        continuation_run_id=continuation_run_id,
        binding_digest=rebind_digest,
    )
    # ``mark_decided`` returns False on a lost race — another resume, a peer
    # timeout sweep, or the human endpoint decided it between our get + update.
    if not decided:
        # Stream 13.2 — re-read the winner's row; if it carries our key, replay
        # its continuation (idempotent). Otherwise it is a real conflict (409).
        loser = await approvals.get_by_run(run_id=run_id, tenant_id=tenant_id)
        replay = _idempotent_continuation(loser, idempotency_key)
        if replay is not None:
            return None, replay, True
        raise HTTPException(status_code=409, detail="approval already decided")

    # RT-6 Tier B (RT-ADR-20) — did a workspace-write-capable tool run between
    # the approval request and this verdict? Audit-only, never blocks. This runs
    # post-CAS / pre-spawn, where a raising read would wedge a legitimately-
    # approved run for good (the decision is already consumed; a retry replays
    # without re-spawning) — the helper swallows every failure to False.
    workspace_drift = await _workspace_drift(
        workspace_store,
        tenant_id=tenant_id,
        user_id=approval.user_id,
        reason_kind=approval.reason_kind,
        requested_at=approval.requested_at,
    )
    await emit(
        audit,
        tenant_id=tenant_id,
        actor_id=actor_id,
        action=AuditAction.APPROVAL_DECIDED,
        resource_type="approval",
        resource_id=str(run_id),
        trace_id=trace_id,
        details={
            "thread_id": str(thread_id),
            "decision": graph_decision,
            "status": db_status.value,
            "request_id": approval.request_id,
            "workspace_drift": workspace_drift,
        },
    )

    # Deletion hygiene PR4 — same 410-over-404 split as the run-start path: a
    # soft-DELETED agent's approval continuation is refused with a precise 410.
    spec_record = await agent_repo.get(
        tenant_id=tenant_id,
        name=meta.agent_name,
        version=meta.agent_version,
        include_deleted=True,
    )
    if spec_record is None:
        raise HTTPException(
            status_code=404,
            detail=f"agent {meta.agent_name}@{meta.agent_version} not found",
        )
    if spec_record.status is AgentSpecStatus.DELETED:
        raise HTTPException(
            status_code=410,
            detail={
                "code": "AGENT_DELETED",
                "message": f"agent {meta.agent_name}@{meta.agent_version} has been deleted",
            },
        )
    try:
        built = await runtime.get_agent(
            tenant_id=tenant_id,
            name=meta.agent_name,
            version=meta.agent_version,
            spec=spec_record.spec,
            user_id=oauth_user_id,
        )
    except AgentFactoryError as exc:
        raise HTTPException(
            status_code=422, detail=f"agent manifest cannot be built: {exc}"
        ) from exc

    # Write the verdict into the paused thread's checkpoint. ``as_node=
    # "agent"`` re-positions the graph as if the agent had just run,
    # so the next step evaluates the agent's conditional edge — the
    # last message still carries the gated tool_calls → routes to
    # ``tools``, where ``approval_resume`` is applied.
    checkpoint_config: RunnableConfig = {
        "configurable": {"thread_id": str(thread_id), "tenant_id": str(tenant_id)}
    }
    await built.graph.aupdate_state(  # type: ignore[attr-defined]
        checkpoint_config,
        {
            "pending_approval": None,
            "approval_resume": {
                "decision": graph_decision,
                "modified_args": modified_args,
                "reason": reason,
                # RT-6 Tier A (RT-ADR-19) — the graph re-hashes the dispatched
                # args and matches them against this; drift → integrity veto.
                "binding_digest": expected_digest,
            },
        },
        as_node="agent",
    )

    # Spawn a continuation worker for the CAS winner. ``continuation_run_id``
    # was generated + stored atomically with the decision above. RunManager
    # tracks it as a new run; the checkpoint (keyed by thread_id) is the
    # continuity. ``graph_input=None`` resumes from the checkpoint.
    run_record = await runtime.run_manager.create(
        run_id=continuation_run_id,
        thread_id=thread_id,
        tenant_id=tenant_id,
        user_id=caller_user_id,
        is_resume=True,
        trace_id=trace_id,  # Mini-ADR H-9.5
    )
    # SE-7d-3b-ii — carry build-time distilled skills to the terminal hook.
    run_record.bound_distilled_skills = built.bound_distilled_skills
    config: RunnableConfig = {
        "configurable": {
            "thread_id": str(thread_id),
            "tenant_id": str(tenant_id),
            "run_id": str(continuation_run_id),
        }
    }
    if caller_user_id is not None:
        config["configurable"]["user_id"] = str(caller_user_id)  # type: ignore[index]
        current_user_id_var.set(caller_user_id)
    # MCP-OAUTH (OA-3b-后续) — carry the OAuth subject so a delegated sub-agent /
    # worker can resolve the same per-user OAuth pool (distinct from user_id).
    if oauth_user_id is not None:
        config["configurable"]["oauth_user_id"] = oauth_user_id  # type: ignore[index]
    worker = asyncio.create_task(
        run_agent(
            bridge=runtime.stream_bridge,
            run_manager=runtime.run_manager,
            record=run_record,
            graph=built.graph,  # type: ignore[arg-type]
            graph_input=None,
            config=config,
            audit_logger=audit,
            approval_store=approvals,
            # Stream H.3 PR 3 — durable SSE mirror.
            event_store=runtime.run_event_store,
            skill_run_usage_recorder=runtime.skill_run_usage_recorder,
            # Stream L.L7 — record the trajectory (curation / eval-gate source).
            trajectory_recorder=runtime.trajectory_recorder,
            trajectory_enabled=built.trajectory_recording,
            token_budget=built.token_budget,
            worker_spawn_budget=await runtime.new_worker_spawn_budget(),
            # perf phase2 PR3 T3 — process-wide delegation concurrency gate.
            delegation_gate=runtime.delegation_gate(),
            # Stream HX-3 — replay-safety resolver for transient retry.
            tool_replay_safe=built.tool_replay_safe,
        )
    )
    await runtime.run_manager.attach_task(continuation_run_id, worker)
    return run_record, continuation_run_id, False


async def spawn_run(
    *,
    runtime: AgentRuntime,
    audit: AuditLogger,
    approvals: ApprovalStore,
    request: Request,
    settings: Settings,
    built: BuiltAgent,
    record_spec: AgentSpec,
    thread_id: UUID,
    tenant_id: UUID,
    actor_id: str,
    effective_user_id: UUID | None,
    oauth_subject: str,
    payload: RunRequest,
    trace_id: str,
    extra_headers: dict[str, str] | None = None,
    on_behalf_of: str | None = None,
) -> StreamingResponse | JSONResponse:
    """Register + spawn one run, returning the SSE stream (or 202 for queue mode).

    Extracted from ``trigger_run`` so both the per-session run endpoint and the
    external per-user run endpoint (Stream Agent-Templates M1-5b) share the exact
    spawn / SSE / queue logic. ``effective_user_id`` is the user the run is scoped
    to — the long-term-memory RLS, the workspace volume, and per-user token
    accounting all key on it (the caller for a normal session run; the minted
    end-user for an on-behalf-of external run). ``oauth_subject`` keys the per-user
    OAuth MCP pool. ``on_behalf_of`` records the end-user when a machine principal
    acts for one."""
    # Stream J.6 — enforce image-ref invariants before any side effects.
    _validate_image_refs(
        payload.image_refs,
        tenant_id=tenant_id,
        thread_id=thread_id,
        supports_vision=built.supports_vision,
        has_vision_block=record_spec.spec.vision is not None,
        max_per_run=settings.multimodal_max_images_per_run,
    )

    # Stream Dynamic-Prompt — validate run inputs against the agent's declared
    # variables BEFORE any side effect (queue mode rejects synchronously too).
    try:
        validate_prompt_inputs(built, payload.inputs)
    except PromptRenderError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None

    await emit(
        audit,
        tenant_id=tenant_id,
        actor_id=actor_id,
        action=AuditAction.SESSION_WRITE,
        resource_type="session",
        resource_id=str(thread_id),
        trace_id=trace_id,
        details={
            "stage": "run.start",
            "input_len": len(payload.input or ""),
            # Dynamic-Prompt safety net: which declared variables rendered.
            # Names only — never values — so audit stays free of PII/secrets
            # (CodeQL clear-text-logging) while staying reproducible from the
            # template + the caller's own ``inputs``.
            **(
                {"prompt_var_names": [v.name for v in built.prompt_variables]}
                if built.prompt_jinja
                else {}
            ),
        },
        on_behalf_of=on_behalf_of,
    )

    run_id = uuid4()
    prior_runs = await runtime.run_manager.list_by_thread(thread_id, tenant_id=tenant_id)

    # Stream 9.5 — queue mode: persist as ``queued`` + return 202.
    if payload.mode == "queue":
        await runtime.run_manager.enqueue(
            run_id=run_id,
            thread_id=thread_id,
            tenant_id=tenant_id,
            user_id=effective_user_id,
            enqueued_input={
                "input": payload.input,
                "image_refs": payload.image_refs,
                "untrusted_content": payload.untrusted_content,
                "inputs": payload.inputs,
            },
            is_resume=bool(prior_runs),
            trace_id=trace_id,
        )
        logger.info("control_plane.run.enqueued run_id=%s", run_id)
        return JSONResponse(
            status_code=202,
            content={"run_id": str(run_id), "thread_id": str(thread_id), "status": "queued"},
        )

    run_record = await runtime.run_manager.create(
        run_id=run_id,
        thread_id=thread_id,
        tenant_id=tenant_id,
        user_id=effective_user_id,
        is_resume=bool(prior_runs),
        trace_id=trace_id,
    )
    run_record.bound_distilled_skills = built.bound_distilled_skills
    graph_input = build_run_graph_input(
        built,
        input_text=payload.input,
        image_refs=payload.image_refs,
        untrusted_content=payload.untrusted_content,
        inputs=payload.inputs,
    )
    configurable: dict[str, Any] = {
        "thread_id": str(thread_id),
        "tenant_id": str(tenant_id),
        "run_id": str(run_id),
    }
    if effective_user_id is not None:
        configurable["user_id"] = str(effective_user_id)
        # Stream J.3 — carry the user scope into the worker's context so the
        # long-term-memory store's user-level RLS applies (inherited by the task).
        current_user_id_var.set(effective_user_id)
    # MCP-OAUTH (OA-3b-后续) — the OAuth subject (per-user OAuth pool key).
    configurable["oauth_user_id"] = oauth_subject
    if built.run_deadline_s > 0:
        configurable["deadline_at"] = time.monotonic() + float(built.run_deadline_s)
    config: RunnableConfig = {"configurable": configurable}
    worker = asyncio.create_task(
        run_agent(
            bridge=runtime.stream_bridge,
            run_manager=runtime.run_manager,
            record=run_record,
            graph=built.graph,  # type: ignore[arg-type]
            graph_input=graph_input,
            config=config,
            audit_logger=audit,
            approval_store=approvals,
            event_store=runtime.run_event_store,
            skill_run_usage_recorder=runtime.skill_run_usage_recorder,
            trajectory_recorder=runtime.trajectory_recorder,
            trajectory_enabled=built.trajectory_recording,
            token_budget=built.token_budget,
            worker_spawn_budget=await runtime.new_worker_spawn_budget(),
            # perf phase2 PR3 T3 — process-wide delegation concurrency gate.
            delegation_gate=runtime.delegation_gate(),
            tool_replay_safe=built.tool_replay_safe,
        )
    )
    await runtime.run_manager.attach_task(run_id, worker)
    logger.info("control_plane.run.started run_id=%s", run_id)

    headers = {
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
        "X-Expert-Work-Run-Id": str(run_id),
    }
    if extra_headers:
        headers.update(extra_headers)
    return StreamingResponse(
        sse_consumer(
            bridge=runtime.stream_bridge,
            record=run_record,
            run_manager=runtime.run_manager,
            is_disconnected=request.is_disconnected,
            last_event_id=request.headers.get("Last-Event-ID"),
        ),
        media_type="text/event-stream",
        headers=headers,
    )


def build_runs_router() -> APIRouter:
    router = APIRouter(prefix="/v1/sessions", tags=["sessions"])

    @router.post(
        "/{thread_id}/runs",
        response_model=None,
        dependencies=[Depends(require_key_scope("write")), Depends(console_only())],
    )
    async def trigger_run(
        thread_id: UUID,
        payload: RunRequest,
        request: Request,
        threads: Annotated[object, Depends(_get_thread_repo)],
        users: Annotated[TenantUserStore, Depends(get_user_repo)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
        quota: Annotated[QuotaService, Depends(_get_quota)],
        agent_repo: Annotated[AgentSpecStore, Depends(_get_agent_repo)],
        runtime: Annotated[AgentRuntime, Depends(_get_agent_runtime)],
        settings: Annotated[Settings, Depends(_get_settings)],
        approvals: Annotated[ApprovalStore, Depends(_get_approval_store)],
    ) -> StreamingResponse | JSONResponse:
        tenant_id: UUID = request.state.tenant_id
        actor_id: str = request.state.actor_id
        trace_id = current_trace_id_hex()

        # Stream U (PR E) — defense in depth. AuthMiddleware already 403s a
        # suspended tenant's members, but the run-creation path is the one we
        # most want to never serve for a suspended tenant, so re-check here.
        # ``getattr`` guards test setups that don't wire the service.
        status_svc = getattr(request.app.state, "tenant_status_service", None)
        if status_svc is not None and await status_svc.is_suspended(tenant_id):
            return JSONResponse(
                status_code=403,
                content={
                    "success": False,
                    "data": None,
                    "error": {
                        "code": "TENANT_SUSPENDED",
                        "message": "this tenant is suspended",
                    },
                },
            )

        # Stream K.K2 (Mini-ADR K-2) — SSE cross-tenant safety lives here.
        # ``threads.get(thread_id, tenant_id=tenant_id)`` 404s when the
        # thread belongs to a different tenant, so the SSE stream never
        # opens for a cross-tenant caller. No duplicate guard at the
        # SSE layer (Mini-ADR K-2); the invariant is locked by
        # tests/test_runs_api.py::test_runs_cross_tenant_sse_rejected.
        meta = await threads.get(thread_id, tenant_id=tenant_id)  # type: ignore[attr-defined]
        if meta is None:
            raise HTTPException(status_code=404, detail="session not found")
        # Stream J.14 — a user-owned thread accepts runs only from its
        # owner (or an admin); 404 so cross-user existence stays hidden.
        caller_user_id = await resolve_caller_user_id(request, users)
        if not caller_owns_thread(
            meta=meta, caller_user_id=caller_user_id, principal=request.state.principal
        ):
            raise HTTPException(status_code=404, detail="session not found")
        # Stream R (R-8) — first run promotes an invited member to active.
        await ensure_member_active(request, caller_user_id=caller_user_id)
        if meta.status is not ThreadStatus.ACTIVE:
            raise HTTPException(
                status_code=409,
                detail=f"session is {meta.status.value}; only active sessions accept runs",
            )
        if meta.agent_name is None or meta.agent_version is None:
            raise HTTPException(status_code=409, detail="session is not bound to an agent")

        # Stream RT-4 (RT-ADR-16) — agent kill switch. Reject a new run for a
        # disabled agent (defense in depth alongside the tenant-suspend gate
        # above). ``getattr`` guards test setups that don't wire the service.
        disable_svc = getattr(request.app.state, "agent_disable_service", None)
        if disable_svc is not None and await disable_svc.is_disabled(tenant_id, meta.agent_name):
            return JSONResponse(
                status_code=403,
                content={
                    "success": False,
                    "data": None,
                    "error": {
                        "code": "AGENT_DISABLED",
                        "message": "this agent is disabled",
                    },
                },
            )

        # Admission (Stream C.5b): bucket the run against the bound
        # agent. Denial returns 429 + Retry-After and audits — no stream.
        denial = await check_admission(
            quota=quota,
            audit=audit,
            tenant_id=tenant_id,
            actor_id=actor_id,
            agent=meta.agent_name,
            resource_kind="run",
        )
        if denial is not None:
            return denial

        # Load the agent manifest + build (cache-hit) a runnable agent.
        # Deletion hygiene PR4 — look up including soft-deleted rows so a
        # DELETED agent gets a precise 410 instead of a generic 404.
        record = await agent_repo.get(
            tenant_id=tenant_id,
            name=meta.agent_name,
            version=meta.agent_version,
            include_deleted=True,
        )
        if record is None:
            raise HTTPException(
                status_code=404,
                detail=f"agent {meta.agent_name}@{meta.agent_version} not found",
            )
        if record.status is AgentSpecStatus.DELETED:
            raise HTTPException(
                status_code=410,
                detail={
                    "code": "AGENT_DELETED",
                    "message": f"agent {meta.agent_name}@{meta.agent_version} has been deleted",
                },
            )
        try:
            built = await runtime.get_agent(
                tenant_id=tenant_id,
                name=meta.agent_name,
                version=meta.agent_version,
                spec=record.spec,
                # Stream MCP-OAUTH (OA-3b) — subject_id keys the per-user OAuth
                # MCP pool (= mcp_oauth_connection.user_id).
                user_id=request.state.principal.subject_id,
            )
        except AgentFactoryError as exc:
            raise HTTPException(
                status_code=422, detail=f"agent manifest cannot be built: {exc}"
            ) from exc

        # Session-history — auto-title the thread from its first user message.
        # Only when unset, so a manual rename (PATCH) is never clobbered by a
        # later run. Best-effort: a title failure must not block the run.
        if getattr(meta, "title", None) is None and payload.input:
            auto_title = title_from_text(payload.input)
            if auto_title:
                try:
                    await threads.update_title(  # type: ignore[attr-defined]
                        thread_id, auto_title, tenant_id=tenant_id
                    )
                except Exception:
                    logger.warning("session.auto_title_failed", exc_info=True)

        # Stream Agent-Templates (M1-5b-2) — the spawn / SSE / queue logic is
        # shared with the external per-user run endpoint. A normal session run is
        # scoped to its caller; the OAuth subject keys the per-user OAuth pool.
        return await spawn_run(
            runtime=runtime,
            audit=audit,
            approvals=approvals,
            request=request,
            settings=settings,
            built=built,
            record_spec=record.spec,
            thread_id=thread_id,
            tenant_id=tenant_id,
            actor_id=actor_id,
            effective_user_id=caller_user_id,
            oauth_subject=request.state.principal.subject_id,
            payload=payload,
            trace_id=trace_id,
        )

    @router.get(
        "/{thread_id}/runs/{run_id}",
        response_model=None,
        dependencies=[Depends(require_key_scope("read")), Depends(console_only())],
    )
    async def get_run(
        thread_id: UUID,
        run_id: UUID,
        request: Request,
        threads: Annotated[object, Depends(_get_thread_repo)],
        users: Annotated[TenantUserStore, Depends(get_user_repo)],
        approvals: Annotated[ApprovalStore, Depends(_get_approval_store)],
        runs: Annotated[RunStore, Depends(_get_run_store)],
        token_usage: Annotated[TokenUsageStore, Depends(_get_token_usage_store)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
        # W2 read scope — a concrete id lets a system_admin drill into a
        # foreign tenant's run from the tenant switcher; "*" is meaningless
        # (a run belongs to one tenant).
        tenant_id: Annotated[UUID | Literal["*"] | None, Query()] = None,
    ) -> JSONResponse:
        """Stream J.8 — a run's status + any pending approval.

        Reads the durable ``agent_run`` row (Mini-ADR J-41) so a run's
        status survives the in-memory RunManager's 5-minute TTL and a
        control-plane restart; the ``agent_approval`` row carries any
        pending verdict. 404 hides cross-tenant / cross-user existence,
        identical to ``trigger_run``.
        """
        scope = await ensure_single_tenant_scope(
            request.state.principal,
            tenant_id,
            audit,
            trace_id=current_trace_id_hex(),
            endpoint="GET /v1/sessions/{thread_id}/runs/{run_id}",
            cross_tenant_enabled=cross_tenant_query_enabled(request),
        )
        target_tenant = scope.tenant_id
        async with applied_scope(scope):
            meta = await threads.get(thread_id, tenant_id=target_tenant)  # type: ignore[attr-defined]
        if meta is None:
            raise HTTPException(status_code=404, detail="session not found")
        caller_user_id = await resolve_caller_user_id(request, users)
        if not caller_owns_thread(
            meta=meta, caller_user_id=caller_user_id, principal=request.state.principal
        ):
            raise HTTPException(status_code=404, detail="session not found")

        async with applied_scope(scope):
            approval = await approvals.get_by_run(run_id=run_id, tenant_id=target_tenant)
            pending: dict[str, Any] | None = None
            if approval is not None and approval.status is ApprovalStatus.PENDING:
                # RT-6 Tier B — compute the workspace-drift signal live so the
                # review card can warn the human *before* they decide (the
                # resume-time audit is too late for the pending view).
                # Best-effort inside the helper.
                drift = await _workspace_drift(
                    getattr(request.app.state, "user_workspace_store", None),
                    tenant_id=target_tenant,
                    user_id=approval.user_id,
                    reason_kind=approval.reason_kind,
                    requested_at=approval.requested_at,
                )
                pending = {
                    "request_id": approval.request_id,
                    "node": approval.node,
                    "reason_kind": approval.reason_kind,
                    "action_summary": approval.action_summary,
                    "proposed_args": approval.proposed_args,
                    "requested_at": approval.requested_at.isoformat(),
                    "timeout_at": approval.timeout_at.isoformat(),
                    # RT-6 Tier A — the approved args fingerprint receipt (empty
                    # for a legacy / unbound or action-screen approval).
                    "binding_digest": approval.binding_digest,
                    # RT-6 Tier B — workspace mutated since the request
                    # (audit-only).
                    "workspace_drift": drift,
                }
            # Status resolution (Mini-ADR J-41): the in-memory RunManager is
            # authoritative while the run is live, but its record is dropped
            # 5 minutes after the run ends — and on a control-plane restart.
            # The durable ``agent_run`` row is the fallback, so a finished
            # run stays queryable past the TTL instead of 404-ing.
            run_status = runtime_run_status(request, run_id)
            # Mini-ADR H-9.5 — surface the persisted trace_id when the agent_run
            # row exists. The in-memory record carries it for live runs; the
            # durable row carries it past the TTL.
            persisted = await runs.get(run_id=run_id, tenant_id=target_tenant)
            trace_id: str | None = persisted.trace_id if persisted is not None else None
            if run_status is None:
                if persisted is not None:
                    run_status = persisted.status.value
            if run_status is None and approval is None:
                raise HTTPException(status_code=404, detail="run not found")
            status = run_status or (approval.status.value if approval is not None else "unknown")
            # Run summary — token usage joined by trace_id (expert_work's own
            # token_usage, no Langfuse round-trip). Scoped to the resolved
            # target tenant so RLS applies (token_usage isolation rides on the
            # tenant GUC, set by applied_scope).
            tokens: dict[str, Any] | None = None
            if trace_id is not None:
                totals = await token_usage.totals_by_trace_ids([trace_id])
                tokens = _tokens_to_dict(totals.get(trace_id))
        return JSONResponse(
            content={
                "run_id": str(run_id),
                "thread_id": str(thread_id),
                "status": status,
                "pending_approval": pending,
                "trace_id": trace_id,
                "tokens": tokens,
                # Timestamps from the durable row (None when the run is only in
                # the in-memory RunManager) — the detail summary derives duration.
                "created_at": (persisted.created_at.isoformat() if persisted is not None else None),
                "finished_at": (
                    persisted.finished_at.isoformat()
                    if persisted is not None and persisted.finished_at is not None
                    else None
                ),
            }
        )

    @router.get(
        "/{thread_id}/runs/{run_id}/trace",
        response_model=None,
        dependencies=[Depends(require_key_scope("read")), Depends(console_only())],
    )
    async def get_run_trace(
        thread_id: UUID,
        run_id: UUID,
        request: Request,
        threads: Annotated[object, Depends(_get_thread_repo)],
        users: Annotated[TenantUserStore, Depends(get_user_repo)],
        runs: Annotated[RunStore, Depends(_get_run_store)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
        # W2 read scope — see ``get_run``.
        tenant_id: Annotated[UUID | Literal["*"] | None, Query()] = None,
    ) -> JSONResponse:
        """Batch 4b Task 2 — the run's Langfuse trace, normalized for the
        debug console's "precise" view.

        Ownership-gated identically to ``get_run`` (404 hides cross-tenant
        / cross-user existence); unlike ``get_run`` this does NOT require
        system_admin — it only ever returns the caller's own run's trace,
        never a cross-tenant one (a system_admin crosses via ``?tenant_id=``).
        """
        scope = await ensure_single_tenant_scope(
            request.state.principal,
            tenant_id,
            audit,
            trace_id=current_trace_id_hex(),
            endpoint="GET /v1/sessions/{thread_id}/runs/{run_id}/trace",
            cross_tenant_enabled=cross_tenant_query_enabled(request),
        )
        target_tenant = scope.tenant_id
        async with applied_scope(scope):
            meta = await threads.get(thread_id, tenant_id=target_tenant)  # type: ignore[attr-defined]
        if meta is None:
            raise HTTPException(status_code=404, detail="session not found")
        caller_user_id = await resolve_caller_user_id(request, users)
        if not caller_owns_thread(
            meta=meta, caller_user_id=caller_user_id, principal=request.state.principal
        ):
            raise HTTPException(status_code=404, detail="session not found")

        async with applied_scope(scope):
            persisted = await runs.get(run_id=run_id, tenant_id=target_tenant)
        trace_id = persisted.trace_id if persisted is not None else None
        if trace_id is None:
            return JSONResponse(content={"status": "no_trace"})

        client = getattr(request.app.state, "langfuse_read_client", None)
        return JSONResponse(content=fetch_and_normalize(client, trace_id))

    @router.get(
        "/{thread_id}/runs/{run_id}/trace/raw",
        response_model=None,
        dependencies=[Depends(require_key_scope("read")), Depends(console_only())],
    )
    async def get_run_trace_raw(
        thread_id: UUID,
        run_id: UUID,
        request: Request,
        threads: Annotated[object, Depends(_get_thread_repo)],
        users: Annotated[TenantUserStore, Depends(get_user_repo)],
        runs: Annotated[RunStore, Depends(_get_run_store)],
        span: Annotated[str, Query()],
        field: Annotated[str, Query()],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
        # W2 read scope — see ``get_run``.
        tenant_id: Annotated[UUID | Literal["*"] | None, Query()] = None,
    ) -> JSONResponse:
        """Task 4 —— 单 span input/output 的未截断全文("查看原文").

        Ownership-gated identically to ``get_run_trace`` (404 hides
        cross-tenant / cross-user existence). ``fetch_span_raw`` is
        best-effort — bad ``field``, unknown ``span``, no client, or a
        Langfuse outage all degrade to ``None``, which this route turns
        into a 404 rather than ever 500ing.
        """
        scope = await ensure_single_tenant_scope(
            request.state.principal,
            tenant_id,
            audit,
            trace_id=current_trace_id_hex(),
            endpoint="GET /v1/sessions/{thread_id}/runs/{run_id}/trace/raw",
            cross_tenant_enabled=cross_tenant_query_enabled(request),
        )
        target_tenant = scope.tenant_id
        async with applied_scope(scope):
            meta = await threads.get(thread_id, tenant_id=target_tenant)  # type: ignore[attr-defined]
        if meta is None:
            raise HTTPException(status_code=404, detail="session not found")
        caller_user_id = await resolve_caller_user_id(request, users)
        if not caller_owns_thread(
            meta=meta, caller_user_id=caller_user_id, principal=request.state.principal
        ):
            raise HTTPException(status_code=404, detail="session not found")

        async with applied_scope(scope):
            persisted = await runs.get(run_id=run_id, tenant_id=target_tenant)
        trace_id = persisted.trace_id if persisted is not None else None
        if trace_id is None:
            raise HTTPException(status_code=404, detail="trace not found")

        client = getattr(request.app.state, "langfuse_read_client", None)
        content = fetch_span_raw(client, trace_id, span, field)
        if content is None:
            raise HTTPException(status_code=404, detail="span not found")
        return JSONResponse(content={"spanId": span, "field": field, "content": content})

    @router.get(
        "/{thread_id}/messages",
        response_model=None,
        dependencies=[Depends(require_key_scope("read")), Depends(console_only())],
    )
    async def get_thread_messages(
        thread_id: UUID,
        request: Request,
        threads: Annotated[object, Depends(_get_thread_repo)],
        users: Annotated[TenantUserStore, Depends(get_user_repo)],
        runtime: Annotated[AgentRuntime, Depends(_get_agent_runtime)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
        # Conversation-centric IA fast-follow — a concrete id lets a
        # system_admin read a foreign tenant's transcript when drilling in
        # from the cross-tenant conversation browser; "*" is meaningless
        # (a thread belongs to one tenant).
        tenant_id: Annotated[UUID | None, Query()] = None,
    ) -> JSONResponse:
        """Playground resume (#6) — the thread's conversation history.

        Reads the thread's durable LangGraph checkpoint (keyed by ``thread_id``)
        DIRECTLY off the checkpointer — no agent rebuild. The previous version
        called ``runtime.get_agent(...).graph.aget_state(...)``, which coupled a
        read-only history view to a full (slow, fragile) agent build whose graph
        could end up bound to a different checkpointer than the durable one —
        silently returning an empty list. Returns only user/assistant text
        turns; tool/system messages are omitted. Best-effort: any failure
        degrades to an empty list rather than erroring the page.
        """
        scope = await ensure_tenant_scope(
            request.state.principal,
            tenant_id,
            audit,
            trace_id=current_trace_id_hex(),
            endpoint="GET /v1/sessions/{thread_id}/messages",
            cross_tenant_enabled=cross_tenant_query_enabled(request),
        )
        if isinstance(scope, CrossTenant):
            raise HTTPException(
                status_code=422,
                detail="a thread belongs to one tenant; pass a concrete tenant_id",
            )
        target_tenant = scope.tenant_id
        meta = await threads.get(thread_id, tenant_id=target_tenant)  # type: ignore[attr-defined]
        if meta is None:
            raise HTTPException(status_code=404, detail="session not found")
        caller_user_id = await resolve_caller_user_id(request, users)
        if not caller_owns_thread(
            meta=meta, caller_user_id=caller_user_id, principal=request.state.principal
        ):
            raise HTTPException(status_code=404, detail="session not found")
        # Reading another tenant's conversation content is sensitive —
        # leave an audit row for the explicit cross-tenant drill-in (the
        # Playground's high-frequency self-reads stay unlogged).
        if target_tenant != request.state.tenant_id:
            await emit(
                audit,
                tenant_id=request.state.tenant_id,
                actor_id=request.state.actor_id,
                action=AuditAction.SESSION_READ,
                resource_type="session",
                resource_id=str(thread_id),
                result=AuditResult.SUCCESS,
                trace_id=current_trace_id_hex(),
                details={"view": "transcript", "target_tenant_id": str(target_tenant)},
            )

        empty = JSONResponse({"success": True, "data": {"messages": []}})
        checkpointer = runtime.durable_checkpointer
        if checkpointer is None:
            return empty
        try:
            # Shared extraction with the transcript mirror sweep (IA M4) —
            # one definition of "a transcript turn". RT-ADR-9: the bubble view
            # hides orchestrator scaffolding (``expert_work_hide_from_ui``), but the
            # cross-tenant audit drill-in (``target_tenant`` differs — the same
            # signal that emits the SESSION_READ audit row above) sees the
            # faithful transcript. The durable record + search mirror always do.
            is_cross_tenant_audit = target_tenant != request.state.tenant_id
            turns = await read_turns(checkpointer, thread_id, include_hidden=is_cross_tenant_audit)
        except Exception:
            logger.warning("thread_messages.read_failed", exc_info=True)
            return empty
        out = [{"role": t.role, "content": t.content, "channel": t.channel} for t in turns]
        return JSONResponse({"success": True, "data": {"messages": out}})

    @router.get(
        "/{thread_id}/runs",
        response_model=None,
        dependencies=[Depends(require_key_scope("read")), Depends(console_only())],
    )
    async def list_thread_runs(
        thread_id: UUID,
        request: Request,
        threads: Annotated[object, Depends(_get_thread_repo)],
        users: Annotated[TenantUserStore, Depends(get_user_repo)],
        runs: Annotated[RunStore, Depends(_get_run_store)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
        tenant_id: Annotated[UUID | None, Query()] = None,
    ) -> JSONResponse:
        """Playground history reconstruction — the thread's runs, oldest-first.

        Lets the debug console lazily replay each past run's event stream
        (``GET .../runs/{run_id}/events``) to rebuild a full historical turn.
        Ownership-gated identically to ``get_thread_messages``; a concrete
        ``tenant_id`` lets a system_admin read a foreign tenant's runs.
        Returns ``run_id`` / ``status`` / ``is_resume`` / ``created_at`` only —
        the debug payload lives in the per-run event replay, not here.
        """
        scope = await ensure_tenant_scope(
            request.state.principal,
            tenant_id,
            audit,
            trace_id=current_trace_id_hex(),
            endpoint="GET /v1/sessions/{thread_id}/runs",
            cross_tenant_enabled=cross_tenant_query_enabled(request),
        )
        if isinstance(scope, CrossTenant):
            raise HTTPException(
                status_code=422,
                detail="a thread belongs to one tenant; pass a concrete tenant_id",
            )
        target_tenant = scope.tenant_id
        meta = await threads.get(thread_id, tenant_id=target_tenant)  # type: ignore[attr-defined]
        if meta is None:
            raise HTTPException(status_code=404, detail="session not found")
        caller_user_id = await resolve_caller_user_id(request, users)
        if not caller_owns_thread(
            meta=meta, caller_user_id=caller_user_id, principal=request.state.principal
        ):
            raise HTTPException(status_code=404, detail="session not found")

        # Best-effort read (spec: store 无/异常 → return {"runs": []}, not 500,
        # mirroring get_thread_messages). The ownership gate above stays OUTSIDE
        # this try — a non-owned thread must still 404, not degrade to empty.
        try:
            rows = await runs.list_by_thread(thread_id=thread_id, tenant_id=target_tenant)
            out = [
                {
                    "run_id": str(r.run_id),
                    "status": r.status.value,
                    "is_resume": r.is_resume,
                    "created_at": r.created_at.isoformat(),
                }
                for r in rows
            ]
        except Exception:
            logger.warning("thread_runs.read_failed", exc_info=True)
            return JSONResponse({"success": True, "data": {"runs": []}})
        return JSONResponse({"success": True, "data": {"runs": out}})

    @router.get(
        "/{thread_id}/runs/{run_id}/events",
        response_model=None,
        dependencies=[Depends(require_key_scope("read")), Depends(console_only())],
    )
    async def stream_run_events(
        thread_id: UUID,
        run_id: UUID,
        request: Request,
        threads: Annotated[object, Depends(_get_thread_repo)],
        users: Annotated[TenantUserStore, Depends(get_user_repo)],
        runs: Annotated[RunStore, Depends(_get_run_store)],
        event_store: Annotated[RunEventStore | None, Depends(_get_run_event_store)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
        since_seq: Annotated[int | None, Query(ge=0)] = None,
        # W2 read scope — resolved BEFORE the stream is built, so 403/400 are
        # plain HTTP errors (never SSE frames); see ``get_run``.
        tenant_id: Annotated[UUID | Literal["*"] | None, Query()] = None,
    ) -> StreamingResponse:
        """Stream H.3 PR 4 (Mini-ADR H-7) — SSE event stream for one run.

        Two backends, one wire format:

        * Active run (``RunStatus.PENDING`` / ``RUNNING``) → live attach
          via :meth:`StreamBridge.subscribe`. The bridge buffer holds up
          to 256 events (drop-oldest) so a late opener still catches the
          last 256 frames; older frames depend on the durable store.
        * Terminal run (``SUCCESS`` / ``ERROR`` / ``TIMEOUT`` /
          ``INTERRUPTED`` / ``PAUSED``) → replay via
          :meth:`RunEventStore.list` with ``since_seq`` (Last-Event-ID).

        Either way the response is ``text/event-stream`` with SSE id
        ``"{created_at_ms}-{seq}"`` so the client's parser doesn't have
        to know which mode it got (decision A).

        404 hides cross-tenant / cross-user existence, identical to
        ``get_run``.
        """
        scope = await ensure_single_tenant_scope(
            request.state.principal,
            tenant_id,
            audit,
            trace_id=current_trace_id_hex(),
            endpoint="GET /v1/sessions/{thread_id}/runs/{run_id}/events",
            cross_tenant_enabled=cross_tenant_query_enabled(request),
        )
        target_tenant = scope.tenant_id
        async with applied_scope(scope):
            meta = await threads.get(thread_id, tenant_id=target_tenant)  # type: ignore[attr-defined]
        if meta is None:
            raise HTTPException(status_code=404, detail="session not found")
        caller_user_id = await resolve_caller_user_id(request, users)
        if not caller_owns_thread(
            meta=meta, caller_user_id=caller_user_id, principal=request.state.principal
        ):
            raise HTTPException(status_code=404, detail="session not found")

        async with applied_scope(scope):
            persisted = await runs.get(run_id=run_id, tenant_id=target_tenant)
        if persisted is None:
            raise HTTPException(status_code=404, detail="run not found")

        # Active vs terminal — picks live attach vs replay.
        is_terminal = persisted.status in TERMINAL_RUN_STATUSES
        runtime: AgentRuntime = request.app.state.agent_runtime

        producer = build_event_producer(
            run_id=run_id,
            is_terminal=is_terminal,
            event_store=event_store,
            stream_bridge=runtime.stream_bridge,
            since_seq=since_seq,
            scope=lambda: applied_scope(scope),
        )
        return StreamingResponse(
            producer,
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "X-Expert-Work-Run-Id": str(run_id),
                "X-Expert-Work-Stream-Mode": "replay" if is_terminal else "live",
            },
        )

    @router.post(
        "/{thread_id}/runs/{run_id}/resume",
        response_model=None,
        dependencies=[Depends(require_key_scope("write")), Depends(console_only())],
    )
    async def resume_run(
        thread_id: UUID,
        run_id: UUID,
        payload: ResumeRequest,
        request: Request,
        threads: Annotated[object, Depends(_get_thread_repo)],
        users: Annotated[TenantUserStore, Depends(get_user_repo)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
        agent_repo: Annotated[AgentSpecStore, Depends(_get_agent_repo)],
        runtime: Annotated[AgentRuntime, Depends(_get_agent_runtime)],
        approvals: Annotated[ApprovalStore, Depends(_get_approval_store)],
    ) -> StreamingResponse | JSONResponse:
        """Stream J.8 — apply a human verdict + resume a paused run.

        Writes the verdict into the checkpoint via ``aupdate_state``
        (re-positioned ``as_node="agent"`` so the graph re-enters
        ``tools``), then streams a continuation run. The continuation
        gets a fresh ``run_id``; the original paused ``run_id`` is what
        the ``agent_approval`` row + APPROVAL_DECIDED audit reference.
        """
        run_record, continuation_run_id, replayed = await apply_approval_decision(
            request=request,
            thread_id=thread_id,
            run_id=run_id,
            decision=payload.decision,
            modified_args=payload.modified_args,
            reason=payload.reason,
            threads=threads,
            users=users,
            audit=audit,
            agent_repo=agent_repo,
            runtime=runtime,
            approvals=approvals,
            idempotency_key=payload.idempotency_key,
        )
        # Log only ``continuation_run_id`` — it is server-generated
        # (``uuid4()``). The paused ``run_id`` is a request path param;
        # CodeQL py/log-injection taints it even though FastAPI has
        # already validated it as a UUID. Same rule as ``trigger_run``.
        logger.info("control_plane.run.resumed continuation=%s", continuation_run_id)
        # Stream 13.2 — idempotent replay: the continuation already exists (it
        # may have finished), so there is no live worker to stream. Return its
        # id; the client re-attaches via GET .../runs/{id}/events (H.3 durable
        # mirror). Keep ``X-Expert-Work-Run-Id`` so both paths surface it uniformly.
        if replayed:
            return JSONResponse(
                {
                    "success": True,
                    "data": {"run_id": str(continuation_run_id), "idempotent_replay": True},
                    "error": None,
                },
                headers={"X-Expert-Work-Run-Id": str(continuation_run_id)},
            )
        return StreamingResponse(
            sse_consumer(
                bridge=runtime.stream_bridge,
                record=run_record,
                run_manager=runtime.run_manager,
                is_disconnected=request.is_disconnected,
                last_event_id=request.headers.get("Last-Event-ID"),
            ),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "X-Expert-Work-Run-Id": str(continuation_run_id),
            },
        )

    return router


def runtime_run_status(request: Request, run_id: UUID) -> str | None:
    """Return the in-memory RunManager's status string for ``run_id``.

    ``None`` when the run is unknown to this process — either it never
    ran here or the control-plane restarted (RunManager is in-memory;
    Mini-ADR J-24 — the ``agent_approval`` row is the durable fallback).
    """
    runtime: AgentRuntime = request.app.state.agent_runtime
    record = runtime.run_manager.get(run_id)
    return record.status.value if record is not None else None


# ---------------------------------------------------------------------------
# Stream H.3 PR 1 — cross-thread Runs index
#
# Mini-ADR H-6 — ``/v1/sessions`` is per-thread; the admin UI's
# Runs page needs a flat aggregate. We mount a SECOND router with
# prefix ``/v1/runs`` exposing only the cross-thread list. Stream N
# tenant-scope framework (ensure_tenant_scope + applied_scope +
# bypass_rls_session) is reused unchanged.
# ---------------------------------------------------------------------------

# Prometheus signals — declared at module import (idempotent collector
# registry handles double-import in tests).
_RUN_LIST_TOTAL = expert_work_counter(
    "expert_work_control_plane_run_list_total",
    "GET /v1/runs invocations by tenant scope.",
    ("tenant_scope",),
)
_RUN_LIST_SECONDS = expert_work_histogram(
    "expert_work_control_plane_run_list_seconds",
    "GET /v1/runs latency in seconds.",
)


def _tokens_to_dict(tokens: TokenTotals | None) -> dict[str, Any] | None:
    """Serialise a run's aggregated token usage (``None`` → no usage recorded).

    The Runs list + detail read this to show "what happened" without a
    Langfuse round-trip; the numbers come from expert_work's own ``token_usage``
    (G.9), joined to the run by ``trace_id``.
    """
    if tokens is None:
        return None
    return {
        "input_tokens": tokens.input_tokens,
        "output_tokens": tokens.output_tokens,
        "cache_creation_tokens": tokens.cache_creation_tokens,
        "cache_read_tokens": tokens.cache_read_tokens,
        "total_tokens": tokens.total_tokens,
        "llm_calls": tokens.llm_calls,
        "models": list(tokens.models),
    }


def _run_to_dict(
    info: Any,
    *,
    agent_name: str | None,
    agent_version: str | None,
    tokens: TokenTotals | None = None,
) -> dict[str, Any]:
    """Serialise a :class:`RunInfo` + JOIN'd thread agent fields to JSON.

    ``agent_name`` / ``agent_version`` come from a per-row
    ``ThreadMetaStore.get`` (Mini-ADR H-6 § 6.5.5 — N+1 JOIN at M0;
    M1 turns into SQL JOIN). ``None`` when the thread has been deleted.
    ``tokens`` is the run's aggregated token usage (``None`` when it has no
    ``trace_id`` or no recorded usage — legacy / auto-triggered runs).
    """
    return {
        "run_id": str(info.run_id),
        "tenant_id": str(info.tenant_id),
        "thread_id": str(info.thread_id),
        "user_id": str(info.user_id) if info.user_id is not None else None,
        "status": info.status.value,
        "is_resume": info.is_resume,
        "error": info.error,
        "agent_name": agent_name,
        "agent_version": agent_version,
        "created_at": info.created_at.isoformat(),
        "updated_at": info.updated_at.isoformat(),
        "finished_at": info.finished_at.isoformat() if info.finished_at is not None else None,
        # Mini-ADR H-9.5 — OTel trace id persisted on agent_run.
        "trace_id": info.trace_id,
        "tokens": _tokens_to_dict(tokens),
    }


def build_runs_list_router() -> APIRouter:
    """Mount ``GET /v1/runs`` — the cross-thread index.

    Lives next to ``build_runs_router`` (per-thread) but ships its own
    APIRouter so the prefix ``/v1/runs`` stays clean.
    """
    router = APIRouter(prefix="/v1/runs", tags=["runs"])

    @router.get(
        "",
        response_model=None,
        dependencies=[Depends(require_key_scope("read")), Depends(console_only())],
    )
    async def list_runs(
        request: Request,
        runs: Annotated[RunStore, Depends(_get_run_store)],
        threads: Annotated[ThreadMetaStore, Depends(_get_thread_repo)],
        token_usage: Annotated[TokenUsageStore, Depends(_get_token_usage_store)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
        status: Annotated[RunStatus | None, Query()] = None,
        agent_name: Annotated[str | None, Query(min_length=1)] = None,
        agent_version: Annotated[str | None, Query(min_length=1)] = None,
        # Operator free-text filter — substring match on run_id / thread_id.
        q: Annotated[str | None, Query(min_length=1, max_length=128)] = None,
        # Narrow to one end-user's runs (AdminUI "member's runs" view).
        user_id: Annotated[UUID | None, Query()] = None,
        limit: Annotated[int, Query(ge=1, le=10000)] = 100,
        offset: Annotated[int, Query(ge=0)] = 0,
        tenant_id: Annotated[UUID | Literal["*"] | None, Query()] = None,
    ) -> JSONResponse:
        trace_id = current_trace_id_hex()
        start = time.monotonic()

        # Stream H.6 (Mini-ADR H-12) — a bare version filter is meaningless.
        if agent_version is not None and agent_name is None:
            raise HTTPException(
                status_code=422,
                detail="agent_version requires agent_name",
            )

        scope = await ensure_tenant_scope(
            request.state.principal,
            tenant_id,
            audit,
            trace_id=trace_id,
            endpoint="GET /v1/runs",
            cross_tenant_enabled=cross_tenant_query_enabled(request),
        )

        async with applied_scope(scope):
            # Stream H.6 (Mini-ADR H-10) — two-step agent resolve: agent →
            # newest-first thread window (capped at MAX_LIST_LIMIT) → runs of
            # those threads. ``thread_window_capped`` honestly signals when
            # the agent has more threads than the window; the SQL-JOIN
            # single-query variant is the M2 upgrade path.
            thread_ids: list[UUID] | None = None
            thread_window_capped = False
            if agent_name is not None:
                if isinstance(scope, CrossTenant):
                    metas = await threads.list_all_tenants(
                        agent_name=agent_name,
                        agent_version=agent_version,
                        limit=MAX_LIST_LIMIT + 1,
                    )
                else:
                    metas = await threads.list_by_tenant(
                        scope.tenant_id,
                        agent_name=agent_name,
                        agent_version=agent_version,
                        limit=MAX_LIST_LIMIT + 1,
                    )
                thread_window_capped = len(metas) > MAX_LIST_LIMIT
                thread_ids = [m.thread_id for m in metas[:MAX_LIST_LIMIT]]

            if isinstance(scope, CrossTenant):
                items = await runs.list_all_tenants(
                    status=status,
                    thread_ids=thread_ids,
                    user_id=user_id,
                    q=q,
                    limit=limit,
                    offset=offset,
                )
                tenant_scope_label = "cross"
            else:
                items = await runs.list_for_tenant(
                    tenant_id=scope.tenant_id,
                    status=status,
                    thread_ids=thread_ids,
                    user_id=user_id,
                    q=q,
                    limit=limit,
                    offset=offset,
                )
                tenant_scope_label = (
                    "home" if scope.tenant_id == request.state.principal.tenant_id else "target"
                )

            # § 6.5.5 (b) — server-side JOIN agent_name from thread_meta.
            # Batched: one `get_many` per distinct tenant in the page (the
            # single-tenant path = 1 query) instead of a per-run `get` — that
            # per-row loop was the M0 N+1 this block used to be. Grouped by
            # tenant so the per-row tenant scoping is preserved on the
            # cross-tenant (system_admin) path. Bound stays MAX_LIST_LIMIT (500).
            ids_by_tenant: dict[UUID, list[UUID]] = {}
            for info in items:
                bucket = ids_by_tenant.setdefault(info.tenant_id, [])
                if info.thread_id not in bucket:
                    bucket.append(info.thread_id)
            agents_by_thread: dict[UUID, tuple[str | None, str | None]] = {}
            for meta_tenant_id, thread_ids in ids_by_tenant.items():
                metas = await threads.get_many(thread_ids, tenant_id=meta_tenant_id)
                for thread_id in thread_ids:
                    meta = metas.get(thread_id)
                    agents_by_thread[thread_id] = (
                        (meta.agent_name, meta.agent_version) if meta is not None else (None, None)
                    )

            # Per-run token summary — one aggregate over this page's trace_ids
            # (token_usage joins runs by trace_id; no run_id column). Runs
            # inside the same scope, so no cross-tenant bleed. A run with no
            # trace_id / no recorded usage maps to None.
            trace_ids = [i.trace_id for i in items if i.trace_id]
            tokens_by_trace = await token_usage.totals_by_trace_ids(trace_ids) if trace_ids else {}

        items_json = [
            _run_to_dict(
                i,
                agent_name=agents_by_thread[i.thread_id][0],
                agent_version=agents_by_thread[i.thread_id][1],
                tokens=tokens_by_trace.get(i.trace_id) if i.trace_id else None,
            )
            for i in items
        ]

        # Mini-ADR H-7 (D) — Hard cap signal so clients know the page was
        # clamped. ``_clamp_limit`` (silently) bounds to MAX_LIST_LIMIT.
        clamped = limit > MAX_LIST_LIMIT
        headers = {"X-Limit-Capped": "true"} if clamped else None
        if clamped:
            limit = _clamp_limit(limit)

        await emit(
            audit,
            tenant_id=request.state.tenant_id,
            actor_id=request.state.actor_id,
            action=AuditAction.RUN_LIST_READ,
            resource_type="run",
            resource_id=None,
            result=AuditResult.SUCCESS,
            trace_id=trace_id,
            details={
                "status": status.value if status is not None else None,
                "agent_name": agent_name,
                "cross_tenant": isinstance(scope, CrossTenant),
                "count": len(items_json),
                "limit": limit,
                "offset": offset,
            },
        )

        _RUN_LIST_TOTAL.labels(tenant_scope=tenant_scope_label).inc()
        _RUN_LIST_SECONDS.observe(time.monotonic() - start)

        return JSONResponse(
            content={
                "success": True,
                "data": {
                    "items": items_json,
                    "total": len(items_json),
                    "cross_tenant": isinstance(scope, CrossTenant),
                    # Stream H.6 — true when the agent filter's thread window
                    # hit MAX_LIST_LIMIT (older threads' runs not included).
                    "thread_window_capped": thread_window_capped,
                },
                "error": None,
            },
            headers=headers,
        )

    return router
