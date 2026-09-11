"""``artifact`` / ``artifact_version`` ORM models — Stream J.9.

A logical :class:`ArtifactRow` (a named file) owns one or more
:class:`ArtifactVersionRow` revisions. Content lives in the user's J.15
persistent workspace volume; these rows carry only metadata. See
migration ``0019_artifact`` and STREAM-J-DESIGN § 10.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import BigInteger, DateTime, Integer, Text, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from expert_work.persistence.base import Base


class ArtifactRow(Base):
    """A logical artifact — a named file, scoped to ``(tenant, user, agent)`` (J.9).

    RLS (migration ``0019``) enforces both ``app.tenant_id`` and
    ``app.user_id``.
    """

    __tablename__ = "artifact"

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    tenant_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    user_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    #: B-50 工作区分层 —— 这条产物属于哪个 agent。``sanitize_agent_key(spec.metadata.name)``,
    #: 与沙箱的 ``/opt/skills/<agent_key>`` 同一个值。空串 = 归属不明的历史产物
    #: (迁移 ``0154`` 回填时反推链断了的那批),与 ``ToolContext.agent_key`` 的
    #: 「空串=没绑 agent」同一套语义。
    agent_key: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    name: Mapped[str] = mapped_column(Text, nullable=False)
    #: document / code / data / other — declared by the agent.
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    #: Version number of the newest ``artifact_version`` revision.
    latest_version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    #: Soft-delete timestamp (Mini-ADR J-25). NULL = active.
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: ObjectStore key when the workspace files have been archived.
    #: CHECK ``artifact_archive_consistency`` enforces ``deleted_at IS NOT NULL``
    #: when this is set. The supervisor-side archive flow ships in a
    #: follow-up step (reuses J.15 volume archive path); ``J.9-step1``
    #: leaves this NULL and goes active → soft → hard directly.
    archived_object_key: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "user_id", "agent_key", "name", name="artifact_identity_uniq"
        ),
    )


class ArtifactVersionRow(Base):
    """One saved revision of an artifact (J.9).

    ``artifact_id`` is a bare UUID column — no FK. The parent
    ``artifact`` table is ``FORCE`` RLS, where FK referential-integrity
    checks are a known footgun (Mini-ADR J-1a). ``tenant_id`` /
    ``user_id`` are denormalised here so this table carries the same
    combined RLS policy.
    """

    __tablename__ = "artifact_version"

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    artifact_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    tenant_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    user_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    #: Path of the file inside the user's persistent workspace volume.
    path_in_workspace: Mapped[str] = mapped_column(Text, nullable=False)
    #: Filled lazily on the first content read — NULL until then.
    size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    sha256: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_in_thread: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("artifact_id", "version", name="artifact_version_identity_uniq"),
    )
