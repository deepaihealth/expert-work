"""Unit tests for :class:`InMemoryQualityDriftAlertStore` — RT-5 (RT-ADR-24)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from expert_work.persistence import InMemoryQualityDriftAlertStore
from expert_work.protocol import QualityDriftAlertRecord


def _alert(
    *, tenant: UUID, agent: str = "a", at: datetime | None = None
) -> QualityDriftAlertRecord:
    return QualityDriftAlertRecord(
        tenant_id=tenant,
        agent_name=agent,
        recent_mean=3.0,
        baseline_mean=4.0,
        drift_pct=0.25,
        recent_count=12,
        baseline_count=80,
        detected_at=at,
    )


@pytest.mark.asyncio
async def test_insert_stamps_id_and_detected_at() -> None:
    store = InMemoryQualityDriftAlertStore()
    tenant = uuid4()
    stored = await store.insert(_alert(tenant=tenant))
    assert stored.id is not None
    assert stored.detected_at is not None


@pytest.mark.asyncio
async def test_latest_alert_at_backs_cooldown() -> None:
    store = InMemoryQualityDriftAlertStore()
    tenant = uuid4()
    base = datetime(2026, 7, 6, 12, 0, tzinfo=UTC)
    assert await store.latest_alert_at(tenant_id=tenant, agent_name="a") is None
    await store.insert(_alert(tenant=tenant, agent="a", at=base))
    await store.insert(_alert(tenant=tenant, agent="a", at=base + timedelta(hours=2)))
    await store.insert(_alert(tenant=tenant, agent="b", at=base + timedelta(hours=1)))
    # Latest for (tenant, a) is the newer of the two.
    assert await store.latest_alert_at(tenant_id=tenant, agent_name="a") == base + timedelta(
        hours=2
    )
    assert await store.latest_alert_at(tenant_id=tenant, agent_name="b") == base + timedelta(
        hours=1
    )
    assert await store.latest_alert_at(tenant_id=uuid4(), agent_name="a") is None


@pytest.mark.asyncio
async def test_list_alerts_newest_first_and_filtered() -> None:
    store = InMemoryQualityDriftAlertStore()
    tenant, other = uuid4(), uuid4()
    base = datetime(2026, 7, 6, 12, 0, tzinfo=UTC)
    await store.insert(_alert(tenant=tenant, agent="a", at=base))
    await store.insert(_alert(tenant=tenant, agent="a", at=base + timedelta(hours=1)))
    await store.insert(_alert(tenant=other, agent="a", at=base))

    rows = await store.list_alerts(tenant_id=tenant)
    assert len(rows) == 2
    assert rows[0].detected_at == base + timedelta(hours=1)  # newest first
    # Other tenant's alert is not visible in this in-memory scope filter.
    assert all(r.tenant_id == tenant for r in rows)


@pytest.mark.asyncio
async def test_list_alerts_all_tenants_id_tiebreak_on_equal_detected_at() -> None:
    """W4 review C-3 — ``detected_at`` ties order by ``id`` ASC. ``insert``
    assigns ids in insertion order (list order == id order), so the tie is
    seeded white-box (precedent: ``test_in_memory_memory_consolidator.py``)
    in *descending* id order — a dropped id sort degrades to list order and
    the exact-order assertion goes red."""
    store = InMemoryQualityDriftAlertStore()
    ts = datetime(2026, 7, 6, 12, 0, tzinfo=UTC)
    tenant_a, tenant_b = uuid4(), uuid4()
    rec_1 = _alert(tenant=tenant_a, at=ts).model_copy(update={"id": 1})
    rec_2 = _alert(tenant=tenant_b, at=ts).model_copy(update={"id": 2})
    store._alerts.extend([rec_2, rec_1])  # descending id seed

    rows = await store.list_alerts_all_tenants()
    assert [r.id for r in rows] == [1, 2]
