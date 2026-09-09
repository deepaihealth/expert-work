"""``eval_dataset`` + ``curation_candidate`` ORM models — Stream J.12.

Mini-ADR J-19 / J-27 / J-43 (STREAM-J-DESIGN § 17). Schema mirrors
migration 0034_eval_dataset exactly. Tenant RLS is enforced at the row
level by the migration's policies; the application still passes
``tenant_id`` so an in-memory backend can match semantics without a
Postgres GUC.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from expert_work.persistence.base import Base

_SOURCE_VALUES = "('golden', 'trajectory', 'regression')"
_OUTCOME_VALUES = "('success', 'failed', 'max_steps', 'cancelled')"
_SIGNAL_VALUES = "('negative_feedback', 'failed_outcome', 'positive_feedback', 'implicit_success')"
_RATING_VALUES = "('up', 'down')"
_STATUS_VALUES = "('pending', 'promoted', 'dismissed')"


class EvalDatasetRow(Base):
    """One curated eval case — shared with J.13.

    A "dataset" is the set of rows sharing ``(tenant_id, agent_name,
    name)``; ``name`` is not unique.
    """

    __tablename__ = "eval_dataset"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    agent_name: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    input: Mapped[dict[str, object]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    expected: Mapped[dict[str, object] | None] = mapped_column(JSONB, nullable=True)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    source_trajectory_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_user_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint(f"source IN {_SOURCE_VALUES}", name="eval_dataset_source_valid"),
        Index("ix_eval_dataset_tenant_id", "tenant_id"),
        Index("ix_eval_dataset_agent", "tenant_id", "agent_name"),
    )


class CurationCandidateRow(Base):
    """One trajectory the curation worker flagged for human review.

    Unique per ``(tenant_id, trajectory_key)`` — the worker upserts
    insert-if-absent so a trajectory becomes a candidate at most once.
    """

    __tablename__ = "curation_candidate"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    agent_name: Mapped[str] = mapped_column(Text, nullable=False)
    agent_version: Mapped[str | None] = mapped_column(Text, nullable=True)
    thread_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    user_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    trajectory_key: Mapped[str] = mapped_column(Text, nullable=False)
    outcome: Mapped[str] = mapped_column(Text, nullable=False)
    signal: Mapped[str] = mapped_column(Text, nullable=False)
    feedback_rating: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'pending'"))
    eval_dataset_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: 4.4 (#5) — SE-6 skill-evolution marker, orthogonal to ``status`` (the
    #: J.12 human-review verdict). Set once the evolution worker has processed
    #: this candidate so it isn't re-distilled every interval. NULL = not yet
    #: evolved (the worker's scan filter).
    evolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: SE-16 (SE-A40) — transient-failure retries; the worker gives up
    #: (marks evolved) at 3. Added in migration 0107.
    retry_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0"), default=0
    )
    #: P-2 — 被踩的那一轮 / 用户原话 / 👎→👍 改票时间(NULL = 没改过)/
    #: 这一踩的来源('console' = 员工,'external' = 终端用户;NULL = worker
    #: 兜底建的候选,归因不到某一条 feedback)。四列是同一组反馈快照。
    #: ``feedback_source`` 的写入方与展示方在 PR4 Task 20,本 PR 只建列。
    feedback_run_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    feedback_comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    feedback_changed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    feedback_source: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        CheckConstraint(f"outcome IN {_OUTCOME_VALUES}", name="curation_candidate_outcome_valid"),
        CheckConstraint(f"signal IN {_SIGNAL_VALUES}", name="curation_candidate_signal_valid"),
        CheckConstraint(
            f"feedback_rating IN {_RATING_VALUES}", name="curation_candidate_rating_valid"
        ),
        CheckConstraint(f"status IN {_STATUS_VALUES}", name="curation_candidate_status_valid"),
        CheckConstraint(
            "feedback_source IS NULL OR feedback_source IN ('console', 'external')",
            name="candidate_feedback_source_valid",
        ),
        UniqueConstraint("tenant_id", "trajectory_key", name="curation_candidate_trajectory_uniq"),
        Index("ix_curation_candidate_tenant_id", "tenant_id"),
        Index("ix_curation_candidate_agent", "tenant_id", "agent_name"),
        Index(
            "ix_curation_candidate_pending",
            "tenant_id",
            "agent_name",
            postgresql_where=text("status = 'pending'"),
        ),
        Index(
            "ix_curation_candidate_feedback_run",
            "tenant_id",
            "feedback_run_id",
            postgresql_where=text("feedback_run_id IS NOT NULL"),
        ),
    )
