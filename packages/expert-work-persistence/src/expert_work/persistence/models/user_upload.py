"""``user_upload`` ORM model —— 对外附件模型统一(spec 2026-08-17)。

Schema mirrors migration 0146_user_upload exactly. Tenant RLS is
enforced at the row level by the migration's policy; the application
still passes ``tenant_id`` for clarity + so an in-memory backend can
match semantics without a Postgres GUC.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import BigInteger, CheckConstraint, DateTime, Index, Text, text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from expert_work.persistence.base import Base


class UserUploadRow(Base):
    """One landed third-party attachment (document or image), uniform id."""

    __tablename__ = "user_upload"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    user_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    thread_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    ref: Mapped[str] = mapped_column(Text, nullable=False)
    mime_type: Mapped[str] = mapped_column(Text, nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    filename: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint("kind IN ('image','document')", name="user_upload_kind_enum"),
        CheckConstraint("size_bytes >= 0", name="user_upload_size_nonneg"),
        Index("ix_user_upload_tenant_user", "tenant_id", "user_id"),
        Index("ix_user_upload_tenant_thread", "tenant_id", "thread_id"),
    )
