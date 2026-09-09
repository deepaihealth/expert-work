"""``thread_message`` / ``thread_message_sync`` ORM models — conversation IA M4.

Write-side mirror of the user/assistant turns living in LangGraph's
``checkpoints`` blob, so the conversation browser's content search can run
as an indexed, RLS-scoped SQL query (migration 0106). ``seq`` is the turn's
index in the checkpoint's append-only ``messages`` channel — stable, so the
mirror upsert is ``ON CONFLICT DO NOTHING`` idempotent.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import DateTime, ForeignKey, Index, Integer, Text, func
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from expert_work.persistence.base import Base


class ThreadMessageRow(Base):
    """One mirrored user/assistant text turn of a conversation."""

    __tablename__ = "thread_message"

    thread_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("thread_meta.thread_id", ondelete="CASCADE"),
        primary_key=True,
    )
    seq: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    role: Mapped[str] = mapped_column(Text, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    # P-1 —— 镜像上的「已被取代」标记(值 = 新 run_id)。检查点是真相,这一列
    # 由 supersede 显式 UPDATE 同步(DO NOTHING 学不到)。
    superseded_by: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    # The content trigram GIN index lives in migration 0106 only —
    # ``gin_trgm_ops`` has no first-class SQLAlchemy declaration.
    __table_args__ = (Index("thread_message_tenant_idx", "tenant_id"),)


class ThreadMessageSyncRow(Base):
    """Per-thread mirror watermark — a missing row IS the backfill queue."""

    __tablename__ = "thread_message_sync"

    thread_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("thread_meta.thread_id", ondelete="CASCADE"),
        primary_key=True,
    )
    tenant_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    message_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
