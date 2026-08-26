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
    #: Stream SE (SE-7d-3b-ii) — distilled skill versions bound into this run's
    #: agent at build time (from ``BuiltAgent.bound_distilled_skills``). The SSE
    #: worker emits one ``skill_run_usage`` row per entry at the run's terminal
    #: hook so the rollback monitor can attribute the outcome. Not serialized.
    bound_distilled_skills: tuple[BoundDistilledSkill, ...] = ()
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

    @property
    def instance_id(self) -> str:
        return self._instance_id

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
        """Update a run's status. Returns ``True`` iff the run exists.

        ``error`` carries the failure detail for ERROR / TIMEOUT
        transitions; it lands in the durable ``agent_run`` row. A
        transition into a terminal status also stamps ``finished_at``.
        ``artifacts``(产物清单契约)— the run's registration snapshot,
        written with the terminal status in the same store UPDATE so any
        reader that sees the terminal status also sees the manifest;
        ``None`` leaves the stored value untouched.
        """
        async with self._lock:
            record = self._runs.get(run_id)
            if record is None:
                return False
            now = datetime.now(UTC)
            record.status = status
            record.updated_at = now
            if self._store is not None:
                finished_at = now if status in TERMINAL_RUN_STATUSES else None
                await self._store.set_status(
                    run_id=run_id,
                    tenant_id=record.tenant_id,
                    status=status,
                    updated_at=now,
                    error=error,
                    finished_at=finished_at,
                    artifacts=artifacts,
                )
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
