"""``webhook_endpoint`` + ``webhook_delivery`` ORM models — HX-9 (STREAM-HX § 13).

Schema mirrors migration 0074_webhook_hook exactly. Tenant RLS is enforced
at the row level by the migration's policies; the application still passes
``tenant_id`` so an in-memory backend matches semantics without a Postgres GUC.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from expert_work.persistence.base import Base

_SOURCE_VALUES = "('manifest', 'api')"
_DELIVERY_STATUS_VALUES = (
    "('pending', 'delivering', 'delivered', 'failed', 'retrying', 'dead_letter')"
)


class WebhookEndpointRow(Base):
    """One registered outbound webhook endpoint (HX-9)."""

    __tablename__ = "webhook_endpoint"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    user_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    event_types: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    # NULL ``agent_name`` = subscribe to events from every agent in the tenant.
    agent_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    secret_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    source: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'api'"))
    #: Delivery body shape (generic envelope vs IM bot message) — see
    #: ``WebhookPayloadFormat`` in the protocol.
    payload_format: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'generic'")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint(f"source IN {_SOURCE_VALUES}", name="webhook_endpoint_source_valid"),
        UniqueConstraint("tenant_id", "name", name="webhook_endpoint_name_uniq"),
        Index("ix_webhook_endpoint_tenant_id", "tenant_id"),
        # Partial index — the delivery worker scans only enabled endpoints.
        Index(
            "ix_webhook_endpoint_enabled",
            "enabled",
            postgresql_where=text("enabled = true"),
        ),
    )


class WebhookDeliveryRow(Base):
    """One event→endpoint delivery — carries the DLQ retry state (HX-9)."""

    __tablename__ = "webhook_delivery"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    endpoint_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    event_id: Mapped[str] = mapped_column(Text, nullable=False)
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    run_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    payload: Mapped[dict[str, object]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'pending'"))
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    response_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint(
            f"status IN {_DELIVERY_STATUS_VALUES}", name="webhook_delivery_status_valid"
        ),
        # Idempotent enqueue — re-scanning the event spine never double-delivers.
        UniqueConstraint("endpoint_id", "event_id", name="webhook_delivery_dedup"),
        Index("ix_webhook_delivery_tenant_id", "tenant_id"),
        Index("ix_webhook_delivery_endpoint_id", "endpoint_id"),
        # Partial index — the delivery sweep scans only deliverable rows.
        # Includes 'delivering' (W1-PR1) so claim_ready's third OR arm
        # (stale in-flight reclaim) stays covered by the predicate — a
        # narrower predicate would make Postgres fall back to a full
        # table scan on the whole OR clause (0138_webhook_delivering_status).
        Index(
            "ix_webhook_delivery_ready",
            "next_retry_at",
            postgresql_where=text("status IN ('pending', 'retrying', 'delivering')"),
        ),
    )
