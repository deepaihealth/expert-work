"""Knowledge retrieval log store contract (B-175 P2-PR4) — **both backends run
the same assertions**.

A row records one retrieval (agent / console test / open API): the bases it
spanned, the query (capped at 2,000 characters), parameters, outcome, result
ids + scores (never document text), flags, latency, run and actor. Reads are
tenant-scoped and per base, newest first, filtered by source / status / zero
hits / time and paged with a total. Rows go with the base, with a purged
user's runs, and past the retention window (cross-tenant, bounded batches).

The memory variant runs in unit CI; the SQL variant is ``integration`` and
gets its own database in the container.

Before running the SQL variant locally:
export DOCKER_HOST=unix:///Users/mac/.docker/run/docker.sock
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from testcontainers.postgres import PostgresContainer

from expert_work.persistence import (
    DatabaseConfig,
    create_async_engine_from_config,
    create_async_session_factory,
)
from expert_work.persistence.knowledge import (
    RETRIEVAL_LOG_QUERY_MAX_CHARS,
    InMemoryKnowledgeRetrievalLogStore,
    KnowledgeRetrievalLogStore,
    RetrievalLogEntry,
    SqlKnowledgeRetrievalLogStore,
)

ALEMBIC_INI = Path(__file__).resolve().parent.parent / "alembic.ini"
_DB = "knowledge_retrieval_log_contract"
_NOW = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)


@pytest.fixture(
    params=[
        "memory",
        pytest.param("sql", marks=pytest.mark.integration),
    ]
)
async def store(request: pytest.FixtureRequest) -> AsyncIterator[KnowledgeRetrievalLogStore]:
    if request.param == "memory":
        yield InMemoryKnowledgeRetrievalLogStore()
        return

    container: PostgresContainer = request.getfixturevalue("postgres_container")
    admin = str(container.get_connection_url()).replace("+psycopg2", "")
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{_DB}" WITH (FORCE)')
        conn.execute(f'CREATE DATABASE "{_DB}"')
    base, _, _ = admin.rpartition("/")
    dsn = f"{base}/{_DB}"
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", dsn.replace("postgresql://", "postgresql+psycopg://", 1))
    command.upgrade(cfg, "head")
    engine = create_async_engine_from_config(
        DatabaseConfig(dsn=dsn.replace("postgresql://", "postgresql+asyncpg://", 1))
    )
    try:
        yield SqlKnowledgeRetrievalLogStore(create_async_session_factory(engine))
    finally:
        await engine.dispose()
        with psycopg.connect(admin, autocommit=True) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{_DB}" WITH (FORCE)')


def _entry(
    tenant: UUID,
    kb_ids: tuple[UUID, ...],
    *,
    age_minutes: int = 0,
    source: str = "agent",
    status: str = "ok",
    results: tuple[dict[str, Any], ...] = ({"chunk_id": "c1", "rerank_score": 0.9},),
    run_id: UUID | None = None,
    thread_id: UUID | None = None,
    actor_id: str | None = None,
    query: str = "what is the deductible",
) -> RetrievalLogEntry:
    return RetrievalLogEntry(
        tenant_id=tenant,
        kb_ids=kb_ids,
        source=source,  # type: ignore[arg-type]
        query=query,
        status=status,
        latency_ms=120,
        params={"limit": 5, "method": "hybrid"},
        results=results,
        flags={"embedding_degraded": False, "rerank_outcome": "ok"},
        run_id=run_id,
        thread_id=thread_id,
        actor_id=actor_id,
        created_at=_NOW - timedelta(minutes=age_minutes),
    )


async def _ids(
    store: KnowledgeRetrievalLogStore, tenant: UUID, kb: UUID, **filters: Any
) -> list[UUID]:
    rows, _total = await store.list_for_base(tenant_id=tenant, kb_id=kb, **filters)
    return [row.id for row in rows]


async def test_a_written_entry_reads_back_whole(store: KnowledgeRetrievalLogStore) -> None:
    tenant, kb, other_kb, run = uuid4(), uuid4(), uuid4(), uuid4()
    entry = _entry(tenant, (kb, other_kb), run_id=run, thread_id=uuid4(), actor_id="employee-1")
    await store.add(entry)

    got = await store.get(tenant_id=tenant, log_id=entry.id)
    assert got == entry
    assert got is not None
    assert got.result_count == 1
    # Listed under every base it spanned.
    assert await _ids(store, tenant, other_kb) == [entry.id]


async def test_a_long_query_is_stored_cut_to_the_cap(store: KnowledgeRetrievalLogStore) -> None:
    tenant, kb = uuid4(), uuid4()
    entry = _entry(tenant, (kb,), query="x" * (RETRIEVAL_LOG_QUERY_MAX_CHARS + 500))
    await store.add(entry)
    got = await store.get(tenant_id=tenant, log_id=entry.id)
    assert got is not None
    assert got.query == "x" * RETRIEVAL_LOG_QUERY_MAX_CHARS


def test_an_unknown_source_is_refused() -> None:
    with pytest.raises(ValueError, match="source"):
        _entry(uuid4(), (uuid4(),), source="webhook")


async def test_lists_newest_first_with_paging_and_a_total(
    store: KnowledgeRetrievalLogStore,
) -> None:
    tenant, kb = uuid4(), uuid4()
    entries = [_entry(tenant, (kb,), age_minutes=age) for age in (30, 10, 50, 20, 40)]
    for entry in entries:
        await store.add(entry)
    newest_first = [e.id for e in sorted(entries, key=lambda e: e.created_at, reverse=True)]

    rows, total = await store.list_for_base(tenant_id=tenant, kb_id=kb, limit=2, offset=2)
    assert [row.id for row in rows] == newest_first[2:4]
    assert total == 5
    assert await _ids(store, tenant, kb) == newest_first


async def test_filters_by_source_status_zero_hits_and_time(
    store: KnowledgeRetrievalLogStore,
) -> None:
    tenant, kb = uuid4(), uuid4()
    agent = _entry(tenant, (kb,), age_minutes=5)
    console = _entry(tenant, (kb,), age_minutes=15, source="console_test", actor_id="e-1")
    empty = _entry(tenant, (kb,), age_minutes=25, status="no_hits", results=())
    below = _entry(tenant, (kb,), age_minutes=35, status="below_threshold", results=())
    # A search that raised has no results either, but it did not come back empty.
    failed = _entry(tenant, (kb,), age_minutes=45, status="error", results=())
    for entry in (agent, console, empty, below, failed):
        await store.add(entry)

    assert await _ids(store, tenant, kb, source="console_test") == [console.id]
    assert await _ids(store, tenant, kb, status="no_hits") == [empty.id]
    assert await _ids(store, tenant, kb, zero_hit=True) == [empty.id, below.id]
    # ``since`` inclusive, ``until`` exclusive.
    assert await _ids(store, tenant, kb, since=console.created_at, until=agent.created_at) == [
        console.id
    ]
    # Filters combine: no console test came back empty.
    rows, total = await store.list_for_base(
        tenant_id=tenant, kb_id=kb, source="console_test", zero_hit=True
    )
    assert (rows, total) == ([], 0)


async def test_another_tenant_or_base_sees_nothing(store: KnowledgeRetrievalLogStore) -> None:
    tenant, other_tenant, kb = uuid4(), uuid4(), uuid4()
    entry = _entry(tenant, (kb,))
    await store.add(entry)

    assert await store.get(tenant_id=other_tenant, log_id=entry.id) is None
    assert await _ids(store, other_tenant, kb) == []
    assert await _ids(store, tenant, uuid4()) == []
    assert await store.delete_for_base(tenant_id=other_tenant, kb_id=kb) == 0
    assert await store.delete_for_runs(tenant_id=other_tenant, run_ids=[uuid4()]) == 0
    assert await store.delete_for_threads(tenant_id=other_tenant, thread_ids=[uuid4()]) == 0
    assert await store.delete_for_actor(tenant_id=other_tenant, actor_id="employee-1") == 0
    assert await store.get(tenant_id=tenant, log_id=entry.id) == entry


async def test_another_tenant_keeps_rows_with_the_same_thread_run_and_actor(
    store: KnowledgeRetrievalLogStore,
) -> None:
    """The per-tenant deletes match on the tenant too, not only on the id."""
    tenant, other_tenant, kb, run, thread = uuid4(), uuid4(), uuid4(), uuid4(), uuid4()
    theirs = _entry(other_tenant, (kb,), run_id=run, thread_id=thread, actor_id="e-1")
    await store.add(theirs)
    assert await store.delete_for_runs(tenant_id=tenant, run_ids=[run]) == 0
    assert await store.delete_for_threads(tenant_id=tenant, thread_ids=[thread]) == 0
    assert await store.delete_for_actor(tenant_id=tenant, actor_id="e-1") == 0
    assert await store.get(tenant_id=other_tenant, log_id=theirs.id) == theirs


async def test_deleting_threads_deletes_their_rows_whoever_ran_them(
    store: KnowledgeRetrievalLogStore,
) -> None:
    """A conversation's rows go by its thread id — also rows of runs another
    user (an approver) or no user started on it."""
    tenant, kb, thread, other_thread = uuid4(), uuid4(), uuid4(), uuid4()
    on_thread = [_entry(tenant, (kb,), thread_id=thread, run_id=uuid4()) for _ in range(3)]
    elsewhere = _entry(tenant, (kb,), thread_id=other_thread, run_id=uuid4(), age_minutes=1)
    console = _entry(tenant, (kb,), source="console_test", age_minutes=2)
    for entry in (*on_thread, elsewhere, console):
        await store.add(entry)

    assert await store.delete_for_threads(tenant_id=tenant, thread_ids=[thread]) == 3
    assert await store.delete_for_threads(tenant_id=tenant, thread_ids=[]) == 0
    assert await _ids(store, tenant, kb) == [elsewhere.id, console.id]


async def test_deleting_an_actor_deletes_their_console_tests_only(
    store: KnowledgeRetrievalLogStore,
) -> None:
    tenant, kb = uuid4(), uuid4()
    mine = _entry(tenant, (kb,), source="console_test", actor_id="e-1")
    theirs = _entry(tenant, (kb,), source="console_test", actor_id="e-2", age_minutes=1)
    agent = _entry(tenant, (kb,), run_id=uuid4(), age_minutes=2)
    for entry in (mine, theirs, agent):
        await store.add(entry)

    assert await store.delete_for_actor(tenant_id=tenant, actor_id="e-1") == 1
    assert await _ids(store, tenant, kb) == [theirs.id, agent.id]


async def test_deleting_a_base_deletes_every_row_that_spanned_it(
    store: KnowledgeRetrievalLogStore,
) -> None:
    tenant, kb, other_kb = uuid4(), uuid4(), uuid4()
    only = _entry(tenant, (kb,))
    both = _entry(tenant, (other_kb, kb))
    unrelated = _entry(tenant, (other_kb,))
    for entry in (only, both, unrelated):
        await store.add(entry)

    assert await store.delete_for_base(tenant_id=tenant, kb_id=kb) == 2
    assert await _ids(store, tenant, kb) == []
    assert await _ids(store, tenant, other_kb) == [unrelated.id]


async def test_deleting_runs_deletes_their_rows_only(store: KnowledgeRetrievalLogStore) -> None:
    tenant, kb, run_a, run_b, run_c = uuid4(), uuid4(), uuid4(), uuid4(), uuid4()
    a1 = _entry(tenant, (kb,), run_id=run_a, age_minutes=1)
    a2 = _entry(tenant, (kb,), run_id=run_a, age_minutes=2)
    b = _entry(tenant, (kb,), run_id=run_b, age_minutes=3)
    c = _entry(tenant, (kb,), run_id=run_c, age_minutes=4)
    console = _entry(tenant, (kb,), source="console_test", age_minutes=5)
    for entry in (a1, a2, b, c, console):
        await store.add(entry)

    assert await store.delete_for_runs(tenant_id=tenant, run_ids=[run_a, run_b]) == 3
    assert await store.delete_for_runs(tenant_id=tenant, run_ids=[]) == 0
    assert await _ids(store, tenant, kb) == [c.id, console.id]


async def test_retention_deletes_old_rows_across_tenants_in_bounded_batches(
    store: KnowledgeRetrievalLogStore,
) -> None:
    tenant, other_tenant, kb = uuid4(), uuid4(), uuid4()
    cutoff = _NOW - timedelta(days=30)
    day = 24 * 60
    old = [
        _entry(tenant, (kb,), age_minutes=31 * day),
        _entry(tenant, (kb,), age_minutes=40 * day),
        _entry(other_tenant, (kb,), age_minutes=35 * day),
    ]
    kept = _entry(tenant, (kb,), age_minutes=29 * day)
    for entry in (*old, kept):
        await store.add(entry)

    assert await store.delete_older_than(before=cutoff, limit=2) == 2
    assert await store.delete_older_than(before=cutoff, limit=2) == 1
    assert await store.delete_older_than(before=cutoff, limit=2) == 0
    assert await _ids(store, tenant, kb) == [kept.id]
    assert await _ids(store, other_tenant, kb) == []
