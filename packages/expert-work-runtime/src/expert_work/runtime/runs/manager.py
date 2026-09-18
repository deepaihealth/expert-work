# ============================================================
# Adapted from bytedance/deer-flow @ 813d3c94efa7fdea6aafcb4f459304db91fcaed0
# Source: backend/packages/harness/deerflow/runtime/runs/manager.py
# License: MIT (see vendor LICENSE)
# Modifications:
#   - In-memory per-process registry; Mini-ADR J-41 adds a durable
#     RunStore mirror (agent_run table) so run status survives the
#     5-minute TTL sweep + control-plane restarts
#   - run_id / thread_id typed as UUID (expert-work convention)
#   - Added tenant_id (ADR-0002 + Stream C.4 RLS) + user_id
#   - Dropped assistant_id / multitask_strategy / metadata / kwargs —
#     run queueing / retry / DLQ are J.10 work (Mini-ADR J-26)
#   - Lock retained from DeerFlow; mutations are serialized
# Last sync: 2026-05-11
# ============================================================

"""In-memory ``RunManager`` — per-process run lifecycle registry."""

from __future__ import annotations

import asyncio
import logging
import os
import socket
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from expert_work.common.skill_run_usage import BoundDistilledSkill
from expert_work.runtime.runs.schemas import (
    TERMINAL_RUN_STATUSES,
    DisconnectMode,
    RunInfo,
    RunStatus,
)
from expert_work.runtime.runs.store import RunStore

logger = logging.getLogger(__name__)

#: B-80 观测钩子 —— ``(事件名, 受影响的 run 记录, 收口上限秒)``。
#: 事件名只有两个:``drain_waiting``(开始等)与 ``drain_hard_stopped``(到点硬停)。
type DrainObserver = Callable[[str, list[RunRecord], float], Awaitable[None]]


@dataclass
class RunRecord:
    """Mutable per-run state held in the in-memory registry.

    The ``task`` and ``abort_event`` fields back live orchestrator execution;
    they are not serialized.
    """

    run_id: UUID
    thread_id: UUID
    tenant_id: UUID
    status: RunStatus
    user_id: UUID | None = None
    on_disconnect: DisconnectMode = DisconnectMode.CANCEL
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    task: asyncio.Task[None] | None = field(default=None, repr=False)
    abort_event: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    #: Stream K.K10 — whether this run resumed an existing checkpointed
    #: thread (caller computes from prior ``thread_meta`` state). The SSE
    #: worker observes ``expert_work_durable_resume_seconds`` only when ``True``
    #: so the histogram cleanly tracks SLO #5 (durable resume latency).
    is_resume: bool = False
    #: Stream H.3 PR 2 (Mini-ADR H-9.5) — OTel trace id captured at
    #: create time by the caller. ``None`` for auto-triggered runs
    #: (scheduler / J.13a curation) where no caller-bound trace exists.
    trace_id: str | None = None
    #: P-1 —— 这一轮是对哪个旧 run 的重新生成 / 编辑重发;``None`` = 普通轮。
    #: 与 ``trace_id`` 同样是建行时写死、之后不再改的字段。
    regenerated_from_run_id: UUID | None = None
    #: Stream SE (SE-7d-3b-ii) — distilled skill versions bound into this run's
    #: agent at build time (from ``BuiltAgent.bound_distilled_skills``). The SSE
    #: worker emits one ``skill_run_usage`` row per entry at the run's terminal
    #: hook so the rollback monitor can attribute the outcome. Not serialized.
    bound_distilled_skills: tuple[BoundDistilledSkill, ...] = ()
    #: 产物清单契约 —— 审批续跑(continuation)是**新** run_id、新 durable 行,
    #: 父 run PAUSED 时固化的清单不在自己行上;decide 端点把父行清单挂在这里,
    #: ``run_agent`` 的 resume seeding 优先读它(fallback 仍读自己的行,兜住
    #: 同 run_id 重入的形态)。Not serialized。
    seed_artifacts: list[dict[str, Any]] | None = None
    #: External-API-v1 P2-a Task 14 — the caller's ``Idempotency-Key`` /
    #: request-fingerprint, write-once at creation (mirrors ``trace_id``
    #: above: read only by :func:`_record_to_info`, never mutated after
    #: create). ``None`` for the vast majority of runs — only a stream-mode
    #: external run created with a header carries these.
    idempotency_key: str | None = None
    request_digest: str | None = None


def _record_to_info(record: RunRecord) -> RunInfo:
    """Project a :class:`RunRecord` into the persistable :class:`RunInfo`.

    Called at run creation — ``error`` / ``finished_at`` are always
    ``None`` for a fresh PENDING run; later transitions reach the store
    through :meth:`RunManager.set_status`.
    """
    return RunInfo(
        run_id=record.run_id,
        tenant_id=record.tenant_id,
        thread_id=record.thread_id,
        user_id=record.user_id,
        status=record.status,
        on_disconnect=record.on_disconnect,
        is_resume=record.is_resume,
        error=None,
        created_at=record.created_at,
        updated_at=record.updated_at,
        finished_at=None,
        trace_id=record.trace_id,
        idempotency_key=record.idempotency_key,
        request_digest=record.request_digest,
        regenerated_from_run_id=record.regenerated_from_run_id,
    )


def _default_instance_id() -> str:
    """Stream 9.4 — a stable-per-process control-plane instance id.

    ``hostname-pid-<rand>``: the hostname + pid identify the process; the random
    suffix disambiguates a fast restart that reused the pid. Stamped as
    ``agent_run.claimed_by`` so the orphan sweep attributes a run to the
    instance executing it.
    """
    return f"{socket.gethostname()}-{os.getpid()}-{uuid4().hex[:8]}"


class RunManager:
    """Per-process registry of active runs.

    All mutations are serialized by an :class:`asyncio.Lock`. When a
    :class:`RunStore` is supplied (Mini-ADR J-41) every create / status
    transition is mirror-written to the durable ``agent_run`` table so
    a run's status outlives the in-memory record's 5-minute TTL.
    """

    def __init__(
        self,
        store: RunStore | None = None,
        *,
        instance_id: str | None = None,
        lease_ttl_s: float = 30.0,
    ) -> None:
        self._runs: dict[UUID, RunRecord] = {}
        self._lock = asyncio.Lock()
        #: Durable mirror (Mini-ADR J-41). ``None`` keeps the registry
        #: purely in-memory — unit tests + the default app before the
        #: SQL backend is wired.
        self._store = store
        #: Stream 9.4 (HA failover) — this control-plane instance's stable id.
        #: Stamped as ``agent_run.claimed_by`` on the → RUNNING transition so a
        #: peer's orphan sweep can tell a crashed owner's runs from live ones.
        self._instance_id = instance_id or _default_instance_id()
        #: How long a lease is valid; the worker renews it every
        #: ``lease_ttl_s / 3`` via :meth:`heartbeat`, so two missed heartbeats
        #: still leave margin before a peer reclaims.
        self._lease_ttl_s = lease_ttl_s
        #: B-80 —— 本进程是否已进入优雅关机。``run_agent`` 的
        #: ``asyncio.CancelledError`` 兜底分支据此分辨「关机取消」(交接)与
        #: 「用户取消 / 断流」(照旧收成 INTERRUPTED)。单向,不回退。
        self._shutting_down = False

    @property
    def store(self) -> RunStore | None:
        """The durable mirror this manager writes through, if any.

        Exposed so a caller that must write a column the manager has no method
        for (``run_trace.bind_exec_spec``) reaches the store **holding this
        run's row** rather than guessing at ``app.state.run_store`` — those are
        the same object in the wired app but not in tests that inject their own
        runtime. ``None`` for the purely in-memory registry.
        """
        return self._store

    @property
    def instance_id(self) -> str:
        return self._instance_id

    @property
    def shutting_down(self) -> bool:
        """B-80 —— 本进程是否已进入优雅关机。见 :meth:`mark_shutting_down`。"""
        return self._shutting_down

    def mark_shutting_down(self) -> None:
        """进入优雅关机。**在取消任何 run 任务之前**由 app lifespan 调用一次。

        顺序是这条的全部意义:标志必须先于取消置起,否则被取消的 run 在兜底
        分支里读到的还是 ``False``,会照老路收成 INTERRUPTED —— 正是 B-80。
        同步方法(不抢 :attr:`_lock`):关机路径上拿不到锁就形同没置。
        """
        self._shutting_down = True

    @property
    def lease_ttl_s(self) -> float:
        return self._lease_ttl_s

    async def create(
        self,
        *,
        run_id: UUID,
        thread_id: UUID,
        tenant_id: UUID,
        user_id: UUID | None = None,
        on_disconnect: DisconnectMode = DisconnectMode.CANCEL,
        is_resume: bool = False,
        trace_id: str | None = None,
        idempotency_key: str | None = None,
        request_digest: str | None = None,
        regenerated_from_run_id: UUID | None = None,
    ) -> RunRecord:
        """Create + register a new run in PENDING state.

        ``is_resume`` (Stream K.K10) flags the run as resuming a thread
        with a non-empty checkpoint so the SSE worker can observe the
        durable-resume histogram only on those runs.

        ``trace_id`` (Stream H.3 PR 2 — Mini-ADR H-9.5) is the OTel
        trace id the caller observed; pass ``None`` for auto-triggered
        runs that have no user-bound trace. The value is written through
        to the durable ``agent_run`` row as part of the initial insert.

        ``idempotency_key`` / ``request_digest`` (External-API-v1 P2-a
        Task 14) mirror the same-named parameters :meth:`enqueue` already
        accepts (Task 13) — this is the stream-mode counterpart. Threaded
        straight onto the ``RunInfo`` so ``self._store.create`` makes "claim
        the key" and "create the run row" the same atomic insert; a
        colliding key raises :class:`~expert_work.runtime.runs.store.
        RunIdempotencyConflict` out of this call, same as ``enqueue``. Both
        default to ``None`` — every pre-existing caller (the internal
        session-run endpoint, and every stream-mode run before this task)
        is unaffected.
        """
        async with self._lock:
            if run_id in self._runs:
                msg = f"run_id={run_id} already exists"
                raise ValueError(msg)
            record = RunRecord(
                run_id=run_id,
                thread_id=thread_id,
                tenant_id=tenant_id,
                user_id=user_id,
                status=RunStatus.PENDING,
                on_disconnect=on_disconnect,
                is_resume=is_resume,
                trace_id=trace_id,
                idempotency_key=idempotency_key,
                request_digest=request_digest,
                regenerated_from_run_id=regenerated_from_run_id,
            )
            # Mirror to the durable store before the in-memory insert —
            # a store failure then leaves no orphan registry entry.
            if self._store is not None:
                await self._store.create(_record_to_info(record))
            self._runs[run_id] = record
            logger.info("run.create id=%s thread=%s tenant=%s", run_id, thread_id, tenant_id)
            return record

    async def enqueue(
        self,
        *,
        run_id: UUID,
        thread_id: UUID,
        tenant_id: UUID,
        enqueued_input: dict[str, Any],
        user_id: UUID | None = None,
        is_resume: bool = False,
        trace_id: str | None = None,
        idempotency_key: str | None = None,
        request_digest: str | None = None,
        regenerated_from_run_id: UUID | None = None,
    ) -> None:
        """Persist a ``QUEUED`` run for the distributed queue (Stream 9.5).

        Unlike :meth:`create`, this writes only the durable row — the run
        belongs to no process yet, so there is no in-memory record and no
        ``asyncio.Task``. A :class:`RunQueueWorker` on any instance later
        CAS-claims it (``status='queued'`` → ``running``), adopts it, and
        executes it from ``enqueued_input``. Requires a durable store.

        External-API-v1 P2-a Task 13 — ``idempotency_key`` / ``request_digest``
        thread straight onto the ``RunInfo`` so ``self._store.create`` makes
        "claim the key" and "create the run row" the same atomic insert (the
        partial unique index backing that is on ``agent_run`` itself — see
        ``RunStore.create``'s docstring). A non-``None`` key that collides
        with an existing ``(tenant_id, idempotency_key)`` row raises
        :class:`~expert_work.runtime.runs.store.RunIdempotencyConflict` out of
        this call — the caller (the external run endpoint) catches it and
        re-queries the winner. Both default to ``None``, so every existing
        caller (the internal session-run endpoint) is unaffected: no key,
        no possible conflict, byte-identical behaviour to before this task.
        """
        if self._store is None:
            msg = "enqueue requires a durable RunStore"
            raise RuntimeError(msg)
        now = datetime.now(UTC)
        info = RunInfo(
            run_id=run_id,
            tenant_id=tenant_id,
            thread_id=thread_id,
            user_id=user_id,
            status=RunStatus.QUEUED,
            on_disconnect=DisconnectMode.CONTINUE,
            is_resume=is_resume,
            error=None,
            created_at=now,
            updated_at=now,
            finished_at=None,
            trace_id=trace_id,
            enqueued_input=enqueued_input,
            idempotency_key=idempotency_key,
            request_digest=request_digest,
            regenerated_from_run_id=regenerated_from_run_id,
        )
        await self._store.create(info)
        logger.info("run.enqueue id=%s thread=%s tenant=%s", run_id, thread_id, tenant_id)

    def get(self, run_id: UUID) -> RunRecord | None:
        """Snapshot lookup; safe outside the lock since dict reads are atomic."""
        return self._runs.get(run_id)

    async def list_by_thread(self, thread_id: UUID, *, tenant_id: UUID) -> list[RunRecord]:
        """Return all runs for ``thread_id`` belonging to ``tenant_id``."""
        async with self._lock:
            return [
                r
                for r in self._runs.values()
                if r.thread_id == thread_id and r.tenant_id == tenant_id
            ]

    async def delete_by_thread(self, thread_id: UUID, *, tenant_id: UUID) -> int:
        """Hard-delete a thread's runs from both the registry and the durable
        store (session purge). Returns the durable rows removed.

        Drops the in-memory records first so a concurrent lookup can't
        resurrect a purged run; then deletes the durable rows (the count
        callers report).
        """
        async with self._lock:
            victims = [
                rid
                for rid, r in self._runs.items()
                if r.thread_id == thread_id and r.tenant_id == tenant_id
            ]
            for rid in victims:
                del self._runs[rid]
        if self._store is None:
            return len(victims)
        return await self._store.delete_by_thread(thread_id=thread_id, tenant_id=tenant_id)

    async def set_status(
        self,
        run_id: UUID,
        status: RunStatus,
        *,
        error: str | None = None,
        artifacts: list[dict[str, Any]] | None = None,
    ) -> bool:
        """Update a run's status. Returns ``True`` iff the run exists
        **and the durable transition landed**.

        ``error`` carries the failure detail for ERROR / TIMEOUT
        transitions; it lands in the durable ``agent_run`` row. A
        transition into a terminal status also stamps ``finished_at``.
        ``artifacts``(产物清单契约)— the run's registration snapshot,
        written with the terminal status in the same store UPDATE so any
        reader that sees the terminal status also sees the manifest;
        ``None`` leaves the stored value untouched.

        多副本 CAS 守卫(两个洞,一处修):

        * → RUNNING 带 ``expected_statuses=(PENDING, QUEUED, RUNNING)``。
          守卫失败 = PENDING 窗口里已被跨副本 ``request_cancel`` 改成
          INTERRUPTED —— 本进程 record 镜像成 INTERRUPTED、``abort_event``
          置位、**不 claim**,返回 ``False``,调用方(``sse.py``)据此不跑图。
        * 终局写带 ``guard_claimed_by=self._instance_id``(NULL 也放行 ——
          从未 claim 过的 run 的终局写是合法的)。守卫失败 = 本副本的租约
          已被 orphan sweep 判死、run 被别的副本 reclaim 续跑 —— 迟到的
          终局写 no-op,不把新属主刚写的 running 盖掉;本进程 record 照旧
          镜像终局(本副本的执行确实结束了),返回 ``False``。
        """
        async with self._lock:
            record = self._runs.get(run_id)
            if record is None:
                return False
            now = datetime.now(UTC)
            if self._store is not None:
                is_terminal = status in TERMINAL_RUN_STATUSES
                landed = await self._store.set_status(
                    run_id=run_id,
                    tenant_id=record.tenant_id,
                    status=status,
                    updated_at=now,
                    error=error,
                    finished_at=now if is_terminal else None,
                    artifacts=artifacts,
                    expected_statuses=(
                        (RunStatus.PENDING, RunStatus.QUEUED, RunStatus.RUNNING)
                        if status is RunStatus.RUNNING
                        else None
                    ),
                    guard_claimed_by=self._instance_id if is_terminal else None,
                )
                if not landed and status is RunStatus.RUNNING:
                    # 洞 A —— 取消已经赢了,复活被守卫挡下。镜像真实终态并
                    # 让执行方停下;不 claim(行不属于任何执行)。
                    logger.warning(
                        "run.start_lost_to_cancel id=%s durable_row_no_longer_startable", run_id
                    )
                    record.status = RunStatus.INTERRUPTED
                    record.updated_at = now
                    record.abort_event.set()
                    return False
                if not landed:
                    # 洞 B —— 终局写迟到,run 已被别的副本 reclaim。本进程的
                    # 执行确实结束了:record 照旧镜像终局,durable 行归新属主。
                    logger.warning(
                        "run.terminal_write_after_reclaim id=%s status=%s dropped",
                        run_id,
                        status,
                    )
                    record.status = status
                    record.updated_at = now
                    return False
                # Stream 9.4 — claim the ownership lease when execution begins.
                # No explicit release at terminal status: the sweep + index both
                # gate on ``status='running'``, so a finished run is never an
                # orphan regardless of its (now stale) lease_until.
                if status is RunStatus.RUNNING:
                    await self._store.claim(
                        run_id=run_id,
                        tenant_id=record.tenant_id,
                        claimed_by=self._instance_id,
                        lease_until=now + timedelta(seconds=self._lease_ttl_s),
                        heartbeat_at=now,
                    )
            record.status = status
            record.updated_at = now
            logger.info("run.status_change id=%s status=%s", run_id, status)
            return True

    async def persisted_artifacts(
        self, run_id: UUID, *, tenant_id: UUID
    ) -> list[dict[str, Any]] | None:
        """durable 行上已固化的产物清单(产物清单契约)。

        resume(审批续跑)的 ``run_agent`` 用它把 PAUSED 时写入的清单读回
        累积器,续跑段的登记接着记而不是整表覆盖。无 store(单测)或行
        不存在时 ``None``。
        """
        if self._store is None:
            return None
        info = await self._store.get(run_id=run_id, tenant_id=tenant_id)
        return info.artifacts if info is not None else None

    async def adopt(
        self,
        *,
        run_id: UUID,
        thread_id: UUID,
        tenant_id: UUID,
        user_id: UUID | None = None,
    ) -> RunRecord:
        """Stream 9.4 — register an already-durable run this instance reclaimed.

        Unlike :meth:`create`, it does NOT write the store (the orphan row
        already exists; the sweep's reclaim CAS just took ownership of it). It
        only builds the in-memory :class:`RunRecord` (status RUNNING) so the
        re-spawned worker's heartbeat + terminal status writes route through the
        usual path. ``is_resume=True`` so the worker observes the durable-resume
        timing on its first chunk from the checkpoint.
        """
        async with self._lock:
            if run_id in self._runs:
                return self._runs[run_id]
            record = RunRecord(
                run_id=run_id,
                thread_id=thread_id,
                tenant_id=tenant_id,
                user_id=user_id,
                status=RunStatus.RUNNING,
                on_disconnect=DisconnectMode.CONTINUE,
                is_resume=True,
            )
            self._runs[run_id] = record
            logger.info("run.adopt id=%s thread=%s by=%s", run_id, thread_id, self._instance_id)
            return record

    async def heartbeat(self, run_id: UUID) -> bool:
        """Stream 9.4 — renew the run's lease; ``True`` iff this instance still owns it.

        The worker calls this periodically while executing. A ``False`` return
        means a peer reclaimed the run (this instance's lease lapsed, e.g. after
        a long GC pause) — the caller should stop to avoid double execution.
        No-op (returns ``True``) when no durable store is wired (unit tests).
        """
        if self._store is None:
            return True
        record = self._runs.get(run_id)
        if record is None:
            return False
        now = datetime.now(UTC)
        return await self._store.heartbeat(
            run_id=run_id,
            claimed_by=self._instance_id,
            lease_until=now + timedelta(seconds=self._lease_ttl_s),
            heartbeat_at=now,
        )

    async def drain_runs(self, *, timeout_s: float, on_event: DrainObserver | None = None) -> int:
        """B-80 —— 优雅关机收口:等在跑的 run 收尾,到点还没收的硬停。返回硬停几个。

        调用顺序是这条的全部意义,**必须**是:先 :meth:`mark_shutting_down`,
        再停队列 worker(不再认领新的),最后调这里。

        等待期间可安全交接的那些 run 会**自己**退出 —— ``run_agent`` 的关机哨兵
        发现它此刻可安全重放,就走协作取消把所有权交出去。所以 ``timeout_s`` 是
        **上限**不是固定等待:典型情形下几秒就回,只有悬空着不可重放工具的那几轮
        才会一直占到上限。

        到点仍在跑的一律 ``cancel()`` 并**等它跑完收口** —— ``run_agent`` 的
        ``asyncio.CancelledError`` 分支要在那里决定交接还是收成 INTERRUPTED,
        cancel 完就走会把这些写丢在半路(这正是「事件循环拆解期间 await 不可靠」
        那条注释说的情形;显式收口就是为了把这些写挪回循环还健康的时候)。

        ``on_event`` 是**观测**通道,不是控制通道:调用方拿它把「这个 pod 正等着
        N 个 run」落进审计。为什么必须落库 —— 关机期间 Prometheus 抓不到(pod 一
        进入 terminating 就从 Endpoints 摘除),pod 日志随 pod 回收,于是事后没有
        任何办法回答「它当时在等谁」。2026-09-18 实况:一条对话在跑,旧 pod 一直
        Terminating,smoke 报红、金丝雀被跳过,而**没有任何持久记录**说明它在等什么。
        观察者自己抛异常一律吞掉:观测不能拖垮关机。
        """
        waiting = [r for r in list(self._runs.values()) if r.task is not None and not r.task.done()]
        if not waiting:
            return 0
        logger.info("run.drain_started in_flight=%d timeout_s=%.1f", len(waiting), timeout_s)
        await self._notify_drain(on_event, "drain_waiting", waiting, timeout_s=timeout_s)
        live = [r.task for r in waiting if r.task is not None]
        await asyncio.wait(live, timeout=max(0.0, timeout_s))
        stalled = [r for r in waiting if r.task is not None and not r.task.done()]
        for record in stalled:
            if record.task is not None:
                record.task.cancel()
        if stalled:
            # ``return_exceptions=True`` 把每个任务的 ``CancelledError`` 吞掉,
            # 同时保证等到它们各自的收口分支真的跑完。
            await asyncio.gather(
                *[r.task for r in stalled if r.task is not None], return_exceptions=True
            )
            await self._notify_drain(on_event, "drain_hard_stopped", stalled, timeout_s=timeout_s)
        logger.info("run.drain_done hard_stopped=%d", len(stalled))
        return len(stalled)

    @staticmethod
    async def _notify_drain(
        on_event: DrainObserver | None,
        event: str,
        records: list[RunRecord],
        *,
        timeout_s: float,
    ) -> None:
        """把排空事件递给观察者。**吞掉它自己的异常** —— 观测不能拖垮关机。"""
        if on_event is None:
            return
        try:
            await on_event(event, list(records), timeout_s)
        except Exception:
            logger.exception("run.drain_observer_failed event=%s", event)

    async def hand_off(self, run_id: UUID) -> bool:
        """B-80 —— 交出所有权:租约作废、行留在 ``running``,等别的副本接管。

        优雅关机时本副本不再执行这一行,但 run 并没有失败 —— 它的 durable
        checkpoint 还在,活着的副本的 ``OrphanSweep`` 会从那里接着跑。所以这里
        **不是终局写**:不碰 ``status``、不写 ``error``、不写 ``finished_at``。

        返回 ``True`` iff 真的交接出去了。三种落空都返回 ``False``,调用方据此
        退回原来的 INTERRUPTED 路径:

        * 没有 durable 行(``store is None``)—— 没有别的副本能看见它;
        * 本进程没有这个 record;
        * store 侧 CAS 输了 —— run 已自己跑完(终局),或已被别的副本 reclaim。
        """
        async with self._lock:
            record = self._runs.get(run_id)
            if record is None or self._store is None:
                return False
            handed = await self._store.abandon_lease(
                run_id=run_id, claimed_by=self._instance_id, now=datetime.now(UTC)
            )
        logger.info("run.hand_off id=%s handed=%s by=%s", run_id, handed, self._instance_id)
        return handed

    async def attach_task(self, run_id: UUID, task: asyncio.Task[None]) -> bool:
        """Bind the live orchestrator task to its run record."""
        async with self._lock:
            record = self._runs.get(run_id)
            if record is None:
                return False
            record.task = task
            return True

    async def cancel(self, run_id: UUID, *, reason: str | None = None) -> bool:
        """Signal an in-flight run to abort.

        Sets ``abort_event`` (orchestrator polls this) and transitions status
        to INTERRUPTED if currently RUNNING/PENDING. Returns ``True`` iff
        the run exists. ``reason``(:class:`InterruptReason` 的值)写进
        ``error`` 列 —— 不带原因的 INTERRUPTED 在界面上分不出「用户主动取消」
        与「断流 / 连带取消」。
        """
        async with self._lock:
            record = self._runs.get(run_id)
            if record is None:
                return False
            record.abort_event.set()
            if record.status in (RunStatus.PENDING, RunStatus.RUNNING):
                now = datetime.now(UTC)
                record.status = RunStatus.INTERRUPTED
                record.updated_at = now
                if self._store is not None:
                    await self._store.set_status(
                        run_id=run_id,
                        tenant_id=record.tenant_id,
                        status=RunStatus.INTERRUPTED,
                        updated_at=now,
                        error=reason,
                        finished_at=now,
                    )
            logger.info("run.cancel id=%s prev_status=%s reason=%s", run_id, record.status, reason)
            return True

    async def close_paused(self, run_id: UUID, *, tenant_id: UUID, reason: str) -> bool:
        """把停在审批上的 run 收成 INTERRUPTED —— 它的待审批被新一轮作废了(班车 2)。

        只动 PAUSED(durable 行上的 CAS),其余状态一概不碰;返回是否真的收了。
        与 :meth:`cancel` 不同,不置 ``abort_event``:PAUSED 的 run 早已不在执行。
        本副本若还留着它的 record(结束后 TTL 内),一并镜像,读实时状态的端点
        才不会继续报 paused。``finished_at`` 保留暂停那一刻。
        """
        now = datetime.now(UTC)
        async with self._lock:
            record = self._runs.get(run_id)
            if self._store is not None:
                closed = await self._store.set_status(
                    run_id=run_id,
                    tenant_id=tenant_id,
                    status=RunStatus.INTERRUPTED,
                    updated_at=now,
                    error=reason,
                    expected_statuses=(RunStatus.PAUSED,),
                )
            else:
                closed = (
                    record is not None
                    and record.tenant_id == tenant_id
                    and record.status is RunStatus.PAUSED
                )
            if closed and record is not None and record.status is RunStatus.PAUSED:
                record.status = RunStatus.INTERRUPTED
                record.updated_at = now
        logger.info("run.close_paused id=%s closed=%s reason=%s", run_id, closed, reason)
        return closed

    async def has_inflight(self, thread_id: UUID, *, tenant_id: UUID) -> bool:
        """Return True if there is any PENDING/RUNNING run for the thread."""
        async with self._lock:
            return any(
                r.thread_id == thread_id
                and r.tenant_id == tenant_id
                and r.status in (RunStatus.PENDING, RunStatus.RUNNING)
                for r in self._runs.values()
            )

    async def cleanup(self, run_id: UUID, *, delay: float = 300.0) -> None:
        """Remove a run from the registry after ``delay`` seconds.

        Default 5 min — long enough for late SSE consumers to drain
        replayed events from the stream bridge but short enough to keep
        memory bounded. Only the in-memory record is dropped; the
        durable ``agent_run`` row (Mini-ADR J-41) is left intact so the
        run's status stays queryable past the TTL.
        """
        if delay > 0:
            await asyncio.sleep(delay)
        async with self._lock:
            self._runs.pop(run_id, None)
