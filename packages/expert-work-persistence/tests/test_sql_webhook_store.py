"""Integration tests for the SQL webhook stores against a real Postgres — HX-9."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.exc import IntegrityError
from testcontainers.postgres import PostgresContainer

from expert_work.persistence import (
    DatabaseConfig,
    SqlWebhookDeliveryStore,
    SqlWebhookEndpointStore,
    create_async_engine_from_config,
    create_async_session_factory,
)
from expert_work.protocol import (
    WebhookDeliveryRecord,
    WebhookDeliveryStatus,
    WebhookEndpointRecord,
    WebhookEventType,
)

pytestmark = pytest.mark.integration

ALEMBIC_INI = Path(__file__).resolve().parent.parent / "alembic.ini"

_BASE = datetime(2026, 6, 13, 12, 0, 0, tzinfo=UTC)


def _sync_dsn(container: PostgresContainer) -> str:
    url = str(container.get_connection_url())
    return url.replace("+psycopg2", "+psycopg").replace("postgresql://", "postgresql+psycopg://", 1)


def _async_dsn(container: PostgresContainer) -> str:
    url = str(container.get_connection_url())
    return url.replace("+psycopg2", "+asyncpg").replace("postgresql://", "postgresql+asyncpg://", 1)


@pytest.fixture
def stores(
    postgres_container: PostgresContainer,
) -> Iterator[tuple[SqlWebhookEndpointStore, SqlWebhookDeliveryStore]]:
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", _sync_dsn(postgres_container))
    command.upgrade(cfg, "head")

    engine = create_async_engine_from_config(DatabaseConfig(dsn=_async_dsn(postgres_container)))
    factory = create_async_session_factory(engine)
    yield SqlWebhookEndpointStore(factory), SqlWebhookDeliveryStore(factory)


def _endpoint(
    *,
    endpoint_id: UUID | None = None,
    tenant_id: UUID | None = None,
    name: str = "ops-notify",
    agent_name: str | None = None,
    event_types: tuple[WebhookEventType, ...] = ("run.completed", "run.failed"),
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


def _delivery(
    *,
    delivery_id: UUID | None = None,
    tenant_id: UUID | None = None,
    endpoint_id: UUID | None = None,
    event_id: str = "run:abc",
    status: WebhookDeliveryStatus = WebhookDeliveryStatus.PENDING,
    next_retry_at: datetime | None = None,
    created_at: datetime = _BASE,
) -> WebhookDeliveryRecord:
    return WebhookDeliveryRecord(
        id=delivery_id or uuid4(),
        tenant_id=tenant_id or uuid4(),
        endpoint_id=endpoint_id or uuid4(),
        event_id=event_id,
        event_type="run.completed",
        run_id=uuid4(),
        payload={"run_id": "abc", "status": "success"},
        status=status,
        attempt=0,
        next_retry_at=next_retry_at,
        created_at=created_at,
        updated_at=created_at,
    )


# --- endpoint store --------------------------------------------------------


@pytest.mark.asyncio
async def test_endpoint_round_trips(
    stores: tuple[SqlWebhookEndpointStore, SqlWebhookDeliveryStore],
) -> None:
    endpoints, _ = stores
    eid, tenant = uuid4(), uuid4()
    await endpoints.create(_endpoint(endpoint_id=eid, tenant_id=tenant, agent_name="reporter"))

    fetched = await endpoints.get(endpoint_id=eid, tenant_id=tenant)
    assert fetched is not None
    assert fetched.event_types == ("run.completed", "run.failed")
    assert fetched.agent_name == "reporter"
    assert fetched.secret_ref == "webhook-endpoint/abc"


@pytest.mark.asyncio
async def test_endpoint_duplicate_name_violates_unique(
    stores: tuple[SqlWebhookEndpointStore, SqlWebhookDeliveryStore],
) -> None:
    endpoints, _ = stores
    tenant = uuid4()
    await endpoints.create(_endpoint(tenant_id=tenant, name="ops-notify"))
    with pytest.raises(IntegrityError):
        await endpoints.create(_endpoint(tenant_id=tenant, name="ops-notify"))


@pytest.mark.asyncio
async def test_endpoint_list_filters_update_delete_count(
    stores: tuple[SqlWebhookEndpointStore, SqlWebhookDeliveryStore],
) -> None:
    endpoints, _ = stores
    tenant = uuid4()
    eid = uuid4()
    await endpoints.create(
        _endpoint(endpoint_id=eid, tenant_id=tenant, name="all", agent_name=None)
    )
    await endpoints.create(
        _endpoint(tenant_id=tenant, name="scoped", agent_name="reporter", enabled=False)
    )

    assert await endpoints.count_by_tenant(tenant_id=tenant) == 2
    scoped = await endpoints.list_by_tenant(tenant_id=tenant, agent_name="reporter")
    assert [e.name for e in scoped] == ["scoped"]

    rec = await endpoints.get(endpoint_id=eid, tenant_id=tenant)
    assert rec is not None
    updated = await endpoints.update(rec.model_copy(update={"enabled": False}))
    assert updated is True

    deleted = await endpoints.delete(endpoint_id=eid, tenant_id=tenant)
    assert deleted is True
    assert await endpoints.count_by_tenant(tenant_id=tenant) == 1


@pytest.mark.asyncio
async def test_endpoint_list_enabled_all_tenants(
    stores: tuple[SqlWebhookEndpointStore, SqlWebhookDeliveryStore],
) -> None:
    endpoints, _ = stores
    await endpoints.create(_endpoint(tenant_id=uuid4(), name="e1", enabled=True))
    await endpoints.create(_endpoint(tenant_id=uuid4(), name="e2", enabled=False))

    listed = await endpoints.list_enabled_all_tenants()
    names = {e.name for e in listed}
    assert "e1" in names
    assert "e2" not in names


# --- delivery store --------------------------------------------------------


@pytest.mark.asyncio
async def test_delivery_round_trips(
    stores: tuple[SqlWebhookEndpointStore, SqlWebhookDeliveryStore],
) -> None:
    _, deliveries = stores
    did, tenant = uuid4(), uuid4()
    await deliveries.create(_delivery(delivery_id=did, tenant_id=tenant))

    fetched = await deliveries.get(delivery_id=did, tenant_id=tenant)
    assert fetched is not None
    assert fetched.payload == {"run_id": "abc", "status": "success"}
    assert fetched.status is WebhookDeliveryStatus.PENDING


@pytest.mark.asyncio
async def test_delivery_dedup_violates_unique(
    stores: tuple[SqlWebhookEndpointStore, SqlWebhookDeliveryStore],
) -> None:
    _, deliveries = stores
    endpoint = uuid4()
    await deliveries.create(_delivery(endpoint_id=endpoint, event_id="run:x"))
    with pytest.raises(IntegrityError):
        await deliveries.create(_delivery(endpoint_id=endpoint, event_id="run:x"))


@pytest.mark.asyncio
async def test_delivery_exists_update_list_ready(
    stores: tuple[SqlWebhookEndpointStore, SqlWebhookDeliveryStore],
) -> None:
    _, deliveries = stores
    now = datetime(2026, 6, 13, 15, 0, 0, tzinfo=UTC)
    endpoint, tenant = uuid4(), uuid4()
    did = uuid4()
    await deliveries.create(
        _delivery(delivery_id=did, tenant_id=tenant, endpoint_id=endpoint, event_id="p1")
    )
    await deliveries.create(
        _delivery(
            endpoint_id=endpoint,
            event_id="r-due",
            status=WebhookDeliveryStatus.RETRYING,
            next_retry_at=now - timedelta(minutes=1),
        )
    )
    await deliveries.create(
        _delivery(
            endpoint_id=endpoint,
            event_id="r-future",
            status=WebhookDeliveryStatus.RETRYING,
            next_retry_at=now + timedelta(hours=1),
        )
    )

    assert await deliveries.exists_for_event(endpoint_id=endpoint, event_id="p1") is True
    assert await deliveries.exists_for_event(endpoint_id=endpoint, event_id="missing") is False

    rec = await deliveries.get(delivery_id=did, tenant_id=tenant)
    assert rec is not None
    delivered = rec.model_copy(
        update={
            "status": WebhookDeliveryStatus.DELIVERED,
            "attempt": 1,
            "response_status": 200,
        }
    )
    updated = await deliveries.update(delivered)
    assert updated is True

    # list_ready is cross-tenant (the shared container carries sibling tests'
    # rows), so assert by membership on this test's own event_ids — the
    # trigger SQL suite uses the same convention.
    ready = {d.event_id for d in await deliveries.list_ready(before=now)}
    assert "r-due" in ready  # retrying + next_retry_at passed
    assert "p1" not in ready  # now delivered
    assert "r-future" not in ready  # retry not yet due


# --- claim_ready (W1-PR1 CAS) -----------------------------------------------


@pytest.mark.asyncio
async def test_claim_ready_writes_delivering_and_matches_list_ready_set(
    stores: tuple[SqlWebhookEndpointStore, SqlWebhookDeliveryStore],
) -> None:
    _, deliveries = stores
    now = datetime(2026, 6, 13, 16, 0, 0, tzinfo=UTC)
    endpoint = uuid4()
    await deliveries.create(_delivery(endpoint_id=endpoint, event_id="c-p1"))
    await deliveries.create(
        _delivery(
            endpoint_id=endpoint,
            event_id="c-r-due",
            status=WebhookDeliveryStatus.RETRYING,
            next_retry_at=now - timedelta(minutes=1),
        )
    )
    await deliveries.create(
        _delivery(
            endpoint_id=endpoint,
            event_id="c-r-future",
            status=WebhookDeliveryStatus.RETRYING,
            next_retry_at=now + timedelta(hours=1),
        )
    )

    claimed = await deliveries.claim_ready(before=now)
    claimed_by_event = {d.event_id: d for d in claimed}
    assert "c-p1" in claimed_by_event
    assert "c-r-due" in claimed_by_event
    assert "c-r-future" not in claimed_by_event
    # claim_ready hands back the row already flipped to ``delivering``.
    assert claimed_by_event["c-p1"].status is WebhookDeliveryStatus.DELIVERING
    assert claimed_by_event["c-r-due"].status is WebhookDeliveryStatus.DELIVERING

    # A second claim right away sees nothing left for this batch (both rows
    # are now ``delivering``, not ``pending``/due-``retrying``).
    reclaimed = {d.event_id for d in await deliveries.claim_ready(before=now)}
    assert "c-p1" not in reclaimed
    assert "c-r-due" not in reclaimed


@pytest.mark.asyncio
async def test_claim_ready_reclaims_stale_delivering_not_fresh(
    stores: tuple[SqlWebhookEndpointStore, SqlWebhookDeliveryStore],
) -> None:
    _, deliveries = stores
    now = datetime(2026, 6, 13, 17, 0, 0, tzinfo=UTC)
    endpoint = uuid4()
    # A crashed replica's claim, last touched further back than the
    # _DELIVERING_STALE_S = 300s window — the next sweep must reclaim it.
    stale = _delivery(
        endpoint_id=endpoint,
        event_id="c-stale",
        status=WebhookDeliveryStatus.DELIVERING,
        created_at=now - timedelta(hours=1),
    ).model_copy(update={"updated_at": now - timedelta(seconds=301)})
    # A live replica's claim, touched inside the window — must stay theirs.
    fresh = _delivery(
        endpoint_id=endpoint,
        event_id="c-fresh",
        status=WebhookDeliveryStatus.DELIVERING,
        created_at=now - timedelta(hours=1),
    ).model_copy(update={"updated_at": now - timedelta(seconds=10)})
    await deliveries.create(stale)
    await deliveries.create(fresh)

    claimed = {d.event_id for d in await deliveries.claim_ready(before=now)}
    assert "c-stale" in claimed  # updated_at older than the stale window — reclaimed
    assert "c-fresh" not in claimed  # updated_at recent — still owned by its claimer


@pytest.mark.asyncio
async def test_claim_ready_concurrent_exactly_once_across_batch(
    stores: tuple[SqlWebhookEndpointStore, SqlWebhookDeliveryStore],
) -> None:
    """True DB concurrency — the CAS + SKIP LOCKED claim hands each row to
    exactly one concurrent claimer, no double-POST across worker replicas."""
    _, deliveries = stores
    now = datetime(2026, 6, 13, 18, 0, 0, tzinfo=UTC)
    endpoint = uuid4()
    seeded = {f"conc-{i}" for i in range(5)}
    for event_id in seeded:
        await deliveries.create(_delivery(endpoint_id=endpoint, event_id=event_id))

    batches = await asyncio.gather(
        *[deliveries.claim_ready(before=now, limit=50) for _ in range(2)]
    )
    claimed_a = {d.event_id for d in batches[0] if d.event_id in seeded}
    claimed_b = {d.event_id for d in batches[1] if d.event_id in seeded}

    assert claimed_a.isdisjoint(claimed_b)  # never claimed by both
    assert claimed_a | claimed_b == seeded  # every row claimed exactly once, by someone
