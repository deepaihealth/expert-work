"""Platform dynamic-worker limits ORM model — B3 PR2.

A single-row (``id == "singleton"``) table storing the platform-global
``dynamic_worker`` limits: ``max_concurrent``, ``max_per_run``, and
``max_iterations``. An absent row means "not configured" → the platform
falls back to its built-in defaults.

Platform-global, tenant-less (like ``platform_tool_budget_config`` /
``platform_judge_config``) — no RLS policy; all access goes through
``bypass_rls_session()``.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Integer, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from expert_work.persistence.base import Base


class PlatformDynamicWorkerConfigRow(Base):
    """The single platform dynamic-worker limits row."""

    __tablename__ = "platform_dynamic_worker_config"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    max_concurrent: Mapped[int] = mapped_column(Integer, nullable=False)
    max_per_run: Mapped[int] = mapped_column(Integer, nullable=False)
    max_iterations: Mapped[int] = mapped_column(Integer, nullable=False)
    # 弹性 worker 预算(2026-08-28)— 平台硬顶档:per-agent 请求被 clamp 到
    # 这三个值;上面三列退化为「manifest 未请求时的平台默认档」。
    cap_max_concurrent: Mapped[int] = mapped_column(Integer, nullable=False)
    cap_max_per_run: Mapped[int] = mapped_column(Integer, nullable=False)
    cap_max_iterations: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_by: Mapped[str | None] = mapped_column(Text, nullable=True)
