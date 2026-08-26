"""Durable run-lifecycle store — Stream J.8 closeout follow-up (Mini-ADR J-41).

:class:`~expert_work.runtime.runs.manager.RunManager` is an in-memory
registry with a 5-minute TTL; this store is its durable mirror.
``RunManager`` writes every create / status transition here, and
``GET .../runs/{id}`` reads it when the in-memory record has expired —
so a run's status survives both the TTL sweep and a control-plane
restart.

Mini-ADR J-41 scopes this to the *bare run lifecycle layer*. Run
queueing / multitask strategy / retry / DLQ are J.10 work (Mini-ADR
J-26) and add their columns later via expand-contract.

Implementations:
- :class:`InMemoryRunStore` — unit tests.
- :class:`SqlRunStore` — Postgres-backed, the ``agent_run`` table.
"""

from __future__ import annotations

import abc
from collections.abc import Collection
from dataclasses import replace
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import String, and_, cast, delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from expert_work.persistence.models import AgentRunRow, RunEventRow, ThreadMetaRow
from expert_work.persistence.thread_meta.base import ThreadMetaStore
from expert_work.runtime.runs.event_store import RunEventStore
from expert_work.runtime.runs.schemas import (
    DisconnectMode,
    RunInfo,
    RunStatus,
    ThreadRunAggregate,
)

#: Terminal run statuses that count as a conversation-level failure signal.
_FAILED_RUN_VALUES: frozenset[str] = frozenset({RunStatus.ERROR.value, RunStatus.TIMEOUT.value})
_PENDING_RUN_VALUES: frozenset[str] = frozenset({RunStatus.PAUSED.value})

#: External-API-v1 P2 block 1-C — the ``agent_run`` partial unique index
#: name. Must match ``0145_agent_run_idempotency``'s ``_INDEX`` and
#: ``AgentRunRow.__table_args__`` verbatim: the SQL backend's ``create``
#: recognises a conflict by matching this string against the failed
#: constraint's structured name (never the free-text error message — see
#: :func:`_is_idempotency_conflict`).
_IDEMPOTENCY_INDEX_NAME = "uq_agent_run_tenant_idempotency_key"
#: Postgres ``unique_violation`` SQLSTATE — the first of two required
#: structured checks in :func:`_is_idempotency_conflict`.
_UNIQUE_VIOLATION_SQLSTATE = "23505"


class RunIdempotencyConflict(Exception):  # noqa: N818 -- required name, External-API-v1 P2 interface
    """A run already exists for this ``(tenant_id, idempotency_key)``.

    Raised by :meth:`RunStore.create` when the caller's ``Idempotency-Key``
    collides with an existing row under the same tenant. The partial unique
    index on ``agent_run`` (migration 0145) is what makes "claim the key"
    and "create the run row" the same atomic insert — there is no separate
    key table, so this can only ever be raised from ``create`` itself, never
    from a preceding lookup (which would leave a claim-without-a-run orphan
    on a losing race).
    """

    def __init__(self, *, tenant_id: UUID, idempotency_key: str) -> None:
        super().__init__(
            f"agent_run idempotency conflict: tenant_id={tenant_id} "
            f"idempotency_key={idempotency_key!r}"
        )
        self.tenant_id = tenant_id
        self.idempotency_key = idempotency_key


def _is_idempotency_conflict(exc: IntegrityError) -> bool:
    """``True`` iff ``exc`` came from the idempotency partial unique index.

    Distinguishes it from any other ``IntegrityError`` the insert could
    raise (e.g. a ``run_id`` primary-key collision, or any future CHECK /
    NOT NULL constraint on ``agent_run``) so only the former becomes
    :class:`RunIdempotencyConflict`. Both checks are on **structured**
    driver fields, never on the free-text error message — Postgres embeds
    the *entire failing row* (``DETAIL: Failing row contains (...)``) in
    the text of every constraint violation, including whatever the caller
    put in ``idempotency_key``. Matching that text against the index name
    would let a caller who sends ``Idempotency-Key: "uq_agent_run_tenant_
    idempotency_key"`` turn an unrelated CHECK-constraint failure into a
    silently-wrong "idempotency conflict" (verified in review; see
    ``test_check_violation_with_lookalike_key_is_not_misclassified``).

    SQLAlchemy's asyncpg dialect only forwards ``sqlstate`` onto ``exc.orig``
    itself when translating the driver error (see
    ``AsyncAdapt_asyncpg_connection._handle_exception`` in
    ``sqlalchemy/dialects/postgresql/asyncpg.py``) — no structured
    ``constraint_name`` survives on ``exc.orig`` directly, unlike psycopg.
    But the translation does ``raise translated_error from error``, so the
    original ``asyncpg.exceptions.PostgresError`` — which *does* carry a
    structured ``constraint_name`` — is still reachable one hop further, on
    ``exc.orig.__cause__``. Both checks must pass: the SQLSTATE narrows to
    "some unique violation", the constraint name narrows to "this specific
    partial index". Either being unavailable/mismatched returns ``False``,
    the fail-safe direction — the caller re-raises the plain
    ``IntegrityError`` instead of silently mis-handling it.
    """
    if getattr(exc.orig, "sqlstate", None) != _UNIQUE_VIOLATION_SQLSTATE:
        return False
    cause = getattr(exc.orig, "__cause__", None)
    return getattr(cause, "constraint_name", None) == _IDEMPOTENCY_INDEX_NAME


def _only_status_values(only: Literal["failed", "pending"] | None) -> frozenset[str] | None:
    """Map a ``thread_ids_with_runs`` ``only`` filter to run-status values."""
    if only is None:
        return None
    return _FAILED_RUN_VALUES if only == "failed" else _PENDING_RUN_VALUES


def _like_contains(q: str) -> str:
    """Escape LIKE wildcards in ``q`` and wrap for an ``ILIKE %q%`` match.

    ``q`` is an operator's free-text filter (typically a copied run_id /
    thread_id fragment). Escaping ``\\ % _`` keeps a stray wildcard from
    widening the match; the value still binds as a parameter (no SQL
    injection surface).
    """
    esc = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{esc}%"


class RunStore(abc.ABC):
    """Durable, tenant-scoped run-lifecycle registry."""

    @abc.abstractmethod
    async def create(self, info: RunInfo) -> None:
        """Persist a new run row.

        ``run_id`` is the primary key — a second ``create`` for the
        same id is a programming error and the SQL backend's primary
        key surfaces it.

        External-API-v1 P2 block 1-C — when ``info.idempotency_key`` is not
        ``None`` and another row already exists for ``(tenant_id,
        idempotency_key)``, raises :class:`RunIdempotencyConflict` instead
        of inserting. The partial unique index backing this is on
        ``agent_run`` itself, so "claim the key" and "create the run row"
        are the same atomic insert — a caller must never implement this as
        a separate lookup-then-create (that leaves an orphaned claim on a
        losing race). ``idempotency_key=None`` never conflicts — any number
        of such rows can coexist.
        """

    @abc.abstractmethod
    async def set_status(
        self,
        *,
        run_id: UUID,
        tenant_id: UUID,
        status: RunStatus,
        updated_at: datetime,
        error: str | None = None,
        finished_at: datetime | None = None,
    ) -> bool:
        """Update a run's status; return ``True`` iff the row exists.

        ``error`` / ``finished_at`` are written only when not ``None``
        so a non-terminal transition (e.g. → RUNNING) never clears a
        verdict an earlier terminal write recorded.
        """

    @abc.abstractmethod
    async def request_cancel(
        self, *, run_id: UUID, tenant_id: UUID, updated_at: datetime, reason: str | None = None
    ) -> bool:
        """Stream RT-4 (RT-ADR-17) — cross-replica cancel: guarded transition of a
        still-executing OR still-queued run to INTERRUPTED.

        ``reason``(:class:`InterruptReason` 的值)写进 ``error`` 列 —— 与
        :meth:`RunManager.cancel` 同一份词表,让界面能区分「用户主动取消」
        与「断流 / 连带取消」。

        Updates ``status → interrupted`` ONLY when the row is currently
        ``running`` / ``pending`` / ``queued`` (so a run that just finished
        ``SUCCESS`` is never clobbered). Returns ``True`` iff a row was
        interrupted.

        This is how a kill-switch stops a run owned by *another* instance: the
        owning worker's next lease heartbeat CAS (``status='running' AND
        claimed_by=<owner>``) then fails, which makes ``_renew_lease`` set that
        worker's ``abort_event`` — the run stops within one heartbeat interval.
        A local run is stopped immediately via :meth:`RunManager.cancel`; this
        is the fallback for the runs that manager does not own. Including
        ``queued`` here (External-API-v1 P1 review fix — Critical C1) is what
        makes a still-unclaimed run actually stoppable: once this CAS wins,
        :meth:`claim_queued`'s own ``status = QUEUED`` guard can never match
        it, so no worker ever picks it up — no race with a concurrent claim.
        """

    @abc.abstractmethod
    async def fail_if_active(
        self, *, run_id: UUID, tenant_id: UUID, error: str, now: datetime
    ) -> bool:
        """W1 PR1 task 4 — CAS-guarded terminal transition to ``ERROR``.

        Updates ``status → error`` (+ ``error`` / ``finished_at`` /
        ``updated_at``) ONLY when the row is currently ``running`` /
        ``pending``. Returns ``True`` iff a row was transitioned.

        This is :class:`~control_plane.orphan_sweep.OrphanSweep`'s
        ``_fail_orphan`` guard: every replica's sweep loop can list the same
        lease-expired, reclaim-capped orphan in the same cycle (a scan-then-act
        race, structurally identical to the :meth:`reclaim` CAS). Without this
        guard each replica's unconditional ``set_status`` would "succeed",
        double-incrementing the failed-orphan counter and emitting a duplicate
        audit record for one terminal transition. ``False`` means a peer
        already won (or the run reached a terminal status on its own) — the
        caller skips its counter/audit side effects.
        """

    @abc.abstractmethod
    async def get(self, *, run_id: UUID, tenant_id: UUID) -> RunInfo | None:
        """Return the run row, or ``None`` when unknown / cross-tenant.

        ``None`` (never raising) on a cross-tenant probe so callers can
        turn it straight into a 404 that hides existence.
        """

    @abc.abstractmethod
    async def find_by_idempotency_key(self, *, tenant_id: UUID, key: str) -> RunInfo | None:
        """Return the run claimed by ``(tenant_id, key)``, or ``None``.

        External-API-v1 P2 block 1-C — the read-path companion to
        :meth:`create`'s conflict check: a caller that catches
        :class:`RunIdempotencyConflict` calls this to fetch the run the
        *first* request created, so a retried request returns the original
        run instead of erroring.
        """

    @abc.abstractmethod
    async def list_by_thread(self, *, thread_id: UUID, tenant_id: UUID) -> list[RunInfo]:
        """Return all runs for ``thread_id`` under ``tenant_id``, oldest first."""

    @abc.abstractmethod
    async def delete_by_thread(self, *, thread_id: UUID, tenant_id: UUID) -> int:
        """Hard-delete every run row for ``thread_id`` under ``tenant_id``.

        Session purge (hard delete of a whole conversation). Also empties
        the runs' ``run_event`` children first (``ON DELETE RESTRICT``
        would otherwise block the parent delete — deletion-hygiene PR3
        §A): SQL inside the same transaction, in-memory via the injected
        event store. Returns the number of run rows removed (children not
        counted). Tenant-scoped — never touches another tenant's runs.
        """

    @abc.abstractmethod
    async def anonymize_all_for_user(self, *, tenant_id: UUID, user_id: UUID) -> int:
        """Phase 3a (purge_user) — null the ``user_id`` on a user's runs, KEEP them.

        Sets ``user_id = NULL`` on every ``agent_run`` row for ``(tenant_id,
        user_id)`` and returns the count updated. The rows are NOT deleted —
        ``run_event.run_id`` references ``agent_run.id`` with ``ON DELETE
        RESTRICT``, and the run lifecycle is billing / analytics data; only the
        per-user PII link is severed. Tenant- AND user-scoped. Rows on the
        user's own threads are usually already gone (the per-thread purge's
        ``delete_by_thread`` removed them); this is the catch-all that ensures
        no surviving ``agent_run`` row retains the purged ``user_id``."""

    @abc.abstractmethod
    async def list_for_tenant(
        self,
        *,
        tenant_id: UUID,
        status: RunStatus | None = None,
        thread_ids: Collection[UUID] | None = None,
        user_id: UUID | None = None,
        agent_name: str | None = None,
        q: str | None = None,
        limit: int = 100,
        offset: int = 0,
        before: tuple[datetime, UUID] | None = None,
    ) -> list[RunInfo]:
        """Return runs for ``tenant_id``, newest first; paginated.

        ``before`` is a **keyset cursor**: ``(created_at, run_id)`` of the
        oldest row the caller already has; only strictly-older rows come
        back. Use it instead of ``offset`` whenever new rows can be
        created while the caller pages — this list is newest-first, so a
        run started mid-pagination shifts every offset by one and the
        client silently re-reads a row it already had. Pass one or the
        other; combining them applies the offset *within* the keyset
        window, which is almost never what a caller means.

        The cursor is a pair, not a bare timestamp, because ``created_at``
        is not unique: two runs submitted on the same thread in the same
        microsecond would straddle a page boundary and the second one
        would be skipped forever by a ``created_at``-only cursor. The
        comparison is SQL row-value ordering, which is what Python's tuple
        comparison already does — the two backends must stay literally
        interchangeable here.

        Stream H.3 PR 1 — feeds the cross-thread ``GET /v1/runs`` index.
        ``limit`` is clamped to ``MAX_LIST_LIMIT`` (Mini-ADR H-7 D).
        ``thread_ids`` narrows to runs of those threads (Stream H.6
        Mini-ADR H-10 — the API layer resolves an agent to its thread
        window via ``ThreadMetaStore`` and passes the ids here; an empty
        collection returns no rows).

        ``agent_name`` (阶段 3, 3.2) narrows to runs whose thread is bound
        to that agent. Runs carry no agent — the binding lives on
        ``thread_meta`` — so this is a join, exactly like
        :meth:`list_running_for_agent`. Doing it in the API layer instead
        (fetch the thread ids first, pass ``thread_ids=``) breaks
        pagination: a tenant's thread count is unbounded, so taking one
        page of threads and then filtering runs makes ``offset`` mean
        something else entirely.

        An in-memory store with no ``thread_meta_store`` wired returns
        ``[]`` for any ``agent_name`` query rather than silently ignoring
        the filter — same rule as :meth:`list_running_for_agent`. Silently
        dropping the predicate would leak other agents' runs.
        """

    @abc.abstractmethod
    async def list_running_for_agent(
        self, *, tenant_id: UUID, agent_name: str, agent_version: str | None = None
    ) -> list[RunInfo]:
        """Return the ``RUNNING`` runs bound to ``agent_name`` under ``tenant_id``.

        Stream RT-4 (RT-ADR-17) — the agent-level kill switch bulk-cancels
        these. Runs carry no ``agent_name``; the binding lives on
        ``thread_meta`` (set when the session was created). The SQL backend
        joins ``agent_run`` ↔ ``thread_meta`` on ``thread_id`` where
        ``thread_meta.agent_name = agent_name`` and ``agent_run.status =
        'running'``. Tenant-scoped — never returns another tenant's runs.

        ``agent_version`` given narrows the result to that version (the
        version-level delete cascade); ``None`` keeps the name-level
        semantics (the kill switch, which covers all versions).
        ``thread_meta.agent_version`` is nullable, so a thread with no
        recorded version never matches a version-level query — deleting one
        version must not cancel runs that were never bound to it.

        A version-level query is **not** exhaustive for a second reason: the
        external-app path (``api/agents.py:_resolve_session``) reuses an
        existing thread by ``agent_name`` alone, so a thread stamped ``1.0.0``
        may be executing the newest ACTIVE version's code. Deleting that newer
        version does not cancel such a run. Delete-cascade cancellation is
        hygiene (the run still dies on its deadline); the emergency stop is the
        kill switch, which stays name-level precisely for this reason.
        """

    @abc.abstractmethod
    async def list_all_tenants(
        self,
        *,
        status: RunStatus | None = None,
        thread_ids: Collection[UUID] | None = None,
        user_id: UUID | None = None,
        q: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[RunInfo]:
        """Cross-tenant variant for ``system_admin`` aggregate views.

        Stream N: callers MUST wrap this in ``bypass_rls_session()`` so
        the SQL backend bypasses tenant RLS. ``limit`` is clamped to
        ``MAX_LIST_LIMIT`` (Mini-ADR H-7 D). ``thread_ids`` as in
        :meth:`list_for_tenant` (Mini-ADR H-10).
        """

    @abc.abstractmethod
    async def thread_ids_with_runs(
        self,
        *,
        tenant_id: UUID | None,
        since: datetime | None = None,
        only: Literal["failed", "pending"] | None = None,
        limit: int = 500,
    ) -> set[UUID]:
        """Distinct ``thread_id``s with ≥1 matching run.

        Feeds the conversation browser's activity filters (conversation-
        centric IA): the endpoint resolves the thread set here first, then
        reads their metadata via ``ThreadMetaStore.list_by_tenant(thread_ids=…)``.
        ``only="failed"`` restricts to failed-terminal runs (error/timeout)
        — the "what broke" filter; ``only="pending"`` to runs paused at an
        approval gate — the "needs a human" filter. ``since`` restricts to
        runs created at or after that instant — the "active in the last N
        hours" window; it composes with ``only`` ("what broke today"). The
        endpoint intersects separate calls when several ``only`` filters
        are active at once. ``tenant_id=None`` is the cross-tenant
        aggregate — the caller MUST wrap it in ``bypass_rls_session()``
        (Stream N contract). ``limit`` caps the set (a capped read is
        surfaced by the endpoint via ``X-Limit-Capped``).
        """

    @abc.abstractmethod
    async def aggregate_by_threads(
        self,
        *,
        thread_ids: Collection[UUID],
        tenant_id: UUID | None,
    ) -> dict[UUID, ThreadRunAggregate]:
        """Roll up ``agent_run`` rows per thread — the conversation-list feed.

        Returns one :class:`ThreadRunAggregate` per thread that has ≥1 run,
        keyed by ``thread_id``; threads with no runs are absent. ``tenant_id``
        scopes the rollup; pass ``None`` for the cross-tenant aggregate, in
        which case the caller MUST wrap the call in ``bypass_rls_session()``
        (Stream N contract, same as :meth:`list_all_tenants`). An empty
        ``thread_ids`` returns ``{}`` without touching the store.
        """

    @abc.abstractmethod
    async def set_trace_id(
        self,
        *,
        run_id: UUID,
        tenant_id: UUID,
        trace_id: str,
    ) -> bool:
        """Persist the OTel ``trace_id`` for ``run_id``.

        Stream H.3 PR 2 (Mini-ADR H-9.5). Idempotent overwrite — if the
        worker observes its own trace_id (rare), the second call wins.
        Returns ``True`` iff the row exists; cross-tenant probes return
        ``False`` so callers can hide existence.
        """

    # --- Stream 9.4 (HA failover) — ownership lease ------------------------

    @abc.abstractmethod
    async def claim(
        self,
        *,
        run_id: UUID,
        tenant_id: UUID,
        claimed_by: str,
        lease_until: datetime,
        heartbeat_at: datetime,
    ) -> bool:
        """Stamp the run-ownership lease (at the → RUNNING transition).

        Sets ``claimed_by`` / ``lease_until`` / ``heartbeat_at`` so the orphan
        sweep can tell a live owner from a crashed one. Returns ``True`` iff the
        row exists.
        """

    @abc.abstractmethod
    async def heartbeat(
        self,
        *,
        run_id: UUID,
        claimed_by: str,
        lease_until: datetime,
        heartbeat_at: datetime,
    ) -> bool:
        """Renew the lease — only when ``claimed_by`` still owns the running run.

        CAS on ``(status='running' AND claimed_by=<owner>)`` so a worker whose
        run was reclaimed by a peer (its lease lapsed) sees ``False`` and stops.
        Returns ``True`` iff this owner still holds the running run.
        """

    @abc.abstractmethod
    async def list_orphans(self, *, now: datetime, limit: int) -> list[RunInfo]:
        """Cross-tenant running runs whose lease expired — the orphan candidates.

        ``status='running' AND lease_until < now``. Caller MUST wrap in
        ``bypass_rls_session()`` (cross-tenant sweep). The reclaim CAS
        (:meth:`reclaim`) is the authoritative guard against two sweepers
        racing on one orphan; this scan only nominates candidates.
        """

    @abc.abstractmethod
    async def list_stale_pending(self, *, cutoff: datetime, limit: int) -> list[RunInfo]:
        """Cross-tenant PENDING runs stuck past the create→RUNNING window.

        ``status='pending' AND created_at < cutoff``, ordered
        ``created_at`` ascending. PENDING is normally a milliseconds-scale
        in-process step on the synchronous SSE path (see
        :attr:`~expert_work.runtime.runs.schemas.RunStatus.PENDING`); a row
        still PENDING past ``cutoff`` means its owning replica crashed
        before ever stamping a lease. :meth:`list_orphans` never nominates
        it (that scan only looks at ``running`` + expired lease), so without
        this scan the row is stuck forever — and it also permanently blocks
        its thread's external plan writes (``_WRITE_BLOCKED_STATUSES`` in
        ``control_plane.api.plan`` treats PENDING as plan-write-blocking).
        Caller MUST wrap in ``bypass_rls_session()`` (cross-tenant sweep).
        """

    @abc.abstractmethod
    async def reclaim(
        self,
        *,
        run_id: UUID,
        new_owner: str,
        lease_until: datetime,
        heartbeat_at: datetime,
        now: datetime,
    ) -> bool:
        """Atomically take over an expired-lease running run; ``True`` iff won.

        CAS on ``(status='running' AND lease_until < now)`` so exactly one
        sweeper wins when several race on the same orphan (the others get
        ``False`` — rowcount 0). Caller wraps in ``bypass_rls_session()``.
        """

    @abc.abstractmethod
    async def list_queued(self, *, limit: int) -> list[RunInfo]:
        """Cross-tenant ``status='queued'`` runs, oldest first (FIFO).

        Stream 9.5 — the run-queue worker's scan. Caller MUST wrap in
        ``bypass_rls_session()`` (cross-tenant). The claim CAS
        (:meth:`claim_queued`) is the authoritative exactly-once guard;
        this scan only nominates candidates.
        """

    @abc.abstractmethod
    async def claim_queued(
        self,
        *,
        run_id: UUID,
        new_owner: str,
        lease_until: datetime,
        heartbeat_at: datetime,
    ) -> RunInfo | None:
        """Atomically claim a queued run for execution; the run iff won.

        Stream 9.5 — CAS on ``status='queued'`` (→ ``running`` + stamp the
        ownership lease) so exactly one worker wins when several race on the
        same queued run. Returns the claimed :class:`RunInfo` (carrying the
        ``enqueued_input`` the worker needs) on a win, ``None`` on a loss
        (rowcount 0). Caller wraps in ``bypass_rls_session()``.
        """


#: Stream H.3 PR 1 — Mini-ADR H-7 (D) hard cap so a single page can never
#: return more than this many rows. Callers passing a larger ``limit``
#: are silently clamped; ``/v1/runs`` sets ``X-Limit-Capped: true`` in
#: that case (same envelope-header convention as agents / triggers
#: list endpoints).
MAX_LIST_LIMIT = 500


def _clamp_limit(limit: int) -> int:
    """Apply the :data:`MAX_LIST_LIMIT` ceiling — public so the API
    layer can set the response header when it actually clamped."""
    if limit < 1:
        return 1
    return min(limit, MAX_LIST_LIMIT)


class InMemoryRunStore(RunStore):
    """In-memory ``RunStore`` for unit tests."""

    def __init__(
        self,
        *,
        thread_meta_store: ThreadMetaStore | None = None,
        event_store: RunEventStore | None = None,
    ) -> None:
        # Keyed by run_id — the primary key.
        self._rows: dict[UUID, RunInfo] = {}
        # Stream RT-4 — runs carry no agent_name; the binding is on thread_meta.
        # The SQL backend does a real join; the in-memory double emulates it by
        # holding the thread store so ``list_running_for_agent`` can resolve a
        # run's thread → agent. ``None`` when the caller does not wire it —
        # ``list_running_for_agent`` then returns [] (no join source).
        self._thread_meta_store = thread_meta_store
        # Deletion-hygiene PR3 §A — the SQL store empties ``run_event``
        # inside its delete transaction; the in-memory double mirrors that
        # through the injected event store. ``None`` when the caller does
        # not wire it — ``delete_by_thread`` then touches run rows only.
        self._event_store = event_store

    async def create(self, info: RunInfo) -> None:
        if info.run_id in self._rows:
            msg = f"run row already exists for run {info.run_id}"
            raise ValueError(msg)
        # Same predicate as the SQL partial unique index — byte-identical:
        # ``(tenant_id, idempotency_key)`` collides only when the key is
        # not ``None``.
        if info.idempotency_key is not None:
            for row in self._rows.values():
                if row.tenant_id == info.tenant_id and row.idempotency_key == info.idempotency_key:
                    raise RunIdempotencyConflict(
                        tenant_id=info.tenant_id, idempotency_key=info.idempotency_key
                    )
        self._rows[info.run_id] = info

    async def set_status(
        self,
        *,
        run_id: UUID,
        tenant_id: UUID,
        status: RunStatus,
        updated_at: datetime,
        error: str | None = None,
        finished_at: datetime | None = None,
    ) -> bool:
        row = self._rows.get(run_id)
        if row is None or row.tenant_id != tenant_id:
            return False
        self._rows[run_id] = replace(
            row,
            status=status,
            updated_at=updated_at,
            error=error if error is not None else row.error,
            finished_at=finished_at if finished_at is not None else row.finished_at,
        )
        return True

    async def request_cancel(
        self, *, run_id: UUID, tenant_id: UUID, updated_at: datetime, reason: str | None = None
    ) -> bool:
        row = self._rows.get(run_id)
        if (
            row is None
            or row.tenant_id != tenant_id
            or row.status not in (RunStatus.RUNNING, RunStatus.PENDING, RunStatus.QUEUED)
        ):
            return False
        self._rows[run_id] = replace(
            row,
            status=RunStatus.INTERRUPTED,
            updated_at=updated_at,
            # 谓词与 SQL 店 byte-同义:reason 为 None 时保留既有 error。
            error=reason if reason is not None else row.error,
            finished_at=updated_at,
        )
        return True

    async def fail_if_active(
        self, *, run_id: UUID, tenant_id: UUID, error: str, now: datetime
    ) -> bool:
        row = self._rows.get(run_id)
        if (
            row is None
            or row.tenant_id != tenant_id
            or row.status not in (RunStatus.RUNNING, RunStatus.PENDING)
        ):
            return False
        self._rows[run_id] = replace(
            row,
            status=RunStatus.ERROR,
            error=error,
            finished_at=now,
            updated_at=now,
        )
        return True

    async def get(self, *, run_id: UUID, tenant_id: UUID) -> RunInfo | None:
        row = self._rows.get(run_id)
        if row is None or row.tenant_id != tenant_id:
            return None
        return row

    async def find_by_idempotency_key(self, *, tenant_id: UUID, key: str) -> RunInfo | None:
        for row in self._rows.values():
            if row.tenant_id == tenant_id and row.idempotency_key == key:
                return row
        return None

    async def list_by_thread(self, *, thread_id: UUID, tenant_id: UUID) -> list[RunInfo]:
        rows = [
            r for r in self._rows.values() if r.thread_id == thread_id and r.tenant_id == tenant_id
        ]
        rows.sort(key=lambda r: r.created_at)
        return rows

    async def delete_by_thread(self, *, thread_id: UUID, tenant_id: UUID) -> int:
        victims = [
            rid
            for rid, r in self._rows.items()
            if r.thread_id == thread_id and r.tenant_id == tenant_id
        ]
        # Children first — mirrors the SQL store's in-transaction order.
        if self._event_store is not None:
            await self._event_store.delete_for_runs(run_ids=victims)
        for rid in victims:
            del self._rows[rid]
        return len(victims)

    async def anonymize_all_for_user(self, *, tenant_id: UUID, user_id: UUID) -> int:
        updated = 0
        for rid, r in list(self._rows.items()):
            if r.tenant_id == tenant_id and r.user_id == user_id:
                self._rows[rid] = replace(r, user_id=None)
                updated += 1
        return updated

    async def list_for_tenant(
        self,
        *,
        tenant_id: UUID,
        status: RunStatus | None = None,
        thread_ids: Collection[UUID] | None = None,
        user_id: UUID | None = None,
        agent_name: str | None = None,
        q: str | None = None,
        limit: int = 100,
        offset: int = 0,
        before: tuple[datetime, UUID] | None = None,
    ) -> list[RunInfo]:
        rows = [r for r in self._rows.values() if r.tenant_id == tenant_id]
        if before is not None:
            # 与 SQL 侧的行值比较同义:先比 created_at,相等再比 run_id。
            rows = [r for r in rows if (r.created_at, r.run_id) < before]
        if status is not None:
            rows = [r for r in rows if r.status is status]
        if user_id is not None:
            rows = [r for r in rows if r.user_id == user_id]
        if thread_ids is not None:
            wanted = set(thread_ids)
            rows = [r for r in rows if r.thread_id in wanted]
        if agent_name is not None:
            if self._thread_meta_store is None:
                return []
            kept: list[RunInfo] = []
            for r in rows:
                meta = await self._thread_meta_store.get(r.thread_id, tenant_id=tenant_id)
                if meta is not None and meta.agent_name == agent_name:
                    kept.append(r)
            rows = kept
        if q:
            ql = q.lower()
            rows = [
                r for r in rows if ql in str(r.run_id).lower() or ql in str(r.thread_id).lower()
            ]
        # ``run_id`` 是排序的第二键,与 SQL 侧的 ``ORDER BY created_at DESC, id
        # DESC`` 同义 —— keyset 游标只有在排序键与游标键一致时才不漏不重。
        rows.sort(key=lambda r: (r.created_at, r.run_id), reverse=True)
        clamped = _clamp_limit(limit)
        return rows[offset : offset + clamped]

    async def list_running_for_agent(
        self, *, tenant_id: UUID, agent_name: str, agent_version: str | None = None
    ) -> list[RunInfo]:
        if self._thread_meta_store is None:
            return []
        running = [
            r
            for r in self._rows.values()
            if r.tenant_id == tenant_id and r.status is RunStatus.RUNNING
        ]
        out: list[RunInfo] = []
        for r in running:
            meta = await self._thread_meta_store.get(r.thread_id, tenant_id=tenant_id)
            if meta is None or meta.agent_name != agent_name:
                continue
            if agent_version is not None and meta.agent_version != agent_version:
                continue
            out.append(r)
        out.sort(key=lambda r: r.created_at, reverse=True)
        return out

    async def list_all_tenants(
        self,
        *,
        status: RunStatus | None = None,
        thread_ids: Collection[UUID] | None = None,
        user_id: UUID | None = None,
        q: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[RunInfo]:
        rows = list(self._rows.values())
        if status is not None:
            rows = [r for r in rows if r.status is status]
        if user_id is not None:
            rows = [r for r in rows if r.user_id == user_id]
        if thread_ids is not None:
            wanted = set(thread_ids)
            rows = [r for r in rows if r.thread_id in wanted]
        if q:
            ql = q.lower()
            rows = [
                r for r in rows if ql in str(r.run_id).lower() or ql in str(r.thread_id).lower()
            ]
        rows.sort(key=lambda r: r.created_at, reverse=True)
        clamped = _clamp_limit(limit)
        return rows[offset : offset + clamped]

    async def thread_ids_with_runs(
        self,
        *,
        tenant_id: UUID | None,
        since: datetime | None = None,
        only: Literal["failed", "pending"] | None = None,
        limit: int = 500,
    ) -> set[UUID]:
        wanted = _only_status_values(only)
        out: set[UUID] = set()
        for r in self._rows.values():
            if tenant_id is not None and r.tenant_id != tenant_id:
                continue
            if wanted is not None and r.status.value not in wanted:
                continue
            if since is not None and r.created_at < since:
                continue
            out.add(r.thread_id)
            if len(out) >= limit:
                break
        return out

    async def aggregate_by_threads(
        self,
        *,
        thread_ids: Collection[UUID],
        tenant_id: UUID | None,
    ) -> dict[UUID, ThreadRunAggregate]:
        wanted = set(thread_ids)
        if not wanted:
            return {}
        rows = [
            r
            for r in self._rows.values()
            if r.thread_id in wanted and (tenant_id is None or r.tenant_id == tenant_id)
        ]
        return _aggregate_runs_by_thread(rows)

    async def set_trace_id(
        self,
        *,
        run_id: UUID,
        tenant_id: UUID,
        trace_id: str,
    ) -> bool:
        row = self._rows.get(run_id)
        if row is None or row.tenant_id != tenant_id:
            return False
        self._rows[run_id] = replace(row, trace_id=trace_id)
        return True

    async def claim(
        self,
        *,
        run_id: UUID,
        tenant_id: UUID,
        claimed_by: str,
        lease_until: datetime,
        heartbeat_at: datetime,
    ) -> bool:
        row = self._rows.get(run_id)
        if row is None or row.tenant_id != tenant_id:
            return False
        self._rows[run_id] = replace(
            row, claimed_by=claimed_by, lease_until=lease_until, heartbeat_at=heartbeat_at
        )
        return True

    async def heartbeat(
        self,
        *,
        run_id: UUID,
        claimed_by: str,
        lease_until: datetime,
        heartbeat_at: datetime,
    ) -> bool:
        row = self._rows.get(run_id)
        if row is None or row.status is not RunStatus.RUNNING or row.claimed_by != claimed_by:
            return False
        self._rows[run_id] = replace(row, lease_until=lease_until, heartbeat_at=heartbeat_at)
        return True

    async def list_orphans(self, *, now: datetime, limit: int) -> list[RunInfo]:
        rows = [
            r
            for r in self._rows.values()
            if r.status is RunStatus.RUNNING and r.lease_until is not None and r.lease_until < now
        ]
        rows.sort(key=lambda r: r.lease_until or r.created_at)
        return rows[: max(1, limit)]

    async def list_stale_pending(self, *, cutoff: datetime, limit: int) -> list[RunInfo]:
        rows = [
            r
            for r in self._rows.values()
            if r.status is RunStatus.PENDING and r.created_at < cutoff
        ]
        rows.sort(key=lambda r: r.created_at)
        return rows[: max(1, limit)]

    async def reclaim(
        self,
        *,
        run_id: UUID,
        new_owner: str,
        lease_until: datetime,
        heartbeat_at: datetime,
        now: datetime,
    ) -> bool:
        row = self._rows.get(run_id)
        # CAS — only an expired-lease running run can be taken over.
        if (
            row is None
            or row.status is not RunStatus.RUNNING
            or row.lease_until is None
            or row.lease_until >= now
        ):
            return False
        self._rows[run_id] = replace(
            row,
            claimed_by=new_owner,
            lease_until=lease_until,
            heartbeat_at=heartbeat_at,
            reclaim_count=row.reclaim_count + 1,
        )
        return True

    async def list_queued(self, *, limit: int) -> list[RunInfo]:
        rows = [r for r in self._rows.values() if r.status is RunStatus.QUEUED]
        rows.sort(key=lambda r: r.created_at)
        return rows[: max(1, limit)]

    async def claim_queued(
        self,
        *,
        run_id: UUID,
        new_owner: str,
        lease_until: datetime,
        heartbeat_at: datetime,
    ) -> RunInfo | None:
        row = self._rows.get(run_id)
        if row is None or row.status is not RunStatus.QUEUED:
            return None
        claimed = replace(
            row,
            status=RunStatus.RUNNING,
            claimed_by=new_owner,
            lease_until=lease_until,
            heartbeat_at=heartbeat_at,
        )
        self._rows[run_id] = claimed
        return claimed


def _aggregate_runs_by_thread(rows: list[RunInfo]) -> dict[UUID, ThreadRunAggregate]:
    """Fold a flat run list into per-thread :class:`ThreadRunAggregate` rows.

    Used by the in-memory backend; the SQL backend does the equivalent as a
    ``GROUP BY`` so it never materialises every run.
    """
    counts: dict[UUID, int] = {}
    errors: dict[UUID, int] = {}
    pending: dict[UUID, int] = {}
    last_at: dict[UUID, datetime] = {}
    traces: dict[UUID, list[str]] = {}
    for r in rows:
        tid = r.thread_id
        counts[tid] = counts.get(tid, 0) + 1
        if r.status.value in _FAILED_RUN_VALUES:
            errors[tid] = errors.get(tid, 0) + 1
        if r.status is RunStatus.PAUSED:
            pending[tid] = pending.get(tid, 0) + 1
        prev = last_at.get(tid)
        if prev is None or r.created_at > prev:
            last_at[tid] = r.created_at
        if r.trace_id is not None:
            traces.setdefault(tid, []).append(r.trace_id)
    return {
        tid: ThreadRunAggregate(
            thread_id=tid,
            run_count=counts[tid],
            error_count=errors.get(tid, 0),
            pending_count=pending.get(tid, 0),
            last_run_at=last_at.get(tid),
            # Distinct + sorted so the downstream token roll-up is deterministic.
            trace_ids=tuple(sorted(set(traces.get(tid, [])))),
        )
        for tid in counts
    }


def _row_to_dto(row: AgentRunRow) -> RunInfo:
    return RunInfo(
        run_id=row.id,
        tenant_id=row.tenant_id,
        thread_id=row.thread_id,
        user_id=row.user_id,
        status=RunStatus(row.status),
        on_disconnect=DisconnectMode(row.on_disconnect),
        is_resume=row.is_resume,
        error=row.error,
        created_at=row.created_at,
        updated_at=row.updated_at,
        finished_at=row.finished_at,
        trace_id=row.trace_id,
        claimed_by=row.claimed_by,
        lease_until=row.lease_until,
        heartbeat_at=row.heartbeat_at,
        reclaim_count=row.reclaim_count,
        enqueued_input=row.enqueued_input,
        idempotency_key=row.idempotency_key,
        request_digest=row.request_digest,
    )


class SqlRunStore(RunStore):
    """Postgres-backed run-lifecycle registry — the ``agent_run`` table."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sf = session_factory

    async def create(self, info: RunInfo) -> None:
        async with self._sf() as session:
            session.add(
                AgentRunRow(
                    id=info.run_id,
                    tenant_id=info.tenant_id,
                    user_id=info.user_id,
                    thread_id=info.thread_id,
                    status=info.status.value,
                    on_disconnect=info.on_disconnect.value,
                    is_resume=info.is_resume,
                    error=info.error,
                    created_at=info.created_at,
                    updated_at=info.updated_at,
                    finished_at=info.finished_at,
                    trace_id=info.trace_id,
                    claimed_by=info.claimed_by,
                    lease_until=info.lease_until,
                    heartbeat_at=info.heartbeat_at,
                    reclaim_count=info.reclaim_count,
                    enqueued_input=info.enqueued_input,
                    idempotency_key=info.idempotency_key,
                    request_digest=info.request_digest,
                )
            )
            try:
                await session.commit()
            except IntegrityError as exc:
                await session.rollback()
                if _is_idempotency_conflict(exc):
                    # The partial unique index only fires on a non-NULL key.
                    assert info.idempotency_key is not None  # noqa: S101
                    raise RunIdempotencyConflict(
                        tenant_id=info.tenant_id, idempotency_key=info.idempotency_key
                    ) from exc
                raise

    async def set_status(
        self,
        *,
        run_id: UUID,
        tenant_id: UUID,
        status: RunStatus,
        updated_at: datetime,
        error: str | None = None,
        finished_at: datetime | None = None,
    ) -> bool:
        values: dict[str, Any] = {"status": status.value, "updated_at": updated_at}
        if error is not None:
            values["error"] = error
        if finished_at is not None:
            values["finished_at"] = finished_at
        async with self._sf() as session:
            result = await session.execute(
                update(AgentRunRow)
                .where(AgentRunRow.id == run_id, AgentRunRow.tenant_id == tenant_id)
                .values(values)
            )
            await session.commit()
        return int(getattr(result, "rowcount", 0) or 0) > 0

    async def request_cancel(
        self, *, run_id: UUID, tenant_id: UUID, updated_at: datetime, reason: str | None = None
    ) -> bool:
        values: dict[str, Any] = {
            "status": RunStatus.INTERRUPTED.value,
            "updated_at": updated_at,
            "finished_at": updated_at,
        }
        # 谓词与 in-memory 店 byte-同义:reason 为 None 时不碰既有 error。
        if reason is not None:
            values["error"] = reason
        async with self._sf() as session:
            result = await session.execute(
                update(AgentRunRow)
                .where(
                    AgentRunRow.id == run_id,
                    AgentRunRow.tenant_id == tenant_id,
                    AgentRunRow.status.in_(
                        (
                            RunStatus.RUNNING.value,
                            RunStatus.PENDING.value,
                            RunStatus.QUEUED.value,
                        )
                    ),
                )
                .values(values)
            )
            await session.commit()
        return int(getattr(result, "rowcount", 0) or 0) > 0

    async def fail_if_active(
        self, *, run_id: UUID, tenant_id: UUID, error: str, now: datetime
    ) -> bool:
        # Same CAS shape as request_cancel / approval.mark_decided — the
        # ``status IN (running, pending)`` guard is the exactly-once win
        # condition when several replicas race the same orphan's failover.
        async with self._sf() as session:
            result = await session.execute(
                update(AgentRunRow)
                .where(
                    AgentRunRow.id == run_id,
                    AgentRunRow.tenant_id == tenant_id,
                    AgentRunRow.status.in_((RunStatus.RUNNING.value, RunStatus.PENDING.value)),
                )
                .values(
                    status=RunStatus.ERROR.value,
                    error=error,
                    finished_at=now,
                    updated_at=now,
                )
            )
            await session.commit()
        return int(getattr(result, "rowcount", 0) or 0) > 0

    async def get(self, *, run_id: UUID, tenant_id: UUID) -> RunInfo | None:
        async with self._sf() as session:
            row = (
                await session.execute(
                    select(AgentRunRow).where(
                        AgentRunRow.id == run_id,
                        AgentRunRow.tenant_id == tenant_id,
                    )
                )
            ).scalar_one_or_none()
        return _row_to_dto(row) if row is not None else None

    async def find_by_idempotency_key(self, *, tenant_id: UUID, key: str) -> RunInfo | None:
        async with self._sf() as session:
            row = (
                await session.execute(
                    select(AgentRunRow).where(
                        AgentRunRow.tenant_id == tenant_id,
                        AgentRunRow.idempotency_key == key,
                    )
                )
            ).scalar_one_or_none()
        return _row_to_dto(row) if row is not None else None

    async def list_by_thread(self, *, thread_id: UUID, tenant_id: UUID) -> list[RunInfo]:
        async with self._sf() as session:
            rows = (
                (
                    await session.execute(
                        select(AgentRunRow)
                        .where(
                            AgentRunRow.thread_id == thread_id,
                            AgentRunRow.tenant_id == tenant_id,
                        )
                        .order_by(AgentRunRow.created_at.asc())
                    )
                )
                .scalars()
                .all()
            )
        return [_row_to_dto(r) for r in rows]

    async def delete_by_thread(self, *, thread_id: UUID, tenant_id: UUID) -> int:
        # Deletion-hygiene PR3 §A — empty the ``run_event`` children in the
        # SAME transaction, children before parents: ``ON DELETE RESTRICT``
        # would otherwise block the ``agent_run`` delete (the ghost-run bug).
        async with self._sf() as session:
            run_ids = (
                select(AgentRunRow.id)
                .where(
                    AgentRunRow.thread_id == thread_id,
                    AgentRunRow.tenant_id == tenant_id,
                )
                .scalar_subquery()
            )
            await session.execute(delete(RunEventRow).where(RunEventRow.run_id.in_(run_ids)))
            result = await session.execute(
                delete(AgentRunRow).where(
                    AgentRunRow.thread_id == thread_id,
                    AgentRunRow.tenant_id == tenant_id,
                )
            )
            await session.commit()
        return int(getattr(result, "rowcount", 0) or 0)

    async def anonymize_all_for_user(self, *, tenant_id: UUID, user_id: UUID) -> int:
        # Null the PII link, KEEP the row (run_event → agent_run is ON DELETE
        # RESTRICT; billing/analytics keep the lifecycle row). Explicit
        # tenant_id predicate — never rely on RLS alone for a mutation.
        stmt = (
            update(AgentRunRow)
            .where(
                AgentRunRow.tenant_id == tenant_id,
                AgentRunRow.user_id == user_id,
            )
            .values(user_id=None)
        )
        async with self._sf() as session:
            result = await session.execute(stmt)
            await session.commit()
        return int(getattr(result, "rowcount", 0) or 0)

    async def list_for_tenant(
        self,
        *,
        tenant_id: UUID,
        status: RunStatus | None = None,
        thread_ids: Collection[UUID] | None = None,
        user_id: UUID | None = None,
        agent_name: str | None = None,
        q: str | None = None,
        limit: int = 100,
        offset: int = 0,
        before: tuple[datetime, UUID] | None = None,
    ) -> list[RunInfo]:
        if thread_ids is not None and not thread_ids:
            return []
        clamped = _clamp_limit(limit)
        stmt = (
            select(AgentRunRow)
            .where(AgentRunRow.tenant_id == tenant_id)
            .order_by(AgentRunRow.created_at.desc(), AgentRunRow.id.desc())
            .limit(clamped)
            .offset(max(0, offset))
        )
        if before is not None:
            # 行值比较 ``(created_at, id) < before``,与内存 store 的元组比较
            # 同义。展开成 or_/and_ 而不用 ``tuple_``:两种写法在 Postgres 上
            # 等价,展开式对 ``ix_agent_run_thread_id`` 更友好,也不依赖驱动怎么
            # 绑定复合字面量。
            cursor_at, cursor_id = before
            stmt = stmt.where(
                or_(
                    AgentRunRow.created_at < cursor_at,
                    and_(AgentRunRow.created_at == cursor_at, AgentRunRow.id < cursor_id),
                )
            )
        if status is not None:
            stmt = stmt.where(AgentRunRow.status == status.value)
        if user_id is not None:
            stmt = stmt.where(AgentRunRow.user_id == user_id)
        if thread_ids is not None:
            stmt = stmt.where(AgentRunRow.thread_id.in_(list(thread_ids)))
        if agent_name is not None:
            # agent 绑定在 thread_meta 上,不在 agent_run 上 —— 与
            # ``list_running_for_agent`` 同一个 join。注意这个 join 必须
            # 只在需要时加:无条件 join 会把没有 thread_meta 行的 run
            # (理论上不该有,但历史数据里可能存在)从默认列表里静默剔掉。
            stmt = stmt.join(ThreadMetaRow, ThreadMetaRow.thread_id == AgentRunRow.thread_id).where(
                ThreadMetaRow.agent_name == agent_name
            )
        if q:
            pat = _like_contains(q)
            stmt = stmt.where(
                or_(
                    cast(AgentRunRow.id, String).ilike(pat, escape="\\"),
                    cast(AgentRunRow.thread_id, String).ilike(pat, escape="\\"),
                )
            )
        async with self._sf() as session:
            rows = (await session.execute(stmt)).scalars().all()
        return [_row_to_dto(r) for r in rows]

    async def list_running_for_agent(
        self, *, tenant_id: UUID, agent_name: str, agent_version: str | None = None
    ) -> list[RunInfo]:
        # Stream RT-4 (RT-ADR-17) — join agent_run ↔ thread_meta on thread_id;
        # the agent binding lives on thread_meta. RLS scopes agent_run to the
        # tenant; the explicit tenant_id predicate keeps the in-memory double's
        # semantics aligned.
        stmt = (
            select(AgentRunRow)
            .join(ThreadMetaRow, ThreadMetaRow.thread_id == AgentRunRow.thread_id)
            .where(
                AgentRunRow.tenant_id == tenant_id,
                AgentRunRow.status == RunStatus.RUNNING.value,
                ThreadMetaRow.agent_name == agent_name,
            )
            .order_by(AgentRunRow.created_at.desc())
        )
        if agent_version is not None:
            # NULL agent_version rows drop out here (SQL NULL comparison) —
            # same as the in-memory double's ``!= agent_version`` skip.
            stmt = stmt.where(ThreadMetaRow.agent_version == agent_version)
        async with self._sf() as session:
            rows = (await session.execute(stmt)).scalars().all()
        return [_row_to_dto(r) for r in rows]

    async def list_all_tenants(
        self,
        *,
        status: RunStatus | None = None,
        thread_ids: Collection[UUID] | None = None,
        user_id: UUID | None = None,
        q: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[RunInfo]:
        # Stream N — no tenant filter; caller MUST wrap in bypass_rls_session().
        if thread_ids is not None and not thread_ids:
            return []
        clamped = _clamp_limit(limit)
        stmt = (
            select(AgentRunRow)
            .order_by(AgentRunRow.created_at.desc())
            .limit(clamped)
            .offset(max(0, offset))
        )
        if status is not None:
            stmt = stmt.where(AgentRunRow.status == status.value)
        if user_id is not None:
            stmt = stmt.where(AgentRunRow.user_id == user_id)
        if thread_ids is not None:
            stmt = stmt.where(AgentRunRow.thread_id.in_(list(thread_ids)))
        if q:
            pat = _like_contains(q)
            stmt = stmt.where(
                or_(
                    cast(AgentRunRow.id, String).ilike(pat, escape="\\"),
                    cast(AgentRunRow.thread_id, String).ilike(pat, escape="\\"),
                )
            )
        async with self._sf() as session:
            rows = (await session.execute(stmt)).scalars().all()
        return [_row_to_dto(r) for r in rows]

    async def thread_ids_with_runs(
        self,
        *,
        tenant_id: UUID | None,
        since: datetime | None = None,
        only: Literal["failed", "pending"] | None = None,
        limit: int = 500,
    ) -> set[UUID]:
        wanted = _only_status_values(only)
        stmt = select(AgentRunRow.thread_id).distinct().limit(limit)
        if wanted is not None:
            stmt = stmt.where(AgentRunRow.status.in_(sorted(wanted)))
        if since is not None:
            stmt = stmt.where(AgentRunRow.created_at >= since)
        if tenant_id is not None:
            stmt = stmt.where(AgentRunRow.tenant_id == tenant_id)
        async with self._sf() as session:
            rows = (await session.execute(stmt)).scalars().all()
        return set(rows)

    async def aggregate_by_threads(
        self,
        *,
        thread_ids: Collection[UUID],
        tenant_id: UUID | None,
    ) -> dict[UUID, ThreadRunAggregate]:
        ids = list(thread_ids)
        if not ids:
            return {}
        trace_col = AgentRunRow.trace_id
        stmt = (
            select(
                AgentRunRow.thread_id,
                func.count().label("run_count"),
                func.count()
                .filter(AgentRunRow.status.in_(sorted(_FAILED_RUN_VALUES)))
                .label("error_count"),
                func.count()
                .filter(AgentRunRow.status == RunStatus.PAUSED.value)
                .label("pending_count"),
                func.max(AgentRunRow.created_at).label("last_run_at"),
                # Non-null trace ids only; deduped/sorted in Python below.
                func.array_agg(trace_col).filter(trace_col.isnot(None)).label("trace_ids"),
            )
            .where(AgentRunRow.thread_id.in_(ids))
            .group_by(AgentRunRow.thread_id)
        )
        if tenant_id is not None:
            stmt = stmt.where(AgentRunRow.tenant_id == tenant_id)
        async with self._sf() as session:
            rows = (await session.execute(stmt)).all()
        result: dict[UUID, ThreadRunAggregate] = {}
        for row in rows:
            raw_traces = row.trace_ids or []
            result[row.thread_id] = ThreadRunAggregate(
                thread_id=row.thread_id,
                run_count=int(row.run_count),
                error_count=int(row.error_count),
                pending_count=int(row.pending_count),
                last_run_at=row.last_run_at,
                trace_ids=tuple(sorted({t for t in raw_traces if t is not None})),
            )
        return result

    async def set_trace_id(
        self,
        *,
        run_id: UUID,
        tenant_id: UUID,
        trace_id: str,
    ) -> bool:
        async with self._sf() as session:
            result = await session.execute(
                update(AgentRunRow)
                .where(AgentRunRow.id == run_id, AgentRunRow.tenant_id == tenant_id)
                .values({"trace_id": trace_id})
            )
            await session.commit()
        return int(getattr(result, "rowcount", 0) or 0) > 0

    async def claim(
        self,
        *,
        run_id: UUID,
        tenant_id: UUID,
        claimed_by: str,
        lease_until: datetime,
        heartbeat_at: datetime,
    ) -> bool:
        async with self._sf() as session:
            result = await session.execute(
                update(AgentRunRow)
                .where(AgentRunRow.id == run_id, AgentRunRow.tenant_id == tenant_id)
                .values(claimed_by=claimed_by, lease_until=lease_until, heartbeat_at=heartbeat_at)
            )
            await session.commit()
        return int(getattr(result, "rowcount", 0) or 0) > 0

    async def heartbeat(
        self,
        *,
        run_id: UUID,
        claimed_by: str,
        lease_until: datetime,
        heartbeat_at: datetime,
    ) -> bool:
        async with self._sf() as session:
            result = await session.execute(
                update(AgentRunRow)
                .where(
                    AgentRunRow.id == run_id,
                    AgentRunRow.claimed_by == claimed_by,
                    AgentRunRow.status == RunStatus.RUNNING.value,
                )
                .values(lease_until=lease_until, heartbeat_at=heartbeat_at)
            )
            await session.commit()
        return int(getattr(result, "rowcount", 0) or 0) > 0

    async def list_orphans(self, *, now: datetime, limit: int) -> list[RunInfo]:
        # Cross-tenant: no tenant filter — caller wraps in bypass_rls_session().
        stmt = (
            select(AgentRunRow)
            .where(
                AgentRunRow.status == RunStatus.RUNNING.value,
                AgentRunRow.lease_until.is_not(None),
                AgentRunRow.lease_until < now,
            )
            .order_by(AgentRunRow.lease_until.asc())
            .limit(max(1, limit))
        )
        async with self._sf() as session:
            rows = (await session.execute(stmt)).scalars().all()
        return [_row_to_dto(r) for r in rows]

    async def list_stale_pending(self, *, cutoff: datetime, limit: int) -> list[RunInfo]:
        # Cross-tenant: no tenant filter — caller wraps in bypass_rls_session().
        stmt = (
            select(AgentRunRow)
            .where(
                AgentRunRow.status == RunStatus.PENDING.value,
                AgentRunRow.created_at < cutoff,
            )
            .order_by(AgentRunRow.created_at.asc())
            .limit(max(1, limit))
        )
        async with self._sf() as session:
            rows = (await session.execute(stmt)).scalars().all()
        return [_row_to_dto(r) for r in rows]

    async def reclaim(
        self,
        *,
        run_id: UUID,
        new_owner: str,
        lease_until: datetime,
        heartbeat_at: datetime,
        now: datetime,
    ) -> bool:
        # Atomic CAS — exactly one sweeper wins the orphan. The
        # ``status='running' AND lease_until < now`` predicate is the guard;
        # the loser's UPDATE matches zero rows (lease already renewed).
        async with self._sf() as session:
            result = await session.execute(
                update(AgentRunRow)
                .where(
                    AgentRunRow.id == run_id,
                    AgentRunRow.status == RunStatus.RUNNING.value,
                    AgentRunRow.lease_until.is_not(None),
                    AgentRunRow.lease_until < now,
                )
                .values(
                    claimed_by=new_owner,
                    lease_until=lease_until,
                    heartbeat_at=heartbeat_at,
                    reclaim_count=AgentRunRow.reclaim_count + 1,
                )
            )
            await session.commit()
        return int(getattr(result, "rowcount", 0) or 0) > 0

    async def list_queued(self, *, limit: int) -> list[RunInfo]:
        # Cross-tenant: no tenant filter — caller wraps in bypass_rls_session().
        stmt = (
            select(AgentRunRow)
            .where(AgentRunRow.status == RunStatus.QUEUED.value)
            .order_by(AgentRunRow.created_at.asc())
            .limit(max(1, limit))
        )
        async with self._sf() as session:
            rows = (await session.execute(stmt)).scalars().all()
        return [_row_to_dto(r) for r in rows]

    async def claim_queued(
        self,
        *,
        run_id: UUID,
        new_owner: str,
        lease_until: datetime,
        heartbeat_at: datetime,
    ) -> RunInfo | None:
        # Atomic CAS — exactly one worker wins the queued run. ``RETURNING``
        # hands back the claimed row (with ``enqueued_input``) in one round
        # trip; the loser's UPDATE matches zero rows and returns ``None``.
        async with self._sf() as session:
            result = await session.execute(
                update(AgentRunRow)
                .where(
                    AgentRunRow.id == run_id,
                    AgentRunRow.status == RunStatus.QUEUED.value,
                )
                .values(
                    status=RunStatus.RUNNING.value,
                    claimed_by=new_owner,
                    lease_until=lease_until,
                    heartbeat_at=heartbeat_at,
                )
                .returning(AgentRunRow)
            )
            row = result.scalars().first()
            await session.commit()
        return _row_to_dto(row) if row is not None else None
