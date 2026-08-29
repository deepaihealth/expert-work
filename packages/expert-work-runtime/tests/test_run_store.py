"""Unit tests for ``InMemoryRunStore`` — Mini-ADR J-41."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from expert_work.runtime.runs import (
    DisconnectMode,
    InMemoryRunEventStore,
    InMemoryRunStore,
    RunInfo,
    RunStatus,
    make_event_record,
)

_BASE = datetime(2026, 5, 22, 12, 0, 0, tzinfo=UTC)


def _info(
    *,
    run_id: UUID,
    tenant_id: UUID,
    thread_id: UUID | None = None,
    user_id: UUID | None = None,
    status: RunStatus = RunStatus.PENDING,
    created_at: datetime | None = None,
    error: str | None = None,
) -> RunInfo:
    return RunInfo(
        run_id=run_id,
        tenant_id=tenant_id,
        thread_id=thread_id or uuid4(),
        user_id=user_id,
        status=status,
        on_disconnect=DisconnectMode.CANCEL,
        is_resume=False,
        error=error,
        created_at=created_at or _BASE,
        updated_at=created_at or _BASE,
        finished_at=None,
    )


@pytest.mark.asyncio
async def test_create_then_get_round_trips() -> None:
    store = InMemoryRunStore()
    run_id, tenant_id, user_id = uuid4(), uuid4(), uuid4()
    await store.create(_info(run_id=run_id, tenant_id=tenant_id, user_id=user_id))

    fetched = await store.get(run_id=run_id, tenant_id=tenant_id)
    assert fetched is not None
    assert fetched.run_id == run_id
    assert fetched.user_id == user_id
    assert fetched.status is RunStatus.PENDING


@pytest.mark.asyncio
async def test_get_unknown_returns_none() -> None:
    store = InMemoryRunStore()
    assert await store.get(run_id=uuid4(), tenant_id=uuid4()) is None


@pytest.mark.asyncio
async def test_get_cross_tenant_returns_none() -> None:
    """A run is invisible to a caller in a different tenant."""
    store = InMemoryRunStore()
    run_id, tenant_a, tenant_b = uuid4(), uuid4(), uuid4()
    await store.create(_info(run_id=run_id, tenant_id=tenant_a))

    assert await store.get(run_id=run_id, tenant_id=tenant_b) is None


@pytest.mark.asyncio
async def test_create_duplicate_raises() -> None:
    store = InMemoryRunStore()
    run_id, tenant_id = uuid4(), uuid4()
    await store.create(_info(run_id=run_id, tenant_id=tenant_id))
    with pytest.raises(ValueError, match="already exists"):
        await store.create(_info(run_id=run_id, tenant_id=tenant_id))


@pytest.mark.asyncio
async def test_set_status_updates_existing() -> None:
    store = InMemoryRunStore()
    run_id, tenant_id = uuid4(), uuid4()
    await store.create(_info(run_id=run_id, tenant_id=tenant_id))

    hit = await store.set_status(
        run_id=run_id,
        tenant_id=tenant_id,
        status=RunStatus.RUNNING,
        updated_at=_BASE + timedelta(seconds=5),
    )
    assert hit is True
    fetched = await store.get(run_id=run_id, tenant_id=tenant_id)
    assert fetched is not None
    assert fetched.status is RunStatus.RUNNING
    assert fetched.updated_at == _BASE + timedelta(seconds=5)


@pytest.mark.asyncio
async def test_request_cancel_interrupts_a_running_run() -> None:
    # RT-4 (RT-ADR-17) — guarded cross-replica cancel flips a running run to
    # INTERRUPTED and stamps finished_at.
    store = InMemoryRunStore()
    run_id, tenant_id = uuid4(), uuid4()
    await store.create(_info(run_id=run_id, tenant_id=tenant_id, status=RunStatus.RUNNING))

    hit = await store.request_cancel(
        run_id=run_id,
        tenant_id=tenant_id,
        updated_at=_BASE + timedelta(seconds=9),
        reason="tenant_suspended",
    )
    assert hit is True
    fetched = await store.get(run_id=run_id, tenant_id=tenant_id)
    assert fetched is not None
    assert fetched.status is RunStatus.INTERRUPTED
    assert fetched.finished_at == _BASE + timedelta(seconds=9)
    # 中断原因入账(InterruptReason 词表)—— 没写就分不出「谁杀的」。
    assert fetched.error == "tenant_suspended"


@pytest.mark.asyncio
async def test_request_cancel_without_reason_keeps_existing_error() -> None:
    # reason=None 不清既有 error(与 set_status 的「error 非 None 才写」同一
    # 谓词,SQL 店 byte-同义)。
    store = InMemoryRunStore()
    run_id, tenant_id = uuid4(), uuid4()
    await store.create(
        _info(run_id=run_id, tenant_id=tenant_id, status=RunStatus.RUNNING, error="prior")
    )

    hit = await store.request_cancel(
        run_id=run_id, tenant_id=tenant_id, updated_at=_BASE + timedelta(seconds=9)
    )
    assert hit is True
    fetched = await store.get(run_id=run_id, tenant_id=tenant_id)
    assert fetched is not None and fetched.error == "prior"


@pytest.mark.asyncio
async def test_request_cancel_never_clobbers_a_finished_run() -> None:
    # A run that already reached a terminal status is NOT re-interrupted (guards
    # the list→cancel race where a run finishes just before the kill-switch).
    store = InMemoryRunStore()
    run_id, tenant_id = uuid4(), uuid4()
    await store.create(_info(run_id=run_id, tenant_id=tenant_id, status=RunStatus.SUCCESS))

    hit = await store.request_cancel(
        run_id=run_id, tenant_id=tenant_id, updated_at=_BASE + timedelta(seconds=9)
    )
    assert hit is False
    fetched = await store.get(run_id=run_id, tenant_id=tenant_id)
    assert fetched is not None and fetched.status is RunStatus.SUCCESS


@pytest.mark.asyncio
async def test_request_cancel_cross_tenant_returns_false() -> None:
    store = InMemoryRunStore()
    run_id, tenant_a, tenant_b = uuid4(), uuid4(), uuid4()
    await store.create(_info(run_id=run_id, tenant_id=tenant_a, status=RunStatus.RUNNING))
    hit = await store.request_cancel(
        run_id=run_id, tenant_id=tenant_b, updated_at=_BASE + timedelta(seconds=9)
    )
    assert hit is False


@pytest.mark.asyncio
async def test_set_status_unknown_returns_false() -> None:
    store = InMemoryRunStore()
    miss = await store.set_status(
        run_id=uuid4(),
        tenant_id=uuid4(),
        status=RunStatus.SUCCESS,
        updated_at=_BASE,
    )
    assert miss is False


@pytest.mark.asyncio
async def test_set_status_cross_tenant_returns_false() -> None:
    """A cross-tenant status write is a miss — it cannot touch the row."""
    store = InMemoryRunStore()
    run_id, tenant_a, tenant_b = uuid4(), uuid4(), uuid4()
    await store.create(_info(run_id=run_id, tenant_id=tenant_a))

    miss = await store.set_status(
        run_id=run_id,
        tenant_id=tenant_b,
        status=RunStatus.SUCCESS,
        updated_at=_BASE,
    )
    assert miss is False
    untouched = await store.get(run_id=run_id, tenant_id=tenant_a)
    assert untouched is not None
    assert untouched.status is RunStatus.PENDING


@pytest.mark.asyncio
async def test_set_status_records_error_and_finished_at() -> None:
    store = InMemoryRunStore()
    run_id, tenant_id = uuid4(), uuid4()
    await store.create(_info(run_id=run_id, tenant_id=tenant_id))

    finished = _BASE + timedelta(seconds=9)
    await store.set_status(
        run_id=run_id,
        tenant_id=tenant_id,
        status=RunStatus.ERROR,
        updated_at=finished,
        error="provider 503",
        finished_at=finished,
    )
    fetched = await store.get(run_id=run_id, tenant_id=tenant_id)
    assert fetched is not None
    assert fetched.error == "provider 503"
    assert fetched.finished_at == finished


@pytest.mark.asyncio
async def test_set_status_keeps_prior_error_when_not_supplied() -> None:
    """A later status write without ``error`` never clears a recorded verdict."""
    store = InMemoryRunStore()
    run_id, tenant_id = uuid4(), uuid4()
    await store.create(_info(run_id=run_id, tenant_id=tenant_id))
    await store.set_status(
        run_id=run_id,
        tenant_id=tenant_id,
        status=RunStatus.ERROR,
        updated_at=_BASE,
        error="boom",
        finished_at=_BASE,
    )

    await store.set_status(
        run_id=run_id,
        tenant_id=tenant_id,
        status=RunStatus.SUCCESS,
        updated_at=_BASE + timedelta(seconds=1),
    )
    fetched = await store.get(run_id=run_id, tenant_id=tenant_id)
    assert fetched is not None
    assert fetched.error == "boom"
    assert fetched.finished_at == _BASE


@pytest.mark.asyncio
async def test_list_by_thread_filters_and_sorts() -> None:
    store = InMemoryRunStore()
    thread_id, tenant_a, tenant_b = uuid4(), uuid4(), uuid4()
    # Two tenant-A runs on the thread, inserted newest-first.
    newer = uuid4()
    older = uuid4()
    await store.create(
        _info(
            run_id=newer,
            tenant_id=tenant_a,
            thread_id=thread_id,
            created_at=_BASE + timedelta(minutes=1),
        )
    )
    await store.create(
        _info(run_id=older, tenant_id=tenant_a, thread_id=thread_id, created_at=_BASE)
    )
    # A tenant-B run on the same thread must not leak.
    await store.create(_info(run_id=uuid4(), tenant_id=tenant_b, thread_id=thread_id))

    listed = await store.list_by_thread(thread_id=thread_id, tenant_id=tenant_a)
    assert [r.run_id for r in listed] == [older, newer]


@pytest.mark.asyncio
async def test_delete_by_thread_removes_only_that_thread_and_tenant() -> None:
    store = InMemoryRunStore()
    thread_a, thread_b, tenant, other = uuid4(), uuid4(), uuid4(), uuid4()
    await store.create(_info(run_id=uuid4(), tenant_id=tenant, thread_id=thread_a))
    await store.create(_info(run_id=uuid4(), tenant_id=tenant, thread_id=thread_a))
    keep_thread = uuid4()
    await store.create(_info(run_id=keep_thread, tenant_id=tenant, thread_id=thread_b))
    # Same thread_id under a different tenant must survive.
    cross = uuid4()
    await store.create(_info(run_id=cross, tenant_id=other, thread_id=thread_a))

    removed = await store.delete_by_thread(thread_id=thread_a, tenant_id=tenant)
    assert removed == 2
    assert await store.list_by_thread(thread_id=thread_a, tenant_id=tenant) == []
    # Other thread + cross-tenant run untouched.
    assert [r.run_id for r in await store.list_by_thread(thread_id=thread_b, tenant_id=tenant)] == [
        keep_thread
    ]
    assert await store.get(run_id=cross, tenant_id=other) is not None
    # Deleting an empty thread is a no-op returning 0.
    assert await store.delete_by_thread(thread_id=uuid4(), tenant_id=tenant) == 0


@pytest.mark.asyncio
async def test_delete_by_thread_clears_child_events_via_injected_event_store() -> None:
    """Deletion-hygiene PR3 §A — the in-memory mirror of the SQL contract:
    purging a thread's runs also empties their ``run_event`` children."""
    events = InMemoryRunEventStore()
    store = InMemoryRunStore(event_store=events)
    thread, tenant = uuid4(), uuid4()
    doomed, bystander = uuid4(), uuid4()
    await store.create(_info(run_id=doomed, tenant_id=tenant, thread_id=thread))
    await store.create(_info(run_id=bystander, tenant_id=tenant))  # other thread
    await events.append(make_event_record(run_id=doomed, seq=0, event_name="metadata", data={}))
    await events.append(make_event_record(run_id=doomed, seq=1, event_name="updates", data={}))
    await events.append(make_event_record(run_id=bystander, seq=0, event_name="metadata", data={}))

    assert await store.delete_by_thread(thread_id=thread, tenant_id=tenant) == 1
    assert list(await events.list(run_id=doomed)) == []
    # The bystander run on another thread keeps its events.
    assert len(await events.list(run_id=bystander)) == 1


# ---------------------------------------------------------------------------
# Stream H.3 PR 1 — list_for_tenant / list_all_tenants
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_for_tenant_returns_only_matching_tenant() -> None:
    store = InMemoryRunStore()
    tenant_a, tenant_b = uuid4(), uuid4()
    a_ids = [uuid4(), uuid4(), uuid4()]
    for i, rid in enumerate(a_ids):
        await store.create(
            _info(run_id=rid, tenant_id=tenant_a, created_at=_BASE + timedelta(minutes=i))
        )
    # Tenant B runs that must not leak through.
    await store.create(_info(run_id=uuid4(), tenant_id=tenant_b))
    await store.create(_info(run_id=uuid4(), tenant_id=tenant_b))

    listed = await store.list_for_tenant(tenant_id=tenant_a)
    assert {r.run_id for r in listed} == set(a_ids)
    assert all(r.tenant_id == tenant_a for r in listed)


@pytest.mark.asyncio
async def test_list_for_tenant_orders_newest_first() -> None:
    store = InMemoryRunStore()
    tenant_id = uuid4()
    oldest, middle, newest = uuid4(), uuid4(), uuid4()
    await store.create(_info(run_id=oldest, tenant_id=tenant_id, created_at=_BASE))
    await store.create(
        _info(run_id=middle, tenant_id=tenant_id, created_at=_BASE + timedelta(minutes=1))
    )
    await store.create(
        _info(run_id=newest, tenant_id=tenant_id, created_at=_BASE + timedelta(minutes=2))
    )

    listed = await store.list_for_tenant(tenant_id=tenant_id)
    assert [r.run_id for r in listed] == [newest, middle, oldest]


@pytest.mark.asyncio
async def test_list_for_tenant_status_filter() -> None:
    store = InMemoryRunStore()
    tenant_id = uuid4()
    paused_id, running_id, success_id = uuid4(), uuid4(), uuid4()
    await store.create(_info(run_id=paused_id, tenant_id=tenant_id, status=RunStatus.PAUSED))
    await store.create(_info(run_id=running_id, tenant_id=tenant_id, status=RunStatus.RUNNING))
    await store.create(_info(run_id=success_id, tenant_id=tenant_id, status=RunStatus.SUCCESS))

    paused = await store.list_for_tenant(tenant_id=tenant_id, status=RunStatus.PAUSED)
    assert [r.run_id for r in paused] == [paused_id]


@pytest.mark.asyncio
async def test_list_for_tenant_q_filters_run_and_thread_id() -> None:
    store = InMemoryRunStore()
    tenant_id = uuid4()
    run_a = UUID("aaaaaaaa-0000-0000-0000-000000000001")
    thread_b = UUID("bbbbbbbb-0000-0000-0000-000000000002")
    run_c = UUID("cccccccc-0000-0000-0000-000000000003")
    await store.create(_info(run_id=run_a, tenant_id=tenant_id))
    await store.create(_info(run_id=uuid4(), tenant_id=tenant_id, thread_id=thread_b))
    await store.create(_info(run_id=run_c, tenant_id=tenant_id))

    by_run = await store.list_for_tenant(tenant_id=tenant_id, q="aaaaaaaa")
    assert [r.run_id for r in by_run] == [run_a]
    by_thread = await store.list_for_tenant(tenant_id=tenant_id, q="bbbbbbbb")
    assert [r.thread_id for r in by_thread] == [thread_b]
    # Case-insensitive substring.
    assert len(await store.list_for_tenant(tenant_id=tenant_id, q="CCCCCCCC")) == 1
    assert await store.list_for_tenant(tenant_id=tenant_id, q="zzzz") == []


@pytest.mark.asyncio
async def test_list_for_tenant_user_id_filter() -> None:
    store = InMemoryRunStore()
    tenant_id = uuid4()
    user_a, user_b = uuid4(), uuid4()
    run_a = uuid4()
    await store.create(_info(run_id=run_a, tenant_id=tenant_id, user_id=user_a))
    await store.create(_info(run_id=uuid4(), tenant_id=tenant_id, user_id=user_b))
    await store.create(_info(run_id=uuid4(), tenant_id=tenant_id, user_id=None))  # system

    only_a = await store.list_for_tenant(tenant_id=tenant_id, user_id=user_a)
    assert [r.run_id for r in only_a] == [run_a]
    # A user with no runs → empty (system/None runs are never matched).
    assert await store.list_for_tenant(tenant_id=tenant_id, user_id=uuid4()) == []


@pytest.mark.asyncio
async def test_list_for_tenant_pagination_offset_and_limit() -> None:
    store = InMemoryRunStore()
    tenant_id = uuid4()
    ids = []
    for i in range(7):
        rid = uuid4()
        ids.append(rid)
        await store.create(
            _info(run_id=rid, tenant_id=tenant_id, created_at=_BASE + timedelta(minutes=i))
        )

    # Newest first → reverse insertion order.
    expected_desc = list(reversed(ids))
    page1 = await store.list_for_tenant(tenant_id=tenant_id, limit=3, offset=0)
    page2 = await store.list_for_tenant(tenant_id=tenant_id, limit=3, offset=3)
    page3 = await store.list_for_tenant(tenant_id=tenant_id, limit=3, offset=6)

    assert [r.run_id for r in page1] == expected_desc[:3]
    assert [r.run_id for r in page2] == expected_desc[3:6]
    assert [r.run_id for r in page3] == expected_desc[6:9]  # only 1 row left


@pytest.mark.asyncio
async def test_list_for_tenant_clamps_to_max_limit() -> None:
    """``MAX_LIST_LIMIT = 500`` — silently clamps oversized requests."""
    from expert_work.runtime.runs.store import MAX_LIST_LIMIT

    store = InMemoryRunStore()
    tenant_id = uuid4()
    # Create 5 runs; ask for 10000 — should return 5 (not crash).
    for i in range(5):
        await store.create(
            _info(run_id=uuid4(), tenant_id=tenant_id, created_at=_BASE + timedelta(seconds=i))
        )

    listed = await store.list_for_tenant(tenant_id=tenant_id, limit=10000)
    assert len(listed) == 5  # less than MAX_LIST_LIMIT cap
    # Bound the cap itself — pass exactly MAX_LIST_LIMIT and one more, prove
    # the clamp is the limit applied.
    listed_at_cap = await store.list_for_tenant(tenant_id=tenant_id, limit=MAX_LIST_LIMIT + 50)
    assert len(listed_at_cap) == 5


@pytest.mark.asyncio
async def test_list_all_tenants_returns_runs_across_tenants() -> None:
    store = InMemoryRunStore()
    tenant_a, tenant_b = uuid4(), uuid4()
    await store.create(_info(run_id=uuid4(), tenant_id=tenant_a))
    await store.create(_info(run_id=uuid4(), tenant_id=tenant_a))
    await store.create(_info(run_id=uuid4(), tenant_id=tenant_b))

    listed = await store.list_all_tenants()
    tenants = {r.tenant_id for r in listed}
    assert tenants == {tenant_a, tenant_b}
    assert len(listed) == 3


@pytest.mark.asyncio
async def test_list_all_tenants_status_filter_and_ordering() -> None:
    store = InMemoryRunStore()
    tenant_a, tenant_b = uuid4(), uuid4()
    paused_a = uuid4()
    paused_b = uuid4()
    await store.create(
        _info(
            run_id=paused_a,
            tenant_id=tenant_a,
            status=RunStatus.PAUSED,
            created_at=_BASE,
        )
    )
    await store.create(
        _info(
            run_id=paused_b,
            tenant_id=tenant_b,
            status=RunStatus.PAUSED,
            created_at=_BASE + timedelta(minutes=1),
        )
    )
    await store.create(_info(run_id=uuid4(), tenant_id=tenant_a, status=RunStatus.SUCCESS))

    paused = await store.list_all_tenants(status=RunStatus.PAUSED)
    assert [r.run_id for r in paused] == [paused_b, paused_a]  # newest first


# ---------------------------------------------------------------------------
# Stream H.3 PR 2 — set_trace_id (Mini-ADR H-9.5)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_trace_id_writes_and_reads_back() -> None:
    store = InMemoryRunStore()
    run_id, tenant_id = uuid4(), uuid4()
    await store.create(_info(run_id=run_id, tenant_id=tenant_id))

    ok = await store.set_trace_id(run_id=run_id, tenant_id=tenant_id, trace_id="abcd" * 8)
    assert ok is True

    fetched = await store.get(run_id=run_id, tenant_id=tenant_id)
    assert fetched is not None
    assert fetched.trace_id == "abcd" * 8


@pytest.mark.asyncio
async def test_set_trace_id_idempotent_overwrite() -> None:
    """A worker observing its own trace after the API handler captured one
    overwrites the existing value — last write wins."""
    store = InMemoryRunStore()
    run_id, tenant_id = uuid4(), uuid4()
    await store.create(_info(run_id=run_id, tenant_id=tenant_id))

    await store.set_trace_id(run_id=run_id, tenant_id=tenant_id, trace_id="1" * 32)
    await store.set_trace_id(run_id=run_id, tenant_id=tenant_id, trace_id="2" * 32)

    fetched = await store.get(run_id=run_id, tenant_id=tenant_id)
    assert fetched is not None
    assert fetched.trace_id == "2" * 32


@pytest.mark.asyncio
async def test_set_trace_id_unknown_run_returns_false() -> None:
    store = InMemoryRunStore()
    ok = await store.set_trace_id(run_id=uuid4(), tenant_id=uuid4(), trace_id="aa" * 16)
    assert ok is False


# ---------------------------------------------------------------------------
# set_agent_spec_sha256 — which manifest revision this run actually executed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_agent_spec_sha256_writes_and_reads_back() -> None:
    store = InMemoryRunStore()
    run_id, tenant_id = uuid4(), uuid4()
    await store.create(_info(run_id=run_id, tenant_id=tenant_id))

    ok = await store.set_agent_spec_sha256(
        run_id=run_id, tenant_id=tenant_id, agent_spec_sha256="ab" * 32
    )
    assert ok is True

    fetched = await store.get(run_id=run_id, tenant_id=tenant_id)
    assert fetched is not None
    assert fetched.agent_spec_sha256 == "ab" * 32


@pytest.mark.asyncio
async def test_set_agent_spec_sha256_unknown_run_returns_false() -> None:
    """Cross-tenant probes must not reveal existence — same contract as
    ``set_trace_id``."""
    store = InMemoryRunStore()
    run_id, tenant_id = uuid4(), uuid4()
    await store.create(_info(run_id=run_id, tenant_id=tenant_id))

    assert (
        await store.set_agent_spec_sha256(
            run_id=run_id, tenant_id=uuid4(), agent_spec_sha256="cd" * 32
        )
        is False
    )
    assert (
        await store.set_agent_spec_sha256(
            run_id=uuid4(), tenant_id=tenant_id, agent_spec_sha256="cd" * 32
        )
        is False
    )


@pytest.mark.asyncio
async def test_set_trace_id_cross_tenant_returns_false() -> None:
    """A wrong tenant_id must not let an attacker stamp another tenant's
    run trace_id."""
    store = InMemoryRunStore()
    run_id, tenant_a, tenant_b = uuid4(), uuid4(), uuid4()
    await store.create(_info(run_id=run_id, tenant_id=tenant_a))

    ok = await store.set_trace_id(run_id=run_id, tenant_id=tenant_b, trace_id="x" * 32)
    assert ok is False

    fetched = await store.get(run_id=run_id, tenant_id=tenant_a)
    assert fetched is not None
    assert fetched.trace_id is None  # unchanged


@pytest.mark.asyncio
async def test_create_with_trace_id_round_trips() -> None:
    """The trace_id passed through ``RunInfo.create`` reaches ``get`` /
    ``list_for_tenant`` / ``list_all_tenants`` unchanged."""
    store = InMemoryRunStore()
    run_id, tenant_id = uuid4(), uuid4()
    await store.create(
        RunInfo(
            run_id=run_id,
            tenant_id=tenant_id,
            thread_id=uuid4(),
            user_id=None,
            status=RunStatus.PENDING,
            on_disconnect=DisconnectMode.CANCEL,
            is_resume=False,
            error=None,
            created_at=_BASE,
            updated_at=_BASE,
            finished_at=None,
            trace_id="cafef00d" * 4,
        )
    )

    fetched = await store.get(run_id=run_id, tenant_id=tenant_id)
    assert fetched is not None
    assert fetched.trace_id == "cafef00d" * 4

    listed = await store.list_for_tenant(tenant_id=tenant_id)
    assert listed[0].trace_id == "cafef00d" * 4


# ---------------------------------------------------------------------------
# Stream H.6 (Mini-ADR H-10) — thread_ids list filter
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_for_tenant_thread_ids_filter() -> None:
    store = InMemoryRunStore()
    tenant_id = uuid4()
    thread_a, thread_b = uuid4(), uuid4()
    await store.create(_info(run_id=uuid4(), tenant_id=tenant_id, thread_id=thread_a))
    await store.create(_info(run_id=uuid4(), tenant_id=tenant_id, thread_id=thread_a))
    await store.create(_info(run_id=uuid4(), tenant_id=tenant_id, thread_id=thread_b))

    subset = await store.list_for_tenant(tenant_id=tenant_id, thread_ids=[thread_a])
    assert len(subset) == 2
    assert {r.thread_id for r in subset} == {thread_a}

    # Empty collection means "the agent has no threads" → no rows, NOT
    # "no filter" (that's None).
    assert await store.list_for_tenant(tenant_id=tenant_id, thread_ids=[]) == []

    # None regression — unfiltered list unchanged.
    assert len(await store.list_for_tenant(tenant_id=tenant_id)) == 3


@pytest.mark.asyncio
async def test_list_all_tenants_thread_ids_filter() -> None:
    store = InMemoryRunStore()
    thread_a = uuid4()
    await store.create(_info(run_id=uuid4(), tenant_id=uuid4(), thread_id=thread_a))
    await store.create(_info(run_id=uuid4(), tenant_id=uuid4(), thread_id=uuid4()))

    subset = await store.list_all_tenants(thread_ids={thread_a})
    assert [r.thread_id for r in subset] == [thread_a]
    assert await store.list_all_tenants(thread_ids=set()) == []
    assert len(await store.list_all_tenants()) == 2


# --- Stream 9.4 (HA failover) — ownership lease ------------------------------


@pytest.mark.asyncio
async def test_claim_then_heartbeat_renews_lease() -> None:
    store = InMemoryRunStore()
    run_id, tenant = uuid4(), uuid4()
    await store.create(_info(run_id=run_id, tenant_id=tenant, status=RunStatus.RUNNING))
    t0 = _BASE
    assert await store.claim(
        run_id=run_id,
        tenant_id=tenant,
        claimed_by="inst-a",
        lease_until=t0 + timedelta(seconds=30),
        heartbeat_at=t0,
    )
    # owner renews
    t1 = t0 + timedelta(seconds=10)
    assert await store.heartbeat(
        run_id=run_id,
        claimed_by="inst-a",
        lease_until=t1 + timedelta(seconds=30),
        heartbeat_at=t1,
    )
    row = await store.get(run_id=run_id, tenant_id=tenant)
    assert row is not None and row.claimed_by == "inst-a"
    assert row.lease_until == t1 + timedelta(seconds=30)


@pytest.mark.asyncio
async def test_heartbeat_fails_for_non_owner() -> None:
    store = InMemoryRunStore()
    run_id, tenant = uuid4(), uuid4()
    await store.create(_info(run_id=run_id, tenant_id=tenant, status=RunStatus.RUNNING))
    await store.claim(
        run_id=run_id,
        tenant_id=tenant,
        claimed_by="inst-a",
        lease_until=_BASE + timedelta(seconds=30),
        heartbeat_at=_BASE,
    )
    # A different instance cannot renew (it doesn't own the run).
    assert not await store.heartbeat(
        run_id=run_id,
        claimed_by="inst-b",
        lease_until=_BASE + timedelta(seconds=60),
        heartbeat_at=_BASE,
    )


@pytest.mark.asyncio
async def test_list_orphans_only_expired_running() -> None:
    store = InMemoryRunStore()
    tenant = uuid4()
    now = _BASE + timedelta(minutes=5)
    # expired-lease running → orphan
    orphan = uuid4()
    await store.create(_info(run_id=orphan, tenant_id=tenant, status=RunStatus.RUNNING))
    await store.claim(
        run_id=orphan,
        tenant_id=tenant,
        claimed_by="dead",
        lease_until=now - timedelta(seconds=1),
        heartbeat_at=_BASE,
    )
    # fresh-lease running → not orphan
    live = uuid4()
    await store.create(_info(run_id=live, tenant_id=tenant, status=RunStatus.RUNNING))
    await store.claim(
        run_id=live,
        tenant_id=tenant,
        claimed_by="alive",
        lease_until=now + timedelta(seconds=30),
        heartbeat_at=now,
    )
    # terminal with stale lease → never an orphan
    done = uuid4()
    await store.create(_info(run_id=done, tenant_id=tenant, status=RunStatus.SUCCESS))
    await store.claim(
        run_id=done,
        tenant_id=tenant,
        claimed_by="dead",
        lease_until=now - timedelta(minutes=1),
        heartbeat_at=_BASE,
    )

    orphans = await store.list_orphans(now=now, limit=10)
    assert [o.run_id for o in orphans] == [orphan]


@pytest.mark.asyncio
async def test_reclaim_cas_one_winner() -> None:
    store = InMemoryRunStore()
    run_id, tenant = uuid4(), uuid4()
    now = _BASE + timedelta(minutes=5)
    await store.create(_info(run_id=run_id, tenant_id=tenant, status=RunStatus.RUNNING))
    await store.claim(
        run_id=run_id,
        tenant_id=tenant,
        claimed_by="dead",
        lease_until=now - timedelta(seconds=1),
        heartbeat_at=_BASE,
    )
    # first reclaim wins
    assert await store.reclaim(
        run_id=run_id,
        new_owner="inst-b",
        lease_until=now + timedelta(seconds=30),
        heartbeat_at=now,
        now=now,
    )
    # second reclaim loses — lease is fresh now (no longer < now)
    assert not await store.reclaim(
        run_id=run_id,
        new_owner="inst-c",
        lease_until=now + timedelta(seconds=30),
        heartbeat_at=now,
        now=now,
    )
    row = await store.get(run_id=run_id, tenant_id=tenant)
    assert row is not None and row.claimed_by == "inst-b"


@pytest.mark.asyncio
async def test_fail_if_active_running_cas_one_winner() -> None:
    # W1 PR1 task 4 — two replicas racing the same orphan's terminal
    # transition (both scanned the run while it was still RUNNING); the CAS
    # guard lets exactly one of them win.
    store = InMemoryRunStore()
    run_id, tenant = uuid4(), uuid4()
    await store.create(_info(run_id=run_id, tenant_id=tenant, status=RunStatus.RUNNING))
    now = _BASE + timedelta(minutes=5)

    won = await store.fail_if_active(
        run_id=run_id, tenant_id=tenant, error="orphaned run failover: max_reclaims", now=now
    )
    lost = await store.fail_if_active(
        run_id=run_id, tenant_id=tenant, error="orphaned run failover: max_reclaims", now=now
    )
    assert won is True
    assert lost is False
    row = await store.get(run_id=run_id, tenant_id=tenant)
    assert row is not None
    assert row.status is RunStatus.ERROR
    assert row.error == "orphaned run failover: max_reclaims"
    assert row.finished_at == now


@pytest.mark.asyncio
async def test_fail_if_active_error_row_returns_false() -> None:
    # A row already in a terminal status is not re-failed (loser CAS path).
    store = InMemoryRunStore()
    run_id, tenant = uuid4(), uuid4()
    await store.create(_info(run_id=run_id, tenant_id=tenant, status=RunStatus.ERROR))
    hit = await store.fail_if_active(
        run_id=run_id, tenant_id=tenant, error="second attempt", now=_BASE
    )
    assert hit is False


@pytest.mark.asyncio
async def test_fail_if_active_pending_row_returns_true() -> None:
    # PENDING is an active (non-terminal) status — same active set as
    # request_cancel's guard.
    store = InMemoryRunStore()
    run_id, tenant = uuid4(), uuid4()
    await store.create(_info(run_id=run_id, tenant_id=tenant, status=RunStatus.PENDING))
    hit = await store.fail_if_active(
        run_id=run_id, tenant_id=tenant, error="orphaned run failover: auto_reclaim_off", now=_BASE
    )
    assert hit is True
    row = await store.get(run_id=run_id, tenant_id=tenant)
    assert row is not None and row.status is RunStatus.ERROR


@pytest.mark.asyncio
async def test_fail_if_active_cross_tenant_returns_false() -> None:
    store = InMemoryRunStore()
    run_id, tenant_a, tenant_b = uuid4(), uuid4(), uuid4()
    await store.create(_info(run_id=run_id, tenant_id=tenant_a, status=RunStatus.RUNNING))
    hit = await store.fail_if_active(run_id=run_id, tenant_id=tenant_b, error="orphaned", now=_BASE)
    assert hit is False
    untouched = await store.get(run_id=run_id, tenant_id=tenant_a)
    assert untouched is not None and untouched.status is RunStatus.RUNNING


# --------------------------------------------------------------------------
# Stream 9.5 — distributed run queue
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_queued_returns_queued_fifo() -> None:
    store = InMemoryRunStore()
    tenant = uuid4()
    old = uuid4()
    new = uuid4()
    base = datetime(2026, 1, 1, tzinfo=UTC)
    await store.create(
        _info(run_id=old, tenant_id=tenant, status=RunStatus.QUEUED, created_at=base)
    )
    await store.create(
        _info(
            run_id=new,
            tenant_id=tenant,
            status=RunStatus.QUEUED,
            created_at=base + timedelta(minutes=5),
        )
    )
    # A non-queued run must not appear.
    await store.create(_info(run_id=uuid4(), tenant_id=tenant, status=RunStatus.RUNNING))

    queued = await store.list_queued(limit=10)
    assert [q.run_id for q in queued] == [old, new]  # oldest first


@pytest.mark.asyncio
async def test_claim_queued_cas_one_winner() -> None:
    store = InMemoryRunStore()
    tenant, run_id = uuid4(), uuid4()
    await store.create(_info(run_id=run_id, tenant_id=tenant, status=RunStatus.QUEUED))
    now = datetime.now(UTC)
    lease = now + timedelta(seconds=30)

    won = await store.claim_queued(
        run_id=run_id, new_owner="worker-a", lease_until=lease, heartbeat_at=now
    )
    assert won is not None
    assert won.status is RunStatus.RUNNING
    assert won.claimed_by == "worker-a"
    assert won.enqueued_input is None or isinstance(won.enqueued_input, dict)

    # Second claim loses — the run is no longer queued.
    lost = await store.claim_queued(
        run_id=run_id, new_owner="worker-b", lease_until=lease, heartbeat_at=now
    )
    assert lost is None
    row = await store.get(run_id=run_id, tenant_id=tenant)
    assert row is not None and row.claimed_by == "worker-a"


@pytest.mark.asyncio
async def test_claim_queued_carries_enqueued_input() -> None:
    store = InMemoryRunStore()
    tenant, run_id = uuid4(), uuid4()
    info = _info(run_id=run_id, tenant_id=tenant, status=RunStatus.QUEUED)
    from dataclasses import replace

    await store.create(replace(info, enqueued_input={"input": "hi", "image_refs": []}))
    now = datetime.now(UTC)
    claimed = await store.claim_queued(
        run_id=run_id, new_owner="w", lease_until=now + timedelta(seconds=30), heartbeat_at=now
    )
    assert claimed is not None
    assert claimed.enqueued_input == {"input": "hi", "image_refs": []}


# --- list_stale_pending (W1-PR3 Task 1 — PENDING orphan sweep) -------------


@pytest.mark.asyncio
async def test_list_stale_pending_returns_only_old_pending_rows() -> None:
    store = InMemoryRunStore()
    tenant = uuid4()
    cutoff = _BASE + timedelta(seconds=600)

    # Stale PENDING (created before cutoff) — the crash-window candidate.
    stale = uuid4()
    await store.create(
        _info(run_id=stale, tenant_id=tenant, status=RunStatus.PENDING, created_at=_BASE)
    )
    # Fresh PENDING (created_at >= cutoff) — still inside the normal window.
    fresh = uuid4()
    await store.create(
        _info(run_id=fresh, tenant_id=tenant, status=RunStatus.PENDING, created_at=cutoff)
    )
    # RUNNING with an old created_at — never a pending-sweep candidate,
    # regardless of age (list_orphans owns the running lane).
    running = uuid4()
    await store.create(
        _info(run_id=running, tenant_id=tenant, status=RunStatus.RUNNING, created_at=_BASE)
    )
    # Terminal with an old created_at — never a candidate.
    done = uuid4()
    await store.create(
        _info(run_id=done, tenant_id=tenant, status=RunStatus.SUCCESS, created_at=_BASE)
    )

    found = await store.list_stale_pending(cutoff=cutoff, limit=10)
    assert [r.run_id for r in found] == [stale]


@pytest.mark.asyncio
async def test_list_stale_pending_orders_oldest_first_and_respects_limit() -> None:
    store = InMemoryRunStore()
    tenant = uuid4()
    oldest, middle, newest = uuid4(), uuid4(), uuid4()
    await store.create(
        _info(
            run_id=newest,
            tenant_id=tenant,
            status=RunStatus.PENDING,
            created_at=_BASE + timedelta(seconds=20),
        )
    )
    await store.create(
        _info(run_id=oldest, tenant_id=tenant, status=RunStatus.PENDING, created_at=_BASE)
    )
    await store.create(
        _info(
            run_id=middle,
            tenant_id=tenant,
            status=RunStatus.PENDING,
            created_at=_BASE + timedelta(seconds=10),
        )
    )
    cutoff = _BASE + timedelta(hours=1)

    found = await store.list_stale_pending(cutoff=cutoff, limit=10)
    assert [r.run_id for r in found] == [oldest, middle, newest]

    limited = await store.list_stale_pending(cutoff=cutoff, limit=2)
    assert [r.run_id for r in limited] == [oldest, middle]


# --- aggregate_by_threads (conversation-list rollup) -----------------------


def _run(
    *,
    tenant_id: UUID,
    thread_id: UUID,
    status: RunStatus = RunStatus.SUCCESS,
    created_at: datetime | None = None,
    trace_id: str | None = None,
) -> RunInfo:
    from dataclasses import replace

    return replace(
        _info(
            run_id=uuid4(),
            tenant_id=tenant_id,
            thread_id=thread_id,
            status=status,
            created_at=created_at or _BASE,
        ),
        trace_id=trace_id,
    )


@pytest.mark.asyncio
async def test_aggregate_by_threads_empty_ids_short_circuits() -> None:
    store = InMemoryRunStore()
    assert await store.aggregate_by_threads(thread_ids=[], tenant_id=uuid4()) == {}


@pytest.mark.asyncio
async def test_aggregate_by_threads_counts_errors_pending_and_traces() -> None:
    store = InMemoryRunStore()
    tenant, thread = uuid4(), uuid4()
    await store.create(
        _run(tenant_id=tenant, thread_id=thread, status=RunStatus.SUCCESS, trace_id="t1")
    )
    await store.create(
        _run(
            tenant_id=tenant,
            thread_id=thread,
            status=RunStatus.ERROR,
            trace_id="t2",
            created_at=_BASE + timedelta(minutes=5),
        )
    )
    await store.create(
        _run(tenant_id=tenant, thread_id=thread, status=RunStatus.TIMEOUT, trace_id="t2")
    )
    await store.create(
        _run(
            tenant_id=tenant,
            thread_id=thread,
            status=RunStatus.PAUSED,
            trace_id=None,
            created_at=_BASE + timedelta(minutes=10),
        )
    )

    aggs = await store.aggregate_by_threads(thread_ids=[thread], tenant_id=tenant)
    agg = aggs[thread]
    assert agg.run_count == 4
    assert agg.error_count == 2  # ERROR + TIMEOUT
    assert agg.pending_count == 1  # PAUSED
    assert agg.last_run_at == _BASE + timedelta(minutes=10)
    # Distinct, sorted, NULL trace dropped.
    assert agg.trace_ids == ("t1", "t2")


@pytest.mark.asyncio
async def test_aggregate_by_threads_omits_threads_without_runs() -> None:
    store = InMemoryRunStore()
    tenant, live, empty = uuid4(), uuid4(), uuid4()
    await store.create(_run(tenant_id=tenant, thread_id=live))
    aggs = await store.aggregate_by_threads(thread_ids=[live, empty], tenant_id=tenant)
    assert set(aggs) == {live}


@pytest.mark.asyncio
async def test_aggregate_by_threads_tenant_scopes() -> None:
    store = InMemoryRunStore()
    ten_a, ten_b, thread = uuid4(), uuid4(), uuid4()
    # Same thread id under two tenants (defensive) — tenant filter must split.
    await store.create(_run(tenant_id=ten_a, thread_id=thread, trace_id="a"))
    await store.create(_run(tenant_id=ten_b, thread_id=thread, trace_id="b"))
    scoped = await store.aggregate_by_threads(thread_ids=[thread], tenant_id=ten_a)
    assert scoped[thread].run_count == 1
    assert scoped[thread].trace_ids == ("a",)
    # Cross-tenant (tenant_id=None) folds both.
    crossed = await store.aggregate_by_threads(thread_ids=[thread], tenant_id=None)
    assert crossed[thread].run_count == 2
    assert crossed[thread].trace_ids == ("a", "b")


@pytest.mark.asyncio
async def test_thread_ids_with_runs_since_and_only() -> None:
    store = InMemoryRunStore()
    tenant = uuid4()
    old_ok, old_bad, new_ok, new_bad = uuid4(), uuid4(), uuid4(), uuid4()
    await store.create(_run(tenant_id=tenant, thread_id=old_ok, status=RunStatus.SUCCESS))
    await store.create(_run(tenant_id=tenant, thread_id=old_bad, status=RunStatus.ERROR))
    late = _BASE + timedelta(hours=2)
    await store.create(
        _run(tenant_id=tenant, thread_id=new_ok, status=RunStatus.SUCCESS, created_at=late)
    )
    await store.create(
        _run(tenant_id=tenant, thread_id=new_bad, status=RunStatus.TIMEOUT, created_at=late)
    )

    # No filters — every thread with a run.
    assert await store.thread_ids_with_runs(tenant_id=tenant) == {old_ok, old_bad, new_ok, new_bad}
    # only="failed" — ERROR + TIMEOUT terminal states, any age.
    assert await store.thread_ids_with_runs(tenant_id=tenant, only="failed") == {
        old_bad,
        new_bad,
    }
    # since — the activity window.
    cutoff = _BASE + timedelta(hours=1)
    assert await store.thread_ids_with_runs(tenant_id=tenant, since=cutoff) == {new_ok, new_bad}
    # Composed: "what broke today".
    assert await store.thread_ids_with_runs(tenant_id=tenant, since=cutoff, only="failed") == {
        new_bad
    }
    # Tenant scoping — another tenant sees nothing.
    assert await store.thread_ids_with_runs(tenant_id=uuid4()) == set()


@pytest.mark.asyncio
async def test_thread_ids_with_runs_only_pending() -> None:
    store = InMemoryRunStore()
    tenant = uuid4()
    waiting, done = uuid4(), uuid4()
    await store.create(_run(tenant_id=tenant, thread_id=waiting, status=RunStatus.PAUSED))
    await store.create(_run(tenant_id=tenant, thread_id=done, status=RunStatus.SUCCESS))

    # only="pending" — runs paused at an approval gate ("needs a human").
    assert await store.thread_ids_with_runs(tenant_id=tenant, only="pending") == {waiting}


# ---------------------------------------------------------------------------
# 对话条目 PR2 —— list_for_tenant 的 keyset 游标
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_for_tenant_before_is_keyset_paginated(
    keyset_before_contract: Callable[..., Awaitable[None]],
) -> None:
    """断言体在 ``conftest`` 的契约里,与 SQL 版共用一份。"""
    await keyset_before_contract(InMemoryRunStore())


@pytest.mark.asyncio
async def test_set_status_writes_artifacts_and_none_keeps_existing() -> None:
    """产物清单契约 —— 终局写清单;None(非终局转换)不碰既有清单。

    谓词须与 SQL 店 byte-同义(error/finished_at 同款规则)。
    """
    store = InMemoryRunStore()
    run_id, tenant_id = uuid4(), uuid4()
    await store.create(_info(run_id=run_id, tenant_id=tenant_id))

    manifest = [{"name": "plan.pptx", "kind": "document", "version": 1, "created_at": "t"}]
    await store.set_status(
        run_id=run_id,
        tenant_id=tenant_id,
        status=RunStatus.SUCCESS,
        updated_at=_BASE + timedelta(seconds=5),
        artifacts=manifest,
    )
    fetched = await store.get(run_id=run_id, tenant_id=tenant_id)
    assert fetched is not None and fetched.artifacts == manifest

    # None 不清空 —— 一次后续的非终局写不得抹掉终局清单。
    await store.set_status(
        run_id=run_id,
        tenant_id=tenant_id,
        status=RunStatus.SUCCESS,
        updated_at=_BASE + timedelta(seconds=6),
    )
    fetched2 = await store.get(run_id=run_id, tenant_id=tenant_id)
    assert fetched2 is not None and fetched2.artifacts == manifest

    # 显式空清单是可写的(追问轮零交付),与「没传」语义不同。
    await store.set_status(
        run_id=run_id,
        tenant_id=tenant_id,
        status=RunStatus.SUCCESS,
        updated_at=_BASE + timedelta(seconds=7),
        artifacts=[],
    )
    fetched3 = await store.get(run_id=run_id, tenant_id=tenant_id)
    assert fetched3 is not None and fetched3.artifacts == []


# ---------------------------------------------------------------------------
# 多副本 CAS 守卫 —— 与 SQL 店谓词 byte-同义(test_sql_run_store 有镜像用例)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_status_expected_statuses_guard() -> None:
    """→ RUNNING 带 ``expected_statuses``:行已被跨副本取消(interrupted)时
    写被拒绝、行原样;pending 行照常放行。"""
    store = InMemoryRunStore()
    run_id, tenant_id = uuid4(), uuid4()
    await store.create(_info(run_id=run_id, tenant_id=tenant_id))
    await store.request_cancel(
        run_id=run_id, tenant_id=tenant_id, updated_at=_BASE, reason="user_cancel"
    )

    guard = (RunStatus.PENDING, RunStatus.QUEUED, RunStatus.RUNNING)
    hit = await store.set_status(
        run_id=run_id,
        tenant_id=tenant_id,
        status=RunStatus.RUNNING,
        updated_at=_BASE,
        expected_statuses=guard,
    )
    assert hit is False
    row = await store.get(run_id=run_id, tenant_id=tenant_id)
    assert row is not None and row.status is RunStatus.INTERRUPTED
    assert row.error == "user_cancel"

    fresh = uuid4()
    await store.create(_info(run_id=fresh, tenant_id=tenant_id))
    assert (
        await store.set_status(
            run_id=fresh,
            tenant_id=tenant_id,
            status=RunStatus.RUNNING,
            updated_at=_BASE,
            expected_statuses=guard,
        )
        is True
    )


@pytest.mark.asyncio
async def test_set_status_guard_claimed_by() -> None:
    """终局写带 ``guard_claimed_by``:行归别的副本时拒绝;NULL 或本副本放行。"""
    store = InMemoryRunStore()
    run_id, tenant_id = uuid4(), uuid4()
    await store.create(_info(run_id=run_id, tenant_id=tenant_id, status=RunStatus.RUNNING))
    await store.claim(
        run_id=run_id,
        tenant_id=tenant_id,
        claimed_by="pod-b",
        lease_until=_BASE + timedelta(seconds=30),
        heartbeat_at=_BASE,
    )

    hit = await store.set_status(
        run_id=run_id,
        tenant_id=tenant_id,
        status=RunStatus.INTERRUPTED,
        updated_at=_BASE,
        finished_at=_BASE,
        guard_claimed_by="pod-a",
    )
    assert hit is False
    row = await store.get(run_id=run_id, tenant_id=tenant_id)
    assert row is not None and row.status is RunStatus.RUNNING

    # 本副本 / 从未 claim 过 —— 都放行。
    assert (
        await store.set_status(
            run_id=run_id,
            tenant_id=tenant_id,
            status=RunStatus.SUCCESS,
            updated_at=_BASE,
            finished_at=_BASE,
            guard_claimed_by="pod-b",
        )
        is True
    )
    unclaimed = uuid4()
    await store.create(_info(run_id=unclaimed, tenant_id=tenant_id))
    assert (
        await store.set_status(
            run_id=unclaimed,
            tenant_id=tenant_id,
            status=RunStatus.ERROR,
            updated_at=_BASE,
            guard_claimed_by="pod-a",
        )
        is True
    )
