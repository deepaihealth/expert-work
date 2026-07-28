"""Platform delegation-gate config ORM model — perf phase2 PR3.

A single-row (``id == "singleton"``) table storing the platform-global
delegation-gate capacity: ``max_concurrent_delegations``. An absent row means
"not configured" → the platform falls back to its built-in default.

Platform-global, tenant-less (like ``platform_tool_budget_config`` /
``platform_judge_config``) — no RLS policy; all access goes through
``bypass_rls_session()``.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Integer, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from expert_work.persistence.base import Base


class PlatformDelegationConfigRow(Base):
    """The single platform delegation-gate config row."""

    __tablename__ = "platform_delegation_config"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    max_concurrent_delegations: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_by: Mapped[str | None] = mapped_column(Text, nullable=True)
