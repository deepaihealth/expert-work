"""Unit tests for InMemoryApprovalStore — Stream J.8 (Mini-ADR J-24)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from expert_work.persistence import InMemoryApprovalStore
from expert_work.protocol import ApprovalRecord, ApprovalStatus


def _record(
    *,
    tenant_id: object = None,
    run_id: object = None,
    timeout_at: datetime | None = None,
) -> ApprovalRecord:
    now = datetime(2026, 5, 22, 12, 0, 0, tzinfo=UTC)
    return ApprovalRecord(
        id=uuid4(),
        tenant_id=tenant_id or uuid4(),  # type: ignore[arg-type]
        run_id=run_id or uuid4(),  # type: ignore[arg-type]
        thread_id=uuid4(),
        request_id="approval:abc",
        node="tools",
        reason_kind="policy_gate",
        action_summary="approval-gated tool 'send_email'",
        proposed_args={"to": "x@example.com"},
        requested_at=now,
        timeout_at=timeout_at or (now + timedelta(hours=24)),
    )


@pytest.mark.asyncio
async def test_create_and_get_by_run() -> None:
    store = InMemoryApprovalStore()
    tenant_id, run_id = uuid4(), uuid4()
    rec = _record(tenant_id=tenant_id, run_id=run_id)
    await store.create(rec)
    fetched = await store.get_by_run(run_id=run_id, tenant_id=tenant_id)
    assert fetched is not None
    assert fetched.run_id == run_id
    assert fetched.status is ApprovalStatus.PENDING


@pytest.mark.asyncio
async def test_get_by_run_cross_tenant_returns_none() -> None:
    store = InMemoryApprovalStore()
    tenant_id, run_id = uuid4(), uuid4()
    await store.create(_record(tenant_id=tenant_id, run_id=run_id))
    # A different tenant probing the same run id sees nothing.
    assert await store.get_by_run(run_id=run_id, tenant_id=uuid4()) is None


@pytest.mark.asyncio
async def test_get_by_run_unknown_returns_none() -> None:
    store = InMemoryApprovalStore()
    assert await store.get_by_run(run_id=uuid4(), tenant_id=uuid4()) is None


@pytest.mark.asyncio
async def test_create_duplicate_run_raises() -> None:
    """A run pauses at most once at a time — a second create is a bug."""
    store = InMemoryApprovalStore()
    run_id = uuid4()
    await store.create(_record(run_id=run_id))
    with pytest.raises(ValueError, match="already exists"):
        await store.create(_record(run_id=run_id))


@pytest.mark.asyncio
async def test_list_expired_returns_pending_past_horizon() -> None:
    store = InMemoryApprovalStore()
    now = datetime.now(UTC)
    await store.create(_record(timeout_at=now - timedelta(hours=2)))  # expired
    await store.create(_record(timeout_at=now + timedelta(hours=2)))  # fresh
    expired = await store.list_expired(before=now)
    assert len(expired) == 1
    assert expired[0].timeout_at < now


@pytest.mark.asyncio
async def test_list_expired_excludes_decided_rows() -> None:
    """Only pending rows count — a decided row past timeout is not re-swept."""
    store = InMemoryApprovalStore()
    now = datetime.now(UTC)
    tenant_id, run_id = uuid4(), uuid4()
    await store.create(
        _record(tenant_id=tenant_id, run_id=run_id, timeout_at=now - timedelta(hours=2))
    )
    await store.mark_decided(
        run_id=run_id,
        tenant_id=tenant_id,
        status=ApprovalStatus.APPROVED,
        decided_by="user-a",
        decided_at=now,
    )
    assert await store.list_expired(before=now) == []


@pytest.mark.asyncio
async def test_mark_decided_flips_status_once() -> None:
    store = InMemoryApprovalStore()
    tenant_id, run_id = uuid4(), uuid4()
    await store.create(_record(tenant_id=tenant_id, run_id=run_id))
    now = datetime.now(UTC)
    hit = await store.mark_decided(
        run_id=run_id,
        tenant_id=tenant_id,
        status=ApprovalStatus.REJECTED,
        decided_by="user-a",
        decided_at=now,
    )
    assert hit is True
    fetched = await store.get_by_run(run_id=run_id, tenant_id=tenant_id)
    assert fetched is not None
    assert fetched.status is ApprovalStatus.REJECTED
    assert fetched.decided_by == "user-a"
    # A second decide is a no-op miss (idempotent-once).
    assert not await store.mark_decided(
        run_id=run_id,
        tenant_id=tenant_id,
        status=ApprovalStatus.APPROVED,
        decided_by="user-b",
        decided_at=now,
    )


@pytest.mark.asyncio
async def test_mark_decided_persists_idempotency_fields() -> None:
    """Stream 13.2 — the CAS writes idempotency_key + continuation_run_id atomically."""
    store = InMemoryApprovalStore()
    tenant_id, run_id, continuation = uuid4(), uuid4(), uuid4()
    await store.create(_record(tenant_id=tenant_id, run_id=run_id))
    hit = await store.mark_decided(
        run_id=run_id,
        tenant_id=tenant_id,
        status=ApprovalStatus.APPROVED,
        decided_by="user-a",
        decided_at=datetime.now(UTC),
        idempotency_key="k1",
        continuation_run_id=continuation,
    )
    assert hit is True
    fetched = await store.get_by_run(run_id=run_id, tenant_id=tenant_id)
    assert fetched is not None
    assert fetched.idempotency_key == "k1"
    assert fetched.continuation_run_id == continuation


@pytest.mark.asyncio
async def test_mark_decided_concurrent_exactly_once() -> None:
    """Stream 13.2 — N concurrent decides on one run → exactly one CAS winner."""
    import asyncio

    store = InMemoryApprovalStore()
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

    results = await asyncio.gather(*(_decide(i) for i in range(16)))
    assert sum(1 for r in results if r) == 1  # exactly one winner


@pytest.mark.asyncio
async def test_mark_decided_cross_tenant_misses() -> None:
    store = InMemoryApprovalStore()
    tenant_id, run_id = uuid4(), uuid4()
    await store.create(_record(tenant_id=tenant_id, run_id=run_id))
    miss = await store.mark_decided(
        run_id=run_id,
        tenant_id=uuid4(),
        status=ApprovalStatus.APPROVED,
        decided_by="attacker",
        decided_at=datetime.now(UTC),
    )
    assert miss is False


@pytest.mark.asyncio
async def test_mark_decided_modified_carries_args() -> None:
    store = InMemoryApprovalStore()
    tenant_id, run_id = uuid4(), uuid4()
    await store.create(_record(tenant_id=tenant_id, run_id=run_id))
    await store.mark_decided(
        run_id=run_id,
        tenant_id=tenant_id,
        status=ApprovalStatus.MODIFIED,
        decided_by="user-a",
        decided_at=datetime.now(UTC),
        modified_args={"to": "safe@example.com"},
    )
    fetched = await store.get_by_run(run_id=run_id, tenant_id=tenant_id)
    assert fetched is not None
    assert fetched.modified_args == {"to": "safe@example.com"}


@pytest.mark.asyncio
async def test_binding_digest_round_trips_on_create() -> None:
    """RT-6 Tier A — the mint digest persists on the row (RT-ADR-19)."""
    store = InMemoryApprovalStore()
    tenant_id, run_id = uuid4(), uuid4()
    rec = _record(tenant_id=tenant_id, run_id=run_id).model_copy(
        update={"binding_digest": "abc123"}
    )
    await store.create(rec)
    fetched = await store.get_by_run(run_id=run_id, tenant_id=tenant_id)
    assert fetched is not None
    assert fetched.binding_digest == "abc123"


@pytest.mark.asyncio
async def test_mark_decided_rebinds_digest_on_modify() -> None:
    """RT-6 Tier A — a modify overwrites the mint digest atomically."""
    store = InMemoryApprovalStore()
    tenant_id, run_id = uuid4(), uuid4()
    await store.create(
        _record(tenant_id=tenant_id, run_id=run_id).model_copy(
            update={"binding_digest": "mint-digest"}
        )
    )
    await store.mark_decided(
        run_id=run_id,
        tenant_id=tenant_id,
        status=ApprovalStatus.MODIFIED,
        decided_by="user-a",
        decided_at=datetime.now(UTC),
        modified_args={"to": "safe@example.com"},
        binding_digest="rebound-digest",
    )
    fetched = await store.get_by_run(run_id=run_id, tenant_id=tenant_id)
    assert fetched is not None
    assert fetched.binding_digest == "rebound-digest"


@pytest.mark.asyncio
async def test_mark_decided_keeps_mint_digest_on_approve() -> None:
    """RT-6 Tier A — approve passes binding_digest=None → mint digest intact."""
    store = InMemoryApprovalStore()
    tenant_id, run_id = uuid4(), uuid4()
    await store.create(
        _record(tenant_id=tenant_id, run_id=run_id).model_copy(
            update={"binding_digest": "mint-digest"}
        )
    )
    await store.mark_decided(
        run_id=run_id,
        tenant_id=tenant_id,
        status=ApprovalStatus.APPROVED,
        decided_by="user-a",
        decided_at=datetime.now(UTC),
    )
    fetched = await store.get_by_run(run_id=run_id, tenant_id=tenant_id)
    assert fetched is not None
    assert fetched.binding_digest == "mint-digest"


# ---------------------------------------------------------------------------
# Stream HX-7 — queue listing (list_for_tenant / list_all_tenants)
# ---------------------------------------------------------------------------


def _record_at(tenant_id: object, *, minutes: int) -> ApprovalRecord:
    base = datetime(2026, 6, 12, 9, 0, 0, tzinfo=UTC)
    rec = _record(tenant_id=tenant_id)
    return rec.model_copy(update={"requested_at": base + timedelta(minutes=minutes)})


@pytest.mark.asyncio
async def test_list_for_tenant_orders_oldest_first_with_total() -> None:
    store = InMemoryApprovalStore()
    tenant = uuid4()
    newer = _record_at(tenant, minutes=30)
    older = _record_at(tenant, minutes=0)
    await store.create(newer)
    await store.create(older)
    await store.create(_record_at(uuid4(), minutes=5))  # another tenant

    items, total = await store.list_for_tenant(tenant_id=tenant, status=ApprovalStatus.PENDING)

    assert total == 2
    assert [r.run_id for r in items] == [older.run_id, newer.run_id]


@pytest.mark.asyncio
async def test_list_for_tenant_filters_status_and_paginates() -> None:
    store = InMemoryApprovalStore()
    tenant = uuid4()
    rows = [_record_at(tenant, minutes=i) for i in range(3)]
    for row in rows:
        await store.create(row)
    await store.mark_decided(
        run_id=rows[0].run_id,
        tenant_id=tenant,
        status=ApprovalStatus.REJECTED,
        decided_by="op",
        decided_at=datetime(2026, 6, 12, 10, 0, 0, tzinfo=UTC),
    )

    pending, total = await store.list_for_tenant(
        tenant_id=tenant, status=ApprovalStatus.PENDING, limit=1, offset=1
    )
    assert total == 2
    assert [r.run_id for r in pending] == [rows[2].run_id]

    rejected, rejected_total = await store.list_for_tenant(
        tenant_id=tenant, status=ApprovalStatus.REJECTED
    )
    assert rejected_total == 1
    assert rejected[0].run_id == rows[0].run_id


@pytest.mark.asyncio
async def test_list_all_tenants_spans_tenants() -> None:
    store = InMemoryApprovalStore()
    a = _record_at(uuid4(), minutes=10)
    b = _record_at(uuid4(), minutes=0)
    await store.create(a)
    await store.create(b)

    items, total = await store.list_all_tenants(status=ApprovalStatus.PENDING)

    assert total == 2
    assert [r.run_id for r in items] == [b.run_id, a.run_id]


# ---------------------------------------------------------------------------
# Deletion-hygiene PR3 — delete_for_threads (purge_session cascade)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_for_threads_scopes_by_tenant_and_thread() -> None:
    store = InMemoryApprovalStore()
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


@pytest.mark.asyncio
async def test_list_filters_by_reason_kinds() -> None:
    """B-20 — ``reason_kinds`` narrows both list methods; ``None`` = all."""
    store = InMemoryApprovalStore()
    tenant = uuid4()
    gate = _record_at(tenant, minutes=0)
    question = _record_at(tenant, minutes=1).model_copy(update={"reason_kind": "missing_info"})
    fork = _record_at(tenant, minutes=2).model_copy(update={"reason_kind": "approach_choice"})
    for row in (gate, question, fork):
        await store.create(row)

    clar, clar_total = await store.list_for_tenant(
        tenant_id=tenant,
        status=ApprovalStatus.PENDING,
        reason_kinds=("approach_choice", "ambiguous_requirement", "missing_info"),
    )
    assert clar_total == 2
    assert [r.run_id for r in clar] == [question.run_id, fork.run_id]

    safety, safety_total = await store.list_all_tenants(
        status=ApprovalStatus.PENDING,
        reason_kinds=("policy_gate", "risk_confirmation"),
    )
    assert safety_total == 1
    assert safety[0].run_id == gate.run_id

    _unfiltered, unfiltered_total = await store.list_for_tenant(
        tenant_id=tenant, status=ApprovalStatus.PENDING
    )
    assert unfiltered_total == 3
