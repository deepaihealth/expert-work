"""Unit tests for the in-memory webhook stores — HX-9 (STREAM-HX § 13)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from expert_work.persistence import (
    InMemoryWebhookDeliveryStore,
    InMemoryWebhookEndpointStore,
)
from expert_work.protocol import (
    WebhookDeliveryRecord,
    WebhookDeliveryStatus,
    WebhookEndpointRecord,
    WebhookEventType,
)

_BASE = datetime(2026, 6, 13, 12, 0, 0, tzinfo=UTC)


def _endpoint(
    *,
    endpoint_id: UUID | None = None,
    tenant_id: UUID | None = None,
    name: str = "ops-notify",
    agent_name: str | None = None,
    event_types: tuple[WebhookEventType, ...] = ("run.completed",),
    enabled: bool = True,
) -> WebhookEndpointRecord:
    return WebhookEndpointRecord(
        id=endpoint_id or uuid4(),
        tenant_id=tenant_id or uuid4(),
        name=name,
        url="https://hooks.example.com/ingest",
        event_types=event_types,
        agent_name=agent_name,
        secret_ref="webhook-endpoint/abc",
        enabled=enabled,
        source="api",
        created_at=_BASE,
        updated_at=_BASE,
    )


# --- InMemoryWebhookEndpointStore -----------------------------------------


@pytest.mark.asyncio
async def test_endpoint_create_then_get_round_trips() -> None:
    store = InMemoryWebhookEndpointStore()
    eid, tenant = uuid4(), uuid4()
    await store.create(_endpoint(endpoint_id=eid, tenant_id=tenant))

    fetched = await store.get(endpoint_id=eid, tenant_id=tenant)
    assert fetched is not None
    assert fetched.id == eid
    assert fetched.event_types == ("run.completed",)


@pytest.mark.asyncio
async def test_endpoint_get_unknown_and_cross_tenant_return_none() -> None:
    store = InMemoryWebhookEndpointStore()
    eid, tenant_a, tenant_b = uuid4(), uuid4(), uuid4()
    await store.create(_endpoint(endpoint_id=eid, tenant_id=tenant_a))

    assert await store.get(endpoint_id=uuid4(), tenant_id=tenant_a) is None
    assert await store.get(endpoint_id=eid, tenant_id=tenant_b) is None


@pytest.mark.asyncio
async def test_endpoint_duplicate_name_per_tenant_raises() -> None:
    store = InMemoryWebhookEndpointStore()
    tenant = uuid4()
    await store.create(_endpoint(tenant_id=tenant, name="ops-notify"))
    with pytest.raises(ValueError, match="already exists"):
        await store.create(_endpoint(tenant_id=tenant, name="ops-notify"))


@pytest.mark.asyncio
async def test_endpoint_same_name_different_tenant_allowed() -> None:
    store = InMemoryWebhookEndpointStore()
    await store.create(_endpoint(tenant_id=uuid4(), name="ops-notify"))
    await store.create(_endpoint(tenant_id=uuid4(), name="ops-notify"))


@pytest.mark.asyncio
async def test_endpoint_list_by_tenant_agent_filter() -> None:
    store = InMemoryWebhookEndpointStore()
    tenant = uuid4()
    await store.create(_endpoint(tenant_id=tenant, name="all", agent_name=None))
    await store.create(_endpoint(tenant_id=tenant, name="scoped", agent_name="reporter"))

    assert len(await store.list_by_tenant(tenant_id=tenant)) == 2
    scoped = await store.list_by_tenant(tenant_id=tenant, agent_name="reporter")
    assert [e.name for e in scoped] == ["scoped"]


@pytest.mark.asyncio
async def test_endpoint_list_enabled_all_tenants_is_cross_tenant() -> None:
    store = InMemoryWebhookEndpointStore()
    await store.create(_endpoint(tenant_id=uuid4(), name="e1", enabled=True))
    await store.create(_endpoint(tenant_id=uuid4(), name="e2", enabled=True))
    await store.create(_endpoint(tenant_id=uuid4(), name="e3", enabled=False))

    listed = await store.list_enabled_all_tenants()
    assert {e.name for e in listed} == {"e1", "e2"}


@pytest.mark.asyncio
async def test_endpoint_update_replaces_and_cross_tenant_misses() -> None:
    store = InMemoryWebhookEndpointStore()
    eid, tenant_a, tenant_b = uuid4(), uuid4(), uuid4()
    await store.create(_endpoint(endpoint_id=eid, tenant_id=tenant_a, enabled=True))

    rec = await store.get(endpoint_id=eid, tenant_id=tenant_a)
    assert rec is not None
    updated = await store.update(rec.model_copy(update={"enabled": False}))
    assert updated is True
    again = await store.get(endpoint_id=eid, tenant_id=tenant_a)
    assert again is not None and again.enabled is False

    impostor = again.model_copy(update={"tenant_id": tenant_b})
    cross = await store.update(impostor)
    assert cross is False


@pytest.mark.asyncio
async def test_endpoint_delete_and_count() -> None:
    store = InMemoryWebhookEndpointStore()
    eid, tenant = uuid4(), uuid4()
    await store.create(_endpoint(endpoint_id=eid, tenant_id=tenant, name="a"))
    await store.create(_endpoint(tenant_id=tenant, name="b"))

    assert await store.count_by_tenant(tenant_id=tenant) == 2
    deleted = await store.delete(endpoint_id=eid, tenant_id=tenant)
    assert deleted is True
    assert await store.get(endpoint_id=eid, tenant_id=tenant) is None
    deleted_again = await store.delete(endpoint_id=eid, tenant_id=tenant)
    assert deleted_again is False
    assert await store.count_by_tenant(tenant_id=tenant) == 1


# --- InMemoryWebhookDeliveryStore -----------------------------------------


def _delivery(
    *,
    delivery_id: UUID | None = None,
    tenant_id: UUID | None = None,
    endpoint_id: UUID | None = None,
    event_id: str = "run:abc",
    event_type: WebhookEventType = "run.completed",
    status: WebhookDeliveryStatus = WebhookDeliveryStatus.PENDING,
    next_retry_at: datetime | None = None,
    created_at: datetime = _BASE,
) -> WebhookDeliveryRecord:
    return WebhookDeliveryRecord(
        id=delivery_id or uuid4(),
        tenant_id=tenant_id or uuid4(),
        endpoint_id=endpoint_id or uuid4(),
        event_id=event_id,
        event_type=event_type,
        run_id=uuid4(),
        payload={"run_id": "abc"},
        status=status,
        attempt=0,
        next_retry_at=next_retry_at,
        created_at=created_at,
        updated_at=created_at,
    )


@pytest.mark.asyncio
async def test_delivery_create_then_get_round_trips() -> None:
    store = InMemoryWebhookDeliveryStore()
    did, tenant = uuid4(), uuid4()
    await store.create(_delivery(delivery_id=did, tenant_id=tenant))

    fetched = await store.get(delivery_id=did, tenant_id=tenant)
    assert fetched is not None
    assert fetched.id == did
    assert fetched.status is WebhookDeliveryStatus.PENDING


@pytest.mark.asyncio
async def test_delivery_dedup_on_endpoint_and_event() -> None:
    store = InMemoryWebhookDeliveryStore()
    endpoint = uuid4()
    await store.create(_delivery(endpoint_id=endpoint, event_id="run:x"))
    with pytest.raises(ValueError, match="already exists"):
        await store.create(_delivery(endpoint_id=endpoint, event_id="run:x"))
    # Same event_id on a different endpoint is a distinct delivery.
    await store.create(_delivery(endpoint_id=uuid4(), event_id="run:x"))


@pytest.mark.asyncio
async def test_delivery_exists_for_event() -> None:
    store = InMemoryWebhookDeliveryStore()
    endpoint = uuid4()
    await store.create(_delivery(endpoint_id=endpoint, event_id="run:y"))

    assert await store.exists_for_event(endpoint_id=endpoint, event_id="run:y") is True
    assert await store.exists_for_event(endpoint_id=endpoint, event_id="run:z") is False
    assert await store.exists_for_event(endpoint_id=uuid4(), event_id="run:y") is False


@pytest.mark.asyncio
async def test_delivery_get_cross_tenant_returns_none() -> None:
    store = InMemoryWebhookDeliveryStore()
    did, tenant_a, tenant_b = uuid4(), uuid4(), uuid4()
    await store.create(_delivery(delivery_id=did, tenant_id=tenant_a))
    assert await store.get(delivery_id=did, tenant_id=tenant_b) is None


@pytest.mark.asyncio
async def test_delivery_update_transitions_and_cross_tenant_misses() -> None:
    store = InMemoryWebhookDeliveryStore()
    did, tenant_a, tenant_b = uuid4(), uuid4(), uuid4()
    await store.create(_delivery(delivery_id=did, tenant_id=tenant_a))

    rec = await store.get(delivery_id=did, tenant_id=tenant_a)
    assert rec is not None
    done = rec.model_copy(
        update={"status": WebhookDeliveryStatus.DELIVERED, "attempt": 1, "response_status": 200}
    )
    updated = await store.update(done)
    assert updated is True
    again = await store.get(delivery_id=did, tenant_id=tenant_a)
    assert again is not None and again.status is WebhookDeliveryStatus.DELIVERED

    impostor = again.model_copy(update={"tenant_id": tenant_b})
    cross = await store.update(impostor)
    assert cross is False


@pytest.mark.asyncio
async def test_delivery_list_by_endpoint_filters_and_sorts() -> None:
    store = InMemoryWebhookDeliveryStore()
    tenant, endpoint = uuid4(), uuid4()
    await store.create(
        _delivery(tenant_id=tenant, endpoint_id=endpoint, event_id="e1", created_at=_BASE)
    )
    await store.create(
        _delivery(
            tenant_id=tenant,
            endpoint_id=endpoint,
            event_id="e2",
            created_at=_BASE.replace(hour=13),
        )
    )
    await store.create(_delivery(tenant_id=tenant, endpoint_id=uuid4(), event_id="e3"))

    listed = await store.list_by_endpoint(endpoint_id=endpoint, tenant_id=tenant)
    assert len(listed) == 2
    assert listed[0].created_at > listed[1].created_at  # newest first


@pytest.mark.asyncio
async def test_delivery_list_ready_pending_now_and_due_retries() -> None:
    store = InMemoryWebhookDeliveryStore()
    now = datetime(2026, 6, 13, 15, 0, 0, tzinfo=UTC)
    await store.create(_delivery(event_id="p1", status=WebhookDeliveryStatus.PENDING))
    await store.create(
        _delivery(
            event_id="r-due",
            status=WebhookDeliveryStatus.RETRYING,
            next_retry_at=now - timedelta(minutes=1),
        )
    )
    await store.create(
        _delivery(
            event_id="r-future",
            status=WebhookDeliveryStatus.RETRYING,
            next_retry_at=now + timedelta(hours=1),
        )
    )
    await store.create(_delivery(event_id="done", status=WebhookDeliveryStatus.DELIVERED))
    await store.create(_delivery(event_id="dead", status=WebhookDeliveryStatus.DEAD_LETTER))

    ready = await store.list_ready(before=now)
    assert {d.event_id for d in ready} == {"p1", "r-due"}


# --- claim_ready (W1-PR1 CAS) -----------------------------------------------


@pytest.mark.asyncio
async def test_claim_ready_matches_list_ready_set_and_writes_delivering() -> None:
    store = InMemoryWebhookDeliveryStore()
    now = datetime(2026, 6, 13, 15, 0, 0, tzinfo=UTC)
    p1 = _delivery(event_id="p1", status=WebhookDeliveryStatus.PENDING)
    await store.create(p1)
    await store.create(
        _delivery(
            event_id="r-due",
            status=WebhookDeliveryStatus.RETRYING,
            next_retry_at=now - timedelta(minutes=1),
        )
    )
    await store.create(
        _delivery(
            event_id="r-future",
            status=WebhookDeliveryStatus.RETRYING,
            next_retry_at=now + timedelta(hours=1),
        )
    )
    await store.create(_delivery(event_id="done", status=WebhookDeliveryStatus.DELIVERED))
    await store.create(_delivery(event_id="dead", status=WebhookDeliveryStatus.DEAD_LETTER))

    claimed = await store.claim_ready(before=now)
    assert {d.event_id for d in claimed} == {"p1", "r-due"}
    assert all(d.status is WebhookDeliveryStatus.DELIVERING for d in claimed)

    # The store's own row is flipped too, not just the returned copy.
    refetched = await store.get(delivery_id=p1.id, tenant_id=p1.tenant_id)
    assert refetched is not None and refetched.status is WebhookDeliveryStatus.DELIVERING

    # A second claim right away finds nothing left — both rows are now
    # ``delivering`` (not ``pending``/due-``retrying``) and freshly touched.
    assert await store.claim_ready(before=now) == []


@pytest.mark.asyncio
async def test_claim_ready_reclaims_stale_delivering_not_fresh() -> None:
    store = InMemoryWebhookDeliveryStore()
    now = datetime(2026, 6, 13, 17, 0, 0, tzinfo=UTC)
    stale = _delivery(
        event_id="c-stale",
        status=WebhookDeliveryStatus.DELIVERING,
        created_at=now - timedelta(hours=1),
    ).model_copy(update={"updated_at": now - timedelta(seconds=301)})
    fresh = _delivery(
        event_id="c-fresh",
        status=WebhookDeliveryStatus.DELIVERING,
        created_at=now - timedelta(hours=1),
    ).model_copy(update={"updated_at": now - timedelta(seconds=10)})
    await store.create(stale)
    await store.create(fresh)

    claimed = {d.event_id for d in await store.claim_ready(before=now)}
    assert "c-stale" in claimed  # past the _DELIVERING_STALE_S = 300s window — reclaimed
    assert "c-fresh" not in claimed  # inside the window — still owned by its claimer


@pytest.mark.asyncio
async def test_claim_ready_concurrent_one_winner_per_row() -> None:
    store = InMemoryWebhookDeliveryStore()
    now = datetime(2026, 6, 13, 18, 0, 0, tzinfo=UTC)
    seeded = {f"conc-{i}" for i in range(5)}
    for event_id in seeded:
        await store.create(_delivery(event_id=event_id, status=WebhookDeliveryStatus.PENDING))

    batches = await asyncio.gather(*[store.claim_ready(before=now, limit=50) for _ in range(2)])
    claimed_a = {d.event_id for d in batches[0]}
    claimed_b = {d.event_id for d in batches[1]}

    assert claimed_a.isdisjoint(claimed_b)
    assert claimed_a | claimed_b == seeded
