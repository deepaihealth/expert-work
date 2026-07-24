"""Unit tests for :class:`InMemoryRunEventStore` — Stream H.3 PR 3 (Mini-ADR H-7)."""

from __future__ import annotations

from uuid import uuid4

import pytest

from expert_work.runtime.runs import (
    InMemoryRunEventStore,
    RunEventRecord,
    make_event_record,
)
from expert_work.runtime.runs.event_store import MAX_LIST_LIMIT


@pytest.mark.asyncio
async def test_append_then_list_round_trips() -> None:
    store = InMemoryRunEventStore()
    run_id = uuid4()
    record = make_event_record(
        run_id=run_id, seq=0, event_name="metadata", data={"run_id": str(run_id)}
    )
    await store.append(record)

    listed = await store.list(run_id=run_id)
    assert len(listed) == 1
    assert listed[0].event_name == "metadata"
    assert listed[0].data == {"run_id": str(run_id)}


@pytest.mark.asyncio
async def test_list_returns_only_matching_run() -> None:
    store = InMemoryRunEventStore()
    run_a, run_b = uuid4(), uuid4()
    await store.append(make_event_record(run_id=run_a, seq=0, event_name="metadata", data={}))
    await store.append(make_event_record(run_id=run_b, seq=0, event_name="metadata", data={}))

    listed = await store.list(run_id=run_a)
    assert len(listed) == 1
    assert listed[0].run_id == run_a


@pytest.mark.asyncio
async def test_list_orders_by_seq_ascending() -> None:
    """``replay`` must reproduce the live order, so the store sorts by seq."""
    store = InMemoryRunEventStore()
    run_id = uuid4()
    # Insert out of order to confirm the store re-sorts.
    await store.append(make_event_record(run_id=run_id, seq=2, event_name="updates", data={"i": 2}))
    await store.append(make_event_record(run_id=run_id, seq=0, event_name="metadata", data={}))
    await store.append(make_event_record(run_id=run_id, seq=1, event_name="updates", data={"i": 1}))

    listed = await store.list(run_id=run_id)
    assert [r.seq for r in listed] == [0, 1, 2]


@pytest.mark.asyncio
async def test_list_since_seq_filters_cursor() -> None:
    store = InMemoryRunEventStore()
    run_id = uuid4()
    for i in range(5):
        await store.append(make_event_record(run_id=run_id, seq=i, event_name="updates", data={}))

    # Continue from seq=2 — expect seq 3 + 4.
    listed = await store.list(run_id=run_id, since_seq=2)
    assert [r.seq for r in listed] == [3, 4]


@pytest.mark.asyncio
async def test_list_limit_clamps_to_max() -> None:
    """``MAX_LIST_LIMIT = 500`` — silently clamps oversized requests."""
    store = InMemoryRunEventStore()
    run_id = uuid4()
    for i in range(5):
        await store.append(make_event_record(run_id=run_id, seq=i, event_name="updates", data={}))

    # Ask for 10000 — clamped to MAX_LIST_LIMIT, so we get all 5 rows.
    listed = await store.list(run_id=run_id, limit=10000)
    assert len(listed) == 5
    # MAX_LIST_LIMIT is the public constant the API layer uses to detect
    # clamping; this test pins the value so the API doesn't drift.
    assert MAX_LIST_LIMIT == 500


@pytest.mark.asyncio
async def test_append_duplicate_seq_raises() -> None:
    """The SQL primary key would block a duplicate ``(run_id, seq)``; the
    in-memory store mirrors that invariant so producer bugs surface in
    both backends."""
    store = InMemoryRunEventStore()
    run_id = uuid4()
    await store.append(make_event_record(run_id=run_id, seq=0, event_name="metadata", data={}))
    with pytest.raises(ValueError, match="duplicate seq"):
        await store.append(
            make_event_record(run_id=run_id, seq=0, event_name="metadata", data={"dup": True})
        )


@pytest.mark.asyncio
async def test_make_event_record_derives_created_at_from_ms() -> None:
    record = make_event_record(
        run_id=uuid4(),
        seq=42,
        event_name="updates",
        data={"x": 1},
        created_at_ms=1_700_000_000_000,
    )
    assert isinstance(record, RunEventRecord)
    assert record.created_at_ms == 1_700_000_000_000
    assert int(record.created_at.timestamp() * 1000) == 1_700_000_000_000


@pytest.mark.asyncio
async def test_list_unknown_run_returns_empty() -> None:
    store = InMemoryRunEventStore()
    listed = await store.list(run_id=uuid4())
    assert listed == []


@pytest.mark.asyncio
async def test_next_seq_empty_is_zero() -> None:
    """No events yet → the first free seq is 0 (Stream 9.4 resume seed)."""
    store = InMemoryRunEventStore()
    assert await store.next_seq(run_id=uuid4()) == 0


@pytest.mark.asyncio
async def test_next_seq_continues_past_durable_tail() -> None:
    """``next_seq`` seeds a resumed run past the prior owner's frames so the
    resumed events stay append-only on the ``(run_id, seq)`` key (Stream 9.4)."""
    store = InMemoryRunEventStore()
    run_id = uuid4()
    for seq in range(3):
        await store.append(
            make_event_record(run_id=run_id, seq=seq, event_name="updates", data={"s": seq})
        )
    assert await store.next_seq(run_id=run_id) == 3
    # Appending at the seeded seq must not collide.
    await store.append(make_event_record(run_id=run_id, seq=3, event_name="updates", data={}))
    assert await store.next_seq(run_id=run_id) == 4


@pytest.mark.asyncio
async def test_delete_for_runs_removes_only_targeted_runs() -> None:
    """Deletion-hygiene PR3 §A — purge empties only the targeted runs'
    frames; empty input removes nothing."""
    store = InMemoryRunEventStore()
    run_a, run_b = uuid4(), uuid4()
    await store.append(make_event_record(run_id=run_a, seq=0, event_name="metadata", data={}))
    await store.append(make_event_record(run_id=run_a, seq=1, event_name="updates", data={}))
    await store.append(make_event_record(run_id=run_b, seq=0, event_name="metadata", data={}))

    assert await store.delete_for_runs(run_ids=[run_a]) == 2
    assert list(await store.list(run_id=run_a)) == []
    assert len(await store.list(run_id=run_b)) == 1
    assert await store.delete_for_runs(run_ids=[]) == 0


@pytest.mark.asyncio
async def test_next_seq_pages_beyond_list_limit() -> None:
    """The default paging implementation finds the true max even when the run
    has more frames than ``MAX_LIST_LIMIT`` (single ``list`` call can't)."""
    store = InMemoryRunEventStore()
    run_id = uuid4()
    total = MAX_LIST_LIMIT + 7
    for seq in range(total):
        await store.append(make_event_record(run_id=run_id, seq=seq, event_name="updates", data={}))
    assert await store.next_seq(run_id=run_id) == total
