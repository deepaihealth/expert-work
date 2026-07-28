"""Stream K.K7 — ``MemoryDLQWorker`` retry semantics.

Covers the worker's behaviour against an in-memory DLQ + memory store:

- happy retry → row removed
- transient failure → row stays, ``attempts`` bumps, ``next_retry_at``
  pushed by the configured backoff
- ``attempts >= max_attempts`` → row marked as dead letter (not
  retried, ``next_retry_at`` parked far in the future)
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from control_plane.memory import MemoryDLQWorker
from control_plane.memory.dlq_worker import _build_memory_items
from expert_work.persistence import InMemoryMemoryStore
from expert_work.persistence.memory import InMemoryMemoryWritebackDLQ

_TENANT = uuid4()
_USER = uuid4()


class _ScriptedEmbedder:
    """Each call returns the next scripted output. Failure rotation is
    handled by raising from a recorded exception."""

    def __init__(
        self,
        outputs: Sequence[list[tuple[float, ...]] | Exception],
    ) -> None:
        self._outputs = list(outputs)
        self.calls = 0

    async def embed(self, texts: Sequence[str], *, tenant_id: UUID) -> list[tuple[float, ...]]:
        del tenant_id
        idx = self.calls
        self.calls += 1
        if idx >= len(self._outputs):
            raise RuntimeError(f"scripted embedder ran out at call {idx}")
        out = self._outputs[idx]
        if isinstance(out, Exception):
            raise out
        # The recorded output has one vector per text; assert the
        # shape matches so a buggy script surfaces here, not in the
        # store.
        assert len(out) == len(texts), (out, texts)
        return out


async def _seed_dlq(dlq: InMemoryMemoryWritebackDLQ, *, error: str = "boom") -> None:
    await dlq.enqueue(
        tenant_id=_TENANT,
        user_id=_USER,
        source_thread_id="t-1",
        extracted=[("fact", "Likes coffee")],
        error=error,
    )


@pytest.mark.asyncio
async def test_build_memory_items_carries_source_run_id() -> None:
    """P5b-2a — DLQ-retry reconstruction must stamp the rebuilt
    ``MemoryItem``s with the row's ``source_run_id`` so a retried memory
    keeps its provenance link back to the run whose write-back failed."""
    dlq = InMemoryMemoryWritebackDLQ()
    row = await dlq.enqueue(
        tenant_id=_TENANT,
        user_id=_USER,
        source_thread_id="t-1",
        source_run_id="run-123",
        extracted=[("fact", "Likes coffee"), ("episodic", "Visited Kyoto")],
        error="boom",
    )
    vectors = [(0.1, 0.2, 0.3), (0.4, 0.5, 0.6)]

    items = _build_memory_items(row, vectors)

    assert len(items) == 2
    assert all(item.source_run_id == "run-123" for item in items)


@pytest.mark.asyncio
async def test_happy_retry_writes_memory_and_clears_dlq() -> None:
    dlq = InMemoryMemoryWritebackDLQ()
    store = InMemoryMemoryStore()
    await _seed_dlq(dlq)
    embedder = _ScriptedEmbedder([[(0.1, 0.2, 0.3)]])

    worker = MemoryDLQWorker(dlq=dlq, memory_store=store, embedder=embedder)
    succeeded, retried, dead = await worker.run_once()

    assert (succeeded, retried, dead) == (1, 0, 0)
    assert await dlq.count() == 0
    items = await store.list_for_user(tenant_id=_TENANT, user_id=_USER)
    assert len(items) == 1
    assert items[0].content == "Likes coffee"


@pytest.mark.asyncio
async def test_transient_failure_schedules_backoff_retry() -> None:
    """An embedder failure leaves the row in the DLQ with attempts=1
    and ``next_retry_at`` pushed to the first-backoff slot (1 min)."""
    dlq = InMemoryMemoryWritebackDLQ()
    store = InMemoryMemoryStore()
    await _seed_dlq(dlq)
    # First call raises; the worker must catch and reschedule.
    embedder = _ScriptedEmbedder([RuntimeError("embed-fail")])

    worker = MemoryDLQWorker(dlq=dlq, memory_store=store, embedder=embedder)
    succeeded, retried, dead = await worker.run_once()
    assert (succeeded, retried, dead) == (0, 1, 0)

    # Still in the queue, attempts bumped, next_retry_at pushed >= 60s out.
    pending = list(dlq._rows.values())
    assert len(pending) == 1
    pending_row = pending[0]
    assert pending_row.attempts == 1
    assert pending_row.last_error is not None
    assert "embed-fail" in pending_row.last_error
    # Sanity on the schedule — first backoff is 60 s.
    delta = (pending_row.next_retry_at - datetime.now(UTC)).total_seconds()
    assert 30 <= delta <= 90, delta


@pytest.mark.asyncio
async def test_max_attempts_marks_dead_letter() -> None:
    """A row at ``attempts == max_attempts - 1`` whose retry fails
    becomes a dead letter; future ``take_ready`` calls inside the
    backoff window skip it.

    W1-PR1 Task 2 recalibration: ``attempts`` is now counted at claim
    time (``take_ready``), not inside ``record_failure`` — so "4 prior
    failed attempts" means 4 prior claim+fail cycles, not 4 bare
    ``record_failure`` calls (which no longer touch ``attempts`` at
    all)."""
    dlq = InMemoryMemoryWritebackDLQ()
    store = InMemoryMemoryStore()
    await _seed_dlq(dlq)
    # Bring the row to attempts=4 via 4 claim+fail cycles — the 5th claim
    # (inside worker.run_once below) crosses the max_attempts=5 threshold.
    row_id = next(iter(dlq._rows))
    for _ in range(4):
        now = datetime.now(UTC)
        claimed = await dlq.take_ready(limit=10, now=now)
        assert [r.id for r in claimed] == [row_id]
        await dlq.record_failure(row_id=row_id, error="prior", when=now, next_retry_at=now)

    embedder = _ScriptedEmbedder([RuntimeError("still broken")])
    worker = MemoryDLQWorker(dlq=dlq, memory_store=store, embedder=embedder)
    succeeded, retried, dead = await worker.run_once()
    assert (succeeded, retried, dead) == (0, 0, 1)

    pending = list(dlq._rows.values())
    assert len(pending) == 1, "dead letter should remain in the queue for review"
    assert pending[0].attempts == 5  # claimed once per cycle, never doubled
    # next_retry_at parked far ahead so the cycle doesn't keep
    # re-trying it.
    far = (pending[0].next_retry_at - datetime.now(UTC)).total_seconds()
    assert far > 30 * 24 * 3600  # parked > 30 days out


class _FlakyMarkDoneDLQ(InMemoryMemoryWritebackDLQ):
    """``mark_done`` raises once — exercises the ``run_once`` release path
    for a row ``_attempt_one`` claimed but never resolved (W1-PR1 Task 2
    follow-up, same trap as the webhook delivery worker's unhandled-
    exception branch)."""

    def __init__(self) -> None:
        super().__init__()
        self.mark_done_calls = 0

    async def mark_done(self, *, row_id: UUID) -> None:
        self.mark_done_calls += 1
        raise RuntimeError("db down")


@pytest.mark.asyncio
async def test_unhandled_store_exception_releases_claim_immediately() -> None:
    """If the DLQ-store call inside ``_attempt_one`` itself raises past its
    own handling (here: ``mark_done`` failing on the happy path), the row
    must not sit stuck until the 600s claim lease elapses. ``run_once``
    must (a) not propagate the exception, and (b) release the row back to
    ``next_retry_at=now`` so the very next sweep retries it.

    Choice locked here (see task-2-report.md): release keeps ``attempts``
    exactly as the claim left it — a release is not a second attempt, but
    the claim already spent one, so it is not rolled back either."""
    dlq = _FlakyMarkDoneDLQ()
    store = InMemoryMemoryStore()
    await _seed_dlq(dlq)
    embedder = _ScriptedEmbedder([[(0.1, 0.2, 0.3)]])
    worker = MemoryDLQWorker(dlq=dlq, memory_store=store, embedder=embedder)

    succeeded, retried, dead = await worker.run_once()  # must not raise

    assert (succeeded, retried, dead) == (0, 0, 0)
    assert dlq.mark_done_calls == 1

    [row] = list(dlq._rows.values())
    assert row.attempts == 1  # the claim's bump stands; release doesn't roll it back
    delta = (row.next_retry_at - datetime.now(UTC)).total_seconds()
    assert delta < 5, "released row must be immediately re-claimable, not stuck at the 600s lease"


@pytest.mark.asyncio
async def test_run_once_is_noop_when_queue_empty() -> None:
    """``take_ready`` returns nothing → counters all zero, no calls
    to the embedder."""
    dlq = InMemoryMemoryWritebackDLQ()
    store = InMemoryMemoryStore()
    embedder = _ScriptedEmbedder([])  # no outputs → would raise if called
    worker = MemoryDLQWorker(dlq=dlq, memory_store=store, embedder=embedder)

    result = await worker.run_once()

    assert result == (0, 0, 0)
    assert embedder.calls == 0


@pytest.mark.asyncio
async def test_take_ready_skips_rows_not_yet_due(monkeypatch: pytest.MonkeyPatch) -> None:
    """A row whose next_retry_at is later than ``now`` is left alone."""
    dlq = InMemoryMemoryWritebackDLQ()
    store = InMemoryMemoryStore()
    await _seed_dlq(dlq)
    row_id = next(iter(dlq._rows))
    # Push the row's retry into the future — worker.run_once with the
    # current clock must not pick it up.
    future = datetime.now(UTC).replace(year=datetime.now(UTC).year + 1)
    await dlq.record_failure(
        row_id=row_id, error="later", when=datetime.now(UTC), next_retry_at=future
    )

    embedder = _ScriptedEmbedder([])
    worker = MemoryDLQWorker(dlq=dlq, memory_store=store, embedder=embedder)
    succeeded, retried, dead = await worker.run_once()

    assert (succeeded, retried, dead) == (0, 0, 0)
    assert embedder.calls == 0


@pytest.mark.asyncio
async def test_rejects_non_positive_constructor_args() -> None:
    dlq = InMemoryMemoryWritebackDLQ()
    store = InMemoryMemoryStore()
    embedder = _ScriptedEmbedder([])
    with pytest.raises(ValueError, match="interval_s"):
        MemoryDLQWorker(dlq=dlq, memory_store=store, embedder=embedder, interval_s=0)
    with pytest.raises(ValueError, match="batch_size"):
        MemoryDLQWorker(dlq=dlq, memory_store=store, embedder=embedder, batch_size=0)
    with pytest.raises(ValueError, match="max_attempts"):
        MemoryDLQWorker(dlq=dlq, memory_store=store, embedder=embedder, max_attempts=0)


@pytest.mark.asyncio
async def test_dedup_skips_duplicate_writeback_via_on_conflict() -> None:
    """Stream K.K7 dedup — writing the same content twice yields one
    row, not two. (Mirrors what the SQL UNIQUE + ON CONFLICT does.)"""
    store = InMemoryMemoryStore()
    from expert_work.protocol import MemoryItem

    same_text = "Loves espresso"
    items = [
        MemoryItem(
            id=uuid4(),
            tenant_id=_TENANT,
            user_id=_USER,
            kind="fact",
            content=same_text,
            embedding=(0.1, 0.2, 0.3),
        ),
        MemoryItem(
            id=uuid4(),
            tenant_id=_TENANT,
            user_id=_USER,
            kind="fact",
            content=same_text,  # identical
            embedding=(0.4, 0.5, 0.6),
        ),
    ]
    await store.write(items)
    out = await store.list_for_user(tenant_id=_TENANT, user_id=_USER)
    assert len(out) == 1
    # The first write wins — the dedup is "do nothing on conflict",
    # so the second item with the same content_hash is dropped.
    assert out[0].embedding == (0.1, 0.2, 0.3)
