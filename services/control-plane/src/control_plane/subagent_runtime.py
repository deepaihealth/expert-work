"""``ChildAgentBuilder`` wiring — Stream J.4 (sub-agent delegation).

The orchestrator's ``SubAgentTool`` delegates to a deployed sub-agent but
cannot resolve an ``agent_ref`` itself — the :class:`AgentSpecStore` lives
here in the control-plane. :func:`make_child_agent_builder` closes over
the spec store and the recursive ``build_agent`` path to produce the
:class:`ChildAgentBuilder` the orchestrator's ``ToolEnv`` carries.
"""

from __future__ import annotations

import logging
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import replace
from typing import Any
from uuid import UUID

import httpx
from langgraph.checkpoint.base import BaseCheckpointSaver

from control_plane.platform_dynamic_worker_config import PlatformDynamicWorkerConfigService
from control_plane.platform_mcp_pool import PlatformMcpPoolProvider
from control_plane.runtime import make_provider_key_resolver, make_skill_resolver
from control_plane.tenancy import TenantConfigService
from control_plane.tenant_mcp_pool import TenantMcpPoolProvider
from control_plane.user_mcp_oauth_pool import UserMcpOAuthPoolProvider
from expert_work.common.credentials import CredentialsResolver
from expert_work.common.skill_activity import SkillActivityRecorder
from expert_work.common.uplift_metrics import set_built_agent_cache_entries
from expert_work.persistence.agent_spec import AgentSpecStore
from expert_work.persistence.skill import SkillStore
from expert_work.protocol import AgentSpec, BuiltinToolSpec, SystemPromptSpec, ToolSpecEntry
from expert_work.runtime.audit.logger import AuditLogger
from expert_work.runtime.secret_store import SecretStore
from expert_work.runtime.skill_assets import ObjectStore as SkillAssetStore
from orchestrator import BuiltAgent, MemoryEnv, MiddlewareEnv, ToolEnv, build_agent
from orchestrator.tools import ChildAgentBuilder
from orchestrator.tools.spawn_worker import WorkerBuildFn

logger = logging.getLogger(__name__)

# Child-agent cache key: (tenant, name, version, depth); a 5th OAuth-subject
# element is appended only for users with a connected OAuth pool (see _build).
_ChildKey = tuple[UUID, str, str, int] | tuple[UUID, str, str, int, str]


def _worker_system_prompt(role: str | None) -> str:
    """Generated system prompt for an ephemeral worker (1.3).

    A fresh, focused worker prompt (Claude Code / hermes shape) — the worker
    sees only this + the delegated task, none of the parent conversation.
    """
    focus = f" Your focus for this task: {role}." if role else ""
    return (
        "You are a worker sub-agent spawned to complete a single, focused subtask "
        "in isolation." + focus + " Do the task fully and return a concise, complete "
        "result as your final message — it is reported straight back to the "
        "orchestrator, which sees none of your intermediate work."
    )


def _without_manage_task(spec: AgentSpec) -> AgentSpec:
    """Strip the ``manage_task`` builtin from a delegated build's spec.

    「子 Agent / worker 不排任务」是既定设计意图(app.py 主构建器注释:
    trigger_store *only the main builder*)。此前的实现方式是「不给子构建器
    trigger_store」—— 但 build_agent 对「spec 声明 manage_task 而无
    TriggerStore」是硬闸,于是父 Agent 带 manage_task 时**整个委派构建**
    直接炸(BUG-19b,真栈 run 8829abdf 三次 spawn_worker 全败于此)。
    意图的正确表达是「子代 spec 里没有这个工具」:LLM 看不到它,构建也
    不再撞闸。spec 未声明 manage_task 时原样返回(缓存/模型身份零扰动)。
    """
    has_it = any(
        isinstance(e, BuiltinToolSpec) and e.name == "manage_task" for e in spec.spec.tools
    )
    if not has_it:
        return spec
    kept = [
        e
        for e in spec.spec.tools
        if not (isinstance(e, BuiltinToolSpec) and e.name == "manage_task")
    ]
    return spec.model_copy(update={"spec": spec.spec.model_copy(update={"tools": kept})})


def _filter_worker_tools(tools: list[ToolSpecEntry], allowed: list[str]) -> list[ToolSpecEntry]:
    """A worker inherits its parent's tools, optionally narrowed by the
    platform allowlist. Empty allowlist = inherit verbatim (still a subset
    of what the parent itself had). A non-empty allowlist keeps only entries
    whose builtin ``name`` or tool ``type`` is listed."""
    if not allowed:
        return list(tools)
    keep: list[ToolSpecEntry] = []
    for t in tools:
        ident = getattr(t, "name", None) or getattr(t, "type", None)
        if ident in allowed:
            keep.append(t)
    return keep


def synthesize_worker_spec(
    parent: AgentSpec,
    *,
    role: str | None,
    max_iterations: int,
    allowed_toolsets: list[str],
) -> AgentSpec:
    """Derive an ephemeral worker :class:`AgentSpec` from ``parent`` (1.3).

    Inherits the parent's model + sandbox isolation + tenant_config +
    defenses (the security boundary is NOT relaxed) — unless the manifest's
    ``dynamic_workers.model`` overrides the worker LLM (full knob set, no
    fallback chain; the protocol validator enforces that). Replaces the
    system prompt with a generated worker prompt, narrows tools to the
    platform allowlist, clamps iterations to the platform cap, and strips
    stateful / delegation blocks (memory / triggers / skills / static
    subagents / reflection / routing / knowledge) — the worker is stateless
    and ephemeral. ``dynamic_workers`` stays default-on so a worker may
    itself spawn while below the depth cap (a grand-worker inherits the
    same override).
    """
    body = parent.spec
    worker_body = body.model_copy(
        update={
            **(
                {"model": body.dynamic_workers.model}
                if body.dynamic_workers.model is not None
                else {}
            ),
            "system_prompt": SystemPromptSpec(template=_worker_system_prompt(role)),
            # BUG-19b —— 与下面剥 triggers **块**同理由,连 manage_task **工具**
            # 一起剥:worker 无 TriggerStore,留着必撞 build_agent 硬闸。
            "tools": [
                t
                for t in _filter_worker_tools(body.tools, allowed_toolsets)
                if not (isinstance(t, BuiltinToolSpec) and t.name == "manage_task")
            ],
            "subagents": [],
            "memory": None,
            "triggers": [],
            "skills": [],
            "reflection": None,
            "routing": None,
            "knowledge": None,
            "workflow": body.workflow.model_copy(
                update={"max_iterations": min(body.workflow.max_iterations, max_iterations)}
            ),
        }
    )
    worker_meta = parent.metadata.model_copy(update={"name": f"{parent.metadata.name}-worker"})
    return parent.model_copy(update={"metadata": worker_meta, "spec": worker_body})


async def _resolve_worker_max_iterations(
    service: PlatformDynamicWorkerConfigService | None, fallback: int
) -> int:
    """Per-build effective worker iteration cap — DB-wins-over-env (B3 PR2)."""
    if service is None:
        return fallback
    return (await service.effective()).max_iterations


class SubAgentNotFoundError(Exception):
    """Raised when a ``SubAgentTool``'s ``agent_ref`` does not resolve to a
    deployed, non-deleted AgentSpec in the tenant.

    The orchestrator's tools node wraps it into a ``ToolMessage`` error
    (Mini-ADR E-12) — a dangling ``agent_ref`` fails that one delegation,
    not the whole parent run.
    """

    def __init__(self, *, tenant_id: UUID, name: str, version: str) -> None:
        super().__init__(
            f"sub-agent not found: tenant_id={tenant_id} name={name!r} version={version!r}"
        )
        self.tenant_id = tenant_id
        self.name = name
        self.version = version


def make_child_agent_builder(
    *,
    spec_store: AgentSpecStore,
    secret_store: SecretStore,
    checkpointer: BaseCheckpointSaver[Any],
    base_tool_env: ToolEnv,
    middleware_env: MiddlewareEnv | None = None,
    memory_env: MemoryEnv | None = None,
    credentials_resolver: CredentialsResolver | None = None,
    tenant_mcp_pool_provider: TenantMcpPoolProvider | None = None,
    platform_mcp_pool_provider: PlatformMcpPoolProvider | None = None,
    user_mcp_oauth_pool_provider: UserMcpOAuthPoolProvider | None = None,
    skill_store: SkillStore | None = None,
    # BUG-19b —— 子代的技能创作工具(#1302 起可构建)此前在无审计状态下跑。
    audit_logger: AuditLogger | None = None,
    # skill-asset-store — dual-read for externalized skill supporting files.
    skill_asset_store: SkillAssetStore | None = None,
    skill_activity_recorder: SkillActivityRecorder | None = None,
    tenant_config_service: TenantConfigService | None = None,
    register_invalidation: Callable[[Callable[[UUID], None]], None] | None = None,
    register_invalidation_all: Callable[[Callable[[], None]], None] | None = None,
    register_user_invalidation: Callable[[Callable[[UUID, str], None]], None] | None = None,
    # 一期 Task 5 — process-level shared HTTP client, forwarded into every
    # delegated child build's ``build_agent`` call. ``None`` keeps every LLM
    # provider client on its original per-call ``httpx.AsyncClient``.
    http_client: httpx.AsyncClient | None = None,
    # 二期 PR2 T4 — child-cache bounds: LRU capacity + flat TTL, plus the
    # injectable monotonic time source (tests). Mirrors AgentRuntime._cache.
    cache_max_size: int = 256,
    cache_ttl_s: float = 1800.0,
    clock: Callable[[], float] = time.monotonic,
) -> ChildAgentBuilder:
    """Build the :class:`ChildAgentBuilder` the orchestrator's ``ToolEnv`` carries.

    The returned callback resolves an ``agent_ref`` through ``spec_store``,
    recursively builds the sub-agent at ``subagent_depth=depth``, and
    caches the result keyed on ``(tenant_id, name, version, depth)``. The
    cache key includes ``depth`` because the same manifest builds a
    *different* graph at different depths — an agent built at
    ``MAX_SUBAGENT_DEPTH`` carries no further ``SubAgentTool``\\s.

    The sub-agent's own ``ToolEnv`` carries this same builder, so a child
    can delegate to a grandchild; the recursion is bounded by the
    build-time depth cap, not by this wiring.

    The returned callback raises :class:`SubAgentNotFoundError` for an
    unresolvable ``agent_ref`` — the orchestrator turns that into a tool
    error rather than crashing the parent run.
    """
    # Key: (tenant, name, version, depth), extended with the OAuth subject ONLY
    # when that user has ≥1 connected OAuth connector (mirrors the top-level
    # AgentRuntime cache) — so the common no-OAuth child stays shared and only
    # OAuth users get a per-user child build (never cross-user pool sharing).
    # 二期 PR2 T4 — bounded LRU; the value carries its ``expires_at``. The TTL
    # only backstops invalidation paths that were never wired; explicit
    # invalidation (the registered hooks below) stays the primary path.
    cache: OrderedDict[_ChildKey, tuple[BuiltAgent, float]] = OrderedDict()

    def _publish_cache_size() -> None:
        """Refresh the subagent-scope cache size gauge (二期 PR2 T4)."""
        set_built_agent_cache_entries(scope="subagent", count=len(cache))

    async def _build(
        *, tenant_id: UUID, name: str, version: str, depth: int, oauth_user_id: str | None = None
    ) -> BuiltAgent:
        # Resolve the caller's OAuth pool up front so the cache key can reflect it.
        user_pool = None
        if user_mcp_oauth_pool_provider is not None and oauth_user_id is not None:
            candidate = await user_mcp_oauth_pool_provider(tenant_id, oauth_user_id)
            if candidate.names():
                user_pool = candidate
        key: _ChildKey = (
            (tenant_id, name, version, depth, oauth_user_id)
            if user_pool is not None and oauth_user_id is not None
            else (tenant_id, name, version, depth)
        )
        cached = cache.get(key)
        if cached is not None:
            cached_built, expires_at = cached
            if clock() < expires_at:
                cache.move_to_end(key)
                return cached_built
            # Expired — drop and treat as a miss. Pure abandonment: the
            # BuiltAgent may hold live MCP connections (MCPServerPool) still
            # in use by an in-flight run, so never close() here; dropping the
            # reference leaves cleanup to GC, matching the invalidators'
            # behavior. No hook semantics apply — natural expiry is not a
            # config change (二期 PR2 T4).
            del cache[key]
            _publish_cache_size()
        record = await spec_store.get(tenant_id=tenant_id, name=name, version=version)
        if record is None:
            raise SubAgentNotFoundError(tenant_id=tenant_id, name=name, version=version)
        provider_key_resolver = (
            make_provider_key_resolver(resolver=credentials_resolver, tenant_id=tenant_id)
            if credentials_resolver is not None
            else None
        )
        # Stream X (Mini-ADR X-4) — sub-agents resolve skills too; a child
        # whose manifest declares skills would otherwise hard-fail at build.
        skill_resolver = (
            make_skill_resolver(store=skill_store, tenant_config_service=tenant_config_service)
            if skill_store is not None and tenant_config_service is not None
            else None
        )
        # Stream V (Mini-ADR V-4) — attach the tenant's own remote MCP pool
        # per-call so delegated sub-agents can also use tenant MCP servers.
        call_tool_env = child_tool_env
        # Stream MCP platform-servers (P1b) — delegated sub-agents see the same
        # platform-curated shared catalog servers as the parent.
        if platform_mcp_pool_provider is not None:
            platform_pool = await platform_mcp_pool_provider()
            if platform_pool.names():
                call_tool_env = replace(call_tool_env, platform_mcp_pool=platform_pool)
        if tenant_mcp_pool_provider is not None:
            tenant_pool = await tenant_mcp_pool_provider(tenant_id)
            if tenant_pool.names():
                call_tool_env = replace(call_tool_env, tenant_mcp_pool=tenant_pool)
        # MCP-OAUTH (OA-3b-后续) — inject the caller's per-user OAuth pool so the
        # delegated child resolves the SAME OAuth-connected MCP servers as the
        # parent (resolved above for the cache key).
        if user_pool is not None:
            call_tool_env = replace(call_tool_env, user_mcp_oauth_pool=user_pool)
        built = await build_agent(
            # BUG-19b —— 子 Agent 不排任务(既定意图):spec 层剥 manage_task,
            # 而不是让缺席的 TriggerStore 炸掉整个委派构建。
            _without_manage_task(record.spec),
            secret_store=secret_store,
            checkpointer=checkpointer,
            tool_env=call_tool_env,
            middleware_env=middleware_env,
            memory_env=memory_env,
            subagent_depth=depth,
            tenant_id=tenant_id,
            provider_key_resolver=provider_key_resolver,
            skill_resolver=skill_resolver,
            audit_logger=audit_logger,
            # skill_store 此前漏传(只喂了 skill_resolver)——凡 spec 声明技能
            # 创作 builtin(remember/author_skill 系,现网 Agent 全带)的委派
            # 构建一律死在 agent_factory 的 skill_store 硬闸,全平台委派从未
            # 真正跑通过(2026-08-26 首次真栈触发即撞上)。与主构建路径
            # (runtime.make_agent_builder)对齐。
            skill_store=skill_store,
            skill_asset_store=skill_asset_store,
            skill_activity_recorder=skill_activity_recorder,
            http_client=http_client,
        )
        cache[key] = (built, clock() + cache_ttl_s)
        cache.move_to_end(key)
        while len(cache) > cache_max_size:
            # LRU eviction — pure abandonment, no close (same rationale as
            # the expiry branch above).
            cache.popitem(last=False)
        _publish_cache_size()
        logger.info(
            "control_plane.subagent.built name=%s version=%s depth=%d",
            name,
            version,
            depth,
        )
        return built

    def _invalidate_tenant(tenant_id: UUID) -> None:
        """Drop cached sub-agents for a tenant (Stream V-D, audit #1).

        Registered with the :class:`AgentRuntime` so a tenant's MCP registry
        change evicts stale delegated sub-agents (whose ``ToolEnv`` holds a
        now-closed tenant MCP pool), mirroring the top-level cache.
        """
        for key in [k for k in cache if k[0] == tenant_id]:
            del cache[key]
        _publish_cache_size()

    def _invalidate_all() -> None:
        """Drop every cached sub-agent (P1b).

        Registered with the :class:`AgentRuntime` so a process-global pool
        change (the platform shared catalog) evicts stale delegated sub-agents
        across all tenants, mirroring the top-level cache.
        """
        cache.clear()
        _publish_cache_size()

    def _invalidate_user(tenant_id: UUID, user_id: str) -> None:
        """Drop cached per-user (OAuth) sub-agents for ``(tenant, user)``
        (二期 PR2 T2).

        Registered with the :class:`AgentRuntime` so a user's OAuth token
        refresh / disconnect evicts stale delegated child builds (whose
        ``ToolEnv`` holds the old per-user OAuth pool). Only 5-tuple keys
        match — index 4 is the OAuth subject (see ``_ChildKey``); shared
        4-tuple builds and other users' entries are left intact.
        """
        for key in [k for k in cache if len(k) == 5 and k[0] == tenant_id and k[4] == user_id]:
            del cache[key]
        _publish_cache_size()

    if register_invalidation is not None:
        register_invalidation(_invalidate_tenant)
    if register_invalidation_all is not None:
        register_invalidation_all(_invalidate_all)
    if register_user_invalidation is not None:
        register_user_invalidation(_invalidate_user)

    # The sub-agent's ToolEnv carries _build itself so a child can in turn
    # delegate to a grandchild. Assigned after _build is defined; the
    # closure reads it only at call time, by which point it is bound.
    child_tool_env = replace(base_tool_env, child_agent_builder=_build)
    return _build


def make_worker_build_fn(
    *,
    secret_store: SecretStore,
    checkpointer: BaseCheckpointSaver[Any],
    base_tool_env: ToolEnv,
    max_iterations: int,
    allowed_toolsets: list[str],
    middleware_env: MiddlewareEnv | None = None,
    memory_env: MemoryEnv | None = None,
    credentials_resolver: CredentialsResolver | None = None,
    tenant_mcp_pool_provider: TenantMcpPoolProvider | None = None,
    platform_mcp_pool_provider: PlatformMcpPoolProvider | None = None,
    user_mcp_oauth_pool_provider: UserMcpOAuthPoolProvider | None = None,
    skill_store: SkillStore | None = None,
    # BUG-19b —— 子代的技能创作工具(#1302 起可构建)此前在无审计状态下跑。
    audit_logger: AuditLogger | None = None,
    # skill-asset-store — dual-read for externalized skill supporting files.
    skill_asset_store: SkillAssetStore | None = None,
    skill_activity_recorder: SkillActivityRecorder | None = None,
    tenant_config_service: TenantConfigService | None = None,
    dynamic_worker_config_service: PlatformDynamicWorkerConfigService | None = None,
    # 一期 Task 5 — process-level shared HTTP client, forwarded into every
    # spawned worker's ``build_agent`` call. ``None`` keeps every LLM
    # provider client on its original per-call ``httpx.AsyncClient``.
    http_client: httpx.AsyncClient | None = None,
) -> WorkerBuildFn:
    """Build the :class:`WorkerBuildFn` the orchestrator's ``ToolEnv`` carries
    for the ``spawn_worker`` tool (1.3 dynamic Orchestrator-Worker).

    Mirrors :func:`make_child_agent_builder` but **synthesizes** the worker
    spec from the parent (:func:`synthesize_worker_spec`) instead of resolving
    a deployed ``agent_ref`` — there is no store lookup, the worker is
    ephemeral. The build reuses the same plumbing (provider key / skill
    resolvers, tenant MCP pool, ``build_agent`` at ``subagent_depth=depth``).
    Not cached: each worker carries a per-call role/prompt.

    ``dynamic_worker_config_service`` (B3 PR2) — when set, ``max_iterations``
    is re-read from the live platform config on EVERY build via
    :func:`_resolve_worker_max_iterations`, so a config change is hot (the
    ``max_iterations`` param stays the boot-time fallback). ``None`` keeps
    every worker clamped to the ``max_iterations`` param, unchanged.
    """

    async def _build(
        parent_spec: AgentSpec,
        *,
        tenant_id: UUID,
        role: str | None,
        depth: int,
        oauth_user_id: str | None = None,
    ) -> BuiltAgent:
        worker_spec = synthesize_worker_spec(
            parent_spec,
            role=role,
            max_iterations=await _resolve_worker_max_iterations(
                dynamic_worker_config_service, max_iterations
            ),
            allowed_toolsets=allowed_toolsets,
        )
        provider_key_resolver = (
            make_provider_key_resolver(resolver=credentials_resolver, tenant_id=tenant_id)
            if credentials_resolver is not None
            else None
        )
        skill_resolver = (
            make_skill_resolver(store=skill_store, tenant_config_service=tenant_config_service)
            if skill_store is not None and tenant_config_service is not None
            else None
        )
        call_tool_env = worker_tool_env
        # Stream MCP platform-servers (P1b) — workers see the platform-curated
        # shared catalog servers too.
        if platform_mcp_pool_provider is not None:
            platform_pool = await platform_mcp_pool_provider()
            if platform_pool.names():
                call_tool_env = replace(call_tool_env, platform_mcp_pool=platform_pool)
        if tenant_mcp_pool_provider is not None:
            tenant_pool = await tenant_mcp_pool_provider(tenant_id)
            if tenant_pool.names():
                call_tool_env = replace(call_tool_env, tenant_mcp_pool=tenant_pool)
        # MCP-OAUTH (OA-3b-后续) — the worker inherits the caller's per-user OAuth
        # pool (workers aren't cached, so resolve + inject inline).
        if user_mcp_oauth_pool_provider is not None and oauth_user_id is not None:
            user_pool = await user_mcp_oauth_pool_provider(tenant_id, oauth_user_id)
            if user_pool.names():
                call_tool_env = replace(call_tool_env, user_mcp_oauth_pool=user_pool)
        built = await build_agent(
            worker_spec,
            secret_store=secret_store,
            checkpointer=checkpointer,
            tool_env=call_tool_env,
            middleware_env=middleware_env,
            memory_env=memory_env,
            subagent_depth=depth,
            tenant_id=tenant_id,
            provider_key_resolver=provider_key_resolver,
            skill_resolver=skill_resolver,
            audit_logger=audit_logger,
            # 同 make_child_agent_builder 的修注:skill_store 漏传把带技能创作
            # builtin 的父 Agent 的 spawn_worker 全数变成构建期 AgentFactoryError
            # (worker 继承父 spec,闸在 worker 构建时同样触发)。
            skill_store=skill_store,
            skill_asset_store=skill_asset_store,
            skill_activity_recorder=skill_activity_recorder,
            http_client=http_client,
        )
        logger.info("control_plane.worker.built role=%s depth=%d", role or "general", depth)
        return built

    # The worker's own ToolEnv carries this same build_fn so a worker can in
    # turn spawn a grandchild worker (bounded by the depth cap).
    worker_tool_env = replace(base_tool_env, worker_build_fn=_build)
    return _build
