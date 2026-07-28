"""Integration tests for ``SqlMemoryWritebackDLQ`` against Postgres —
W1-PR1 Task 2 (``take_ready`` merges the claim into a single transaction:
``SELECT ... FOR UPDATE SKIP LOCKED`` → ``UPDATE ... RETURNING``).

Mirrors ``test_sql_knowledge_store.py``'s
``test_claim_documents_concurrent_exactly_once`` — the guarantee an
in-memory store cannot prove.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.ext.asyncio import AsyncEngine
from testcontainers.postgres import PostgresContainer

from expert_work.persistence import (
    DatabaseConfig,
    create_async_engine_from_config,
    create_async_session_factory,
)
from expert_work.persistence.memory.dlq import SqlMemoryWritebackDLQ

pytestmark = pytest.mark.integration

ALEMBIC_INI = Path(__file__).resolve().parent.parent / "alembic.ini"

SqlDlqFixture = tuple[SqlMemoryWritebackDLQ, AsyncEngine]

#: Comfortably past the 600s claim lease, for asserting a deleted/settled
#: row never resurfaces even once the lease would otherwise have elapsed.
_PAST_LEASE_S = 700


def _sync_dsn(container: PostgresContainer) -> str:
    url = str(container.get_connection_url())
    return url.replace("+psycopg2", "+psycopg").replace("postgresql://", "postgresql+psycopg://", 1)


def _async_dsn(container: PostgresContainer) -> str:
    url = str(container.get_connection_url())
    return url.replace("+psycopg2", "+asyncpg").replace("postgresql://", "postgresql+asyncpg://", 1)


@pytest.fixture
def sql_dlq(postgres_container: PostgresContainer) -> Iterator[SqlDlqFixture]:
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", _sync_dsn(postgres_container))
    command.upgrade(cfg, "head")

    engine = create_async_engine_from_config(DatabaseConfig(dsn=_async_dsn(postgres_container)))
    session_factory = create_async_session_factory(engine)
    yield SqlMemoryWritebackDLQ(session_factory), engine


# ``take_ready`` is intentionally cross-tenant, so assertions below scope to
# this test's own seeded row ids and never assume the claimable set is
# empty — other tests in the same container leave rows behind.


@pytest.mark.asyncio
async def test_take_ready_concurrent_claims_disjoint(sql_dlq: SqlDlqFixture) -> None:
    # The CAS + SKIP LOCKED claim must hand each row to exactly one
    # concurrent claimer, and count exactly one attempt per row — never
    # zero, never doubled.
    dlq, engine = sql_dlq
    try:
        tenant, user = uuid4(), uuid4()
        seeded = set()
        for i in range(10):
            row = await dlq.enqueue(
                tenant_id=tenant,
                user_id=user,
                source_thread_id=f"t-{i}",
                extracted=[("fact", f"x{i}")],
                error="boom",
            )
            seeded.add(row.id)
        now = datetime.now(UTC)
        # Ample per-claimer capacity so every claimable row (incl. unrelated
        # leftovers from other tests) is taken — what matters is each is
        # taken exactly once.
        batches = await asyncio.gather(*[dlq.take_ready(limit=50, now=now) for _ in range(5)])
        claimed_rows = [r for batch in batches for r in batch]
        claimed_ids = [r.id for r in claimed_rows]
        assert len(claimed_ids) == len(set(claimed_ids))  # never claimed twice, anywhere
        claimed_seeded = [r for r in claimed_rows if r.id in seeded]
        assert sorted(r.id for r in claimed_seeded) == sorted(seeded)
        # Attempts bumped to exactly 1 for every seeded row — not doubled.
        assert all(r.attempts == 1 for r in claimed_seeded)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_take_ready_pushes_lease_and_hides_row_until_it_elapses(
    sql_dlq: SqlDlqFixture,
) -> None:
    dlq, engine = sql_dlq
    try:
        tenant, user = uuid4(), uuid4()
        row = await dlq.enqueue(
            tenant_id=tenant,
            user_id=user,
            source_thread_id="t",
            extracted=[("fact", "x")],
            error="boom",
        )
        now = datetime.now(UTC)

        [claimed] = await dlq.take_ready(limit=10, now=now)
        assert claimed.id == row.id
        assert claimed.attempts == 1
        delta = (claimed.next_retry_at - now).total_seconds()
        assert 590 <= delta <= 610, delta

        # Still leased — a fresh claim right away must not see it again.
        again_now = await dlq.take_ready(limit=10, now=now)
        assert row.id not in {r.id for r in again_now}

        # A sweep after the lease elapses reclaims it — attempts
        # monotonically +1 (a crashed-worker recovery is a second
        # attempt), never doubled to 3+. limit=200 for headroom: this
        # crosses the 600s mark of any other test's own claimed rows in
        # the shared container too, so more than just our one row may be
        # newly eligible here.
        later = now + timedelta(seconds=601)
        reclaimed_batch = await dlq.take_ready(limit=200, now=later)
        [reclaimed] = [r for r in reclaimed_batch if r.id == row.id]
        assert reclaimed.attempts == 2
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_record_failure_does_not_bump_attempts(sql_dlq: SqlDlqFixture) -> None:
    """W1-PR1 Task 2: attempts are only counted at claim time
    (``take_ready``); ``record_failure`` just settles the error and
    reschedules — it must not also bump ``attempts``."""
    dlq, engine = sql_dlq
    try:
        tenant, user = uuid4(), uuid4()
        row = await dlq.enqueue(
            tenant_id=tenant,
            user_id=user,
            source_thread_id="t",
            extracted=[("fact", "x")],
            error="boom",
        )
        now = datetime.now(UTC)
        [claimed] = await dlq.take_ready(limit=10, now=now)
        assert claimed.attempts == 1

        await dlq.record_failure(
            row_id=row.id,
            error="retry-error",
            when=now,
            next_retry_at=now + timedelta(seconds=60),
        )

        later_batch = await dlq.take_ready(limit=10, now=now + timedelta(seconds=61))
        [again] = [r for r in later_batch if r.id == row.id]
        assert again.attempts == 2  # bumped by this claim, not by record_failure
        assert again.last_error == "retry-error"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_mark_done_deletes_the_row(sql_dlq: SqlDlqFixture) -> None:
    dlq, engine = sql_dlq
    try:
        tenant, user = uuid4(), uuid4()
        row = await dlq.enqueue(
            tenant_id=tenant,
            user_id=user,
            source_thread_id="t",
            extracted=[("fact", "x")],
            error="boom",
        )
        now = datetime.now(UTC)
        [claimed] = await dlq.take_ready(limit=10, now=now)
        assert claimed.id == row.id

        await dlq.mark_done(row_id=row.id)

        far_future = now + timedelta(seconds=_PAST_LEASE_S)
        far_future_batch = await dlq.take_ready(limit=10, now=far_future)
        assert row.id not in {r.id for r in far_future_batch}
    finally:
        await engine.dispose()
