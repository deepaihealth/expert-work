"""``image_upload`` ORM model — Stream J.6.补强-3 (Mini-ADR J-32).

Schema mirrors migration 0028_image_upload exactly. Tenant RLS is
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


class ImageUploadRow(Base):
    """One landed image upload registered for lifecycle / retention."""

    __tablename__ = "image_upload"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    user_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    thread_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    object_key: Mapped[str] = mapped_column(Text, nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    mime_type: Mapped[str] = mapped_column(Text, nullable=False)
    sha256: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: B-283 (migration ``0185``) —— 最后一次被下载的时间,每张每天最多记一次。NULL = 列加上
    #: 之后没被下载过。
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint("size_bytes >= 0", name="image_upload_size_nonneg"),
        Index("ix_image_upload_tenant_id", "tenant_id"),
        Index("ix_image_upload_thread_id", "thread_id"),
        Index(
            "ix_image_upload_active",
            "tenant_id",
            "created_at",
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index(
            "ix_image_upload_pending_hard_delete",
            "deleted_at",
            postgresql_where=text("deleted_at IS NOT NULL"),
        ),
    )
