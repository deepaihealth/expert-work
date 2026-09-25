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
    ArgBindingSpec,
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
from orchestrator.tools.arg_bindings import bindings_by_tool
from orchestrator.tools.artifact import ListArtifactsTool, SaveArtifactTool
from orchestrator.tools.bash import BashTool
from orchestrator.tools.file_ops import (
    EditFileTool,
    ListDirTool,
    ReadFileTool,
    SearchFilesTool,
    WriteFileTool,
)
from orchestrator.tools.find_tools import FindToolsTool
from orchestrator.tools.http import AllowlistProvider, DenylistProvider, HTTPTool
from orchestrator.tools.knowledge import KnowledgeRetriever, KnowledgeSearchTool
from orchestrator.tools.locks import NullWorkspaceLock, WorkspaceLock
from orchestrator.tools.mcp import MCPServerPool, register_mcp_tools
from orchestrator.tools.read_document import ReadDocumentTool
from orchestrator.tools.read_page import FigureDelivery, ReadPageTool
from orchestrator.tools.registry import ToolRegistry
from orchestrator.tools.sandbox import ExecPythonTool, SandboxRuntime
from orchestrator.tools.skill_authoring import SKILL_AUTHORING_BUILTINS
from orchestrator.tools.spawn_worker import SpawnWorkerTool, WorkerBuildFn
from orchestrator.tools.subagent import MAX_SUBAGENT_DEPTH, ChildAgentBuilder, SubAgentTool
from orchestrator.tools.vision import AskImageTool, VLUsageMeter
from orchestrator.tools.web_search import DEFAULT_MAX_RESULTS, TavilyClient, WebSearchTool
from orchestrator.tools.workspace_store import WorkspaceStore
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
        "search_files",
        "read_document",
        "read_page",
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
    #: B-84 —— 只读文件工具(``read_file`` / ``list_dir`` / ``search_files``)的后端。
    #: 它们走 control-plane 自己挂着的 NAS, **不起沙箱** —— 实测 60 天 ``list_dir``
    #: 失败率 11%(22/207), 每一次都是沙箱创建失败, 而一次失败的探测会让模型断定
    #: 文件不存在然后重打一遍。``None`` = 这个部署没接工作区存储:与
    #: ``sandbox_runtime is None`` 同款处置(显式声明报错, 基础能力静默跳过)。
    workspace_store: WorkspaceStore | None = None
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
    #: available in this deployment. B-64 —— this is a
    #: ``DispatchingImageResolver`` in production (``make_image_resolver``),
    #: so it already understands the workspace-ref scheme too; there is no
    #: separate field for it.
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
    quick_vl_caller: LLMCaller | None = None,
    vl_usage_meter: VLUsageMeter | None = None,
    context_window: int | None = None,
    supports_vision: bool = False,
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
    declared VL model via ``vl_caller``. ``vl_usage_meter`` (B-64 Task 9)
    records each VL call into ``token_usage``; ``None`` records nothing.
    ``quick_vl_caller`` (B-64 Task 10) is the thinking-off VL router behind
    ``ask_image``'s default ``depth="quick"``; ``None`` = same as ``vl_caller``.

    ``supports_vision`` (B-64) is the main model's native image input
    (manifest ``model.supports_vision``). Together with ``vision`` it decides
    how ``read_page``'s rendered pages reach the model, which is what its
    success receipt tells the model (see :func:`_figure_delivery`).

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
    figure_delivery = _figure_delivery(supports_vision=supports_vision, vision=vision)
    # B-61 §5.4(评审 I-3)—— 参数绑定先**跨条目**并成一张表,再发给每一条 mcp
    # 条目。一份 manifest 可以有多个 ``mcp`` 条目(协议层合法、也有测试),而每一条
    # 都会把它选中的服务器整个再注册一遍。按条目各发各的,后注册那一条就会用「我
    # 这条没有绑定」去覆盖兄弟条目刚剥好的 schema 和刚记下的绑定 —— 参数于是悄悄
    # 回到模型手里,连保存时的告警都看不见(它只知道「没匹配上」,这属于「匹配上
    # 了又被顶掉」)。``(server, tool)`` 在整份 manifest 里唯一由协议层保证
    # (``AgentSpecBody._check_arg_bindings``),所以并表不会有谁静默覆盖谁。
    all_arg_bindings = [
        binding
        for entry in tool_specs
        if isinstance(entry, MCPToolSpec)
        for binding in entry.arg_bindings
    ]
    for entry in tool_specs:
        if isinstance(entry, BuiltinToolSpec):
            _register_builtin(
                registry, entry, tool_env, skill_seed_files, figure_delivery=figure_delivery
            )
        elif isinstance(entry, HTTPToolSpec):
            _register_http(registry, tool_env)
        elif isinstance(entry, MCPToolSpec):
            await _register_mcp(registry, entry, tool_env, all_arg_bindings)
    # B-61 §5.4 —— 「这条绑定一个工具都没落上」只能在**所有** mcp 条目都注册完之后
    # 回答,而且只能照 registry 的真实状态回答(复评 N-1)。单次注册答不了:
    #   * 它只看得见交给它的那一台服务器的工具表,答不了「这台服务器在不在」;
    #   * 兄弟条目的 ``allow_tools`` 会把别人绑的工具挡在它那一次循环之外,那条绑定
    #     在它眼里像是「目录里没有」,其实另一条目刚把它绑得好好的。
    # 谎报比不报更糟:保存时的告警是这套机制加进来的全部价值,配置的人会照着它去
    # 「修」一条从来没坏的绑定。所以判据是「有没有落地」,不是「这一遍有没有剩下」。
    # B-65 —— 「落过地」还不等于「现在还活着」。``mcp_tool_name`` 把非法字符折成 ``_``
    # 并截断到 64 字符,``register`` / ``bind_tool_args`` 又按那个名字覆盖 —— 两个工具
    # 折成同一个 wire 名时,后注册的会把前者的绑定从表里清掉。所以判定分两步:先看落没
    # 落过地(``tool_missing``),再拿落地时的那个 wire 名回查活着的绑定表(``name_collision``)。
    # 把撞名报成「目录里没有这个工具」是假话:工具明明在。
    landed = registry.landed_arg_bindings()
    live = registry.arg_bindings()
    # 一条绑定的参数**全部**漂没了时,``bind_tool_args`` 收到空表、同样会清掉那一项 ——
    # 那不是撞名,而且上面已经报过 ``params_absent`` 了。同一条绑定只说一次。
    configured_args: dict[tuple[str, str], set[str]] = {}
    for b in all_arg_bindings:
        configured_args.setdefault((b.server, b.tool), set()).update(b.args)
    fully_drifted = {
        (u.server, u.tool)
        for u in registry.unmatched_arg_bindings()
        if u.reason == "params_absent"
        and set(u.params) == configured_args.get((u.server, u.tool), set())
    }
    for binding in all_arg_bindings:
        key = (binding.server, binding.tool)
        wire_name = landed.get(key)
        if wire_name is None:
            registry.note_unmatched_arg_binding(
                binding.server, binding.tool, tuple(binding.args), reason="tool_missing"
            )
        elif wire_name not in live and key not in fully_drifted:
            registry.note_unmatched_arg_binding(
                binding.server, binding.tool, tuple(binding.args), reason="name_collision"
            )
    _register_base_capabilities(
        registry, tool_env, skill_seed_files, figure_delivery=figure_delivery
    )
    _register_subagents(registry, subagents, tool_env, subagent_depth)
    _register_spawn_worker(registry, tool_env, parent_spec, dynamic_workers, subagent_depth)
    _register_knowledge_search(registry, knowledge, tool_env)
    _register_ask_image(registry, vision, tool_env, vl_caller, quick_vl_caller, vl_usage_meter)
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


def _ask_image_enabled(vision: VisionSpec | None) -> bool:
    """``ask_image`` 注不注册的**唯一**判据 —— :func:`_register_ask_image` 与
    :func:`_figure_delivery` 都读它,``read_page`` 的回执因此不会许诺一个没注册的工具。

    声明了 ``vision`` 却缺依赖时 :func:`_register_ask_image` 直接抛错、整个构建失败,
    所以构建成功时「这里为真」就等于「``ask_image`` 在注册表里」。
    """
    return vision is not None


def _figure_delivery(*, supports_vision: bool, vision: VisionSpec | None) -> FigureDelivery:
    """B-64 Task 7 回修 —— ``read_page`` 渲出来的页怎么到模型眼前。

    主模型能看图就走 Path A(``graph_builder.figure_block.figure_block_message`` 挂进提示词,
    ``build_react_graph(supports_vision=...)`` 收的是同一个值);否则看 ``ask_image``
    在不在;两样都没有就是 ``"none"``。
    """
    if supports_vision:
        return "inline"
    if _ask_image_enabled(vision):
        return "ask_image"
    return "none"


def _register_ask_image(
    registry: ToolRegistry,
    vision: VisionSpec | None,
    env: ToolEnv,
    vl_caller: LLMCaller | None,
    quick_vl_caller: LLMCaller | None,
    vl_usage_meter: VLUsageMeter | None,
) -> None:
    """Register the ``ask_image`` tool when the manifest declares a
    ``vision:`` block — Stream J.6 Path B. A declared block missing
    either the image resolver or the VL caller is an un-buildable
    manifest."""
    if not _ask_image_enabled(vision) or vision is None:
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
    # B-64 Task 8 —— 带上工作区存储,``ask_image`` 才有 ``path`` + ``unit`` 短形态;
    # 没接时它只收 ``image_ref``,描述里也不提短形态。
    registry.register(
        AskImageTool(
            vl_caller=vl_caller,
            image_resolver=env.image_resolver,
            workspace_store=env.workspace_store,
            usage_meter=vl_usage_meter,
            quick_vl_caller=quick_vl_caller,
            vl_model_name=vision.model.name,
        )
    )


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
    *,
    figure_delivery: FigureDelivery = "none",
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
    elif entry.name in _FILE_OP_BUILTINS:
        _register_file_op(registry, entry.name, env, skill_seed_files)
    elif entry.name == "read_document":
        _register_read_document(registry, env, skill_seed_files)
    elif entry.name == "read_page":
        _register_read_page(registry, env, skill_seed_files, figure_delivery)
    elif entry.name == "save_artifact":
        # 登记前要 stat 工作区里那个文件,所以和 read_file/write_file 一样需要
        # 沙箱执行通道;没有它就没有「文件」这个概念,登记只会造出死产物。
        artifact_store = _require_artifact_store(env, "save_artifact")
        if env.sandbox_runtime is None:
            raise AgentFactoryError(
                "builtin 'save_artifact' declared but no sandbox runtime "
                "is configured (ToolEnv.sandbox_runtime)"
            )
        registry.register(SaveArtifactTool(store=artifact_store, client=env.sandbox_runtime))
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
    "search_files",
    "read_document",
    "read_page",
    "save_artifact",
    "list_artifacts",
)

#: B-84 —— 只读文件工具:走宿主 NAS(``ToolEnv.workspace_store``), 不起沙箱。
_HOST_READ_TOOLS: frozenset[str] = frozenset({"read_file", "list_dir", "search_files"})

#: 文件族全体 —— 读三件走 store, 写两件走沙箱。
_FILE_OP_BUILTINS: frozenset[str] = _HOST_READ_TOOLS | {"write_file", "edit_file"}


def _register_base_capabilities(
    registry: ToolRegistry,
    env: ToolEnv,
    skill_seed_files: tuple[tuple[str, bytes], ...],
    *,
    figure_delivery: FigureDelivery = "none",
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
            # ``save_artifact`` 现在多一条依赖:登记前要 stat 工作区里的文件,
            # 所以没有沙箱通道它注册了也只会造出下载 404 的死产物。
            # ``list_artifacts`` 只读库,不受影响。
            if name == "save_artifact" and env.sandbox_runtime is None:
                continue
        elif name in _HOST_READ_TOOLS:
            # B-84 —— 这三件的依赖是工作区存储, 不是沙箱。
            if env.workspace_store is None:
                continue
        elif env.sandbox_runtime is None:
            continue
        _register_builtin(
            registry,
            BuiltinToolSpec(name=name),
            env,
            skill_seed_files,
            figure_delivery=figure_delivery,
        )


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
    # Stream TE-7 — write_file / edit_file ride the sandbox runtime exec channel
    # as bash / exec_python do (TE-ADR-2 exec-warm locus).
    #
    # B-84 —— 只读那三件(read_file / list_dir / search_files)**不再**走沙箱:它们
    # 直读 control-plane 自己挂着的 NAS。依赖因此按工具分叉, 不是整族一个判据。
    if name in _HOST_READ_TOOLS:
        if env.workspace_store is None:
            raise AgentFactoryError(
                f"builtin {name!r} declared but no workspace store "
                "is configured (ToolEnv.workspace_store)"
            )
        if name == "read_file":
            registry.register(ReadFileTool(store=env.workspace_store))
        elif name == "list_dir":
            registry.register(ListDirTool(store=env.workspace_store))
        else:
            registry.register(SearchFilesTool(store=env.workspace_store))
        return
    if env.sandbox_runtime is None:
        raise AgentFactoryError(
            f"builtin {name!r} declared but no sandbox runtime "
            "is configured (ToolEnv.sandbox_runtime)"
        )
    if name == "write_file":
        registry.register(
            WriteFileTool(
                client=env.sandbox_runtime,
                workspace_lock=env.workspace_lock,
                skill_seed_files=skill_seed_files,
            )
        )
    else:  # edit_file
        registry.register(
            EditFileTool(
                client=env.sandbox_runtime,
                workspace_lock=env.workspace_lock,
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


def _register_read_page(
    registry: ToolRegistry,
    env: ToolEnv,
    skill_seed_files: tuple[tuple[str, bytes], ...],
    figure_delivery: FigureDelivery,
) -> None:
    # B-64 —— read_page rides the same warm sandbox runtime exec channel as
    # read_document (soffice / pdftoppm run inside the per-user sandbox).
    if env.sandbox_runtime is None:
        raise AgentFactoryError(
            "builtin 'read_page' declared but no sandbox runtime "
            "is configured (ToolEnv.sandbox_runtime)"
        )
    registry.register(
        ReadPageTool(
            client=env.sandbox_runtime,
            skill_seed_files=skill_seed_files,
            figure_delivery=figure_delivery,
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


async def _register_mcp(
    registry: ToolRegistry,
    entry: MCPToolSpec,
    env: ToolEnv,
    arg_bindings: Sequence[ArgBindingSpec] = (),
) -> None:
    """Register this ``mcp`` entry's tools.

    ``arg_bindings`` is the **whole manifest's** binding table, not just this
    entry's (review I-3): every ``mcp`` entry re-registers the servers it
    selects, so an entry that carries no bindings of its own must still strip
    and re-bind the ones a sibling entry declared — otherwise it silently
    restores the un-narrowed schema and drops the binding.

    Which bindings fell through is NOT decided here (review N-1): registration
    only records the ones that landed, and the caller answers the question once
    every entry is done. This pass cannot answer it — a tool it filters out via
    ``allow_tools`` may be bound perfectly well by the next one.
    """
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
    bindings_for = {
        server: bindings_by_tool(arg_bindings, server=server)
        for server in {binding.server for binding in arg_bindings}
    }

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
                arg_bindings=bindings_for.get(server_name),
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
                arg_bindings=bindings_for.get(server_name),
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
                arg_bindings=bindings_for.get(server_name),
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
                arg_bindings=bindings_for.get(server_name),
            )
            registered_servers.add(server_name)
