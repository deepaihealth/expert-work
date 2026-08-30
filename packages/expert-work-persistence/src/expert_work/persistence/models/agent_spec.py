"""``agent_spec`` ORM model — Stream B.5."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import CHAR, DateTime, Index, Integer, Text, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from expert_work.persistence.base import Base


class AgentSpecRow(Base):
    """One persisted manifest. ``status`` is the soft-delete bit."""

    __tablename__ = "agent_spec"

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    tenant_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    version: Mapped[str] = mapped_column(Text, nullable=False)
    spec_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    spec_sha256: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    created_by: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    # 未发布的草稿 —— 编辑中的 manifest,**不影响任何 run**。
    #
    # 为什么在同一行而不是单独一张表:草稿与「某个 Agent 版本」是一对一的
    # (一个编辑缓冲区),没有生命周期、没有历史、发布即消失。单独一张表换来
    # 的只是一次 join 和一份要自己维护的引用完整性。
    #
    # 四列同生同灭:要么全是 NULL(没有草稿),要么全非 NULL。
    # ``draft_updated_by`` 记的是谁存的草稿,好让「别人有个草稿在这儿」这件事
    # 说得出是谁 —— 与主行的 ``created_by``(创建者,不随编辑变)不是一回事。
    draft_spec_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    draft_sha256: Mapped[str | None] = mapped_column(CHAR(64), nullable=True)
    draft_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    draft_updated_by: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "name", "version", name="agent_spec_tenant_name_version_uniq"
        ),
        Index("agent_spec_tenant_status_name_idx", "tenant_id", "status", "name"),
    )


class AgentSpecRevisionRow(Base):
    """Immutable manifest revision history — Stream HX-5 (Mini-ADR HX-E2).

    One row per create / content-changing update of an ``agent_spec``
    row, appended in the same transaction. Never updated or deleted;
    a rollback appends a new revision carrying an older snapshot.
    """

    __tablename__ = "agent_spec_revision"

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    tenant_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    agent_name: Mapped[str] = mapped_column(Text, nullable=False)
    agent_version: Mapped[str] = mapped_column(Text, nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    spec_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    spec_sha256: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    actor_id: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "agent_name",
            "agent_version",
            "revision",
            name="agent_spec_revision_uniq",
        ),
    )
