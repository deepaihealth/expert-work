"""Deliver a fired trigger's result back into the originating conversation.

Spec 1 PR3 (conversational scheduled tasks) — component D1. A scheduled task
runs in its own scratch thread; on success the scheduler's reconcile pass calls
:func:`inject_delivery` to append the run's result as an ``AIMessage`` into the
conversation the task was created from. Reuses LangGraph's ``aupdate_state``
(the same mechanism resume / plan-injection / sanitize use): it writes a new
checkpoint version with the message appended via the ``messages`` add-reducer —
no LLM turn, no history replay. The user sees it the next time they open the
conversation (the ``/messages`` endpoint reads the checkpoint directly).

Spec 1 PR4 Task 2 — :func:`deliver_run_result` extracts the scheduler's former
``_deliver`` body into a module function, so the scheduler's reconcile pass and
the manual fire-now endpoint (PR4 Task 3) share the exact same delivery path
(DRY). It also adds FU2: after a successful delivery, sync the originating
thread into the content-search mirror — the background
``TranscriptMirrorSweep`` only re-indexes threads with new ``agent_run``
activity, and a pure checkpoint injection has none, so without this the
delivered message would not be searchable until some *other* activity touches
the thread.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph.state import CompiledStateGraph

from control_plane.runtime import AgentRuntime
from control_plane.transcript import read_turns
from expert_work.common.message_stamp import STAMP_CREATED_AT, STAMP_RUN_ID
from expert_work.persistence import ThreadMessageStore
from expert_work.persistence.agent_spec import AgentSpecStore
from expert_work.protocol import TriggerRecord
from expert_work.protocol.agent_spec import AgentSpecStatus
from expert_work.runtime.runs import RunInfo

logger = logging.getLogger(__name__)


async def inject_delivery(
    graph: CompiledStateGraph[Any, Any, Any, Any],
    *,
    thread_id: UUID,
    tenant_id: UUID,
    result_text: str,
    source_run_id: UUID,
    trigger_id: UUID,
) -> None:
    """Append ``result_text`` as an ``AIMessage`` to ``thread_id``'s checkpoint.

    The message is tagged (but NOT hidden) so the UI shows it and callers can
    trace it back to the firing. ``as_node`` follows the codebase convention
    (``"agent"`` when the thread already has history, ``"__start__"`` otherwise).
    Real delivery targets are always existing conversations (a reuse_thread
    task's originating thread), so the has-history path — left in a clean
    turn-complete state after an assistant message with no tool calls — is the
    one that matters.

    Idempotent by ``source_run_id``: a repeat call for a ``source_run_id``
    already delivered is a no-op. This is what lets the scheduler's reconcile
    pass and the manual ``:fire`` endpoint (Spec 1 PR4) race on the same fired
    run without double-appending the result.
    """
    config: RunnableConfig = {
        "configurable": {"thread_id": str(thread_id), "tenant_id": str(tenant_id)}
    }
    snapshot = await graph.aget_state(config)
    values = snapshot.values if isinstance(snapshot.values, dict) else {}
    existing = values.get("messages") or []
    # FU1a — 幂等:同一 source_run_id 已投递过则跳过。fire-now 端点与 scheduler
    # reconcile 可能同进程各投递一次同一 run 的结果;去重保证只贴一条。
    tag = str(source_run_id)
    for m in existing:
        ak = getattr(m, "additional_kwargs", None)
        if isinstance(ak, dict) and ak.get("expert_work_source_run_id") == tag:
            return
    has_history = bool(existing)
    message = AIMessage(
        content=result_text,
        additional_kwargs={
            "expert_work_scheduled_delivery": True,
            "expert_work_source_run_id": str(source_run_id),
            "expert_work_trigger_id": str(trigger_id),
            STAMP_CREATED_AT: datetime.now(UTC).isoformat(),
            STAMP_RUN_ID: str(source_run_id),
        },
    )
    await graph.aupdate_state(
        config,
        {"messages": [message]},
        as_node="agent" if has_history else "__start__",
    )


@dataclass(frozen=True)
class DeliveryOutcome:
    """Result of :func:`deliver_run_result`.

    ``status`` is also what lands in the ``TRIGGER_COMPLETED`` audit entry's
    ``delivery`` detail. ``text`` carries the delivered result (non-empty
    only when ``status == "delivered"``) so a fire-now HTTP endpoint (PR4
    Task 3) can echo it back to the caller without a second checkpoint read.
    """

    status: str
    text: str | None = None


async def deliver_run_result(
    *,
    trigger: TriggerRecord,
    run: RunInfo,
    runtime: AgentRuntime,
    agent_spec_store: AgentSpecStore,
    thread_message_store: ThreadMessageStore | None,
    now: datetime,
) -> DeliveryOutcome:
    """Deliver a successful run's result into ``trigger``'s originating
    conversation and refresh its content-search mirror (FU2).

    Best-effort: returns a status (+ the text on success) and never raises —
    a delivery failure must not block the ``trigger_run``'s ``SUCCEEDED``
    transition. Only ``reuse_thread`` conversation-created tasks with an
    ``originating_thread_id`` deliver; background (``fresh_thread_per_run``)
    tasks skip.

    Precondition: the caller must already be inside ``trigger``'s tenant RLS
    scope. Both callers establish this before invoking: the scheduler's
    ``_reconcile_one`` via ``_tenant_scope(row.tenant_id)``, and the manual
    fire-now endpoint (PR4 Task 3) via ``current_tenant_id_var``.
    """
    try:
        if trigger.context_mode != "reuse_thread" or trigger.originating_thread_id is None:
            return DeliveryOutcome("skipped")
        checkpointer = runtime.durable_checkpointer
        if checkpointer is None:
            return DeliveryOutcome("no_checkpointer")
        turns = await read_turns(checkpointer, run.thread_id, include_hidden=False)
        result = next((t.content for t in reversed(turns) if t.role == "assistant"), None)
        if not result:
            return DeliveryOutcome("no_output")
        spec_record = await agent_spec_store.get(
            tenant_id=trigger.tenant_id,
            name=trigger.agent_name,
            version=trigger.agent_version,
        )
        if spec_record is None or spec_record.status is not AgentSpecStatus.ACTIVE:
            return DeliveryOutcome("agent_unavailable")
        built = await runtime.get_agent(
            tenant_id=trigger.tenant_id,
            name=trigger.agent_name,
            version=trigger.agent_version,
            spec=spec_record.spec,
        )
        await inject_delivery(
            built.graph,
            thread_id=trigger.originating_thread_id,
            tenant_id=trigger.tenant_id,
            result_text=result,
            source_run_id=run.run_id,
            trigger_id=trigger.id,
        )
        # FU2 — mirror the originating thread into content search. Its own
        # try/except: a mirror-sync failure must NOT downgrade an
        # already-succeeded delivery (the message is durably in the checkpoint).
        if thread_message_store is not None:
            try:
                mirror_turns = await read_turns(
                    checkpointer, trigger.originating_thread_id, include_hidden=True
                )
                await thread_message_store.sync_thread(
                    thread_id=trigger.originating_thread_id,
                    tenant_id=trigger.tenant_id,
                    turns=mirror_turns,
                    synced_at=now,
                )
            except Exception:
                logger.exception(
                    "trigger.mirror_sync_failed",
                    extra={"trigger_id": str(trigger.id), "run_id": str(run.run_id)},
                )
        return DeliveryOutcome("delivered", text=result)
    except Exception:
        logger.exception("trigger.delivery_failed", extra={"trigger_id": str(trigger.id)})
        return DeliveryOutcome("error")


__all__ = ["DeliveryOutcome", "deliver_run_result", "inject_delivery"]
