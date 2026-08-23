"""Integration tests for :class:`SqlApprovalStore` against a real Postgres.

Stream 13.2 — the concurrent-resume race is gated by ``mark_decided`` being an
atomic conditional UPDATE (``WHERE status='pending'``). These tests prove the
DB-level CAS under TRUE concurrency (asyncio.gather over a real connection
pool) — exactly one decide wins, and the winner's idempotency_key +
continuation_run_id persist for replay. The in-memory store covers the
single-event-loop path; only Postgres proves the row-lock serialisation.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.ext.asyncio import AsyncEngine
from testcontainers.postgres import PostgresContainer

from expert_work.persistence import (
    DatabaseConfig,
    SqlApprovalStore,
    create_async_engine_from_config,
    create_async_session_factory,
)
from expert_work.protocol import ApprovalRecord, ApprovalStatus

pytestmark = pytest.mark.integration

ALEMBIC_INI = Path(__file__).resolve().parent.parent / "alembic.ini"


def _sync_dsn(container: PostgresContainer) -> str:
    url = str(container.get_connection_url())
    return url.replace("+psycopg2", "+psycopg").replace("postgresql://", "postgresql+psycopg://", 1)


def _async_dsn(container: PostgresContainer) -> str:
    url = str(container.get_connection_url())
    return url.replace("+psycopg2", "+asyncpg").replace("postgresql://", "postgresql+asyncpg://", 1)


ApprovalStoreFixture = tuple[SqlApprovalStore, AsyncEngine]


@pytest.fixture
def approval_store(postgres_container: PostgresContainer) -> Iterator[ApprovalStoreFixture]:
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", _sync_dsn(postgres_container))
    command.upgrade(cfg, "head")

    engine = create_async_engine_from_config(DatabaseConfig(dsn=_async_dsn(postgres_container)))
    session_factory = create_async_session_factory(engine)
    yield SqlApprovalStore(session_factory), engine


def _record(*, tenant_id: UUID, run_id: UUID) -> ApprovalRecord:
    now = datetime.now(UTC)
    return ApprovalRecord(
        id=uuid4(),
        tenant_id=tenant_id,
        run_id=run_id,
        thread_id=uuid4(),
        request_id="approval:race",
        node="tools",
        reason_kind="policy_gate",
        action_summary="approval-gated tool 'http'",
        proposed_args={"url": "https://example.com"},
        requested_at=now,
        timeout_at=now + timedelta(hours=24),
        status=ApprovalStatus.PENDING,
    )


@pytest.mark.asyncio
async def test_concurrent_mark_decided_exactly_one_winner(
    approval_store: ApprovalStoreFixture,
) -> None:
    store, engine = approval_store
    try:
        tenant_id, run_id = uuid4(), uuid4()
        await store.create(_record(tenant_id=tenant_id, run_id=run_id))
        now = datetime.now(UTC)

        async def _decide(n: int) -> bool:
            return await store.mark_decided(
                run_id=run_id,
                tenant_id=tenant_id,
                status=ApprovalStatus.APPROVED,
                decided_by=f"user-{n}",
                decided_at=now,
                idempotency_key=f"key-{n}",
                continuation_run_id=uuid4(),
            )

        # True DB concurrency — the row-level lock serialises the conditional
        # UPDATEs; exactly one sees status='pending' and updates a row.
        results = await asyncio.gather(*(_decide(i) for i in range(16)))
        assert sum(1 for r in results if r) == 1

        # The winner's idempotency fields survived (one decide, one continuation).
        decided = await store.get_by_run(run_id=run_id, tenant_id=tenant_id)
        assert decided is not None
        assert decided.status is ApprovalStatus.APPROVED
        assert decided.idempotency_key is not None
        assert decided.continuation_run_id is not None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_idempotency_fields_round_trip(
    approval_store: ApprovalStoreFixture,
) -> None:
    store, engine = approval_store
    try:
        tenant_id, run_id, continuation = uuid4(), uuid4(), uuid4()
        await store.create(_record(tenant_id=tenant_id, run_id=run_id))
        hit = await store.mark_decided(
            run_id=run_id,
            tenant_id=tenant_id,
            status=ApprovalStatus.APPROVED,
            decided_by="u",
            decided_at=datetime.now(UTC),
            idempotency_key="resume-key",
            continuation_run_id=continuation,
        )
        assert hit is True
        row = await store.get_by_run(run_id=run_id, tenant_id=tenant_id)
        assert row is not None
        assert row.idempotency_key == "resume-key"
        assert row.continuation_run_id == continuation
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_binding_digest_round_trip_and_rebind(
    approval_store: ApprovalStoreFixture,
) -> None:
    """RT-6 Tier A — binding_digest persists on create and re-binds on modify."""
    store, engine = approval_store
    try:
        tenant_id, run_id = uuid4(), uuid4()
        rec = _record(tenant_id=tenant_id, run_id=run_id).model_copy(
            update={"binding_digest": "mint-digest"}
        )
        await store.create(rec)
        fetched = await store.get_by_run(run_id=run_id, tenant_id=tenant_id)
        assert fetched is not None
        assert fetched.binding_digest == "mint-digest"

        # A modify overwrites the digest atomically with the CAS.
        hit = await store.mark_decided(
            run_id=run_id,
            tenant_id=tenant_id,
            status=ApprovalStatus.MODIFIED,
            decided_by="u",
            decided_at=datetime.now(UTC),
            modified_args={"url": "https://safe.example.com"},
            binding_digest="rebound-digest",
        )
        assert hit is True
        row = await store.get_by_run(run_id=run_id, tenant_id=tenant_id)
        assert row is not None
        assert row.binding_digest == "rebound-digest"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_delete_for_threads_scopes_by_tenant_and_thread(
    approval_store: ApprovalStoreFixture,
) -> None:
    """Deletion-hygiene PR3 — the purge_session cascade deletes by (tenant, thread)."""
    store, engine = approval_store
    try:
        tenant_a, tenant_b = uuid4(), uuid4()
        thread_1, thread_2 = uuid4(), uuid4()
        run_a1, run_a2, run_b1 = uuid4(), uuid4(), uuid4()
        await store.create(
            _record(tenant_id=tenant_a, run_id=run_a1).model_copy(update={"thread_id": thread_1})
        )
        await store.create(
            _record(tenant_id=tenant_a, run_id=run_a2).model_copy(update={"thread_id": thread_2})
        )
        await store.create(
            _record(tenant_id=tenant_b, run_id=run_b1).model_copy(update={"thread_id": thread_1})
        )

        assert await store.delete_for_threads(thread_ids=[thread_1], tenant_id=tenant_a) == 1
        assert await store.delete_for_threads(thread_ids=[], tenant_id=tenant_a) == 0

        # Tenant A's thread-1 row is gone; the other thread / other tenant survive.
        assert await store.get_by_run(run_id=run_a1, tenant_id=tenant_a) is None
        assert await store.get_by_run(run_id=run_a2, tenant_id=tenant_a) is not None
        assert await store.get_by_run(run_id=run_b1, tenant_id=tenant_b) is not None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_list_filters_by_reason_kinds(
    approval_store: ApprovalStoreFixture,
) -> None:
    """B-20 — the SQL ``reason_kinds`` predicate matches the in-memory one."""
    store, engine = approval_store
    tenant = uuid4()
    base = datetime(2026, 6, 12, 9, 0, 0, tzinfo=UTC)
    gate = _record(tenant_id=tenant, run_id=uuid4()).model_copy(update={"requested_at": base})
    question = _record(tenant_id=tenant, run_id=uuid4()).model_copy(
        update={"reason_kind": "missing_info", "requested_at": base + timedelta(minutes=1)}
    )
    fork = _record(tenant_id=tenant, run_id=uuid4()).model_copy(
        update={"reason_kind": "approach_choice", "requested_at": base + timedelta(minutes=2)}
    )
    try:
        for row in (gate, question, fork):
            await store.create(row)

        clar, clar_total = await store.list_for_tenant(
            tenant_id=tenant,
            status=ApprovalStatus.PENDING,
            reason_kinds=("approach_choice", "ambiguous_requirement", "missing_info"),
        )
        assert clar_total == 2
        assert [r.run_id for r in clar] == [question.run_id, fork.run_id]

        safety, safety_total = await store.list_for_tenant(
            tenant_id=tenant,
            status=ApprovalStatus.PENDING,
            reason_kinds=("policy_gate", "risk_confirmation"),
        )
        assert safety_total == 1
        assert safety[0].run_id == gate.run_id

        # I-5 (B-20 终审) — 跨租户平台面走的是 list_all_tenants,它的
        # reason_kinds 谓词必须与 list_for_tenant 同义,单独覆盖。
        clar_all, clar_all_total = await store.list_all_tenants(
            status=ApprovalStatus.PENDING,
            reason_kinds=("approach_choice", "ambiguous_requirement", "missing_info"),
        )
        clar_ids = {r.run_id for r in clar_all}
        assert {question.run_id, fork.run_id} <= clar_ids
        assert gate.run_id not in clar_ids
        assert clar_all_total >= 2

        _all_rows, all_total = await store.list_all_tenants(status=ApprovalStatus.PENDING)
        assert all_total >= 3
    finally:
        await engine.dispose()
