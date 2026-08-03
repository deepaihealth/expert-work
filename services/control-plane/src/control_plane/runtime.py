"""In-process agent execution runtime — control-plane ↔ orchestrator glue.

The control-plane runs the orchestrator as a library (in-process
monolith, STREAM-E-DESIGN § 2.6): an agent graph executes as a
background ``asyncio.Task`` in this process, streaming events to the
SSE client through a :class:`StreamBridge`.

:class:`AgentRuntime` bundles the three long-lived pieces a run needs —
the run-lifecycle registry, the SSE event bridge, and the
manifest→agent build path — behind one object held on ``app.state``.
"""

from __future__ import annotations

import json
import logging
import time
from collections import OrderedDict
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal, Protocol, cast, runtime_checkable
from uuid import UUID

import httpx
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from opentelemetry import trace

from control_plane.credential_value_cache import CredentialValueCache
from control_plane.platform_delegation_config import PlatformDelegationConfigService
from control_plane.platform_dynamic_worker_config import PlatformDynamicWorkerConfigService
from control_plane.platform_embedding_config import PlatformEmbeddingConfigService
from control_plane.platform_judge_config import PlatformJudgeConfigService
from control_plane.platform_mcp_pool import PlatformMcpPoolProvider
from control_plane.platform_tool_budget_config import PlatformToolBudgetConfigService
from control_plane.tenancy import TenantConfigNotConfiguredError, TenantConfigService
from control_plane.tenant_mcp_pool import TenantMcpPoolProvider
from control_plane.tenant_scope import bypass_rls_session
from control_plane.user_mcp_oauth_pool import UserMcpOAuthPoolProvider
from expert_work.common.credentials import CredentialsResolver, CredentialsResolverError
from expert_work.common.skill_activity import SkillActivityRecorder
from expert_work.common.skill_run_usage import SkillRunUsageRecorder
from expert_work.common.uplift_metrics import set_built_agent_cache_entries
from expert_work.common.url_validation import validate_remote_url
from expert_work.persistence import ArtifactStore, KnowledgeStore
from expert_work.persistence.skill import SkillStore
from expert_work.persistence.token_usage_store import TokenUsageStore
from expert_work.persistence.trigger import TriggerStore
from expert_work.protocol import (
    AgentSpec,
    ModelSpec,
    Provider,
    SkillStatus,
    TenantPlan,
    parse_extends_ref,
    resolve_extends,
    tier_satisfies,
)
from expert_work.runtime.audit import DefaultSecretRedactor
from expert_work.runtime.audit.logger import AuditLogger
from expert_work.runtime.llm import InMemoryRedisCache, LLMResponseCache
from expert_work.runtime.middleware import LangfuseClient, RecordingLangfuseClient
from expert_work.runtime.runs import RunEventStore, RunManager, RunStore
from expert_work.runtime.secret_store import SecretStore, parse_secret_ref
from expert_work.runtime.skill_assets import ObjectStore as SkillAssetStore
from expert_work.runtime.storage import ObjectStore, ObjectStoreBackend, S3CompatibleConfig
from expert_work.runtime.stream_bridge import InMemoryStreamBridge, StreamBridge
from orchestrator import (
    ActionJudge,
    BuiltAgent,
    LLMActionJudge,
    LLMCaller,
    LLMOutputJudge,
    MemoryEnv,
    MiddlewareEnv,
    OutputJudge,
    ToolEnv,
    build_agent,
    build_llm_router,
)
from orchestrator.agent_factory import (
    ProviderKeyResolver,
    SkillResolver,
    SubagentSpecResolver,
    _SkillLookupResult,
    detect_subagent_cycle,
)
from orchestrator.errors import AgentFactoryError
from orchestrator.llm import (
    DashScopeReranker,
    Embedder,
    HTTPDashScopeRerankClient,
    HTTPEmbeddingClient,
    OpenAICompatibleEmbedder,
)
from orchestrator.multimodal import (
    CachingImageResolver,
    ImageResolver,
    ObjectStoreImageResolver,
)
from orchestrator.tools import (
    AllowlistProvider,
    DenylistProvider,
    HTTPSupervisorRuntime,
    KnowledgeRetriever,
    LLMReranker,
    MCPClient,
    MCPServerConfig,
    MCPServerPool,
    NullWorkspaceLock,
    Reranker,
    SandboxRuntime,
    SearXNGClient,
    SseMCPClient,
    StdioMCPClient,
    StreamableHttpMCPClient,
    SupervisorWorkspaceStore,
    TavilyClient,
    WorkspaceLock,
    WorkspaceStore,
)
from orchestrator.trajectory.recorder import TrajectoryRecorder


#: Builds a runnable agent from a manifest. The production builder
#: closes over a SecretStore + checkpointer and calls
#: :func:`orchestrator.build_agent`; integration tests substitute a
#: stub returning a :class:`BuiltAgent` over a fake-LLM graph — the
#: real builder wires HTTP provider clients, which a test must not hit.
class AgentBuilder(Protocol):
    """Builds a runnable agent from a manifest.

    Stream O (Mini-ADR O-14) — ``tenant_id`` lets the builder construct a
    per-tenant ``ToolEnv`` (today: the MCP server allowlist; ``None`` keeps
    the platform default). Optional + defaulted so test stubs that ignore
    tenancy still conform."""

    async def __call__(
        self,
        spec: AgentSpec,
        *,
        tenant_id: UUID | None = None,
        user_id: str | None = None,
        token_usage_kind: str = "conversation",  # noqa: S107 — usage label, not a secret
    ) -> BuiltAgent:
        """Build the agent for ``spec``; ``tenant_id`` selects the per-tenant ToolEnv,
        ``user_id`` (= ``principal.subject_id``) the per-user OAuth MCP pool (OA-3b).
        ``token_usage_kind`` (SE-A43) labels this build's LLM spend in
        ``token_usage`` — the evolution replay path passes ``skill_evolution``."""


logger = logging.getLogger(__name__)

# Built-agent cache key. 3-tuple for the shared (no-OAuth) build; 4-tuple
# ``(tenant, name, version, user_id)`` when the caller has connected OAuth
# connectors (Stream MCP-OAUTH, OA-3b). ``k[0]`` is the tenant in both shapes,
# so ``invalidate_tenant`` works uniformly. The cached value carries its
# ``expires_at`` (二期 PR2 T4): the TTL only backstops invalidation write
# paths that were never wired (e.g. a future credential entry point missing
# the T2 fan-out) — explicit invalidation stays the primary freshness path.
_CacheKey = tuple[UUID, str, str] | tuple[UUID, str, str, str]

# Stream Agent-Templates (M1-3) — resolves a platform template ``(name, version)``
# (``version`` may be ``"latest"``) to its base ``AgentSpec``, or ``None`` if the
# template is absent. Bound in the app lifespan to the platform-template store
# under ``bypass_rls_session`` (NULL-tenant rows). Lets ``_build`` merge an
# inheriting fork's manifest against its base + enforce the tier① security floor.
PlatformTemplateResolver = Callable[[str, str], Awaitable[AgentSpec | None]]


@dataclass
class AgentRuntime:
    """The control-plane's in-process agent execution surface.

    Owns the run-lifecycle :class:`RunManager`, the SSE
    :class:`StreamBridge`, and the manifest→agent build path. Built
    agents are cached per ``(tenant_id, name, version)`` — a manifest
    compiles to a graph once, not once per run.
    """

    run_manager: RunManager
    stream_bridge: StreamBridge
    agent_builder: AgentBuilder
    #: Stream H.3 PR 3 (Mini-ADR H-7) — durable mirror of every SSE
    #: frame so RunDetail's Event stream panel can replay terminal runs
    #: past the bridge's 60-second cleanup window. ``None`` keeps SSE
    #: purely in-memory; production wiring passes an
    #: :class:`InMemoryRunEventStore` (dev) or :class:`SqlRunEventStore`.
    run_event_store: RunEventStore | None = None
    #: Stream MCP-OAUTH (OA-3b) — resolves the caller's per-user OAuth MCP pool.
    #: Used only to decide whether a build must be per-user (the user has ≥1
    #: connected OAuth connector); the builder re-resolves it (cheap, cached).
    user_oauth_pool_provider: UserMcpOAuthPoolProvider | None = None
    #: Stream SE (SE-7d-3b-ii) — emits ``skill_run_usage`` at each run's terminal
    #: hook (one row per distilled skill bound), feeding the rollback monitor.
    #: ``None`` disables emission (the monitor then stays a safe no-op).
    skill_run_usage_recorder: SkillRunUsageRecorder | None = None
    #: Stream L.L7 — records each finished run's trajectory to the ObjectStore
    #: (the source the curation worker scans → the skill-evolution flywheel's
    #: fuel, and the J.13 eval gate's input). Set in the app lifespan once the
    #: ObjectStore is open; ``None`` keeps trajectory recording off (no store).
    trajectory_recorder: TrajectoryRecorder | None = None
    #: 1.3 Orchestrator-Worker — per-run dynamic-worker spawn bounds. Set in
    #: the app lifespan from platform settings. ``enabled=False`` makes
    #: :meth:`new_worker_spawn_budget` return ``None`` (no per-run caps; the
    #: feature itself is also gated by ``ToolEnv.worker_build_fn``).
    dynamic_workers_enabled: bool = False
    dynamic_worker_max_concurrent: int = 3
    dynamic_worker_max_per_run: int = 16
    #: B3 PR2 — the platform dynamic-worker config service. When set,
    #: :meth:`new_worker_spawn_budget` reads the live (DB-wins-over-env)
    #: ``max_per_run`` / ``max_concurrent`` per call, so a config change
    #: takes effect on the NEXT run without a process restart. ``None``
    #: (tests / pre-lifespan-swap) falls back to the attrs above, which
    #: the lifespan cold-reads from settings at boot.
    dynamic_worker_config_service: PlatformDynamicWorkerConfigService | None = None
    #: perf phase2 PR3 — the platform delegation-gate config service. When
    #: set, :meth:`delegation_gate` builds a process-wide concurrency gate
    #: that reads capacity live through ``effective()`` (DB-wins-over-env) on
    #: every ``acquire()``, so a platform-settings change is hot for the NEXT
    #: delegation, no restart. ``None`` (tests / pre-lifespan-swap) makes
    #: :meth:`delegation_gate` return ``None`` — delegations run ungated,
    #: same as before this gate existed.
    delegation_config_service: PlatformDelegationConfigService | None = None
    #: Lazily-built :class:`~orchestrator.tools._budget.DelegationGate`
    #: singleton (see :meth:`delegation_gate`). Typed ``Any`` to keep this
    #: module's import graph light — mirrors ``new_worker_spawn_budget``'s
    #: lazy import of the orchestrator type.
    _delegation_gate: Any = field(default=None, repr=False)
    #: The durable checkpointer the app lifespan binds into ``agent_builder``.
    #: Exposed here so read-only history surfaces (Playground resume — the
    #: ``/messages`` endpoint) can read a thread's checkpoint DIRECTLY instead of
    #: rebuilding the whole agent just to call ``aget_state``. ``None`` until the
    #: lifespan sets it (tests / pre-swap), in which case those surfaces degrade
    #: to an empty history rather than erroring.
    durable_checkpointer: BaseCheckpointSaver[Any] | None = None
    #: 二期 PR2 T4 — built-agent cache bounds. At most ``cache_max_size``
    #: entries (LRU eviction past that), each valid for ``cache_ttl_s``
    #: seconds. ``_clock`` is the injectable monotonic time source (tests).
    cache_max_size: int = 256
    cache_ttl_s: float = 1800.0
    _clock: Callable[[], float] = field(default=time.monotonic, repr=False)
    _cache: OrderedDict[_CacheKey, tuple[BuiltAgent, float]] = field(
        default_factory=OrderedDict, repr=False
    )
    #: Extra per-tenant cache invalidators fanned out by ``invalidate_tenant``
    #: — the sub-agent builder registers its own cache here (Stream V-D, audit
    #: #1) since it caches built agents independently of ``_cache``.
    _invalidation_hooks: list[Callable[[UUID], None]] = field(default_factory=list, repr=False)
    #: Stream MCP platform-servers (P1b) — clear-everything invalidators fanned
    #: out by ``invalidate_all`` when a process-global pool (the platform shared
    #: catalog) changes, which affects every tenant's build.
    _invalidate_all_hooks: list[Callable[[], None]] = field(default_factory=list, repr=False)
    #: 二期 PR2 T2 — per-(tenant, user) invalidators fanned out by
    #: ``invalidate_user`` (the sub-agent builder registers its per-user
    #: eviction here) so an OAuth token refresh / disconnect clears BOTH the
    #: top-level and the delegated per-user builds.
    _user_invalidation_hooks: list[Callable[[UUID, str], None]] = field(
        default_factory=list, repr=False
    )

    async def new_worker_spawn_budget(self) -> Any:
        """A fresh per-run :class:`WorkerSpawnBudget`, or ``None`` when dynamic
        workers are disabled. Created per run (the semaphore + count are
        per-run state), passed into ``run_agent``. Lazy-imports the
        orchestrator type to keep this module's import graph light.

        B3 PR2 — ``max_per_run`` / ``max_concurrent`` read through
        ``dynamic_worker_config_service`` (``effective()``) when set, so a
        platform-settings change is HOT: the very next run picks it up, no
        restart. ``None`` falls back to the ``dynamic_worker_max_*`` attrs,
        which are the lifespan's cold read of settings at boot.
        """
        if not self.dynamic_workers_enabled:
            return None
        from orchestrator.tools.spawn_worker import WorkerSpawnBudget

        if self.dynamic_worker_config_service is not None:
            cfg = await self.dynamic_worker_config_service.effective()
            max_per_run = cfg.max_per_run
            max_concurrent = cfg.max_concurrent
        else:
            max_per_run = self.dynamic_worker_max_per_run
            max_concurrent = self.dynamic_worker_max_concurrent

        return WorkerSpawnBudget(
            max_per_run=max_per_run,
            max_concurrent=max_concurrent,
        )

    def delegation_gate(self) -> Any | None:
        """Process-wide delegation concurrency gate — a lazy singleton.

        Unlike :meth:`new_worker_spawn_budget` (a fresh per-run object),
        this is built ONCE per process and reused by every run + every
        delegation depth (subagent + spawn_worker), so it actually bounds
        total concurrent delegations platform-wide. ``None`` when no
        ``delegation_config_service`` is wired — the gate stays unwired and
        delegations run ungated, matching behaviour before this gate existed.
        """
        if self.delegation_config_service is None:
            return None
        if self._delegation_gate is None:
            from orchestrator.tools._budget import DelegationGate

            service = self.delegation_config_service

            async def _capacity() -> int:
                return (await service.effective()).max_concurrent_delegations

            self._delegation_gate = DelegationGate(_capacity)
        return self._delegation_gate

    async def get_agent(
        self,
        *,
        tenant_id: UUID,
        name: str,
        version: str,
        spec: AgentSpec,
        user_id: str | None = None,
    ) -> BuiltAgent:
        """Return the :class:`BuiltAgent` for a manifest, building on cache miss.

        ``spec`` is only consulted on a miss. The cache key is the manifest
        identity ``(tenant, name, version)``; it is extended with ``user_id``
        ONLY when that user has ≥1 connected OAuth connector (Stream MCP-OAUTH,
        OA-3b) — so the common no-OAuth agent stays shared across users and only
        OAuth users get a per-user build.
        """
        key: _CacheKey = (tenant_id, name, version)
        if user_id is not None and self.user_oauth_pool_provider is not None:
            user_pool = await self.user_oauth_pool_provider(tenant_id, user_id)
            if user_pool.names():
                key = (tenant_id, name, version, user_id)
        cached = self._cache.get(key)
        if cached is not None:
            built, expires_at = cached
            if self._clock() < expires_at:
                self._cache.move_to_end(key)
                return built
            # Expired — drop and treat as a miss. Pure abandonment: the
            # BuiltAgent may hold live MCP connections (MCPServerPool) still
            # in use by an in-flight run, so never close() here; dropping the
            # reference leaves cleanup to GC, matching invalidate_* behavior.
            # No hook fan-out: hooks broadcast config changes, and natural
            # expiry is not one (二期 PR2 T4).
            del self._cache[key]
            self._publish_cache_size()
        built = await self.agent_builder(spec, tenant_id=tenant_id, user_id=user_id)
        self._cache[key] = (built, self._clock() + self.cache_ttl_s)
        self._cache.move_to_end(key)
        while len(self._cache) > self.cache_max_size:
            # LRU eviction — pure abandonment, no hook fan-out (same
            # rationale as the expiry branch above).
            self._cache.popitem(last=False)
        self._publish_cache_size()
        return built

    def _publish_cache_size(self) -> None:
        """Refresh the runtime-scope built-agent cache size gauge (二期 PR2 T4)."""
        set_built_agent_cache_entries(scope="runtime", count=len(self._cache))

    def register_invalidation_hook(self, hook: Callable[[UUID], None]) -> None:
        """Register an extra per-tenant cache invalidator (Stream V-D, audit #1).

        The sub-agent builder caches built agents independently of ``_cache``;
        registering its invalidator here keeps the delegation path coherent with
        the top-level cache when a tenant's MCP registry changes.
        """
        self._invalidation_hooks.append(hook)

    def register_invalidation_all(self, hook: Callable[[], None]) -> None:
        """Register an extra clear-everything invalidator (P1b).

        The sub-agent builder caches built agents independently of ``_cache``;
        registering its clear here keeps the delegation path coherent with the
        top-level cache when a process-global pool (the platform shared catalog)
        changes.
        """
        self._invalidate_all_hooks.append(hook)

    def register_user_invalidation_hook(self, hook: Callable[[UUID, str], None]) -> None:
        """Register an extra per-(tenant, user) cache invalidator (二期 PR2 T2).

        The sub-agent builder caches per-user (OAuth) child builds
        independently of ``_cache``; registering its invalidator here keeps
        the delegation path coherent with the top-level cache when a user's
        OAuth tokens rotate or disconnect.
        """
        self._user_invalidation_hooks.append(hook)

    def invalidate_all(self) -> None:
        """Drop every cached built-agent, across all tenants.

        Called when the process-global platform shared MCP pool changes (a
        catalog create/update/delete) so the next run of any agent rebuilds
        against the refreshed pool. Registered clear-all hooks (e.g. the
        sub-agent builder cache) are fanned out too.
        """
        self._cache.clear()
        self._publish_cache_size()
        for hook in self._invalidate_all_hooks:
            hook()

    def invalidate_tenant(self, tenant_id: UUID) -> None:
        """Drop every cached built-agent for ``tenant_id``.

        Called when the tenant's MCP server registry changes so the next run
        rebuilds the agent against the refreshed tenant MCP pool (Stream V-D).
        The cache key is ``(tenant_id, name, version)``. Registered hooks
        (e.g. the sub-agent builder cache) are fanned out too.
        """
        for key in [k for k in self._cache if k[0] == tenant_id]:
            del self._cache[key]
        self._publish_cache_size()
        for hook in self._invalidation_hooks:
            hook(tenant_id)

    def invalidate_user(self, tenant_id: UUID, user_id: str) -> None:
        """Drop the per-user cached agents for ``(tenant_id, user_id)``.

        Called when the user's OAuth connections change (connect / disconnect)
        so the next run rebuilds against the refreshed per-user OAuth pool
        (Stream MCP-OAUTH, OA-3b). Only 4-tuple (per-user) keys match; the
        shared no-OAuth builds are left intact. Registered user hooks (the
        sub-agent builder cache, 二期 PR2 T2) are fanned out too.
        """
        for key in [
            k for k in self._cache if k[0] == tenant_id and len(k) == 4 and k[3] == user_id
        ]:
            del self._cache[key]
        self._publish_cache_size()
        for hook in self._user_invalidation_hooks:
            hook(tenant_id, user_id)


@runtime_checkable
class _ProviderKeysCapable(Protocol):
    """A resolver that can return the ordered key list for a provider (Y-MK)."""

    async def resolve_provider_keys(self, *, tenant_id: UUID, provider: Provider) -> list[str]: ...


def make_provider_key_resolver(
    *, resolver: CredentialsResolver, tenant_id: UUID
) -> ProviderKeyResolver:
    """Bind a :class:`CredentialsResolver` + tenant to a provider→secret_ref
    *list* getter for the agent build (Stream Q, Mini-ADR Q-5; Stream Y-MK).

    Y-MK: returns the ordered list of keys to try for a provider (per-provider
    multi-key failover). An overlay resolver that exposes ``resolve_provider_keys``
    returns the full list; a base resolver falls back to its single
    ``resolve_provider`` wrapped as a 1-key list — preserving single-key builds.

    Translates :class:`CredentialsResolverError` into
    :class:`AgentFactoryError` here (control-plane) so the orchestrator's
    ``build_llm_router`` stays free of any ``expert-work-common.credentials`` import.
    """

    async def _resolve(provider: str) -> list[str]:
        try:
            if isinstance(resolver, _ProviderKeysCapable):
                return await resolver.resolve_provider_keys(
                    tenant_id=tenant_id, provider=cast(Provider, provider)
                )
            return [
                await resolver.resolve_provider(
                    tenant_id=tenant_id, provider=cast(Provider, provider)
                )
            ]
        except CredentialsResolverError as exc:
            msg = f"no platform credential is configured for provider {provider!r}"
            raise AgentFactoryError(msg) from exc

    return _resolve


def make_skill_resolver(
    *, store: SkillStore, tenant_config_service: TenantConfigService
) -> SkillResolver:
    """Bind a :class:`SkillStore` to the agent build's skill resolver
    (Stream X, Mini-ADR X-4).

    Resolution follows tenant-first / platform-fallback semantics:

    * **Tenant-first (name-shadowing, R2)** — if the tenant owns a skill
      with this ``name`` it wins, *even if* the tenant's copy is draft /
      archived (a tenant name shadows the platform library; we never fall
      back to a platform skill of the same name).
    * **Platform-fallback (R3 tier gate)** — when the tenant has no skill
      of that name, read the platform (NULL-tenant) library under
      :func:`bypass_rls_session` (platform rows are invisible to the
      tenant-scoped session otherwise — the X-1/W-8 property). A platform
      skill the tenant's plan tier doesn't satisfy returns
      ``not_entitled`` so the loader names the required plan.

    The plan lookup runs outside ``bypass_rls_session`` — ``tenant_config``
    is its own RLS table and must read under the tenant's normal scope.
    """

    async def _resolve(tenant_id: Any, name: str, version: int | None) -> _SkillLookupResult:
        # Tenant-first (R2): a tenant skill of this name shadows the
        # platform library, draft / archived included → no fallback.
        tskill = await store.get_skill_by_name(tenant_id=tenant_id, name=name)
        if tskill is not None:
            # Reuse the already-fetched ``tskill`` for the version lookup.
            # ``resolve_by_name`` / ``resolve_pinned`` would each re-run
            # ``get_skill_by_name`` internally — a redundant refetch per
            # manifest skill (an N+1 across the manifest's ``skills:``).
            if version is not None:
                pinned = await store.get_version_by_number(
                    skill_id=tskill.id, tenant_id=tenant_id, version=version
                )
                if pinned is not None:
                    return _SkillLookupResult.ok(pinned, skill=tskill)
                return _SkillLookupResult.version_not_found()
            active = (
                await store.get_version_by_number(
                    skill_id=tskill.id, tenant_id=tenant_id, version=tskill.latest_version
                )
                if tskill.status == SkillStatus.ACTIVE and tskill.latest_version != 0
                else None
            )
            if active is not None:
                return _SkillLookupResult.ok(active, skill=tskill)
            return _SkillLookupResult.not_active(skill=tskill)

        # Platform-fallback. Resolve the tenant's plan first, under its own
        # RLS scope (tenant_config is a tenant-scoped table).
        try:
            plan = (await tenant_config_service.get(tenant_id=tenant_id)).plan
        except TenantConfigNotConfiguredError:
            plan = TenantPlan.FREE

        async with bypass_rls_session():
            pskill = await store.get_platform_skill_by_name(name=name)
            if pskill is None:
                return _SkillLookupResult.not_found()
            # R3 tier gate — an un-entitled platform skill is treated as a
            # distinct build-time error so the loader can name the plan.
            if not tier_satisfies(plan, pskill.required_tier):
                return _SkillLookupResult.not_entitled(required_tier=pskill.required_tier.value)
            # Reuse ``pskill`` — the platform ``resolve_*`` helpers would each
            # re-run ``get_platform_skill_by_name`` (same redundant refetch).
            if version is not None:
                pinned = await store.get_platform_version_by_number(
                    skill_id=pskill.id, version=version
                )
                if pinned is not None:
                    return _SkillLookupResult.ok(pinned, skill=pskill)
                return _SkillLookupResult.version_not_found()
            active = (
                await store.get_platform_version_by_number(
                    skill_id=pskill.id, version=pskill.latest_version
                )
                if pskill.status == SkillStatus.ACTIVE and pskill.latest_version != 0
                else None
            )
            if active is not None:
                return _SkillLookupResult.ok(active, skill=pskill)
            return _SkillLookupResult.not_active(skill=pskill)

    return _resolve


def _declares_long_term(spec: AgentSpec) -> bool:
    """True when the manifest declares ``memory.long_term`` (Stream T)."""
    memory = spec.spec.memory
    return memory is not None and memory.long_term is not None


async def _build_judge_caller(
    spec: AgentSpec,
    *,
    tenant_id: UUID,
    credentials_resolver: CredentialsResolver,
    secret_store: SecretStore,
    judge_config_service: PlatformJudgeConfigService | None,
    http_client: httpx.AsyncClient | None = None,
) -> LLMCaller:
    """Stream PI-3-A2 — the LLM caller backing the output/action judges.

    Model selection: the **platform judge config** wins when set; otherwise the
    judge runs the **agent's own primary model** (PI-2b-3 behaviour — a
    separate, injection-free call reusing the provider's platform credential).
    A resolve failure propagates so a misconfigured judge fails the build loudly
    rather than silently shipping an agent that opted into a judge it didn't get.

    ``http_client`` (一期 Task 5) forwards the process-level shared ``httpx``
    client — the judge fires on every gated output/action, so this is a
    high-frequency call, not a background one.
    """
    provider = spec.spec.model.provider
    name = spec.spec.model.name
    if judge_config_service is not None:
        configured = await judge_config_service.effective_judge_config()
        if configured is not None:
            provider, name = cast(Provider, configured[0]), configured[1]
    secret_ref = await credentials_resolver.resolve_provider(tenant_id=tenant_id, provider=provider)
    judge_spec = ModelSpec(provider=provider, name=name, api_key_ref=secret_ref)
    return await build_llm_router(judge_spec, secret_store=secret_store, http_client=http_client)


async def _make_output_judge(
    spec: AgentSpec,
    *,
    tenant_id: UUID,
    credentials_resolver: CredentialsResolver,
    secret_store: SecretStore,
    judge_config_service: PlatformJudgeConfigService | None = None,
    http_client: httpx.AsyncClient | None = None,
) -> OutputJudge | None:
    """Stream PI-2b-3 / PI-3-A2 — build the output judge when the manifest opts
    in (``defenses.output_judge == "block"``), over the platform judge model
    (or the agent's own — see :func:`_build_judge_caller`)."""
    if spec.spec.defenses.output_judge != "block":
        return None
    caller = await _build_judge_caller(
        spec,
        tenant_id=tenant_id,
        credentials_resolver=credentials_resolver,
        secret_store=secret_store,
        judge_config_service=judge_config_service,
        http_client=http_client,
    )
    return LLMOutputJudge(caller=caller)


async def _make_action_judge(
    spec: AgentSpec,
    *,
    tenant_id: UUID,
    credentials_resolver: CredentialsResolver,
    secret_store: SecretStore,
    judge_config_service: PlatformJudgeConfigService | None = None,
    http_client: httpx.AsyncClient | None = None,
) -> ActionJudge | None:
    """Stream PI-3b-2 — build the action judge when the manifest opts in
    (``defenses.action_screen != "off"``), over the platform judge model (or
    the agent's own — see :func:`_build_judge_caller`)."""
    if spec.spec.defenses.action_screen == "off":
        return None
    caller = await _build_judge_caller(
        spec,
        tenant_id=tenant_id,
        credentials_resolver=credentials_resolver,
        secret_store=secret_store,
        judge_config_service=judge_config_service,
        http_client=http_client,
    )
    return LLMActionJudge(caller=caller)


async def _resolve_template_extends(
    spec: AgentSpec, resolver: PlatformTemplateResolver | None
) -> AgentSpec:
    """Merge a fork manifest against its platform base template (M1-3).

    Returns ``spec`` unchanged when it declares no ``extends``. Otherwise loads the
    base via ``resolver`` and applies :func:`resolve_extends` (tier① floor + clears
    ``extends``). **Fail-closed**: an ``extends`` manifest with no resolver wired,
    a malformed ref, or a missing base template raises :class:`AgentFactoryError`
    rather than building an un-floored spec (a security floor that can be skipped
    is not a floor)."""
    ref = spec.spec.extends
    if ref is None:
        return spec
    if resolver is None:
        raise AgentFactoryError(
            f"manifest extends {ref!r} but template resolution is not configured"
        )
    try:
        name, version = parse_extends_ref(ref)
    except ValueError as exc:
        raise AgentFactoryError(str(exc)) from exc
    base = await resolver(name, version)
    if base is None:
        raise AgentFactoryError(f"extends target not found: {ref!r}")
    return resolve_extends(base, spec)


def make_agent_builder(
    secret_store: SecretStore,
    checkpointer: BaseCheckpointSaver[Any],
    *,
    tool_env: ToolEnv | None = None,
    middleware_env: MiddlewareEnv | None = None,
    memory_env: MemoryEnv | None = None,
    subagent_spec_resolver: SubagentSpecResolver | None = None,
    platform_template_resolver: PlatformTemplateResolver | None = None,
    mcp_allowlist_provider: Callable[[UUID], Awaitable[Sequence[str]]] | None = None,
    tenant_mcp_pool_provider: TenantMcpPoolProvider | None = None,
    platform_mcp_pool_provider: PlatformMcpPoolProvider | None = None,
    user_mcp_oauth_pool_provider: UserMcpOAuthPoolProvider | None = None,
    credentials_resolver: CredentialsResolver | None = None,
    platform_embedding_config_service: PlatformEmbeddingConfigService | None = None,
    platform_judge_config_service: PlatformJudgeConfigService | None = None,
    # Phase 3 — platform tool-output-budget master switch (DB-wins over env).
    platform_tool_budget_config_service: PlatformToolBudgetConfigService | None = None,
    skill_store: SkillStore | None = None,
    # Spec 1 PR2 — backs the manage_task builtin (conversational scheduled
    # tasks). ``None`` → a manifest that declares manage_task fails the build.
    trigger_store: TriggerStore | None = None,
    skill_activity_recorder: SkillActivityRecorder | None = None,
    # skill-asset-store — object store for externalized skill supporting files.
    skill_asset_store: SkillAssetStore | None = None,
    tenant_config_service: TenantConfigService | None = None,
    # Stream SE (SE-3b) — backs the in-session skill-authoring builtins'
    # audit emit (SKILL_AUTHORED_BY_AGENT etc.). ``None`` → tools still work,
    # audit is simply not written.
    audit_logger: AuditLogger | None = None,
    # Platform-default wall-clock floor (seconds) for a run's deadline,
    # applied when the manifest leaves ``policies.run_deadline_s`` at 0.
    # 0 = no floor (manifest-only). Bound from ``settings`` by the lifespan.
    default_run_deadline_s: int = 0,
    # 一期 Task 5 — process-level shared HTTP client (control-plane lifespan).
    # Forwarded into ``build_agent`` for every build this builder produces.
    # ``None`` (tests / pre-lifespan placeholder builder) keeps every LLM
    # provider client on its original per-call ``httpx.AsyncClient``.
    http_client: httpx.AsyncClient | None = None,
) -> AgentBuilder:
    """Production :data:`AgentBuilder` bound to a SecretStore + checkpointer.

    ``tool_env`` / ``middleware_env`` / ``memory_env`` inject the
    platform tool, middleware, and long-term-memory backends. Kept
    separate from :func:`make_agent_runtime` so the app lifespan can
    rebuild the builder once the durable checkpointer's connection
    context is open and the tenant-config service is ready.

    ``subagent_spec_resolver`` (Mini-ADR J-40) lets the top-level
    builder check the manifest's delegation graph for cycles before
    assembling any tools. When ``None`` (the default), the check is
    skipped — for unit tests + agents that declare no ``subagents``
    block. The app lifespan binds it to an ``AgentSpecStore`` synchronous
    resolver so a cycle in production is rejected at build time
    (``AgentFactoryError``) rather than blowing the depth cap at run
    time.

    ``platform_embedding_config_service`` (Stream T, PR B) hosts the
    build-time embedding gate. The dynamic embedder is never ``None`` (it
    resolves the live config per call), so the orchestrator's
    ``embedder is None`` gate can no longer fire; this builder checks the
    effective config instead — a manifest declaring ``memory.long_term``
    with platform embedding unconfigured is rejected here at build time.
    ``None`` skips the check (unit tests / the placeholder builder
    ``make_agent_runtime`` installs before the lifespan swap); the
    orchestrator gate stays as defense.
    """

    async def _build(
        spec: AgentSpec,
        *,
        tenant_id: UUID | None = None,
        user_id: str | None = None,
        token_usage_kind: str = "conversation",  # noqa: S107 — usage label, not a secret
    ) -> BuiltAgent:
        # Stream Agent-Templates (M1-3) — resolve template inheritance FIRST so all
        # downstream gates see the merged, floored spec. A fork manifest declares
        # ``spec.extends = "<template>@<version>"``; we load the platform base and
        # enforce the tier① security floor (the tenant cannot weaken inherited
        # defenses), then clear ``extends`` to a plain build-ready spec.
        spec = await _resolve_template_extends(spec, platform_template_resolver)
        # Stream T (PR B) — build-time embedding gate. A manifest that
        # declares long-term memory needs a configured platform embedder;
        # the dynamic embedder object is always present, so we check the
        # effective config rather than ``embedder is None``.
        if platform_embedding_config_service is not None and _declares_long_term(spec):
            if await platform_embedding_config_service.effective_embedding_config() is None:
                raise AgentFactoryError(
                    "manifest declares memory.long_term but platform embedding is not "
                    "configured — configure it in platform settings"
                )
        if subagent_spec_resolver is not None and spec.spec.subagents:
            detect_subagent_cycle(spec, resolve=subagent_spec_resolver)
        build_tool_env = tool_env
        # Stream MCP platform-servers (P1b) — attach the platform-curated shared
        # catalog pool (process-global, gated by the allowlist below, like the
        # operator file pool). Only for real builds (a tenant); preview /
        # validation builds (tenant_id None) get no runnable pool.
        if platform_mcp_pool_provider is not None and tenant_id is not None:
            platform_pool = await platform_mcp_pool_provider()
            if platform_pool.names():
                base_env = build_tool_env if build_tool_env is not None else ToolEnv()
                build_tool_env = replace(base_env, platform_mcp_pool=platform_pool)
        # Stream O (Mini-ADR O-14) — apply the tenant's MCP server allowlist to
        # the per-build ToolEnv. Gates both platform pools (file ``mcp_pool`` +
        # DB ``platform_mcp_pool``); empty / unconfigured → no restriction.
        if (
            mcp_allowlist_provider is not None
            and tenant_id is not None
            and build_tool_env is not None
            and (
                build_tool_env.mcp_pool is not None or build_tool_env.platform_mcp_pool is not None
            )
        ):
            allowlist = await mcp_allowlist_provider(tenant_id)
            if allowlist:
                build_tool_env = replace(build_tool_env, mcp_allowlist=tuple(allowlist))
        # Stream V (Mini-ADR V-4) — attach the tenant's own remote MCP pool.
        if tenant_mcp_pool_provider is not None and tenant_id is not None:
            tenant_pool = await tenant_mcp_pool_provider(tenant_id)
            if tenant_pool.names():
                base_env = build_tool_env if build_tool_env is not None else ToolEnv()
                build_tool_env = replace(base_env, tenant_mcp_pool=tenant_pool)
        # Stream MCP-OAUTH (OA-3b) — attach the caller's own OAuth-connected MCP
        # pool (per-(tenant,user)). user_id = principal.subject_id.
        if (
            user_mcp_oauth_pool_provider is not None
            and tenant_id is not None
            and user_id is not None
        ):
            user_pool = await user_mcp_oauth_pool_provider(tenant_id, user_id)
            if user_pool.names():
                base_env = build_tool_env if build_tool_env is not None else ToolEnv()
                build_tool_env = replace(base_env, user_mcp_oauth_pool=user_pool)
        # Stream Q (Mini-ADR Q-5) / Stream Y-2 — resolve each model's key from
        # the tenant's platform-configured credential. Y-2: manifest api_key_ref
        # is ignored for agent builds, so this resolver is the ONLY key source.
        # Needs a tenant; preview/validation builds (tenant_id None) get no
        # resolver and therefore cannot build a runnable agent (by design).
        provider_key_resolver = (
            make_provider_key_resolver(resolver=credentials_resolver, tenant_id=tenant_id)
            if credentials_resolver is not None and tenant_id is not None
            else None
        )
        # Stream X (Mini-ADR X-4) — first wiring of skill resolution into the
        # agent build. Needs a tenant; preview / validation builds (tenant_id
        # None) keep today's behaviour (a skills manifest errors at build).
        skill_resolver = (
            make_skill_resolver(store=skill_store, tenant_config_service=tenant_config_service)
            if skill_store is not None
            and tenant_config_service is not None
            and tenant_id is not None
            else None
        )
        # Stream PI-2b-3 — model-backed output judge, when the manifest opts in
        # and the tenant credential path is wired (preview/validation builds
        # without a resolver get no judge, same as the agent's own model).
        output_judge = (
            await _make_output_judge(
                spec,
                tenant_id=tenant_id,
                credentials_resolver=credentials_resolver,
                secret_store=secret_store,
                judge_config_service=platform_judge_config_service,
                http_client=http_client,
            )
            if credentials_resolver is not None and tenant_id is not None
            else None
        )
        # Stream PI-3b-2 — model-backed action judge, same gating + model source.
        action_judge = (
            await _make_action_judge(
                spec,
                tenant_id=tenant_id,
                credentials_resolver=credentials_resolver,
                secret_store=secret_store,
                judge_config_service=platform_judge_config_service,
                http_client=http_client,
            )
            if credentials_resolver is not None and tenant_id is not None
            else None
        )
        # Phase 3 — resolve the platform tool-output-budget master switch
        # (DB-wins over the EXPERT_WORK_TOOL_OUTPUT_BUDGET env default). ``None`` lets
        # build_agent fall back to the env default.
        platform_tool_budget_enabled = (
            await platform_tool_budget_config_service.effective_enabled()
            if platform_tool_budget_config_service is not None
            else None
        )
        return await build_agent(
            spec,
            secret_store=secret_store,
            checkpointer=checkpointer,
            default_run_deadline_s=default_run_deadline_s,
            token_usage_kind=token_usage_kind,
            tool_env=build_tool_env,
            middleware_env=middleware_env,
            memory_env=memory_env,
            tenant_id=tenant_id,
            provider_key_resolver=provider_key_resolver,
            skill_resolver=skill_resolver,
            skill_activity_recorder=skill_activity_recorder,
            # Stream SE (SE-3b) — raw store + audit for in-session authoring.
            skill_store=skill_store,
            # Spec 1 PR2 — backs the manage_task builtin.
            trigger_store=trigger_store,
            audit_logger=audit_logger,
            # Stream PI-2b-3 — gated model-backed output judge.
            output_judge=output_judge,
            # Stream PI-3b-2 — gated model-backed action judge.
            action_judge=action_judge,
            # Phase 3 — platform master switch (effective = platform AND agent).
            platform_tool_budget_enabled=platform_tool_budget_enabled,
            # skill-asset-store — dual-read for externalized supporting files.
            skill_asset_store=skill_asset_store,
            # 一期 Task 5 — process-level shared HTTP client.
            http_client=http_client,
        )

    return _build


# ---------------------------------------------------------------------------
# Stream O Mini-ADR O-9 — per-tenant credential-resolving callers
#
# embedder / reranker / web_search were platform singletons with the API key
# baked in at boot. These wrappers resolve the per-tenant secret_ref via
# :class:`CredentialsResolver` at call time (platform vs tenant mode), then
# build the concrete client. Resolution runs once per ``embed`` batch / once
# per rerank / once per search. 二期 PR2 T3 —— vault 读(``secret_store.get``)
# 经 ``_cached_secret`` 走进程内 ``CredentialValueCache``(LRU 256 / TTL 300s,
# T2 的凭据写入口连动失效);resolve(DB)读仍由 PlatformSecretsService 30s TTL 挡。
# The wrappers live here (control-plane glue) so the orchestrator package
# never imports expert-work-common.credentials; they implement the orchestrator
# protocols structurally.
# ---------------------------------------------------------------------------


async def _cached_secret(
    cache: CredentialValueCache | None,
    secret_store: SecretStore,
    tenant_id: UUID,
    secret_ref: str,
) -> str:
    """Resolve ``secret_ref``'s value through the process-level cache.

    ``cache=None``(测试 / 未接线)= 现状行为:每次直读 vault。命中时不打
    vault——调用方既有的 ``secret_ms`` span attribute 自然趋 0,即收益读数。
    """
    if cache is not None:
        hit = cache.get(tenant_id, secret_ref)
        if hit is not None:
            return hit
    value = await secret_store.get(parse_secret_ref(secret_ref))
    if cache is not None:
        cache.put(tenant_id, secret_ref, value)
    return value


@dataclass(frozen=True)
class ResolvingEmbedder:
    """Per-tenant credential-resolving :class:`Embedder` (Mini-ADR O-9)."""

    resolver: CredentialsResolver
    secret_store: SecretStore
    provider: Provider
    model: str
    #: 一期 Task 5 — process-level shared HTTP client. ``None`` (tests / not
    #: yet wired) falls back to the embedding client's own per-call default.
    http: httpx.AsyncClient | None = None
    #: 二期 PR2 T3 — process-level secret-value cache. ``None`` (tests / not
    #: yet wired) reads the vault every call (pre-T3 behaviour).
    secret_cache: CredentialValueCache | None = None

    async def embed(self, texts: Sequence[str], *, tenant_id: UUID) -> list[tuple[float, ...]]:
        if not texts:
            return []
        # 一期 Task 1 —— 每次 embed 都重解一次凭据(DB 读 + vault 读)。这两段
        # 挂到调用方的 ``memory.embed`` span 上而不是各开一个子 span:它们是
        # 同一次调用的内部构成,单列会让瀑布图多两行毫秒级噪音。二期 PR2 T3
        # 起 vault 读走 ``_cached_secret``——命中时 ``secret_ms`` 趋 0,即收益。
        t0 = time.monotonic()
        secret_ref = await self.resolver.resolve_provider(
            tenant_id=tenant_id, provider=self.provider
        )
        t1 = time.monotonic()
        api_key = await _cached_secret(self.secret_cache, self.secret_store, tenant_id, secret_ref)
        t2 = time.monotonic()
        span = trace.get_current_span()
        span.set_attribute("resolve_ms", round((t1 - t0) * 1000))
        span.set_attribute("secret_ms", round((t2 - t1) * 1000))
        delegate = OpenAICompatibleEmbedder(
            client=HTTPEmbeddingClient(api_key=api_key, http=self.http), model=self.model
        )
        return await delegate.embed(texts, tenant_id=tenant_id)


def _is_dashscope_rerank_model(provider: str, model: str) -> bool:
    """DashScope (``qwen``) dedicated rerank models (``qwen3-vl-rerank``,
    ``gte-rerank``, ``text-rerank-*``) are NOT served over the OpenAI-compat
    chat endpoint, so the chat-prompt ``LLMReranker`` 404s on them. Route those
    to the native rerank API (:class:`DashScopeReranker`) instead."""
    return provider == "qwen" and "rerank" in model.lower()


@dataclass(frozen=True)
class ResolvingReranker:
    """Per-tenant credential-resolving :class:`Reranker` (Mini-ADR O-9).

    Rerank is an optional quality pass — if the platform has no credential
    for the rerank provider, degrade to the RRF-fused order rather than
    failing (Mini-ADR O-12)."""

    resolver: CredentialsResolver
    secret_store: SecretStore
    provider: Provider
    model: str
    #: 一期 Task 5 — process-level shared HTTP client. ``None`` (tests / not
    #: yet wired) falls back to each client's own per-call default.
    http: httpx.AsyncClient | None = None
    #: 二期 PR2 T3 — process-level secret-value cache. ``None`` (tests / not
    #: yet wired) reads the vault every call (pre-T3 behaviour).
    secret_cache: CredentialValueCache | None = None

    async def rerank(
        self, *, query: str, documents: Sequence[str], top_k: int, tenant_id: UUID
    ) -> list[int]:
        if not documents:
            return []
        # 一期 Task 1 —— 同 ResolvingEmbedder.embed:凭据解析计时挂到调用方的
        # ``memory.rerank`` span 上。secret_store.get 只在 DashScope 分支里
        # 调用(LLM 分支走 build_llm_router),所以两条子路径各自打点。
        t0 = time.monotonic()
        try:
            secret_ref = await self.resolver.resolve_provider(
                tenant_id=tenant_id, provider=self.provider
            )
        except CredentialsResolverError:
            logger.info(
                "knowledge.rerank_skipped — no credential for provider=%s tenant=%s",
                self.provider,
                tenant_id,
            )
            return list(range(len(documents)))[:top_k]
        t1 = time.monotonic()
        span = trace.get_current_span()
        span.set_attribute("resolve_ms", round((t1 - t0) * 1000))
        if _is_dashscope_rerank_model(self.provider, self.model):
            api_key = await _cached_secret(
                self.secret_cache, self.secret_store, tenant_id, secret_ref
            )
            span.set_attribute("secret_ms", round((time.monotonic() - t1) * 1000))
            return await DashScopeReranker(
                client=HTTPDashScopeRerankClient(api_key=api_key, http=self.http), model=self.model
            ).rerank(query=query, documents=documents, top_k=top_k, tenant_id=tenant_id)
        model_spec = ModelSpec.model_validate(
            {"provider": self.provider, "name": self.model, "api_key_ref": secret_ref}
        )
        # 刻意不传 cache——plan 拍定不穿透 build_llm_router 的 vault 读
        # (rerank-LLM 分支少见配置,见 plan Global Constraints)。
        router = await build_llm_router(
            model_spec, secret_store=self.secret_store, http_client=self.http
        )
        return await LLMReranker(llm_caller=router).rerank(
            query=query, documents=documents, top_k=top_k, tenant_id=tenant_id
        )


@dataclass(frozen=True)
class DynamicResolvingEmbedder:
    """Embedder reading the live platform embedding config per call so an
    admin's change takes effect without restart (Stream T, Mini-ADR T-3)."""

    config_service: PlatformEmbeddingConfigService
    resolver: CredentialsResolver
    secret_store: SecretStore
    #: 一期 Task 5 — process-level shared HTTP client. ``None`` (tests / not
    #: yet wired) falls back to the embedding client's own per-call default.
    http: httpx.AsyncClient | None = None
    #: 二期 PR2 T3 — process-level secret-value cache. ``None`` (tests / not
    #: yet wired) reads the vault every call (pre-T3 behaviour).
    secret_cache: CredentialValueCache | None = None

    async def embed(self, texts: Sequence[str], *, tenant_id: UUID) -> list[tuple[float, ...]]:
        if not texts:
            return []
        # 一期 Task 1 —— 同 ResolvingEmbedder.embed,但这条路径每次调用还多一次
        # DB 读(平台配置),所以是三段计时。挂到调用方的 ``memory.embed`` span
        # 上。``cfg is None`` 的早退直接抛错,没有 embed 可言,不打属性。
        t0 = time.monotonic()
        cfg = await self.config_service.effective_embedding_config()
        t1 = time.monotonic()
        if cfg is None:
            raise AgentFactoryError(
                "platform embedding is not configured — configure it in platform settings"
            )
        provider, model = cfg
        secret_ref = await self.resolver.resolve_provider(tenant_id=tenant_id, provider=provider)
        t2 = time.monotonic()
        api_key = await _cached_secret(self.secret_cache, self.secret_store, tenant_id, secret_ref)
        t3 = time.monotonic()
        span = trace.get_current_span()
        span.set_attribute("config_ms", round((t1 - t0) * 1000))
        span.set_attribute("resolve_ms", round((t2 - t1) * 1000))
        span.set_attribute("secret_ms", round((t3 - t2) * 1000))
        delegate = OpenAICompatibleEmbedder(
            client=HTTPEmbeddingClient(api_key=api_key, http=self.http), model=model
        )
        return await delegate.embed(texts, tenant_id=tenant_id)


@dataclass(frozen=True)
class DynamicResolvingReranker:
    """Reranker reading the live platform rerank config per call; degrades to
    identity order when rerank is unconfigured (Stream T, Mini-ADR T-3)."""

    config_service: PlatformEmbeddingConfigService
    resolver: CredentialsResolver
    secret_store: SecretStore
    #: 一期 Task 5 — process-level shared HTTP client. ``None`` (tests / not
    #: yet wired) falls back to each client's own per-call default.
    http: httpx.AsyncClient | None = None
    #: 二期 PR2 T3 — process-level secret-value cache. ``None`` (tests / not
    #: yet wired) reads the vault every call (pre-T3 behaviour).
    secret_cache: CredentialValueCache | None = None

    async def rerank(
        self, *, query: str, documents: Sequence[str], top_k: int, tenant_id: UUID
    ) -> list[int]:
        if not documents:
            return []
        # 一期 Task 1 —— 同 ResolvingReranker.rerank,但这条路径每次调用还多
        # 一次 DB 读(平台配置)。config_ms/resolve_ms 挂到调用方的
        # ``memory.rerank`` span 上;secret_ms 只在 DashScope 分支打(LLM 分支
        # 走 build_llm_router)。三条早退路径(无 documents/cfg 未配置/无凭据)
        # 都没有 rerank 可言,不打属性。
        t0 = time.monotonic()
        cfg = await self.config_service.effective_rerank_config()
        t1 = time.monotonic()
        if cfg is None:
            return list(range(len(documents)))[:top_k]
        provider, model = cfg
        try:
            secret_ref = await self.resolver.resolve_provider(
                tenant_id=tenant_id, provider=provider
            )
        except CredentialsResolverError:
            logger.info(
                "knowledge.rerank_skipped — no credential for provider=%s tenant=%s",
                provider,
                tenant_id,
            )
            return list(range(len(documents)))[:top_k]
        t2 = time.monotonic()
        span = trace.get_current_span()
        span.set_attribute("config_ms", round((t1 - t0) * 1000))
        span.set_attribute("resolve_ms", round((t2 - t1) * 1000))
        if _is_dashscope_rerank_model(provider, model):
            api_key = await _cached_secret(
                self.secret_cache, self.secret_store, tenant_id, secret_ref
            )
            span.set_attribute("secret_ms", round((time.monotonic() - t2) * 1000))
            return await DashScopeReranker(
                client=HTTPDashScopeRerankClient(api_key=api_key, http=self.http), model=model
            ).rerank(query=query, documents=documents, top_k=top_k, tenant_id=tenant_id)
        model_spec = ModelSpec.model_validate(
            {"provider": provider, "name": model, "api_key_ref": secret_ref}
        )
        # 刻意不传 cache——plan 拍定不穿透 build_llm_router 的 vault 读
        # (rerank-LLM 分支少见配置,见 plan Global Constraints)。
        router = await build_llm_router(
            model_spec, secret_store=self.secret_store, http_client=self.http
        )
        return await LLMReranker(llm_caller=router).rerank(
            query=query, documents=documents, top_k=top_k, tenant_id=tenant_id
        )


async def resolve_embedder(
    *,
    resolver: CredentialsResolver,
    secret_store: SecretStore,
    provider: Provider,
    model: str,
    supported_providers: Sequence[Provider],
) -> Embedder | None:
    """Build the per-tenant credential-resolving embedder for long-term
    memory (Stream J.3 + Mini-ADR O-9).

    ``provider`` not in ``supported_providers`` → ``None``: the deployment
    has no embedding credential at all (legacy or Stream O), so long-term
    memory is globally unavailable and an agent declaring
    ``memory.long_term`` fails at build time (the build-time gate is
    preserved). Per-tenant failures (tenant mode, missing key) surface at
    ``embed`` time instead (Mini-ADR O-11).
    """
    if provider not in supported_providers:
        return None
    return ResolvingEmbedder(
        resolver=resolver, secret_store=secret_store, provider=provider, model=model
    )


async def resolve_reranker(
    *,
    resolver: CredentialsResolver,
    secret_store: SecretStore,
    provider: Provider,
    model: str,
    supported_providers: Sequence[Provider],
) -> Reranker | None:
    """Build the per-tenant credential-resolving reranker (Stream J.5 +
    Mini-ADR O-9). ``provider`` not in ``supported_providers`` → ``None``
    (no rerank pass; hybrid search returns the RRF-fused order)."""
    if provider not in supported_providers:
        return None
    return ResolvingReranker(
        resolver=resolver, secret_store=secret_store, provider=provider, model=model
    )


def _tenant_allowlist_provider(service: TenantConfigService) -> AllowlistProvider:
    """An :data:`AllowlistProvider` reading ``http_tool_allowlist`` from
    the per-tenant config. Empty / un-configured ↔ allow all public hosts
    (the HTTP tool's SSRF guard + denylist are the safety net); a non-empty
    list restricts the tool to those URL patterns."""

    async def _provider(tenant_id: UUID | None) -> Sequence[str]:
        if tenant_id is None:
            return []
        try:
            record = await service.get(tenant_id=tenant_id)
        except TenantConfigNotConfiguredError:
            return []
        return record.http_tool_allowlist

    return _provider


def _tenant_denylist_provider(service: TenantConfigService) -> DenylistProvider:
    """A :data:`DenylistProvider` reading ``http_tool_denylist`` from the
    per-tenant config. Empty / un-configured ↔ nothing denied. Denied hosts
    are refused even under allow-all — precedence over the allowlist."""

    async def _provider(tenant_id: UUID | None) -> Sequence[str]:
        if tenant_id is None:
            return []
        try:
            record = await service.get(tenant_id=tenant_id)
        except TenantConfigNotConfiguredError:
            return []
        return record.http_tool_denylist

    return _provider


def make_mcp_allowlist_provider(
    service: TenantConfigService,
) -> Callable[[UUID], Awaitable[Sequence[str]]]:
    """Stream O (Mini-ADR O-14) — reads ``tenant_config.mcp_allowlist`` for the
    agent builder. Empty / unconfigured → no restriction (agent sees every
    platform MCP server); non-empty restricts to the listed server names."""

    async def _provider(tenant_id: UUID) -> Sequence[str]:
        try:
            record = await service.get(tenant_id=tenant_id)
        except TenantConfigNotConfiguredError:
            return []
        return record.mcp_allowlist

    return _provider


def resolve_web_search_client(
    *, searxng_base_url: str | None, http: httpx.AsyncClient | None = None
) -> TavilyClient | None:
    """Build the web-search backend for the builtin ``web_search`` tool.

    The builtin backend is a self-hosted SearXNG instance (free, no API
    key) — design ``web-search-searxng-builtin-and-tavily-mcp`` M2. Set
    ``searxng_base_url`` → :class:`SearXNGClient`; unset → ``None`` (an
    agent declaring ``web_search`` then fails at build, gate preserved).
    Premium Tavily moved to the platform MCP catalog (not a builtin).

    ``http`` (一期 Task 5) is the process-level shared HTTP client; ``None``
    falls back to the client's own per-call default."""
    if searxng_base_url:
        return SearXNGClient(base_url=searxng_base_url, http=http)
    return None


#: Builds an :class:`MCPClient` from a server config. The default
#: dispatches on ``config.transport``; tests inject a recording client.
McpClientFactory = Callable[[MCPServerConfig], Awaitable[MCPClient]]


def _load_mcp_server_configs(path: str) -> list[MCPServerConfig]:
    """Parse the platform MCP-server JSON file (Mini-ADR E-17 + U-10).

    Backward-compatible shapes:

    - stdio (legacy default): ``{"name", "command": [...], "env": {...}}``
    - stdio (explicit): ``{"name", "transport": "stdio", "command": [...]}``
    - sse / streamable_http (new in Capability Uplift Sprint #5):
      ``{"name", "transport": "sse" | "streamable_http", "url": str,
         "headers": {...}, "auth_type": "none" | "bearer" | "oauth2",
         "auth_config": {...}, "timeout_s": float, "retry_max": int}``

    Unknown keys raise — fail-fast on operator typos.
    """
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        msg = f"mcp_servers_config_file must contain a JSON array: {path}"
        raise ValueError(msg)
    return [_build_mcp_server_config(entry) for entry in raw]


def _build_mcp_server_config(entry: dict[str, Any]) -> MCPServerConfig:
    """Promote one JSON entry to a typed :class:`MCPServerConfig`.

    Validation (transport/url/auth pairing) happens in the dataclass
    ``__post_init__``; this helper only does the dict→kwarg lift +
    backward-compatible transport defaulting.
    """
    transport = entry.get("transport", "stdio")
    kwargs: dict[str, Any] = {"name": entry["name"], "transport": transport}
    if transport == "stdio":
        kwargs["command"] = entry["command"]
        if "env" in entry:
            kwargs["env"] = entry["env"]
    else:
        kwargs["url"] = entry.get("url")
        if "headers" in entry:
            kwargs["headers"] = entry["headers"]
    for opt in ("auth_type", "auth_config", "timeout_s", "retry_max"):
        if opt in entry:
            kwargs[opt] = entry[opt]
    return MCPServerConfig(**kwargs)


async def _build_mcp_client(
    config: MCPServerConfig,
    *,
    secret_store: SecretStore | None,
) -> MCPClient:
    """Build the right :class:`MCPClient` for ``config.transport``.

    OAuth2 servers fail fast (Mini-ADR U-12) — the schema accepts the
    config but the flow ships in a follow-up sprint. Bearer auth
    resolves ``auth_config["token_ref"]`` through the
    :class:`SecretStore` (Mini-ADR U-11) and injects an
    ``Authorization`` header without storing the value on the config.
    """
    if config.transport == "stdio":
        client: MCPClient = StdioMCPClient(config=config)
        await client.start()  # type: ignore[attr-defined]
        return client

    if config.auth_type == "oauth2":
        from orchestrator.tools.mcp import MCPOAuthNotImplementedError

        msg = (
            f"mcp server {config.name!r}: oauth2 auth flow not implemented "
            "in this release — see Mini-ADR L.L8-MCP. Switch to "
            'auth_type="bearer" with a token_ref or remove the server.'
        )
        raise MCPOAuthNotImplementedError(msg)

    resolved_headers = dict(config.headers)
    # Custom headers (M1): resolve the encrypted {name: value} blob and merge it
    # in BEFORE bearer so a bearer ``Authorization`` always wins (the API layer
    # also rejects a custom Authorization header when auth_type=bearer).
    headers_ref = config.auth_config.get("headers_ref")
    if headers_ref:
        if secret_store is None:
            msg = (
                f"mcp server {config.name!r}: custom headers need a "
                "SecretStore but none was provided to build_mcp_pool"
            )
            raise RuntimeError(msg)
        blob = await secret_store.get(parse_secret_ref(headers_ref))
        custom = json.loads(blob)
        if isinstance(custom, dict):
            resolved_headers.update({str(k): str(v) for k, v in custom.items()})
    if config.auth_type == "bearer":
        if secret_store is None:
            msg = (
                f"mcp server {config.name!r}: bearer auth needs a "
                "SecretStore but none was provided to build_mcp_pool"
            )
            raise RuntimeError(msg)
        token_ref = config.auth_config["token_ref"]
        token = await secret_store.get(parse_secret_ref(token_ref))
        resolved_headers["Authorization"] = f"Bearer {token}"

    # Re-validate at the connect-out site (audit #4) so registration, probe,
    # and runtime share one gate — a row that ever reached the DB unvalidated
    # cannot be dialed. NOTE: the guard is static (no DNS resolution), so it
    # does not by itself defeat DNS rebinding; pinning the resolved IP is a
    # separate follow-up.
    if config.url is not None:
        validate_remote_url(config.url)

    remote: SseMCPClient | StreamableHttpMCPClient
    if config.transport == "sse":
        remote = SseMCPClient(config=config, resolved_headers=resolved_headers)
    else:
        remote = StreamableHttpMCPClient(config=config, resolved_headers=resolved_headers)
    await remote.start()
    return remote


@asynccontextmanager
async def build_mcp_pool(
    config_file: str | None,
    *,
    secret_store: SecretStore | None = None,
    client_factory: McpClientFactory | None = None,
) -> AsyncIterator[MCPServerPool]:
    """Yield an :class:`MCPServerPool` of the platform's MCP servers.

    The pool launches each entry in ``config_file`` (Mini-ADR E-17 —
    operator-controlled, never tenant input). ``None`` yields an empty
    pool. The pool — and every connection / subprocess — is torn down
    on exit, including when a mid-startup failure aborts boot.

    ``client_factory`` overrides the default transport-dispatching
    factory (used by tests injecting a :class:`RecordingMCPClient`).
    ``secret_store`` is required for bearer-auth remote servers but
    unused for stdio / unauthenticated remotes.
    """
    if client_factory is None:

        async def _factory(cfg: MCPServerConfig) -> MCPClient:
            return await _build_mcp_client(cfg, secret_store=secret_store)

        client_factory = _factory

    pool = MCPServerPool()
    try:
        if config_file:
            for config in _load_mcp_server_configs(config_file):
                await pool.add(config.name, await client_factory(config))
        yield pool
    finally:
        await pool.close_all()


def build_sandbox_runtime(url: str | None) -> SandboxRuntime | None:
    """Build the Sandbox Supervisor HTTP client from its base URL.

    ``None`` → the ``exec_python`` tool is unavailable; an agent that
    declares it fails at build time with a clear error.

    一期 Task 5 — no ``http`` param here: this factory runs in ``app.py``'s
    synchronous ``create_app`` body, before the lifespan creates the shared
    ``httpx.AsyncClient``, so no caller could ever pass one. The lifespan
    instead mutates ``.http`` on the returned client in place once the
    shared client exists (``HTTPSupervisorRuntime`` is not frozen, so that
    is safe) — see the ``isinstance(..., HTTPSupervisorRuntime)`` guard in
    ``app.py``.
    """
    if url is None:
        return None
    return HTTPSupervisorRuntime(base_url=url)


def build_workspace_store(url: str | None) -> WorkspaceStore | None:
    """Build the workspace-file client from the supervisor's base URL.

    波 1 Task 4 — 工作区文件操作从 ``SandboxRuntime`` 拆出。本地/CI 下
    工作区是 docker 卷,只有 supervisor 碰得到,所以这个实现仍走 HTTP;
    波 2 的 ``NasWorkspaceStore`` 会直接读挂载的文件系统。

    ``None`` → 工作区文件端点不可用,与 ``build_sandbox_runtime`` 同语义。
    """
    if url is None:
        return None
    return SupervisorWorkspaceStore(base_url=url)


def build_tool_env(
    tenant_config_service: TenantConfigService,
    *,
    web_search_client: TavilyClient | None = None,
    sandbox_runtime: SandboxRuntime | None = None,
    mcp_pool: MCPServerPool | None = None,
    artifact_store: ArtifactStore | None = None,
    knowledge_retriever: KnowledgeRetriever | None = None,
    image_resolver: ImageResolver | None = None,
    workspace_lock: WorkspaceLock | None = None,
) -> ToolEnv:
    """Assemble the M0 :class:`ToolEnv`.

    Wires the HTTP tool's per-tenant allowlist, and — when supplied —
    the ``web_search`` Tavily client, the ``exec_python`` sandbox
    runtime, the ``mcp`` server pool, the J.9 artifact store
    backing ``save_artifact`` / ``list_artifacts``, the J.5 knowledge
    retriever backing ``knowledge_search``, and the J.6 image resolver
    backing multimodal input.
    """
    return ToolEnv(
        allowlist_provider=_tenant_allowlist_provider(tenant_config_service),
        denylist_provider=_tenant_denylist_provider(tenant_config_service),
        web_search_client=web_search_client,
        sandbox_runtime=sandbox_runtime,
        mcp_pool=mcp_pool,
        artifact_store=artifact_store,
        knowledge_retriever=knowledge_retriever,
        image_resolver=image_resolver,
        workspace_lock=workspace_lock or NullWorkspaceLock(),
    )


def make_knowledge_retriever(
    *,
    store: KnowledgeStore,
    embedder: Embedder | None,
    reranker: Reranker | None,
) -> KnowledgeRetriever | None:
    """Build the :class:`KnowledgeRetriever` backing ``knowledge_search``.

    ``None`` when no embedder is configured — ``knowledge_search`` is
    then unavailable, like long-term memory without an embedder.
    """
    if embedder is None:
        return None
    return KnowledgeRetriever(store=store, embedder=embedder, reranker=reranker)


async def resolve_object_store_config(
    *,
    backend: ObjectStoreBackend,
    endpoint_url: str | None,
    region: str,
    bucket: str,
    access_key_ref: str | None,
    secret_key_ref: str | None,
    secret_store: SecretStore,
    addressing_style: Literal["path", "virtual", "auto"] = "path",
) -> S3CompatibleConfig | None:
    """Build the S3 config for ``make_object_store`` (Stream J.6).

    ``None`` for the in-memory backend (no config needed). For
    ``s3-compatible`` the endpoint URL + both ``secret://`` key
    references are required; the keys are resolved through the
    SecretStore. Missing fields fail fast with a clear error.

    ``addressing_style`` defaults to ``"path"`` (MinIO local dev);
    callers must pass ``Settings.object_store_addressing_style`` through
    so OSS prod can request ``"virtual"`` (W0 real-bucket finding: OSS
    rejects path-style addressing).
    """
    if backend != "s3-compatible":
        return None
    if not (endpoint_url and access_key_ref and secret_key_ref):
        msg = (
            "object_store_backend='s3-compatible' requires object_store_endpoint_url "
            "+ object_store_access_key_ref + object_store_secret_key_ref"
        )
        raise RuntimeError(msg)
    return S3CompatibleConfig(
        endpoint_url=endpoint_url,
        region=region,
        bucket=bucket,
        access_key=await secret_store.get(parse_secret_ref(access_key_ref)),
        secret_key=await secret_store.get(parse_secret_ref(secret_key_ref)),
        addressing_style=addressing_style,
    )


def make_image_resolver(store: ObjectStore) -> ImageResolver:
    """Build the J.6 image resolver over an object store — backs both
    multimodal paths (Path A content blocks + Path B ``ask_image``).

    Wrapped in a bounded LRU cache: the resolver is a single long-lived
    instance, and every LLM turn re-resolves every image in the conversation
    history, so caching stops the same image being re-fetched each turn.
    """
    return CachingImageResolver(ObjectStoreImageResolver(store=store))


def build_middleware_env(
    *,
    token_usage_store: TokenUsageStore | None = None,
    langfuse_client: LangfuseClient | None = None,
) -> MiddlewareEnv:
    """Assemble the M0 :class:`MiddlewareEnv`.

    Single-instance defaults: an in-process response cache, the Langfuse
    client (Stream HX-7 — ``app.py`` passes ``make_langfuse_client``'s
    resolution: the SDK-backed adapter when the deployment configures a
    Langfuse instance, the span-recording stub otherwise; ``None`` here
    keeps the stub for tests), and the global-pattern secret redactor
    for the E.5 PII middleware. Per-tenant ``pii_fields`` mask only
    dict-shaped audit details (D.2); free-text LLM messages use the
    global patterns, so ``redact_text`` is a plain sync call — no
    per-tenant lookup.

    Stream G.9 — when ``token_usage_store`` is supplied,
    :class:`TokenUsageMiddleware` lands on the ``after_llm_call`` chain
    of every agent built from this env. ``app.py`` wires the SQL store;
    tests can leave it unset to keep the chain shape unchanged.
    """
    secret_redactor = DefaultSecretRedactor()

    def _redact_text(text: str, _tenant_id: UUID | None) -> str:
        return secret_redactor.redact_text(text)

    return MiddlewareEnv(
        response_cache=LLMResponseCache(redis=InMemoryRedisCache()),
        langfuse_client=(
            langfuse_client if langfuse_client is not None else RecordingLangfuseClient()
        ),
        redact_text=_redact_text,
        token_usage_store=token_usage_store,
    )


def make_agent_runtime(
    secret_store: SecretStore,
    *,
    run_store: RunStore | None = None,
    run_event_store: RunEventStore | None = None,
) -> AgentRuntime:
    """Build the production :class:`AgentRuntime` with an in-memory checkpointer.

    :class:`InMemorySaver` has no async setup / teardown, so it is safe
    to construct here (outside a lifespan context). When
    ``settings.checkpointer_backend`` is ``postgres`` the app lifespan
    opens the durable checkpointer's connection context and swaps
    ``agent_builder`` before any run starts — see ``control_plane.app``.

    ``run_store`` (Mini-ADR J-41) is the durable ``agent_run`` mirror
    the :class:`RunManager` writes every create / status transition to;
    ``None`` keeps the registry purely in-memory.

    ``run_event_store`` (Stream H.3 PR 3 — Mini-ADR H-7) durable-mirrors
    every SSE frame; ``None`` keeps SSE purely in-memory (replay endpoint
    will fall through to live attach only).
    """
    return AgentRuntime(
        run_manager=RunManager(store=run_store),
        stream_bridge=InMemoryStreamBridge(),
        agent_builder=make_agent_builder(secret_store, InMemorySaver()),
        run_event_store=run_event_store,
    )
