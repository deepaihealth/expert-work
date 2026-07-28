"""Stream K.K7 — memory writeback DLQ retry worker.

A periodic loop, started from the control-plane lifespan, that drains
the ``memory_writeback_dlq`` table the orchestrator writes to when
the in-run writeback path fails after extraction succeeded. Each
ready row gets a fresh embed + ``MemoryStore.write`` attempt; success
removes the row, failure records the error and schedules an
exponential backoff. After :data:`_MAX_ATTEMPTS` failures the row is
left in place as a dead letter for operator review.

Modelled after :class:`control_plane.quota.reaper.ReservationReaper`:

- ``start`` schedules the task; idempotent.
- ``stop`` cooperates via an :class:`asyncio.Event`; the loop never
  raises, so the process is never crashed by the worker.
- Per-cycle errors are logged + counted.
- ``run_once`` is the unit-testable entry point.

Mini-ADR K-5 / STREAM-K-DESIGN § 3.K7.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from control_plane.uplift.threat_metrics import record_memory_blocked
from expert_work.common.observability import expert_work_counter
from expert_work.persistence.memory import MemoryStore, MemoryWritebackDLQ
from expert_work.persistence.memory.base import MemoryInjectionBlockedError
from expert_work.protocol import MemoryItem
from orchestrator.llm import Embedder

logger = logging.getLogger("expert_work.control_plane.memory.dlq_worker")

#: Each cycle drains up to this many rows. Bounded so the loop tail
#: latency stays predictable.
_BATCH_SIZE: int = 25

#: Past this many failed attempts we stop retrying and leave the row
#: as a dead letter — its ``last_error`` carries the diagnostic context
#: an operator needs.
_MAX_ATTEMPTS: int = 5

#: Backoff schedule (seconds) keyed by attempt number. 1 min → 5 min →
#: 30 min → 2 h → 6 h. Cycle interval is independent of this — the
#: worker only retries rows whose ``next_retry_at`` has elapsed.
_BACKOFF_SCHEDULE: tuple[int, ...] = (60, 5 * 60, 30 * 60, 2 * 3600, 6 * 3600)


_cycle_errors = expert_work_counter(
    "expert_work_control_plane_memory_dlq_cycle_errors_total",
    "Memory DLQ worker cycles that ended in a caught exception.",
)
_dead_letters = expert_work_counter(
    "expert_work_control_plane_memory_dlq_dead_letters_total",
    "DLQ rows abandoned because they exceeded the max retry attempts.",
)
_retries_succeeded = expert_work_counter(
    "expert_work_control_plane_memory_dlq_retries_succeeded_total",
    "DLQ rows successfully retried and removed from the queue.",
)


def _backoff_seconds(next_attempt: int) -> int:
    """Return the wait time before ``next_attempt`` (1-based)."""
    if next_attempt <= 0:
        return _BACKOFF_SCHEDULE[0]
    idx = min(next_attempt - 1, len(_BACKOFF_SCHEDULE) - 1)
    return _BACKOFF_SCHEDULE[idx]


class MemoryDLQWorker:
    """Background task: re-embed and re-write failed memory writebacks."""

    def __init__(
        self,
        *,
        dlq: MemoryWritebackDLQ,
        memory_store: MemoryStore,
        embedder: Embedder,
        interval_s: int = 30,
        batch_size: int = _BATCH_SIZE,
        max_attempts: int = _MAX_ATTEMPTS,
    ) -> None:
        if interval_s <= 0:
            msg = "interval_s must be positive"
            raise ValueError(msg)
        if batch_size <= 0:
            msg = "batch_size must be positive"
            raise ValueError(msg)
        if max_attempts <= 0:
            msg = "max_attempts must be positive"
            raise ValueError(msg)
        self._dlq = dlq
        self._store = memory_store
        self._embedder = embedder
        self._interval_s = interval_s
        self._batch_size = batch_size
        self._max_attempts = max_attempts
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        """Schedule the periodic loop. Idempotent."""
        if self.is_running:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._loop(), name="memory-dlq-worker")

    async def stop(self) -> None:
        """Cooperative stop — signal + await the loop's clean exit."""
        if self._task is None:
            return
        self._stop.set()
        try:
            await asyncio.wait_for(self._task, timeout=self._interval_s + 5)
        except (TimeoutError, asyncio.CancelledError):
            self._task.cancel()
        finally:
            self._task = None

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self.run_once()
            except Exception:
                _cycle_errors.inc()
                logger.exception("memory.dlq_worker.cycle_failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval_s)
            except TimeoutError:
                continue
            else:
                break

    async def run_once(self) -> tuple[int, int, int]:
        """Drain one batch.

        Returns ``(succeeded, retried, dead_lettered)``.
        """
        now = datetime.now(UTC)
        ready = await self._dlq.take_ready(limit=self._batch_size, now=now)
        succeeded = 0
        retried = 0
        dead = 0
        for row in ready:
            try:
                outcome = await self._attempt_one(row, now=now)
            except Exception as exc:
                # take_ready already claimed this row (attempts bumped,
                # next_retry_at pushed to the claim lease) before handing
                # it to _attempt_one. If the DLQ-store call *inside* one of
                # _attempt_one's own except-branches raises (mark_done /
                # record_failure itself failing — e.g. a DB hiccup), that
                # exception is not caught there, so neither the success nor
                # the failure path settles the claim. Left alone the row
                # would sit stuck until the lease elapses — the same trap
                # the webhook delivery worker's unhandled-exception branch
                # hit (see ``webhook_delivery_worker._release``). Release
                # it immediately instead.
                logger.warning("memory.dlq_worker.unhandled row_id=%s err=%s", row.id, exc)
                await self._release(row, now=now, error=exc)
                continue
            if outcome == "ok":
                succeeded += 1
                _retries_succeeded.inc()
            elif outcome == "retry":
                retried += 1
            else:
                dead += 1
                _dead_letters.inc()
        if ready:
            logger.info(
                "memory.dlq_worker.cycle batch=%d ok=%d retry=%d dead=%d",
                len(ready),
                succeeded,
                retried,
                dead,
            )
        return succeeded, retried, dead

    async def _attempt_one(self, row: DLQRowLike, *, now: datetime) -> str:
        """Retry one row. Returns ``"ok"`` / ``"retry"`` / ``"dead"``."""
        try:
            vectors = await self._embedder.embed(
                [content for _, content in row.extracted], tenant_id=row.tenant_id
            )
            items = _build_memory_items(row, vectors)
            await self._store.write(items)
        except MemoryInjectionBlockedError as exc:
            # Capability Uplift Sprint #2 — content can't pass strict
            # scan; retrying won't change the content, so skip the
            # backoff loop and dead-letter immediately. The audit row
            # already lands inside MemoryStore.write() callers as the
            # exception bubbles up here.
            logger.error(
                "memory.dlq_worker.dead_letter_injection row_id=%s blocked=%d",
                row.id,
                len(exc.blocked),
            )
            record_memory_blocked(source="dlq")
            await self._dlq.record_failure(
                row_id=row.id,
                error=f"MemoryInjectionBlockedError: {len(exc.blocked)} item(s)",
                when=now,
                next_retry_at=now + timedelta(days=365),
            )
            return "dead"
        except Exception as exc:
            # ``row.attempts`` already counts this try — take_ready's claim
            # bumped it atomically before handing the row here (W1-PR1).
            next_attempt_number = row.attempts
            if next_attempt_number >= self._max_attempts:
                logger.error(
                    "memory.dlq_worker.dead_letter row_id=%s attempts=%d last_error=%s",
                    row.id,
                    next_attempt_number,
                    exc,
                )
                # Record the failure but do not bump ``next_retry_at``
                # past the schedule — the row stays as a dead letter for
                # operator review.
                await self._dlq.record_failure(
                    row_id=row.id,
                    error=f"{type(exc).__name__}: {exc}",
                    when=now,
                    next_retry_at=now + timedelta(days=365),
                )
                return "dead"
            backoff = _backoff_seconds(next_attempt_number)
            await self._dlq.record_failure(
                row_id=row.id,
                error=f"{type(exc).__name__}: {exc}",
                when=now,
                next_retry_at=now + timedelta(seconds=backoff),
            )
            return "retry"
        await self._dlq.mark_done(row_id=row.id)
        return "ok"

    async def _release(self, row: DLQRowLike, *, now: datetime, error: BaseException) -> None:
        """Best-effort release for a row ``take_ready`` claimed but
        ``_attempt_one`` never resolved (see the call site in
        :meth:`run_once`). Reuses :meth:`MemoryWritebackDLQ.record_failure`
        — its new claim-time-only attempts semantics (W1-PR1) mean this
        call only touches ``last_error``/``next_retry_at``, so it does not
        double-count the attempt ``take_ready`` already booked; a release
        is not a second try, and the claim already spent one. Setting
        ``next_retry_at=now`` makes the row immediately re-claimable
        instead of sitting out the rest of the ``_CLAIM_LEASE_S`` window.

        Failure here is logged and swallowed, not raised — the claim
        lease itself is the backstop that reclaims the row even if this
        call fails too.
        """
        try:
            await self._dlq.record_failure(
                row_id=row.id,
                error=f"dlq_worker.unhandled: {type(error).__name__}: {error}",
                when=now,
                next_retry_at=now,
            )
        except Exception:
            logger.exception("memory.dlq_worker.release_failed row_id=%s", row.id)


def _build_memory_items(row: DLQRowLike, vectors: Sequence[Sequence[float]]) -> list[MemoryItem]:
    return [
        MemoryItem(
            id=uuid4(),
            tenant_id=row.tenant_id,
            user_id=row.user_id,
            kind=kind,  # type: ignore[arg-type]
            content=content,
            embedding=tuple(float(x) for x in vector),
            source_thread_id=row.source_thread_id,
            source_run_id=row.source_run_id,
        )
        for (kind, content), vector in zip(row.extracted, vectors, strict=True)
    ]


# ``DLQRow`` lives in expert-work-persistence; importing it here would create
# a fragile cycle through the control-plane → persistence → control-plane
# chain that runs at module load. The worker only ever consumes the
# fields below, so a lightweight structural alias is enough.
class DLQRowLike:  # pragma: no cover - protocol-only
    """Structural duck-type for :class:`expert_work.persistence.memory.DLQRow`."""

    id: object
    tenant_id: UUID
    user_id: object
    source_thread_id: str | None
    source_run_id: str | None
    extracted: Sequence[tuple[str, str]]
    attempts: int


__all__ = ["MemoryDLQWorker"]
