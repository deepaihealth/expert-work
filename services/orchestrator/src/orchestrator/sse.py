"""SSE streaming — background run worker + SSE consumer (Stream E.14).

In-process monolith model (STREAM-E-DESIGN § 2.6, corrected against
deer-flow's ``gateway`` + ``runtime`` co-location). There is **no**
separate orchestrator service: the control-plane FastAPI app imports
this module, runs graphs as background ``asyncio.Task``s, and streams
their events to clients over SSE.

Two halves, decoupled by :class:`StreamBridge`:

- :func:`run_agent` — the **producer**. A background task that drives a
  compiled LangGraph graph via ``graph.astream(...)`` and publishes
  each chunk to the bridge. Cooperative cancellation: it polls
  ``record.abort_event`` between chunks (set by
  :meth:`RunManager.cancel`). Always publishes a terminal ``end`` via
  :meth:`StreamBridge.publish_end`, even on error / cancel, so no
  consumer hangs.
- :func:`sse_consumer` — the **consumer**. An async generator that
  subscribes to the bridge and yields SSE wire frames. Its ``finally``
  block implements ``on_disconnect``: when the client disconnects and
  the run's mode is :data:`DisconnectMode.CANCEL`, the run is
  cancelled.

The worker is **graph-injected** — it receives an already-compiled
graph. Assembling a graph from an agent manifest (the "agent factory")
is a separate concern out of E.14 scope.

Backpressure is the :class:`StreamBridge`'s bounded-buffer drop-oldest
(Mini-ADR E-8, M0): a consumer that falls behind resumes from the
earliest retained event. There is no cancel-on-full in M0.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, Final, Protocol
from uuid import UUID, uuid4

from langchain_core.messages import BaseMessage
from langchain_core.runnables import RunnableConfig
from pydantic import BaseModel

from expert_work.common.context import reset_current_run_id, set_current_run_id
from expert_work.common.observability import (
    ExpertWorkComponent,
    expert_work_counter,
    expert_work_histogram,
    expert_work_span,
)
from expert_work.common.skill_run_usage import SkillRunUsageRecorder
from expert_work.persistence import ApprovalStore
from expert_work.protocol import (
    ApprovalRecord,
    ApprovalRequest,
    AuditAction,
    AuditEntry,
    AuditResult,
    EventType,
)
from expert_work.runtime.audit.logger import AuditLogger
from expert_work.runtime.cancellation import (
    CANCELLATION_TOKEN_KEY,
    CancellationToken,
    RunCancelledError,
)
from expert_work.runtime.runs import (
    RunEventRecord,
    RunEventStore,
    RunManager,
    RunRecord,
    RunStatus,
    make_event_record,
)
from expert_work.runtime.stream_bridge import (
    END_SENTINEL,
    HEARTBEAT_SENTINEL,
    StreamBridge,
)
from orchestrator.errors import MaxStepsExceededError
from orchestrator.graph_builder._config import AUDIT_LOGGER_KEY, COMPACTION_SINK_KEY, TOKEN_SINK_KEY
from orchestrator.run_retry import (
    MAX_RUN_RETRIES,
    is_transient_run_error,
    replay_is_safe,
    retry_backoff_s,
    retry_enabled,
    run_retry_total,
)
from orchestrator.tools._budget import DELEGATION_GATE_KEY, DelegationGate
from orchestrator.tools._guards import GUARD_SINK_KEY, TOKEN_BUDGET_KEY, TokenBudget
from orchestrator.tools._worker_events import WORKER_EVENT_SINK_KEY
from orchestrator.tools.spawn_worker import WorkerSpawnBudget
from orchestrator.trajectory import (
    TrajectoryOutcome,
    TrajectoryRecord,
    TrajectoryRecorder,
)

logger = logging.getLogger(__name__)


# Stream K.K10 — Session TTFT (Time-To-First-Token). Measured from the
# moment ``run_agent`` flips the run to ``RUNNING`` to the first real
# ``updates`` chunk published to the bridge. The ``metadata`` event we
# publish synchronously beforehand isn't counted — TTFT tracks how long
# the user waits for actual agent work to start. SLO #3 (slo.md):
# P95 < 1.5s @ 30d.
# 一期 Task 3 —— 老名字叫 ttft 但测的是"第一个图节点完成",有 memory_recall
# 的 run 测到的是召回结束,不是首字。改名说实话,真首字用下面的
# first_output_seconds。
_session_first_node_seconds = expert_work_histogram(
    "expert_work_session_first_node_seconds",
    "Seconds from RUNNING to the first graph-node update chunk.",
    buckets=(0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0),
)

#: 用户第一次看到内容的时刻。``source="token"`` 走流式首帧;``source="node"``
#: 是无 token 流的 run(output judge 开启 / LLM cache 命中 / provider 不支持
#: 流式)的兜底,取第一个 **agent** 节点的 updates 帧。两条路径互斥、先到先得。
#: 不做兜底的话最慢的那批(judge-on)完全不进直方图,是幸存者偏差。
_first_output_seconds = expert_work_histogram(
    "expert_work_first_output_seconds",
    "Seconds from RUNNING to the first content the user can see.",
    ("source",),
    buckets=(0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0),
)

# Stream K.K10 — Durable resume duration. Measured at the same point as
# TTFT, but only emitted when the run resumed an existing checkpointed
# thread. The control-plane decides ``is_resume`` from the thread's
# pre-existing state (see ``runs.py``) and passes it via the
# ``RunRecord``. SLO #5 reformulated to seconds-of-resume + a success
# counter (resume failures are run failures → already counted).
_durable_resume_seconds = expert_work_histogram(
    "expert_work_durable_resume_seconds",
    "Seconds from RUNNING to first chunk on a resumed (non-empty checkpoint) run.",
    buckets=(0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0),
)

# Stream M Gate — Session end-to-end duration. Measured from just before
# ``set_status(RUNNING)`` to the run's terminal status (the ``finally``
# block, so every exit path emits exactly once). Labelled by ``outcome``
# so Gate dashboards can query successful-run P95 separately from
# error / max_steps / cancelled tails — Stream M Exit Criteria
# (STREAM-M-DESIGN § 2.1) targets the user-facing successful-run latency.
_session_duration_seconds = expert_work_histogram(
    "expert_work_session_duration_seconds",
    "Seconds from RUNNING to terminal status; labelled by run outcome.",
    ("outcome",),
    buckets=(0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0),
)

# Stream H.3 PR 3 (Mini-ADR H-7) — RunEventStore persistence telemetry.
# A failure to mirror a frame is logged + counted; the SSE stream is
# NOT blocked (graceful degradation — better miss-an-event than fail the
# user-visible run).
_run_event_persist_errors = expert_work_counter(
    "expert_work_run_event_persist_errors_total",
    "RunEventStore.append failures during run_agent dual-write.",
    ("event_name",),
)
_run_event_persist_total = expert_work_counter(
    "expert_work_run_event_persist_total",
    "RunEventStore.append successes during run_agent dual-write.",
    ("event_name",),
)

#: Default LangGraph stream mode. ``"updates"`` yields ``{node: writes}``
#: per step — the natural granularity for a ReAct SSE stream (one event
#: per agent / tools node completion).
DEFAULT_STREAM_MODE = "updates"

#: Seconds to keep a finished run's events in the bridge before cleanup,
#: giving late / reconnecting consumers time to drain.
_CLEANUP_DELAY_S = 60.0


class StreamableGraph(Protocol):
    """The slice of a compiled LangGraph graph :func:`run_agent` needs.

    ``langgraph.graph.state.CompiledStateGraph`` satisfies this
    structurally; tests inject a scripted async-iterator stub.
    """

    def astream(
        self,
        input: Any,
        config: RunnableConfig | None = ...,
        *,
        stream_mode: str | list[str] | None = ...,
    ) -> AsyncIterator[Any]:
        """Yield streaming chunks for one graph execution."""

    async def aget_state(self, config: RunnableConfig) -> Any:
        """Return the run's final checkpointed state.

        Stream L.L7 — :func:`run_agent` calls this on terminal paths to
        fetch the conversation messages for trajectory recording.
        ``langgraph.graph.state.CompiledStateGraph.aget_state`` returns
        a ``StateSnapshot`` whose ``values`` dict carries
        :class:`AgentState`. We accept ``Any`` here because the
        Protocol is intentionally minimal — the snapshot's exact shape
        is LangGraph-internal.
        """


# ---------------------------------------------------------------------------
# Producer — background run worker
# ---------------------------------------------------------------------------


async def _heartbeat_loop(run_manager: RunManager, run_id: UUID, record: Any) -> None:
    """Stream 9.4 — renew the run's ownership lease until cancelled.

    Touches the lease every ``lease_ttl_s / 3`` (two missed renewals still leave
    margin before a peer reclaims). If :meth:`RunManager.heartbeat` returns
    ``False`` the run was reclaimed by a peer (this owner's lease lapsed, e.g.
    a long GC pause) — we set ``abort_event`` so this stale worker stops rather
    than double-executing alongside the peer's continuation.
    """
    interval = max(1.0, run_manager.lease_ttl_s / 3.0)
    try:
        while True:
            await asyncio.sleep(interval)
            still_owner = await run_manager.heartbeat(run_id)
            if not still_owner:
                logger.warning("run_agent.lease_lost run_id=%s peer_reclaimed", run_id)
                record.abort_event.set()
                return
    except asyncio.CancelledError:
        return


async def run_agent(
    *,
    bridge: StreamBridge,
    run_manager: RunManager,
    record: RunRecord,
    graph: StreamableGraph,
    graph_input: Any,
    config: RunnableConfig,
    audit_logger: AuditLogger | None = None,
    stream_mode: str = DEFAULT_STREAM_MODE,
    trajectory_recorder: TrajectoryRecorder | None = None,
    trajectory_enabled: bool = True,
    skill_run_usage_recorder: SkillRunUsageRecorder | None = None,
    approval_store: ApprovalStore | None = None,
    event_store: RunEventStore | None = None,
    tool_replay_safe: Callable[[str], bool] | None = None,
    worker_spawn_budget: WorkerSpawnBudget | None = None,
    delegation_gate: DelegationGate | None = None,
    token_budget: int = 0,
) -> None:
    """Drive ``graph`` to completion, publishing events to ``bridge``.

    Intended to run as a background :class:`asyncio.Task`. Lifecycle:

    1. Mark the run ``RUNNING``; publish a ``metadata`` event.
    2. Stream ``graph.astream(...)`` chunks → ``bridge.publish``. Between
       chunks, poll ``record.abort_event`` — set means cooperative
       cancel, so stop early.
    3. Final status: ``INTERRUPTED`` if aborted, else ``SUCCESS``.
    4. On exception: status ``ERROR`` + publish an ``error`` event.
    5. **Always** (``finally``): ``publish_end`` + schedule bridge
       cleanup. A terminal ``end`` reaches every consumer no matter how
       the run finished.

    When ``audit_logger`` is supplied, one run-lifecycle row lands at
    run end — ``run:completed`` on a normal / interrupted finish,
    ``run:failed`` on an exception. The defensive
    :class:`asyncio.CancelledError` path is *not* audited: awaiting the
    logger during loop teardown is unreliable. Audit-write failures are
    logged and swallowed — they never fail the run.

    ``trajectory_enabled`` is the per-agent manifest opt-out
    (``policies.trajectory_recording`` via ``BuiltAgent``): ``False``
    drops the recorder for this run even when the deployment has one.
    """
    if not trajectory_enabled:
        trajectory_recorder = None
    run_id = record.run_id
    # Stream HX-4 (Mini-ADR HX-D4) — bind the run id for the structured
    # log formatter; every log line from this worker (and tasks it
    # spawns) carries ``run_id`` without manual threading. Reset in the
    # ``finally`` below.
    run_id_token = set_current_run_id(run_id)
    # Bind a cancellation token to the run's abort_event and thread it
    # to graph nodes via config["configurable"] (E.15). RunManager.cancel
    # sets abort_event → the token reports cancelled → nodes surface
    # RunCancelledError at their next checkpoint.
    token = CancellationToken.from_event(record.abort_event)
    effective_config: RunnableConfig = {
        **config,
        "configurable": {
            **(config.get("configurable") or {}),
            CANCELLATION_TOKEN_KEY: token,
            # Stream TE-2 — thread the AuditLogger to the tools node so each
            # tool dispatch emits a TOOL_CALL / TOOL_BLOCKED row. A live
            # object (not checkpoint-serialisable), like the cancel token.
            AUDIT_LOGGER_KEY: audit_logger,
            # 1.3 Orchestrator-Worker — the per-run spawn budget (count cap +
            # concurrency gate), shared across every spawn_worker call. A live
            # object like the cancel token; ``None`` when the feature is off.
            "worker_spawn_budget": worker_spawn_budget,
            # 二期 PR3(spec P4)— the process-wide delegation concurrency gate
            # (a process-level singleton, unlike worker_spawn_budget above).
            # ``None`` when unwired (no delegation_config_service).
            DELEGATION_GATE_KEY: delegation_gate,
        },
    }
    # Stream M Gate — session E2E duration. Started before ``set_status``
    # so the ``finally`` block always sees a valid clock; ``session_outcome``
    # defaults to ``"error"`` for the worst-case path (a crash before any
    # branch sets it). Each terminal branch below updates ``session_outcome``
    # before its ``await`` chain so ``_session_duration_seconds`` carries
    # the correct label even if a later step in that branch raises.
    session_started = time.monotonic()
    session_outcome = "error"
    # Stream HX-3 — count of in-worker transient retries this run took.
    # Initialised before the ``try`` so every except handler can read it.
    retry_attempts = 0
    # Stream H.3 PR 3 — per-run sequence counter for RunEventStore mirror.
    # Starts at 0; increments before each persist call so the seq in the
    # durable row matches its insertion order. The bridge has its own
    # internal counter; the two are independent (replay endpoint emits
    # SSE id from the persisted ``created_at_ms`` + ``seq``).
    # Stream 9.4 (HA failover) — a peer that resumed a reclaimed run re-enters
    # here; seed past the prior owner's durable frames so the resumed run's
    # events stay append-only (a fresh 0 would collide on ``(run_id, seq)``).
    event_seq = 0
    if event_store is not None and getattr(record, "is_resume", False):
        event_seq = await event_store.next_seq(run_id=run_id)

    # 二期 PR3 — run_event 持久化移出流路径(spec PR3 Task 2)。
    # 主循环每帧只做「seq 同步预分配 + put_nowait」;后台 writer 攒批
    # (≤_PERSIST_BATCH_MAX 条或 _PERSIST_FLUSH_INTERVAL_S)写 append_batch。
    # 队满 drop-oldest(H-7 立场:调试台 replay 可容忍缺帧,live SSE 不能慢)。
    persist_queue: asyncio.Queue[RunEventRecord | None] = asyncio.Queue(maxsize=_PERSIST_QUEUE_MAX)

    def _enqueue_event(event_name: str, data: Any) -> None:
        nonlocal event_seq
        # seq 分配先于 event_store 判空——与现状 event_seq 无条件递增的
        # 行为完全一致(即便没有 store,resume 场景的种子计数仍要对齐)。
        seq = event_seq
        event_seq += 1
        if event_store is None:
            return
        record_ = make_event_record(run_id=run_id, seq=seq, event_name=event_name, data=data)
        try:
            persist_queue.put_nowait(record_)
        except asyncio.QueueFull:
            try:
                persist_queue.get_nowait()  # drop-oldest
                persist_queue.task_done()
            except asyncio.QueueEmpty:
                # writer 恰在 QueueFull 与 get_nowait 之间清空了队列——位子
                # 已经腾出来,无需(也无从)丢帧,直接重投即可。
                pass
            _run_event_queue_dropped.labels(event_name=event_name).inc()
            persist_queue.put_nowait(record_)

    # mypy: event_store is RunEventStore | None here; _persist_writer wants a
    # concrete RunEventStore. No writer (and no queue consumer) when there's
    # no store to write to — _enqueue_event already no-ops in that case, so
    # the queue simply never receives anything to drain.
    writer_task: asyncio.Task[None] | None = None
    if event_store is not None:
        writer_task = asyncio.create_task(
            _persist_writer(event_store, persist_queue, run_id=run_id)
        )
        _BACKGROUND_PERSIST_WRITERS.add(writer_task)
        writer_task.add_done_callback(_BACKGROUND_PERSIST_WRITERS.discard)

    async def _drain_persist_queue() -> None:
        """Block until every enqueued frame so far has been flushed (or
        dropped) by the background writer — capped at
        ``_PERSIST_DRAIN_TIMEOUT_S`` so a stuck writer never hangs a
        terminal ``set_status`` transition."""
        if event_store is None:
            return
        if writer_task is not None and writer_task.done():
            # The writer is gone (crashed on an exception it didn't swallow,
            # or was externally cancelled) — nobody will ever call
            # queue.task_done() again, so persist_queue.join() can only ever
            # resolve by timing out. Skip straight to the terminal path
            # instead of paying the full _PERSIST_DRAIN_TIMEOUT_S for a wait
            # that is guaranteed to fail.
            logger.warning(
                "run_event.writer_dead run_id=%s pending=%s", run_id, persist_queue.qsize()
            )
            return
        try:
            async with asyncio.timeout(_PERSIST_DRAIN_TIMEOUT_S):
                await persist_queue.join()
        except TimeoutError:
            logger.warning(
                "run_event.drain_timeout run_id=%s pending=%s", run_id, persist_queue.qsize()
            )

    # Stream RT-2 PR-4 — COMPACTION event sink. ``agent_node`` fires this when
    # the context compressor produces a summary; publishing lives here (the
    # driver owns the bridge + event store), so the graph layer stays
    # SSE-agnostic. The frame is a free-string ``"compaction"`` event
    # (``EventType.COMPACTION`` names it in the canonical taxonomy) mirrored to
    # the durable store on the same monotonic ``event_seq`` as every other
    # frame; it runs inside the node's turn, before the ``updates`` chunk that
    # turn yields, so it lands earlier in the stream. Best-effort by contract —
    # the caller (agent_node) already swallows failures; ``_enqueue_event``
    # never raises (queue put is synchronous) and the background persist
    # writer swallows its own (H-7).
    async def _publish_compaction(payload: Any) -> None:
        await bridge.publish(run_id, EventType.COMPACTION.value, payload)
        _enqueue_event(EventType.COMPACTION.value, payload)

    # 一期 Task 3 —— ``source`` 路径的赢家标记。token / node 两条路径互斥、
    # 先到先得;声明在 ``_publish_token`` 定义之前,闭包用 ``nonlocal`` 改写。
    first_output_recorded = False

    async def _publish_token(frame: Any) -> None:
        nonlocal first_output_recorded
        # Live-only: token frames are provisional; do NOT mirror to the event
        # store (the authoritative ``updates`` frame is what replays).
        if not first_output_recorded:
            first_output_recorded = True
            _first_output_seconds.labels(source="token").observe(time.monotonic() - ttft_started)
        await bridge.publish(run_id, "token", frame)

    # B2 worker 可观测性 — worker 事件 sink。child run(spawn_worker /
    # 静态 subagent)的 start/update/end 帧经此进父 run 的 bridge + 事件
    # 库,实时与回放同源。与 _publish_compaction 的关键差异:并发
    # worker(≤dynamic_worker_max_concurrent)会交错 await 本函数,seq
    # 必须在任何 await 之前同步分配,否则两帧读到同一 event_seq 撞
    # (run_id, seq) 主键 —— 二期 PR3 起这条铁律由 _enqueue_event 内部
    # 保证(其 seq 读取+自增全程无 await,不管调用方在 bridge.publish
    # 前后调它都不会撞号),本函数不再手工预分配。
    async def _publish_worker(frame: dict[str, Any]) -> None:
        await bridge.publish(run_id, "worker", frame)
        _enqueue_event("worker", frame)

    # B3 — guard marker 帧 sink(_publish_worker 同款:seq 分配交给
    # _enqueue_event;worker 树里的 guard 也经它)。无条件注入 ——
    # max_steps / no_progress 的 tripped 可见化与 token 预算是否启用无关。
    async def _publish_guard(frame: dict[str, Any]) -> None:
        await bridge.publish(run_id, "guard", frame)
        _enqueue_event("guard", frame)

    # ``configurable`` was populated in the effective_config literal above.
    effective_config["configurable"][COMPACTION_SINK_KEY] = _publish_compaction
    effective_config["configurable"][TOKEN_SINK_KEY] = _publish_token
    effective_config["configurable"][WORKER_EVENT_SINK_KEY] = _publish_worker
    effective_config["configurable"][GUARD_SINK_KEY] = _publish_guard
    if token_budget > 0:
        effective_config["configurable"][TOKEN_BUDGET_KEY] = TokenBudget(limit=token_budget)
    # Stream 9.4 (HA failover) — renew the ownership lease while executing so a
    # peer's orphan sweep can tell this live run from a crashed owner's. Spawned
    # after the → RUNNING claim; cancelled in ``finally``.
    heartbeat_task: asyncio.Task[None] | None = None
    try:
        await run_manager.set_status(run_id, RunStatus.RUNNING)
        heartbeat_task = asyncio.create_task(
            _heartbeat_loop(run_manager, run_id, record), name=f"run-heartbeat-{run_id}"
        )
        metadata_payload = {"run_id": str(run_id), "thread_id": str(record.thread_id)}
        await bridge.publish(run_id, "metadata", metadata_payload)
        _enqueue_event("metadata", metadata_payload)

        # Stream K.K10 — start the TTFT / durable-resume timer at RUNNING.
        # The metadata frame above is server-synthesised, not LLM output,
        # so we measure from this point to the first ``updates`` chunk.
        ttft_started = time.monotonic()
        first_chunk_seen = False
        # Batch 4a — per-superstep wall-clock. Each ``updates`` chunk is one
        # node execution; the gap between successive chunk arrivals is that
        # step's coarse duration. Baseline = the same RUNNING mark used for
        # TTFT, so the first chunk's duration ≈ TTFT.
        last_frame_ts = ttft_started
        # Stream HX-3 — run-level transient retry (Mini-ADR HX-C1..C3).
        # At most one in-worker retry, same run_id: the SSE stream stays
        # continuous and the trajectory stays a single record. The retry
        # resumes from the committed checkpoint (``graph_input=None`` —
        # the J-24 continuation semantics) after the replay-safety guard
        # inspects the checkpoint tail.
        # Stream A.8 / 10.1 — the session root span. One ``expert_work.session.run``
        # per run wraps the whole streaming loop (retries included), so every
        # LLM / tool / subagent child span created inside ``graph.astream``
        # attaches under it and a run becomes one connected trace.
        with expert_work_span(
            ExpertWorkComponent.SESSION,
            "run",
            attributes={
                "run_id": str(run_id),
                "thread_id": str(record.thread_id),
            },
        ):
            while True:
                try:
                    async for chunk in graph.astream(
                        graph_input, effective_config, stream_mode=stream_mode
                    ):
                        if record.abort_event.is_set():
                            logger.info("run_agent.abort_requested run_id=%s", run_id)
                            break
                        if not first_chunk_seen:
                            ttft = time.monotonic() - ttft_started
                            _session_first_node_seconds.observe(ttft)
                            if getattr(record, "is_resume", False):
                                _durable_resume_seconds.observe(ttft)
                            first_chunk_seen = True
                        jsonable_chunk = _to_jsonable(chunk)
                        # 一期 Task 3 —— agent 节点的 updates 帧是无 token 流
                        # 时用户第一次看到内容的时刻。必须挑 agent 那帧:
                        # first_chunk_seen 认的是任意第一个节点,有 recall 的
                        # run 那是召回完成,比真实首字早好几秒。
                        if not first_output_recorded and isinstance(jsonable_chunk, dict):
                            if "agent" in jsonable_chunk:
                                first_output_recorded = True
                                _first_output_seconds.labels(source="node").observe(
                                    time.monotonic() - ttft_started
                                )
                        now = time.monotonic()
                        duration_ms = round((now - last_frame_ts) * 1000)
                        last_frame_ts = now
                        if isinstance(jsonable_chunk, dict):
                            for node_val in jsonable_chunk.values():
                                if isinstance(node_val, dict):
                                    node_val["_duration_ms"] = duration_ms
                        await bridge.publish(run_id, stream_mode, jsonable_chunk)
                        _enqueue_event(stream_mode, jsonable_chunk)
                except Exception as exc:
                    if (
                        retry_attempts >= MAX_RUN_RETRIES
                        or record.abort_event.is_set()
                        or not retry_enabled()
                        or not is_transient_run_error(exc)
                        or not await replay_is_safe(graph, effective_config, tool_replay_safe)
                    ):
                        raise
                    retry_attempts += 1
                    backoff_s = retry_backoff_s()
                    logger.warning(
                        "run_agent.transient_retry run_id=%s attempt=%d backoff_s=%s error=%s",
                        run_id,
                        retry_attempts,
                        backoff_s,
                        exc,
                    )
                    retry_payload = {
                        "attempt": retry_attempts,
                        "error_class": type(exc).__name__,
                        "backoff_s": backoff_s,
                    }
                    await bridge.publish(run_id, "retry", retry_payload)
                    _enqueue_event("retry", retry_payload)
                    # Abort-aware backoff: a timeout means the backoff simply
                    # elapsed; a cancel during the wait exits immediately and
                    # takes the INTERRUPTED path below.
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(record.abort_event.wait(), timeout=backoff_s)
                    if record.abort_event.is_set():
                        break
                    graph_input = None
                else:
                    break

        # Stream J.8 (Mini-ADR J-24) — a run that streamed to its natural
        # end with ``pending_approval`` set did not finish: it paused at
        # an approval gate (RunStatus.PAUSED). The checkpoint persists so
        # ``POST .../resume`` can re-invoke. A paused run emits no
        # RUN_COMPLETED audit + no trajectory — both belong to the *true*
        # run end after a resume. A failing ``aget_state`` degrades to
        # "not paused" (same graceful-degradation contract as the
        # trajectory recorder) rather than failing the run.
        pending_request: ApprovalRequest | None = None
        if not record.abort_event.is_set():
            try:
                snapshot = await graph.aget_state(effective_config)
                raw_pending = snapshot.values.get("pending_approval")
                if raw_pending is not None:
                    pending_request = (
                        raw_pending
                        if isinstance(raw_pending, ApprovalRequest)
                        else ApprovalRequest.model_validate(raw_pending)
                    )
            except Exception:
                logger.warning("run_agent.pause_check_failed run_id=%s", run_id, exc_info=True)

        if record.abort_event.is_set():
            final = RunStatus.INTERRUPTED
        elif pending_request is not None:
            final = RunStatus.PAUSED
        else:
            final = RunStatus.SUCCESS
        session_outcome = {
            RunStatus.INTERRUPTED: "interrupted",
            RunStatus.PAUSED: "paused",
            RunStatus.SUCCESS: "success",
        }[final]
        # Stream HX-3 — a retried run that reached a healthy terminal
        # counts as recovered. An abort after a retry counts as neither.
        if retry_attempts and final in (RunStatus.SUCCESS, RunStatus.PAUSED):
            run_retry_total.labels(outcome="recovered").inc()
        # 二期 PR3 — flush every frame queued so far before the terminal
        # status write lands, so a client polling right after ``set_status``
        # sees a fully-persisted replay (bounded by _PERSIST_DRAIN_TIMEOUT_S).
        await _drain_persist_queue()
        await run_manager.set_status(run_id, final)
        if final is RunStatus.PAUSED and pending_request is not None:
            # Register the paused run in the durable ``agent_approval``
            # table + emit APPROVAL_REQUESTED. The table — not the
            # in-memory RunManager — is what the resume endpoint, the
            # GET surface, and the 24h timeout job all consult.
            await _register_pending_approval(
                approval_store=approval_store,
                audit_logger=audit_logger,
                record=record,
                request=pending_request,
            )
            # Surface the gate to live SSE clients deterministically: a
            # dedicated ``approval`` event (mirrored to the event store) so the
            # Playground renders the pending gate without having to infer the
            # pause by polling ``/v1/approvals`` after the terminal ``end``
            # frame — that poll never fires when the client misses ``end``.
            approval_payload = {
                "run_id": str(run_id),
                "thread_id": str(record.thread_id),
                **pending_request.model_dump(mode="json"),
            }
            await bridge.publish(run_id, "approval", approval_payload)
            _enqueue_event("approval", approval_payload)
            # PAUSED is itself a terminal branch here (no further set_status
            # call follows on this path within this function invocation) —
            # flush now so the approval row is durable before run_agent
            # returns, matching the drain guarantee every other terminal
            # branch gives its own last enqueued frame.
            await _drain_persist_queue()
        if final is not RunStatus.PAUSED:
            await _emit_run_end_audit(
                audit_logger,
                record,
                action=AuditAction.RUN_COMPLETED,
                result=AuditResult.SUCCESS,
                reason=None,
                status="interrupted" if final is RunStatus.INTERRUPTED else "success",
            )
            # Stream L.L7 — record the trajectory for the J.13 eval gate.
            # Fire-and-forget; failures are swallowed inside the recorder.
            _dispatch_trajectory(
                trajectory_recorder,
                graph,
                effective_config,
                record,
                outcome="cancelled" if final is RunStatus.INTERRUPTED else "success",
                metadata={"retried": retry_attempts} if retry_attempts else None,
            )
            _dispatch_skill_run_usage(
                skill_run_usage_recorder,
                record,
                outcome="cancelled" if final is RunStatus.INTERRUPTED else "success",
            )

    except RunCancelledError:
        # A node surfaced cooperative cancellation mid-step (E.15) — a
        # normal interrupted finish, not a failure.
        session_outcome = "interrupted"
        await _drain_persist_queue()
        await run_manager.set_status(run_id, RunStatus.INTERRUPTED)
        logger.info("run_agent.cancelled_cooperatively run_id=%s", run_id)
        await _emit_run_end_audit(
            audit_logger,
            record,
            action=AuditAction.RUN_COMPLETED,
            result=AuditResult.SUCCESS,
            reason=None,
            status="interrupted",
        )
        _dispatch_trajectory(
            trajectory_recorder,
            graph,
            effective_config,
            record,
            outcome="cancelled",
            metadata={"retried": retry_attempts} if retry_attempts else None,
        )
        _dispatch_skill_run_usage(skill_run_usage_recorder, record, outcome="cancelled")
    except asyncio.CancelledError:
        # Task-level cancellation (event-loop shutdown / explicit
        # task.cancel()). The cooperative abort_event path above is the
        # normal cancel route; this is the defensive backstop. We do
        # NOT dispatch the trajectory here — awaiting any helper during
        # loop teardown is unreliable (same reason
        # ``_emit_run_end_audit`` is skipped on this path).
        session_outcome = "cancelled"
        await run_manager.set_status(run_id, RunStatus.INTERRUPTED)
        logger.info("run_agent.cancelled run_id=%s", run_id)
        raise
    except MaxStepsExceededError as exc:
        # Distinct from a generic failure — the agent hit its iteration
        # budget. The J.13 eval gate filters this bucket separately
        # because "budget exhausted" is a tunable trade-off, not a
        # provider / code failure.
        session_outcome = "max_steps"
        if retry_attempts:
            run_retry_total.labels(outcome="failed_again").inc()
        await run_manager.set_status(run_id, RunStatus.ERROR, error=str(exc))
        logger.warning(
            "run_agent.max_steps_exceeded run_id=%s step_count=%d max_steps=%d",
            run_id,
            exc.step_count,
            exc.max_steps,
        )
        error_payload = {"message": str(exc), "name": type(exc).__name__}
        await bridge.publish(run_id, "error", error_payload)
        _enqueue_event("error", error_payload)
        # set_status(ERROR) already fired above (before the error frame
        # existed) — drain here instead flushes it (+ anything still queued)
        # before the run's terminal path continues.
        await _drain_persist_queue()
        await _emit_run_end_audit(
            audit_logger,
            record,
            action=AuditAction.RUN_FAILED,
            result=AuditResult.ERROR,
            reason=str(exc),
            status="error",
        )
        _dispatch_trajectory(
            trajectory_recorder,
            graph,
            effective_config,
            record,
            outcome="max_steps",
            metadata={"retried": retry_attempts} if retry_attempts else None,
        )
        _dispatch_skill_run_usage(skill_run_usage_recorder, record, outcome="max_steps")
    except Exception as exc:
        session_outcome = "error"
        if retry_attempts:
            run_retry_total.labels(outcome="failed_again").inc()
        await run_manager.set_status(run_id, RunStatus.ERROR, error=str(exc))
        logger.exception("run_agent.failed run_id=%s", run_id)
        error_payload = {"message": str(exc), "name": type(exc).__name__}
        await bridge.publish(run_id, "error", error_payload)
        _enqueue_event("error", error_payload)
        # Same rationale as the MaxSteps branch above.
        await _drain_persist_queue()
        await _emit_run_end_audit(
            audit_logger,
            record,
            action=AuditAction.RUN_FAILED,
            result=AuditResult.ERROR,
            reason=str(exc),
            status="error",
        )
        _dispatch_trajectory(
            trajectory_recorder,
            graph,
            effective_config,
            record,
            outcome="failed",
            metadata={"retried": retry_attempts} if retry_attempts else None,
        )
        _dispatch_skill_run_usage(skill_run_usage_recorder, record, outcome="failed")
    finally:
        # 二期 PR3 — sentinel(None)tells the background persist writer to
        # flush its tail and exit. Synchronous put — safe even on the
        # asyncio.CancelledError teardown path above (no await). QueueFull
        # uses the same drop-oldest scheme as ``_enqueue_event`` to
        # guarantee room; the writer is self-collecting via
        # ``_BACKGROUND_PERSIST_WRITERS`` so nothing here awaits it.
        try:
            persist_queue.put_nowait(None)
        except asyncio.QueueFull:
            try:
                persist_queue.get_nowait()
                persist_queue.task_done()
            except asyncio.QueueEmpty:
                # writer 恰在 QueueFull 与 get_nowait 之间清空了队列——位子
                # 已腾出,sentinel 直接重投(此路径必须全同步,不能 await)。
                pass
            persist_queue.put_nowait(None)
        # Stream 9.4 — stop renewing the lease; the terminal status write
        # already moved the run out of ``running`` so it's no longer an orphan
        # candidate regardless of the now-stale lease.
        if heartbeat_task is not None:
            heartbeat_task.cancel()
            # ``gather(return_exceptions=True)`` awaits the cancelled task to
            # completion and swallows its ``CancelledError``. Awaiting a call
            # expression (not a bare ``await name``) also keeps CodeQL from
            # reading the teardown as an ineffectual statement.
            await asyncio.gather(heartbeat_task, return_exceptions=True)
        # Stream M Gate — emit on every terminal path. Always synchronous
        # so the ``asyncio.CancelledError`` teardown path (no await) still
        # counts the session in the histogram.
        _session_duration_seconds.labels(outcome=session_outcome).observe(
            time.monotonic() - session_started
        )
        await bridge.publish_end(run_id)
        # Fire-and-forget cleanup; keep a reference so the task isn't
        # garbage-collected mid-flight.
        cleanup_task = asyncio.create_task(bridge.cleanup(run_id, delay=_CLEANUP_DELAY_S))
        _BACKGROUND_CLEANUP_TASKS.add(cleanup_task)
        cleanup_task.add_done_callback(_BACKGROUND_CLEANUP_TASKS.discard)
        reset_current_run_id(run_id_token)


#: Strong refs to in-flight cleanup tasks — without this the event loop
#: may garbage-collect a bare ``create_task`` result before it runs.
_BACKGROUND_CLEANUP_TASKS: set[asyncio.Task[None]] = set()

# 二期 PR3 — run_event 后台批写 writer。sentinel(None)= 收尾:flush 余量后退出。
_PERSIST_QUEUE_MAX: Final = 512
_PERSIST_BATCH_MAX: Final = 32
_PERSIST_FLUSH_INTERVAL_S: Final = 0.1
_PERSIST_DRAIN_TIMEOUT_S: Final = 5.0
_BACKGROUND_PERSIST_WRITERS: set[asyncio.Task[None]] = set()

_run_event_queue_dropped = expert_work_counter(
    "expert_work_run_event_queue_dropped_total",
    "Frames dropped from the run_event persist queue (drop-oldest on overflow).",
    ("event_name",),
)

#: Sentinel distinguishing "queue.get() timed out, no item" from a real
#: item (including the ``None`` shutdown sentinel) in ``_persist_writer``.
_NO_ITEM: Final = object()


async def _persist_writer(
    event_store: RunEventStore,
    queue: asyncio.Queue[RunEventRecord | None],
    *,
    run_id: UUID,
) -> None:
    """Drain ``queue`` in batches into ``event_store.append_batch``.

    H-7 立场:batch 写失败 → counter + warning,继续下一批;绝不向上抛。
    收到 sentinel(None)→ flush 剩余 → 退出。
    """
    batch: list[RunEventRecord] = []
    stopping = False
    while not stopping:
        item: RunEventRecord | None | object
        try:
            item = await asyncio.wait_for(queue.get(), timeout=_PERSIST_FLUSH_INTERVAL_S)
        except TimeoutError:
            item = _NO_ITEM
        if item is _NO_ITEM:
            pass
        elif item is None:
            stopping = True
            queue.task_done()
        elif isinstance(item, RunEventRecord):
            # task_done 延迟到 flush 之后——drain 的 queue.join() 语义必须是
            # 「帧已落库(或已尽力)」,不是「writer 已拿走」。在 get 时就
            # task_done 会让 join() 在批未 flush 时假完成,终态先于帧落库。
            # (isinstance, not a bare ``else``: ``item``'s static type is
            # ``RunEventRecord | None | object`` — ``object`` swallows the
            # ``is`` checks above for mypy's narrowing, so this branch needs
            # an explicit runtime check to narrow back to ``RunEventRecord``.)
            batch.append(item)
        else:
            # Off-type item should never occur — the queue only ever carries
            # RunEventRecord / None(sentinel) / the local _NO_ITEM marker.
            # But silently doing nothing here would skip task_done() for a
            # slot that was already queue.get()'d, and _drain_persist_queue's
            # persist_queue.join() would then hang forever waiting for a
            # count that can never reach zero. Fail loud, not silent-hang.
            logger.warning("run_event.unexpected_queue_item run_id=%s item=%r", run_id, item)
            queue.task_done()
        # ``or queue.empty()`` — flush as soon as the backlog is drained
        # instead of waiting out the rest of the idle timeout with a
        # partial batch sitting unflushed. Without this, a terminal drain
        # (_drain_persist_queue) that clears the queue mid-batch pays up to
        # one full _PERSIST_FLUSH_INTERVAL_S (100ms) of pure idle wait before
        # the next ``get()`` times out and triggers the flush — a tax that
        # lands on every run's terminal path for no reason once nothing is
        # left to batch with. Idle (nothing ever arrived) is unaffected: the
        # 100ms wait there is genuine polling cadence, not a batching delay.
        if batch and (
            len(batch) >= _PERSIST_BATCH_MAX or item is _NO_ITEM or stopping or queue.empty()
        ):
            await _flush_batch(event_store, batch, run_id=run_id)
            for _ in batch:
                queue.task_done()
            batch = []
    # sentinel 后队列可能仍有余量(CancelledError 路径 put_nowait 竞态)——清空
    tail_count = 0
    while True:
        try:
            tail = queue.get_nowait()
        except asyncio.QueueEmpty:
            break
        tail_count += 1
        if tail is not None:
            batch.append(tail)
    if batch:
        await _flush_batch(event_store, batch, run_id=run_id)
    for _ in range(tail_count):
        queue.task_done()


async def _flush_batch(
    event_store: RunEventStore, batch: list[RunEventRecord], *, run_id: UUID
) -> None:
    try:
        await event_store.append_batch(batch)
        for rec in batch:
            _run_event_persist_total.labels(event_name=rec.event_name).inc()
    except Exception as exc:
        for rec in batch:
            _run_event_persist_errors.labels(event_name=rec.event_name).inc()
        logger.warning(
            "run_event.batch_persist_failed run_id=%s count=%s err=%s",
            run_id,
            len(batch),
            exc,
        )


#: Strong refs to in-flight trajectory dispatch tasks (Stream L.L7) —
#: same garbage-collection guard as ``_BACKGROUND_CLEANUP_TASKS``.
_BACKGROUND_TRAJECTORY_TASKS: set[asyncio.Task[None]] = set()

#: Wall-clock cap on trajectory-record dispatch. The recorder swallows
#: its own errors; this deadline guards against an unrecoverably-slow
#: ObjectStore put dragging the run's terminal path or piling up tasks.
_TRAJECTORY_DISPATCH_TIMEOUT_S: float = 5.0

#: Strong refs to in-flight skill-run-usage dispatch tasks (Stream SE,
#: SE-7d-3b-ii) — same GC guard as the trajectory tasks.
_BACKGROUND_SKILL_USAGE_TASKS: set[asyncio.Task[None]] = set()


def _dispatch_trajectory(
    recorder: TrajectoryRecorder | None,
    graph: StreamableGraph,
    config: RunnableConfig,
    record: RunRecord,
    *,
    outcome: TrajectoryOutcome,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Stream L.L7 — schedule a fire-and-forget trajectory write.

    Returns immediately. The actual ObjectStore I/O happens in a
    background task with a hard timeout so a slow / broken store
    cannot stall the run's terminal path (TraJectoryRecorder.record
    swallows its own errors; this deadline is the outer guard).
    ``recorder=None`` is a no-op — manifest opt-out or recorder not
    configured in this deployment.
    """
    if recorder is None:
        return
    task = asyncio.create_task(
        _record_trajectory_safe(recorder, graph, config, record, outcome=outcome, metadata=metadata)
    )
    _BACKGROUND_TRAJECTORY_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TRAJECTORY_TASKS.discard)


async def _record_trajectory_safe(
    recorder: TrajectoryRecorder,
    graph: StreamableGraph,
    config: RunnableConfig,
    record: RunRecord,
    *,
    outcome: TrajectoryOutcome,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Background body for :func:`_dispatch_trajectory`."""
    try:
        async with asyncio.timeout(_TRAJECTORY_DISPATCH_TIMEOUT_S):
            messages = await _fetch_final_messages(graph, config)
            tenant_id = _tenant_id_from_config(config) or record.tenant_id
            user_id = _user_id_from_config(config)
            await recorder.record(
                TrajectoryRecord(
                    thread_id=record.thread_id,
                    tenant_id=tenant_id,
                    outcome=outcome,
                    messages=messages,
                    user_id=user_id,
                    run_id=record.run_id,
                    finished_at=datetime.now(UTC),
                    metadata=dict(metadata) if metadata else {},
                )
            )
    except TimeoutError:
        logger.warning(
            "run_agent.trajectory_dispatch_timeout run_id=%s outcome=%s",
            record.run_id,
            outcome,
        )
    except Exception:
        # The recorder catches its own errors; reaching here means the
        # state fetch itself failed. Best-effort by design.
        logger.exception(
            "run_agent.trajectory_dispatch_failed run_id=%s outcome=%s",
            record.run_id,
            outcome,
        )


def _dispatch_skill_run_usage(
    recorder: SkillRunUsageRecorder | None,
    record: RunRecord,
    *,
    outcome: TrajectoryOutcome,
) -> None:
    """Stream SE (SE-7d-3b-ii) — fire-and-forget ``skill_run_usage`` emit.

    One row per distilled skill version bound into this run, tagged with the
    terminal ``outcome``, so the rollback monitor can attribute regressions.
    No-op when no recorder is wired or no distilled skill was bound. Returns
    immediately; the write happens in a background task with a hard timeout so
    a slow store cannot stall the run's terminal path.
    """
    if recorder is None or not record.bound_distilled_skills:
        return
    task = asyncio.create_task(_record_skill_run_usage_safe(recorder, record, outcome=outcome))
    _BACKGROUND_SKILL_USAGE_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_SKILL_USAGE_TASKS.discard)


async def _record_skill_run_usage_safe(
    recorder: SkillRunUsageRecorder,
    record: RunRecord,
    *,
    outcome: TrajectoryOutcome,
) -> None:
    """Background body for :func:`_dispatch_skill_run_usage`."""
    try:
        async with asyncio.timeout(_TRAJECTORY_DISPATCH_TIMEOUT_S):
            for skill in record.bound_distilled_skills:
                await recorder.record(
                    skill_id=skill.skill_id,
                    skill_version=skill.skill_version,
                    tenant_id=record.tenant_id,
                    agent_name=skill.agent_name,
                    thread_id=record.thread_id,
                    outcome=outcome,
                )
    except TimeoutError:
        logger.warning(
            "run_agent.skill_run_usage_timeout run_id=%s outcome=%s", record.run_id, outcome
        )
    except Exception:
        # The recorder swallows its own errors; this is the belt-and-braces
        # guard. Best-effort by design — never fail the run's terminal path.
        logger.exception(
            "run_agent.skill_run_usage_failed run_id=%s outcome=%s", record.run_id, outcome
        )


async def _fetch_final_messages(
    graph: StreamableGraph, config: RunnableConfig
) -> Sequence[BaseMessage]:
    """Best-effort fetch of the run's terminal ``messages`` list.

    Tries ``graph.aget_state(config)`` if the graph exposes it (the
    real ``CompiledStateGraph`` does; test stubs may not). On any
    failure the returned list is empty — the recorder will still
    write the envelope, just without conversation content. That's
    preferable to crashing the dispatch task.
    """
    aget_state = getattr(graph, "aget_state", None)
    if not callable(aget_state):
        return []
    try:
        snapshot = await aget_state(config)
    except Exception:
        logger.exception("run_agent.trajectory_state_fetch_failed")
        return []
    values = getattr(snapshot, "values", None)
    if not isinstance(values, Mapping):
        return []
    raw = values.get("messages")
    if not isinstance(raw, Sequence):
        return []
    out: list[BaseMessage] = [m for m in raw if isinstance(m, BaseMessage)]
    return out


def _tenant_id_from_config(config: RunnableConfig) -> UUID | None:
    configurable = config.get("configurable") or {}
    return _maybe_uuid(configurable.get("tenant_id"))


def _user_id_from_config(config: RunnableConfig) -> UUID | None:
    configurable = config.get("configurable") or {}
    return _maybe_uuid(configurable.get("user_id"))


def _maybe_uuid(raw: object) -> UUID | None:
    if isinstance(raw, UUID):
        return raw
    if isinstance(raw, str):
        try:
            return UUID(raw)
        except ValueError:
            return None
    return None


async def _emit_run_end_audit(
    audit_logger: AuditLogger | None,
    record: RunRecord,
    *,
    action: AuditAction,
    result: AuditResult,
    reason: str | None,
    status: str,
) -> None:
    """Write one run-lifecycle audit row.

    The orchestrator worker is the actor (``actor_type="system"``); the
    row is keyed to the run's session so it sits alongside the
    ``session:write`` row the control-plane emits at run start. An
    audit-write failure is logged and swallowed — it must never fail
    an otherwise-finished run.
    """
    if audit_logger is None:
        return
    try:
        await audit_logger.write(
            AuditEntry(
                tenant_id=record.tenant_id,
                actor_type="system",
                actor_id="orchestrator",
                action=action,
                resource_type="session",
                resource_id=str(record.thread_id),
                result=result,
                reason=reason,
                details={"run_id": str(record.run_id), "status": status},
            )
        )
    except Exception:
        logger.exception("run_agent.audit_failed run_id=%s", record.run_id)


async def _register_pending_approval(
    *,
    approval_store: ApprovalStore | None,
    audit_logger: AuditLogger | None,
    record: RunRecord,
    request: ApprovalRequest,
) -> None:
    """Persist a paused run's ``agent_approval`` row + emit APPROVAL_REQUESTED.

    Stream J.8 (Mini-ADR J-24). The durable row — not the in-memory
    RunManager — is what survives a control-plane restart so the resume
    endpoint / GET surface / 24h timeout job can all find the paused
    run. A store / audit failure is logged and swallowed: the run
    already paused cleanly with its checkpoint intact; a missing
    registry row degrades to "resume-by-checkpoint only", never a
    crash.
    """
    if approval_store is not None:
        try:
            await approval_store.create(
                ApprovalRecord(
                    id=uuid4(),
                    tenant_id=record.tenant_id,
                    user_id=None,
                    run_id=record.run_id,
                    thread_id=record.thread_id,
                    request_id=request.request_id,
                    node=request.node,
                    reason_kind=request.reason_kind,
                    action_summary=request.action_summary,
                    proposed_args=request.proposed_args,
                    requested_at=request.requested_at,
                    timeout_at=request.timeout_at,
                    # RT-6 Tier A (RT-ADR-19) — persist the mint-time args digest
                    # on the row (integrity domain independent of the checkpoint).
                    binding_digest=request.binding_digest,
                )
            )
        except Exception:
            logger.exception("run_agent.approval_register_failed run_id=%s", record.run_id)
    if audit_logger is not None:
        try:
            await audit_logger.write(
                AuditEntry(
                    tenant_id=record.tenant_id,
                    actor_type="system",
                    actor_id="orchestrator",
                    action=AuditAction.APPROVAL_REQUESTED,
                    resource_type="approval",
                    resource_id=str(record.run_id),
                    result=AuditResult.SUCCESS,
                    reason=None,
                    details={
                        "run_id": str(record.run_id),
                        "thread_id": str(record.thread_id),
                        "node": request.node,
                        "reason_kind": request.reason_kind,
                        "action_summary": request.action_summary,
                    },
                )
            )
        except Exception:
            logger.exception("run_agent.approval_audit_failed run_id=%s", record.run_id)


# ---------------------------------------------------------------------------
# Consumer — SSE generator
# ---------------------------------------------------------------------------


async def sse_consumer(
    *,
    bridge: StreamBridge,
    record: RunRecord,
    run_manager: RunManager,
    is_disconnected: Callable[[], Awaitable[bool]],
    last_event_id: str | None = None,
    heartbeat_interval: float = 15.0,
) -> AsyncIterator[bytes]:
    """Yield SSE wire frames for ``record``'s run.

    Subscribes to ``bridge`` and translates each :class:`StreamEvent`
    into an SSE frame. :data:`HEARTBEAT_SENTINEL` becomes an SSE comment
    (``: heartbeat``); :data:`END_SENTINEL` becomes a final ``end``
    event and terminates the generator.

    ``is_disconnected`` is an injected coroutine (FastAPI's
    ``request.is_disconnected`` in production) — checked before each
    yield so a vanished client stops the stream promptly.

    The ``finally`` block enforces ``on_disconnect``: if the run is
    still in flight and its mode is :data:`DisconnectMode.CANCEL`, the
    run is cancelled. A run that already finished is left untouched.
    """
    try:
        async for entry in bridge.subscribe(
            record.run_id,
            last_event_id=last_event_id,
            heartbeat_interval=heartbeat_interval,
        ):
            if await is_disconnected():
                logger.info("sse_consumer.client_disconnected run_id=%s", record.run_id)
                break

            if entry is HEARTBEAT_SENTINEL:
                yield b": heartbeat\n\n"
                continue

            if entry is END_SENTINEL:
                yield format_sse("end", None)
                return

            yield format_sse(entry.event, entry.data, event_id=entry.id or None)
    finally:
        from expert_work.runtime.runs import DisconnectMode

        if (
            record.status in (RunStatus.PENDING, RunStatus.RUNNING)
            and record.on_disconnect is DisconnectMode.CANCEL
        ):
            await run_manager.cancel(record.run_id)


def format_sse(event: str, data: Any, *, event_id: str | None = None) -> bytes:
    """Render one SSE event in wire format (``id:`` / ``event:`` / ``data:``).

    ``data`` is JSON-encoded; ``None`` renders as ``data: null``. Frames
    end with the blank line the SSE spec requires as a record separator.
    """
    lines: list[str] = []
    if event_id:
        lines.append(f"id: {event_id}")
    lines.append(f"event: {event}")
    lines.append(f"data: {json.dumps(data, separators=(',', ':'))}")
    return ("\n".join(lines) + "\n\n").encode("utf-8")


# ---------------------------------------------------------------------------
# Chunk serialisation
# ---------------------------------------------------------------------------


def _to_jsonable(value: Any) -> Any:
    """Recursively convert a LangGraph stream chunk to JSON-safe data.

    LangGraph ``updates`` chunks are ``{node: {channel: value}}`` dicts
    whose values include :class:`BaseMessage` instances, UUIDs, and
    datetimes — none JSON-serialisable as-is. Conversions:

    - :class:`BaseMessage` → ``msg.model_dump()`` (LangChain's canonical
      dict form: type / content / id / tool_calls / ...).
    - :class:`pydantic.BaseModel` → ``model_dump(mode="json")``.
    - dataclass instance → ``dataclasses.asdict`` (recursed).
    - :class:`UUID` → ``str``; :class:`datetime` → ISO-8601 string.
    - ``Mapping`` / ``Sequence`` → recurse element-wise.
    - Anything else with no obvious encoding → ``str(value)`` fallback
      (a stream event must never crash the run on a stray type).
    """
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, BaseMessage):
        return _to_jsonable(value.model_dump())
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, BaseModel):
        # pydantic v2 → canonical JSON-mode dict (UUID/datetime already
        # stringified); recurse for any residual non-JSON leaf.
        return _to_jsonable(value.model_dump(mode="json"))
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        # frozen dataclasses (e.g. ClassifiedToolError) → dict; recurse so
        # nested UUID/datetime leaves get stringified.
        return _to_jsonable(dataclasses.asdict(value))
    if isinstance(value, Mapping):
        return {str(k): _to_jsonable(v) for k, v in value.items()}
    if isinstance(value, Sequence | set | frozenset):
        return [_to_jsonable(v) for v in value]
    return str(value)
