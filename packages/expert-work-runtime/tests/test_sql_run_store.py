"""Integration tests for ``SqlRunStore`` against a real Postgres — Mini-ADR J-41."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine
from testcontainers.postgres import PostgresContainer

from expert_work.persistence import (
    DatabaseConfig,
    create_async_engine_from_config,
    create_async_session_factory,
)
from expert_work.persistence.thread_meta import InMemoryThreadMetaStore, SqlThreadMetaStore
from expert_work.runtime.runs import (
    DisconnectMode,
    InMemoryRunStore,
    RunInfo,
    RunStatus,
    SqlRunEventStore,
    SqlRunStore,
    make_event_record,
)

pytestmark = pytest.mark.integration

PERSISTENCE_ROOT = Path(__file__).resolve().parents[2] / "expert-work-persistence"
ALEMBIC_INI = PERSISTENCE_ROOT / "alembic.ini"

_BASE = datetime(2026, 5, 22, 12, 0, 0, tzinfo=UTC)


def _sync_dsn(container: PostgresContainer) -> str:
    url = str(container.get_connection_url())
    return url.replace("+psycopg2", "+psycopg").replace("postgresql://", "postgresql+psycopg://", 1)


def _async_dsn(container: PostgresContainer) -> str:
    url = str(container.get_connection_url())
    return url.replace("+psycopg2", "+asyncpg").replace("postgresql://", "postgresql+asyncpg://", 1)


@pytest.fixture
def run_store(postgres_container: PostgresContainer) -> Iterator[SqlRunStore]:
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", _sync_dsn(postgres_container))
    command.upgrade(cfg, "head")

    engine: AsyncEngine = create_async_engine_from_config(
        DatabaseConfig(dsn=_async_dsn(postgres_container))
    )
    yield SqlRunStore(create_async_session_factory(engine))


@pytest.fixture
def run_event_store(postgres_container: PostgresContainer) -> Iterator[SqlRunEventStore]:
    """``SqlRunEventStore`` on the same database as ``run_store``."""
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", _sync_dsn(postgres_container))
    command.upgrade(cfg, "head")

    engine: AsyncEngine = create_async_engine_from_config(
        DatabaseConfig(dsn=_async_dsn(postgres_container))
    )
    yield SqlRunEventStore(create_async_session_factory(engine))


@pytest.fixture
def thread_meta_store(postgres_container: PostgresContainer) -> Iterator[SqlThreadMetaStore]:
    """``SqlThreadMetaStore`` on the same database as ``run_store`` —— agent
    过滤 join 的是 ``thread_meta``,所以两个 store 必须落在同一个库上。"""
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", _sync_dsn(postgres_container))
    command.upgrade(cfg, "head")

    engine: AsyncEngine = create_async_engine_from_config(
        DatabaseConfig(dsn=_async_dsn(postgres_container))
    )
    yield SqlThreadMetaStore(create_async_session_factory(engine))


def _info(
    *,
    run_id: UUID,
    tenant_id: UUID,
    thread_id: UUID | None = None,
    user_id: UUID | None = None,
    status: RunStatus = RunStatus.PENDING,
    created_at: datetime | None = None,
) -> RunInfo:
    return RunInfo(
        run_id=run_id,
        tenant_id=tenant_id,
        thread_id=thread_id or uuid4(),
        user_id=user_id,
        status=status,
        on_disconnect=DisconnectMode.CANCEL,
        is_resume=False,
        error=None,
        created_at=created_at or _BASE,
        updated_at=created_at or _BASE,
        finished_at=None,
    )


@pytest.mark.asyncio
async def test_create_then_get_round_trips(run_store: SqlRunStore) -> None:
    run_id, tenant_id, user_id = uuid4(), uuid4(), uuid4()
    await run_store.create(
        _info(run_id=run_id, tenant_id=tenant_id, user_id=user_id, status=RunStatus.RUNNING)
    )

    fetched = await run_store.get(run_id=run_id, tenant_id=tenant_id)
    assert fetched is not None
    assert fetched.run_id == run_id
    assert fetched.user_id == user_id
    assert fetched.status is RunStatus.RUNNING
    assert fetched.on_disconnect is DisconnectMode.CANCEL
    assert fetched.is_resume is False
    assert fetched.finished_at is None


@pytest.mark.asyncio
async def test_get_unknown_returns_none(run_store: SqlRunStore) -> None:
    assert await run_store.get(run_id=uuid4(), tenant_id=uuid4()) is None


@pytest.mark.asyncio
async def test_get_cross_tenant_returns_none(run_store: SqlRunStore) -> None:
    run_id, tenant_a, tenant_b = uuid4(), uuid4(), uuid4()
    await run_store.create(_info(run_id=run_id, tenant_id=tenant_a))

    assert await run_store.get(run_id=run_id, tenant_id=tenant_b) is None


@pytest.mark.asyncio
async def test_set_status_updates_row(run_store: SqlRunStore) -> None:
    run_id, tenant_id = uuid4(), uuid4()
    await run_store.create(_info(run_id=run_id, tenant_id=tenant_id))

    finished = _BASE + timedelta(seconds=12)
    hit = await run_store.set_status(
        run_id=run_id,
        tenant_id=tenant_id,
        status=RunStatus.SUCCESS,
        updated_at=finished,
        finished_at=finished,
    )
    assert hit is True
    fetched = await run_store.get(run_id=run_id, tenant_id=tenant_id)
    assert fetched is not None
    assert fetched.status is RunStatus.SUCCESS
    assert fetched.finished_at == finished


@pytest.mark.asyncio
async def test_set_status_error_persists_detail(run_store: SqlRunStore) -> None:
    run_id, tenant_id = uuid4(), uuid4()
    await run_store.create(_info(run_id=run_id, tenant_id=tenant_id))

    await run_store.set_status(
        run_id=run_id,
        tenant_id=tenant_id,
        status=RunStatus.ERROR,
        updated_at=_BASE,
        error="provider 503",
        finished_at=_BASE,
    )
    fetched = await run_store.get(run_id=run_id, tenant_id=tenant_id)
    assert fetched is not None
    assert fetched.status is RunStatus.ERROR
    assert fetched.error == "provider 503"


@pytest.mark.asyncio
async def test_set_status_unknown_returns_false(run_store: SqlRunStore) -> None:
    miss = await run_store.set_status(
        run_id=uuid4(),
        tenant_id=uuid4(),
        status=RunStatus.SUCCESS,
        updated_at=_BASE,
    )
    assert miss is False


@pytest.mark.asyncio
async def test_list_by_thread_filters_and_sorts(run_store: SqlRunStore) -> None:
    thread_id, tenant_a, tenant_b = uuid4(), uuid4(), uuid4()
    newer, older = uuid4(), uuid4()
    await run_store.create(
        _info(
            run_id=newer,
            tenant_id=tenant_a,
            thread_id=thread_id,
            created_at=_BASE + timedelta(minutes=1),
        )
    )
    await run_store.create(
        _info(run_id=older, tenant_id=tenant_a, thread_id=thread_id, created_at=_BASE)
    )
    await run_store.create(_info(run_id=uuid4(), tenant_id=tenant_b, thread_id=thread_id))

    listed = await run_store.list_by_thread(thread_id=thread_id, tenant_id=tenant_a)
    assert [r.run_id for r in listed] == [older, newer]


@pytest.mark.asyncio
async def test_delete_by_thread_scoped_to_thread_and_tenant(run_store: SqlRunStore) -> None:
    thread_a, thread_b, tenant, other = uuid4(), uuid4(), uuid4(), uuid4()
    await run_store.create(_info(run_id=uuid4(), tenant_id=tenant, thread_id=thread_a))
    await run_store.create(_info(run_id=uuid4(), tenant_id=tenant, thread_id=thread_a))
    keep = uuid4()
    await run_store.create(_info(run_id=keep, tenant_id=tenant, thread_id=thread_b))
    cross = uuid4()
    await run_store.create(_info(run_id=cross, tenant_id=other, thread_id=thread_a))

    removed = await run_store.delete_by_thread(thread_id=thread_a, tenant_id=tenant)
    assert removed == 2
    assert await run_store.list_by_thread(thread_id=thread_a, tenant_id=tenant) == []
    # Other thread + same-thread-different-tenant rows survive.
    assert [
        r.run_id for r in await run_store.list_by_thread(thread_id=thread_b, tenant_id=tenant)
    ] == [keep]
    assert await run_store.get(run_id=cross, tenant_id=other) is not None
    # Empty thread → no-op, rowcount 0.
    assert await run_store.delete_by_thread(thread_id=uuid4(), tenant_id=tenant) == 0


@pytest.mark.asyncio
async def test_delete_for_runs_removes_only_targeted_runs_sql(
    run_store: SqlRunStore, run_event_store: SqlRunEventStore
) -> None:
    """Deletion-hygiene PR3 §A — SQL twin of the in-memory contract."""
    tenant = uuid4()
    run_a, run_b = uuid4(), uuid4()
    await run_store.create(_info(run_id=run_a, tenant_id=tenant))
    await run_store.create(_info(run_id=run_b, tenant_id=tenant))
    await run_event_store.append(
        make_event_record(run_id=run_a, seq=0, event_name="metadata", data={})
    )
    await run_event_store.append(
        make_event_record(run_id=run_a, seq=1, event_name="updates", data={})
    )
    await run_event_store.append(
        make_event_record(run_id=run_b, seq=0, event_name="metadata", data={})
    )

    assert await run_event_store.delete_for_runs(run_ids=[run_a]) == 2
    assert list(await run_event_store.list(run_id=run_a)) == []
    assert len(await run_event_store.list(run_id=run_b)) == 1
    assert await run_event_store.delete_for_runs(run_ids=[]) == 0


@pytest.mark.asyncio
async def test_delete_by_thread_also_removes_child_run_events(
    run_store: SqlRunStore, run_event_store: SqlRunEventStore
) -> None:
    """Deletion-hygiene PR3 §A — ghost-run root cause: ``run_event`` rows
    (``ON DELETE RESTRICT``) must be emptied inside the same transaction,
    otherwise the ``agent_run`` delete is blocked and the purge fails."""
    tenant, thread = uuid4(), uuid4()
    doomed, bystander = uuid4(), uuid4()
    await run_store.create(_info(run_id=doomed, tenant_id=tenant, thread_id=thread))
    await run_store.create(_info(run_id=bystander, tenant_id=tenant))  # other thread
    await run_event_store.append(
        make_event_record(run_id=doomed, seq=0, event_name="metadata", data={})
    )
    await run_event_store.append(
        make_event_record(run_id=doomed, seq=1, event_name="updates", data={})
    )
    await run_event_store.append(
        make_event_record(run_id=bystander, seq=0, event_name="metadata", data={})
    )

    removed = await run_store.delete_by_thread(thread_id=thread, tenant_id=tenant)
    assert removed == 1
    assert await run_store.get(run_id=doomed, tenant_id=tenant) is None
    assert list(await run_event_store.list(run_id=doomed)) == []
    # The bystander run on another thread keeps its row AND its events.
    assert await run_store.get(run_id=bystander, tenant_id=tenant) is not None
    assert len(await run_event_store.list(run_id=bystander)) == 1


# ---------------------------------------------------------------------------
# 二期 PR3 — SqlRunEventStore.append_batch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_append_batch_round_trips_in_order_sql(
    run_store: SqlRunStore, run_event_store: SqlRunEventStore
) -> None:
    """One ``append_batch`` call, one session + ``add_all`` + single commit
    (matches ``event_log/db.py``'s ``put_batch`` precedent) → ``list``
    returns every row, ordered by ``seq``."""
    tenant, run_id = uuid4(), uuid4()
    await run_store.create(_info(run_id=run_id, tenant_id=tenant))
    records = [
        make_event_record(run_id=run_id, seq=i, event_name="updates", data={"i": i})
        for i in range(5)
    ]

    await run_event_store.append_batch(records)

    listed = await run_event_store.list(run_id=run_id)
    assert [r.seq for r in listed] == [0, 1, 2, 3, 4]
    assert [r.data for r in listed] == [{"i": i} for i in range(5)]


@pytest.mark.asyncio
async def test_append_batch_duplicate_seq_rolls_back_whole_batch_sql(
    run_store: SqlRunStore, run_event_store: SqlRunEventStore
) -> None:
    """A batch containing a ``(run_id, seq)`` collision raises
    ``IntegrityError`` and commits NOTHING from the batch — the single
    transaction rolls back atomically."""
    tenant, run_id = uuid4(), uuid4()
    await run_store.create(_info(run_id=run_id, tenant_id=tenant))
    await run_event_store.append(
        make_event_record(run_id=run_id, seq=0, event_name="metadata", data={})
    )

    with pytest.raises(IntegrityError):
        await run_event_store.append_batch(
            [
                make_event_record(run_id=run_id, seq=1, event_name="updates", data={"i": 1}),
                # Collides with the pre-existing seq=0 row.
                make_event_record(run_id=run_id, seq=0, event_name="updates", data={"dup": True}),
            ]
        )

    listed = await run_event_store.list(run_id=run_id)
    # Only the original seq=0 row survives — the batch's seq=1 row never
    # committed because the transaction rolled back on the seq=0 collision.
    assert [r.seq for r in listed] == [0]


@pytest.mark.asyncio
async def test_append_batch_empty_is_noop_sql(
    run_store: SqlRunStore, run_event_store: SqlRunEventStore
) -> None:
    run_id = uuid4()

    await run_event_store.append_batch([])

    assert await run_event_store.list(run_id=run_id) == []


# ---------------------------------------------------------------------------
# Stream H.3 PR 1 — list_for_tenant / list_all_tenants
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_for_tenant_orders_desc_and_filters_by_tenant(
    run_store: SqlRunStore,
) -> None:
    tenant_a, tenant_b = uuid4(), uuid4()
    oldest, newest = uuid4(), uuid4()
    await run_store.create(_info(run_id=oldest, tenant_id=tenant_a, created_at=_BASE))
    await run_store.create(
        _info(run_id=newest, tenant_id=tenant_a, created_at=_BASE + timedelta(minutes=1))
    )
    # tenant B row must not leak.
    await run_store.create(_info(run_id=uuid4(), tenant_id=tenant_b))

    listed = await run_store.list_for_tenant(tenant_id=tenant_a)
    assert [r.run_id for r in listed] == [newest, oldest]
    assert all(r.tenant_id == tenant_a for r in listed)


@pytest.mark.asyncio
async def test_list_for_tenant_status_filter(run_store: SqlRunStore) -> None:
    tenant_id = uuid4()
    paused_id = uuid4()
    await run_store.create(_info(run_id=paused_id, tenant_id=tenant_id, status=RunStatus.PAUSED))
    await run_store.create(_info(run_id=uuid4(), tenant_id=tenant_id, status=RunStatus.SUCCESS))

    paused = await run_store.list_for_tenant(tenant_id=tenant_id, status=RunStatus.PAUSED)
    assert [r.run_id for r in paused] == [paused_id]


@pytest.mark.asyncio
async def test_list_for_tenant_q_filters_run_and_thread_id(run_store: SqlRunStore) -> None:
    """``q`` substring-matches run_id / thread_id via CAST … ILIKE (real PG)."""
    tenant_id = uuid4()
    run_a = UUID("aaaaaaaa-0000-0000-0000-000000000a01")
    thread_b = UUID("bbbbbbbb-0000-0000-0000-000000000b02")
    await run_store.create(_info(run_id=run_a, tenant_id=tenant_id))
    await run_store.create(_info(run_id=uuid4(), tenant_id=tenant_id, thread_id=thread_b))
    await run_store.create(_info(run_id=uuid4(), tenant_id=tenant_id))

    by_run = await run_store.list_for_tenant(tenant_id=tenant_id, q="aaaaaaaa")
    assert [r.run_id for r in by_run] == [run_a]
    by_thread = await run_store.list_for_tenant(tenant_id=tenant_id, q="bbbbbbbb")
    assert [r.thread_id for r in by_thread] == [thread_b]
    # Case-insensitive.
    assert len(await run_store.list_for_tenant(tenant_id=tenant_id, q="AAAAAAAA")) == 1
    # A LIKE wildcard is escaped, so it matches literally (UUIDs have no '%').
    assert await run_store.list_for_tenant(tenant_id=tenant_id, q="%") == []


@pytest.mark.asyncio
async def test_list_for_tenant_user_id_filter(run_store: SqlRunStore) -> None:
    tenant_id = uuid4()
    user_a, user_b = uuid4(), uuid4()
    run_a = uuid4()
    await run_store.create(_info(run_id=run_a, tenant_id=tenant_id, user_id=user_a))
    await run_store.create(_info(run_id=uuid4(), tenant_id=tenant_id, user_id=user_b))
    await run_store.create(_info(run_id=uuid4(), tenant_id=tenant_id, user_id=None))  # system

    only_a = await run_store.list_for_tenant(tenant_id=tenant_id, user_id=user_a)
    assert [r.run_id for r in only_a] == [run_a]
    assert await run_store.list_for_tenant(tenant_id=tenant_id, user_id=uuid4()) == []


@pytest.mark.asyncio
async def test_list_for_tenant_offset_limit(run_store: SqlRunStore) -> None:
    tenant_id = uuid4()
    ids = []
    for i in range(5):
        rid = uuid4()
        ids.append(rid)
        await run_store.create(
            _info(run_id=rid, tenant_id=tenant_id, created_at=_BASE + timedelta(minutes=i))
        )
    expected_desc = list(reversed(ids))

    page1 = await run_store.list_for_tenant(tenant_id=tenant_id, limit=2, offset=0)
    page2 = await run_store.list_for_tenant(tenant_id=tenant_id, limit=2, offset=2)
    page3 = await run_store.list_for_tenant(tenant_id=tenant_id, limit=2, offset=4)
    assert [r.run_id for r in page1] == expected_desc[:2]
    assert [r.run_id for r in page2] == expected_desc[2:4]
    assert [r.run_id for r in page3] == expected_desc[4:5]


@pytest.mark.asyncio
async def test_list_all_tenants_returns_runs_across_tenants(
    run_store: SqlRunStore,
) -> None:
    """The test container's postgres user has BYPASSRLS, so prior tests
    in the same session may have left rows. Assert ``tenant_a`` and
    ``tenant_b`` are *both visible* as a subset rather than exact match."""
    tenant_a, tenant_b = uuid4(), uuid4()
    await run_store.create(_info(run_id=uuid4(), tenant_id=tenant_a))
    await run_store.create(_info(run_id=uuid4(), tenant_id=tenant_b))

    listed = await run_store.list_all_tenants(limit=500)
    tenants = {r.tenant_id for r in listed}
    assert tenant_a in tenants
    assert tenant_b in tenants


# ---------------------------------------------------------------------------
# Stream H.3 PR 2 — set_trace_id (Mini-ADR H-9.5)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_trace_id_writes_and_reads_back_sql(run_store: SqlRunStore) -> None:
    run_id, tenant_id = uuid4(), uuid4()
    await run_store.create(_info(run_id=run_id, tenant_id=tenant_id))

    ok = await run_store.set_trace_id(run_id=run_id, tenant_id=tenant_id, trace_id="cafef00d" * 4)
    assert ok is True

    fetched = await run_store.get(run_id=run_id, tenant_id=tenant_id)
    assert fetched is not None
    assert fetched.trace_id == "cafef00d" * 4


@pytest.mark.asyncio
async def test_set_trace_id_unknown_run_returns_false_sql(
    run_store: SqlRunStore,
) -> None:
    ok = await run_store.set_trace_id(run_id=uuid4(), tenant_id=uuid4(), trace_id="abc")
    assert ok is False


@pytest.mark.asyncio
async def test_list_for_tenant_thread_ids_filter_sql(run_store: SqlRunStore) -> None:
    """Stream H.6 (Mini-ADR H-10) — ``WHERE thread_id IN (...)`` + empty fast-path."""
    tenant_id = uuid4()
    thread_a, thread_b = uuid4(), uuid4()
    await run_store.create(_info(run_id=uuid4(), tenant_id=tenant_id, thread_id=thread_a))
    await run_store.create(_info(run_id=uuid4(), tenant_id=tenant_id, thread_id=thread_a))
    await run_store.create(_info(run_id=uuid4(), tenant_id=tenant_id, thread_id=thread_b))

    subset = await run_store.list_for_tenant(tenant_id=tenant_id, thread_ids=[thread_a])
    assert len(subset) == 2
    assert {r.thread_id for r in subset} == {thread_a}

    # Empty collection → no rows (the agent has no threads), not "no filter".
    assert await run_store.list_for_tenant(tenant_id=tenant_id, thread_ids=[]) == []
    # None regression — unfiltered list unchanged.
    assert len(await run_store.list_for_tenant(tenant_id=tenant_id)) == 3


# --- Stream 9.4 (HA failover) — ownership lease on real Postgres -------------


@pytest.mark.asyncio
async def test_claim_heartbeat_and_orphan_lease_round_trip(run_store: SqlRunStore) -> None:
    run_id, tenant = uuid4(), uuid4()
    await run_store.create(_info(run_id=run_id, tenant_id=tenant, status=RunStatus.RUNNING))
    t0 = datetime.now(UTC)
    assert await run_store.claim(
        run_id=run_id,
        tenant_id=tenant,
        claimed_by="inst-a",
        lease_until=t0 + timedelta(seconds=30),
        heartbeat_at=t0,
    )
    # owner renews; a non-owner cannot
    t1 = t0 + timedelta(seconds=10)
    assert await run_store.heartbeat(
        run_id=run_id,
        claimed_by="inst-a",
        lease_until=t1 + timedelta(seconds=30),
        heartbeat_at=t1,
    )
    assert not await run_store.heartbeat(
        run_id=run_id,
        claimed_by="inst-b",
        lease_until=t1 + timedelta(seconds=60),
        heartbeat_at=t1,
    )
    row = await run_store.get(run_id=run_id, tenant_id=tenant)
    assert row is not None and row.claimed_by == "inst-a" and row.reclaim_count == 0


@pytest.mark.asyncio
async def test_concurrent_reclaim_exactly_one_winner(run_store: SqlRunStore) -> None:
    """True DB concurrency — the reclaim CAS row-lock serialises sweepers."""
    import asyncio

    run_id, tenant = uuid4(), uuid4()
    await run_store.create(_info(run_id=run_id, tenant_id=tenant, status=RunStatus.RUNNING))
    now = datetime.now(UTC)
    # expired lease → orphan
    await run_store.claim(
        run_id=run_id,
        tenant_id=tenant,
        claimed_by="dead",
        lease_until=now - timedelta(seconds=1),
        heartbeat_at=now,
    )

    async def _reclaim(n: int) -> bool:
        return await run_store.reclaim(
            run_id=run_id,
            new_owner=f"sweeper-{n}",
            lease_until=now + timedelta(seconds=30),
            heartbeat_at=now,
            now=now,
        )

    results = await asyncio.gather(*(_reclaim(i) for i in range(8)))
    assert sum(1 for r in results if r) == 1  # exactly one sweeper wins
    row = await run_store.get(run_id=run_id, tenant_id=tenant)
    assert row is not None and row.reclaim_count == 1  # incremented exactly once


@pytest.mark.asyncio
async def test_list_orphans_finds_only_expired_running(run_store: SqlRunStore) -> None:
    tenant = uuid4()
    now = datetime.now(UTC)
    orphan = uuid4()
    await run_store.create(_info(run_id=orphan, tenant_id=tenant, status=RunStatus.RUNNING))
    await run_store.claim(
        run_id=orphan,
        tenant_id=tenant,
        claimed_by="dead",
        lease_until=now - timedelta(seconds=1),
        heartbeat_at=now,
    )
    live = uuid4()
    await run_store.create(_info(run_id=live, tenant_id=tenant, status=RunStatus.RUNNING))
    await run_store.claim(
        run_id=live,
        tenant_id=tenant,
        claimed_by="alive",
        lease_until=now + timedelta(seconds=30),
        heartbeat_at=now,
    )
    found = await run_store.list_orphans(now=now, limit=10)
    assert orphan in [o.run_id for o in found]
    assert live not in [o.run_id for o in found]


@pytest.mark.asyncio
async def test_list_stale_pending_finds_only_old_pending(run_store: SqlRunStore) -> None:
    """W1-PR3 Task 1 — the crash-window recovery scan's predicate, proven
    against real Postgres (partial index ``ix_agent_run_pending_sweep``).
    Exact ordering/limit is covered by the in-memory double
    (``test_list_stale_pending_orders_oldest_first_and_respects_limit``);
    this is a cross-tenant scan (no tenant filter) over a table shared with
    every other test in this session, so assertions use membership, same
    convention as ``test_list_orphans_finds_only_expired_running``."""
    tenant = uuid4()
    cutoff = _BASE + timedelta(seconds=600)

    stale = uuid4()
    await run_store.create(
        _info(run_id=stale, tenant_id=tenant, status=RunStatus.PENDING, created_at=_BASE)
    )
    fresh = uuid4()
    await run_store.create(
        _info(run_id=fresh, tenant_id=tenant, status=RunStatus.PENDING, created_at=cutoff)
    )
    running = uuid4()
    await run_store.create(
        _info(run_id=running, tenant_id=tenant, status=RunStatus.RUNNING, created_at=_BASE)
    )

    # limit is generous: other tests in this shared-container session also
    # leave stale PENDING rows, and a small limit would truncate ours away.
    found_ids = [r.run_id for r in await run_store.list_stale_pending(cutoff=cutoff, limit=1000)]
    assert stale in found_ids
    assert fresh not in found_ids
    assert running not in found_ids


@pytest.mark.asyncio
async def test_concurrent_claim_queued_exactly_one_winner(run_store: SqlRunStore) -> None:
    """True DB concurrency — the claim CAS row-lock serialises queue workers."""
    import asyncio
    from dataclasses import replace

    run_id, tenant = uuid4(), uuid4()
    info = _info(run_id=run_id, tenant_id=tenant, status=RunStatus.QUEUED)
    await run_store.create(replace(info, enqueued_input={"input": "go", "image_refs": []}))
    now = datetime.now(UTC)

    async def _claim(n: int) -> RunInfo | None:
        return await run_store.claim_queued(
            run_id=run_id,
            new_owner=f"worker-{n}",
            lease_until=now + timedelta(seconds=30),
            heartbeat_at=now,
        )

    results = await asyncio.gather(*(_claim(i) for i in range(16)))
    winners = [r for r in results if r is not None]
    assert len(winners) == 1  # exactly one worker claims the queued run
    assert winners[0].enqueued_input == {"input": "go", "image_refs": []}
    row = await run_store.get(run_id=run_id, tenant_id=tenant)
    assert row is not None and row.status is RunStatus.RUNNING


@pytest.mark.asyncio
async def test_request_cancel_interrupts_a_queued_run(run_store: SqlRunStore) -> None:
    """External-API-v1 P1 review fix (Critical C1) — a run still sitting in the
    distributed queue, never claimed by any worker, must be cancellable too:
    ``request_cancel``'s CAS now matches ``queued`` in addition to
    ``running``/``pending``. Once interrupted, ``claim_queued``'s own
    ``status = queued`` guard can never match it again — the run is
    permanently unclaimable, not just marked cancelled while still racing a
    worker for execution.
    """
    from dataclasses import replace

    run_id, tenant_id = uuid4(), uuid4()
    info = _info(run_id=run_id, tenant_id=tenant_id, status=RunStatus.QUEUED)
    await run_store.create(replace(info, enqueued_input={"input": "hi", "image_refs": []}))

    hit = await run_store.request_cancel(
        run_id=run_id,
        tenant_id=tenant_id,
        updated_at=_BASE + timedelta(seconds=9),
        reason="user_cancel",
    )
    assert hit is True
    fetched = await run_store.get(run_id=run_id, tenant_id=tenant_id)
    assert fetched is not None
    assert fetched.status is RunStatus.INTERRUPTED
    assert fetched.finished_at == _BASE + timedelta(seconds=9)
    # 中断原因写进 error 列(InterruptReason 词表)—— in-memory 店同款谓词。
    assert fetched.error == "user_cancel"

    # A worker racing to claim the same run after the cancel must lose — the
    # CAS's own ``status = queued`` predicate no longer matches.
    now = datetime.now(UTC)
    claimed = await run_store.claim_queued(
        run_id=run_id,
        new_owner="worker-late",
        lease_until=now + timedelta(seconds=30),
        heartbeat_at=now,
    )
    assert claimed is None


@pytest.mark.asyncio
async def test_list_queued_round_trips_enqueued_input(run_store: SqlRunStore) -> None:
    from dataclasses import replace

    tenant, run_id = uuid4(), uuid4()
    info = _info(run_id=run_id, tenant_id=tenant, status=RunStatus.QUEUED)
    await run_store.create(
        replace(info, enqueued_input={"input": "hi", "untrusted_content": ["x"]})
    )

    queued = await run_store.list_queued(limit=10)
    assert any(q.run_id == run_id for q in queued)
    match = next(q for q in queued if q.run_id == run_id)
    assert match.enqueued_input == {"input": "hi", "untrusted_content": ["x"]}


@pytest.mark.asyncio
async def test_list_running_for_agent_joins_thread_meta(run_store: SqlRunStore) -> None:
    """Stream RT-4 (RT-ADR-17) — the agent bulk-cancel enumeration join."""
    from expert_work.persistence.thread_meta import SqlThreadMetaStore

    threads = SqlThreadMetaStore(run_store._sf)  # same engine as the run store
    tenant = uuid4()
    t_a1, t_a2, t_b = uuid4(), uuid4(), uuid4()
    for tid, agent in ((t_a1, "a"), (t_a2, "a"), (t_b, "b")):
        await threads.create(
            thread_id=tid,
            tenant_id=tenant,
            created_by="seed",
            agent_name=agent,
            agent_version="1.0.0",
        )
    run_a_running = uuid4()
    run_a_done = uuid4()
    run_b_running = uuid4()
    await run_store.create(
        _info(run_id=run_a_running, tenant_id=tenant, thread_id=t_a1, status=RunStatus.RUNNING)
    )
    await run_store.create(
        _info(run_id=run_a_done, tenant_id=tenant, thread_id=t_a2, status=RunStatus.SUCCESS)
    )
    await run_store.create(
        _info(run_id=run_b_running, tenant_id=tenant, thread_id=t_b, status=RunStatus.RUNNING)
    )

    running = await run_store.list_running_for_agent(tenant_id=tenant, agent_name="a")
    # Only agent a's RUNNING run — not its SUCCESS run, not agent b's run.
    assert {r.run_id for r in running} == {run_a_running}


@pytest.mark.asyncio
async def test_list_running_for_agent_filters_by_version_when_given(
    run_store: SqlRunStore,
) -> None:
    """Deletion hygiene follow-up — parity with the in-memory double:
    ``agent_version`` narrows to that version; ``None`` stays name-level;
    a thread with a NULL version is out of scope of a version-level cancel."""
    from expert_work.persistence.thread_meta import SqlThreadMetaStore

    threads = SqlThreadMetaStore(run_store._sf)  # same engine as the run store
    tenant = uuid4()
    t_v1, t_v2, t_unbound = uuid4(), uuid4(), uuid4()
    for tid, version in ((t_v1, "1.0.0"), (t_v2, "2.0.0"), (t_unbound, None)):
        await threads.create(
            thread_id=tid,
            tenant_id=tenant,
            created_by="seed",
            agent_name="foo",
            agent_version=version,
        )
    run_v1, run_v2, run_unbound = uuid4(), uuid4(), uuid4()
    for run_id, thread_id in ((run_v1, t_v1), (run_v2, t_v2), (run_unbound, t_unbound)):
        await run_store.create(
            _info(run_id=run_id, tenant_id=tenant, thread_id=thread_id, status=RunStatus.RUNNING)
        )

    both = await run_store.list_running_for_agent(tenant_id=tenant, agent_name="foo")
    assert {r.run_id for r in both} == {run_v1, run_v2, run_unbound}  # no version = old semantics

    only_v1 = await run_store.list_running_for_agent(
        tenant_id=tenant, agent_name="foo", agent_version="1.0.0"
    )
    assert {r.run_id for r in only_v1} == {run_v1}  # version sentinel; NULL row excluded


@pytest.mark.asyncio
async def test_agent_name_filter_matches_the_in_memory_store(
    run_store: SqlRunStore, thread_meta_store: SqlThreadMetaStore
) -> None:
    """两个后端各写一遍谓词,是本仓反复出问题的地方(SQL 的 join 语义与
    内存的逐行比较不是自动等价的)。同一组输入喂两边,断言结果集相同。

    覆盖四种输入:两个命中的、一个不命中的、``None``(不过滤)。只测命中
    的话,「SQL 把过滤条件写反了」和「写对了」会给出同样的绿。
    """
    mem_threads = InMemoryThreadMetaStore()
    mem_runs = InMemoryRunStore(thread_meta_store=mem_threads)
    tenant_id = uuid4()
    layout = [("alpha", uuid4()), ("beta", uuid4()), ("alpha", uuid4())]

    for agent_name, thread_id in layout:
        for threads in (thread_meta_store, mem_threads):
            await threads.create(
                thread_id=thread_id,
                tenant_id=tenant_id,
                created_by="seed",
                agent_name=agent_name,
            )
        run_id = uuid4()
        for runs in (run_store, mem_runs):
            await runs.create(_info(run_id=run_id, tenant_id=tenant_id, thread_id=thread_id))

    for probe in ("alpha", "beta", "nonexistent", None):
        sql_rows = await run_store.list_for_tenant(tenant_id=tenant_id, agent_name=probe)
        mem_rows = await mem_runs.list_for_tenant(tenant_id=tenant_id, agent_name=probe)
        assert {r.run_id for r in sql_rows} == {r.run_id for r in mem_rows}, (
            f"agent_name={probe!r} 两个后端结果不一致"
        )


# ---------------------------------------------------------------------------
# 对话条目 PR2 —— event_names 过滤 / keyset 游标(与内存版共用契约)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_event_names_filter_sql(
    run_store: SqlRunStore,
    run_event_store: SqlRunEventStore,
    event_names_filter_contract: Callable[..., Awaitable[None]],
) -> None:
    """SQL 侧的 ``event_names`` 过滤必须与内存侧逐条同义 —— 断言体在
    ``conftest`` 的契约里,两边喂的是同一份。``run_event`` 是 ``agent_run``
    的子表,所以先建 run 行。"""
    run_id = uuid4()
    await run_store.create(_info(run_id=run_id, tenant_id=uuid4()))
    await event_names_filter_contract(run_event_store, run_id=run_id)


@pytest.mark.asyncio
async def test_list_for_tenant_before_is_keyset_paginated_sql(
    run_store: SqlRunStore,
    keyset_before_contract: Callable[..., Awaitable[None]],
) -> None:
    """SQL 侧的 keyset 游标与内存侧同义,包括 ``created_at`` 并列时按
    ``run_id`` 定序这一条。"""
    await keyset_before_contract(run_store)


@pytest.mark.asyncio
async def test_set_status_artifacts_round_trip_and_none_keeps(run_store: SqlRunStore) -> None:
    """产物清单契约 —— SQL 店与 in-memory 店谓词 byte-同义(见
    test_run_store.py 同名场景):终局写清单、None 不碰既有、[] 可写。"""
    run_id, tenant_id = uuid4(), uuid4()
    await run_store.create(_info(run_id=run_id, tenant_id=tenant_id))

    manifest = [{"name": "plan.pptx", "kind": "document", "version": 1, "created_at": "t"}]
    await run_store.set_status(
        run_id=run_id,
        tenant_id=tenant_id,
        status=RunStatus.SUCCESS,
        updated_at=_BASE + timedelta(seconds=5),
        artifacts=manifest,
    )
    fetched = await run_store.get(run_id=run_id, tenant_id=tenant_id)
    assert fetched is not None and fetched.artifacts == manifest

    await run_store.set_status(
        run_id=run_id,
        tenant_id=tenant_id,
        status=RunStatus.SUCCESS,
        updated_at=_BASE + timedelta(seconds=6),
    )
    fetched2 = await run_store.get(run_id=run_id, tenant_id=tenant_id)
    assert fetched2 is not None and fetched2.artifacts == manifest

    await run_store.set_status(
        run_id=run_id,
        tenant_id=tenant_id,
        status=RunStatus.SUCCESS,
        updated_at=_BASE + timedelta(seconds=7),
        artifacts=[],
    )
    fetched3 = await run_store.get(run_id=run_id, tenant_id=tenant_id)
    assert fetched3 is not None and fetched3.artifacts == []
