"""``sandbox_instance`` ORM model — Stream F.1.

One row per ``exec_python`` sandbox container; see migration
``0012_sandbox_instance`` and STREAM-F-DESIGN § 4.6.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import DateTime, Index, Integer, Numeric, Text, func, text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from expert_work.persistence.base import Base


class SandboxInstanceRow(Base):
    """The Sandbox Supervisor's record of one sandbox container's lifecycle."""

    __tablename__ = "sandbox_instance"

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    tenant_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    #: Owning user (Stream J.15). ``None`` for the pre-J.15 ephemeral
    #: tmpfs path — a sandbox acquired without a user scope.
    user_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    #: The per-user persistent workspace this container mounts
    #: (``user_workspace.id``). ``None`` when ``user_id`` is ``None``.
    workspace_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    image_ref: Mapped[str] = mapped_column(Text, nullable=False)
    node: Mapped[str] = mapped_column(Text, nullable=False)
    #: ``None`` while ``CREATING`` — set once ``docker run`` returns an id.
    container_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: CREATING / IN_USE / DESTROYED / FAILED — STREAM-F-DESIGN § 2.2.
    state: Mapped[str] = mapped_column(Text, nullable=False)
    thread_id: Mapped[str] = mapped_column(Text, nullable=False)
    cpu_quota: Mapped[Decimal] = mapped_column(Numeric(4, 2), nullable=False)
    memory_mb: Mapped[int] = mapped_column(Integer, nullable=False)
    pids_limit: Mapped[int] = mapped_column(Integer, nullable=False)
    timeout_s: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    acquired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: Time of the last ``exec`` (Stream J.15 warm sessions). The reaper
    #: reclaims a session idle past ``session_idle_ttl_s``; ``None`` until
    #: the first exec — the reaper then falls back to ``acquired_at``.
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    destroyed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: release / idle_timeout / cancelled / oom — ``None`` until terminal.
    destroy_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: B-60 —— 沙箱内布局版本(迁移 0155)。``'user-root'`` = NAS 直接挂 ``/workspace``
    #: 的旧布局;``'agent-ns'`` = NAS 挂 ``/mnt/workspace``、每次 exec 在自己的命名空间
    #: 里把 agent 目录 bind 成 ``/workspace``。``AgentSandboxClient.acquire`` 拿到与本
    #: 进程不同布局的热会话就销毁重建(``destroy_reason='layout_mismatch'``)。字面量
    #: 的单源在 ``sandbox_instance_store.SANDBOX_LAYOUT_*``,这里只是 server_default。
    layout: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'user-root'"))

    __table_args__ = (
        Index("sandbox_instance_tenant_state_idx", "tenant_id", "state"),
        Index("sandbox_instance_state_acquired_idx", "state", "acquired_at"),
    )
