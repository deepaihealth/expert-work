"""Shared child-agent run core — Stream J.4 (sub-agent) + 1.3 (dynamic worker).

Both :class:`~orchestrator.tools.subagent.SubAgentTool` (static ``agent_ref``
delegation) and :class:`~orchestrator.tools.spawn_worker.SpawnWorkerTool`
(dynamic ephemeral worker) build a child :class:`BuiltAgent` and then run it
to completion *the same way*:

* a fresh ``thread_id`` / ``run_id`` seeded with the delegated ``task``,
* the parent's :class:`CancellationToken` + ``deadline_at`` shared so a
  parent cancel / global-deadline reaches every child node,
* a fire-and-forget L7 trajectory write (Mini-ADR J-21) so J.13 eval can
  replay every node of the delegation tree,
* the child's final answer returned as a :class:`ToolResult` carrying a
  :class:`SubAgentInvocation` in ``state_updates``.

The two tools differ only in **how they obtain the child** and the
``label`` / ``agent_ref`` recorded on the invocation. That shared core
lives here.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig

from expert_work.common.observability import expert_work_counter
from expert_work.protocol import MAX_RESULT_EXCERPT_CHARS, SubAgentInvocation, SubagentStatus
from expert_work.runtime.cancellation import (
    CANCELLATION_TOKEN_KEY,
    CancellationToken,
    RunCancelledError,
)
from orchestrator.errors import MaxStepsExceededError
from orchestrator.tools._budget import DELEGATION_GATE_KEY
from orchestrator.tools._guards import GUARD_SINK_KEY, TOKEN_BUDGET_KEY
from orchestrator.tools._worker_events import (
    WORKER_EVENT_SINK_KEY,
    WorkerEventSink,
    WorkerIdentity,
    build_worker_end_frame,
    build_worker_start_frame,
    build_worker_update_frame,
)
from orchestrator.tools.artifact import ARTIFACT_RECORDER_KEY
from orchestrator.tools.registry import ToolContext, ToolResult
from orchestrator.trajectory import (
    TrajectoryOutcome,
    TrajectoryRecord,
    TrajectoryRecorder,
)

if TYPE_CHECKING:
    from orchestrator.built_agent import BuiltAgent

logger = logging.getLogger(__name__)

#: Strong refs to in-flight child trajectory dispatch tasks (Mini-ADR J-21):
#: ``asyncio.create_task`` drops its return value, so we keep the task in a
#: module set until it completes — otherwise GC may finalize it before the
#: ObjectStore put returns.
_BACKGROUND_TRAJECTORY_TASKS: set[asyncio.Task[None]] = set()

#: Wall-clock cap on one child trajectory dispatch.
_TRAJECTORY_DISPATCH_TIMEOUT_S: float = 5.0

#: B-35 PR-4 — child runs halted at an approval gate and soft-refused back
#: to the parent (before this counter existed the parent saw a fake
#: success — the silent-swallow bug this fixes).
_worker_approval_blocked = expert_work_counter(
    "expert_work_worker_approval_blocked_total",
    "Child runs halted at an approval gate and soft-refused to the parent.",
)


async def run_child_to_result(
    *,
    child: BuiltAgent,
    task: str,
    ctx: ToolContext,
    child_depth: int,
    label: str,
    agent_ref: str,
    trajectory_recorder: TrajectoryRecorder | None,
    trajectory_metadata: Mapping[str, Any],
    extra_meta: Mapping[str, Any] | None = None,
) -> ToolResult:
    """Run ``child`` to completion on a fresh thread seeded with ``task``.

    ``label`` / ``agent_ref`` are recorded on the :class:`SubAgentInvocation`
    (a static sub-agent passes its tool name + ``name@version``; a dynamic
    worker passes its worker label + a ``dynamic:<role>`` marker).
    ``extra_meta`` is merged into the result ``meta`` (e.g. ``{"dynamic":
    True, "role": ...}``).

    A child that exhausts its ``max_steps`` is a *partial result*, not a
    tool failure — its partial-progress note returns as a normal
    ``ToolResult`` so the parent can reason about it. A cancellation
    re-raises (the parent run tears down anyway).
    """
    sub_thread_id = uuid4()
    sub_run_id = uuid4()
    child_config = _child_config(ctx, sub_thread_id=sub_thread_id, sub_run_id=sub_run_id)
    child_input: dict[str, Any] = {
        "messages": [
            SystemMessage(content=child.system_prompt),
            HumanMessage(content=task),
        ],
        "step_count": 0,
        "max_steps": child.max_steps,
        "max_no_progress": child.max_no_progress,
    }

    started_at = datetime.now(UTC)
    start_monotonic = time.monotonic()
    result: Any = None
    raised_max_steps = False

    # B2 worker 可观测性 — 帧身份 + 局部序。sink 为 None(未接线:eval /
    # 单测)时零帧零开销。depth>1 说明"发起方自己就是 worker",其
    # ctx.run_id 即父 worker 的 sub_run_id。
    sink = ctx.worker_event_sink
    role_raw = (extra_meta or {}).get("role")
    ident = WorkerIdentity(
        worker_id=str(sub_run_id),
        parent_worker_id=str(ctx.run_id) if child_depth > 1 and ctx.run_id else None,
        parent_tool_call_id=ctx.tool_call_id,
        label=label,
        agent_ref=agent_ref,
        depth=child_depth,
    )
    wseq = 0
    if sink is not None:
        await _emit_worker_frame(
            sink,
            build_worker_start_frame(
                ident,
                wseq=wseq,
                task=task,
                role=str(role_raw) if role_raw else None,
                max_steps=child.max_steps,
            ),
        )
        wseq += 1

    try:
        # B2 — ainvoke → astream:同一 compiled graph、同一 config,
        # updates chunk 逐个截断成 worker 帧;最后一个 values chunk 即
        # ainvoke 的返回值(LangGraph 语义),异常时缺失 → 下方
        # _fetch_partial 兜底(原语义)。
        last_chunk = time.monotonic()
        async for part in child.graph.astream(
            child_input, child_config, stream_mode=["updates", "values"]
        ):
            mode, chunk = part
            if mode == "values":
                result = chunk
                continue
            now = time.monotonic()
            duration_ms = int((now - last_chunk) * 1000)
            last_chunk = now
            if sink is None or not isinstance(chunk, Mapping):
                continue
            for node, writes in chunk.items():
                await _emit_worker_frame(
                    sink,
                    build_worker_update_frame(
                        ident,
                        wseq=wseq,
                        node=str(node),
                        writes=writes if isinstance(writes, Mapping) else {},
                        duration_ms=duration_ms,
                    ),
                )
                wseq += 1
        outcome: TrajectoryOutcome = "success"
    except MaxStepsExceededError:
        outcome = "max_steps"
        raised_max_steps = True
        logger.info("child_run.max_steps label=%s agent_ref=%s", label, agent_ref)
    except RunCancelledError:
        partial_msgs, partial_steps = await _fetch_partial(child.graph, child_config, label=label)
        _dispatch_trajectory(
            tenant_id=ctx.tenant_id,
            user_id=ctx.user_id,
            sub_thread_id=sub_thread_id,
            sub_run_id=sub_run_id,
            outcome="cancelled",
            messages=partial_msgs,
            started_at=started_at,
            finished_at=datetime.now(UTC),
            step_count=partial_steps,
            recorder=trajectory_recorder,
            metadata=trajectory_metadata,
        )
        if sink is not None:
            await _emit_worker_frame(
                sink,
                build_worker_end_frame(
                    ident,
                    wseq=wseq,
                    outcome="cancelled",
                    iteration_used=partial_steps,
                    llm_call_count=sum(1 for m in partial_msgs if isinstance(m, AIMessage)),
                    wall_clock_ms=int((time.monotonic() - start_monotonic) * 1000),
                ),
            )
        raise

    wall_clock_ms = int((time.monotonic() - start_monotonic) * 1000)
    finished_at = datetime.now(UTC)
    if result is not None and isinstance(result, Mapping):
        messages: Sequence[BaseMessage] = list(result.get("messages", []))
        step_count = int(result.get("step_count", 0) or 0)
    else:
        messages, step_count = await _fetch_partial(child.graph, child_config, label=label)

    llm_call_count = sum(1 for msg in messages if isinstance(msg, AIMessage))

    # B-35 PR-4(现状 bug 修复)— fourth exit: the child graph ended with
    # ``pending_approval`` set (its tools_node hit an approval gate and
    # routed to END — RunStatus.PAUSED semantics). Before this check the
    # parent treated that final values chunk as a normal completion: the
    # approval request was silently swallowed and the worker looked
    # successful. Approval semantics stay with the main conversation —
    # surface a structured soft-refusal so the parent LLM takes the
    # sub-task back inline (spec 2026-08-28-plan-first-execution-design §5).
    pending_approval = result.get("pending_approval") if isinstance(result, Mapping) else None
    approval_blocked = pending_approval is not None and not raised_max_steps
    if approval_blocked:
        outcome = "failed"

    if sink is not None:
        await _emit_worker_frame(
            sink,
            build_worker_end_frame(
                ident,
                wseq=wseq,
                outcome=(
                    "max_steps"
                    if raised_max_steps
                    else "approval_blocked"
                    if approval_blocked
                    else "success"
                ),
                iteration_used=step_count,
                llm_call_count=llm_call_count,
                wall_clock_ms=wall_clock_ms,
            ),
        )

    _dispatch_trajectory(
        tenant_id=ctx.tenant_id,
        user_id=ctx.user_id,
        sub_thread_id=sub_thread_id,
        sub_run_id=sub_run_id,
        outcome=outcome,
        messages=messages,
        started_at=started_at,
        finished_at=finished_at,
        step_count=step_count,
        recorder=trajectory_recorder,
        metadata=trajectory_metadata,
    )

    meta: dict[str, Any] = {
        "subagent": label,
        "iteration_used": step_count,
        "llm_call_count": llm_call_count,
        "wall_clock_ms": wall_clock_ms,
    }
    if extra_meta:
        meta.update(extra_meta)

    answer = _final_answer(messages)
    if approval_blocked:
        summary = getattr(pending_approval, "action_summary", "") or "a gated action"
        _worker_approval_blocked.inc()
        meta["worker_approval_blocked"] = True
        logger.warning(
            "child_run.approval_blocked label=%s agent_ref=%s summary=%r",
            label,
            agent_ref,
            summary,
        )
        return _build_tool_result(
            content=(
                f"[worker halted: {summary!r} requires human approval, which is "
                f"unavailable inside a worker sub-agent; handle this sub-task in "
                "the main conversation instead]"
            ),
            meta=meta,
            status=SubagentStatus.FAILED,
            label=label,
            agent_ref=agent_ref,
            child_depth=child_depth,
            sub_thread_id=sub_thread_id,
            sub_run_id=sub_run_id,
            result_excerpt="",
            error=f"approval required inside worker: {summary}",
            started_at=started_at,
            finished_at=finished_at,
            iteration_used=step_count,
            llm_call_count=llm_call_count,
            wall_clock_ms=wall_clock_ms,
        )

    if raised_max_steps:
        meta["subagent_max_steps"] = True
        return _build_tool_result(
            content=(
                f"[sub-agent {label!r} reached its step limit before producing a final answer]"
            ),
            meta=meta,
            status=SubagentStatus.FAILED,
            label=label,
            agent_ref=agent_ref,
            child_depth=child_depth,
            sub_thread_id=sub_thread_id,
            sub_run_id=sub_run_id,
            result_excerpt="",
            error=f"reached step limit before producing a final answer ({step_count} steps)",
            started_at=started_at,
            finished_at=finished_at,
            iteration_used=step_count,
            llm_call_count=llm_call_count,
            wall_clock_ms=wall_clock_ms,
        )

    if answer is None:
        meta["subagent_empty"] = True
        return _build_tool_result(
            content=f"[sub-agent {label!r} produced no answer]",
            meta=meta,
            status=SubagentStatus.COMPLETED,
            label=label,
            agent_ref=agent_ref,
            child_depth=child_depth,
            sub_thread_id=sub_thread_id,
            sub_run_id=sub_run_id,
            result_excerpt="",
            error=None,
            started_at=started_at,
            finished_at=finished_at,
            iteration_used=step_count,
            llm_call_count=llm_call_count,
            wall_clock_ms=wall_clock_ms,
        )

    return _build_tool_result(
        content=answer,
        meta=meta,
        status=SubagentStatus.COMPLETED,
        label=label,
        agent_ref=agent_ref,
        child_depth=child_depth,
        sub_thread_id=sub_thread_id,
        sub_run_id=sub_run_id,
        result_excerpt=answer[:MAX_RESULT_EXCERPT_CHARS],
        error=None,
        started_at=started_at,
        finished_at=finished_at,
        iteration_used=step_count,
        llm_call_count=llm_call_count,
        wall_clock_ms=wall_clock_ms,
    )


def _build_tool_result(
    *,
    content: str,
    meta: dict[str, Any],
    status: SubagentStatus,
    label: str,
    agent_ref: str,
    child_depth: int,
    sub_thread_id: UUID,
    sub_run_id: UUID,
    result_excerpt: str,
    error: str | None,
    started_at: datetime,
    finished_at: datetime,
    iteration_used: int,
    llm_call_count: int,
    wall_clock_ms: int,
) -> ToolResult:
    invocation = SubAgentInvocation(
        task_id=sub_run_id,
        sub_thread_id=sub_thread_id,
        name=label,
        agent_ref=agent_ref,
        child_depth=child_depth,
        status=status,
        result_excerpt=result_excerpt,
        error=error,
        started_at=started_at,
        finished_at=finished_at,
        iteration_used=iteration_used,
        llm_call_count=llm_call_count,
        wall_clock_ms=wall_clock_ms,
    )
    return ToolResult(
        content=content,
        meta=meta,
        state_updates={"subagent_invocations": [invocation]},
    )


async def _fetch_partial(
    graph: Any, config: RunnableConfig, *, label: str
) -> tuple[list[BaseMessage], int]:
    """Best-effort read of a partial child state — Mini-ADR J-21."""
    aget_state = getattr(graph, "aget_state", None)
    if aget_state is None:
        return [], 0
    try:
        snapshot = await aget_state(config)
    except Exception as exc:
        logger.warning("child_run.fetch_partial_failed label=%s err=%s", label, type(exc).__name__)
        return [], 0
    values = getattr(snapshot, "values", None)
    if not isinstance(values, Mapping):
        return [], 0
    msgs = list(values.get("messages", []))
    step_count = int(values.get("step_count", 0) or 0)
    return msgs, step_count


async def _emit_worker_frame(sink: WorkerEventSink, frame: dict[str, Any]) -> None:
    """Best-effort — 桥接故障绝不影响 worker 本体执行(spec 红线)."""
    try:
        await sink(frame)
    except Exception as exc:
        logger.warning(
            "child_run.worker_frame_failed kind=%s err=%s",
            frame.get("kind", "?"),
            type(exc).__name__,
        )


def _dispatch_trajectory(
    *,
    tenant_id: UUID | None,
    user_id: UUID | None,
    sub_thread_id: UUID,
    sub_run_id: UUID,
    outcome: TrajectoryOutcome,
    messages: Sequence[BaseMessage],
    started_at: datetime,
    finished_at: datetime,
    step_count: int,
    recorder: TrajectoryRecorder | None,
    metadata: Mapping[str, Any],
) -> None:
    """Schedule a fire-and-forget L7 trajectory write for the child run."""
    if recorder is None or tenant_id is None:
        return
    record = TrajectoryRecord(
        thread_id=sub_thread_id,
        tenant_id=tenant_id,
        user_id=user_id,
        run_id=sub_run_id,
        outcome=outcome,
        messages=list(messages),
        started_at=started_at,
        finished_at=finished_at,
        step_count=step_count,
        metadata=dict(metadata),
    )
    task = asyncio.create_task(_record_safe(recorder, record))
    _BACKGROUND_TRAJECTORY_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TRAJECTORY_TASKS.discard)


async def _record_safe(recorder: TrajectoryRecorder, record: TrajectoryRecord) -> None:
    try:
        async with asyncio.timeout(_TRAJECTORY_DISPATCH_TIMEOUT_S):
            await recorder.record(record)
    except (TimeoutError, asyncio.CancelledError):
        logger.warning(
            "child_run.trajectory_dispatch_timeout label=%s",
            record.metadata.get("subagent_name", "?"),
        )


def _final_answer(messages: Sequence[BaseMessage]) -> str | None:
    """Return the last ``AIMessage``'s content as text, or ``None``."""
    for message in reversed(messages):
        if isinstance(message, AIMessage):
            content = message.content
            return content if isinstance(content, str) else str(content)
    return None


def _child_config(ctx: ToolContext, *, sub_thread_id: UUID, sub_run_id: UUID) -> RunnableConfig:
    """Build the child run's ``RunnableConfig`` — shares the parent's
    cancellation token + deadline so a parent cancel reaches every child
    node and the whole delegation tree honours one wall-clock cap."""
    token = ctx.cancellation_token or CancellationToken()
    configurable: dict[str, Any] = {
        CANCELLATION_TOKEN_KEY: token,
        "thread_id": str(sub_thread_id),
        "run_id": str(sub_run_id),
        "tenant_id": str(ctx.tenant_id),
        # BUG-10 终审 F3 — mark delegated children: every delegation mints a
        # fresh sub_thread_id nobody ever steers or revisits, so the
        # thread-scoped PLAN.md projection/ingest skips child runs entirely
        # (otherwise each delegation leaves an orphan threads/<uuid>/ dir).
        "child_run": True,
    }
    if ctx.user_id is not None:
        configurable["user_id"] = str(ctx.user_id)
    # MCP-OAUTH (OA-3b-后续): carry the caller's OAuth subject so the child's
    # tool context resolves the same per-user OAuth pool as the parent.
    if ctx.oauth_user_id is not None:
        configurable["oauth_user_id"] = ctx.oauth_user_id
    if ctx.deadline_at is not None:
        configurable["deadline_at"] = ctx.deadline_at
    # B2 — 向下透传 worker 事件 sink,孙 worker 帧直达父 run bridge。
    if ctx.worker_event_sink is not None:
        configurable[WORKER_EVENT_SINK_KEY] = ctx.worker_event_sink
    # B3 — token 池 + guard sink 下传:全树共扣一个额度,guard 帧直达父流。
    if ctx.token_budget is not None:
        configurable[TOKEN_BUDGET_KEY] = ctx.token_budget
    if ctx.guard_sink is not None:
        configurable[GUARD_SINK_KEY] = ctx.guard_sink
    # 产物清单契约 —— 记录器下传:子代(worker/静态子 Agent)登记的产物
    # 同属本 run 的交付物,漏传会让「委派干活」的 run 清单缺项。真栈第三跑
    # (run 02ab4cfc)实证 worker 确实会 save_artifact。
    if ctx.artifact_recorder is not None:
        configurable[ARTIFACT_RECORDER_KEY] = ctx.artifact_recorder
    # 二期 PR3(spec P4)— the delegation gate is process-wide (one singleton
    # per process, not per-run like worker_spawn_budget): forward the SAME
    # object so a depth-2 delegation from within this child contends for the
    # SAME slots as its parent, which is exactly the nested-acquire scenario
    # the 30s acquire timeout exists to resolve without deadlocking.
    if ctx.delegation_gate is not None:
        configurable[DELEGATION_GATE_KEY] = ctx.delegation_gate
    return {"configurable": configurable}
