"""Per-tenant runtime configuration schemas — Stream C.7.

Distinct from :class:`expert_work.protocol.agent_spec.TenantConfig`
(which is an *agent manifest* fragment). The schemas here back the
``tenant_config`` table from migration 0007 and the C.7 admin
``GET / PUT /v1/tenants/{tid}/config`` endpoints.

Subsystems/15 § 6 + STREAM-C-DESIGN § 2.8 set the contract:

* ``model_credentials_ref`` is ``{<provider>: "kms://..."}``. The
  control plane stores only the URI; F.6 ``SecretStore`` resolves it
  on the LLM-call hot path.
* ``mcp_allowlist`` controls which MCP servers the tenant may
  connect to (Stream E).
* ``rate_limit_override`` is consumed by :class:`TenantRateLimitMiddleware`
  in M1; in M0 it lands here as forward-compatible storage.
* ``pii_fields`` feeds the Stream D PII redactor.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from expert_work.protocol.provider_catalog import Tool

__all__ = [
    "FILE_RETENTION_DEFAULT_DAYS",
    "FILE_RETENTION_FIELDS",
    "FILE_RETENTION_MAX_DAYS",
    "FILE_RETENTION_MIN_DAYS",
    "CredentialsMode",
    "MemoryRecallMode",
    "TenantConfigPatch",
    "TenantConfigRecord",
    "TenantPlan",
    "TenantStatus",
    "TriggerFireScanMode",
]

# Capability Uplift Sprint #1 — Mini-ADR U-2.
# ``warn`` is the platform-wide default: a fire-time injection match
# emits ``TRIGGER_PROMPT_INJECTION_WARN`` and the run still starts.
# High-compliance tenants opt in to ``block``: a match emits
# ``TRIGGER_PROMPT_INJECTION_BLOCKED`` and the run does not start.
TriggerFireScanMode = Literal["warn", "block"]

# Capability Uplift Sprint #6 — Mini-ADR U-5.
# ``hybrid`` is the platform-wide default: ``MemoryStore.retrieve()``
# runs vector + Postgres full-text and fuses via Reciprocal Rank Fusion
# (k=60). Tenants can opt out to ``vector`` to keep the legacy pure-
# pgvector cosine path (e.g. for workloads where the eval baseline
# regressed against expectations).
MemoryRecallMode = Literal["hybrid", "vector"]

# Stream Y-1 — LLM platform-exclusive. Tenant credentials mode.
# ``platform`` is now the ONLY mode: all LLM provider + tool API key
# lookups resolve to the platform-level secret_refs. The former
# ``tenant`` BYOK mode (resolve to the tenant's own secret_refs) was
# removed — LLM keys are platform-exclusive. The ``model_credentials_ref``
# / ``tool_credentials`` fields are retained dormant (no destructive
# schema drop) but are never read at resolve time.
CredentialsMode = Literal["platform"]

# Stream U — PR E. Tenant lifecycle status.
# ``active`` (default): the tenant operates normally.
# ``suspended``: the tenant is administratively disabled; enforcement
# (rejecting its requests) lands in PR E Task 2. CHECK constraint in
# migration 0053 mirrors this Literal.
TenantStatus = Literal["active", "suspended"]


class TenantPlan(StrEnum):
    """Pricing tier label. M0 informational only; M1 derives default quotas."""

    FREE = "free"
    PRO = "pro"
    ENTERPRISE = "enterprise"


_RETENTION_MIN_DAYS = 1
_RETENTION_MAX_DAYS = 3650

# B-63 —— 附件与产物的保留期(按最后一次使用算起)。租户可在这个范围里改;``None`` =
# 跟随平台默认(留存作业的 ``EXPERT_WORK_RETENTION_UPLOAD_RETENTION_DAYS`` /
# ``..._ARTIFACT_RETENTION_DAYS``,默认 90)。上限一年:没有「永不清理」这一档;下限一周
# (用户拍板:再短就是误操作的量级)。迁移 0183 的 CHECK 与这里同一范围。
FILE_RETENTION_MIN_DAYS = 7
FILE_RETENTION_MAX_DAYS = 365
#: 平台默认天数(租户没设时)。控制面判「这次是不是改小」时拿它当没设那一档的值;
#: 留存作业实际用的是自己的环境变量 ``EXPERT_WORK_RETENTION_{UPLOAD,ARTIFACT}_RETENTION_DAYS``,
#: **两者必须一致**(作业那边的默认值同样是 90,见 ``retention_cleanup_job/settings.py``)。
FILE_RETENTION_DEFAULT_DAYS = 90
#: 按租户可调、「显式 ``null`` = 平台默认」、改小要确认的保留期字段。B-282 加了对话工作目录那一项。
FILE_RETENTION_FIELDS: tuple[str, ...] = (
    "upload_retention_days",
    "artifact_retention_days",
    "conversation_workspace_retention_days",
)

# Capability Uplift Sprint #4 — Mini-ADR U-28.
# Curator threshold bounds. Defaults 30 / 90 derive from external
# skill-marketplace observations; M1-K J.7b-1 will revisit after 2-4
# weeks of real agent-self-create data. The 365 / 730 day ceilings
# guard against accidental "effectively disabled" settings — anything
# longer than 1-2 years and the Curator stops being useful infrastructure.
_SKILL_STALE_MIN_DAYS = 1
_SKILL_STALE_MAX_DAYS = 365
_SKILL_ARCHIVE_MIN_DAYS = 2
_SKILL_ARCHIVE_MAX_DAYS = 730

# Capability Uplift Sprint #7 — Mini-ADR U-38.
# MemoryConsolidator threshold bounds.
#   min_cluster_size:  N=2 too aggressive (any 2 similar items trip),
#                      N>5 too conservative (real clusters missed)
#   similarity:        embedding cosine ≥ 0.85 = "clearly synonymous"
#                      empirical floor; below 0.7 too permissive,
#                      above 0.99 only exact-paraphrase clusters trigger
#   purge_min_age:     7d floor = "still in use"; 365d ceiling = ~off
_MEMORY_CLUSTER_MIN_SIZE = 2
_MEMORY_CLUSTER_MAX_SIZE = 20
_MEMORY_SIMILARITY_MIN = 0.7
_MEMORY_SIMILARITY_MAX = 0.99
_MEMORY_PURGE_MIN_DAYS = 7
_MEMORY_PURGE_MAX_DAYS = 365


class TenantConfigRecord(BaseModel):
    """One row of ``tenant_config`` as exposed by the admin API."""

    model_config = ConfigDict(frozen=True)

    tenant_id: UUID
    display_name: str
    plan: TenantPlan = TenantPlan.FREE
    # Stream U — PR E. Tenant lifecycle status ('active'|'suspended').
    status: TenantStatus = "active"
    # Stream Y-1 — DEPRECATED/DORMANT: tenant LLM BYOK was removed (LLM keys
    # are platform-exclusive). Field retained for non-destructive schema
    # compatibility but no longer resolved.
    model_credentials_ref: dict[str, str] = Field(default_factory=dict)
    mcp_allowlist: list[str] = Field(default_factory=list)
    rate_limit_override: dict[str, Any] = Field(default_factory=dict)
    pii_fields: list[str] = Field(default_factory=list)
    # E.8 HTTP-tool egress policy (denylist model — mirrors the sandbox
    # ``NetworkSpec``). ``http_tool_allowlist``: glob URL patterns
    # (``"https://api.github.com/*"``). Empty ↔ allow all *public* hosts
    # (SSRF / private-net targets are still blocked by the tool's static
    # guard); non-empty ↔ strict allow-only-these. ``http_tool_denylist``:
    # hosts (exact or subdomain, e.g. ``"internal.example.com"``) refused
    # even under allow-all — takes precedence over the allowlist.
    http_tool_allowlist: list[str] = Field(default_factory=list)
    http_tool_denylist: list[str] = Field(default_factory=list)
    # E.9: MCP server configs.
    # Shape: ``[{"name": str, "command": [str, ...], "env": {str: str}}]``.
    # NOT used to launch servers in M0 — STREAM-E-DESIGN Mini-ADR E-17:
    # ``command`` is operator-controlled (subprocess RCE risk), so the
    # platform's MCP servers come from ``mcp_servers_config_file``. The
    # M1 role of this per-tenant field is enablement / filtering over
    # the platform pool, not command specification.
    mcp_servers: list[dict[str, Any]] = Field(default_factory=list)
    # D.3: per-tenant retention. Bounded ranges mirror the DB CHECK
    # constraints in migration 0010 so admin clients fail fast rather
    # than tripping a 23514 at write time.
    audit_retention_days: int = Field(default=90, ge=_RETENTION_MIN_DAYS, le=_RETENTION_MAX_DAYS)
    event_log_retention_days: int = Field(
        default=30, ge=_RETENTION_MIN_DAYS, le=_RETENTION_MAX_DAYS
    )
    # B-63 —— 附件 / 产物保留期(天,按最后一次使用算起)。``None`` = 平台默认(90)。
    upload_retention_days: int | None = Field(
        default=None, ge=FILE_RETENTION_MIN_DAYS, le=FILE_RETENTION_MAX_DAYS
    )
    artifact_retention_days: int | None = Field(
        default=None, ge=FILE_RETENTION_MIN_DAYS, le=FILE_RETENTION_MAX_DAYS
    )
    # B-282 —— 对话工作目录(``agents/<key>/threads/<会话>/``)闲置多少天后清掉(天,从会话最后
    # 一次 run 算起)。``None`` = 平台默认(90)。范围与上面两个相同。
    conversation_workspace_retention_days: int | None = Field(
        default=None, ge=FILE_RETENTION_MIN_DAYS, le=FILE_RETENTION_MAX_DAYS
    )
    # Capability Uplift Sprint #1 — Mini-ADR U-2.
    trigger_fire_scan_mode: TriggerFireScanMode = "warn"
    # Capability Uplift Sprint #6 — Mini-ADR U-5.
    memory_recall_mode: MemoryRecallMode = "hybrid"
    # Capability Uplift Sprint #4 — Mini-ADR U-28. Curator state-
    # machine thresholds. Cross-field invariant ``archive_days > stale_days``
    # is enforced by the model validator below + a DB CHECK in migration
    # 0044.
    skill_stale_days: int = Field(default=30, ge=_SKILL_STALE_MIN_DAYS, le=_SKILL_STALE_MAX_DAYS)
    skill_archive_days: int = Field(
        default=90, ge=_SKILL_ARCHIVE_MIN_DAYS, le=_SKILL_ARCHIVE_MAX_DAYS
    )
    # Capability Uplift Sprint #7 — Mini-ADR U-38. MemoryConsolidator
    # thresholds. ``min_cluster_size`` is the number of similar transient
    # items required before LLM verification fires; ``similarity`` is the
    # cosine-similarity floor (1 - cosine_distance). ``purge_enabled`` lets
    # high-compliance tenants opt out of lone-item noise purge entirely
    # (the consolidate path still runs). ``purge_min_age_days`` is the
    # grace window during which a transient item cannot be purged even if
    # it is never retrieved (per Mini-ADR U-37 protection #1).
    memory_consolidation_min_cluster_size: int = Field(
        default=3, ge=_MEMORY_CLUSTER_MIN_SIZE, le=_MEMORY_CLUSTER_MAX_SIZE
    )
    memory_consolidation_similarity: float = Field(
        default=0.85, ge=_MEMORY_SIMILARITY_MIN, le=_MEMORY_SIMILARITY_MAX
    )
    memory_purge_enabled: bool = True
    memory_purge_min_age_days: int = Field(
        default=30, ge=_MEMORY_PURGE_MIN_DAYS, le=_MEMORY_PURGE_MAX_DAYS
    )
    # Stream P5b-2b ⑦ — predictive review opt-in. When True, MemoryConsolidator's
    # SUB-PASS 3 proactively re-reviews facts whose predicted validity window
    # (``expected_valid_days``) has come due, via one aux LLM call per due fact.
    # Off by default (LLM cost) — tenants opt in explicitly.
    memory_predictive_review_enabled: bool = False
    # Stream Y-1 — LLM platform-exclusive. ``credentials_mode`` is now
    # always ``platform`` (the only permitted value); all LLM provider +
    # tool API key lookups resolve via the platform's secret_refs.
    credentials_mode: CredentialsMode = "platform"
    # Stream Y-1 — DEPRECATED/DORMANT: tenant LLM BYOK was removed. Field
    # retained for non-destructive schema compatibility but no longer
    # resolved.
    tool_credentials: dict[Tool, str] = Field(default_factory=dict)
    # Stream Y-1 — DORMANT: previously the per-tenant MCP bearer secret_ref
    # used in the removed ``tenant`` credentials mode. Stream V/W tenant MCP
    # servers store their own ``token_secret_ref`` on the server row, so this
    # field is no longer read. Retained for non-destructive compatibility.
    mcp_credentials: dict[str, str] = Field(default_factory=dict)
    # Stream R — Mini-ADR R-9. The agent a tenant's members get by default
    # when a thread is created without an explicit agent. ``None`` → platform
    # fallback (``canonical-agent``).
    default_agent_name: str | None = None
    # Stream W — Mini-ADR W-4. When False, the tenant may only instantiate MCP
    # servers from the platform catalog; off-catalog custom registration is
    # rejected. Defaults True to preserve Stream V self-service behavior — a
    # platform admin sets it False per tenant for catalog-only governance.
    allow_custom_mcp_servers: bool = True
    # SE-16 (SE-A41) — per-tenant skill-evolution rollout gate. ANDed with
    # the platform master switch (``enable_skill_evolution_worker``): the
    # evolution worker only distils this tenant's candidates when both are
    # on. Default off — production rolls out tenant by tenant.
    skill_evolution_enabled: bool = False
    # SE-16 (SE-A45) — what fraction of ``implicit_success`` candidates the
    # evolution worker screens through the cheap aux quality judge before
    # distilling; un-sampled implicit candidates are dropped (marked evolved,
    # never distilled). Bounds the flywheel's aux + distillation spend on the
    # abundant implicit pool. Explicit 👍 / failed candidates are unaffected.
    skill_evolution_judge_sample_pct: int = Field(default=5, ge=0, le=100)
    created_at: datetime
    updated_at: datetime
    updated_by: str

    @model_validator(mode="after")
    def _skill_archive_strictly_greater_than_stale(self) -> TenantConfigRecord:
        if self.skill_archive_days <= self.skill_stale_days:
            msg = (
                f"skill_archive_days ({self.skill_archive_days}) must be strictly "
                f"greater than skill_stale_days ({self.skill_stale_days})"
            )
            raise ValueError(msg)
        return self


class TenantConfigPatch(BaseModel):
    """Admin ``PUT /v1/tenants/{tid}/config`` payload.

    All fields are optional so admins can update one knob at a time.
    Empty containers are *meaningful* values (e.g. clear the
    allowlist); to leave a field untouched, omit the key entirely.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    display_name: str | None = None
    plan: TenantPlan | None = None
    model_credentials_ref: dict[str, str] | None = None
    mcp_allowlist: list[str] | None = None
    rate_limit_override: dict[str, Any] | None = None
    pii_fields: list[str] | None = None
    http_tool_allowlist: list[str] | None = None
    http_tool_denylist: list[str] | None = None
    mcp_servers: list[dict[str, Any]] | None = None
    audit_retention_days: int | None = Field(
        default=None, ge=_RETENTION_MIN_DAYS, le=_RETENTION_MAX_DAYS
    )
    event_log_retention_days: int | None = Field(
        default=None, ge=_RETENTION_MIN_DAYS, le=_RETENTION_MAX_DAYS
    )
    # B-63 —— 附件 / 产物保留期。与其它字段不同,这两个字段**显式传 ``null``** 表示改回
    # 平台默认(清掉租户自己的值);不传这个键才是「不改」。见 :meth:`retention_updates`。
    upload_retention_days: int | None = Field(
        default=None, ge=FILE_RETENTION_MIN_DAYS, le=FILE_RETENTION_MAX_DAYS
    )
    artifact_retention_days: int | None = Field(
        default=None, ge=FILE_RETENTION_MIN_DAYS, le=FILE_RETENTION_MAX_DAYS
    )
    # B-282 —— 对话工作目录闲置保留期;与上面两个同样「显式 ``null`` = 改回平台默认」。
    conversation_workspace_retention_days: int | None = Field(
        default=None, ge=FILE_RETENTION_MIN_DAYS, le=FILE_RETENTION_MAX_DAYS
    )
    # Capability Uplift Sprint #1 — Mini-ADR U-2.
    trigger_fire_scan_mode: TriggerFireScanMode | None = None
    # Capability Uplift Sprint #6 — Mini-ADR U-5.
    memory_recall_mode: MemoryRecallMode | None = None
    # Capability Uplift Sprint #4 — Mini-ADR U-28. Curator threshold
    # patch. The cross-field invariant is enforced by the service
    # layer when applying the patch (it has the merged record), not
    # here — a patch that only carries one of the two days fields is
    # legal in isolation.
    skill_stale_days: int | None = Field(
        default=None, ge=_SKILL_STALE_MIN_DAYS, le=_SKILL_STALE_MAX_DAYS
    )
    skill_archive_days: int | None = Field(
        default=None, ge=_SKILL_ARCHIVE_MIN_DAYS, le=_SKILL_ARCHIVE_MAX_DAYS
    )
    # Capability Uplift Sprint #7 — Mini-ADR U-38. MemoryConsolidator
    # threshold patch. All optional; patches carrying any subset are valid.
    memory_consolidation_min_cluster_size: int | None = Field(
        default=None, ge=_MEMORY_CLUSTER_MIN_SIZE, le=_MEMORY_CLUSTER_MAX_SIZE
    )
    memory_consolidation_similarity: float | None = Field(
        default=None, ge=_MEMORY_SIMILARITY_MIN, le=_MEMORY_SIMILARITY_MAX
    )
    memory_purge_enabled: bool | None = None
    memory_purge_min_age_days: int | None = Field(
        default=None, ge=_MEMORY_PURGE_MIN_DAYS, le=_MEMORY_PURGE_MAX_DAYS
    )
    # Stream P5b-2b ⑦ — predictive review opt-in. None = leave unchanged.
    memory_predictive_review_enabled: bool | None = None
    # Stream Y-1 — LLM platform-exclusive. ``credentials_mode`` can only be
    # patched to ``platform`` now; Pydantic rejects other values with 422.
    credentials_mode: CredentialsMode | None = None
    # Stream Y-1 — DEPRECATED/DORMANT: tenant LLM BYOK removed; retained for
    # non-destructive compatibility but no longer resolved.
    tool_credentials: dict[Tool, str] | None = None
    # Stream Y-1 — DORMANT: see TenantConfigRecord.mcp_credentials.
    mcp_credentials: dict[str, str] | None = None
    # Stream R — Mini-ADR R-9. Tenant default agent (None in a patch means
    # "leave unchanged"; clearing it back to platform fallback is out of scope
    # this iteration — an admin re-points it instead).
    default_agent_name: str | None = None
    # Stream W — Mini-ADR W-4. None = leave unchanged.
    allow_custom_mcp_servers: bool | None = None
    # SE-16 (SE-A41) — per-tenant skill-evolution rollout gate. None = leave
    # unchanged.
    skill_evolution_enabled: bool | None = None
    # SE-16 (SE-A45) — implicit-candidate judge sample rate. None = leave
    # unchanged.
    skill_evolution_judge_sample_pct: int | None = Field(default=None, ge=0, le=100)

    def retention_decreases(self, current: TenantConfigRecord | None) -> list[dict[str, object]]:
        """B-63 —— 这个 patch 相对 ``current`` 把哪些保留期改小了:``[{field, from, to}]``(按生效值
        比,没设 = :data:`FILE_RETENTION_DEFAULT_DAYS`)。调大、不变、改回默认但默认更大,都不算。
        ``current`` 为 ``None`` = 还没有配置行(两项都是默认)。"""
        out: list[dict[str, object]] = []
        for name, new in self.retention_updates().items():
            old = getattr(current, name) if current is not None else None
            old_days = old if old is not None else FILE_RETENTION_DEFAULT_DAYS
            new_days = new if new is not None else FILE_RETENTION_DEFAULT_DAYS
            if new_days < old_days:
                out.append({"field": name, "from": old_days, "to": new_days})
        return out

    def retention_updates(self) -> dict[str, int | None]:
        """B-63 —— 这个 patch 对保留期字段(:data:`FILE_RETENTION_FIELDS`)的改动:键在
        ``model_fields_set`` 里才算(显式 ``null`` → ``None`` = 改回平台默认);没传的键不出现。"""
        return {
            name: getattr(self, name)
            for name in FILE_RETENTION_FIELDS
            if name in self.model_fields_set
        }
