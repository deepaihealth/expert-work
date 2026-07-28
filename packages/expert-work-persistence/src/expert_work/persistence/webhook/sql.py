"""SQLAlchemy-backed webhook stores — HX-9 (STREAM-HX § 13)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import cast
from uuid import UUID

from sqlalchemy import delete as sa_delete
from sqlalchemy import func, or_, select
from sqlalchemy import update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from expert_work.persistence.models import WebhookDeliveryRow, WebhookEndpointRow
from expert_work.persistence.webhook.base import WebhookDeliveryStore, WebhookEndpointStore
from expert_work.protocol import (
    WebhookDeliveryRecord,
    WebhookDeliveryStatus,
    WebhookEndpointRecord,
    WebhookEndpointSource,
    WebhookEventType,
    WebhookPayloadFormat,
)


def _endpoint_row_to_dto(row: WebhookEndpointRow) -> WebhookEndpointRecord:
    return WebhookEndpointRecord(
        id=row.id,
        tenant_id=row.tenant_id,
        user_id=row.user_id,
        name=row.name,
        url=row.url,
        event_types=tuple(cast("list[WebhookEventType]", list(row.event_types or []))),
        agent_name=row.agent_name,
        secret_ref=row.secret_ref,
        enabled=row.enabled,
        source=cast(WebhookEndpointSource, row.source),
        payload_format=cast(WebhookPayloadFormat, row.payload_format or "generic"),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


class SqlWebhookEndpointStore(WebhookEndpointStore):
    """Postgres-backed endpoint registry — the ``webhook_endpoint`` table."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sf = session_factory

    async def create(self, record: WebhookEndpointRecord) -> WebhookEndpointRecord:
        async with self._sf() as session:
            session.add(
                WebhookEndpointRow(
                    id=record.id,
                    tenant_id=record.tenant_id,
                    user_id=record.user_id,
                    name=record.name,
                    url=record.url,
                    event_types=list(record.event_types),
                    agent_name=record.agent_name,
                    secret_ref=record.secret_ref,
                    enabled=record.enabled,
                    source=record.source,
                    payload_format=record.payload_format,
                    created_at=record.created_at,
                    updated_at=record.updated_at,
                )
            )
            await session.commit()
        return record

    async def get(self, *, endpoint_id: UUID, tenant_id: UUID) -> WebhookEndpointRecord | None:
        async with self._sf() as session:
            row = (
                await session.execute(
                    select(WebhookEndpointRow).where(
                        WebhookEndpointRow.id == endpoint_id,
                        WebhookEndpointRow.tenant_id == tenant_id,
                    )
                )
            ).scalar_one_or_none()
        return _endpoint_row_to_dto(row) if row is not None else None

    async def list_by_tenant(
        self, *, tenant_id: UUID, agent_name: str | None = None
    ) -> list[WebhookEndpointRecord]:
        stmt = (
            select(WebhookEndpointRow)
            .where(WebhookEndpointRow.tenant_id == tenant_id)
            .order_by(WebhookEndpointRow.created_at.asc())
        )
        if agent_name is not None:
            stmt = stmt.where(WebhookEndpointRow.agent_name == agent_name)
        async with self._sf() as session:
            rows = (await session.execute(stmt)).scalars().all()
        return [_endpoint_row_to_dto(r) for r in rows]

    async def list_all_tenants(
        self, *, agent_name: str | None = None
    ) -> list[WebhookEndpointRecord]:
        # Stream N — no tenant filter; caller must wrap in bypass_rls_session().
        stmt = select(WebhookEndpointRow).order_by(WebhookEndpointRow.created_at.asc())
        if agent_name is not None:
            stmt = stmt.where(WebhookEndpointRow.agent_name == agent_name)
        async with self._sf() as session:
            rows = (await session.execute(stmt)).scalars().all()
        return [_endpoint_row_to_dto(r) for r in rows]

    async def list_enabled_all_tenants(self) -> list[WebhookEndpointRecord]:
        # Cross-tenant — caller must wrap in bypass_rls_session().
        async with self._sf() as session:
            rows = (
                (
                    await session.execute(
                        select(WebhookEndpointRow)
                        .where(WebhookEndpointRow.enabled.is_(True))
                        .order_by(WebhookEndpointRow.created_at.asc())
                    )
                )
                .scalars()
                .all()
            )
        return [_endpoint_row_to_dto(r) for r in rows]

    async def update(self, record: WebhookEndpointRecord) -> bool:
        async with self._sf() as session:
            result = await session.execute(
                sa_update(WebhookEndpointRow)
                .where(
                    WebhookEndpointRow.id == record.id,
                    WebhookEndpointRow.tenant_id == record.tenant_id,
                )
                .values(
                    user_id=record.user_id,
                    name=record.name,
                    url=record.url,
                    event_types=list(record.event_types),
                    agent_name=record.agent_name,
                    secret_ref=record.secret_ref,
                    enabled=record.enabled,
                    source=record.source,
                    payload_format=record.payload_format,
                    updated_at=record.updated_at,
                )
            )
            await session.commit()
        return int(getattr(result, "rowcount", 0) or 0) > 0

    async def delete(self, *, endpoint_id: UUID, tenant_id: UUID) -> bool:
        async with self._sf() as session:
            result = await session.execute(
                sa_delete(WebhookEndpointRow).where(
                    WebhookEndpointRow.id == endpoint_id,
                    WebhookEndpointRow.tenant_id == tenant_id,
                )
            )
            await session.commit()
        return int(getattr(result, "rowcount", 0) or 0) > 0

    async def delete_all_for_user(self, *, tenant_id: UUID, user_id: UUID) -> list[UUID]:
        stmt = (
            sa_delete(WebhookEndpointRow)
            .where(
                WebhookEndpointRow.tenant_id == tenant_id,
                WebhookEndpointRow.user_id == user_id,
            )
            .returning(WebhookEndpointRow.id)
        )
        async with self._sf() as session:
            deleted = (await session.execute(stmt)).scalars().all()
            await session.commit()
        return list(deleted)

    async def count_by_tenant(self, *, tenant_id: UUID) -> int:
        async with self._sf() as session:
            result = await session.execute(
                select(func.count())
                .select_from(WebhookEndpointRow)
                .where(WebhookEndpointRow.tenant_id == tenant_id)
            )
        return int(result.scalar_one())


def _delivery_row_to_dto(row: WebhookDeliveryRow) -> WebhookDeliveryRecord:
    return WebhookDeliveryRecord(
        id=row.id,
        tenant_id=row.tenant_id,
        endpoint_id=row.endpoint_id,
        event_id=row.event_id,
        event_type=cast(WebhookEventType, row.event_type),
        run_id=row.run_id,
        payload=dict(row.payload or {}),
        status=WebhookDeliveryStatus(row.status),
        attempt=row.attempt,
        next_retry_at=row.next_retry_at,
        response_status=row.response_status,
        error=row.error,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


#: How long a ``delivering`` row can go untouched before the next sweep
#: treats it as an orphaned claim (crashed replica) and reclaims it. Mirrors
#: the in-memory store's constant of the same name (W1-PR1).
_DELIVERING_STALE_S: int = 300


class SqlWebhookDeliveryStore(WebhookDeliveryStore):
    """Postgres-backed delivery queue — the ``webhook_delivery`` table."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sf = session_factory

    async def create(self, record: WebhookDeliveryRecord) -> WebhookDeliveryRecord:
        async with self._sf() as session:
            session.add(
                WebhookDeliveryRow(
                    id=record.id,
                    tenant_id=record.tenant_id,
                    endpoint_id=record.endpoint_id,
                    event_id=record.event_id,
                    event_type=record.event_type,
                    run_id=record.run_id,
                    payload=dict(record.payload),
                    status=record.status.value,
                    attempt=record.attempt,
                    next_retry_at=record.next_retry_at,
                    response_status=record.response_status,
                    error=record.error,
                    created_at=record.created_at,
                    updated_at=record.updated_at,
                )
            )
            await session.commit()
        return record

    async def get(self, *, delivery_id: UUID, tenant_id: UUID) -> WebhookDeliveryRecord | None:
        async with self._sf() as session:
            row = (
                await session.execute(
                    select(WebhookDeliveryRow).where(
                        WebhookDeliveryRow.id == delivery_id,
                        WebhookDeliveryRow.tenant_id == tenant_id,
                    )
                )
            ).scalar_one_or_none()
        return _delivery_row_to_dto(row) if row is not None else None

    async def exists_for_event(self, *, endpoint_id: UUID, event_id: str) -> bool:
        # Cross-tenant — caller bypasses RLS (idempotent-enqueue check).
        async with self._sf() as session:
            row = (
                await session.execute(
                    select(WebhookDeliveryRow.id).where(
                        WebhookDeliveryRow.endpoint_id == endpoint_id,
                        WebhookDeliveryRow.event_id == event_id,
                    )
                )
            ).first()
        return row is not None

    async def update(self, record: WebhookDeliveryRecord) -> bool:
        async with self._sf() as session:
            result = await session.execute(
                sa_update(WebhookDeliveryRow)
                .where(
                    WebhookDeliveryRow.id == record.id,
                    WebhookDeliveryRow.tenant_id == record.tenant_id,
                )
                .values(
                    status=record.status.value,
                    attempt=record.attempt,
                    next_retry_at=record.next_retry_at,
                    response_status=record.response_status,
                    error=record.error,
                    updated_at=record.updated_at,
                )
            )
            await session.commit()
        return int(getattr(result, "rowcount", 0) or 0) > 0

    async def list_by_endpoint(
        self, *, endpoint_id: UUID, tenant_id: UUID, limit: int = 100
    ) -> list[WebhookDeliveryRecord]:
        async with self._sf() as session:
            rows = (
                (
                    await session.execute(
                        select(WebhookDeliveryRow)
                        .where(
                            WebhookDeliveryRow.endpoint_id == endpoint_id,
                            WebhookDeliveryRow.tenant_id == tenant_id,
                        )
                        .order_by(WebhookDeliveryRow.created_at.desc())
                        .limit(limit)
                    )
                )
                .scalars()
                .all()
            )
        return [_delivery_row_to_dto(r) for r in rows]

    async def delete_for_endpoints(self, *, endpoint_ids: Sequence[UUID], tenant_id: UUID) -> int:
        ids = list(endpoint_ids)
        if not ids:
            return 0
        stmt = sa_delete(WebhookDeliveryRow).where(
            WebhookDeliveryRow.tenant_id == tenant_id,
            WebhookDeliveryRow.endpoint_id.in_(ids),
        )
        async with self._sf() as session:
            result = await session.execute(stmt)
            await session.commit()
        return int(getattr(result, "rowcount", 0) or 0)

    async def list_ready(
        self, *, before: datetime, limit: int = 1000
    ) -> list[WebhookDeliveryRecord]:
        async with self._sf() as session:
            rows = (
                (
                    await session.execute(
                        select(WebhookDeliveryRow)
                        .where(
                            or_(
                                WebhookDeliveryRow.status == WebhookDeliveryStatus.PENDING.value,
                                (WebhookDeliveryRow.status == WebhookDeliveryStatus.RETRYING.value)
                                & (WebhookDeliveryRow.next_retry_at <= before),
                            )
                        )
                        .order_by(WebhookDeliveryRow.created_at.asc())
                        .limit(limit)
                    )
                )
                .scalars()
                .all()
            )
        return [_delivery_row_to_dto(r) for r in rows]

    async def claim_ready(
        self, *, before: datetime, limit: int = 1000
    ) -> list[WebhookDeliveryRecord]:
        # Atomic CAS — a fleet of worker replicas shares this queue without
        # double-POSTing: ``FOR UPDATE SKIP LOCKED`` lets each replica's
        # transaction skip rows another replica already has locked, then the
        # UPDATE flips exactly the rows this replica won to ``delivering``
        # and hands them back via ``RETURNING`` in one round trip.
        stale_before = before - timedelta(seconds=_DELIVERING_STALE_S)
        ready = or_(
            WebhookDeliveryRow.status == WebhookDeliveryStatus.PENDING.value,
            (WebhookDeliveryRow.status == WebhookDeliveryStatus.RETRYING.value)
            & (WebhookDeliveryRow.next_retry_at <= before),
            (WebhookDeliveryRow.status == WebhookDeliveryStatus.DELIVERING.value)
            & (WebhookDeliveryRow.updated_at <= stale_before),
        )
        async with self._sf() as session:
            candidate_ids = (
                (
                    await session.execute(
                        select(WebhookDeliveryRow.id)
                        .where(ready)
                        .order_by(WebhookDeliveryRow.created_at.asc())
                        .limit(limit)
                        .with_for_update(skip_locked=True)
                    )
                )
                .scalars()
                .all()
            )
            if not candidate_ids:
                await session.commit()
                return []
            rows = (
                (
                    await session.execute(
                        sa_update(WebhookDeliveryRow)
                        .where(WebhookDeliveryRow.id.in_(candidate_ids))
                        .values(status=WebhookDeliveryStatus.DELIVERING.value, updated_at=before)
                        .returning(WebhookDeliveryRow)
                    )
                )
                .scalars()
                .all()
            )
            await session.commit()
        ordered = sorted(rows, key=lambda r: r.created_at)
        return [_delivery_row_to_dto(r) for r in ordered]
