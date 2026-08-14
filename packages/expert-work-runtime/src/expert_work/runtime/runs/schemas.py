# ============================================================
# Adapted from bytedance/deer-flow @ 813d3c94efa7fdea6aafcb4f459304db91fcaed0
# Source: backend/packages/harness/deerflow/runtime/runs/schemas.py
# License: MIT (see vendor LICENSE)
# Modifications:
#   - Lowercase enum values aligned to ADR-0002 audit_log result words
# Last sync: 2026-05-11
# ============================================================

"""Run lifecycle status + disconnect-mode enums + the ``RunStore`` DTO."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID


class RunStatus(StrEnum):
    """Lifecycle status of a single run."""

    PENDING = "pending"
    #: Stream 9.5 — enqueued for the distributed run queue: the run is durable
    #: but belongs to no process yet. A ``RunQueueWorker`` on any instance
    #: CAS-claims it (``status='queued'`` → ``running``) and executes it from
    #: the persisted ``enqueued_input``. Distinct from ``PENDING`` (a transient
    #: in-process create → RUNNING step on the synchronous SSE path).
    QUEUED = "queued"
    RUNNING = "running"
    SUCCESS = "success"
    ERROR = "error"
    TIMEOUT = "timeout"
    INTERRUPTED = "interrupted"
    #: Stream J.8 (Mini-ADR J-24) — run ended at an approval gate and is
    #: resumable. The graph routed to END after writing
    #: ``AgentState.pending_approval``; the checkpoint persists so
    #: ``POST /v1/runs/{id}/resume`` can re-invoke from it. Distinct from
    #: ``INTERRUPTED`` (a user-driven cancel) — a PAUSED run is waiting
    #: on a human verdict, not aborted.
    PAUSED = "paused"


class DisconnectMode(StrEnum):
    """Behaviour when the SSE consumer disconnects mid-run."""

    CANCEL = "cancel"  # abort the run
    CONTINUE = "continue"  # keep running; results still go to event_log


#: Run statuses that mark a run as finished — ``RunManager`` stamps
#: ``finished_at`` when a run transitions into one of these.
TERMINAL_RUN_STATUSES: frozenset[RunStatus] = frozenset(
    {
        RunStatus.SUCCESS,
        RunStatus.ERROR,
        RunStatus.TIMEOUT,
        RunStatus.INTERRUPTED,
        RunStatus.PAUSED,
    }
)


@dataclass(frozen=True, slots=True)
class RunInfo:
    """Serialisable run-lifecycle snapshot — the ``RunStore`` DTO.

    A persistence-facing projection of
    :class:`~expert_work.runtime.runs.manager.RunRecord` without the
    live-execution handles (``task`` / ``abort_event``), which are
    process-bound and never persisted. ``RunStore`` reads and writes
    this; ``RunManager`` builds it on each create.
    """

    run_id: UUID
    tenant_id: UUID
    thread_id: UUID
    user_id: UUID | None
    status: RunStatus
    on_disconnect: DisconnectMode
    is_resume: bool
    error: str | None
    created_at: datetime
    updated_at: datetime
    finished_at: datetime | None
    #: OTel trace id (32-char hex). Set by the API handler at run start
    #: via ``current_trace_id_hex()``; ``None`` for auto-triggered runs
    #: that don't propagate a caller-bound trace (Mini-ADR H-9.5).
    trace_id: str | None = None
    #: Stream 9.4 (HA failover) — the run-ownership lease. ``claimed_by`` is the
    #: executing instance id; ``lease_until`` is the renew-or-orphan deadline;
    #: ``heartbeat_at`` is the last liveness touch. All ``None`` until an
    #: instance claims the run (at the → RUNNING transition).
    claimed_by: str | None = None
    lease_until: datetime | None = None
    heartbeat_at: datetime | None = None
    #: Stream 9.4 — orphan-sweep reclaim counter; the sweep stops respawning a
    #: run past a cap (a run that crashes its owner every time).
    reclaim_count: int = 0
    #: Stream 9.5 — the persisted run input for a ``QUEUED`` run, so a worker on
    #: another instance can rebuild ``graph_input`` and execute it. Shape:
    #: ``{"input": str, "image_refs": [...], "untrusted_content": [...]}``.
    #: ``None`` for synchronous (SSE) runs and after a queued run is claimed
    #: (nulled out — the input then lives in the checkpoint / event log).
    enqueued_input: dict[str, Any] | None = None
    #: External-API-v1 P2 block 1-C — the caller's ``Idempotency-Key``
    #: header. ``None`` for runs created without it (the common case).
    #: Unique per ``(tenant_id, idempotency_key)`` via a partial index on
    #: ``agent_run`` (migration 0145) — that index is what makes "claim
    #: the key" and "create the run row" the same atomic insert.
    idempotency_key: str | None = None
    #: A digest of the retried request's body, so a *reused* key sent with
    #: a *different* body can be distinguished from a genuine retry.
    #: ``None`` whenever ``idempotency_key`` is ``None``.
    request_digest: str | None = None


@dataclass(frozen=True, slots=True)
class ThreadRunAggregate:
    """Per-thread run rollup — the conversation-list enrichment DTO.

    A conversation is a ``thread_meta`` row (identity + user + agent +
    title + status); this rolls up the thread's ``agent_run`` rows so the
    conversation list can show run count, whether anything errored or is
    awaiting a human, when it was last active, and — via ``trace_ids`` —
    the token totals joined from ``token_usage`` (which keys on
    ``trace_id``, not ``run_id``).

    ``run_count == 0`` never appears here: threads with no runs are simply
    absent from the aggregate map. ``trace_ids`` holds only the non-null
    trace ids (legacy / auto-triggered runs have none).
    """

    thread_id: UUID
    run_count: int
    #: runs in a failed terminal state (``error`` / ``timeout``).
    error_count: int
    #: runs paused at an approval gate — actionable "needs a human" signal.
    pending_count: int
    #: newest ``created_at`` across the thread's runs — the "last active" clock.
    last_run_at: datetime | None
    #: distinct non-null OTel trace ids, for the ``token_usage`` roll-up.
    trace_ids: tuple[str, ...]
