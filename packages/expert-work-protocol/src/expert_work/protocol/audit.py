"""audit_log row shape — see ADR-0002 §audit_log and subsystems/17-audit-log."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from ipaddress import IPv4Address, IPv6Address
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class AuditResult(StrEnum):
    """Outcome of an audited action."""

    SUCCESS = "success"
    DENIED = "denied"
    ERROR = "error"


class AuditAction(StrEnum):
    """Canonical action word — strict whitelist (subsystems/17-audit-log §5.1).

    Format: ``<resource>:<verb>``. New actions land via PR + word-list update
    + CI lint (``audit.write(action=...)`` must be in this enum).
    """

    # auth
    AUTH_LOGIN = "auth:login"
    AUTH_LOGOUT = "auth:logout"
    AUTH_LOGIN_FAILED = "auth:login_failed"
    AUTH_TOKEN_REFRESH = "auth:token_refresh"
    # manifest
    MANIFEST_READ = "manifest:read"
    MANIFEST_WRITE = "manifest:write"
    MANIFEST_DELETE = "manifest:delete"
    MANIFEST_SIGN = "manifest:sign"
    MANIFEST_PUBLISH = "manifest:publish"
    # session
    SESSION_READ = "session:read"
    SESSION_WRITE = "session:write"
    SESSION_CANCEL = "session:cancel"
    # run lifecycle — emitted by the orchestrator worker at run end
    RUN_COMPLETED = "run:completed"
    RUN_FAILED = "run:failed"
    # H.3 PR 1 — emitted by ``GET /v1/runs`` cross-thread index (Stream H Mini-ADR H-6)
    RUN_LIST_READ = "run:list_read"
    # Stream 9.4 (HA failover) — emitted by the orphan sweep when it reclaims +
    # resumes a crashed-owner run, or marks it errored past the reclaim cap.
    RUN_FAILOVER = "run:failover"
    # secret
    SECRET_READ = "secret:read"
    SECRET_WRITE = "secret:write"
    SECRET_ROTATE = "secret:rotate"
    SECRET_DELETE = "secret:delete"
    # quota
    QUOTA_RATE_LIMIT_DENIED = "quota:rate_limit_denied"
    QUOTA_BUDGET_EXCEEDED = "quota:budget_exceeded"
    QUOTA_CONFIG_READ = "quota:config_read"
    QUOTA_CONFIG_WRITE = "quota:config_write"
    QUOTA_CONFIG_DELETE = "quota:config_delete"
    QUOTA_RESERVATION_EXPIRED = "quota:reservation_expired"
    # W4 (PR-2) — runtime commit/release ops on the token-reservation ledger
    # (``POST /v1/quota/commit`` / ``POST /v1/quota/release/{id}``).
    QUOTA_COMMIT = "quota:commit"
    QUOTA_RELEASE = "quota:release"
    # tenant_config (C.7)
    TENANT_CONFIG_READ = "tenant_config:read"
    TENANT_CONFIG_WRITE = "tenant_config:write"
    # tenant lifecycle — Stream P Mini-ADR P-1 (POST /v1/tenants creates the
    # first tenant_config row; system_admin-gated).
    TENANT_CREATE = "tenant:create"
    # Stream U — PR E. Tenant lifecycle deactivate/activate (system_admin-gated).
    TENANT_DEACTIVATE = "tenant:deactivate"
    TENANT_ACTIVATE = "tenant:activate"
    # tenant member onboarding — Stream R (Mini-ADR R-3/R-6). MEMBER_INVITE
    # on invite; RESEND on the idempotent compensation path; REVOKE/SUSPEND
    # on DELETE (invited→revoked / active→suspended); ACTIVATE on the W3
    # first-run hook (invited→active). KEYCLOAK_USER_CREATE(_FAILED) record
    # the cross-system account-provisioning side of the DB-first compensation.
    MEMBER_INVITE = "member:invite"
    MEMBER_RESEND = "member:resend"
    MEMBER_REVOKE = "member:revoke"
    MEMBER_SUSPEND = "member:suspend"
    # deletion-hygiene PR5 — the members-page one-shot deactivate + purge
    # (lifecycle + role-binding cleanup + Keycloak account delete + data
    # cascade); details carry the per-step booleans/counts.
    MEMBER_PURGE = "member:purge"
    MEMBER_ACTIVATE = "member:activate"
    MEMBER_PASSWORD_RESET = "member:password_reset"
    KEYCLOAK_USER_CREATE = "keycloak_user:create"
    KEYCLOAK_USER_CREATE_FAILED = "keycloak_user:create_failed"
    # user-dimension governance (Phase 2) — a tenant admin READS another
    # user's per-user content (memory / workspace) via ``?user_id=``. Only
    # emitted when caller≠target, so there is a "who looked at whom" trail
    # (reads are otherwise unaudited). resource_type = "user".
    USER_DATA_VIEW = "user:data_view"
    # user-dimension governance (Phase 3a) — a tenant admin irreversibly
    # purges a user's data + assets (cascade delete of high-PII rows,
    # anonymize of billing/tenant-asset rows, workspace soft-delete, and
    # soft-deactivation of the tenant_user row). Best-effort per step: the
    # summary details carry per-store counts + any step that failed.
    # resource_type = "user".
    USER_PURGE = "user:purge"
    # tenant credentials — Stream O Mini-ADR O-8.
    # PROVIDER_CREDENTIALS_UPDATED / TOOL_CREDENTIALS_UPDATED are emitted
    # whenever the corresponding dict is mutated via PUT; RESOLVE_FAILED is
    # emitted by the CredentialsResolver when a platform provider / tool
    # lookup finds no configured credential (a 401 fail-fast signal — an
    # operator misconfiguration the runbook walks through).
    # Stream Y-1: CREDENTIALS_MODE_CHANGED is retained for wire-contract
    # stability but is no longer emitted — LLM credentials are platform-
    # exclusive, so there is no tenant-mode switch to record.
    CREDENTIALS_MODE_CHANGED = "credentials:mode_changed"
    PROVIDER_CREDENTIALS_UPDATED = "credentials:provider_updated"
    TOOL_CREDENTIALS_UPDATED = "credentials:tool_updated"
    CREDENTIALS_RESOLVE_FAILED = "credentials:resolve_failed"
    # sandbox
    SANDBOX_ACQUIRED = "sandbox:acquired"
    SANDBOX_FORCE_DESTROY = "sandbox:force_destroy"
    SANDBOX_QUOTA_DENIED = "sandbox:quota_denied"
    # workspace (Stream J.15-补强-1 — Mini-ADR J-29 第 1 项 + J-36)
    WORKSPACE_QUOTA_DENIED = "workspace:quota_denied"
    WORKSPACE_SOFT_DELETE = "workspace:soft_delete"
    WORKSPACE_ARCHIVE = "workspace:archive"
    # workspace (Stream J.15-补强-2 — Mini-ADR J-29 第 2 项 backup pipeline)
    WORKSPACE_BACKUP = "workspace:backup"
    # workspace single-file delete via the inspector / user-detail tab
    # (Phase 1 shipped the delete with no audit; Phase 2 backfills it — the
    # governance surface lets an admin delete another user's file).
    WORKSPACE_FILE_DELETE = "workspace:file_delete"
    # workspace state projection (Stream CM-0 — Mini-ADR CM-A6); DB→file
    # projection of agent state (PLAN.md / TODO.md / MEMORY.md). resource_type
    # reuses ``user_workspace``.
    STATE_PROJECTED = "state:projected"
    # workspace state ingest (Stream CM-0 PR2b — Mini-ADR CM-A6); file→DB:
    # a human-edited PLAN.md applied back to AgentState.plan at run start.
    STATE_INGESTED = "state:ingested"
    # plan edited via the admin UI channel (Stream CM-8 — Mini-ADR CM-I2/I6);
    # PUT /v1/sessions/{thread_id}/plan wrote AgentState.plan through
    # ``aupdate_state``. resource_type reuses ``session``.
    PLAN_EDITED = "plan:edited"
    # image upload (Stream J.6.补强-2 — Mini-ADR J-31)
    IMAGE_UPLOAD = "image:upload"
    # document upload → workspace (read_document base capability). resource_type
    # reuses ``user_workspace`` (the doc lands in the durable workspace volume).
    DOCUMENT_UPLOAD = "document:upload"
    # skill (Stream J.7a — Mini-ADR J-23)
    SKILL_CREATE = "skill:create"
    SKILL_VERSION_CREATE = "skill_version:create"
    SKILL_STATUS_CHANGE = "skill:status_change"
    # Fires when a ``.skill`` package fails the import parse/safety gate
    # (structural layout, charset, size, extension, frontmatter — see the
    # ``_ZipRejectReason`` taxonomy). Records the safe path-free ``reason`` so
    # operators can diagnose a rejected import instead of guessing at a generic
    # 400. Distinct from PROMPT_INJECTION_BLOCKED (content threat scan).
    SKILL_PACKAGE_REJECTED = "skill:package_rejected"
    # skill — Capability Uplift Sprint #3 supporting files (Mini-ADR U-17)
    SKILL_SUPPORTING_FILE_UPLOADED = "skill_supporting_file:uploaded"
    SKILL_SUPPORTING_FILE_REMOVED = "skill_supporting_file:removed"
    # skill — Capability Uplift Sprint #3 threat scan + drift (Mini-ADR U-21).
    # PROMPT_INJECTION_BLOCKED fires on write-time strict-scope match;
    # DRIFT_DETECTED fires when skill_view recomputes content_hash and
    # finds a mismatch (DB row tampered past the strict scan — almost
    # certainly SQL injection or internal actor).
    SKILL_PROMPT_INJECTION_BLOCKED = "skill:prompt_injection_blocked"
    SKILL_DRIFT_DETECTED = "skill:drift_detected"
    # skill — Capability Uplift Sprint #3 high-risk publish gate (Mini-ADR
    # U-24). High-risk = tool_names ∩ {exec_python, http, exec_shell} ≠ ∅
    # or any supporting_files path starts with "scripts/". The gate
    # blocks DRAFT → ACTIVE for non-admin actors. M0 transparent (all
    # writes are admin); M1-K J.7b-1 self-authored skills get gated.
    SKILL_HIGH_RISK_ACTIVATION_BLOCKED = "skill:high_risk_activation_blocked"
    SKILL_HIGH_RISK_ACTIVATED = "skill:high_risk_activated"
    # skill — Capability Uplift Sprint #4 Curator (Mini-ADRs U-26 / U-29 / U-30).
    # Sweep summary (per-tenant per-day); per-skill auto-revival from
    # ``stale`` on activity; skill_view blocked because the skill was
    # auto-archived (cold path); admin pin / unpin events.
    SKILL_CURATOR_RUN = "skill:curator_run"
    SKILL_AUTO_REVIVED = "skill:auto_revived"
    SKILL_VIEW_BLOCKED_ARCHIVED = "skill:view_blocked_archived"
    SKILL_PINNED = "skill:pinned"
    SKILL_UNPINNED = "skill:unpinned"
    SKILL_CATEGORY_CHANGED = "skill:category_changed"
    # skill — Stream SE (SE-3b) in-session agent self-authoring (Layer A).
    # Emitted by the author_skill / refine_skill / fork_skill builtins when
    # an agent creates / refines / forks a skill in a run. All produce
    # DRAFT + agent_private rows; activation still goes through U-24 + the
    # SE-7 governance gate (these are never auto-active).
    SKILL_AUTHORED_BY_AGENT = "skill:authored_by_agent"
    SKILL_REFINED_BY_AGENT = "skill:refined_by_agent"
    SKILL_FORKED_BY_AGENT = "skill:forked_by_agent"
    # skill — Stream SE (SE-7c) Layer B governance: a replay-verified DRAFT was
    # auto-promoted to ACTIVE by the evolution worker (non-high-risk, eligible,
    # within rate limit + breaker closed). High-risk / ineligible stay DRAFT.
    SKILL_EVOLUTION_AUTO_PROMOTED = "skill:evolution_auto_promoted"
    # skill — SE-16 (SE-A47) dedup-at-ingest: a fresh distillate was
    # semantically similar to an existing same-agent distilled skill, so it
    # became that skill's next revision (version+1) instead of a new entry.
    # Details carry the similarity score + the ACTIVE→DRAFT re-review flip.
    SKILL_EVOLUTION_DEDUP_REVISION = "skill:evolution_dedup_revision"
    # skill — Stream SE (SE-7d) regression rollback: an auto-promoted ACTIVE
    # version regressed in production (windowed success rate significantly below
    # its promote-time baseline, or below the absolute floor) and was auto-
    # archived. The audit details carry the rollback evidence (observed rate /
    # baseline / drop / p-value / n) — it is not a replay, so it does not write
    # a ``skill_eval_result`` row.
    SKILL_EVOLUTION_ROLLED_BACK = "skill:evolution_rolled_back"
    # skill — Stream SE (SE-8, Mini-ADR SE-A13b) promote-approval flow. An
    # agent_private skill version is proposed for tenant-wide visibility
    # (REQUESTED), then a tenant admin / system_admin APPROVES (visibility
    # agent_private→tenant) or REJECTS it. Orthogonal to status (draft→active).
    SKILL_PROMOTE_REQUESTED = "skill:promote_requested"
    SKILL_PROMOTE_APPROVED = "skill:promote_approved"
    SKILL_PROMOTE_REJECTED = "skill:promote_rejected"
    # skill — Stream SE (SE-8, Mini-ADR SE-A13c) persistent emergency stop.
    # A human ENGAGES the kill-switch to degrade the whole auto-promote channel
    # to human review (tenant or global scope), then RELEASES it. Complements
    # the in-process SE-7b CircuitBreaker (automatic) with a durable manual override.
    SKILL_EVOLUTION_KILL_SWITCH_ENGAGED = "skill:evolution_kill_switch_engaged"
    SKILL_EVOLUTION_KILL_SWITCH_RELEASED = "skill:evolution_kill_switch_released"
    # skill — Skill Marketplace Phase 1. A tenant subscribes to / cancels a
    # platform skill (semantic A: accounting/UX marker, never gates the runtime
    # resolver). Cancel is a soft-stop (enabled=false), so it's still an action.
    SKILL_SUBSCRIBED = "skill:subscribed"
    SKILL_UNSUBSCRIBED = "skill:unsubscribed"
    # artifact (Stream J.9-step3 — Mini-ADR J-25). ``ARTIFACT_SAVE`` is
    # reserved for the orchestrator-side save-artifact tool emit; that
    # wiring lands when ToolEnv gains an :class:`AuditLogger` handle.
    ARTIFACT_DELETE = "artifact:delete"
    ARTIFACT_UPDATE = "artifact:update"
    # approval / HITL (Stream J.8 — Mini-ADR J-24)
    APPROVAL_REQUESTED = "approval:requested"
    APPROVAL_DECIDED = "approval:decided"
    # RT-6 Tier A (RT-ADR-19) — the approved args digest failed re-verification
    # before dispatch: the checkpointed tool_call drifted from what was approved.
    APPROVAL_BINDING_DRIFT = "approval:binding_drift"
    # triggers (Stream J.10 — Mini-ADR J-26 / J-42)
    TRIGGER_CREATE = "trigger:create"
    TRIGGER_UPDATE = "trigger:update"
    TRIGGER_DELETE = "trigger:delete"
    TRIGGER_FIRE = "trigger:fire"
    # triggers — Spec 1 PR3 (conversational scheduled tasks): reconcile-time
    # lifecycle outcomes emitted when a fired run reaches a terminal status.
    TRIGGER_COMPLETED = "trigger:completed"
    TRIGGER_FAILED = "trigger:failed"
    # triggers — Capability Uplift Sprint #1 (Mini-ADR U-2)
    TRIGGER_PROMPT_INJECTION_BLOCKED = "trigger:prompt_injection_blocked"
    TRIGGER_PROMPT_INJECTION_WARN = "trigger:prompt_injection_warn"
    # outbound webhook hook (HX-9 — STREAM-HX § 13)
    WEBHOOK_ENDPOINT_CREATE = "webhook_endpoint:create"
    WEBHOOK_ENDPOINT_UPDATE = "webhook_endpoint:update"
    WEBHOOK_ENDPOINT_DELETE = "webhook_endpoint:delete"
    # curation / eval-dataset (Stream J.12 — Mini-ADR J-43)
    EVAL_DATASET_CREATE = "eval_dataset:create"
    EVAL_DATASET_UPDATE = "eval_dataset:update"
    EVAL_DATASET_DELETE = "eval_dataset:delete"
    CURATION_PROMOTE = "curation_candidate:promote"
    CURATION_DISMISS = "curation_candidate:dismiss"
    # tools (Stream E.6 + E.8 + onwards)
    TOOL_CALL = "tool:call"
    TOOL_BLOCKED = "tool:blocked"
    # output guards — audit-eval Phase 4. PI-2 output screen blocks a terminal
    # response (credential/exfil shape) → refusal; 7.4 DLP redacts PII in a
    # terminal response. Previously metric-only; now also a durable audit row.
    OUTPUT_SCREEN_BLOCKED = "output:screen_blocked"
    OUTPUT_DLP_REDACTED = "output:dlp_redacted"
    # llm (Stream E.11 + E.13)
    LLM_CIRCUIT_OPENED = "llm:circuit_opened"
    LLM_FALLBACK_TRIGGERED = "llm:fallback_triggered"
    LLM_CACHE_HIT = "llm:cache_hit"
    # api_key
    API_KEY_CREATE = "api_key:create"
    API_KEY_REVOKE = "api_key:revoke"
    API_KEY_ROTATE = "api_key:rotate"  # Stream K.K1
    # memory (Stream K.K6)
    MEMORY_UPDATE = "memory:update"
    MEMORY_FORGET = "memory:forget"
    # memory — Stream Memory-Enhance (M-4): a user's authoritative self-correction
    # (rewrite → confidence 1.0, or forget-as-wrong). Distinct from the
    # admin-facing MEMORY_UPDATE / MEMORY_FORGET so corrections are auditable
    # as end-user actions.
    MEMORY_CORRECT = "memory:correct"
    # memory — Capability Uplift Sprint #2 (Mini-ADR U-3 / U-4)
    MEMORY_INJECTION_BLOCKED = "memory:injection_blocked"
    MEMORY_INJECTION_REDACTED = "memory:injection_redacted"
    # audit-eval Phase 3 — a strict-scope scan hit on USER-authored memory no
    # longer hard-blocks the write (over-blocked legit devops/security notes);
    # the write proceeds and is flagged here for traceability (audit over
    # blocking). The runtime injection vectors (recall, auto-extracted write-
    # back) still block — see docs/design/sandbox-audit-evaluation.md.
    MEMORY_INJECTION_WARN = "memory:injection_warn"
    MEMORY_DRIFT_DETECTED = "memory:drift_detected"
    # memory — Capability Uplift Sprint #7 MemoryConsolidator (Mini-ADRs
    # U-34 / U-36 / U-37 / U-40 / U-42). Mirrors the control-plane
    # ResourceType/Action Literal in services/control-plane/src/control_plane/
    # audit_log.py (per [memory:audit-literal-drift] — both must stay in sync).
    # MEMORY_CONSOLIDATED       — cluster verified + summary written
    # MEMORY_CONSOLIDATION_REJECTED — cluster LLM returned keep=false
    #                              with anti_mislearn:* reason
    # MEMORY_PURGED_AS_NOISE    — lone-item review classified noise + soft-deleted
    # MEMORY_REVIEWED_DURABLE   — lone-item review classified durable + marked
    # MEMORY_DEMOTED            — explicit "consolidated_into set" event
    #                             (M1-K Admin UI; Sprint #7 reserves)
    # MEMORY_ARCHIVED           — M2-C archive pipeline (Sprint #7 reserves)
    # MEMORY_CONSOLIDATOR_RUN   — per-sweep summary row (single audit per tick)
    # MEMORY_FACT_EXPIRED       — P5b-2b ⑦ predictive review: still_valid=false retraction
    # MEMORY_FACT_RENEWED       — P5b-2b ⑦ predictive review: still_valid=true window extended
    MEMORY_CONSOLIDATED = "memory:consolidated"
    MEMORY_CONSOLIDATION_REJECTED = "memory:consolidation_rejected"
    MEMORY_PURGED_AS_NOISE = "memory:purged_as_noise"
    MEMORY_REVIEWED_DURABLE = "memory:reviewed_durable"
    MEMORY_DEMOTED = "memory:demoted"
    MEMORY_ARCHIVED = "memory:archived"
    MEMORY_CONSOLIDATOR_RUN = "memory:consolidator_run"
    MEMORY_FACT_EXPIRED = "memory:fact_expired"
    MEMORY_FACT_RENEWED = "memory:fact_renewed"
    # service_account
    SERVICE_ACCOUNT_CREATE = "service_account:create"
    SERVICE_ACCOUNT_DELETE = "service_account:delete"
    SERVICE_ACCOUNT_READ = "service_account:read"
    # role_binding
    ROLE_BINDING_CREATE = "role_binding:create"
    ROLE_BINDING_DELETE = "role_binding:delete"
    ROLE_BINDING_READ = "role_binding:read"
    # feedback (Stream G.6)
    FEEDBACK_CREATE = "feedback:create"
    #: Stream HX-2 — FeedbackConsumerWorker processed one 👎 row into the
    #: learning loop (memory review flags; skill side is gate-pull).
    FEEDBACK_CONSUMED = "feedback:consumed"
    # audit (meta)
    AUDIT_READ = "audit:read"
    AUDIT_EXPORT = "audit:export"
    # system / cross-tenant (Stream N — Mini-ADR N-5)
    SYSTEM_CROSS_TENANT_QUERY = "system:cross_tenant_query"
    SYSTEM_TENANT_SWITCH = "system:tenant_switch"
    # cross-tenant access attempt rejected by the deployment-level
    # ``cross_tenant_query_enabled`` switch — Stream HX-8 (Mini-ADR HX-H4).
    SYSTEM_CROSS_TENANT_BLOCKED = "system:cross_tenant_blocked"

    # platform credentials (the runtime DB overlay) — Stream P Mini-ADR P-11.
    # system_admin-only writes to the platform provider/tool secret-ref tables.
    PLATFORM_PROVIDER_CREDENTIAL_UPSERT = "platform_credential:provider_upsert"
    PLATFORM_PROVIDER_CREDENTIAL_DELETE = "platform_credential:provider_delete"
    PLATFORM_TOOL_CREDENTIAL_UPSERT = "platform_credential:tool_upsert"
    PLATFORM_TOOL_CREDENTIAL_DELETE = "platform_credential:tool_delete"
    # per-tenant credential overrides — Stream HX-8 (system_admin-only writes
    # to the tenant_provider_secret / tenant_tool_secret sister tables).
    PLATFORM_PROVIDER_CREDENTIAL_TENANT_UPSERT = "platform_credential:tenant_provider_upsert"
    PLATFORM_PROVIDER_CREDENTIAL_TENANT_DELETE = "platform_credential:tenant_provider_delete"
    PLATFORM_TOOL_CREDENTIAL_TENANT_UPSERT = "platform_credential:tenant_tool_upsert"
    PLATFORM_TOOL_CREDENTIAL_TENANT_DELETE = "platform_credential:tenant_tool_delete"
    # platform embedding/rerank config (the runtime DB overlay) — Stream T PR C.
    # system_admin-only write to the platform embedding-config row.
    PLATFORM_EMBEDDING_CONFIG_UPDATED = "platform_embedding_config:updated"
    # platform judge-model config (Stream PI-3-A1) — system_admin-only write
    # to the platform judge-config row.
    PLATFORM_JUDGE_CONFIG_UPDATED = "platform_judge_config:updated"
    # platform quality-monitor config (Stream RT-5 PR-3b) — system_admin-only
    # write to the platform quality-config row (sampling / judge / drift / enable).
    PLATFORM_QUALITY_CONFIG_UPDATED = "platform_quality_config:updated"
    # platform billing config (Stream 12.4) — system_admin-only write to the
    # platform billing-rollup enable flag.
    PLATFORM_BILLING_CONFIG_UPDATED = "platform_billing_config:updated"
    # platform tool-output-budget config (Phase 3) — system_admin-only write to
    # the platform tool-budget on/off flag.
    PLATFORM_TOOL_BUDGET_UPDATED = "platform_tool_budget_config:updated"
    # platform dynamic-worker limits config (B3 PR2) — system_admin-only write
    # to the platform dynamic_worker max_concurrent/max_per_run/max_iterations row.
    PLATFORM_DYNAMIC_WORKER_UPDATED = "platform_dynamic_worker_config:updated"
    # platform delegation-gate config (perf phase2 PR3) — system_admin-only write
    # to the platform max_concurrent_delegations row.
    PLATFORM_DELEGATION_UPDATED = "platform_delegation_config:updated"
    # mcp_server (Stream V — tenant remote MCP server registry)
    MCP_SERVER_CREATE = "mcp_server:create"
    MCP_SERVER_UPDATE = "mcp_server:update"
    MCP_SERVER_DELETE = "mcp_server:delete"
    # mcp_catalog (Stream W — platform MCP connector catalog, system_admin)
    MCP_CATALOG_CREATE = "mcp_catalog:create"
    MCP_CATALOG_UPDATE = "mcp_catalog:update"
    MCP_CATALOG_DELETE = "mcp_catalog:delete"
    # mcp_catalog tenant enable/disable (Stream MCP platform-servers, P2) —
    # tenant admin opts a platform catalog server into / out of the tenant's
    # ``mcp_allowlist`` (the tenant-side "选择使用" action).
    MCP_CATALOG_ENABLE = "mcp_catalog:enable"
    MCP_CATALOG_DISABLE = "mcp_catalog:disable"
    # rate_card (Stream Y — platform model rate card, system_admin)
    RATE_CARD_CREATE = "rate_card:create"
    RATE_CARD_UPDATE = "rate_card:update"
    RATE_CARD_DELETE = "rate_card:delete"
    # agent_template (Stream Agent-Templates — platform Agent templates, system_admin)
    AGENT_TEMPLATE_CREATE = "agent_template:create"
    AGENT_TEMPLATE_UPDATE = "agent_template:update"
    AGENT_TEMPLATE_DELETE = "agent_template:delete"
    # agent kill switch — Stream RT-4 (RT-ADR-18). Tenant-admin engages/releases
    # the per-agent emergency stop (reject new runs + bulk-cancel in-flight +
    # queue refuses to claim). Reversible.
    AGENT_DISABLED = "agent:disabled"
    AGENT_ENABLED = "agent:enabled"


ResourceType = Literal[
    "manifest",
    "session",
    "sandbox",
    "secret",
    "audit",
    "quota",
    "tenant_config",
    "user",
    "role_binding",
    "api_key",
    "service_account",
    "feedback",
    "memory_item",  # Stream K.K6 — long-term memory CRUD
    "user_workspace",  # Stream J.15-补强-1 — volume quota + lifecycle
    "image_upload",  # Stream J.6.补强-2 — Mini-ADR J-31
    "skill",  # Stream J.7a — Mini-ADR J-23
    # Capability Uplift Sprint #3 (Mini-ADR U-17) — supporting-files
    # subresource. Mirrors the control-plane ResourceType Literal in
    # services/control-plane/src/control_plane/audit.py (per
    # [memory:audit-literal-drift] — both must stay in sync).
    "skill_supporting_file",
    # Stream SE (Mini-ADR SE-A2) — replay-verification evidence row.
    # Mirrors the control-plane ``ResourceType`` Literal per
    # [memory:audit-literal-drift] (both must stay in sync).
    "skill_eval_result",
    # Stream SE (SE-8, Mini-ADR SE-A13b/c) — promote-approval request +
    # persistent kill-switch. Mirrors the control-plane ``ResourceType``
    # Literal per [memory:audit-literal-drift] (both must stay in sync).
    "skill_promote_request",
    "skill_evolution_kill_switch",
    "artifact",  # Stream J.9-step3 — Mini-ADR J-25
    "approval",  # Stream J.8 — Mini-ADR J-24
    "trigger",  # Stream J.10 — Mini-ADR J-26 / J-42
    "webhook_endpoint",  # HX-9 — STREAM-HX § 13 (outbound webhook hook)
    "eval_dataset",  # Stream J.12 — Mini-ADR J-43
    "curation_candidate",  # Stream J.12 — Mini-ADR J-43
    "system",  # Stream N — Mini-ADR N-5 (cross-tenant query / tenant switch)
    "run",  # Stream H.3 PR 1 — Mini-ADR H-6 (RUN_LIST_READ)
    "tenant",  # Stream P — Mini-ADR P-1 (POST /v1/tenants)
    "platform_credential",  # Stream P — Mini-ADR P-11 (/v1/platform/credentials)
    "tenant_member",  # Stream R — Mini-ADR R-3 (member onboarding)
    "keycloak_user",  # Stream R — Mini-ADR R-3 (Keycloak account provisioning)
    "tenant_mcp_server",  # Stream V — tenant remote MCP server registry
    "mcp_connector_catalog",  # Stream W — platform MCP connector catalog
    "model_rate_card",  # Stream Y — platform model rate card (Y-3)
    # Stream Agent-Templates — platform Agent template catalog (system_admin).
    # Mirrors the control-plane ResourceType Literal in
    # services/control-plane/src/control_plane/audit.py per
    # [memory:audit-literal-drift] (both must stay in sync).
    "platform_agent_template",
    # Stream TE-2 — per-tool-call audit (TOOL_CALL / TOOL_BLOCKED). Mirrors
    # the control-plane ResourceType Literal in
    # services/control-plane/src/control_plane/audit.py per
    # [memory:audit-literal-drift] (both must stay in sync).
    "tool",
    # Stream RT-4 — agent kill switch (AGENT_DISABLED / AGENT_ENABLED). Mirrors
    # the control-plane ResourceType Literal in
    # services/control-plane/src/control_plane/audit.py per
    # [memory:audit-literal-drift] (both must stay in sync).
    "agent",
    # Deletion-hygiene PR2 — per-user MCP OAuth connection (was mis-tagged
    # "tenant_mcp_server"). Mirrors the control-plane ResourceType Literal in
    # services/control-plane/src/control_plane/audit.py per
    # [memory:audit-literal-drift] (both must stay in sync).
    "mcp_oauth_connection",
]
"""Canonical resource type strings used in audit log entries.

Kept in sync with the ``ResourceType`` Literal in
``services/control-plane/src/control_plane/audit.py`` per
[memory:audit-literal-drift] — both must change together.
"""


class AuditEntry(BaseModel):
    """One row of ``audit_log`` (pre-WORM, post-redactor).

    Reads / writes only via the AuditLogger Python API (see subsystems/17 §4);
    external POST is disallowed.
    """

    model_config = ConfigDict(frozen=True)

    id: int | None = Field(default=None, description="DB autoincrement; None pre-insert")
    tenant_id: UUID
    actor_type: Literal["user", "service_account", "system", "agent"]
    actor_id: str
    on_behalf_of: str | None = Field(default=None, description="Original user when sa-driven")
    action: AuditAction
    resource_type: ResourceType
    resource_id: str | None = None
    result: AuditResult
    reason: str | None = Field(default=None, description="Required when result != success")
    ip: IPv4Address | IPv6Address | None = None
    user_agent: str | None = None
    request_id: UUID | None = None
    trace_id: str | None = None
    details: dict[str, Any] = Field(
        default_factory=dict,
        description="Already PII/secret-redacted",
    )
    occurred_at: datetime | None = Field(default=None, description="DB default now() at insert")


class AuditQuery(BaseModel):
    """Read-side query model for ``GET /v1/audit`` (subsystems/17 §3.2)."""

    model_config = ConfigDict(frozen=True)

    tenant_id: UUID | Literal["*"] = Field(description="'*' requires admin role")
    actor_id: str | None = None
    action: AuditAction | None = None
    resource_type: str | None = None
    resource_id: str | None = None
    result: AuditResult | None = None
    from_ts: datetime | None = None
    to_ts: datetime | None = None
    limit: int = Field(default=100, le=1000)
    cursor: str | None = Field(default=None, description="Opaque base64 cursor")


class AuditPage(BaseModel):
    """One page of ``AuditEntry`` results plus an opaque next-cursor.

    ``next_cursor`` is ``None`` when the result set is exhausted; otherwise
    pass it back as :attr:`AuditQuery.cursor` to fetch the next page.
    """

    model_config = ConfigDict(frozen=True)

    entries: list[AuditEntry]
    next_cursor: str | None = None
