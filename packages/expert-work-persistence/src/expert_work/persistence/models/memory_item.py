"""``memory_item`` ORM model — Stream J.3 long-term memory.

Cross-session memory for the per-user persistent agent. Each row is one
remembered fact or episodic summary, scoped to ``(tenant_id, user_id)``
and carrying an embedding for semantic retrieval.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pgvector.sqlalchemy import Vector
from sqlalchemy import CHAR, DateTime, Float, Index, Integer, String, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from expert_work.persistence.base import Base
from expert_work.persistence.embedding import EMBEDDING_DIM


class MemoryItemRow(Base):
    """One long-term memory — a fact or episodic summary (Stream J.3).

    Tenant-scoped *and* user-scoped: RLS (migration ``0017``) enforces
    both ``app.tenant_id`` and ``app.user_id``.
    """

    __tablename__ = "memory_item"

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    tenant_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    user_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    # Stream Agent-Templates (M1-5c) — owning agent for episodic memory (per-agent
    # isolation); NULL for shared fact rows. Migration 0098.
    agent_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIM), nullable=False)
    # Stream K.K7 — SHA-256 hex of ``lower(trim(content))``. Filled by
    # the application store at write time; the partial UNIQUE index
    # ``(tenant_id, user_id, content_hash, COALESCE(agent_name, ''))
    # WHERE deleted_at IS NULL AND invalid_at IS NULL AND expired_at IS NULL``
    # (migrations 0098 + P5b 0127) backs ON CONFLICT DO NOTHING dedup —
    # bi-temporal-hidden rows (superseded / expired) are excluded so a
    # re-asserted fact can be re-inserted (see supersede()).
    content_hash: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    source_thread_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Stream Memory-Enhance (M-2) — importance/confidence scoring (migration
    # 0099). ``importance`` feeds the writeback write-filter; ``confidence``
    # records extraction certainty (1.0 = user-asserted via the M-4 correction
    # API). Both default 0.5 (neutral) so backfilled legacy rows are unbiased.
    # CHECK ``memory_item_score_check`` keeps both in [0, 1].
    importance: Mapped[float] = mapped_column(Float, nullable=False, server_default=text("0.5"))
    confidence: Mapped[float] = mapped_column(Float, nullable=False, server_default=text("0.5"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    last_used_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    # P5a — access reinforcement counter. Bumped on recall; feeds the ranking
    # frequency boost and replaces the (buggy) time-approximation for the
    # consolidator's "never retrieved" purge protection.
    access_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    # Stream K.K6 — soft-delete column. ``retrieve`` and the per-user
    # list endpoint filter out rows with ``deleted_at IS NOT NULL``;
    # the retention sweep hard-deletes 90 days after (implemented,
    # see ``hard_delete_expired`` / ``retention-cleanup-job``).
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Capability Uplift Sprint #6 (Mini-ADR U-5 / U-6) — keyword-search
    # vector backing hybrid retrieve. Populated app-side from
    # ``tokenize_for_search(content)`` under the ``simple`` config so
    # jieba-segmented CJK works without a Postgres extension. Nullable
    # so the migration is non-blocking; the GIN index in migration 0040
    # backs the ``@@ plainto_tsquery`` filter.
    content_tsv: Mapped[str | None] = mapped_column(TSVECTOR(), nullable=True)
    # Capability Uplift Sprint #7 (Mini-ADR U-33) — lifecycle columns.
    # CHECK constraint ``memory_item_status_check`` mirrors the
    # ``MemoryStatus`` Literal in expert_work.protocol.memory_item.
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        server_default=text("'transient'"),
    )
    consolidated_into: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        nullable=True,
    )
    consolidated_from: Mapped[list[UUID]] = mapped_column(
        JSONB,
        nullable=False,
        server_default=text("'[]'::jsonb"),
    )
    last_reviewed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    #: Stream HX-2 (Mini-ADR HX-B3) -- set when a user 👎 flags this
    #: item's source thread; consolidator SUB-PASS 2 reviews flagged
    #: items regardless of age and clears the flag via mark_reviewed.
    review_flagged_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    # ---- Stream P5b (溯源 + bi-temporal) migration 0126 ----
    # source_run_id: Text (mirrors source_thread_id — a stringified UUID, not a
    # uuid column, to keep the two provenance columns the same type).
    source_run_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    valid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    invalid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    supersedes: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    superseded_by: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    expected_valid_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # ---- B-300 PR 2, migration 0189 ----
    # origin: ``user_stated`` / ``assistant_derived`` / NULL (not recorded). No CHECK
    # constraint — the application maps an unknown value to NULL on read.
    origin: Mapped[str | None] = mapped_column(Text, nullable=True)
    # embedding_model: the ``provider/model`` that computed ``embedding``; NULL = unknown.
    embedding_model: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (Index("memory_item_tenant_user_idx", "tenant_id", "user_id"),)
