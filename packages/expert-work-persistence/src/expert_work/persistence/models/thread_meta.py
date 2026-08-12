"""``thread_meta`` ORM model — vendor-aligned per 06-OPEN-SOURCE-DEPS §P0."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import DateTime, Index, Integer, Text, func, text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from expert_work.persistence.base import Base


class ThreadMetaRow(Base):
    """LangGraph thread lifecycle metadata; extended with ``tenant_id`` per ADR-0002."""

    __tablename__ = "thread_meta"

    thread_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    # Stream J.14 — owning user. Nullable, no FK (see migration 0015).
    user_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    created_by: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        server_default="active",
    )
    # Human label for the session-history list — auto-set from the first user
    # message, manually overridable. Nullable: pre-existing threads have none.
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    agent_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    agent_version: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Third-party-visible message count (include_hidden=False), recomputed by
    # run finalization. No server_default: pre-existing rows stay NULL
    # ("not yet computed"), distinct from a genuinely empty thread (0).
    message_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (
        Index("thread_meta_tenant_status_idx", "tenant_id", "status"),
        Index("thread_meta_tenant_user_idx", "tenant_id", "user_id"),
        # Conversation-centric IA — agent-first thread reads (Conversations
        # tab / browser filters / M2 users rollup). Migration 0105.
        Index(
            "thread_meta_tenant_agent_user_idx",
            "tenant_id",
            "agent_name",
            "user_id",
            postgresql_where=text("agent_name IS NOT NULL"),
        ),
    )
