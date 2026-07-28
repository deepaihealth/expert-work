"""In-memory ``WebhookEndpointStore`` + ``WebhookDeliveryStore`` for unit tests."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from uuid import UUID

from expert_work.persistence.webhook.base import WebhookDeliveryStore, WebhookEndpointStore
from expert_work.protocol import (
    WebhookDeliveryRecord,
    WebhookDeliveryStatus,
    WebhookEndpointRecord,
)

#: How long a ``delivering`` row can go untouched before the next sweep
#: treats it as an orphaned claim (crashed replica) and reclaims it. Mirrors
#: the SQL store's constant of the same name (W1-PR1).
_DELIVERING_STALE_S: int = 300


class InMemoryWebhookEndpointStore(WebhookEndpointStore):
    """In-memory ``WebhookEndpointStore`` — keyed by endpoint id."""

    def __init__(self) -> None:
        self._rows: dict[UUID, WebhookEndpointRecord] = {}

    async def create(self, record: WebhookEndpointRecord) -> WebhookEndpointRecord:
        for existing in self._rows.values():
            if existing.tenant_id == record.tenant_id and existing.name == record.name:
                msg = f"webhook endpoint {record.name!r} already exists for this tenant"
                raise ValueError(msg)
        self._rows[record.id] = record
        return record

    async def get(self, *, endpoint_id: UUID, tenant_id: UUID) -> WebhookEndpointRecord | None:
        row = self._rows.get(endpoint_id)
        if row is None or row.tenant_id != tenant_id:
            return None
        return row

    async def list_by_tenant(
        self, *, tenant_id: UUID, agent_name: str | None = None
    ) -> list[WebhookEndpointRecord]:
        return [
            r
            for r in self._rows.values()
            if r.tenant_id == tenant_id and (agent_name is None or r.agent_name == agent_name)
        ]

    async def list_all_tenants(
        self, *, agent_name: str | None = None
    ) -> list[WebhookEndpointRecord]:
        return [r for r in self._rows.values() if agent_name is None or r.agent_name == agent_name]

    async def list_enabled_all_tenants(self) -> list[WebhookEndpointRecord]:
        return [r for r in self._rows.values() if r.enabled]

    async def update(self, record: WebhookEndpointRecord) -> bool:
        existing = self._rows.get(record.id)
        if existing is None or existing.tenant_id != record.tenant_id:
            return False
        self._rows[record.id] = record
        return True

    async def delete(self, *, endpoint_id: UUID, tenant_id: UUID) -> bool:
        row = self._rows.get(endpoint_id)
        if row is None or row.tenant_id != tenant_id:
            return False
        del self._rows[endpoint_id]
        return True

    async def delete_all_for_user(self, *, tenant_id: UUID, user_id: UUID) -> list[UUID]:
        victims = [
            eid
            for eid, r in self._rows.items()
            if r.tenant_id == tenant_id and r.user_id == user_id
        ]
        for eid in victims:
            del self._rows[eid]
        return victims

    async def count_by_tenant(self, *, tenant_id: UUID) -> int:
        return sum(1 for r in self._rows.values() if r.tenant_id == tenant_id)


class InMemoryWebhookDeliveryStore(WebhookDeliveryStore):
    """In-memory ``WebhookDeliveryStore`` — keyed by delivery id."""

    def __init__(self) -> None:
        self._rows: dict[UUID, WebhookDeliveryRecord] = {}

    async def create(self, record: WebhookDeliveryRecord) -> WebhookDeliveryRecord:
        for existing in self._rows.values():
            if existing.endpoint_id == record.endpoint_id and existing.event_id == record.event_id:
                msg = (
                    f"delivery for event {record.event_id!r} already exists "
                    f"on endpoint {record.endpoint_id}"
                )
                raise ValueError(msg)
        self._rows[record.id] = record
        return record

    async def get(self, *, delivery_id: UUID, tenant_id: UUID) -> WebhookDeliveryRecord | None:
        row = self._rows.get(delivery_id)
        if row is None or row.tenant_id != tenant_id:
            return None
        return row

    async def exists_for_event(self, *, endpoint_id: UUID, event_id: str) -> bool:
        return any(
            r.endpoint_id == endpoint_id and r.event_id == event_id for r in self._rows.values()
        )

    async def update(self, record: WebhookDeliveryRecord) -> bool:
        existing = self._rows.get(record.id)
        if existing is None or existing.tenant_id != record.tenant_id:
            return False
        self._rows[record.id] = record
        return True

    async def list_by_endpoint(
        self, *, endpoint_id: UUID, tenant_id: UUID, limit: int = 100
    ) -> list[WebhookDeliveryRecord]:
        rows = [
            r
            for r in self._rows.values()
            if r.endpoint_id == endpoint_id and r.tenant_id == tenant_id
        ]
        rows.sort(key=lambda r: r.created_at, reverse=True)
        return rows[:limit]

    async def delete_for_endpoints(self, *, endpoint_ids: Sequence[UUID], tenant_id: UUID) -> int:
        wanted = set(endpoint_ids)
        if not wanted:
            return 0
        victims = [
            did
            for did, r in self._rows.items()
            if r.endpoint_id in wanted and r.tenant_id == tenant_id
        ]
        for did in victims:
            del self._rows[did]
        return len(victims)

    async def list_ready(
        self, *, before: datetime, limit: int = 1000
    ) -> list[WebhookDeliveryRecord]:
        rows = [
            r
            for r in self._rows.values()
            if (
                r.status is WebhookDeliveryStatus.PENDING
                or (
                    r.status is WebhookDeliveryStatus.RETRYING
                    and r.next_retry_at is not None
                    and r.next_retry_at <= before
                )
            )
        ]
        rows.sort(key=lambda r: r.next_retry_at or r.created_at)
        return rows[:limit]

    async def claim_ready(
        self, *, before: datetime, limit: int = 1000
    ) -> list[WebhookDeliveryRecord]:
        stale_before = before - timedelta(seconds=_DELIVERING_STALE_S)
        candidates = [
            r
            for r in self._rows.values()
            if (
                r.status is WebhookDeliveryStatus.PENDING
                or (
                    r.status is WebhookDeliveryStatus.RETRYING
                    and r.next_retry_at is not None
                    and r.next_retry_at <= before
                )
                or (r.status is WebhookDeliveryStatus.DELIVERING and r.updated_at <= stale_before)
            )
        ]
        candidates.sort(key=lambda r: r.created_at)
        claimed: list[WebhookDeliveryRecord] = []
        for r in candidates[:limit]:
            updated = r.model_copy(
                update={"status": WebhookDeliveryStatus.DELIVERING, "updated_at": before}
            )
            self._rows[r.id] = updated
            claimed.append(updated)
        return claimed
