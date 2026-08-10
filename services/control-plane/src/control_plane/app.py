"""Control Plane FastAPI app factory — Stream B.1.

The factory is intentionally synchronous + side-effect-free so tests can
build many isolated apps without disturbing each other.

Wiring overview (see [STREAM-B-DESIGN § 2.2](../../../docs/streams/STREAM-B-DESIGN.md)):

* Synchronous fail-fast guard: ``settings.auth_mode == "prod"`` raises
  (ADR B-5) until C.1 OIDC middleware lands.
* ``lifespan`` initialises structured logging + OTel tracing and arms
  the :class:`Lifecycle` graceful-shutdown drain.
* Middleware stack (outermost → innermost):
    1. ``ObservabilityMiddleware``
    2. ``AuditContextMiddleware``
    3. ``InFlightMiddleware``
* Routes: ``/healthz/*`` + ``/metrics`` (B.1 surface). The
  ``/v1/agents`` / ``/v1/sessions`` / ``/v1/sessions/{id}/runs`` routers
  land in B.5 / B.6 / B.7.
"""

from __future__ import annotations

import http.cookiejar
import logging
import os
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, replace
from datetime import timedelta
from pathlib import Path
from uuid import UUID

import httpx
from fastapi import FastAPI
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from control_plane.agent_disable_status import AgentDisableService
from control_plane.api import (
    build_agent_schema_router,
    build_agent_templates_router,
    build_agent_users_router,
    build_agents_router,
    build_api_keys_router,
    build_approvals_router,
    build_artifacts_router,
    build_audit_router,
    build_billing_admin_router,
    build_conversations_router,
    build_curation_router,
    build_eval_dataset_router,
    build_eval_runs_router,
    build_feedback_router,
    build_health_router,
    build_knowledge_router,
    build_mcp_catalog_router,
    build_mcp_oauth_router,
    build_mcp_servers_router,
    build_me_router,
    build_members_router,
    build_memory_router,
    build_metrics_router,
    build_model_catalog_router,
    build_plan_router,
    build_platform_billing_config_router,
    build_platform_config_router,
    build_platform_delegation_config_router,
    build_platform_dynamic_worker_config_router,
    build_platform_embedding_config_router,
    build_platform_judge_config_router,
    build_platform_quality_config_router,
    build_platform_skills_router,
    build_platform_tool_budget_config_router,
    build_quality_router,
    build_quota_router,
    build_rate_card_router,
    build_role_bindings_router,
    build_runs_list_router,
    build_runs_router,
    build_sandbox_egress_audit_router,
    build_sandboxes_router,
    build_service_accounts_router,
    build_sessions_router,
    build_setup_router,
    build_skill_evolution_router,
    build_skills_router,
    build_tenant_config_router,
    build_tenant_quotas_router,
    build_tenant_users_router,
    build_tenants_router,
    build_triggers_router,
    build_uploads_router,
    build_usage_router,
    build_webhook_endpoints_router,
    build_webhooks_router,
    build_workspace_router,
)
from control_plane.api.model_catalog import PlatformConfiguredProviders
from control_plane.approval_metrics import ApprovalGaugeWorker
from control_plane.approval_timeout_sweep import ApprovalTimeoutSweep
from control_plane.audit import (
    TenantConfigPiiResolver,
    build_default_audit_logger,
)
from control_plane.audit import (
    emit as audit_emit,
)
from control_plane.auth import (
    ApiKeyVerifier,
    HTTPJWKSProvider,
    JWTVerifier,
    MTLSVerifier,
    build_mtls_verifier,
)
from control_plane.aux_model_adapter import make_llm_router_aux_model
from control_plane.catalog_seed import CatalogSeedError, load_catalog_seed, seed_catalog
from control_plane.credential_value_cache import CredentialValueCache
from control_plane.curation_worker import CurationWorker
from control_plane.encrypted_secret_store import (
    SqlEncryptedSecretStore,
    build_kek_from_b64,
)
from control_plane.eval_engine import M0_BASELINE_SUITE, RunBaselineEvalEngine
from control_plane.eval_engine_live import (
    ADVERSARIAL_SUITE,
    TRACE_EVAL_SUITE,
    AdversarialEvalEngine,
    DispatchEvalEngine,
    LiveEvalHarness,
    TraceEvalEngine,
)
from control_plane.eval_worker import EvalWorker
from control_plane.feedback_consumer import FeedbackConsumerWorker
from control_plane.keycloak import (
    FakeKeycloakAdminClient,
    HttpKeycloakAdminClient,
    KeycloakAdminClient,
    ServiceAccountTokenProvider,
)
from control_plane.knowledge.ingestion import KnowledgeIngestionRunner
from control_plane.knowledge.recovery import KnowledgeIngestRecoveryWorker
from control_plane.manifest import ManifestLoader
from control_plane.mcp_oauth import default_http_client
from control_plane.mcp_oauth_refresh import McpOAuthRefresher
from control_plane.mcp_oauth_refresh_lock import PgMcpOAuthRefreshLock
from control_plane.memory import MemoryDLQWorker
from control_plane.memory_consolidator import (
    ConsolidatorAuxModel,
    MemoryConsolidator,
    make_consolidator_embedder,
)
from control_plane.middleware import (
    AuditContextMiddleware,
    AuthMiddleware,
    BackpressureMiddleware,
    CancellationMiddleware,
    DeadlineMiddleware,
    InFlightMiddleware,
    ObservabilityMiddleware,
    RateLimitMiddleware,
    RLSContextMiddleware,
    TenantRateLimitMiddleware,
)
from control_plane.orphan_sweep import OrphanSweep
from control_plane.platform_delegation_config import (
    DelegationConfig,
    PlatformDelegationConfigService,
)
from control_plane.platform_dynamic_worker_config import (
    DynamicWorkerConfig,
    PlatformDynamicWorkerConfigService,
)
from control_plane.platform_embedding_config import PlatformEmbeddingConfigService
from control_plane.platform_judge_config import PlatformJudgeConfigService
from control_plane.platform_mcp_pool import PlatformMcpPoolService
from control_plane.platform_quality_config import PlatformQualityConfigService
from control_plane.platform_secrets import PlatformSecretsService
from control_plane.platform_tool_budget_config import PlatformToolBudgetConfigService
from control_plane.quality_drift_worker import QualityDriftWorker
from control_plane.quality_judge import QualityJudge
from control_plane.quality_monitor_worker import QualityMonitorWorker
from control_plane.quota import (
    InMemoryQuotaService,
    QuotaService,
    RedisQuotaService,
    ReservationReaper,
)
from control_plane.ratelimit import (
    InProcessTokenBucketLimiter,
    RateLimiter,
    RedisTokenBucketLimiter,
)
from control_plane.run_queue_worker import RunQueueWorker
from control_plane.runtime import (
    AgentRuntime,
    DynamicResolvingEmbedder,
    DynamicResolvingReranker,
    _build_mcp_client,
    build_mcp_pool,
    build_middleware_env,
    build_sandbox_runtime,
    build_tool_env,
    build_workspace_store,
    make_agent_builder,
    make_agent_runtime,
    make_image_resolver,
    make_knowledge_retriever,
    make_mcp_allowlist_provider,
    resolve_object_store_config,
    resolve_web_search_client,
)
from control_plane.sandbox_egress_metrics import SandboxEgressMetricsWorker
from control_plane.sandbox_reap_worker import SandboxReapWorker
from control_plane.scheduler import TriggerScheduler
from control_plane.settings import Settings
from control_plane.skill_activity import ThrottledActivityRecorder
from control_plane.skill_curator import SkillCurator
from control_plane.skill_evolution_limits import CircuitBreaker
from control_plane.skill_evolution_worker import SkillEvolutionWorker
from control_plane.skill_rollback_monitor import RollbackMonitor
from control_plane.skill_run_usage_recorder import StoreSkillRunUsageRecorder
from control_plane.subagent_runtime import make_child_agent_builder, make_worker_build_fn
from control_plane.tenancy import (
    ServiceBackedTenantConfigStore,
    TenantConfigNotConfiguredError,
    TenantConfigService,
)
from control_plane.tenant_mcp_pool import TenantMcpPoolService
from control_plane.tenant_scope import bypass_rls_session
from control_plane.tenant_secret_overlay import TenantOverlayCredentialsResolver
from control_plane.tenant_status import TenantStatusService
from control_plane.transcript_mirror_sweep import TranscriptMirrorSweep
from control_plane.user_mcp_oauth_pool import UserMcpOAuthPoolService
from control_plane.webhook_delivery_worker import WebhookDeliveryWorker
from control_plane.workspace_janitor import WorkspaceJanitorWorker
from control_plane.workspace_lock import PgWorkspaceLock
from control_plane.workspace_quota import WorkspaceQuotaService
from expert_work.common.credentials import CredentialsResolver
from expert_work.common.health import DefaultHealthProvider
from expert_work.common.lifecycle import Lifecycle
from expert_work.common.observability import current_trace_id_hex, init_logging, init_tracing
from expert_work.common.uplift_metrics import record_legacy_credentials_fallback
from expert_work.persistence.agent_disable import (
    AgentDisableStore,
    InMemoryAgentDisableStore,
    SqlAgentDisableStore,
)
from expert_work.persistence.agent_instance import (
    AgentInstanceStore,
    InMemoryAgentInstanceStore,
    SqlAgentInstanceStore,
)
from expert_work.persistence.agent_spec import (
    AgentSpecStore,
    InMemoryAgentSpecStore,
    SqlAgentSpecStore,
)
from expert_work.persistence.approval import (
    ApprovalStore,
    InMemoryApprovalStore,
    SqlApprovalStore,
)
from expert_work.persistence.artifact import (
    ArtifactStore,
    InMemoryArtifactStore,
    SqlArtifactStore,
)
from expert_work.persistence.audit_log import AuditLogStore, SqlAuditLogStore
from expert_work.persistence.auth import (
    ApiKeyStore,
    InMemoryApiKeyStore,
    InMemoryRoleBindingStore,
    InMemoryServiceAccountStore,
    RoleBindingStore,
    ServiceAccountStore,
    SqlApiKeyStore,
    SqlRoleBindingStore,
    SqlServiceAccountStore,
)
from expert_work.persistence.billing import (
    DbModelRateCardStore,
    DbTenantBillingLedgerStore,
    InMemoryModelRateCardStore,
    InMemoryTenantBillingLedgerStore,
    ModelRateCardStore,
    TenantBillingLedgerStore,
)
from expert_work.persistence.curation import (
    CurationCandidateStore,
    EvalDatasetStore,
    InMemoryCurationCandidateStore,
    InMemoryEvalDatasetStore,
    SqlCurationCandidateStore,
    SqlEvalDatasetStore,
)
from expert_work.persistence.database import (
    DatabaseConfig,
    create_async_engine_from_config,
    create_async_session_factory,
)
from expert_work.persistence.eval import (
    EvalRunStore,
    InMemoryEvalRunStore,
    SqlEvalRunStore,
)
from expert_work.persistence.feedback_store import (
    DbFeedbackStore,
    FeedbackStore,
    InMemoryFeedbackStore,
)
from expert_work.persistence.image_upload import (
    ImageUploadStore,
    InMemoryImageUploadStore,
    SqlImageUploadStore,
)
from expert_work.persistence.knowledge import (
    InMemoryKnowledgeStore,
    KnowledgeStore,
    SqlKnowledgeStore,
)
from expert_work.persistence.mcp_connector_catalog import (
    InMemoryMcpConnectorCatalogStore,
    McpConnectorCatalogStore,
    SqlMcpConnectorCatalogStore,
)
from expert_work.persistence.mcp_oauth_connection import (
    InMemoryMcpOAuthConnectionStore,
    McpOAuthConnectionStore,
    SqlMcpOAuthConnectionStore,
)
from expert_work.persistence.memory import (
    InMemoryMemoryStore,
    InMemoryMemoryWritebackDLQ,
    MemoryStore,
    MemoryWritebackDLQ,
    SqlMemoryStore,
    SqlMemoryWritebackDLQ,
)
from expert_work.persistence.platform_agent_template import (
    InMemoryPlatformAgentTemplateStore,
    PlatformAgentTemplateStore,
    SqlPlatformAgentTemplateStore,
)
from expert_work.persistence.platform_billing_config import (
    InMemoryPlatformBillingConfigStore,
    PlatformBillingConfigStore,
    SqlPlatformBillingConfigStore,
)
from expert_work.persistence.platform_delegation_config import (
    InMemoryPlatformDelegationConfigStore,
    PlatformDelegationConfigStore,
    SqlPlatformDelegationConfigStore,
)
from expert_work.persistence.platform_dynamic_worker_config import (
    InMemoryPlatformDynamicWorkerConfigStore,
    PlatformDynamicWorkerConfigStore,
    SqlPlatformDynamicWorkerConfigStore,
)
from expert_work.persistence.platform_embedding_config import (
    InMemoryPlatformEmbeddingConfigStore,
    PlatformEmbeddingConfigStore,
    SqlPlatformEmbeddingConfigStore,
)
from expert_work.persistence.platform_judge_config import (
    InMemoryPlatformJudgeConfigStore,
    PlatformJudgeConfigStore,
    SqlPlatformJudgeConfigStore,
)
from expert_work.persistence.platform_quality_config import (
    InMemoryPlatformQualityConfigStore,
    PlatformQualityConfigStore,
    SqlPlatformQualityConfigStore,
)
from expert_work.persistence.platform_secrets import (
    InMemoryPlatformSecretStore,
    PlatformSecretStore,
    SqlPlatformSecretStore,
)
from expert_work.persistence.platform_tool_budget_config import (
    InMemoryPlatformToolBudgetConfigStore,
    PlatformToolBudgetConfigStore,
    SqlPlatformToolBudgetConfigStore,
)
from expert_work.persistence.quality_candidate import (
    InMemoryQualityCandidateSource,
    QualityCandidateSource,
    SqlQualityCandidateSource,
)
from expert_work.persistence.quality_drift_alert import (
    InMemoryQualityDriftAlertStore,
    QualityDriftAlertStore,
    SqlQualityDriftAlertStore,
)
from expert_work.persistence.quality_score import (
    InMemoryQualityScoreStore,
    QualityScoreStore,
    SqlQualityScoreStore,
)
from expert_work.persistence.quota import (
    InMemoryTenantQuotaStore,
    InMemoryTokenReservationStore,
    SqlTenantQuotaStore,
    SqlTokenReservationStore,
    TenantQuotaStore,
    TokenReservationStore,
)
from expert_work.persistence.rls import build_rls_sessionmaker
from expert_work.persistence.sandbox_egress_audit import (
    InMemorySandboxEgressAuditStore,
    SandboxEgressAuditStore,
    SqlSandboxEgressAuditStore,
)
from expert_work.persistence.sandbox_instance_store import SqlSandboxInstanceStore
from expert_work.persistence.skill import (
    InMemorySkillStore,
    SkillStore,
    SqlSkillStore,
)
from expert_work.persistence.tenant_config import (
    InMemoryTenantConfigStore,
    SqlTenantConfigStore,
    TenantConfigStore,
)
from expert_work.persistence.tenant_mcp_server import (
    InMemoryTenantMcpServerStore,
    SqlTenantMcpServerStore,
    TenantMcpServerStore,
)
from expert_work.persistence.tenant_member import (
    InMemoryTenantMemberStore,
    SqlTenantMemberStore,
    TenantMemberStore,
)
from expert_work.persistence.tenant_skill_subscription import (
    InMemoryTenantSkillSubscriptionStore,
    SqlTenantSkillSubscriptionStore,
    TenantSkillSubscriptionStore,
)
from expert_work.persistence.tenant_user import (
    InMemoryTenantUserStore,
    SqlTenantUserStore,
    TenantUserStore,
)
from expert_work.persistence.thread_message import (
    InMemoryThreadMessageStore,
    SqlThreadMessageStore,
    ThreadMessageStore,
)
from expert_work.persistence.thread_meta import (
    InMemoryThreadMetaStore,
    SqlThreadMetaStore,
    ThreadMetaStore,
)
from expert_work.persistence.token_usage_store import (
    DbTokenUsageStore,
    InMemoryTokenUsageStore,
    TokenUsageStore,
)
from expert_work.persistence.trigger import (
    InMemoryTriggerRunStore,
    InMemoryTriggerStore,
    SqlTriggerRunStore,
    SqlTriggerStore,
    TriggerRunStore,
    TriggerStore,
)
from expert_work.persistence.webhook import (
    InMemoryWebhookDeliveryStore,
    InMemoryWebhookEndpointStore,
    SqlWebhookDeliveryStore,
    SqlWebhookEndpointStore,
    WebhookDeliveryStore,
    WebhookEndpointStore,
)
from expert_work.persistence.workspace import (
    InMemoryUserWorkspaceStore,
    SqlUserWorkspaceStore,
    UserWorkspaceStore,
)
from expert_work.protocol import (
    PROVIDER_CATALOG,
    AgentSpec,
    AuditAction,
    AuditResult,
    PlatformAgentTemplateStatus,
    TokenReservationRecord,
)
from expert_work.runtime.audit.logger import AuditLogger
from expert_work.runtime.checkpointer import make_checkpointer
from expert_work.runtime.middleware import make_langfuse_client
from expert_work.runtime.runs import (
    InMemoryRunEventStore,
    InMemoryRunStore,
    RunEventStore,
    RunStore,
    SqlRunEventStore,
    SqlRunStore,
)
from expert_work.runtime.secret_store import SecretStore, make_secret_store
from expert_work.runtime.storage import make_object_store
from orchestrator import MemoryEnv
from orchestrator.tools import (
    AgentSandboxClient,
    HTTPSupervisorRuntime,
    SupervisorWorkspaceStore,
)
from orchestrator.trajectory import TrajectoryReader

__all__ = ["create_app"]

logger = logging.getLogger("expert_work.control_plane")

_VERSION = "0.0.0"


def create_app(
    *,
    settings: Settings | None = None,
    lifecycle: Lifecycle | None = None,
    rate_limiter: RateLimiter | None = None,
    agent_spec_repo: AgentSpecStore | None = None,
    thread_meta_repo: ThreadMetaStore | None = None,
    tenant_user_repo: TenantUserStore | None = None,
    feedback_repo: FeedbackStore | None = None,
    token_usage_repo: TokenUsageStore | None = None,
    curation_candidate_repo: CurationCandidateStore | None = None,
    eval_dataset_repo: EvalDatasetStore | None = None,
    eval_run_repo: EvalRunStore | None = None,
    artifact_repo: ArtifactStore | None = None,
    knowledge_repo: KnowledgeStore | None = None,
    image_upload_repo: ImageUploadStore | None = None,
    approval_repo: ApprovalStore | None = None,
    run_repo: RunStore | None = None,
    run_event_repo: RunEventStore | None = None,
    trigger_repo: TriggerStore | None = None,
    trigger_run_repo: TriggerRunStore | None = None,
    webhook_endpoint_repo: WebhookEndpointStore | None = None,
    webhook_delivery_repo: WebhookDeliveryStore | None = None,
    skill_repo: SkillStore | None = None,
    knowledge_ingestion_runner: KnowledgeIngestionRunner | None = None,
    audit_logger: AuditLogger | None = None,
    manifest_loader: ManifestLoader | None = None,
    jwt_verifier: JWTVerifier | None = None,
    mtls_verifier: MTLSVerifier | None = None,
    service_account_repo: ServiceAccountStore | None = None,
    api_key_repo: ApiKeyStore | None = None,
    role_binding_repo: RoleBindingStore | None = None,
    api_key_verifier: ApiKeyVerifier | None = None,
    tenant_quota_repo: TenantQuotaStore | None = None,
    token_reservation_repo: TokenReservationStore | None = None,
    quota_service: QuotaService | None = None,
    enable_reaper: bool = True,
    enable_scheduler: bool = True,
    enable_curation_worker: bool = True,
    tenant_rate_limiter: RateLimiter | None = None,
    tenant_config_repo: TenantConfigStore | None = None,
    tenant_config_service: TenantConfigService | None = None,
    tenant_member_repo: TenantMemberStore | None = None,
    keycloak_admin_client: KeycloakAdminClient | None = None,
    platform_secret_store: PlatformSecretStore | None = None,
    secret_store: SecretStore | None = None,
    agent_runtime: AgentRuntime | None = None,
    memory_repo: MemoryStore | None = None,
) -> FastAPI:
    """Build a configured FastAPI app.

    :param settings: Optional pre-built settings (tests use this).
    :param lifecycle: Optional pre-built :class:`Lifecycle`; default is a
        fresh instance owned by the app.
    :param rate_limiter: Optional pre-built limiter (tests inject a stub
        or a tuned bucket). Defaults to :class:`InProcessTokenBucketLimiter`
        sized from ``settings.rate_limit_*``.
    :param jwt_verifier: Optional pre-built JWT verifier. Tests provide a
        :class:`StaticJWKSProvider`-backed verifier to avoid HTTP calls to
        Keycloak; production wiring builds one from ``settings`` (C.1).
    """
    resolved_settings = settings or Settings()
    # Stream O (Mini-ADR O-1) — Platform Catalog startup validation.
    # Fail fast at create_app rather than at first LLM call so a
    # mis-configured deployment crashes the pipeline at boot.
    _validate_platform_catalog(resolved_settings)
    # Stream O (Mini-ADR O-10) — warn + meter when the effective catalog
    # is still gap-filled from a legacy ``*_api_key_ref`` field.
    _signal_legacy_credentials_derivation(resolved_settings)
    # ADR B-6: in ``sql`` mode every store is Postgres-backed off one
    # RLS-wrapped sessionmaker; the engine is disposed in ``lifespan``.
    # Injected repos still win, so unit tests keep their in-memory
    # stores untouched.
    sql_stores = (
        _build_sql_stores(resolved_settings) if resolved_settings.store_backend == "sql" else None
    )
    resolved_lifecycle = lifecycle or Lifecycle()
    resolved_limiter = rate_limiter or _build_default_gateway_limiter(resolved_settings)
    resolved_tenant_limiter = tenant_rate_limiter or _build_default_tenant_limiter(
        resolved_settings
    )
    # Dedicated tight bucket for MCP probe-bearing endpoints (audit #6).
    resolved_mcp_probe_limiter = (
        _build_mcp_probe_limiter(resolved_settings)
        if resolved_settings.tenant_rate_limit_enabled
        else None
    )
    resolved_repo = agent_spec_repo or (
        sql_stores.agent_spec if sql_stores else InMemoryAgentSpecStore()
    )
    resolved_threads = thread_meta_repo or (
        sql_stores.thread_meta if sql_stores else InMemoryThreadMetaStore()
    )
    # Conversation IA M4 — the transcript mirror behind content search.
    resolved_thread_messages = (
        sql_stores.thread_message if sql_stores else InMemoryThreadMessageStore()
    )
    resolved_tenant_users = tenant_user_repo or (
        sql_stores.tenant_user if sql_stores else InMemoryTenantUserStore()
    )
    # Stream J.3 — long-term memory store for the agent runtime.
    resolved_memory_store: MemoryStore = memory_repo or (
        sql_stores.memory if sql_stores else InMemoryMemoryStore()
    )
    # Stream K.K7 — DLQ used by the writeback path; defaults match the
    # memory store's backing (SQL when sql_stores is set, else in-memory).
    resolved_memory_dlq: MemoryWritebackDLQ = (
        sql_stores.memory_dlq if sql_stores else InMemoryMemoryWritebackDLQ()
    )
    # Stream J.9 — artifact registry backing save_artifact / list_artifacts
    # and the artifact API. The sandbox runtime backs artifact content
    # download (only the supervisor can read a per-user volume); it is
    # shared with the agent tool env.
    resolved_artifact_store: ArtifactStore = artifact_repo or (
        sql_stores.artifact if sql_stores else InMemoryArtifactStore()
    )
    # Stream Playground-Uplift (D4) — read-only per-user workspace registry,
    # backing the playground workspace inspector (verify a user's VM/persistence).
    resolved_user_workspace_store: UserWorkspaceStore = (
        sql_stores.user_workspace if sql_stores else InMemoryUserWorkspaceStore()
    )
    # Stream J.5 — knowledge bases (RAG) backing the knowledge API + the
    # knowledge_search tool.
    resolved_knowledge_store: KnowledgeStore = knowledge_repo or (
        sql_stores.knowledge if sql_stores else InMemoryKnowledgeStore()
    )
    # Stream J.6.补强-3 (Mini-ADR J-32) — image upload registry.
    resolved_image_upload_store: ImageUploadStore = image_upload_repo or (
        sql_stores.image_upload if sql_stores else InMemoryImageUploadStore()
    )
    # Stream J.8 (Mini-ADR J-24) — paused-run approval registry.
    resolved_approval_store: ApprovalStore = approval_repo or (
        sql_stores.approval if sql_stores else InMemoryApprovalStore()
    )
    # Stream H.3 PR 3 (Mini-ADR H-7) — durable SSE event store. Assembled
    # before the run store: the in-memory run store empties a purged
    # thread's ``run_event`` children through it (deletion-hygiene PR3 §A).
    resolved_run_event_store: RunEventStore = run_event_repo or (
        sql_stores.run_event if sql_stores else InMemoryRunEventStore()
    )
    # Stream J.8 closeout follow-up (Mini-ADR J-41) — durable run
    # lifecycle store. The same instance is wired into the RunManager
    # (mirror writes) and read by GET .../runs/{id} as the fallback
    # once the in-memory record has expired.
    resolved_run_store: RunStore = run_repo or (
        # Stream RT-4 — the in-memory double needs the thread store to resolve a
        # run → agent for ``list_running_for_agent`` (the SQL store joins).
        sql_stores.run
        if sql_stores
        else InMemoryRunStore(
            thread_meta_store=resolved_threads, event_store=resolved_run_event_store
        )
    )
    # Stream J.10 (Mini-ADR J-26 / J-42) — trigger registry + firing log.
    resolved_trigger_store: TriggerStore = trigger_repo or (
        sql_stores.trigger if sql_stores else InMemoryTriggerStore()
    )
    resolved_trigger_run_store: TriggerRunStore = trigger_run_repo or (
        sql_stores.trigger_run if sql_stores else InMemoryTriggerRunStore()
    )
    # HX-9 (STREAM-HX § 13) — outbound webhook hook registry + delivery queue.
    resolved_webhook_endpoint_store: WebhookEndpointStore = webhook_endpoint_repo or (
        sql_stores.webhook_endpoint if sql_stores else InMemoryWebhookEndpointStore()
    )
    resolved_webhook_delivery_store: WebhookDeliveryStore = webhook_delivery_repo or (
        sql_stores.webhook_delivery if sql_stores else InMemoryWebhookDeliveryStore()
    )
    # Stream J.12 (Mini-ADR J-43) — curation candidate + eval-dataset registries.
    resolved_curation_candidate_store: CurationCandidateStore = curation_candidate_repo or (
        sql_stores.curation_candidate if sql_stores else InMemoryCurationCandidateStore()
    )
    resolved_eval_dataset_store: EvalDatasetStore = eval_dataset_repo or (
        sql_stores.eval_dataset if sql_stores else InMemoryEvalDatasetStore()
    )
    # P1-S2.1d — eval-run registry (eval platform ops layer).
    resolved_eval_run_store: EvalRunStore = eval_run_repo or (
        SqlEvalRunStore(sql_stores.session_factory) if sql_stores else InMemoryEvalRunStore()
    )
    # Stream J.7a (Mini-ADR J-23) — skill registry.
    resolved_skill_store: SkillStore = skill_repo or (
        sql_stores.skill if sql_stores else InMemorySkillStore()
    )
    # sandbox-egress §3.1 Phase 3 — read side of sandbox_egress_audit (the
    # credential-proxy writes it; the admin audit endpoint reads it).
    resolved_egress_audit_store: SandboxEgressAuditStore = (
        SqlSandboxEgressAuditStore(sql_stores.session_factory)
        if sql_stores
        else InMemorySandboxEgressAuditStore()
    )
    # 波 1 Task 7 — the agent_sandbox branch needs a warm-session CAS store;
    # only a real SQL session factory can back it durably (sandbox_instance
    # is shared with the docker-supervisor backend). ``None`` sql_stores
    # (persistence_backend="memory") falls back to the factory's own
    # InMemorySandboxInstanceStore default.
    #
    # 波 2 Task 4 — kept as a named variable (not inlined into the
    # build_sandbox_runtime call below) so build_workspace_store can be
    # handed the SAME store instance: NasWorkspaceStore.mark_deleted reads
    # get_warm() through it to find a soft-deleted user's warm session to
    # tear down (see that method's docstring), and it must be looking at the
    # same rows AgentSandboxClient itself writes.
    resolved_sandbox_instance_store = (
        SqlSandboxInstanceStore(sql_stores.session_factory) if sql_stores else None
    )
    resolved_sandbox_runtime = build_sandbox_runtime(
        resolved_settings,
        sandbox_instance_store=resolved_sandbox_instance_store,
    )
    # 波 1 Task 4 — workspace-file ops now live on a separate client, built
    # from the same supervisor URL (still the only thing that can reach the
    # docker-volume workspace today; wave 2's NAS mount replaces this).
    #
    # 波 2 Task 4 — built AFTER resolved_sandbox_runtime and handed both it
    # and resolved_sandbox_instance_store (brief-mandated ordering: "先
    # build_sandbox_runtime 再 build_workspace_store(settings, runtime=...,
    # instance_store=...)") so NasWorkspaceStore.mark_deleted can destroy a
    # purged user's warm sandbox session.
    resolved_workspace_store = build_workspace_store(
        resolved_settings,
        runtime=resolved_sandbox_runtime,
        instance_store=resolved_sandbox_instance_store,
    )
    resolved_feedback = feedback_repo or (
        sql_stores.feedback if sql_stores else InMemoryFeedbackStore()
    )
    resolved_token_usage = token_usage_repo or (
        sql_stores.token_usage if sql_stores else InMemoryTokenUsageStore()
    )
    # In-process agent runtime (RunManager + StreamBridge + manifest→agent
    # build path). Backend per ``secret_store_backend`` (Stream Q); tests
    # inject a runtime whose builder returns a fake-LLM agent.
    resolved_secret_store = secret_store or _build_secret_store(resolved_settings, sql_stores)
    resolved_agent_runtime = agent_runtime or make_agent_runtime(
        resolved_secret_store,
        run_store=resolved_run_store,
        run_event_store=resolved_run_event_store,
    )
    # SE-7d-3b-ii — wire run-end skill_run_usage emission. Always on (live
    # pilot finding #9): gating it behind ``enable_skill_rollback_monitor``
    # silently starved the rollback monitor AND the curator liveness signal
    # whenever evolution ran with the monitor off — data must accumulate
    # regardless of whether the read-side judgement loop is enabled. Cheap:
    # most runs bind zero distilled skills, so the emit is a no-op.
    resolved_agent_runtime.skill_run_usage_recorder = StoreSkillRunUsageRecorder(
        store=resolved_skill_store
    )
    # Late-bound PII resolver: lets the audit logger reference
    # tenant_config without forcing it to exist yet (D.2 cycle break).
    pii_resolver = TenantConfigPiiResolver()
    resolved_audit = audit_logger or build_default_audit_logger(
        store=sql_stores.audit_log if sql_stores else None,
        pii_fields_resolver=pii_resolver,
    )
    resolved_loader = manifest_loader or ManifestLoader()
    resolved_verifier = jwt_verifier or _build_default_jwt_verifier(resolved_settings)
    resolved_mtls = mtls_verifier or _build_default_mtls_verifier(resolved_settings)
    resolved_service_accounts = service_account_repo or (
        sql_stores.service_account if sql_stores else InMemoryServiceAccountStore()
    )
    resolved_api_keys = api_key_repo or (
        sql_stores.api_key if sql_stores else InMemoryApiKeyStore()
    )
    resolved_role_bindings = role_binding_repo or (
        sql_stores.role_binding if sql_stores else InMemoryRoleBindingStore()
    )
    resolved_api_key_verifier = api_key_verifier or ApiKeyVerifier.from_store(resolved_api_keys)
    resolved_tenant_quotas = tenant_quota_repo or (
        sql_stores.tenant_quota if sql_stores else InMemoryTenantQuotaStore()
    )
    # 沙箱迁移波 3 —— 工作区配额闸。tenant_quota store 建得比 sandbox
    # runtime 晚,所以走 post-assign(同 :1346 resolved_workspace_store.http
    # 的先例)。仅 agent_sandbox 后端 + 配了 NAS 根才有闸;其余部署
    # quota_gate 保持 None,零行为变化。
    resolved_workspace_quota: WorkspaceQuotaService | None = None
    if (
        isinstance(resolved_sandbox_runtime, AgentSandboxClient)
        and resolved_settings.workspace_nas_root
    ):
        resolved_workspace_quota = WorkspaceQuotaService(
            user_workspaces=resolved_user_workspace_store,
            tenant_quotas=resolved_tenant_quotas,
            workspace_root=resolved_settings.workspace_nas_root,
        )
        resolved_sandbox_runtime.quota_gate = resolved_workspace_quota
    resolved_reservations = token_reservation_repo or (
        sql_stores.token_reservation if sql_stores else InMemoryTokenReservationStore()
    )
    resolved_quota = quota_service or _build_default_quota_service(
        settings=resolved_settings,
        quota_store=resolved_tenant_quotas,
        reservation_store=resolved_reservations,
    )
    resolved_tenant_config_repo = tenant_config_repo or (
        sql_stores.tenant_config if sql_stores else InMemoryTenantConfigStore()
    )
    resolved_tenant_config_service = tenant_config_service or TenantConfigService(
        store=resolved_tenant_config_repo,
        audit_logger=resolved_audit,
        ttl_s=float(resolved_settings.tenant_config_cache_ttl_s),
    )
    # Stream U (PR E) — per-tenant suspended-status cache. AuthMiddleware reads
    # it on the hot path to 403 a suspended tenant's members; the
    # deactivate/activate endpoints call ``invalidate`` for immediate effect.
    # Stream RT-4 — this same service is the tenant half of the kill switch
    # (queue-claim / trigger / resume / orphan gates), so it uses the short
    # kill-switch TTL: suspend is an emergency stop and must propagate to peer
    # replicas in seconds, not up to a minute.
    resolved_tenant_status_service = TenantStatusService(
        store=resolved_tenant_config_repo,
        ttl_seconds=float(resolved_settings.kill_switch_cache_ttl_s),
    )
    # Stream RT-4 (RT-ADR-16) — agent-level kill switch store + TTL cache. The
    # admission / session-resolve / queue-claim gates read the cache; the
    # disable/enable endpoints call ``invalidate`` for immediate effect. Short
    # TTL (emergency stop) so a disabled agent stops being admitted platform-
    # wide within seconds even without an explicit per-replica invalidate.
    resolved_agent_disable_repo: AgentDisableStore = (
        sql_stores.agent_disable if sql_stores else InMemoryAgentDisableStore()
    )
    resolved_agent_disable_service = AgentDisableService(
        store=resolved_agent_disable_repo,
        ttl_seconds=float(resolved_settings.kill_switch_cache_ttl_s),
    )
    # Stream RT-5 (RT-ADR-22/24) — production quality monitor stores. The pull
    # worker (below, gated) samples finished runs and persists per-agent
    # verdicts; both fall back to in-memory for single-process / test runs.
    resolved_quality_score: QualityScoreStore = (
        sql_stores.quality_score if sql_stores else InMemoryQualityScoreStore()
    )
    resolved_quality_candidate: QualityCandidateSource = (
        sql_stores.quality_candidate if sql_stores else InMemoryQualityCandidateSource()
    )
    resolved_quality_drift_alert: QualityDriftAlertStore = (
        sql_stores.quality_drift_alert if sql_stores else InMemoryQualityDriftAlertStore()
    )
    # RT-5 PR-3b (§14) — platform quality config service (DB-wins, TTL-cached).
    # The workers read the effective config from it each cycle; the platform API
    # writes + invalidates it. Always constructed (the workers always start).
    resolved_quality_config_store: PlatformQualityConfigStore = (
        sql_stores.platform_quality_config if sql_stores else InMemoryPlatformQualityConfigStore()
    )
    quality_config_service = PlatformQualityConfigService(
        store=resolved_quality_config_store, settings=resolved_settings
    )
    # Stream R — member roster + Keycloak Admin client. The client is a Fake
    # unless ``keycloak_enabled`` (dev/CI never depend on a live Keycloak).
    resolved_tenant_member_repo = tenant_member_repo or (
        sql_stores.tenant_member if sql_stores else InMemoryTenantMemberStore()
    )
    # Stream V — tenant-registered remote MCP server registry.
    resolved_tenant_mcp_server_store = (
        sql_stores.tenant_mcp_server if sql_stores else InMemoryTenantMcpServerStore()
    )
    # Stream W — platform-curated MCP connector catalog (NULL-tenant rows).
    resolved_mcp_connector_catalog_store = (
        sql_stores.mcp_connector_catalog if sql_stores else InMemoryMcpConnectorCatalogStore()
    )
    # Stream Agent-Templates — platform-curated Agent templates (NULL-tenant rows).
    resolved_platform_agent_template_store = (
        sql_stores.platform_agent_template if sql_stores else InMemoryPlatformAgentTemplateStore()
    )
    # Stream Agent-Templates (M1-5b) — per-user agent-instance bindings.
    resolved_agent_instance_store = (
        sql_stores.agent_instance if sql_stores else InMemoryAgentInstanceStore()
    )
    # Stream MCP-OAUTH — per-user OAuth connections to hosted MCP connectors.
    resolved_mcp_oauth_connection_store = (
        sql_stores.mcp_oauth_connection if sql_stores else InMemoryMcpOAuthConnectionStore()
    )
    # Skill Marketplace Phase 1 — tenant→platform-skill subscription markers
    # (semantic A: accounting/UX only, never gates the runtime resolver).
    resolved_tenant_skill_subscription_store = (
        sql_stores.tenant_skill_subscription
        if sql_stores
        else InMemoryTenantSkillSubscriptionStore()
    )
    # Stream Y (Y-3) — platform-curated model rate card (NULL-tenant rows).
    resolved_model_rate_card_store = (
        sql_stores.model_rate_card if sql_stores else InMemoryModelRateCardStore()
    )
    # Stream Y (Y-4) — per-tenant billing ledger (tenant-scoped; Stream Z reads it).
    resolved_tenant_billing_ledger_store = (
        sql_stores.tenant_billing_ledger if sql_stores else InMemoryTenantBillingLedgerStore()
    )
    resolved_keycloak_admin_client = keycloak_admin_client or _build_keycloak_admin_client(
        resolved_settings, resolved_secret_store
    )
    # Stream P (Mini-ADR P-7/P-9) — platform credential overlay (DB wins over
    # env). The service is lazy (no DB read until first getter call), so it is
    # safe to build in the synchronous factory; lifespan wires its getters into
    # the CredentialsResolver.
    resolved_platform_secret_store = platform_secret_store or (
        sql_stores.platform_secret if sql_stores else InMemoryPlatformSecretStore()
    )
    resolved_platform_secrets_service = PlatformSecretsService(
        store=resolved_platform_secret_store,
        settings=resolved_settings,
        ttl_s=float(resolved_settings.tenant_config_cache_ttl_s),
    )
    # Stream T (PR B) — effective embedding/rerank config (DB-row wins, env
    # fallback), TTL-cached like PlatformSecretsService. The dynamic
    # embedder/reranker read it per call so an admin's change takes effect
    # without a restart; PR C's write endpoint calls ``invalidate`` for
    # immediate effect on the writing instance. Lazy (no DB read until the
    # first getter), so it is safe to build in the synchronous factory.
    resolved_platform_embedding_config_store = (
        sql_stores.platform_embedding_config
        if sql_stores
        else InMemoryPlatformEmbeddingConfigStore()
    )
    resolved_platform_embedding_config_service = PlatformEmbeddingConfigService(
        store=resolved_platform_embedding_config_store,
        settings=resolved_settings,
        ttl_seconds=float(resolved_settings.tenant_config_cache_ttl_s),
    )
    resolved_platform_judge_config_store: PlatformJudgeConfigStore = (
        sql_stores.platform_judge_config if sql_stores else InMemoryPlatformJudgeConfigStore()
    )
    # Phase 3 — platform tool-output-budget on/off, read at agent build time.
    resolved_platform_tool_budget_config_store: PlatformToolBudgetConfigStore = (
        sql_stores.platform_tool_budget_config
        if sql_stores
        else InMemoryPlatformToolBudgetConfigStore()
    )
    # Stream 12.4 — platform billing-rollup toggle, read by the offline rollup job.
    resolved_platform_billing_config_store: PlatformBillingConfigStore = (
        sql_stores.platform_billing_config if sql_stores else InMemoryPlatformBillingConfigStore()
    )
    resolved_platform_judge_config_service = PlatformJudgeConfigService(
        store=resolved_platform_judge_config_store,
        ttl_seconds=float(resolved_settings.tenant_config_cache_ttl_s),
    )
    resolved_platform_tool_budget_config_service = PlatformToolBudgetConfigService(
        store=resolved_platform_tool_budget_config_store,
        ttl_seconds=float(resolved_settings.tenant_config_cache_ttl_s),
    )
    # perf phase2 PR3 — platform delegation-gate capacity
    # (max_concurrent_delegations); DB-row wins over the built-in constant
    # default, mirroring the tool-budget pattern above. No settings env var —
    # the default is a frozen constant (T3 wires runtime consumption).
    resolved_platform_delegation_config_store: PlatformDelegationConfigStore = (
        sql_stores.platform_delegation_config
        if sql_stores
        else InMemoryPlatformDelegationConfigStore()
    )
    resolved_platform_delegation_config_service = PlatformDelegationConfigService(
        store=resolved_platform_delegation_config_store,
        env_default=DelegationConfig(max_concurrent_delegations=16),
        ttl_seconds=float(resolved_settings.tenant_config_cache_ttl_s),
    )
    # B3 PR2 — platform dynamic-worker limits (max_concurrent/max_per_run/
    # max_iterations); DB-row wins over the env-default settings snapshot,
    # mirroring the tool-budget pattern immediately above.
    resolved_platform_dynamic_worker_config_store: PlatformDynamicWorkerConfigStore = (
        sql_stores.platform_dynamic_worker_config
        if sql_stores
        else InMemoryPlatformDynamicWorkerConfigStore()
    )
    resolved_platform_dynamic_worker_config_service = PlatformDynamicWorkerConfigService(
        store=resolved_platform_dynamic_worker_config_store,
        env_default=DynamicWorkerConfig(
            max_concurrent=resolved_settings.dynamic_worker_max_concurrent,
            max_per_run=resolved_settings.dynamic_worker_max_per_run,
            max_iterations=resolved_settings.dynamic_worker_max_iterations,
        ),
        ttl_seconds=float(resolved_settings.tenant_config_cache_ttl_s),
    )
    # Complete the D.2 cycle: TenantAwareRedactor → resolver → service.
    pii_resolver.bind(resolved_tenant_config_service)

    async def _on_reservation_expired(row: TokenReservationRecord) -> None:
        """Audit a stale reservation the reaper transitioned to EXPIRED.

        The reaper invokes this inside the row's tenant RLS context (it has
        already SET the tenant + cleared bypass), so the audit write lands
        under the owning tenant. Errors are caught by the reaper's own
        ``on_expire`` guard, so a failed audit never blocks the release."""
        await audit_emit(
            resolved_audit,
            tenant_id=row.tenant_id,
            actor_id="quota_reaper",
            action=AuditAction.QUOTA_RESERVATION_EXPIRED,
            resource_type="quota",
            resource_id=str(row.id),
            result=AuditResult.SUCCESS,
            trace_id=current_trace_id_hex(),
            details={
                "agent_name": row.agent_name,
                "thread_id": str(row.thread_id),
                "estimated": row.estimated,
            },
        )

    reaper: ReservationReaper | None = (
        ReservationReaper(
            reservation_store=resolved_reservations,
            max_age_s=resolved_settings.quota_reservation_max_age_s,
            interval_s=resolved_settings.quota_reaper_interval_s,
            batch_size=resolved_settings.quota_reaper_batch_size,
            on_expire=_on_reservation_expired,
        )
        if enable_reaper
        else None
    )
    # Stream J.10 — single-replica trigger scheduler (Mini-ADR J-18 / J-42).
    scheduler: TriggerScheduler | None = (
        TriggerScheduler(
            trigger_store=resolved_trigger_store,
            trigger_run_store=resolved_trigger_run_store,
            run_store=resolved_run_store,
            agent_spec_store=resolved_repo,
            thread_store=resolved_threads,
            runtime=resolved_agent_runtime,
            audit_logger=resolved_audit,
            approval_store=resolved_approval_store,
            interval_s=resolved_settings.trigger_scheduler_interval_s,
            batch_size=resolved_settings.trigger_scheduler_batch_size,
            # Capability Uplift Sprint #1 — fire-time scan reads
            # tenant_config.trigger_fire_scan_mode (Mini-ADR U-2 Layer B).
            tenant_config_store=resolved_tenant_config_repo,
            # Stream RT-4 — kill switch gate for scheduled fires.
            agent_disable_service=resolved_agent_disable_service,
            tenant_status_service=resolved_tenant_status_service,
            # Spec 1 PR4 Task 2 — FU2: mirror-sync the originating thread into
            # content search after a successful delivery.
            thread_message_store=resolved_thread_messages,
        )
        if enable_scheduler
        else None
    )
    # Stream 9.4 (HA failover) — orphaned-run recovery sweep. Unlike the
    # single-replica scheduler/curator, EVERY instance runs this: the reclaim
    # CAS serialises competing sweepers so exactly one takes over each orphan.
    orphan_sweep: OrphanSweep | None = (
        OrphanSweep(
            run_store=resolved_run_store,
            thread_store=resolved_threads,
            agent_spec_store=resolved_repo,
            runtime=resolved_agent_runtime,
            audit_logger=resolved_audit,
            approval_store=resolved_approval_store,
            interval_s=resolved_settings.orphan_sweep_interval_s,
            batch_size=resolved_settings.orphan_sweep_batch_size,
            max_reclaims=resolved_settings.orphan_max_reclaims,
            auto_reclaim=resolved_settings.ha_auto_reclaim,
            # Stream RT-4 — never respawn a reclaimed run for a disabled agent
            # or suspended tenant.
            agent_disable_service=resolved_agent_disable_service,
            tenant_status_service=resolved_tenant_status_service,
        )
        if resolved_settings.enable_orphan_sweep
        else None
    )
    # Stream 9.5 — distributed run-queue worker. Drains ``status='queued'``
    # runs (POST /runs mode=queue) across instances; CAS-claim → exactly-once.
    run_queue_worker: RunQueueWorker | None = (
        RunQueueWorker(
            run_store=resolved_run_store,
            thread_store=resolved_threads,
            agent_spec_store=resolved_repo,
            runtime=resolved_agent_runtime,
            audit_logger=resolved_audit,
            approval_store=resolved_approval_store,
            interval_s=resolved_settings.run_queue_interval_s,
            batch_size=resolved_settings.run_queue_batch_size,
            # Stream RT-4 — the kill-switch gate: never claim a queued run whose
            # agent is disabled or whose tenant is suspended.
            agent_disable_service=resolved_agent_disable_service,
            tenant_status_service=resolved_tenant_status_service,
        )
        if resolved_settings.enable_run_queue_worker
        else None
    )
    # Stream 9.5 — approval timeout sweep. Auto-rejects pending approvals past
    # their ``timeout_at`` (J.8 human-in-the-loop completion). Every instance
    # runs it; ``mark_decided`` CAS serialises competing sweepers → exactly-once.
    approval_timeout_sweep: ApprovalTimeoutSweep | None = (
        ApprovalTimeoutSweep(
            approval_store=resolved_approval_store,
            thread_store=resolved_threads,
            agent_spec_store=resolved_repo,
            runtime=resolved_agent_runtime,
            audit_logger=resolved_audit,
            interval_s=resolved_settings.approval_timeout_sweep_interval_s,
            batch_size=resolved_settings.approval_timeout_sweep_batch_size,
            agent_disable_service=resolved_agent_disable_service,
            tenant_status_service=resolved_tenant_status_service,
        )
        if resolved_settings.enable_approval_timeout_sweep
        else None
    )
    # Conversation IA M4 — transcript mirror sweep. Copies user/assistant
    # turns off the LangGraph checkpoint into ``thread_message`` so the
    # browser's content search runs as an indexed, RLS-scoped query.
    # Idempotent ON CONFLICT writes → safe on every instance.
    transcript_mirror_sweep: TranscriptMirrorSweep | None = (
        TranscriptMirrorSweep(
            message_store=resolved_thread_messages,
            runtime=resolved_agent_runtime,
            interval_s=resolved_settings.transcript_mirror_sweep_interval_s,
            batch_size=resolved_settings.transcript_mirror_sweep_batch_size,
        )
        if resolved_settings.enable_transcript_mirror_sweep
        else None
    )
    # Capability Uplift Sprint #4 (Mini-ADRs U-26 / U-27) — Curator
    # sweep + activity recorder. Curator is single-replica (same
    # rationale as the trigger scheduler); the recorder is process-
    # local (each replica throttles independently — Curator's day-scale
    # decisions don't notice the per-replica fuzz).
    skill_curator: SkillCurator | None = (
        SkillCurator(
            skill_store=resolved_skill_store,
            tenant_config_service=resolved_tenant_config_service,
            audit_logger=resolved_audit,
            interval_s=float(resolved_settings.skill_curator_interval_s),
            # Single-flight the sweep across replicas (advisory lock); None
            # in single-process / test runs needs no lock.
            session_factory=sql_stores.session_factory if sql_stores else None,
        )
        if enable_scheduler
        else None
    )
    skill_activity_recorder = ThrottledActivityRecorder(
        resolved_skill_store,
        ttl_seconds=resolved_settings.skill_activity_throttle_s,
    )
    health_provider = DefaultHealthProvider(
        service=resolved_settings.service_name,
        version=_VERSION,
        lifecycle=resolved_lifecycle,
        dependencies=None,
        check_timeout_s=resolved_settings.health_check_timeout_s,
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        # W1-PR2 Task 5 — multi-replica configuration-consistency guard.
        # ``_build_default_gateway_limiter`` / ``_build_default_tenant_limiter``
        # / ``_build_mcp_probe_limiter`` (below) silently fall back to the
        # in-process bucket whenever ``quota_redis_url`` is unset — fine for
        # single_instance=True, but under single_instance=False that fallback
        # means every replica enforces its OWN independent bucket instead of
        # a shared one, i.e. no limit at all under horizontal scale-out
        # (ratelimit/in_process.py's module docstring). Fail fast at startup
        # rather than let that ship as a silent no-op degradation.
        if not resolved_settings.single_instance and not resolved_settings.quota_redis_url:
            msg = (
                "multi-replica deployment (single_instance=False) requires "
                "EXPERT_WORK_QUOTA_REDIS_URL to be set. Without it, every "
                "replica's gateway / tenant / MCP-probe rate limiter falls "
                "back to the in-process token bucket — each replica then "
                "keeps its own independent count, so the configured limit "
                "is not actually enforced across the fleet."
            )
            raise RuntimeError(msg)
        # Final-review I-2 — same rationale, different failure mode: without
        # a shared HMAC salt, each replica mints its own random bucket-id
        # key at construction (``RateLimitMiddleware``), so a given
        # ``X-API-Key`` hashes to a *different* Redis bucket on every
        # replica it happens to land on. The apikey-dimension limit is then
        # effectively multiplied by the replica count instead of shared
        # across the fleet — the Redis backend alone (guarded above) is not
        # sufficient without also pinning the key derivation.
        if (
            not resolved_settings.single_instance
            and resolved_settings.apikey_rate_limit_hmac_salt is None
        ):
            msg = (
                "multi-replica deployment (single_instance=False) requires "
                "EXPERT_WORK_APIKEY_RATE_LIMIT_HMAC_SALT to be set. Without "
                "it, every replica mints its own random HMAC key at "
                "startup, so the same API key hashes to a different "
                "rate-limit bucket per replica — the apikey-dimension limit "
                "is effectively relaxed N-fold across N replicas instead of "
                "enforced. All replicas must be configured with the same "
                "salt value."
            )
            raise RuntimeError(msg)
        init_logging(
            service=resolved_settings.service_name,
            env=resolved_settings.env,
            level=resolved_settings.log_level,
        )
        init_tracing(
            service_name=resolved_settings.service_name,
            env=resolved_settings.env,
            otlp_endpoint=resolved_settings.otlp_traces_endpoint,
        )
        async with AsyncExitStack() as stack:
            # 一期 Task 5 — one process-level httpx.AsyncClient, shared across
            # every LLM / embed / rerank / web-search / sandbox-supervisor
            # call so each reuses a pooled connection instead of paying a
            # fresh TLS handshake per call. httpx pools per (scheme, host,
            # port) internally, so a single instance is enough — no
            # per-provider split needed. Wired into the deps below (embedder /
            # reranker / web-search client / sandbox runtime / agent
            # builder); ``stack`` closes it on shutdown, after every other
            # cleanup callback that might still use it has run (LIFO).
            shared_http = httpx.AsyncClient(
                # No client-level default timeout: every call site passes
                # timeout= per-request explicitly (see each provider's
                # client_for() usage). If one call site is ever missed, the
                # gap should be caught by the router's first_token/idle
                # timeout or the run deadline — NOT by httpx's built-in 5s
                # default silently killing a long-context prefill or a
                # pip-install-grade sandbox exec (code review: deleting any
                # one call site's timeout= left the full test suite green —
                # there is no automated gate for "one call site missed", so
                # the fallback behaviour must be the forgiving one).
                timeout=None,  # noqa: S113 — deliberate; see comment above
                # Keepalive is bounded (that's where the handshake-reuse win
                # comes from); total connections is left unbounded — a 256
                # cap would be a NEW global concurrency gate this refactor
                # would introduce as a side effect. A full pool makes
                # httpx.PoolTimeout translate into LLMNetworkError /
                # "supervisor unreachable", masquerading as a provider
                # fault and triggering a retry storm, when pre-refactor
                # semantics were unbounded.
                # keepalive_expiry: httpx's default is 5s — far below this
                # workload's real inter-call gaps (an agent's LLM calls are
                # separated by tool execution: a 30s bash run is routine, and
                # the first live probe showed bench rounds ~20-40s apart paid
                # a fresh handshake every time). 55s, not 60s: 60 lands
                # exactly on AWS ALB's default idle timeout, so a
                # server-side and client-side expiry at the same value would
                # race every time — 55 keeps us strictly under it (client
                # closing first is the convention; a server that closes
                # earlier still gets caught by httpcore's staleness check on
                # reuse).
                limits=httpx.Limits(
                    max_keepalive_connections=64,
                    max_connections=None,
                    keepalive_expiry=55.0,
                ),
            )
            # The shared client's persistent cookie jar is a NEW cross-tenant
            # state channel: a Set-Cookie a vendor returns for tenant A's
            # call would otherwise be replayed on tenant B's call to the same
            # vendor domain. Every LLM / embed / rerank / web-search /
            # sandbox-supervisor call here is header-authenticated, so an
            # empty cookie policy (never store) is zero-loss. A DefaultCookiePolicy
            # subclass of httpx.Cookies would NOT work — the ``.cookies``
            # setter always re-wraps into the base ``httpx.Cookies`` class.
            shared_http.cookies.jar.set_policy(
                http.cookiejar.DefaultCookiePolicy(allowed_domains=[])
            )
            _app.state.shared_http = shared_http  # ops introspection hook; no reader yet
            stack.push_async_callback(shared_http.aclose)
            # 二期 PR2 T3 — process-level secret-value cache(LRU 256 / TTL
            # 300s,plan 拍定值 = 类默认参数)。embed / rerank / aux / judge
            # 的 vault 读变缓存命中;T2 的凭据写入口经
            # ``app.state.credential_value_cache``(getattr)连动失效,闭合
            # 换 key 生效链。跨副本陈旧度由 TTL 兜底(本仓立场)。
            credential_value_cache = CredentialValueCache()
            _app.state.credential_value_cache = credential_value_cache
            # Stream MCP-OAUTH (OA-5) — env-seed the connector catalog before
            # serving: oauth2 connectors whose ${VAR} client_id placeholders
            # resolve from the environment are created idempotently.
            await _seed_mcp_catalog(resolved_settings, resolved_mcp_connector_catalog_store)
            # Wire the agent runtime's backends before serving: the
            # checkpointer (E.1) plus the tool / middleware env bundles
            # (PR1.5). No run starts before lifespan completes, so the
            # agent cache is still empty — swapping the builder is
            # race-free. An injected runtime (tests) is left untouched.
            curation_worker: CurationWorker | None = None
            skill_evolution_worker: SkillEvolutionWorker | None = None
            skill_rollback_monitor: RollbackMonitor | None = None
            feedback_consumer: FeedbackConsumerWorker | None = None
            webhook_delivery_worker: WebhookDeliveryWorker | None = None
            eval_worker: EvalWorker | None = None
            quality_monitor: QualityMonitorWorker | None = None
            quality_drift: QualityDriftWorker | None = None
            approval_gauge_worker: ApprovalGaugeWorker | None = None
            sandbox_reap_worker: SandboxReapWorker | None = None
            sandbox_egress_metrics_worker: SandboxEgressMetricsWorker | None = None
            workspace_janitor_worker: WorkspaceJanitorWorker | None = None
            if agent_runtime is None:
                if resolved_settings.checkpointer_backend == "postgres":
                    if not resolved_settings.checkpointer_dsn:
                        msg = "checkpointer_backend='postgres' requires checkpointer_dsn"
                        raise RuntimeError(msg)
                    checkpointer = await stack.enter_async_context(
                        make_checkpointer("postgres", resolved_settings.checkpointer_dsn)
                    )
                    logger.info("control_plane.checkpointer.postgres_ready")
                else:
                    checkpointer = InMemorySaver()
                # Expose the durable checkpointer so the ``/messages`` resume
                # endpoint can read a thread's checkpoint directly (no agent
                # rebuild). Same instance bound into ``agent_builder`` below.
                resolved_agent_runtime.durable_checkpointer = checkpointer
                # Stream O Mini-ADR O-9/O-10 — one CredentialsResolver over
                # the effective (legacy-derived) catalog. The web_search /
                # embedder / reranker callers and the consolidator aux model
                # all resolve per-tenant credentials through it. Stream HX-8:
                # the overlay subclass resolves through the tenant-effective
                # view (per-tenant override rows win, disabled rows suppress).
                credentials_resolver: CredentialsResolver = TenantOverlayCredentialsResolver(
                    tenant_provider_view=(
                        resolved_platform_secrets_service.effective_provider_credentials_for
                    ),
                    tenant_tool_view=(
                        resolved_platform_secrets_service.effective_tool_credentials_for
                    ),
                    # Stream Y-MK — tenant-effective per-provider key list for
                    # the agent's main-model multi-key failover.
                    tenant_provider_keys_view=(
                        resolved_platform_secrets_service.effective_provider_secret_refs_for
                    ),
                    platform_provider_credentials=(
                        resolved_settings.effective_platform_provider_credentials
                    ),
                    platform_tool_credentials=(
                        resolved_settings.effective_platform_tool_credentials
                    ),
                    tenant_config_getter=resolved_tenant_config_service,
                    # Stream P (Mini-ADR P-9) — live merged view (env + DB
                    # overlay); these win over the static dicts above so a
                    # platform admin's runtime change takes effect within the
                    # service's TTL without a restart.
                    platform_provider_getter=(
                        resolved_platform_secrets_service.effective_provider_credentials
                    ),
                    platform_tool_getter=(
                        resolved_platform_secrets_service.effective_tool_credentials
                    ),
                )
                _app.state.credentials_resolver = credentials_resolver
                web_search_client = resolve_web_search_client(
                    searxng_base_url=resolved_settings.web_search_searxng_base_url,
                    http=shared_http,
                )
                # 一期 Task 5 — the sandbox runtime is built earlier (before
                # ``shared_http`` exists, so ``app.state.sandbox_runtime``
                # is well-defined pre-lifespan too); wire the shared client in
                # now. ``HTTPSupervisorRuntime`` is not frozen, so this mutates
                # the same instance ``app.state.sandbox_runtime`` and
                # ``base_tool_env`` below both reference. Guarded by
                # isinstance (not ``SandboxRuntime`` the Protocol, which has
                # no ``http`` member) so a future wrapper implementation
                # doesn't silently take a ``.http = ...`` assignment as a
                # stray attribute and lose pooling without either an error or
                # a type-checker complaint.
                if isinstance(resolved_sandbox_runtime, HTTPSupervisorRuntime):
                    resolved_sandbox_runtime.http = shared_http
                # Same pre-lifespan-build / in-place-mutate dance for the
                # split-out workspace-file client (波 1 Task 4).
                if isinstance(resolved_workspace_store, SupervisorWorkspaceStore):
                    resolved_workspace_store.http = shared_http
                mcp_pool = await stack.enter_async_context(
                    build_mcp_pool(
                        resolved_settings.mcp_servers_config_file,
                        secret_store=resolved_secret_store,
                    )
                )

                # Stream V (Mini-ADR V-4) — per-tenant remote MCP pool.
                # Built lazily on first agent build per tenant, cached,
                # and closed at shutdown via the exit stack callback.
                async def _tenant_mcp_client_factory(cfg):  # type: ignore[no-untyped-def]
                    return await _build_mcp_client(cfg, secret_store=resolved_secret_store)

                tenant_mcp_pool_service = TenantMcpPoolService(
                    store=resolved_tenant_mcp_server_store,
                    secret_store=resolved_secret_store,
                    client_factory=_tenant_mcp_client_factory,
                )
                stack.push_async_callback(tenant_mcp_pool_service.close_all)
                _app.state.tenant_mcp_pool_service = tenant_mcp_pool_service

                async def _tenant_mcp_pool_provider(tenant_id):  # type: ignore[no-untyped-def]
                    return await tenant_mcp_pool_service.get_or_build(tenant_id)

                # Stream MCP platform-servers (P1b) — process-global pool of the
                # platform-curated SHARED catalog servers (none/bearer). Built
                # lazily, cached, invalidated on catalog change (mcp_catalog API),
                # closed at shutdown. Reuses the tenant client factory.
                platform_mcp_pool_service = PlatformMcpPoolService(
                    store=resolved_mcp_connector_catalog_store,
                    client_factory=_tenant_mcp_client_factory,
                )
                stack.push_async_callback(platform_mcp_pool_service.close_all)
                _app.state.platform_mcp_pool_service = platform_mcp_pool_service

                async def _platform_mcp_pool_provider():  # type: ignore[no-untyped-def]
                    return await platform_mcp_pool_service.get_or_build()

                # Stream Agent-Templates (M1-3) — resolve a fork's ``extends`` to
                # its platform base template so build-time floor enforcement runs.
                # NULL-tenant rows → bypass_rls (the W-8 trap, same as the MCP
                # platform pool). ``"latest"`` picks the newest PUBLISHED version.
                _platform_template_store = resolved_platform_agent_template_store

                async def _platform_template_resolver(name: str, version: str) -> AgentSpec | None:
                    async with bypass_rls_session():
                        if version == "latest":
                            rec = await _platform_template_store.get_latest(
                                name=name,
                                status=PlatformAgentTemplateStatus.PUBLISHED,
                            )
                        else:
                            rec = await _platform_template_store.get(name=name, version=version)
                    return rec.spec if rec is not None else None

                # Stream MCP-OAUTH (OA-3b) — per-(tenant,user) OAuth MCP pool;
                # OA-6 refresher lazily renews near-expiry access tokens at build.
                mcp_oauth_refresher = McpOAuthRefresher(
                    oauth_store=resolved_mcp_oauth_connection_store,
                    catalog_store=resolved_mcp_connector_catalog_store,
                    secret_store=resolved_secret_store,
                    http_factory=default_http_client,
                    # OA-6 hardening — cross-replica lock so concurrent replicas
                    # don't race the rotated refresh token. Raw (non-RLS) session;
                    # None on in-memory dev (single process → asyncio.Lock suffices).
                    refresh_lock=(
                        PgMcpOAuthRefreshLock(create_async_session_factory(sql_stores.engine))
                        if sql_stores is not None
                        else None
                    ),
                    # 二期 PR2 T2 — a refresh overwrites the token secrets in
                    # place; evict that user's cached built agents (top-level +
                    # delegated via the registered user hook) so the next build
                    # resolves the fresh token instead of the baked-in old one.
                    invalidate_user=resolved_agent_runtime.invalidate_user,
                )
                user_mcp_oauth_pool_service = UserMcpOAuthPoolService(
                    oauth_store=resolved_mcp_oauth_connection_store,
                    catalog_store=resolved_mcp_connector_catalog_store,
                    client_factory=_tenant_mcp_client_factory,
                    refresher=mcp_oauth_refresher,
                )
                stack.push_async_callback(user_mcp_oauth_pool_service.close_all)
                _app.state.user_mcp_oauth_pool_service = user_mcp_oauth_pool_service

                async def _user_mcp_oauth_pool_provider(tenant_id, user_id):  # type: ignore[no-untyped-def]
                    return await user_mcp_oauth_pool_service.get_or_build(tenant_id, user_id)

                # Stream J.6 — object store for uploaded images + the
                # image resolver both multimodal paths draw on (Path A
                # content blocks / Path B ask_image).
                object_store = await stack.enter_async_context(
                    make_object_store(
                        resolved_settings.object_store_backend,
                        await resolve_object_store_config(
                            backend=resolved_settings.object_store_backend,
                            endpoint_url=resolved_settings.object_store_endpoint_url,
                            region=resolved_settings.object_store_region,
                            bucket=resolved_settings.object_store_bucket,
                            access_key_ref=resolved_settings.object_store_access_key_ref,
                            secret_key_ref=resolved_settings.object_store_secret_key_ref,
                            secret_store=resolved_secret_store,
                            addressing_style=resolved_settings.object_store_addressing_style,
                        ),
                    )
                )
                _app.state.object_store = object_store
                # 沙箱迁移波 3 (spec § 五):janitor 只在云工作区路径
                # (quota gate 已组装)时上岗——条件跟随实际组装物,
                # 照 SandboxReapWorker 的 isinstance 先例,不另开开关。
                # 另需 object_store_backend == "s3-compatible":janitor 归档
                # 删的是真数据(purge 后 rm -rf NAS 目录),内存 object store
                # 重启即丢——90 天恢复承诺会悄悄落空,内存后端不算数。
                if (
                    resolved_workspace_quota is not None
                    and resolved_settings.workspace_nas_root
                    and resolved_settings.object_store_backend == "s3-compatible"
                ):
                    workspace_janitor_worker = WorkspaceJanitorWorker(
                        user_workspaces=resolved_user_workspace_store,
                        quota_service=resolved_workspace_quota,
                        object_store=object_store,
                        workspace_root=resolved_settings.workspace_nas_root,
                        session_factory=sql_stores.session_factory if sql_stores else None,
                        interval_s=float(resolved_settings.workspace_janitor_interval_s),
                    )
                    workspace_janitor_worker.start()
                    _app.state.workspace_janitor_worker = workspace_janitor_worker
                elif resolved_workspace_quota is not None and resolved_settings.workspace_nas_root:
                    logger.info("control_plane.workspace_janitor.not_started_memory_object_store")
                # skill-asset-store — only a DURABLE backend may hold skill
                # supporting-file bytes (memory loses them on restart).
                skill_asset_store = (
                    object_store
                    if resolved_settings.object_store_backend == "s3-compatible"
                    else None
                )
                _app.state.skill_asset_store = skill_asset_store
                # Stream L.L7 — record each finished run's trajectory to the
                # ObjectStore now that it is open. This is the source the
                # curation worker scans (→ the skill-evolution flywheel) and the
                # J.13 eval gate reads; without it the run path wrote nothing and
                # the flywheel had no fuel. The run_agent call sites pass
                # ``runtime.trajectory_recorder``.
                from orchestrator.trajectory.recorder import TrajectoryRecorder

                resolved_agent_runtime.trajectory_recorder = TrajectoryRecorder(
                    object_store=object_store
                )
                # 1.3 Orchestrator-Worker — per-run spawn budget bounds (the
                # builder is wired separately above). ``new_worker_spawn_budget``
                # returns a fresh budget per run at the run_agent call sites.
                resolved_agent_runtime.dynamic_workers_enabled = (
                    resolved_settings.enable_dynamic_workers
                )
                resolved_agent_runtime.dynamic_worker_max_concurrent = (
                    resolved_settings.dynamic_worker_max_concurrent
                )
                resolved_agent_runtime.dynamic_worker_max_per_run = (
                    resolved_settings.dynamic_worker_max_per_run
                )
                # B3 PR2 — hot path: new_worker_spawn_budget() reads THROUGH this
                # service (DB-wins-over-env, live) when set; the three cold-read
                # attrs above remain its fallback (service absent).
                resolved_agent_runtime.dynamic_worker_config_service = (
                    resolved_platform_dynamic_worker_config_service
                )
                # perf phase2 PR3 T3 — delegation_gate() reads THROUGH this
                # service live (DB-wins-over-env); ``None`` would keep the
                # gate unwired (delegations run ungated).
                resolved_agent_runtime.delegation_config_service = (
                    resolved_platform_delegation_config_service
                )
                # Stream J.12 — the curation worker reads the L7
                # trajectory ObjectStore, so it is constructed here
                # where the store exists (mirrors MemoryDLQWorker).
                if enable_curation_worker:
                    curation_worker = CurationWorker(
                        trajectory_reader=TrajectoryReader(object_store=object_store),
                        candidate_store=resolved_curation_candidate_store,
                        thread_store=resolved_threads,
                        feedback_store=resolved_feedback,
                        # SE-16 (SE-A38) — settled-quietly implicit-positive check.
                        run_store=resolved_run_store,
                        interval_s=resolved_settings.curation_worker_interval_s,
                        batch_size=resolved_settings.curation_worker_batch_size,
                    )
                image_resolver = make_image_resolver(object_store)
                # Stream J.3 + Stream T (PR B) — long-term memory backend for
                # the agent. The embedder reads the live platform embedding
                # config per call (DB-row wins, env fallback), so an admin's
                # config change takes effect without a restart (Mini-ADR T-3).
                # It is ALWAYS a valid object; if embedding is unconfigured the
                # ``embed`` call raises ``AgentFactoryError`` at use time. The
                # build-time "memory.long_term needs embedding" gate now lives
                # in ``make_agent_builder`` (it checks the effective config).
                embedder = DynamicResolvingEmbedder(
                    config_service=resolved_platform_embedding_config_service,
                    resolver=credentials_resolver,
                    secret_store=resolved_secret_store,
                    http=shared_http,
                    secret_cache=credential_value_cache,
                )
                # Stream K.K6 — memory CRUD endpoint needs the embedder
                # for the PATCH path (re-embed on content change). GET /
                # DELETE work without it; the PATCH path surfaces the
                # embedding-unconfigured error at call time now that the
                # embedder object is always present.
                _app.state.embedder = embedder
                # Stream J.5 — the knowledge retriever backing knowledge_search:
                # hybrid recall + an optional (deployment-configured) LLM rerank.
                # The reranker likewise reads the live rerank config per call and
                # degrades to identity order when rerank is unconfigured.
                reranker = DynamicResolvingReranker(
                    config_service=resolved_platform_embedding_config_service,
                    resolver=credentials_resolver,
                    secret_store=resolved_secret_store,
                    http=shared_http,
                    secret_cache=credential_value_cache,
                )
                knowledge_retriever = make_knowledge_retriever(
                    store=resolved_knowledge_store, embedder=embedder, reranker=reranker
                )
                # Reused by the ``/v1/knowledge/bases/{name}/test`` endpoint so
                # the retrieval-test mirrors production retrieval exactly.
                _app.state.knowledge_retriever = knowledge_retriever
                base_tool_env = build_tool_env(
                    resolved_tenant_config_service,
                    web_search_client=web_search_client,
                    sandbox_runtime=resolved_sandbox_runtime,
                    mcp_pool=mcp_pool,
                    artifact_store=resolved_artifact_store,
                    knowledge_retriever=knowledge_retriever,
                    image_resolver=image_resolver,
                    # Stream TE-8 — cross-replica per-workspace write lock for
                    # write_file / bash. A raw (non-RLS) sessionmaker: the
                    # advisory lock touches no tenant tables.
                    workspace_lock=(
                        PgWorkspaceLock(create_async_session_factory(sql_stores.engine))
                        if sql_stores is not None
                        else None
                    ),
                )
                # Stream HX-7 — resolve the Langfuse client once: the
                # SDK adapter when the deployment configures an instance,
                # the M0 recording stub otherwise (Mini-ADR HX-G3).
                langfuse_client = make_langfuse_client(
                    host=resolved_settings.langfuse_host,
                    public_key=resolved_settings.langfuse_public_key,
                    secret_key=resolved_settings.langfuse_secret_key,
                    # Mini-ADR OBS-L1 — PII masking on by default (fail-safe).
                    pii_masking_enabled=resolved_settings.langfuse_pii_masking_enabled,
                )
                # Batch 4b Task 2 — a second, raw Langfuse SDK instance for
                # read-only trace fetches (``.api.trace.get``); the adapter
                # above only exposes the write-side surface the middleware
                # protocol needs. Same three-env gate as above; ``None``
                # when the deployment doesn't configure Langfuse.
                langfuse_read_client: object | None = None
                if (
                    resolved_settings.langfuse_host
                    and resolved_settings.langfuse_public_key
                    and resolved_settings.langfuse_secret_key
                ):
                    from langfuse import Langfuse

                    langfuse_read_client = Langfuse(
                        public_key=resolved_settings.langfuse_public_key,
                        secret_key=resolved_settings.langfuse_secret_key,
                        host=resolved_settings.langfuse_host,
                    )
                _app.state.langfuse_read_client = langfuse_read_client
                middleware_env = build_middleware_env(
                    token_usage_store=resolved_token_usage,
                    langfuse_client=langfuse_client,
                )
                memory_env = MemoryEnv(
                    store=resolved_memory_store,
                    embedder=embedder,
                    dlq=resolved_memory_dlq,  # K.K7 — failed writebacks land here
                    # Capability Uplift Sprint #6 (Mini-ADR U-5) — recall
                    # node reads tenant_config.memory_recall_mode.
                    # 二期 P1.2 —— 经 ServiceBackedTenantConfigStore 走
                    # TenantConfigService 的 60s 缓存,不再每次召回打 DB。
                    tenant_config_store=ServiceBackedTenantConfigStore(
                        service=resolved_tenant_config_service
                    ),
                    # Stream CM-4 — the same DynamicResolvingReranker that backs
                    # knowledge_search reorders long-term memory recall too. It
                    # reads the live rerank config per call and degrades to the
                    # RRF order when rerank is unconfigured.
                    reranker=reranker,
                )
                # Stream J.4 — the ChildAgentBuilder lets a SubAgentTool
                # resolve an agent_ref and recursively build the sub-agent;
                # the top-level agent's ToolEnv carries it so delegation
                # works at depth 0.
                child_agent_builder = make_child_agent_builder(
                    spec_store=resolved_repo,
                    secret_store=resolved_secret_store,
                    checkpointer=checkpointer,
                    base_tool_env=base_tool_env,
                    middleware_env=middleware_env,
                    memory_env=memory_env,
                    # Stream Q (Mini-ADR Q-5) — sub-agents whose manifest omits
                    # api_key_ref resolve the chat-LLM key from platform creds too.
                    credentials_resolver=credentials_resolver,
                    # Stream V (Mini-ADR V-4) — tenant's own remote MCP pool.
                    tenant_mcp_pool_provider=_tenant_mcp_pool_provider,
                    # Stream MCP platform-servers (P1b) — shared catalog pool.
                    platform_mcp_pool_provider=_platform_mcp_pool_provider,
                    # MCP-OAUTH (OA-3b-后续) — delegated sub-agents inherit the
                    # caller's per-user OAuth pool (keyed on the run's oauth_user_id).
                    user_mcp_oauth_pool_provider=_user_mcp_oauth_pool_provider,
                    # Stream X (Mini-ADR X-4) — sub-agents resolve skills too.
                    skill_store=resolved_skill_store,
                    skill_activity_recorder=skill_activity_recorder,
                    tenant_config_service=resolved_tenant_config_service,
                    skill_asset_store=skill_asset_store,
                    # Stream V-D (audit #1) — evict cached sub-agents when a
                    # tenant's MCP registry changes, like the top-level cache.
                    register_invalidation=resolved_agent_runtime.register_invalidation_hook,
                    # Stream MCP platform-servers (P1b) — evict ALL cached
                    # sub-agents when the process-global catalog pool changes.
                    register_invalidation_all=resolved_agent_runtime.register_invalidation_all,
                    # 二期 PR2 T2 — evict a user's cached per-user (OAuth) child
                    # builds when invalidate_user fires (token refresh / disconnect).
                    register_user_invalidation=resolved_agent_runtime.register_user_invalidation_hook,
                    # 一期 Task 5 — delegated sub-agent builds reuse the shared pool too.
                    http_client=shared_http,
                )
                # 1.3 Orchestrator-Worker — the spawn_worker builder (synthesizes
                # ephemeral workers from the parent). ``None`` when the platform
                # switch is off → no spawn_worker tool registered anywhere.
                worker_build_fn = (
                    make_worker_build_fn(
                        secret_store=resolved_secret_store,
                        checkpointer=checkpointer,
                        base_tool_env=base_tool_env,
                        max_iterations=resolved_settings.dynamic_worker_max_iterations,
                        allowed_toolsets=list(resolved_settings.dynamic_worker_allowed_toolsets),
                        middleware_env=middleware_env,
                        memory_env=memory_env,
                        credentials_resolver=credentials_resolver,
                        tenant_mcp_pool_provider=_tenant_mcp_pool_provider,
                        # Stream MCP platform-servers (P1b) — shared catalog pool.
                        platform_mcp_pool_provider=_platform_mcp_pool_provider,
                        # MCP-OAUTH (OA-3b-后续) — workers inherit caller OAuth pool.
                        user_mcp_oauth_pool_provider=_user_mcp_oauth_pool_provider,
                        skill_store=resolved_skill_store,
                        skill_activity_recorder=skill_activity_recorder,
                        tenant_config_service=resolved_tenant_config_service,
                        skill_asset_store=skill_asset_store,
                        # B3 PR2 — per-build max_iterations reads through this
                        # service (DB-wins-over-env, live); ``max_iterations=``
                        # above stays its boot-time fallback.
                        dynamic_worker_config_service=resolved_platform_dynamic_worker_config_service,
                        # 一期 Task 5 — spawned worker builds reuse the shared pool too.
                        http_client=shared_http,
                    )
                    if resolved_settings.enable_dynamic_workers
                    else None
                )
                resolved_agent_runtime.agent_builder = make_agent_builder(
                    resolved_secret_store,
                    checkpointer,
                    default_run_deadline_s=resolved_settings.default_run_deadline_s,
                    tool_env=replace(
                        base_tool_env,
                        child_agent_builder=child_agent_builder,
                        worker_build_fn=worker_build_fn,
                    ),
                    middleware_env=middleware_env,
                    memory_env=memory_env,
                    # Stream O (Mini-ADR O-14) — per-tenant MCP server allowlist.
                    mcp_allowlist_provider=make_mcp_allowlist_provider(
                        resolved_tenant_config_service
                    ),
                    # Stream Q (Mini-ADR Q-5) — chat-LLM key from platform creds
                    # when the manifest model has no api_key_ref.
                    credentials_resolver=credentials_resolver,
                    # Stream T (PR B) — build-time embedding gate. The dynamic
                    # embedder is never None, so the orchestrator's
                    # ``embedder is None`` gate can't fire; the builder checks
                    # the effective config and rejects a memory.long_term
                    # manifest when platform embedding is unconfigured.
                    platform_embedding_config_service=(resolved_platform_embedding_config_service),
                    # Stream PI-3-A2 — judges prefer the platform judge model
                    # (else fall back to each agent's own model).
                    platform_judge_config_service=resolved_platform_judge_config_service,
                    # Phase 3 — platform tool-output-budget master switch.
                    platform_tool_budget_config_service=(
                        resolved_platform_tool_budget_config_service
                    ),
                    # Stream V (Mini-ADR V-4) — tenant's own remote MCP pool.
                    tenant_mcp_pool_provider=_tenant_mcp_pool_provider,
                    # Stream MCP platform-servers (P1b) — platform shared catalog pool.
                    platform_mcp_pool_provider=_platform_mcp_pool_provider,
                    # Stream Agent-Templates (M1-3) — resolve fork extends → base
                    # template + enforce tier① security floor at build time.
                    platform_template_resolver=_platform_template_resolver,
                    # Stream MCP-OAUTH (OA-3b) — caller's per-user OAuth MCP pool.
                    user_mcp_oauth_pool_provider=_user_mcp_oauth_pool_provider,
                    # Stream X (Mini-ADR X-4) — first wiring of skill resolution
                    # into the agent build: tenant-first / platform-fallback +
                    # plan-tier gate, plus bind-activity tracking.
                    skill_store=resolved_skill_store,
                    skill_activity_recorder=skill_activity_recorder,
                    tenant_config_service=resolved_tenant_config_service,
                    # Stream SE (SE-3b) — audit for in-session skill authoring.
                    audit_logger=resolved_audit,
                    # skill-asset-store — dual-read for externalized skill files.
                    skill_asset_store=skill_asset_store,
                    # Spec 1 PR2 — backs the manage_task builtin (conversational
                    # scheduled tasks). Only the main builder — sub-agents/workers
                    # don't schedule tasks (intentional constraint).
                    trigger_store=resolved_trigger_store,
                    # 一期 Task 5 — every LLM provider client this builder
                    # constructs reuses the process-level shared connection pool.
                    http_client=shared_http,
                )
                # Stream MCP-OAUTH (OA-3b) — let get_agent decide per-user builds.
                resolved_agent_runtime.user_oauth_pool_provider = _user_mcp_oauth_pool_provider
                # Stream T (PR B) — these background workers (ingestion runner,
                # DLQ worker, consolidator) are ALWAYS started. ``embedder`` is a
                # ``DynamicResolvingEmbedder`` that resolves the live platform
                # embedding config at use-time, so it is never None; the previous
                # ``if embedder is not None`` guards were dead. The build-time
                # gate in ``make_agent_builder`` rejects a ``memory.long_term``
                # agent when embedding is unconfigured, so no memory data (hence
                # no DLQ rows) can exist without embedding configured — making it
                # safe to always start these workers. An ``embed`` call against an
                # unconfigured embedding raises ``AgentFactoryError``, which each
                # worker loop already catches and backs off on. We deliberately do
                # NOT gate on a startup config check: that would break "config
                # takes effect without a restart" (resolve-at-use-time is intended).
                #
                # Stream J.5 — the ingestion runner needs the embedder to embed
                # uploaded knowledge documents. Lease + attempt bounds match the
                # recovery worker so the fast path and the reaper share one
                # exactly-once claim contract (Stream KB durability).
                _app.state.knowledge_ingestion_runner = KnowledgeIngestionRunner(
                    store=resolved_knowledge_store,
                    embedder=embedder,
                    lease_seconds=resolved_settings.knowledge_ingest_lease_s,
                    max_attempts=resolved_settings.knowledge_ingest_max_attempts,
                )
                # Stream KB — durable crash recovery for ingestion: claims stuck
                # documents (pending / lease-expired processing) and re-drives
                # them from retained bytes.
                if resolved_settings.enable_knowledge_recovery_worker:
                    knowledge_recovery_worker = KnowledgeIngestRecoveryWorker(
                        store=resolved_knowledge_store,
                        embedder=embedder,
                        interval_s=resolved_settings.knowledge_recovery_interval_s,
                        batch_size=resolved_settings.knowledge_recovery_batch_size,
                        lease_seconds=resolved_settings.knowledge_ingest_lease_s,
                        max_attempts=resolved_settings.knowledge_ingest_max_attempts,
                    )
                    knowledge_recovery_worker.start()
                    _app.state.knowledge_recovery_worker = knowledge_recovery_worker
            if reaper is not None:
                reaper.start()
            if scheduler is not None:
                scheduler.start()
                _app.state.trigger_scheduler = scheduler
            if orphan_sweep is not None:
                orphan_sweep.start()
                _app.state.orphan_sweep = orphan_sweep
            if run_queue_worker is not None:
                run_queue_worker.start()
                _app.state.run_queue_worker = run_queue_worker
            if approval_timeout_sweep is not None:
                approval_timeout_sweep.start()
                _app.state.approval_timeout_sweep = approval_timeout_sweep
            if transcript_mirror_sweep is not None:
                transcript_mirror_sweep.start()
                _app.state.transcript_mirror_sweep = transcript_mirror_sweep
            if skill_curator is not None:
                skill_curator.start()
                _app.state.skill_curator = skill_curator
            if curation_worker is not None:
                curation_worker.start()
                _app.state.curation_worker = curation_worker
            # Stream K.K7 — the DLQ retry worker re-embeds failed writebacks
            # before re-attempting the store write. Always started (see the
            # always-on note above): the embedder is the always-present dynamic
            # embedder, and DLQ rows can only exist for a memory.long_term agent,
            # which the build-time gate already requires embedding for.
            memory_dlq_worker = MemoryDLQWorker(
                dlq=resolved_memory_dlq,
                memory_store=resolved_memory_store,
                embedder=embedder,
                interval_s=resolved_settings.memory_dlq_worker_interval_s,
            )
            memory_dlq_worker.start()
            _app.state.memory_dlq_worker = memory_dlq_worker
            # Capability Uplift Sprint #7 (Mini-ADRs U-34 / U-39) —
            # MemoryConsolidator. Started whenever the scheduler is enabled
            # (the always-present dynamic embedder embeds the consolidated
            # summary text — see the always-on note above).
            #
            # Stream O Mini-ADR O-6 — the aux model flows through the
            # production :class:`LLMRouterAuxModelAdapter`, which resolves
            # credentials per-tenant via :class:`CredentialsResolver`
            # (platform / tenant mode) at call time.
            memory_consolidator: MemoryConsolidator | None = None
            if enable_scheduler:
                default_provider = resolved_settings.memory_consolidator_default_aux_provider
                # The aux model (cluster verify / summarise / lone-item review)
                # resolves its key per-tenant via ``resolve_provider(tenant_id=…)``
                # — the same vault path the eval agent + embedder + skill-evolution
                # worker use (web-pasted platform credentials, Stream Q). So the
                # consolidator always uses the production router adapter and skips a
                # tenant whose credential is unresolvable (each cluster / lone-item
                # call is wrapped in try/except → summary.errors, bounded + logged),
                # mirroring the eval worker which has no startup gate. The previous
                # gate on the env-level ``effective_platform_provider_credentials``
                # dict was the same stale-gate bug as the skill-evolution worker
                # (#656): it fell back to the null adapter even when the aux was
                # resolvable from the vault, so a deployment that pastes its key in
                # the UI (the canonical path) silently ran the consolidator idle
                # (null adapter → zero consolidations / purges).
                aux_model: ConsolidatorAuxModel = make_llm_router_aux_model(
                    resolver=credentials_resolver,
                    secret_store=resolved_secret_store,
                    default_provider=default_provider,
                    default_model=(resolved_settings.memory_consolidator_default_aux_model),
                    secret_cache=credential_value_cache,
                )
                memory_consolidator = MemoryConsolidator(
                    memory_store=resolved_memory_store,
                    tenant_config_service=resolved_tenant_config_service,
                    audit_logger=resolved_audit,
                    aux_model=aux_model,
                    embedder=make_consolidator_embedder(embedder),
                    interval_s=float(resolved_settings.memory_consolidator_interval_s),
                    default_aux_model_name=(
                        resolved_settings.memory_consolidator_default_aux_model
                    ),
                    # Chargeback — consolidator aux spend lands in token_usage
                    # (usage_kind='memory_consolidation'), same as SE-A43 did
                    # for the evolution aux path.
                    usage_store=resolved_token_usage,
                    # Single-flight the sweep across replicas (advisory lock);
                    # None in single-process / test runs needs no lock.
                    session_factory=sql_stores.session_factory if sql_stores else None,
                )
                memory_consolidator.start()
                _app.state.memory_consolidator = memory_consolidator

            # Stream SE (SE-6d) — Layer B skill-evolution worker (gated OFF by
            # default). Built lazily so the orchestrator graph import it needs
            # stays out of the module-import path.
            # SE-7 auto-promote channel breaker, shared by the promote gate
            # (SE-7c) and the rollback monitor (SE-7d) so a promote that later
            # rolls back trips the same channel (SE-A12). In-process state.
            skill_evo_breaker = CircuitBreaker(
                failure_threshold=0.5, min_samples=5, window=timedelta(hours=24)
            )
            if resolved_settings.enable_skill_evolution_worker:
                from control_plane.skill_evolution_wiring import build_evolution_worker

                # The aux model (distil / attribute / judge) resolves its key
                # per-candidate via ``resolve_provider(tenant_id=…)`` — the same
                # vault path the eval agent + embedder use (web-pasted platform
                # credentials, Stream Q). So the worker starts whenever the
                # feature is enabled and skips a candidate whose tenant has no
                # resolvable aux credential (mirroring the eval worker, which
                # has no startup gate). The previous gate on the env-level
                # ``effective_platform_provider_credentials`` dict was stale: it
                # blocked startup even when the aux was resolvable from the
                # vault, so a deployment that pastes its key in the UI (the
                # canonical path) never ran the flywheel.
                se_provider = resolved_settings.memory_consolidator_default_aux_provider

                # SE-16 (SE-A41) — per-tenant rollout gate: the worker only
                # distils candidates of tenants that opted in via
                # ``tenant_config.skill_evolution_enabled`` (ANDed with this
                # platform switch). No config row → not enrolled.
                async def _skill_evolution_tenant_gate(tenant_id: UUID) -> bool:
                    try:
                        cfg = await resolved_tenant_config_service.get(tenant_id=tenant_id)
                    except TenantConfigNotConfiguredError:
                        return False
                    return bool(cfg.skill_evolution_enabled)

                # SE-16 (SE-A45) — implicit-candidate judge sample rate. An
                # unconfigured tenant resolves to 0 (it cannot pass the gate
                # above anyway, so the screen never admits its candidates).
                async def _skill_evolution_judge_sample_pct(tenant_id: UUID) -> int:
                    try:
                        cfg = await resolved_tenant_config_service.get(tenant_id=tenant_id)
                    except TenantConfigNotConfiguredError:
                        return 0
                    return int(cfg.skill_evolution_judge_sample_pct)

                skill_evolution_worker = build_evolution_worker(
                    aux_model=make_llm_router_aux_model(
                        resolver=credentials_resolver,
                        secret_store=resolved_secret_store,
                        default_provider=se_provider,
                        default_model=resolved_settings.memory_consolidator_default_aux_model,
                        secret_cache=credential_value_cache,
                    ),
                    aux_default_model=resolved_settings.memory_consolidator_default_aux_model,
                    tenant_gate=_skill_evolution_tenant_gate,
                    # SE-16 (SE-A45) — sampled quality screen over implicit
                    # candidates (candidate purification, not promotion).
                    judge_sample_pct=_skill_evolution_judge_sample_pct,
                    # SE-16 (SE-A43) — aux spend (distil/judge/screen) writes
                    # real token_usage rows (usage_kind='skill_evolution').
                    token_usage_store=resolved_token_usage,
                    # SE-16 (SE-A47) — ingest-time dedup: a distillate similar
                    # to an existing same-agent skill becomes its next revision.
                    embedder=make_consolidator_embedder(embedder),
                    # SE-16 (SE-A39) — 👎 trajectories (+ comments) join the
                    # contrastive failure side of distillation.
                    feedback_store=resolved_feedback,
                    candidate_store=resolved_curation_candidate_store,
                    skill_store=resolved_skill_store,
                    eval_store=resolved_eval_dataset_store,
                    agent_spec_store=resolved_repo,
                    trajectory_reader=TrajectoryReader(object_store=object_store),
                    agent_builder=resolved_agent_runtime.agent_builder,
                    interval_s=resolved_settings.skill_evolution_worker_interval_s,
                    audit_logger=resolved_audit,
                    breaker=skill_evo_breaker,
                    # Live pilot finding #8 — promotion / dedup-revision flips
                    # change the auto-attach set without a spec-version bump;
                    # the BuiltAgent cache must drop the tenant's entries.
                    cache_invalidator=resolved_agent_runtime.invalidate_tenant,
                )
                skill_evolution_worker.start()
                _app.state.skill_evolution_worker = skill_evolution_worker

            # Stream SE (SE-7d-3b-i) — regression-rollback monitor. DB-only (no
            # LLM/graph), so no provider gate; shares the auto-promote breaker.
            if resolved_settings.enable_skill_rollback_monitor:
                from control_plane.skill_rollback_gate import RollbackGate
                from control_plane.skill_rollback_monitor import RollbackMonitorConfig

                skill_rollback_monitor = RollbackMonitor(
                    skill_store=resolved_skill_store,
                    gate=RollbackGate(
                        skill_store=resolved_skill_store,
                        breaker=skill_evo_breaker,
                        audit_logger=resolved_audit,
                        # Stream HX-2 (Mini-ADR HX-B2) — user 👎 joins the
                        # rollback window as a demoted sample.
                        feedback_store=resolved_feedback,
                        # Live pilot finding #8 — archive must drop the
                        # tenant's BuiltAgent cache entries.
                        cache_invalidator=resolved_agent_runtime.invalidate_tenant,
                    ),
                    config=RollbackMonitorConfig(
                        window=timedelta(days=resolved_settings.skill_rollback_window_days)
                    ),
                    interval_s=resolved_settings.skill_rollback_monitor_interval_s,
                )
                skill_rollback_monitor.start()
                _app.state.skill_rollback_monitor = skill_rollback_monitor

            # Stream HX-2 — feedback consumer (user 👎 → memory review
            # flags). DB-only, no LLM/provider gate.
            if resolved_settings.enable_feedback_consumer:
                feedback_consumer = FeedbackConsumerWorker(
                    feedback_store=resolved_feedback,
                    thread_meta_store=resolved_threads,
                    memory_store=resolved_memory_store,
                    audit_logger=resolved_audit,
                    interval_s=resolved_settings.feedback_consumer_interval_s,
                )
                feedback_consumer.start()
                _app.state.feedback_consumer = feedback_consumer
            # HX-9 (STREAM-HX § 13) — outbound webhook delivery worker.
            if resolved_settings.enable_webhook_delivery:
                webhook_delivery_worker = WebhookDeliveryWorker(
                    delivery_store=resolved_webhook_delivery_store,
                    endpoint_store=resolved_webhook_endpoint_store,
                    secret_store=resolved_secret_store,
                    run_store=resolved_run_store,
                    approval_store=resolved_approval_store,
                    artifact_store=resolved_artifact_store,
                    thread_meta_store=resolved_threads,
                    # SE-16 PR-8 — pending skill-promote requests fan out as
                    # ``skill_promote.requested``.
                    skill_store=resolved_skill_store,
                    interval_s=resolved_settings.webhook_delivery_interval_s,
                    per_tenant_concurrency=(
                        resolved_settings.webhook_delivery_per_tenant_concurrency
                    ),
                )
                webhook_delivery_worker.start()
                _app.state.webhook_delivery_worker = webhook_delivery_worker
            # P1-S2.1d — eval platform drain worker. Gated OFF by default: it
            # needs the ``tools/eval`` harness on the path (a per-run error
            # otherwise). The enqueue API populates the queue regardless.
            if resolved_settings.enable_eval_worker:
                # 11.4/11.5 — the live suites drive a real eval agent against a
                # real model via the same AgentBuilder sessions use; the M0
                # baseline stays deterministic. DispatchEvalEngine routes by
                # suite so the worker / persistence / status machine are
                # untouched.
                live_eval_harness = LiveEvalHarness(
                    resolved_agent_runtime.agent_builder,
                    provider=resolved_settings.eval_agent_provider,
                    model_name=resolved_settings.eval_agent_model,
                )
                eval_worker = EvalWorker(
                    store=resolved_eval_run_store,
                    engine=DispatchEvalEngine(
                        {
                            M0_BASELINE_SUITE: RunBaselineEvalEngine(),
                            ADVERSARIAL_SUITE: AdversarialEvalEngine(live_eval_harness),
                            TRACE_EVAL_SUITE: TraceEvalEngine(live_eval_harness),
                        }
                    ),
                    interval_s=resolved_settings.eval_worker_interval_s,
                )
                eval_worker.start()
                _app.state.eval_worker = eval_worker
            # Stream RT-5 — production quality monitor (sampler + drift detector).
            # PR-3b (§14): the workers ALWAYS start; each cycle reads the effective
            # config from ``quality_config_service`` and no-ops when disabled
            # (``enable_quality_monitor`` deploy gate AND the UI ``enabled`` toggle,
            # default off). The judge resolves its platform credential per-tenant
            # and skips a tenant with no resolvable key.
            quality_monitor = QualityMonitorWorker(
                candidate_source=resolved_quality_candidate,
                score_store=resolved_quality_score,
                judge=QualityJudge(
                    resolver=credentials_resolver,
                    secret_store=resolved_secret_store,
                    secret_cache=credential_value_cache,
                ),
                runtime=resolved_agent_runtime,
                usage_store=resolved_token_usage,
                config=quality_config_service,
            )
            quality_monitor.start()
            _app.state.quality_monitor = quality_monitor
            # RT-5 PR-2 (RT-ADR-24/25) — drift detector; reads the quality series,
            # raises a quality.drift webhook (off the run spine) on a baseline drop.
            quality_drift = QualityDriftWorker(
                score_store=resolved_quality_score,
                alert_store=resolved_quality_drift_alert,
                endpoint_store=resolved_webhook_endpoint_store,
                delivery_store=resolved_webhook_delivery_store,
                config=quality_config_service,
                # Single-flight the cycle across replicas (advisory lock);
                # None in single-process / test runs needs no lock.
                session_factory=sql_stores.session_factory if sql_stores else None,
            )
            quality_drift.start()
            _app.state.quality_drift = quality_drift
            # Stream HX-4 (Mini-ADR HX-D2) — pending-approvals gauge.
            # Always-on: one COUNT(*) a minute, no settings knob.
            approval_gauge_worker = ApprovalGaugeWorker(approval_store=resolved_approval_store)
            approval_gauge_worker.start()
            _app.state.approval_gauge_worker = approval_gauge_worker
            # 波 1 全分支终审 Important-1 — the idle-sandbox sweep. Only for
            # the cloud backend: ``sandbox_backend="supervisor"`` already has
            # its own in-process reaper inside the supervisor service, and
            # that backend is frozen this wave. isinstance rather than a
            # settings read, same reasoning as the ``HTTPSupervisorRuntime``
            # guard above — the wiring should follow what was actually built.
            if isinstance(resolved_sandbox_runtime, AgentSandboxClient):
                sandbox_reap_worker = SandboxReapWorker(runtime=resolved_sandbox_runtime)
                sandbox_reap_worker.start()
                _app.state.sandbox_reap_worker = sandbox_reap_worker
            # sandbox-egress-age-quota Task 3 — 407/blocked egress
            # observability. Always-on like ApprovalGaugeWorker (no settings
            # knob); only when a real SQL-backed audit store exists to scan
            # (``sql_stores`` is ``None`` in memory-backend mode).
            if sql_stores is not None:
                sandbox_egress_metrics_worker = SandboxEgressMetricsWorker(
                    audit_store=resolved_egress_audit_store
                )
                sandbox_egress_metrics_worker.start()
                _app.state.sandbox_egress_metrics_worker = sandbox_egress_metrics_worker
            resolved_lifecycle.mark_ready()
            logger.info(
                "control_plane.lifespan.ready",
                extra={"service": resolved_settings.service_name, "env": resolved_settings.env},
            )
            try:
                yield
            finally:
                if memory_consolidator is not None:
                    await memory_consolidator.stop()
                if memory_dlq_worker is not None:
                    await memory_dlq_worker.stop()
                if reaper is not None:
                    await reaper.stop()
                if scheduler is not None:
                    await scheduler.stop()
                if orphan_sweep is not None:
                    await orphan_sweep.stop()
                if run_queue_worker is not None:
                    await run_queue_worker.stop()
                if approval_timeout_sweep is not None:
                    await approval_timeout_sweep.stop()
                if transcript_mirror_sweep is not None:
                    await transcript_mirror_sweep.stop()
                if skill_curator is not None:
                    await skill_curator.stop()
                if curation_worker is not None:
                    await curation_worker.stop()
                if skill_evolution_worker is not None:
                    await skill_evolution_worker.stop()
                if skill_rollback_monitor is not None:
                    await skill_rollback_monitor.stop()
                if feedback_consumer is not None:
                    await feedback_consumer.stop()
                if webhook_delivery_worker is not None:
                    await webhook_delivery_worker.stop()
                if eval_worker is not None:
                    await eval_worker.stop()
                if quality_monitor is not None:
                    await quality_monitor.stop()
                if quality_drift is not None:
                    await quality_drift.stop()
                if approval_gauge_worker is not None:
                    await approval_gauge_worker.stop()
                if sandbox_reap_worker is not None:
                    await sandbox_reap_worker.stop()
                if sandbox_egress_metrics_worker is not None:
                    await sandbox_egress_metrics_worker.stop()
                if workspace_janitor_worker is not None:
                    await workspace_janitor_worker.stop()
                # Stream HX-7 — drain the Langfuse SDK's background queue
                # before the process exits; the recording stub has no
                # shutdown, hence the duck-typed lookup.
                langfuse_shutdown = getattr(langfuse_client, "shutdown", None)
                if callable(langfuse_shutdown):
                    langfuse_shutdown()
                # Batch 4b Task 2 — best-effort shutdown of the read-only
                # Langfuse SDK instance (duck-typed lookup, same as above).
                langfuse_read_shutdown = getattr(langfuse_read_client, "shutdown", None)
                if callable(langfuse_read_shutdown):
                    langfuse_read_shutdown()
                knowledge_recovery: KnowledgeIngestRecoveryWorker | None = getattr(
                    _app.state, "knowledge_recovery_worker", None
                )
                if knowledge_recovery is not None:
                    await knowledge_recovery.stop()
                ingestion_runner: KnowledgeIngestionRunner | None = getattr(
                    _app.state, "knowledge_ingestion_runner", None
                )
                if ingestion_runner is not None:
                    await ingestion_runner.aclose()
                await resolved_lifecycle.graceful_shutdown()
                # ADR B-6: release the pool after the drain so in-flight
                # requests keep their connections until they complete.
                if sql_stores is not None:
                    await sql_stores.engine.dispose()
                logger.info("control_plane.lifespan.stopped")

    app = FastAPI(
        title="Expert Work Control Plane",
        version=_VERSION,
        lifespan=lifespan,
    )
    app.state.settings = resolved_settings
    app.state.lifecycle = resolved_lifecycle
    # ``AsyncEngine`` in ``sql`` mode, ``None`` in ``memory`` mode (ADR B-6).
    app.state.db_engine = sql_stores.engine if sql_stores else None
    # W1-PR2 Task 4 — per-tenant advisory-lock critical sections
    # (``_tenant_resource_lock.py``) read this off app.state; ``None`` in
    # ``memory`` mode degrades the lock to a no-op (single process, no
    # cross-replica race to guard against).
    app.state.session_factory = sql_stores.session_factory if sql_stores else None
    app.state.health_provider = health_provider
    app.state.rate_limiter = resolved_limiter
    app.state.mcp_probe_limiter = resolved_mcp_probe_limiter
    app.state.tenant_rate_limiter = resolved_tenant_limiter
    app.state.agent_spec_repo = resolved_repo
    app.state.thread_meta_repo = resolved_threads
    app.state.thread_message_store = resolved_thread_messages
    app.state.tenant_user_repo = resolved_tenant_users
    app.state.feedback_store = resolved_feedback
    app.state.token_usage_store = resolved_token_usage
    app.state.artifact_store = resolved_artifact_store
    app.state.user_workspace_store = resolved_user_workspace_store
    # 沙箱迁移波 3 —— None 也照设(与其余部署零行为变化对称;仅 agent_sandbox
    # 后端 + 配了 NAS 根才是非 None,见上面 resolved_workspace_quota 赋值)。
    app.state.workspace_quota_service = resolved_workspace_quota
    app.state.approval_store = resolved_approval_store
    app.state.run_store = resolved_run_store
    app.state.run_event_store = resolved_run_event_store
    app.state.trigger_store = resolved_trigger_store
    app.state.trigger_run_store = resolved_trigger_run_store
    app.state.webhook_endpoint_store = resolved_webhook_endpoint_store
    app.state.webhook_delivery_store = resolved_webhook_delivery_store
    app.state.curation_candidate_store = resolved_curation_candidate_store
    app.state.eval_dataset_store = resolved_eval_dataset_store
    app.state.eval_run_store = resolved_eval_run_store
    # Stream RT-5 (RT-ADR-26) — quality dashboard reads. Attached unconditionally
    # (independent of the ``enable_quality_monitor`` sampler gate): reading stored
    # scores / drift alerts is decoupled from whether the workers are running.
    app.state.quality_score_store = resolved_quality_score
    app.state.quality_drift_alert_store = resolved_quality_drift_alert
    app.state.quality_config_service = quality_config_service
    app.state.sandbox_egress_audit_store = resolved_egress_audit_store
    app.state.knowledge_store = resolved_knowledge_store
    app.state.image_upload_store = resolved_image_upload_store
    app.state.skill_store = resolved_skill_store
    # Skill Marketplace Phase 1 — subscribe/unsubscribe endpoints in the skills
    # router resolve this off app.state (semantic A; runtime path untouched).
    app.state.skill_subscription_store = resolved_tenant_skill_subscription_store
    # Stream T (PR B) — PR C's write endpoint resolves the config service off
    # app.state to upsert the row + invalidate the cache for immediate effect.
    app.state.platform_embedding_config_service = resolved_platform_embedding_config_service
    app.state.platform_judge_config_service = resolved_platform_judge_config_service
    app.state.platform_tool_budget_config_service = resolved_platform_tool_budget_config_service
    app.state.platform_delegation_config_service = resolved_platform_delegation_config_service
    app.state.platform_dynamic_worker_config_service = (
        resolved_platform_dynamic_worker_config_service
    )
    app.state.platform_billing_config_store = resolved_platform_billing_config_store
    # Capability Uplift Sprint #4 — exposed on app.state for callers that
    # need it directly. Stream X (Mini-ADR X-4) wires it through
    # ``make_agent_builder`` / ``make_child_agent_builder`` into
    # ``_load_skills`` + ``SkillViewTool`` for bind / view activity tracking.
    app.state.skill_activity_recorder = skill_activity_recorder
    # Stream J.6 — the object store is created in the lifespan (it goes on
    # the AsyncExitStack); the upload endpoint reads it from app.state.
    app.state.object_store = None
    # The ingestion runner is built in the lifespan (it needs the resolved
    # embedder); tests inject one. ``None`` → document upload returns 503.
    app.state.knowledge_ingestion_runner = knowledge_ingestion_runner
    # The retrieval-test endpoint reuses the same retriever built in the
    # lifespan (so it mirrors production retrieval exactly); ``None`` → the
    # endpoint returns 503. Tests inject one.
    app.state.knowledge_retriever = None
    # The ingestion recovery worker is built + started in the lifespan; this is
    # the pre-lifespan default so ``getattr`` lookups are well-defined.
    app.state.knowledge_recovery_worker = None
    app.state.sandbox_runtime = resolved_sandbox_runtime
    app.state.workspace_store = resolved_workspace_store
    app.state.audit_logger = resolved_audit
    app.state.manifest_loader = resolved_loader
    app.state.jwt_verifier = resolved_verifier
    app.state.mtls_verifier = resolved_mtls
    app.state.service_account_repo = resolved_service_accounts
    app.state.api_key_repo = resolved_api_keys
    app.state.role_binding_repo = resolved_role_bindings
    app.state.api_key_verifier = resolved_api_key_verifier
    app.state.tenant_quota_repo = resolved_tenant_quotas
    app.state.token_reservation_repo = resolved_reservations
    app.state.quota_service = resolved_quota
    app.state.quota_reaper = reaper
    app.state.tenant_config_repo = resolved_tenant_config_repo
    app.state.tenant_config_service = resolved_tenant_config_service
    app.state.tenant_status_service = resolved_tenant_status_service
    # Stream RT-4 (RT-ADR-16) — agent kill switch store + TTL cache.
    app.state.agent_disable_repo = resolved_agent_disable_repo
    app.state.agent_disable_service = resolved_agent_disable_service
    # Stream R — member onboarding roster + Keycloak Admin client.
    app.state.tenant_member_repo = resolved_tenant_member_repo
    app.state.keycloak_admin_client = resolved_keycloak_admin_client
    # Stream V — tenant-registered remote MCP server registry + per-tenant
    # remote pool (V-D). The pool service is built in the lifespan (needs the
    # AsyncExitStack for shutdown); ``None`` until the lifespan sets it.
    app.state.tenant_mcp_server_store = resolved_tenant_mcp_server_store
    app.state.mcp_connector_catalog_store = resolved_mcp_connector_catalog_store
    app.state.platform_agent_template_store = resolved_platform_agent_template_store
    app.state.agent_instance_store = resolved_agent_instance_store
    app.state.mcp_oauth_connection_store = resolved_mcp_oauth_connection_store
    app.state.model_rate_card_store = resolved_model_rate_card_store
    app.state.tenant_billing_ledger_store = resolved_tenant_billing_ledger_store
    app.state.tenant_mcp_pool_service = None
    app.state.platform_mcp_pool_service = None
    app.state.platform_secret_store = resolved_platform_secret_store
    app.state.platform_secrets_service = resolved_platform_secrets_service
    # Stream S (Mini-ADR S-4) — model catalog reads only usable providers
    # (configured + enabled) via the same service; thin adapter wraps it.
    app.state.model_catalog_providers = PlatformConfiguredProviders(
        resolved_platform_secrets_service
    )
    # Stream Q (PR C) — the SecretStore is exposed so the platform-config write
    # path can encrypt a pasted raw key via ``secret_store.put`` before storing
    # only the ``secret://`` ref in the catalog.
    app.state.secret_store = resolved_secret_store
    app.state.agent_runtime = resolved_agent_runtime
    # Stream K.K6 — memory CRUD endpoints. ``memory_repo`` is the store
    # already resolved above (SQL when ``store_backend == "sql"``, else
    # InMemory). In normal operation the lifespan sets ``app.state.embedder``
    # to the always-present ``DynamicResolvingEmbedder``; the PATCH path catches
    # the embedding-unconfigured ``AgentFactoryError`` at call time and returns a
    # 503. GET / DELETE never touch the embedder. The ``None`` fallback below
    # only applies when a runtime is injected (tests) and the lifespan wiring
    # block above was skipped.
    app.state.memory_repo = resolved_memory_store
    # Phase 3a (purge_user) — expose the writeback DLQ so the cascade purge can
    # drop a purged user's pending memory writebacks (they carry extracted PII).
    app.state.memory_writeback_dlq = resolved_memory_dlq
    if not hasattr(app.state, "embedder"):
        app.state.embedder = None

    # Starlette wraps middleware in *reverse* registration order: the
    # last call to ``add_middleware`` becomes the outermost layer. We
    # therefore register innermost first.
    #
    # Effective execution order (outermost → innermost):
    #   1. ObservabilityMiddleware  — open span, record timing
    #   1b. BackpressureMiddleware  — overload guard, 503 shed (16.3)
    #   2. AuthMiddleware           — verify JWT → request.state.principal (C.1)
    #   3. TenantRateLimitMiddleware — per-tenant bucket (C.6)
    #   4. RLSContextMiddleware     — project principal.tenant_id → RLS ctxvar (C.4)
    #   5. AuditContextMiddleware   — project principal.tenant_id → log ctxvar
    #   6. RateLimitMiddleware      — per-IP / per-API-key bucket (B.2)
    #   7. CancellationMiddleware   — mint CancelToken + disconnect poll
    #   8. DeadlineMiddleware       — consume CancelToken + seed
    #                                 DeadlineContext from header
    #   9. InFlightMiddleware       — Lifecycle.track_in_flight (drain)
    app.add_middleware(InFlightMiddleware, lifecycle=resolved_lifecycle)
    app.add_middleware(DeadlineMiddleware)
    app.add_middleware(
        CancellationMiddleware,
        poll_interval_s=resolved_settings.cancellation_poll_interval_s,
    )
    app.add_middleware(
        RateLimitMiddleware,
        limiter=resolved_limiter,
        enabled=resolved_settings.rate_limit_enabled,
        hmac_salt=resolved_settings.apikey_rate_limit_hmac_salt,
    )
    app.add_middleware(
        AuditContextMiddleware,
        default_tenant_id=resolved_settings.default_dev_tenant_id,
        default_actor_id=resolved_settings.default_dev_actor_id,
    )
    app.add_middleware(RLSContextMiddleware)
    app.add_middleware(
        TenantRateLimitMiddleware,
        limiter=resolved_tenant_limiter,
        audit_logger=resolved_audit,
        enabled=resolved_settings.tenant_rate_limit_enabled,
        exempt_path_prefixes=tuple(resolved_settings.auth_exempt_path_prefixes),
        audit_sample_every=resolved_settings.tenant_rate_limit_audit_sample_every,
        # Stream C.6 — per-tenant rate_limit_override (cached, own-tenant RLS read).
        tenant_config_store=resolved_tenant_config_repo,
    )
    app.add_middleware(
        AuthMiddleware,
        verifier=resolved_verifier,
        exempt_path_prefixes=tuple(resolved_settings.auth_exempt_path_prefixes),
        audit_logger=resolved_audit,
        mtls_verifier=resolved_mtls,
        mtls_header_name=resolved_settings.mtls_xfcc_header_name,
        api_key_verifier=resolved_api_key_verifier,
        # Stream N — AuthMiddleware queries this store to populate
        # ``Principal.is_system_admin`` from a platform-scope role binding.
        role_binding_store=resolved_role_bindings,
        # Stream U (PR E) — 403 a suspended tenant's members (never system_admin).
        tenant_status=resolved_tenant_status_service,
        # Stream ACCT — first-login auto-grant of the first platform admin.
        bootstrap_admin_email=resolved_settings.bootstrap_admin_email,
    )
    # Stream 16.3 — overload guard. Outside auth so a shed request never reaches
    # JWT verify / DB; inside Observability so sheds are still traced + timed.
    app.add_middleware(
        BackpressureMiddleware,
        lifecycle=resolved_lifecycle,
        max_in_flight=resolved_settings.backpressure_max_in_flight,
        retry_after_s=resolved_settings.backpressure_retry_after_s,
        exempt_path_prefixes=tuple(resolved_settings.auth_exempt_path_prefixes),
    )
    app.add_middleware(ObservabilityMiddleware)

    app.include_router(build_health_router(health_provider))
    app.include_router(build_metrics_router())
    app.include_router(build_agent_schema_router())
    app.include_router(build_model_catalog_router())
    app.include_router(build_agents_router())
    app.include_router(build_sessions_router())
    app.include_router(build_runs_router())
    app.include_router(build_plan_router())
    app.include_router(build_runs_list_router())
    app.include_router(build_conversations_router())
    app.include_router(build_agent_users_router())
    app.include_router(build_tenant_users_router())
    # Stream HX-7 — the cross-run approval queue + batch decide.
    app.include_router(build_approvals_router())
    app.include_router(build_feedback_router())
    app.include_router(build_artifacts_router())
    app.include_router(build_workspace_router())
    app.include_router(build_knowledge_router())
    app.include_router(build_me_router())
    app.include_router(build_memory_router())
    app.include_router(build_skills_router())
    app.include_router(build_skill_evolution_router())
    app.include_router(build_uploads_router())
    app.include_router(build_service_accounts_router())
    app.include_router(build_mcp_servers_router())
    app.include_router(build_mcp_catalog_router())
    app.include_router(build_agent_templates_router())
    app.include_router(build_mcp_oauth_router())
    app.include_router(build_rate_card_router())
    app.include_router(build_usage_router())
    app.include_router(build_billing_admin_router())
    app.include_router(build_platform_skills_router())
    app.include_router(build_api_keys_router())
    app.include_router(build_role_bindings_router())
    app.include_router(build_quota_router())
    app.include_router(build_tenants_router())
    app.include_router(build_setup_router())
    app.include_router(build_members_router())
    app.include_router(build_sandboxes_router())
    app.include_router(build_platform_config_router())
    app.include_router(build_platform_embedding_config_router())
    app.include_router(build_platform_judge_config_router())
    app.include_router(build_platform_tool_budget_config_router())
    app.include_router(build_platform_delegation_config_router())
    app.include_router(build_platform_dynamic_worker_config_router())
    app.include_router(build_platform_billing_config_router())
    app.include_router(build_tenant_quotas_router())
    app.include_router(build_tenant_config_router())
    app.include_router(build_triggers_router())
    app.include_router(build_webhooks_router())
    app.include_router(build_webhook_endpoints_router())
    app.include_router(build_audit_router())
    app.include_router(build_sandbox_egress_audit_router())
    app.include_router(build_curation_router())
    app.include_router(build_eval_dataset_router())
    app.include_router(build_eval_runs_router())
    app.include_router(build_quality_router())
    app.include_router(build_platform_quality_config_router())

    return app


@dataclass(frozen=True)
class _SqlStores:
    """Postgres-backed store bundle built when ``store_backend == "sql"``.

    Every store shares the one ``engine`` + RLS-wrapped sessionmaker;
    ``engine`` is disposed in the app's ``lifespan`` (ADR B-6).
    """

    engine: AsyncEngine
    #: Stream Q — exposed so the app can build a SqlEncryptedSecretStore over
    #: the same RLS-wrapped sessionmaker when ``secret_store_backend`` is set.
    session_factory: async_sessionmaker[AsyncSession]
    agent_spec: AgentSpecStore
    thread_meta: ThreadMetaStore
    thread_message: ThreadMessageStore  # Conversation IA M4 — content-search mirror
    tenant_user: TenantUserStore
    memory: MemoryStore
    memory_dlq: MemoryWritebackDLQ  # Stream K.K7
    knowledge: KnowledgeStore
    skill: SkillStore
    image_upload: ImageUploadStore  # Stream J.6.补强-3 (Mini-ADR J-32)
    artifact: ArtifactStore
    approval: ApprovalStore  # Stream J.8 (Mini-ADR J-24)
    run: RunStore  # Stream J.8 closeout follow-up (Mini-ADR J-41)
    run_event: RunEventStore  # Stream H.3 PR 3 (Mini-ADR H-7)
    trigger: TriggerStore  # Stream J.10 (Mini-ADR J-26 / J-42)
    trigger_run: TriggerRunStore  # Stream J.10
    webhook_endpoint: WebhookEndpointStore  # HX-9 (STREAM-HX § 13)
    webhook_delivery: WebhookDeliveryStore  # HX-9
    curation_candidate: CurationCandidateStore  # Stream J.12 (Mini-ADR J-43)
    eval_dataset: EvalDatasetStore  # Stream J.12 (Mini-ADR J-43)
    service_account: ServiceAccountStore
    api_key: ApiKeyStore
    role_binding: RoleBindingStore
    platform_secret: PlatformSecretStore
    platform_embedding_config: PlatformEmbeddingConfigStore  # Stream T (PR B)
    platform_judge_config: PlatformJudgeConfigStore  # Stream PI-3-A1
    platform_tool_budget_config: PlatformToolBudgetConfigStore  # Phase 3
    platform_delegation_config: PlatformDelegationConfigStore  # perf phase2 PR3
    platform_dynamic_worker_config: PlatformDynamicWorkerConfigStore  # B3 PR2
    platform_billing_config: PlatformBillingConfigStore  # Stream 12.4
    tenant_quota: TenantQuotaStore
    token_reservation: TokenReservationStore
    tenant_config: TenantConfigStore
    agent_disable: AgentDisableStore  # Stream RT-4 (RT-ADR-16) — agent kill switch
    quality_score: QualityScoreStore  # Stream RT-5 (RT-ADR-24) — quality time-series
    quality_candidate: QualityCandidateSource  # Stream RT-5 (RT-ADR-22) — sampling feed
    quality_drift_alert: QualityDriftAlertStore  # Stream RT-5 (RT-ADR-24) — drift alerts
    platform_quality_config: PlatformQualityConfigStore  # Stream RT-5 (PR-3b) — quality config
    tenant_member: TenantMemberStore  # Stream R
    tenant_mcp_server: TenantMcpServerStore  # Stream V
    tenant_skill_subscription: TenantSkillSubscriptionStore  # Skill Marketplace
    mcp_connector_catalog: McpConnectorCatalogStore  # Stream W
    platform_agent_template: PlatformAgentTemplateStore  # Stream Agent-Templates
    agent_instance: AgentInstanceStore  # Stream Agent-Templates (M1-5b)
    mcp_oauth_connection: McpOAuthConnectionStore  # Stream MCP-OAUTH
    model_rate_card: ModelRateCardStore  # Stream Y (Y-3)
    tenant_billing_ledger: TenantBillingLedgerStore  # Stream Y (Y-4)
    feedback: FeedbackStore
    token_usage: TokenUsageStore
    audit_log: AuditLogStore
    user_workspace: UserWorkspaceStore  # Stream Playground-Uplift (D4 inspector)


def _validate_platform_catalog(settings: Settings) -> None:
    """Stream O (Mini-ADR O-1) — Platform Catalog startup validation.

    Three checks, fail-fast on any:
    1. Every entry in ``supported_providers`` has a matching
       ``platform_provider_credentials`` entry.
    2. Every entry in ``supported_tools`` has a matching
       ``platform_tool_credentials`` entry.
    3. No extraneous credential entries (would point at a typo in env).

    Pydantic already validates that the Literal values are in the
    catalog; this checks the cross-field consistency.
    """
    supported_provs = set(settings.supported_providers)
    cred_provs = set(settings.platform_provider_credentials)
    missing_provs = supported_provs - cred_provs
    extra_provs = cred_provs - supported_provs
    supported_tools = set(settings.supported_tools)
    cred_tools = set(settings.platform_tool_credentials)
    missing_tools = supported_tools - cred_tools
    extra_tools = cred_tools - supported_tools
    if missing_provs or extra_provs or missing_tools or extra_tools:
        details = []
        if missing_provs:
            details.append(
                f"providers in supported_providers without credentials: {sorted(missing_provs)}"
            )
        if extra_provs:
            details.append(
                f"platform_provider_credentials with providers not in "
                f"supported_providers: {sorted(extra_provs)}"
            )
        if missing_tools:
            details.append(f"tools in supported_tools without credentials: {sorted(missing_tools)}")
        if extra_tools:
            details.append(
                f"platform_tool_credentials with tools not in supported_tools: "
                f"{sorted(extra_tools)}"
            )
        msg = "Platform Catalog mismatch — " + "; ".join(details)
        raise RuntimeError(msg)


def _signal_legacy_credentials_derivation(settings: Settings) -> None:
    """Stream O (Mini-ADR O-10) — emit a deprecation signal when the
    effective credentials catalog is gap-filled from a legacy
    ``*_api_key_ref`` field. A deployment that has fully migrated to
    ``platform_provider_credentials`` / ``platform_tool_credentials``
    triggers nothing here; the P3 ``LegacyCredentialsFallbackPresent``
    alert (Mini-ADR O-8) flags any that remain."""
    explicit_provs = set(settings.platform_provider_credentials)
    if settings.embedding_api_key_ref and settings.embedding_provider not in explicit_provs:
        logger.warning(
            "credentials.legacy_derivation role=embedding provider=%s — migrate to "
            "platform_provider_credentials",
            settings.embedding_provider,
        )
        record_legacy_credentials_fallback(role="embedding")
    if (
        settings.rerank_api_key_ref
        and settings.rerank_provider not in explicit_provs
        and settings.rerank_provider in PROVIDER_CATALOG
    ):
        logger.warning(
            "credentials.legacy_derivation role=rerank provider=%s — migrate to "
            "platform_provider_credentials",
            settings.rerank_provider,
        )
        record_legacy_credentials_fallback(role="rerank")


async def _seed_mcp_catalog(settings: Settings, store: McpConnectorCatalogStore) -> None:
    """Env-seed the connector catalog from ``settings.mcp_catalog_seed_file``
    (Stream MCP-OAUTH OA-5). No-op when unset. A malformed template fails fast."""
    if not settings.mcp_catalog_seed_file:
        return
    path = Path(settings.mcp_catalog_seed_file)
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"mcp_catalog_seed_file not readable: {path}") from exc
    try:
        entries, skipped = load_catalog_seed(raw, os.environ)
    except CatalogSeedError as exc:
        raise RuntimeError(f"invalid mcp_catalog_seed_file {path}: {exc}") from exc
    created, existing = await seed_catalog(store=store, entries=entries)
    logger.info(
        "control_plane.mcp_catalog.seeded created=%s existing=%s skipped_unset=%s",
        created,
        existing,
        skipped,
    )


def _build_secret_store(settings: Settings, sql_stores: _SqlStores | None) -> SecretStore:
    """Build the SecretStore for ``settings.secret_store_backend`` (Stream Q).

    ``sql_encrypted`` needs a Postgres session + a 32-byte KEK; it fails loud
    at boot if either is missing rather than silently degrading. Other backends
    delegate to :func:`make_secret_store`.
    """
    backend = settings.secret_store_backend
    if backend == "sql_encrypted":
        if sql_stores is None:
            msg = (
                "secret_store_backend='sql_encrypted' requires store_backend='sql' "
                "(it needs a Postgres session) — got in-memory mode"
            )
            raise RuntimeError(msg)
        if settings.secret_encryption_key is None:
            msg = (
                "secret_store_backend='sql_encrypted' requires "
                "EXPERT_WORK_SECRET_ENCRYPTION_KEY (base64 32-byte KEK)"
            )
            raise RuntimeError(msg)
        kek = build_kek_from_b64(settings.secret_encryption_key.get_secret_value())
        return SqlEncryptedSecretStore(sql_stores.session_factory, kek=kek)
    return make_secret_store(backend, env_file=settings.secret_store_env_file)


def _build_keycloak_admin_client(
    settings: Settings, secret_store: SecretStore
) -> KeycloakAdminClient:
    """Build the Keycloak Admin client for member provisioning (Stream R).

    Returns a :class:`FakeKeycloakAdminClient` unless ``keycloak_enabled`` so
    dev/CI run the full onboarding flow without a live Keycloak. When enabled,
    the service-account client secret is loaded lazily from the vault on each
    token refresh (never held in settings).
    """
    if not settings.keycloak_enabled:
        return FakeKeycloakAdminClient()

    http = httpx.AsyncClient(timeout=httpx.Timeout(10.0, read=30.0))

    async def _load_secret() -> str:
        return await secret_store.get(settings.keycloak_admin_secret_name)

    token_provider = ServiceAccountTokenProvider(
        base_url=settings.keycloak_base_url,
        realm=settings.keycloak_realm,
        client_id=settings.keycloak_admin_client_id,
        secret_loader=_load_secret,
        http=http,
    )
    return HttpKeycloakAdminClient(
        base_url=settings.keycloak_base_url,
        realm=settings.keycloak_realm,
        token_provider=token_provider,
        http=http,
    )


def _build_sql_stores(settings: Settings) -> _SqlStores:
    """Build the Postgres-backed store bundle from ``settings.db_*`` (ADR B-6).

    One engine, one ``build_rls_sessionmaker``-wrapped sessionmaker,
    shared by every store. ``create_async_engine`` is lazy — no
    connection opens here; the engine is disposed in ``lifespan``.
    """
    engine = create_async_engine_from_config(
        DatabaseConfig(
            dsn=settings.db_dsn,
            pgbouncer_mode=settings.db_pgbouncer_mode,
            echo_sql=settings.db_echo,
        )
    )
    session_factory = build_rls_sessionmaker(create_async_session_factory(engine))
    return _SqlStores(
        engine=engine,
        session_factory=session_factory,
        agent_spec=SqlAgentSpecStore(session_factory),
        thread_meta=SqlThreadMetaStore(session_factory),
        thread_message=SqlThreadMessageStore(session_factory),
        tenant_user=SqlTenantUserStore(session_factory),
        memory=SqlMemoryStore(session_factory),
        memory_dlq=SqlMemoryWritebackDLQ(session_factory),
        knowledge=SqlKnowledgeStore(session_factory),
        skill=SqlSkillStore(session_factory),
        image_upload=SqlImageUploadStore(session_factory),
        artifact=SqlArtifactStore(session_factory),
        approval=SqlApprovalStore(session_factory),
        run=SqlRunStore(session_factory),
        run_event=SqlRunEventStore(session_factory),
        trigger=SqlTriggerStore(session_factory),
        trigger_run=SqlTriggerRunStore(session_factory),
        webhook_endpoint=SqlWebhookEndpointStore(session_factory),
        webhook_delivery=SqlWebhookDeliveryStore(session_factory),
        curation_candidate=SqlCurationCandidateStore(session_factory),
        eval_dataset=SqlEvalDatasetStore(session_factory),
        service_account=SqlServiceAccountStore(session_factory),
        api_key=SqlApiKeyStore(session_factory),
        role_binding=SqlRoleBindingStore(session_factory),
        tenant_quota=SqlTenantQuotaStore(session_factory),
        token_reservation=SqlTokenReservationStore(session_factory),
        platform_secret=SqlPlatformSecretStore(session_factory),
        platform_embedding_config=SqlPlatformEmbeddingConfigStore(session_factory),
        platform_judge_config=SqlPlatformJudgeConfigStore(session_factory),
        platform_tool_budget_config=SqlPlatformToolBudgetConfigStore(session_factory),
        platform_delegation_config=SqlPlatformDelegationConfigStore(session_factory),
        platform_dynamic_worker_config=SqlPlatformDynamicWorkerConfigStore(session_factory),
        platform_billing_config=SqlPlatformBillingConfigStore(session_factory),
        tenant_config=SqlTenantConfigStore(session_factory),
        agent_disable=SqlAgentDisableStore(session_factory),
        quality_score=SqlQualityScoreStore(session_factory),
        quality_candidate=SqlQualityCandidateSource(session_factory),
        quality_drift_alert=SqlQualityDriftAlertStore(session_factory),
        platform_quality_config=SqlPlatformQualityConfigStore(session_factory),
        tenant_member=SqlTenantMemberStore(session_factory),
        tenant_mcp_server=SqlTenantMcpServerStore(session_factory),
        tenant_skill_subscription=SqlTenantSkillSubscriptionStore(session_factory),
        mcp_connector_catalog=SqlMcpConnectorCatalogStore(session_factory),
        platform_agent_template=SqlPlatformAgentTemplateStore(session_factory),
        agent_instance=SqlAgentInstanceStore(session_factory),
        mcp_oauth_connection=SqlMcpOAuthConnectionStore(session_factory),
        model_rate_card=DbModelRateCardStore(session_factory),
        tenant_billing_ledger=DbTenantBillingLedgerStore(session_factory),
        feedback=DbFeedbackStore(session_factory),
        token_usage=DbTokenUsageStore(session_factory),
        audit_log=SqlAuditLogStore(session_factory),
        user_workspace=SqlUserWorkspaceStore(session_factory),
    )


def _build_default_jwt_verifier(settings: Settings) -> JWTVerifier:
    """Construct a Keycloak-backed JWT verifier from settings (C.1)."""
    provider = HTTPJWKSProvider(
        settings.resolve_jwks_uri(),
        cache_ttl_s=float(settings.oidc_jwks_cache_ttl_s),
    )
    return JWTVerifier(
        jwks_provider=provider,
        issuer=settings.oidc_issuer,
        audience=settings.oidc_audience,
        leeway_s=settings.oidc_jwt_leeway_s,
    )


def _build_default_mtls_verifier(settings: Settings) -> MTLSVerifier | None:
    """Construct an mTLS verifier when the feature is enabled (C.2)."""
    if not settings.mtls_enabled:
        return None
    return build_mtls_verifier(
        allowed_subjects=settings.mtls_allowed_service_subjects,
        system_tenant_id=settings.mtls_system_tenant_id,
        require_uri_san=settings.mtls_require_uri_san,
    )


def _build_default_gateway_limiter(settings: Settings) -> RateLimiter:
    """Pick the gateway-tier limiter (B.2) impl based on Settings.

    Single-instance dev / unit tests stay on the in-process bucket so
    they don't need a Redis container. Multi-replica deploys
    (``single_instance=False``) plus a configured Redis URL get the
    Lua-backed implementation that survives horizontal scale-out.
    """
    if not settings.single_instance and settings.quota_redis_url:
        import redis.asyncio as redis_async

        client = redis_async.from_url(
            settings.quota_redis_url,
            encoding="utf-8",
            decode_responses=True,
        )
        return RedisTokenBucketLimiter(
            redis_client=client,
            capacity=settings.rate_limit_burst,
            refill_per_sec=settings.rate_limit_per_second,
        )
    return InProcessTokenBucketLimiter(
        capacity=settings.rate_limit_burst,
        refill_per_sec=settings.rate_limit_per_second,
    )


def _build_default_tenant_limiter(settings: Settings) -> RateLimiter:
    """Pick the tenant-tier limiter (C.6) impl based on Settings."""
    if not settings.single_instance and settings.quota_redis_url:
        import redis.asyncio as redis_async

        client = redis_async.from_url(
            settings.quota_redis_url,
            encoding="utf-8",
            decode_responses=True,
        )
        return RedisTokenBucketLimiter(
            redis_client=client,
            capacity=settings.tenant_rate_limit_capacity,
            refill_per_sec=settings.tenant_rate_limit_refill_per_sec,
        )
    return InProcessTokenBucketLimiter(
        capacity=settings.tenant_rate_limit_capacity,
        refill_per_sec=settings.tenant_rate_limit_refill_per_sec,
    )


def _build_mcp_probe_limiter(settings: Settings) -> RateLimiter:
    """Tight per-tenant limiter for MCP probe-bearing endpoints (audit #6).

    Mirrors the tenant-tier impl selection (Redis multi-replica / in-process
    single-replica) but with a much smaller bucket — these endpoints each open
    a server-side outbound connection to a tenant-chosen URL.
    """
    if not settings.single_instance and settings.quota_redis_url:
        import redis.asyncio as redis_async

        client = redis_async.from_url(
            settings.quota_redis_url,
            encoding="utf-8",
            decode_responses=True,
        )
        return RedisTokenBucketLimiter(
            redis_client=client,
            capacity=settings.mcp_probe_rate_limit_capacity,
            refill_per_sec=settings.mcp_probe_rate_limit_refill_per_sec,
        )
    return InProcessTokenBucketLimiter(
        capacity=settings.mcp_probe_rate_limit_capacity,
        refill_per_sec=settings.mcp_probe_rate_limit_refill_per_sec,
    )


def _build_default_quota_service(
    *,
    settings: Settings,
    quota_store: TenantQuotaStore,
    reservation_store: TokenReservationStore,
) -> QuotaService:
    """Pick the quota implementation based on Settings.quota_redis_url.

    Tests + dev (no redis URL) get the InMemoryQuotaService. Prod
    points ``EXPERT_WORK_QUOTA_REDIS_URL`` at the deployed Redis and
    we wire the Lua-backed implementation.
    """
    if settings.quota_redis_url:
        # Local import keeps redis-py off the import path for tests
        # that never touch this branch.
        import redis.asyncio as redis_async

        client = redis_async.from_url(
            settings.quota_redis_url,
            encoding="utf-8",
            decode_responses=True,
        )
        return RedisQuotaService(
            redis_client=client,
            quota_store=quota_store,
            reservation_store=reservation_store,
            default_qps_limit=settings.quota_default_qps_limit,
            default_qps_burst=settings.quota_default_qps_burst,
        )
    return InMemoryQuotaService(
        quota_store=quota_store,
        reservation_store=reservation_store,
        default_qps_limit=settings.quota_default_qps_limit,
        default_qps_burst=settings.quota_default_qps_burst,
    )
