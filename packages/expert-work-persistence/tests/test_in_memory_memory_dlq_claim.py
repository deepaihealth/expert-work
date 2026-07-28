"""``InMemoryMemoryWritebackDLQ`` claim semantics — W1-PR1 Task 2.

``take_ready`` used to be a pure read (``SELECT`` with no state change);
it is now a claim: it atomically bumps ``attempts`` and pushes
``next_retry_at`` out to the claim lease (``_CLAIM_LEASE_S`` = 600s) so a
fleet of DLQ worker replicas never double-embeds/double-writes the same
row. ``record_failure`` no longer touches ``attempts`` — that's counted
once, at claim time.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from expert_work.persistence.memory.dlq import InMemoryMemoryWritebackDLQ


async def _seed(dlq: InMemoryMemoryWritebackDLQ) -> UUID:
    row = await dlq.enqueue(
        tenant_id=uuid4(),
        user_id=uuid4(),
        source_thread_id="t",
        extracted=[("fact", "x")],
        error="boom",
    )
    return row.id


@pytest.mark.asyncio
async def test_take_ready_bumps_attempts_and_pushes_lease() -> None:
    dlq = InMemoryMemoryWritebackDLQ()
    row_id = await _seed(dlq)
    now = datetime.now(UTC)

    [claimed] = await dlq.take_ready(limit=10, now=now)

    assert claimed.id == row_id
    assert claimed.attempts == 1
    delta = (claimed.next_retry_at - now).total_seconds()
    assert 590 <= delta <= 610, delta


@pytest.mark.asyncio
async def test_claimed_row_is_invisible_until_lease_elapses() -> None:
    dlq = InMemoryMemoryWritebackDLQ()
    await _seed(dlq)
    now = datetime.now(UTC)
    first = await dlq.take_ready(limit=10, now=now)
    assert len(first) == 1

    # A second claimer sweeping right away sees nothing — the row is
    # leased until _CLAIM_LEASE_S elapses.
    still_leased = await dlq.take_ready(limit=10, now=now)
    assert still_leased == []

    # A sweep after the lease elapses reclaims it — attempts monotonic
    # +1 (a crashed-worker recovery is a second attempt), never doubled.
    after_lease = await dlq.take_ready(limit=10, now=now + timedelta(seconds=601))
    assert len(after_lease) == 1
    assert after_lease[0].attempts == 2


@pytest.mark.asyncio
async def test_record_failure_does_not_bump_attempts() -> None:
    dlq = InMemoryMemoryWritebackDLQ()
    row_id = await _seed(dlq)
    now = datetime.now(UTC)
    [claimed] = await dlq.take_ready(limit=10, now=now)
    assert claimed.attempts == 1

    await dlq.record_failure(
        row_id=row_id, error="e", when=now, next_retry_at=now + timedelta(seconds=60)
    )

    [again] = await dlq.take_ready(limit=10, now=now + timedelta(seconds=61))
    assert again.attempts == 2  # bumped by this claim, not by record_failure
    assert again.last_error == "e"
