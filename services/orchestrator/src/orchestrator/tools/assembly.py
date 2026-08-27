"""Assemble a :class:`ToolRegistry` from a manifest's ``tools:`` block.

STREAM-E-DESIGN Mini-ADR E-14: the manifest declares tools as a
``type``-discriminated union (:data:`expert_work.protocol.ToolSpecEntry`).
:func:`build_tool_registry` maps each declaration to a concrete adapter
and registers it.

Platform runtime deps — the Tavily client, the per-tenant HTTP
allowlist provider, the MCP server pool — are *not* in the manifest
(they are tenant-/platform-scoped, Mini-ADR E-14). They are injected
via :class:`ToolEnv`. A manifest that declares a tool whose backing
dep is absent from the ``ToolEnv`` raises :class:`AgentFactoryError`,
so the failure surfaces at build time, not on the first tool call.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from expert_work.persistence import ArtifactStore
from expert_work.protocol import (
    AgentSpec,
    BuiltinToolSpec,
    DynamicWorkersSpec,
    HTTPToolSpec,
    KnowledgeSpec,
    MCPToolSpec,
    SubAgentSpec,
    ToolSpecEntry,
    VisionSpec,
)
from expert_work.runtime.tokens import default_estimator
from orchestrator.errors import AgentFactoryError
from orchestrator.multimodal import ImageResolver
from orchestrator.tools.approval import AskForApprovalTool
from orchestrator.tools.artifact import ListArtifactsTool, SaveArtifactTool
from orchestrator.tools.bash import BashTool
from orchestrator.tools.file_ops import (
    EditFileTool,
    ListDirTool,
    ReadFileTool,
    WriteFileTool,
)
from orchestrator.tools.find_tools import FindToolsTool
from orchestrator.tools.http import AllowlistProvider, DenylistProvider, HTTPTool
from orchestrator.tools.knowledge import KnowledgeRetriever, KnowledgeSearchTool
from orchestrator.tools.locks import NullWorkspaceLock, WorkspaceLock
from orchestrator.tools.mcp import MCPServerPool, register_mcp_tools
from orchestrator.tools.read_document import ReadDocumentTool
from orchestrator.tools.registry import ToolRegistry
from orchestrator.tools.sandbox import ExecPythonTool, SandboxRuntime
from orchestrator.tools.skill_authoring import SKILL_AUTHORING_BUILTINS
from orchestrator.tools.spawn_worker import SpawnWorkerTool, WorkerBuildFn
from orchestrator.tools.subagent import MAX_SUBAGENT_DEPTH, ChildAgentBuilder, SubAgentTool
from orchestrator.tools.vision import AskImageTool
from orchestrator.tools.web_search import DEFAULT_MAX_RESULTS, TavilyClient, WebSearchTool
from orchestrator.trajectory import TrajectoryRecorder

if TYPE_CHECKING:
    from uuid import UUID

    # Imported under TYPE_CHECKING only to avoid an ``llm → tools`` cycle.
    from orchestrator.built_agent import BuiltAgent
    from orchestrator.llm import LLMCaller

logger = logging.getLogger(__name__)

#: Built-in tool names the platform ships in M0.
KNOWN_BUILTINS = frozenset(
    {
        "web_search",
        "exec_python",
        "bash",
        "read_file",
        "write_file",
        "edit_file",
        "list_dir",
        "read_document",
        "save_artifact",
        "list_artifacts",
        "ask_for_approval",
        # Stream SE (SE-3b) — in-session skill authoring (Layer A). Registered
        # in ``agent_factory.build_agent`` (it alone has agent_name + the
        # SkillStore); ``_register_builtin`` treats them as no-ops.
        "author_skill",
        "refine_skill",
        "fork_skill",
        "propose_skill_to_tenant",
        # Stream SE (SE-10) — in-session text-class harness component authoring.
        "note_behavior_patch",
        "clarify_tool_usage",
        "remember",
        # Spec 1 PR2 — conversational scheduled tasks. Registered in
        # ``agent_factory.build_agent`` (it has agent_name/version + TriggerStore);
        # ``_register_builtin`` treats it as a no-op.
        "manage_task",
    }
)


@dataclass(frozen=True)
class ToolEnv:
    """Platform runtime deps the assembler draws on.

    Each field backs one tool kind. A field left ``None`` means that
    tool is not available in this deployment — declaring it in a
    manifest raises :class:`AgentFactoryError`. An empty ``ToolEnv()``
    therefore builds a pure-LLM agent and nothing else.
    """

    web_search_client: TavilyClient | None = None
    allowlist_provider: AllowlistProvider | None = None
    #: E.8 — per-tenant HTTP-tool host denylist (blocks specific hosts even
    #: under the default allow-all-public). ``None`` ↔ nothing denied.
    denylist_provider: DenylistProvider | None = None
    mcp_pool: MCPServerPool | None = None
    #: Stream O (Mini-ADR O-14) — per-tenant MCP server allowlist. Empty
    #: (the default) means no restriction: the agent sees every server in
    #: ``mcp_pool``. Non-empty restricts the agent to the listed server
    #: names (others in the platform pool stay hidden from this tenant).
    #: Set per-tenant by the control-plane's agent builder from
    #: ``tenant_config.mcp_allowlist``; bypasses no platform-server cap.
    mcp_allowlist: tuple[str, ...] = ()
    #: Stream V (Mini-ADR V-4) — the calling tenant's own registered REMOTE
    #: MCP servers (sse / streamable_http), built per-tenant by the control
    #: plane from ``tenant_mcp_server`` + the encrypted secret store. Unlike
    #: ``mcp_pool`` (the operator-controlled platform pool, gated by
    #: ``mcp_allowlist``), this pool is the tenant's own and is never gated by
    #: the allowlist. ``None`` → the tenant registered no remote servers.
    tenant_mcp_pool: MCPServerPool | None = None
    #: Stream MCP-OAUTH (OA-3b) — the calling **user's** OAuth-connected MCP
    #: servers (per-(tenant,user)), built by the control plane from the user's
    #: ``mcp_oauth_connection`` rows. Like ``tenant_mcp_pool`` it is the
    #: caller's own and never gated by the allowlist; ``None`` → the user has no
    #: connected OAuth connectors.
    user_mcp_oauth_pool: MCPServerPool | None = None
    #: Stream MCP platform-servers (P1b) — the platform-curated SHARED MCP
    #: servers (``none``/``bearer`` catalog rows, a ``bearer`` row carrying the
    #: platform's own token), built process-globally by the control plane from
    #: ``mcp_connector_catalog``. Unlike ``mcp_pool`` (operator file pool, empty
    #: allowlist = all), this pool is **opt-in**: a tenant uses a shared server
    #: only after enabling it (name in ``mcp_allowlist``), so an empty allowlist
    #: = none (Stream MCP P2). ``None`` → no shared catalog servers configured.
    platform_mcp_pool: MCPServerPool | None = None
    #: Sandbox runtime backing the ``exec_python`` builtin (F.4).
    sandbox_runtime: SandboxRuntime | None = None
    #: Artifact registry backing the ``save_artifact`` / ``list_artifacts``
    #: builtins (Stream J.9).
    artifact_store: ArtifactStore | None = None
    #: Resolves an ``agent_ref`` and builds the referenced sub-agent —
    #: backs the ``SubAgentTool``\\s a manifest's ``spec.subagents``
    #: block declares (Stream J.4). Injected by the control-plane, which
    #: alone holds the ``AgentSpecStore``. A manifest that declares
    #: ``subagents`` with this left ``None`` raises
    #: :class:`AgentFactoryError` (wired in J.4 PR4).
    child_agent_builder: ChildAgentBuilder | None = None
    #: 1.3 Orchestrator-Worker — synthesizes + builds an ephemeral worker
    #: from a parent spec, backing the ``spawn_worker`` tool. Injected by the
    #: control-plane (it owns ``build_agent`` + worker-spec synthesis).
    #: ``None`` → no ``spawn_worker`` tool (also how the platform
    #: ``enable_dynamic_workers=False`` switch is expressed).
    worker_build_fn: WorkerBuildFn | None = None
    #: Hybrid knowledge retriever backing the ``knowledge_search`` tool
    #: a manifest's ``knowledge:`` block activates (Stream J.5). Injected
    #: by the control-plane (it configures the embedder / rerank LLM). A
    #: manifest that declares ``knowledge`` with this left ``None`` raises
    #: :class:`AgentFactoryError`.
    knowledge_retriever: KnowledgeRetriever | None = None
    #: Resolves ``image_ref`` content blocks to bytes (Stream J.6). Both
    #: Path A (image into the ``HumanMessage``) and Path B (the
    #: ``ask_image`` tool) draw on it; ``None`` → no image input is
    #: available in this deployment.
    image_resolver: ImageResolver | None = None
    #: Mini-ADR J-21 — when set, sub-agent runs write their own trajectory
    #: under ``{prefix}/{tenant}/{outcome}/{date}/{sub_thread_id}.jsonl``
    #: so J.13 eval can replay every node in a delegation tree. ``None``
    #: keeps sub-agent runs silent — the parent run's own trajectory still
    #: records via the orchestrator's SSE worker.
    trajectory_recorder: TrajectoryRecorder | None = None
    #: Stream TE-8 — cross-replica per-workspace write lock held around
    #: ``write_file`` / ``bash`` writes. Defaults to the no-op
    #: :class:`NullWorkspaceLock` (single process / tests); the control plane
    #: injects a Postgres advisory-lock implementation in production.
    workspace_lock: WorkspaceLock = field(default_factory=NullWorkspaceLock)


async def build_tool_registry(
    tool_specs: Sequence[ToolSpecEntry],
    *,
    tool_env: ToolEnv,
    skill_seed_files: tuple[tuple[str, bytes], ...] = (),
    subagents: Sequence[SubAgentSpec] = (),
    subagent_depth: int = 0,
    parent_spec: AgentSpec | None = None,
    dynamic_workers: DynamicWorkersSpec | None = None,
    knowledge: KnowledgeSpec | None = None,
    vision: VisionSpec | None = None,
    vl_caller: LLMCaller | None = None,
    context_window: int | None = None,
) -> ToolRegistry:
    """Build a :class:`ToolRegistry` from a manifest's ``tools:`` entries.

    Workspace durability is automatic: the sandbox tools acquire against the
    run user's persistent workspace volume whenever the run is user-scoped
    (no manifest opt-in) — see :func:`run_in_sandbox`.

    ``subagents`` is the manifest's ``spec.subagents`` block (Stream J.4);
    each entry becomes a :class:`SubAgentTool`. ``subagent_depth`` is the
    build-time recursion depth of the agent being assembled (0 for the
    top-level agent) — at :data:`MAX_SUBAGENT_DEPTH` no ``SubAgentTool``
    is registered, so a delegation chain terminates structurally.

    ``knowledge`` is the manifest's ``spec.knowledge`` block (Stream J.5);
    its presence activates the ``knowledge_search`` tool.

    ``vision`` is the manifest's ``spec.vision`` block (Stream J.6 Path B);
    its presence activates the ``ask_image`` tool, which routes to the
    declared VL model via ``vl_caller``.

    ``context_window`` (Stream HX-12) feeds the small-pool escape hatch:
    when the deferred (MCP) pool's total schema size fits comfortably in
    context, defer is pointless overhead and every tool registers active.

    :raises AgentFactoryError: an entry names an unknown builtin, declares
        a tool whose ``ToolEnv`` dependency is not configured, declares
        ``subagents`` with no ``ToolEnv.child_agent_builder``, declares
        ``knowledge`` with no ``ToolEnv.knowledge_retriever``, or declares
        ``vision`` with no ``ToolEnv.image_resolver`` / ``vl_caller``.
    """
    registry = ToolRegistry()
    for entry in tool_specs:
        if isinstance(entry, BuiltinToolSpec):
            _register_builtin(registry, entry, tool_env, skill_seed_files)
        elif isinstance(entry, HTTPToolSpec):
            _register_http(registry, tool_env)
        elif isinstance(entry, MCPToolSpec):
            await _register_mcp(registry, entry, tool_env)
    _register_base_capabilities(registry, tool_env, skill_seed_files)
    _register_subagents(registry, subagents, tool_env, subagent_depth)
    _register_spawn_worker(registry, tool_env, parent_spec, dynamic_workers, subagent_depth)
    _register_knowledge_search(registry, knowledge, tool_env)
    _register_ask_image(registry, vision, tool_env, vl_caller)
    # Stream HX-12 (Mini-ADR HX-I3) — small-pool escape hatch: when the
    # whole deferred pool fits comfortably in context, defer is pure
    # overhead (a find_tools round-trip per capability); activate it all.
    _maybe_activate_small_deferred_pool(registry, context_window)
    # Stream TE-6b — MCP tools register deferred (deer-flow's always-defer-MCP
    # policy, the Context-Bloat fix). Add the ``find_tools`` meta-tool so the
    # model can retrieve them on demand — but only when there IS something
    # deferred, so a no-MCP agent's tool set is byte-identical to pre-TE-6.
    # ``find_tools`` is registered active (never deferred), so the discovery
    # entry point is always reachable.
    if registry.has_deferred():
        registry.register(FindToolsTool(registry=registry))
    return registry


#: Stream HX-12 — absolute ceiling for the escape hatch, independent of the
#: context window (10% of a 1M-window model would still be 100k tokens of
#: schemas — far past "comfortable"). Constant by design; parameterize only
#: when a real deployment needs it.
_ESCAPE_HATCH_TOKEN_CAP = 20_000
_ESCAPE_HATCH_WINDOW_FRACTION = 0.10


def _maybe_activate_small_deferred_pool(registry: ToolRegistry, context_window: int | None) -> None:
    """Un-defer the whole pool when its schemas fit comfortably in context.

    Threshold: ``min(context_window x 10%, 20k)`` tokens, measured with the
    HX-1 estimator over each tool's name + description + parameter schema.
    Any failure keeps the always-defer status quo (fail-open to the
    behaviour-unchanged side).
    """
    if context_window is None:
        # No window means the caller didn't opt in (legacy call sites,
        # tests) — keep the TE-6b always-defer behaviour byte-identical.
        return
    names = registry.deferred_names()
    if not names:
        return
    threshold = min(_ESCAPE_HATCH_TOKEN_CAP, int(context_window * _ESCAPE_HATCH_WINDOW_FRACTION))
    try:
        estimator = default_estimator()
        total = 0
        for name in names:
            tool = registry.get(name)
            if tool is None:  # pragma: no cover - names came from the registry
                return
            payload = json.dumps(
                {
                    "name": tool.spec.name,
                    "description": tool.spec.description,
                    "parameters": dict(tool.spec.parameters),
                },
                ensure_ascii=False,
            )
            total += estimator.count(payload)
            if total >= threshold:
                return  # over budget — keep the deferred pool as is
    except Exception:
        logger.warning("tool_escape_hatch.estimate_failed", exc_info=True)
        return
    for name in names:
        tool = registry.get(name)
        if tool is not None:
            registry.register(tool)  # re-register active (un-defers; source kept)
    logger.info(
        "tool_escape_hatch.activated tools=%d tokens=%d threshold=%d",
        len(names),
        total,
        threshold,
    )


def _register_knowledge_search(
    registry: ToolRegistry, knowledge: KnowledgeSpec | None, env: ToolEnv
) -> None:
    """Register the ``knowledge_search`` tool when the manifest declares a
    ``knowledge:`` block — Stream J.5. A declared block with no
    :attr:`ToolEnv.knowledge_retriever` is an un-buildable manifest."""
    if knowledge is None:
        return
    if env.knowledge_retriever is None:
        raise AgentFactoryError(
            "manifest declares 'knowledge' but no knowledge retriever is "
            "configured (ToolEnv.knowledge_retriever)"
        )
    registry.register(
        KnowledgeSearchTool(
            retriever=env.knowledge_retriever,
            knowledge_base_refs=tuple(knowledge.knowledge_base_refs),
        )
    )


def _register_ask_image(
    registry: ToolRegistry,
    vision: VisionSpec | None,
    env: ToolEnv,
    vl_caller: LLMCaller | None,
) -> None:
    """Register the ``ask_image`` tool when the manifest declares a
    ``vision:`` block — Stream J.6 Path B. A declared block missing
    either the image resolver or the VL caller is an un-buildable
    manifest."""
    if vision is None:
        return
    if env.image_resolver is None:
        raise AgentFactoryError(
            "manifest declares 'vision' but no image resolver is configured "
            "(ToolEnv.image_resolver)"
        )
    if vl_caller is None:
        raise AgentFactoryError(
            "manifest declares 'vision' but no VL llm_caller was built — "
            "this is an agent-factory bug, not a manifest defect"
        )
    registry.register(AskImageTool(vl_caller=vl_caller, image_resolver=env.image_resolver))


def _register_subagents(
    registry: ToolRegistry,
    subagents: Sequence[SubAgentSpec],
    env: ToolEnv,
    subagent_depth: int,
) -> None:
    """Register one :class:`SubAgentTool` per declared sub-agent — Stream J.4.

    At :data:`MAX_SUBAGENT_DEPTH` nothing is registered (a warning, not an
    error): the agent still runs, it just cannot delegate further — this
    is the structural recursion guard (Mini-ADR J-12). Below the cap, a
    declared ``subagents`` block with no
    :attr:`ToolEnv.child_agent_builder` is an un-buildable manifest.
    """
    if not subagents:
        return
    if subagent_depth >= MAX_SUBAGENT_DEPTH:
        logger.warning(
            "tools.subagent_depth_cap depth=%d not_registered=%d",
            subagent_depth,
            len(subagents),
        )
        return
    if env.child_agent_builder is None:
        raise AgentFactoryError(
            "manifest declares 'subagents' but no sub-agent builder is "
            "configured (ToolEnv.child_agent_builder)"
        )
    child_depth = subagent_depth + 1
    for sub in subagents:
        registry.register(
            SubAgentTool(
                subagent=sub,
                builder=env.child_agent_builder,
                child_depth=child_depth,
                trajectory_recorder=env.trajectory_recorder,
            )
        )


def _register_spawn_worker(
    registry: ToolRegistry,
    env: ToolEnv,
    parent_spec: AgentSpec | None,
    dynamic_workers: DynamicWorkersSpec | None,
    subagent_depth: int,
) -> None:
    """Register the ``spawn_worker`` tool — 1.3 dynamic Orchestrator-Worker.

    Gated by: the per-agent opt-out (``dynamic_workers.enabled``), a wired
    worker builder (``ToolEnv.worker_build_fn`` — ``None`` when the platform
    ``enable_dynamic_workers`` switch is off or in tests), and the same
    structural depth cap as static sub-agents (a worker built at
    :data:`MAX_SUBAGENT_DEPTH` carries no further spawn tool). Unlike
    ``subagents``, a missing builder is **not** an error — the feature is
    default-on but optional, so an unwired deployment simply runs without it.
    """
    if dynamic_workers is not None and not dynamic_workers.enabled:
        return
    if env.worker_build_fn is None or parent_spec is None:
        return
    if subagent_depth >= MAX_SUBAGENT_DEPTH:
        logger.warning("tools.spawn_worker_depth_cap depth=%d not_registered", subagent_depth)
        return
    build_fn = env.worker_build_fn
    child_depth = subagent_depth + 1

    async def _builder(
        *, tenant_id: UUID, role: str | None, depth: int, oauth_user_id: str | None = None
    ) -> BuiltAgent:
        return await build_fn(
            parent_spec,
            tenant_id=tenant_id,
            role=role,
            depth=depth,
            oauth_user_id=oauth_user_id,
        )

    registry.register(
        SpawnWorkerTool(
            builder=_builder,
            child_depth=child_depth,
            trajectory_recorder=env.trajectory_recorder,
        )
    )


def _register_builtin(
    registry: ToolRegistry,
    entry: BuiltinToolSpec,
    env: ToolEnv,
    skill_seed_files: tuple[tuple[str, bytes], ...],
) -> None:
    if entry.name not in KNOWN_BUILTINS:
        raise AgentFactoryError(
            f"unknown builtin tool {entry.name!r} (known: {sorted(KNOWN_BUILTINS)})"
        )
    if entry.name == "web_search":
        _register_web_search(registry, entry, env)
    elif entry.name == "exec_python":
        _register_exec_python(registry, env, skill_seed_files)
    elif entry.name == "bash":
        _register_bash(registry, env, skill_seed_files)
    elif entry.name in ("read_file", "write_file", "edit_file", "list_dir"):
        _register_file_op(registry, entry.name, env, skill_seed_files)
    elif entry.name == "read_document":
        _register_read_document(registry, env, skill_seed_files)
    elif entry.name == "save_artifact":
        registry.register(SaveArtifactTool(store=_require_artifact_store(env, "save_artifact")))
    elif entry.name == "list_artifacts":
        registry.register(ListArtifactsTool(store=_require_artifact_store(env, "list_artifacts")))
    elif entry.name == "ask_for_approval":
        # Stream J.8 — zero-dependency builtin; ``tools_node`` intercepts
        # the call before dispatch (see graph_builder/_approval.py).
        registry.register(AskForApprovalTool())
    elif entry.name in SKILL_AUTHORING_BUILTINS:
        # Stream SE (SE-3b) — registered in ``agent_factory.build_agent``
        # (it has agent_name + the SkillStore); no-op here.
        pass
    elif entry.name == "manage_task":
        # Spec 1 PR2 — registered in build_agent (store + agent_name/version there).
        pass


#: Tier 1 base capabilities — code execution + file + artifact tooling that
#: EVERY agent gets, regardless of the manifest ``tools:`` list. A complete
#: agent is not a per-manifest opt-in: without these it degrades to pure
#: question-answer. The sandbox (gVisor + per-tenant isolation + egress proxy
#: + audit) is the security boundary, so always-on code execution merely
#: realises the sandbox's purpose; the governance counterweight is the
#: declarative approval gate (``policies.approval_required_tools``), which can
#: require a human verdict before a tool runs without removing the capability.
#: See docs/design/agent-base-capabilities-and-form.md.
BASE_CAPABILITY_BUILTINS: tuple[str, ...] = (
    "exec_python",
    "bash",
    "read_file",
    "write_file",
    "edit_file",
    "list_dir",
    "read_document",
    "save_artifact",
    "list_artifacts",
)


def _register_base_capabilities(
    registry: ToolRegistry,
    env: ToolEnv,
    skill_seed_files: tuple[tuple[str, bytes], ...],
) -> None:
    """Register the Tier 1 base capabilities the manifest did not declare.

    Each builtin registers only when (a) it is not already present (an
    explicit manifest declaration wins and is not double-registered) and
    (b) its :class:`ToolEnv` dependency is wired. Production always wires
    ``sandbox_runtime`` + ``artifact_store`` (see control-plane app.py),
    so every real agent gets the full set; a deployment lacking them is a
    platform-level choice (no sandbox at all), not a per-agent off switch —
    the implicit set is silently skipped rather than raising, preserving the
    "empty :class:`ToolEnv` builds a pure-LLM agent" invariant for tests. The
    fail-loud raise stays reserved for an EXPLICIT manifest declaration whose
    dependency is missing (a real misconfiguration).
    """
    for name in BASE_CAPABILITY_BUILTINS:
        if registry.get(name) is not None:
            continue  # explicit manifest entry already registered it
        if name in ("save_artifact", "list_artifacts"):
            if env.artifact_store is None:
                continue
        elif env.sandbox_runtime is None:
            continue
        _register_builtin(registry, BuiltinToolSpec(name=name), env, skill_seed_files)


def _register_web_search(registry: ToolRegistry, entry: BuiltinToolSpec, env: ToolEnv) -> None:
    if env.web_search_client is None:
        raise AgentFactoryError(
            "builtin 'web_search' declared but no Tavily client is "
            "configured (ToolEnv.web_search_client)"
        )
    max_results = int(entry.config.get("max_results", DEFAULT_MAX_RESULTS))
    registry.register(WebSearchTool(client=env.web_search_client, default_max_results=max_results))


def _register_exec_python(
    registry: ToolRegistry,
    env: ToolEnv,
    skill_seed_files: tuple[tuple[str, bytes], ...],
) -> None:
    if env.sandbox_runtime is None:
        raise AgentFactoryError(
            "builtin 'exec_python' declared but no sandbox runtime "
            "is configured (ToolEnv.sandbox_runtime)"
        )
    registry.register(
        ExecPythonTool(
            client=env.sandbox_runtime,
            skill_seed_files=skill_seed_files,
        )
    )


def _register_bash(
    registry: ToolRegistry,
    env: ToolEnv,
    skill_seed_files: tuple[tuple[str, bytes], ...],
) -> None:
    # Stream TE-5 — bash rides the same sandbox runtime as exec_python.
    if env.sandbox_runtime is None:
        raise AgentFactoryError(
            "builtin 'bash' declared but no sandbox runtime is configured (ToolEnv.sandbox_runtime)"
        )
    registry.register(
        BashTool(
            client=env.sandbox_runtime,
            workspace_lock=env.workspace_lock,
            skill_seed_files=skill_seed_files,
        )
    )


def _register_file_op(
    registry: ToolRegistry,
    name: str,
    env: ToolEnv,
    skill_seed_files: tuple[tuple[str, bytes], ...],
) -> None:
    # Stream TE-7 — read_file / write_file / list_dir ride the same sandbox
    # runtime exec channel as bash / exec_python (TE-ADR-2 exec-warm locus).
    if env.sandbox_runtime is None:
        raise AgentFactoryError(
            f"builtin {name!r} declared but no sandbox runtime "
            "is configured (ToolEnv.sandbox_runtime)"
        )
    if name == "read_file":
        registry.register(
            ReadFileTool(
                client=env.sandbox_runtime,
                skill_seed_files=skill_seed_files,
            )
        )
    elif name == "write_file":
        registry.register(
            WriteFileTool(
                client=env.sandbox_runtime,
                workspace_lock=env.workspace_lock,
                skill_seed_files=skill_seed_files,
            )
        )
    elif name == "edit_file":
        registry.register(
            EditFileTool(
                client=env.sandbox_runtime,
                workspace_lock=env.workspace_lock,
                skill_seed_files=skill_seed_files,
            )
        )
    else:  # list_dir
        registry.register(
            ListDirTool(
                client=env.sandbox_runtime,
                skill_seed_files=skill_seed_files,
            )
        )


def _register_read_document(
    registry: ToolRegistry,
    env: ToolEnv,
    skill_seed_files: tuple[tuple[str, bytes], ...],
) -> None:
    # read_document rides the same warm sandbox runtime exec channel as the
    # TE-7 file primitives — the parse runs inside the per-user sandbox.
    if env.sandbox_runtime is None:
        raise AgentFactoryError(
            "builtin 'read_document' declared but no sandbox runtime "
            "is configured (ToolEnv.sandbox_runtime)"
        )
    registry.register(
        ReadDocumentTool(
            client=env.sandbox_runtime,
            skill_seed_files=skill_seed_files,
        )
    )


def _require_artifact_store(env: ToolEnv, tool_name: str) -> ArtifactStore:
    if env.artifact_store is None:
        raise AgentFactoryError(
            f"builtin {tool_name!r} declared but no artifact store is "
            "configured (ToolEnv.artifact_store)"
        )
    return env.artifact_store


def _register_http(registry: ToolRegistry, env: ToolEnv) -> None:
    if env.allowlist_provider is None:
        raise AgentFactoryError(
            "'http' tool declared but no allowlist provider is "
            "configured (ToolEnv.allowlist_provider)"
        )
    registry.register(
        HTTPTool(
            allowlist_provider=env.allowlist_provider,
            denylist_provider=env.denylist_provider,
        )
    )


async def _register_mcp(registry: ToolRegistry, entry: MCPToolSpec, env: ToolEnv) -> None:
    if (
        env.mcp_pool is None
        and env.platform_mcp_pool is None
        and env.tenant_mcp_pool is None
        and env.user_mcp_oauth_pool is None
    ):
        raise AgentFactoryError(
            "'mcp' tool declared but no MCP server pool is configured "
            "(ToolEnv.mcp_pool / ToolEnv.platform_mcp_pool / ToolEnv.tenant_mcp_pool / "
            "ToolEnv.user_mcp_oauth_pool)"
        )
    allow = set(entry.allow_tools) or None
    server_select = set(entry.servers) or None  # None = no per-agent restriction
    registered_servers: set[str] = set()

    # Platform pool — gated by the per-tenant allowlist (Mini-ADR O-14).
    if env.mcp_pool is not None:
        server_allow = set(env.mcp_allowlist) or None
        for server_name in env.mcp_pool.names():
            if server_allow is not None and server_name not in server_allow:
                continue
            if server_select is not None and server_name not in server_select:
                continue
            client = env.mcp_pool.get(server_name)
            if client is None:  # pragma: no cover - name came from names()
                continue
            await register_mcp_tools(
                server_name=server_name,
                client=client,
                registry=registry,
                allow_tools=allow,
                deferred=True,
            )
            # Platform reserves the server NAME unconditionally — even if
            # allow_tools filtered out all its tools this build — so a tenant
            # can't shadow a platform server by crafting allow_tools.
            # Server-level dedup is sufficient because tools are namespaced
            # mcp:<server>.<tool>.
            registered_servers.add(server_name)

    # Platform shared catalog pool (P1b) — the platform-curated SHARED servers.
    # Opt-in (Stream MCP P2 "租户选择使用"): a tenant uses a shared server only
    # after explicitly enabling it (name in ``mcp_allowlist``). Unlike the
    # operator file pool (empty allowlist = all), an empty allowlist here means
    # NONE. A name already reserved by the file pool wins (skip the duplicate).
    # Reserves the server NAME unconditionally so a tenant can't shadow it.
    if env.platform_mcp_pool is not None:
        enabled_servers = set(env.mcp_allowlist)
        for server_name in env.platform_mcp_pool.names():
            if server_name in registered_servers:
                logger.info("platform_mcp.server_shadowed_by_file_pool")
                continue
            if server_name not in enabled_servers:
                continue
            if server_select is not None and server_name not in server_select:
                continue
            client = env.platform_mcp_pool.get(server_name)
            if client is None:  # pragma: no cover - name came from names()
                continue
            await register_mcp_tools(
                server_name=server_name,
                client=client,
                registry=registry,
                allow_tools=allow,
                deferred=True,
            )
            registered_servers.add(server_name)

    # Tenant pool — the tenant's own remote servers; never gated by the
    # allowlist. On a name collision the platform server wins (already
    # registered above); skip the tenant duplicate to avoid a double
    # ``mcp:<name>.*`` registration.
    if env.tenant_mcp_pool is not None:
        for server_name in env.tenant_mcp_pool.names():
            if server_name in registered_servers:
                logger.info("tenant_mcp.server_shadowed_by_platform")
                continue
            if server_select is not None and server_name not in server_select:
                continue
            client = env.tenant_mcp_pool.get(server_name)
            if client is None:  # pragma: no cover
                continue
            await register_mcp_tools(
                server_name=server_name,
                client=client,
                registry=registry,
                allow_tools=allow,
                deferred=True,
            )
            registered_servers.add(server_name)

    # User OAuth pool — the calling user's own OAuth-connected servers
    # (Stream MCP-OAUTH, OA-3b). Never gated by the allowlist; a name already
    # registered by the platform or tenant pool wins (skip the duplicate).
    if env.user_mcp_oauth_pool is not None:
        for server_name in env.user_mcp_oauth_pool.names():
            if server_name in registered_servers:
                logger.info("user_mcp_oauth.server_shadowed")
                continue
            if server_select is not None and server_name not in server_select:
                continue
            client = env.user_mcp_oauth_pool.get(server_name)
            if client is None:  # pragma: no cover
                continue
            await register_mcp_tools(
                server_name=server_name,
                client=client,
                registry=registry,
                allow_tools=allow,
                deferred=True,
            )
            registered_servers.add(server_name)
