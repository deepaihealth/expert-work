"""``memory_writeback_job`` ORM model — B-168.

One row per run-end long-term-memory write-back handed to the background
worker. Pointer-only: the row names the conversation checkpoint to read
(``thread_id`` + ``checkpoint_id``), never a copy of its content.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import Boolean, CheckConstraint, DateTime, Integer, Text, func, text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from expert_work.persistence.base import Base


class MemoryWritebackJobRow(Base):
    """One queued / running / settled background memory write-back."""

    __tablename__ = "memory_writeback_job"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'running', 'done', 'failed')",
            name="memory_writeback_job_status_enum",
        ),
        # 每条任务至少一个指针 —— 处理器绝不去读整个会话。
        CheckConstraint(
            "checkpoint_id IS NOT NULL OR message_count IS NOT NULL",
            name="memory_writeback_job_pointer",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    tenant_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    user_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    agent_name: Mapped[str] = mapped_column(Text, nullable=False)
    agent_version: Mapped[str] = mapped_column(Text, nullable=False)
    thread_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    checkpoint_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    message_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    run_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    trace_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: ``pending`` | ``running`` | ``done`` | ``failed`` (CHECK in migration 0161).
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'pending'"))
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    written_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    failed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    queued_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    exec_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
