"""``artifact_share_links`` ORM model — B-326 (migration ``0194``).

One row = one public, time-limited link to one exact artifact version, minted when an
agent marks a file for delivery (``share_file``). The row id is the random token in the
URL (``/v1/public/files/{id}``) — the only credential; it is reported in the run's
artifact list (``share_url``), never in model text. RLS (migration ``0194``) is the same
combined tenant + user predicate as ``artifact`` / ``artifact_version``; the public
endpoint looks a row up by id inside an RLS bypass scope.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import CheckConstraint, DateTime, Index, Integer, Text, func, text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from expert_work.persistence.base import Base


class ArtifactShareLinkRow(Base):
    """A public share link pinned to one ``artifact_version`` row."""

    __tablename__ = "artifact_share_links"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    user_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    agent_key: Mapped[str] = mapped_column(Text, nullable=False)
    artifact_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    #: The exact version the link serves. A bare UUID column, no FK — a FK into
    #: the ``FORCE`` RLS ``artifact_version`` table is the Mini-ADR J-1a footgun.
    #: Once retention (or a user / agent purge) removes the version, the public
    #: endpoint's version lookup misses and answers 404.
    artifact_version_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    #: The conversation the link belongs to (at most one reusable link per version and
    #: conversation) and, for audit, the run that minted it.
    run_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    thread_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    #: Downloads served so far; capped per link (``EXPERT_WORK_SHARE_LINK_MAX_DOWNLOADS``).
    download_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0"), default=0
    )
    #: The artifact's name at mint time — the cosmetic last segment of the URL.
    file_name: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("''"), default=""
    )

    __table_args__ = (
        CheckConstraint("download_count >= 0", name="ck_artifact_share_links_download_count"),
        CheckConstraint("expires_at > created_at", name="ck_artifact_share_links_lifetime"),
        Index("ix_artifact_share_links_expires_at", "expires_at"),
        # (conversation, version) lookups — reuse / re-mint, newest per page — and the
        # conversation purge (a prefix of it).
        Index(
            "ix_artifact_share_links_lookup",
            "tenant_id",
            "thread_id",
            "artifact_version_id",
            "created_at",
        ),
    )
