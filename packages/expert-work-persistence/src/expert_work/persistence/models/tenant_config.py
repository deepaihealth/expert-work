"""``tenant_config`` ORM model — Stream C.7."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Boolean, DateTime, Float, Integer, String, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from expert_work.persistence.base import Base


class TenantConfigRow(Base):
    """Per-tenant runtime configuration (one row per tenant)."""

    __tablename__ = "tenant_config"

    tenant_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    plan: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'free'"))
    # Stream U — PR E. Tenant lifecycle status ('active'|'suspended').
    # CHECK constraint in migration 0053 mirrors the Python ``TenantStatus``.
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="active")
    model_credentials_ref: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    mcp_allowlist: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    rate_limit_override: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    pii_fields: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    # E.8 / E.9: per-tenant tool gates, default ``[]``.
    # ``http_tool_allowlist`` empty ↔ allow all public hosts (SSRF blocked by
    # the tool); ``http_tool_denylist`` blocks specific hosts even under
    # allow-all. ``mcp_servers`` empty ↔ no MCP.
    http_tool_allowlist: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    http_tool_denylist: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    mcp_servers: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    # D.3 retention: per-tenant TTL for the two largest tables that
    # actually need pruning. CHECK constraints in migration 0010
    # bound both to (0, 3650] days.
    audit_retention_days: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("90"), default=90
    )
    event_log_retention_days: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("30"), default=30
    )
    # B-63 (migration 0183): per-tenant retention windows for user uploads and
    # artifacts, counted from LAST USE. NULL → the retention job's platform default
    # (90). CHECK constraints bound both to [7, 365].
    upload_retention_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    artifact_retention_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Capability Uplift Sprint #1 — Mini-ADR U-2.
    # ``warn`` is platform-wide default; ``block`` is opt-in for
    # high-compliance tenants. CHECK constraint in migration 0039.
    trigger_fire_scan_mode: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'warn'"), default="warn"
    )
    # Capability Uplift Sprint #6 — Mini-ADR U-5.
    # ``hybrid`` is platform-wide default; ``vector`` is the legacy
    # pure-pgvector path retained as an escape hatch. CHECK constraint
    # in migration 0041.
    memory_recall_mode: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'hybrid'"), default="hybrid"
    )
    # Capability Uplift Sprint #4 — Mini-ADR U-28. Curator thresholds.
    # Defaults 30 / 90 derive from external skill-marketplace
    # observations; M1-K J.7b-1 will revisit. CHECK constraints +
    # cross-field invariant (archive > stale) live in migration 0044.
    skill_stale_days: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("30"), default=30
    )
    skill_archive_days: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("90"), default=90
    )
    # Capability Uplift Sprint #7 — Mini-ADR U-38. MemoryConsolidator
    # thresholds. Defaults derive from Hermes consolidation observations
    # + M0 estimates; M1 dogfood (2-4 wk after enabling) will revisit.
    # CHECK constraints + cross-field bounds live in migration 0046.
    memory_consolidation_min_cluster_size: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("3"), default=3
    )
    memory_consolidation_similarity: Mapped[float] = mapped_column(
        Float(precision=2), nullable=False, server_default=text("0.85"), default=0.85
    )
    memory_purge_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true"), default=True
    )
    memory_purge_min_age_days: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("30"), default=30
    )
    # Stream P5b-2b ⑦ — predictive review opt-in. When True, MemoryConsolidator's
    # SUB-PASS 3 proactively re-reviews facts whose predicted validity window
    # has come due. Off by default (LLM cost) — tenants opt in explicitly.
    # Added in migration 0129.
    memory_predictive_review_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false"), default=False
    )
    # Stream O — Mini-ADR O-2. Credentials mode + tool API key map.
    # Stream Y-1 narrowed ``CredentialsMode`` to ``Literal["platform"]``;
    # the CHECK constraint (migration 0058) mirrors it as ``IN ('platform')``.
    credentials_mode: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'platform'"), default="platform"
    )
    tool_credentials: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    # Stream O — Mini-ADR O-14. Per-tenant MCP server credentials
    # (``{server_name: secret_ref}``). Added in migration 0048.
    mcp_credentials: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    # Stream R — Mini-ADR R-9. The agent a tenant's members get by default
    # when they don't pick one. NULL → platform fallback (canonical-agent).
    # Added in migration 0052.
    default_agent_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Stream W — Mini-ADR W-4. When False, the tenant may only instantiate MCP
    # servers from the platform catalog; off-catalog custom registration is
    # rejected. Defaults True (preserves Stream V self-service). Added in
    # migration 0056.
    allow_custom_mcp_servers: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true"), default=True
    )
    # SE-16 (SE-A41) — per-tenant skill-evolution rollout gate (ANDed with
    # the platform master switch). Added in migration 0107.
    skill_evolution_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false"), default=False
    )
    # SE-16 (SE-A45) — implicit-candidate judge sample rate (0-100, CHECK in
    # migration 0109). Added in migration 0109.
    skill_evolution_judge_sample_pct: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("5"), default=5
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_by: Mapped[str] = mapped_column(Text, nullable=False)
