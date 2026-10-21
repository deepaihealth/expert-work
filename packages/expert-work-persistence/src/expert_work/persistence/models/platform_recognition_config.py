"""Platform document-recognition config ORM model — B-209 (migration ``0169``).

A single-row (``id == "singleton"``) table: the models the knowledge base uses
to read scanned pages (primary + fallback), the figure-description model, and
the per-document recognition page cap. Non-secret (provider/model names only —
keys live in ``platform_provider_secret``).

Platform-global, tenant-less (like ``platform_judge_config``) — no RLS policy.
An absent row means "not configured" → the built-in defaults apply.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Integer, Text, func, text
from sqlalchemy.orm import Mapped, mapped_column

from expert_work.persistence.base import Base


class PlatformRecognitionConfigRow(Base):
    """The single platform document-recognition config row."""

    __tablename__ = "platform_recognition_config"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    ocr_provider: Mapped[str | None] = mapped_column(Text, nullable=True)
    ocr_model: Mapped[str | None] = mapped_column(Text, nullable=True)
    fallback_provider: Mapped[str | None] = mapped_column(Text, nullable=True)
    fallback_model: Mapped[str | None] = mapped_column(Text, nullable=True)
    figure_provider: Mapped[str | None] = mapped_column(Text, nullable=True)
    figure_model: Mapped[str | None] = mapped_column(Text, nullable=True)
    max_pages: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1000"))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_by: Mapped[str | None] = mapped_column(Text, nullable=True)
