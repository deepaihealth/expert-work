"""Unit tests for :class:`InMemoryEvalRunStore` — P1-S2.1.

Pins the store contract the resident ``EvalWorker`` relies on: status
machine timestamps, tenant isolation, cross-tenant queued scan, and
case-result append/list. The Postgres backend is covered separately
against a real PG (integration).
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from expert_work.persistence import InMemoryEvalRunStore
from expert_work.protocol import (
    EvalCaseResultRecord,
    EvalRunRecord,
    EvalRunStatus,
    EvalTriggeredBy,
)


def _run(
    tenant_id: object,
    *,
    status: EvalRunStatus = EvalRunStatus.QUEUED,
    created_at: datetime | None = None,
    run_id: object | None = None,
) -> EvalRunRecord:
    return EvalRunRecord(
        id=run_id if run_id is not None else uuid4(),  # type: ignore[arg-type]
        tenant_id=tenant_id,  # type: ignore[arg-type]
        suite="m0_baseline",
        status=status,
        triggered_by=EvalTriggeredBy.MANUAL,
        created_at=created_at if created_at is not None else datetime.now(UTC),
    )


@pytest.mark.asyncio
async def test_create_and_get_is_tenant_scoped() -> None:
    store = InMemoryEvalRunStore()
    tenant_a, tenant_b = uuid4(), uuid4()
    run = await store.create_run(_run(tenant_a))

    assert await store.get_run(run_id=run.id, tenant_id=tenant_a) == run
    # Cross-tenant read must miss.
    assert await store.get_run(run_id=run.id, tenant_id=tenant_b) is None


@pytest.mark.asyncio
async def test_set_status_stamps_timestamps_and_summary() -> None:
    store = InMemoryEvalRunStore()
    tenant = uuid4()
    run = await store.create_run(_run(tenant))
    assert run.started_at is None and run.finished_at is None

    assert await store.set_status(run_id=run.id, tenant_id=tenant, status=EvalRunStatus.RUNNING)
    running = await store.get_run(run_id=run.id, tenant_id=tenant)
    assert running is not None
    assert running.status is EvalRunStatus.RUNNING
    assert running.started_at is not None and running.finished_at is None

    summary: dict[str, object] = {"pass_count": 15, "total": 15}
    assert await store.set_status(
        run_id=run.id, tenant_id=tenant, status=EvalRunStatus.PASSED, summary=summary
    )
    done = await store.get_run(run_id=run.id, tenant_id=tenant)
    assert done is not None
    assert done.status is EvalRunStatus.PASSED
    assert done.finished_at is not None
    assert done.summary == summary


@pytest.mark.asyncio
async def test_set_status_cross_tenant_is_noop() -> None:
    store = InMemoryEvalRunStore()
    tenant, other = uuid4(), uuid4()
    run = await store.create_run(_run(tenant))
    assert (
        await store.set_status(run_id=run.id, tenant_id=other, status=EvalRunStatus.RUNNING)
        is False
    )


@pytest.mark.asyncio
async def test_list_by_status_all_tenants_spans_tenants_oldest_first() -> None:
    store = InMemoryEvalRunStore()
    a, b = uuid4(), uuid4()
    r1 = await store.create_run(_run(a))
    r2 = await store.create_run(_run(b))
    # One run advances out of queued — must drop from the queued scan.
    await store.set_status(run_id=r2.id, tenant_id=b, status=EvalRunStatus.RUNNING)

    queued = await store.list_by_status_all_tenants(EvalRunStatus.QUEUED)
    assert [r.id for r in queued] == [r1.id]
    running = await store.list_by_status_all_tenants(EvalRunStatus.RUNNING)
    assert [r.id for r in running] == [r2.id]


@pytest.mark.asyncio
async def test_list_for_tenant_isolates_tenant_and_sorts_desc() -> None:
    store = InMemoryEvalRunStore()
    a, b = uuid4(), uuid4()
    base = datetime(2026, 6, 14, 8, 0, tzinfo=UTC)
    older = await store.create_run(_run(a, created_at=base))
    newer = await store.create_run(_run(a, created_at=base.replace(hour=9)))
    await store.create_run(_run(b, created_at=base.replace(hour=10)))  # other tenant

    items, total = await store.list_for_tenant(tenant_id=a)
    # Only tenant a's runs, newest first.
    assert [r.id for r in items] == [newer.id, older.id]
    assert total == 2


@pytest.mark.asyncio
async def test_list_for_tenant_filters_by_status() -> None:
    store = InMemoryEvalRunStore()
    a = uuid4()
    queued = await store.create_run(_run(a, status=EvalRunStatus.QUEUED))
    await store.create_run(_run(a, status=EvalRunStatus.PASSED))

    items, total = await store.list_for_tenant(tenant_id=a, status=EvalRunStatus.QUEUED)
    assert [r.id for r in items] == [queued.id]
    assert total == 1


@pytest.mark.asyncio
async def test_list_for_tenant_paginates_with_full_total() -> None:
    store = InMemoryEvalRunStore()
    a = uuid4()
    base = datetime(2026, 6, 14, 8, 0, tzinfo=UTC)
    for i in range(5):
        await store.create_run(_run(a, created_at=base.replace(minute=i)))

    page, total = await store.list_for_tenant(tenant_id=a, limit=2, offset=0)
    assert len(page) == 2
    # total is the pre-pagination count, not the page size.
    assert total == 5
    page2, _ = await store.list_for_tenant(tenant_id=a, limit=2, offset=4)
    assert len(page2) == 1  # tail page


@pytest.mark.asyncio
async def test_list_all_tenants_id_tiebreak_on_equal_created_at() -> None:
    """W4 review C-3 — ``created_at`` ties order by ``id`` ASC (``.int``), in
    both the aggregate and the per-tenant sibling (C-7). Rows are inserted in
    *descending* ``id.int`` order so a dropped tiebreak degrades to dict
    insertion order and the exact-order assertion goes red."""
    store = InMemoryEvalRunStore()
    tenant = uuid4()
    ts = datetime(2026, 6, 14, 8, 0, tzinfo=UTC)
    id_lo, id_hi = sorted((uuid4(), uuid4()), key=lambda u: u.int)
    for run_id in (id_hi, id_lo):  # descending insertion
        await store.create_run(_run(tenant, created_at=ts, run_id=run_id))

    items, total = await store.list_all_tenants()
    assert (total, [r.id for r in items]) == (2, [id_lo, id_hi])

    # C-7 — the per-tenant sibling applies the same tiebreak.
    sibling, _ = await store.list_for_tenant(tenant_id=tenant)
    assert [r.id for r in sibling] == [id_lo, id_hi]


@pytest.mark.asyncio
async def test_append_and_list_case_results() -> None:
    store = InMemoryEvalRunStore()
    tenant = uuid4()
    run = await store.create_run(_run(tenant))

    c1 = await store.append_case_result(
        EvalCaseResultRecord(
            run_id=run.id,
            tenant_id=tenant,
            capability="J.1_plan_execute",
            case_id="case-1",
            passed=True,
            scores={"pass_rate": 1.0},
        )
    )
    c2 = await store.append_case_result(
        EvalCaseResultRecord(
            run_id=run.id,
            tenant_id=tenant,
            capability="J.2_reflect",
            case_id="case-2",
            passed=False,
        )
    )
    # Bigserial ids are assigned on append.
    assert c1.id == 1 and c2.id == 2

    results = await store.list_case_results(run_id=run.id, tenant_id=tenant)
    assert [r.case_id for r in results] == ["case-1", "case-2"]
    # Cross-tenant read of the same run id returns nothing.
    assert await store.list_case_results(run_id=run.id, tenant_id=uuid4()) == []


@pytest.mark.asyncio
async def test_claim_cas_exactly_one_winner() -> None:
    """Stream 9.5 — two workers race a queued eval; the CAS lets one win."""
    store = InMemoryEvalRunStore()
    tenant = uuid4()
    run = await store.create_run(_run(tenant))

    first = await store.claim(run_id=run.id, tenant_id=tenant)
    second = await store.claim(run_id=run.id, tenant_id=tenant)
    assert first is True
    assert second is False  # already running — the loser skips
    row = await store.get_run(run_id=run.id, tenant_id=tenant)
    assert row is not None and row.status is EvalRunStatus.RUNNING


@pytest.mark.asyncio
async def test_claim_cross_tenant_is_noop() -> None:
    store = InMemoryEvalRunStore()
    tenant, other = uuid4(), uuid4()
    run = await store.create_run(_run(tenant))
    assert await store.claim(run_id=run.id, tenant_id=other) is False
