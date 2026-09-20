"""``skill`` + ``skill_version`` ORM models — Stream J.7a (Mini-ADR J-23).

Schema mirrors migration 0029_skill exactly. Tenant RLS is enforced at
the row level by the migration's policy; the application still passes
``tenant_id`` for clarity + so an in-memory backend can match semantics
without a Postgres GUC.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from expert_work.persistence.base import Base


class SkillRow(Base):
    """One row of ``skill`` — the named bundle.

    ``latest_version`` points at the current published version row (or
    0 between create + first version insert). The orchestrator's skill
    loader uses ``status='active'`` rows for bare ``name`` references.
    """

    __tablename__ = "skill"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    # Stream X (Mini-ADR X-1) — NULL = platform-global skill; non-NULL =
    # tenant-owned. The COALESCE(tenant_id, zero-uuid) unique index that
    # replaces UNIQUE(tenant_id, name) is declared in migration 0057, not
    # here (mirrors mcp_connector_catalog).
    tenant_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'draft'"))
    latest_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    description: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    category: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Stream X (Mini-ADR X-2) — minimum plan tier to bind this (platform)
    # skill. CHECK free|pro|enterprise lives in migration 0057.
    required_tier: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'free'"))
    # Capability Uplift Sprint #4 — migration 0043.
    # ``pinned`` is the operator's "do not Curator-touch" escape hatch.
    # ``last_used_at`` is the throttled activity timestamp; backfilled to
    # ``updated_at`` so existing rows look "recently used" and don't get
    # immediately stale-flagged on first Curator sweep.
    # ``state_changed_at`` advances on every Curator transition + every
    # manual PATCH status; powers "when did this skill go stale?" without
    # joining the audit log.
    pinned: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    state_changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    # Stream SE (Mini-ADR SE-A1) — migration 0065 + 0066. ``visibility``
    # defaults to 'tenant' so M0 human-authored skills keep current sharing;
    # agent self-authored skills go 'agent_private'. owner = per-user
    # persistent agent = (tenant_id, created_by_user_id, created_by_agent_name)
    # — stable across manifest versions (agent_name, not a version-specific
    # spec id). ``forked_from`` is a lineage pointer (no FK — deleting a
    # source skill must not cascade-delete its forks/derivatives).
    visibility: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'tenant'"))
    created_by_user_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    created_by_agent_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    forked_from: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    # Stream SE — SE-10 (Mini-ADR SE-A15), migration 0069. ``component_type``
    # widens the evolution target from skill-only to three text-class harness
    # components; ``target_tool_name`` names the tool a ``tool_description``
    # component supplements (NULL for every other component_type).
    component_type: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'skill'")
    )
    target_tool_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('draft', 'active', 'stale', 'archived')",
            name="skill_status_check",
        ),
        CheckConstraint("latest_version >= 0", name="skill_latest_version_nonneg"),
        CheckConstraint(
            "required_tier IN ('free', 'pro', 'enterprise')",
            name="skill_required_tier_check",
        ),
        CheckConstraint(
            "visibility IN ('agent_private', 'tenant')",
            name="skill_visibility_check",
        ),
        CheckConstraint(
            "component_type IN ('skill', 'system_prompt', 'tool_description', 'memory_entry')",
            name="skill_component_type_check",
        ),
        # SE-10: target_tool_name ⇔ component_type='tool_description'.
        CheckConstraint(
            "(component_type = 'tool_description') = (target_tool_name IS NOT NULL)",
            name="skill_target_tool_name_check",
        ),
        # The (tenant_id, name) uniqueness is enforced by the COALESCE
        # unique index ``skill_tenant_name_uniq`` declared in migration
        # 0057 (NULLs are distinct, so a plain UniqueConstraint would not
        # collide two platform NULL-tenant rows). Not declared on the model
        # — mirrors mcp_connector_catalog.
        Index("ix_skill_tenant_id", "tenant_id"),
        Index(
            "ix_skill_status_active",
            "tenant_id",
            "name",
            postgresql_where=text("status = 'active'"),
        ),
        # Curator sweep path: per tenant, scan only rows that *could*
        # transition. Stale + active rows that aren't pinned. Archived
        # + draft + pinned rows are inert from Curator's perspective.
        Index(
            "ix_skill_curator_scan",
            "tenant_id",
            "status",
            "last_used_at",
            postgresql_where=text("status IN ('active', 'stale') AND pinned = false"),
        ),
    )


class SkillVersionRow(Base):
    """One row of ``skill_version`` — an immutable published version.

    ``(skill_id, version)`` is unique; ``version`` starts at 1 and the
    Store auto-increments on ``add_version``. ``tool_names`` /
    ``required_models`` land as JSONB arrays of strings.
    """

    __tablename__ = "skill_version"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    # Stream X (Mini-ADR X-1) — NULL = platform version; non-NULL = tenant.
    tenant_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    skill_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("skill.id", ondelete="CASCADE", name="skill_version_skill_id_fk"),
        nullable=False,
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    prompt_fragment: Mapped[str] = mapped_column(Text, nullable=False)
    tool_names: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    description: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    category: Mapped[str | None] = mapped_column(Text, nullable=True)
    required_models: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    authored_by: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'human'"))
    # Capability Uplift Sprint #3 — migration 0042.
    # supporting_files: {"reference/foo.md": {"content": b64, "size": int, "mime": str}, ...}
    # Mini-ADR U-16 caps total bytes at 5 MB via DB CHECK; per-file 1 MB
    # + per-skill 64 entries enforced at the API layer.
    supporting_files: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    # Mini-ADR U-15: progressive disclosure opt-in. RT-ADR-11 flipped the
    # server_default to ``true`` (lazy) so new rows default to progressive
    # disclosure; migration 0113 flips it + retro-lazies curated platform
    # skills. Existing rows store an explicit value and are unaffected.
    lazy_load: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    # Mini-ADR U-21: blake2b-32 of canonicalized (prompt_fragment,
    # supporting_files). Recomputed at skill_view time to catch drift
    # (SQL injection / internal actor writing past the strict scan).
    content_hash: Mapped[bytes] = mapped_column(
        LargeBinary, nullable=False, server_default=text("''::bytea")
    )
    # Mini-ADR U-24: high-risk publish gate. Computed at write time when
    # tool_names ∩ HIGH_RISK_TOOLS ≠ ∅ or any supporting_files path
    # starts with "scripts/". M0 transparent (all writes are admin);
    # M1-K J.7b-1 agent-self-authored skills get gated.
    high_risk: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    # Stream SE (Mini-ADR SE-A1) — migration 0065. Provenance of how this
    # version was produced. NULL = human-authored (M0/admin history).
    # ``in_session`` = agent self-authored in a run (Layer A); ``distilled``
    # = posterior-distilled by the evolution worker (Layer B, SPARK). The
    # ``distilled_from_*`` columns point back at the real evidence so a
    # distilled version is fully traceable; ``evolution_round`` is the
    # co-evolve iteration (SE-6).
    evolution_origin: Mapped[str | None] = mapped_column(Text, nullable=True)
    distilled_from_trajectory_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    distilled_from_candidate_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), nullable=True
    )
    evolution_round: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (
        CheckConstraint("version >= 1", name="skill_version_positive"),
        CheckConstraint(
            "authored_by IN ('human', 'agent')", name="skill_version_authored_by_check"
        ),
        CheckConstraint(
            # 12 MiB — must hold a base64-encoded MAX_TOTAL_BYTES (8 MiB) zip
            # payload (~4/3 inflation + JSON envelope). Migration 0120.
            "octet_length(supporting_files::text) <= 12582912",
            name="skill_version_supporting_files_size_ck",
        ),
        CheckConstraint(
            "evolution_origin IS NULL OR evolution_origin IN ('in_session', 'distilled')",
            name="skill_version_evolution_origin_check",
        ),
        CheckConstraint("evolution_round >= 0", name="skill_version_evolution_round_nonneg"),
        UniqueConstraint("skill_id", "version", name="skill_version_skill_version_uq"),
        Index("ix_skill_version_tenant_id", "tenant_id"),
        Index("ix_skill_version_skill_id", "skill_id"),
    )


class SkillEvalResultRow(Base):
    """One row of ``skill_eval_result`` — Stream SE (Mini-ADR SE-A2).

    Replay-verification evidence for a candidate skill version: the
    ``baseline`` (without the skill) vs ``skill`` (with it) score over
    ``n_cases`` held-out replays, plus the resulting ``verdict``. The
    auto-promote gate (SE-7) requires a ``verdict='pass'`` row before a
    non-high-risk skill goes active (SE-A0). ``tenant_id`` is NULLABLE so
    platform-skill evaluations share the table (0057 NULL-tenant pattern).
    """

    __tablename__ = "skill_eval_result"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    skill_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("skill.id", ondelete="CASCADE", name="skill_eval_result_skill_id_fk"),
        nullable=False,
    )
    skill_version: Mapped[int] = mapped_column(Integer, nullable=False)
    baseline_score: Mapped[float] = mapped_column(Float, nullable=False)
    skill_score: Mapped[float] = mapped_column(Float, nullable=False)
    delta: Mapped[float] = mapped_column(Float, nullable=False)
    n_cases: Mapped[int] = mapped_column(Integer, nullable=False)
    replay_source: Mapped[str] = mapped_column(Text, nullable=False)
    verdict: Mapped[str] = mapped_column(Text, nullable=False)
    high_risk: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    evolution_round: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (
        CheckConstraint("skill_version >= 1", name="skill_eval_result_version_positive"),
        CheckConstraint("n_cases >= 0", name="skill_eval_result_n_cases_nonneg"),
        CheckConstraint(
            "replay_source IN ('trajectory', 'eval_dataset')",
            name="skill_eval_result_replay_source_check",
        ),
        CheckConstraint(
            "verdict IN ('pass', 'fail', 'inconclusive')",
            name="skill_eval_result_verdict_check",
        ),
        Index("ix_skill_eval_result_tenant_id", "tenant_id"),
        Index("ix_skill_eval_result_skill", "skill_id", "skill_version"),
    )


class SkillRunUsageRow(Base):
    """One row of ``skill_run_usage`` — Stream SE (Mini-ADR SE-A11, SE-7d-1).

    Skill-centric attribution for regression rollback: a run
    (``thread_id``) loaded ``skill_version`` and ended with ``outcome``.
    The rollback monitor (SE-7d-3) aggregates these per ``(skill_id,
    skill_version)`` over a rolling window — the ``ix_skill_run_usage_window``
    index makes that a range scan. ``tenant_id`` is NULLABLE so platform-skill
    usage shares the table (0057 NULL-tenant pattern); ``agent_name`` is half
    the circuit-breaker scope key ``{tenant}:{agent}``.
    """

    __tablename__ = "skill_run_usage"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    skill_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("skill.id", ondelete="CASCADE", name="skill_run_usage_skill_id_fk"),
        nullable=False,
    )
    skill_version: Mapped[int] = mapped_column(Integer, nullable=False)
    thread_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    agent_name: Mapped[str] = mapped_column(Text, nullable=False)
    outcome: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (
        CheckConstraint("skill_version >= 1", name="skill_run_usage_version_positive"),
        # B-84 (migration 0159) — ``viewed`` is the fifth value and the only
        # one that is NOT a run terminal outcome: it records that the model
        # actually called ``skill_view`` on this version. It must never enter
        # the rollback sample (``decide_rollback`` would count it as a
        # non-success); ``skill_run_usage_window`` filters it out.
        CheckConstraint(
            "outcome IN ('success', 'failed', 'max_steps', 'cancelled', 'viewed')",
            name="skill_run_usage_outcome_check",
        ),
        Index("ix_skill_run_usage_tenant_id", "tenant_id"),
        Index(
            "ix_skill_run_usage_window",
            "tenant_id",
            "skill_id",
            "skill_version",
            "created_at",
        ),
    )


class SkillPredictionVerdictRow(Base):
    """One row of ``skill_prediction_verdict`` — Stream SE (SE-11, SE-A18/A19).

    Predict→falsify ledger: the replay predicted a lift from ``baseline_score``
    to ``skill_score``; production gave ``observed_rate``; this records how much
    held (``realized_fraction``) + the label. Diagnostic only — never gates
    archive (``decide_rollback`` does). NULL-tenant + ENABLE-only RLS mirrors
    ``skill_eval_result``; the rollback sweep writes cross-tenant as owner.
    """

    __tablename__ = "skill_prediction_verdict"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    skill_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("skill.id", ondelete="CASCADE", name="skill_pred_verdict_skill_id_fk"),
        nullable=False,
    )
    skill_version: Mapped[int] = mapped_column(Integer, nullable=False)
    verdict: Mapped[str] = mapped_column(Text, nullable=False)
    predicted_delta: Mapped[float] = mapped_column(Float, nullable=False)
    realized_delta: Mapped[float] = mapped_column(Float, nullable=False)
    realized_fraction: Mapped[float] = mapped_column(Float, nullable=False)
    baseline_score: Mapped[float] = mapped_column(Float, nullable=False)
    skill_score: Mapped[float] = mapped_column(Float, nullable=False)
    observed_rate: Mapped[float] = mapped_column(Float, nullable=False)
    n_window: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (
        CheckConstraint("skill_version >= 1", name="skill_pred_verdict_version_positive"),
        CheckConstraint("n_window >= 0", name="skill_pred_verdict_n_window_nonneg"),
        CheckConstraint(
            "verdict IN ('effective', 'partially_effective', 'ineffective', 'mixed', 'harmful')",
            name="skill_pred_verdict_verdict_check",
        ),
        Index("ix_skill_pred_verdict_tenant_id", "tenant_id"),
        Index(
            "ix_skill_pred_verdict_version",
            "tenant_id",
            "skill_id",
            "skill_version",
            "created_at",
        ),
    )


class SkillPromoteRequestRow(Base):
    """One row of ``skill_promote_request`` — Stream SE (SE-8, Mini-ADR SE-A13b).

    agent_private→tenant visibility-promotion approval, orthogonal to
    ``skill.status`` (draft→active). ``status='pending'`` rows are the review
    queue; APPROVE flips the parent skill's ``visibility`` to ``tenant``.
    ``tenant_id`` is NOT NULL — agent_private→tenant is always within a tenant
    (no platform rows) — but the table still uses the SAME NULL-tenant RLS shape
    as ``skill`` / ``skill_eval_result`` (ENABLE-only) so the SE-8 review queue
    reads cross-tenant as the table owner while tenant sessions stay isolated.
    A partial unique index keeps at most one ``pending`` request per skill.
    """

    __tablename__ = "skill_promote_request"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    skill_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("skill.id", ondelete="CASCADE", name="skill_promote_request_skill_id_fk"),
        nullable=False,
    )
    skill_version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'pending'"))
    requested_by_user_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    requested_by_agent_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    reason: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    decided_by_user_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decision_reason: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (
        CheckConstraint("skill_version >= 1", name="skill_promote_request_version_positive"),
        CheckConstraint(
            "status IN ('pending', 'approved', 'rejected', 'superseded')",
            name="skill_promote_request_status_check",
        ),
        # At most one pending request per skill (a new propose supersedes the
        # old; APPROVE/REJECT moves it out of 'pending').
        Index(
            "uq_skill_promote_request_pending",
            "skill_id",
            unique=True,
            postgresql_where=text("status = 'pending'"),
        ),
        Index("ix_skill_promote_request_queue", "tenant_id", "status", "created_at"),
    )


class SkillEvolutionKillSwitchRow(Base):
    """One row of ``skill_evolution_kill_switch`` — Stream SE (SE-8, SE-A13c).

    Persistent emergency stop for the auto-promote pipeline. ``scope='global'``
    rows carry ``tenant_id IS NULL`` (platform, system_admin only, sharing the
    0057 NULL-tenant RLS); ``scope='tenant'`` rows carry the tenant id (tenant
    admin manages their own). A partial unique index keeps one global row and at
    most one row per tenant. Complements the in-process SE-7b ``CircuitBreaker``
    with a durable, cross-restart / cross-replica manual override.
    """

    __tablename__ = "skill_evolution_kill_switch"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    scope: Mapped[str] = mapped_column(Text, nullable=False)
    tenant_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    engaged: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    reason: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    engaged_by_user_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    engaged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    released_by_user_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (
        CheckConstraint(
            "(scope = 'global' AND tenant_id IS NULL) "
            "OR (scope = 'tenant' AND tenant_id IS NOT NULL)",
            name="skill_evolution_kill_switch_scope_check",
        ),
        Index(
            "uq_skill_evolution_kill_switch_global",
            "scope",
            unique=True,
            postgresql_where=text("scope = 'global'"),
        ),
        Index(
            "uq_skill_evolution_kill_switch_tenant",
            "tenant_id",
            unique=True,
            postgresql_where=text("scope = 'tenant'"),
        ),
    )
