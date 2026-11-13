"""Memory-consolidation state ORM models — B-300 PR A (migration 0188).

* ``memory_consolidation_cursor`` — one row per tenant: the last user the
  consolidator swept, so the next sweep resumes after it (D4).
* ``memory_cluster_rejection`` — clusters the consolidation model rejected,
  keyed by a hash of the sorted member ids (never the text), so the same
  neighbourhood is not asked again until a member changes.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import DateTime, Text, func
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from expert_work.persistence.base import Base


class MemoryConsolidationCursorRow(Base):
    """Per-tenant consolidation cursor: the last swept user id."""

    __tablename__ = "memory_consolidation_cursor"

    tenant_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    last_user_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class MemoryClusterRejectionRow(Base):
    """A candidate cluster the consolidation model rejected."""

    __tablename__ = "memory_cluster_rejection"

    tenant_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    user_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    member_hash: Mapped[str] = mapped_column(Text, primary_key=True)
    reject_reason: Mapped[str] = mapped_column(Text, nullable=False)
    rejected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
