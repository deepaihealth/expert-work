"""Unit tests for :func:`build_tool_registry` — manifest ``tools:`` → registry."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any
from uuid import UUID

import pytest
from langchain_core.messages import AIMessage, BaseMessage

from expert_work.persistence import InMemoryArtifactStore, InMemoryKnowledgeStore
from expert_work.protocol import (
    BuiltinToolSpec,
    HTTPToolSpec,
    KnowledgeSpec,
    MCPToolSpec,
    ModelSpec,
    SubAgentSpec,
    VisionSpec,
)
from orchestrator import AgentFactoryError
from orchestrator.llm import FakeEmbedder
from orchestrator.multimodal import InMemoryImageResolver
from orchestrator.tools import (
    MAX_SUBAGENT_DEPTH,
    AskImageTool,
    BashTool,
    EditFileTool,
    ExecPythonTool,
    HTTPTool,
    KnowledgeRetriever,
    KnowledgeSearchTool,
    ListArtifactsTool,
    ListDirTool,
    MCPServerPool,
    MCPToolDef,
    ReadDocumentTool,
    ReadFileTool,
    RecordingMCPClient,
    RecordingSandboxRuntime,
    RecordingTavilyClient,
    RecordingWorkspaceLock,
    SaveArtifactTool,
    SubAgentTool,
    ToolEnv,
    WebSearchTool,
    WriteFileTool,
    build_tool_registry,
)


def _knowledge_retriever() -> KnowledgeRetriever:
    return KnowledgeRetriever(store=InMemoryKnowledgeStore(), embedder=FakeEmbedder())


class _StubChildBuilder:
    """Conforms to ``ChildAgentBuilder``. Assembly only *registers*
    SubAgentTools — it never invokes the builder — so the body is unused."""

    async def __call__(
        self,
        *,
        tenant_id: Any,
        name: str,
        version: str,
        depth: int,
        oauth_user_id: str | None = None,
    ) -> Any:
        raise AssertionError("child builder must not be called during assembly")


_SUBAGENTS = [
    SubAgentSpec(name="researcher", agent_ref="deep-researcher@1.0.0", description="research"),
    SubAgentSpec(name="writer", agent_ref="doc-writer@2.0.0", description="drafting"),
]


async def _allowlist(_tenant: UUID | None) -> Sequence[str]:
    return ["https://api.github.com/*"]


async def _seeded_pool() -> MCPServerPool:
    pool = MCPServerPool()
    client = RecordingMCPClient(
        tools=(
            MCPToolDef(name="read_pr", description="read a PR", input_schema={}),
            MCPToolDef(name="post_comment", description="comment", input_schema={}),
        )
    )
    await pool.add("gitlab", client)
    return pool


@pytest.mark.asyncio
async def test_empty_tools_builds_empty_registry() -> None:
    registry = await build_tool_registry([], tool_env=ToolEnv())
    assert len(registry) == 0


@pytest.mark.asyncio
async def test_builtin_web_search_assembled() -> None:
    env = ToolEnv(web_search_client=RecordingTavilyClient())
    registry = await build_tool_registry(
        [BuiltinToolSpec(name="web_search", config={"max_results": 7})],
        tool_env=env,
    )
    tool = registry.get("web_search")
    assert isinstance(tool, WebSearchTool)
    assert tool.default_max_results == 7


@pytest.mark.asyncio
async def test_builtin_unknown_name_raises() -> None:
    with pytest.raises(AgentFactoryError, match="unknown builtin"):
        await build_tool_registry(
            [BuiltinToolSpec(name="nonsense")],
            tool_env=ToolEnv(web_search_client=RecordingTavilyClient()),
        )


@pytest.mark.asyncio
async def test_builtin_ask_for_approval_assembled() -> None:
    """Stream J.8 — ``ask_for_approval`` is a zero-dependency builtin."""
    from orchestrator.tools.approval import AskForApprovalTool

    registry = await build_tool_registry(
        [BuiltinToolSpec(name="ask_for_approval")],
        tool_env=ToolEnv(),
    )
    tool = registry.get("ask_for_approval")
    assert isinstance(tool, AskForApprovalTool)


@pytest.mark.asyncio
async def test_builtin_web_search_missing_client_raises() -> None:
    with pytest.raises(AgentFactoryError, match="Tavily client"):
        await build_tool_registry([BuiltinToolSpec(name="web_search")], tool_env=ToolEnv())


@pytest.mark.asyncio
async def test_http_tool_assembled() -> None:
    registry = await build_tool_registry(
        [HTTPToolSpec()], tool_env=ToolEnv(allowlist_provider=_allowlist)
    )
    assert isinstance(registry.get("http"), HTTPTool)


@pytest.mark.asyncio
async def test_http_tool_missing_allowlist_raises() -> None:
    with pytest.raises(AgentFactoryError, match="allowlist provider"):
        await build_tool_registry([HTTPToolSpec()], tool_env=ToolEnv())


@pytest.mark.asyncio
async def test_mcp_tools_assembled_from_pool() -> None:
    pool = await _seeded_pool()
    registry = await build_tool_registry([MCPToolSpec()], tool_env=ToolEnv(mcp_pool=pool))
    # Stream TE-6b — both server-advertised tools register *deferred*
    # (namespaced ``mcp__<server>__<tool>``, absent from the bind), and
    # ``find_tools`` is auto-added active so they can be discovered.
    assert {s.name for s in registry.specs()} == {"find_tools"}
    assert {s.name for s in registry.all_specs()} == {
        "find_tools",
        "mcp__gitlab__read_pr",
        "mcp__gitlab__post_comment",
    }


@pytest.mark.asyncio
async def test_mcp_allow_tools_filters() -> None:
    pool = await _seeded_pool()
    registry = await build_tool_registry(
        [MCPToolSpec(allow_tools=["read_pr"])], tool_env=ToolEnv(mcp_pool=pool)
    )
    # TE-6b — only read_pr registers (deferred); find_tools auto-added active.
    assert {s.name for s in registry.specs()} == {"find_tools"}
    assert {s.name for s in registry.all_specs()} == {"find_tools", "mcp__gitlab__read_pr"}


async def _two_server_pool() -> MCPServerPool:
    pool = MCPServerPool()
    await pool.add(
        "gitlab",
        RecordingMCPClient(tools=(MCPToolDef(name="read_pr", description="", input_schema={}),)),
    )
    await pool.add(
        "linear",
        RecordingMCPClient(
            tools=(MCPToolDef(name="list_issues", description="", input_schema={}),)
        ),
    )
    return pool


@pytest.mark.asyncio
async def test_mcp_server_allowlist_empty_sees_all_servers() -> None:
    # Stream O Mini-ADR O-14 — empty allowlist (default) = no restriction.
    pool = await _two_server_pool()
    registry = await build_tool_registry([MCPToolSpec()], tool_env=ToolEnv(mcp_pool=pool))
    # TE-6b — both servers' tools register deferred; find_tools auto-added.
    assert {s.name for s in registry.all_specs()} == {
        "find_tools",
        "mcp__gitlab__read_pr",
        "mcp__linear__list_issues",
    }
    assert {s.name for s in registry.specs()} == {"find_tools"}


@pytest.mark.asyncio
async def test_mcp_server_allowlist_hides_unlisted_servers() -> None:
    # A non-empty allowlist restricts the agent to the listed server names;
    # ``linear`` is hidden even though it is in the platform pool.
    pool = await _two_server_pool()
    registry = await build_tool_registry(
        [MCPToolSpec()],
        tool_env=ToolEnv(mcp_pool=pool, mcp_allowlist=("gitlab",)),
    )
    # TE-6b — gitlab's tool registers deferred (dispatchable via get), linear
    # is hidden by the allowlist; find_tools auto-added active.
    assert {s.name for s in registry.all_specs()} == {"find_tools", "mcp__gitlab__read_pr"}
    assert registry.get("mcp__gitlab__read_pr") is not None
    assert registry.get("mcp__linear__list_issues") is None


@pytest.mark.asyncio
async def test_mcp_missing_pool_raises() -> None:
    with pytest.raises(AgentFactoryError, match="MCP server pool"):
        await build_tool_registry([MCPToolSpec()], tool_env=ToolEnv())


@pytest.mark.asyncio
async def test_multiple_tools_all_registered() -> None:
    env = ToolEnv(
        web_search_client=RecordingTavilyClient(),
        allowlist_provider=_allowlist,
    )
    registry = await build_tool_registry(
        [BuiltinToolSpec(name="web_search"), HTTPToolSpec()], tool_env=env
    )
    assert registry.get("web_search") is not None
    assert registry.get("http") is not None


# ---------------------------------------------------------------------------
# subagents — agent-as-tool delegation (Stream J.4)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_subagents_assembled_into_named_tools() -> None:
    env = ToolEnv(child_agent_builder=_StubChildBuilder())
    registry = await build_tool_registry([], tool_env=env, subagents=_SUBAGENTS)
    researcher = registry.get("researcher")
    writer = registry.get("writer")
    assert isinstance(researcher, SubAgentTool)
    assert isinstance(writer, SubAgentTool)
    # Top-level agent builds at depth 0 → its children build at depth 1.
    assert researcher.child_depth == 1


@pytest.mark.asyncio
async def test_subagent_child_depth_is_parent_depth_plus_one() -> None:
    env = ToolEnv(child_agent_builder=_StubChildBuilder())
    registry = await build_tool_registry(
        [], tool_env=env, subagents=_SUBAGENTS[:1], subagent_depth=1
    )
    tool = registry.get("researcher")
    assert isinstance(tool, SubAgentTool)
    assert tool.child_depth == 2


@pytest.mark.asyncio
async def test_subagents_without_builder_raises() -> None:
    with pytest.raises(AgentFactoryError, match="sub-agent builder"):
        await build_tool_registry([], tool_env=ToolEnv(), subagents=_SUBAGENTS)


@pytest.mark.asyncio
async def test_subagents_not_registered_at_depth_cap() -> None:
    # At MAX_SUBAGENT_DEPTH nothing registers — the structural recursion
    # guard. The agent still builds; it just cannot delegate further.
    env = ToolEnv(child_agent_builder=_StubChildBuilder())
    registry = await build_tool_registry(
        [], tool_env=env, subagents=_SUBAGENTS, subagent_depth=MAX_SUBAGENT_DEPTH
    )
    assert len(registry) == 0


@pytest.mark.asyncio
async def test_depth_cap_skips_missing_builder_check() -> None:
    # At the cap nothing is registered, so a missing builder is not an
    # error — the un-buildable check only fires when tools would register.
    registry = await build_tool_registry(
        [], tool_env=ToolEnv(), subagents=_SUBAGENTS, subagent_depth=MAX_SUBAGENT_DEPTH
    )
    assert len(registry) == 0


@pytest.mark.asyncio
async def test_no_subagents_with_empty_builder_is_fine() -> None:
    # No subagents declared → the missing-builder check never fires.
    registry = await build_tool_registry([], tool_env=ToolEnv())
    assert len(registry) == 0


# ---------------------------------------------------------------------------
# knowledge — knowledge_search activation (Stream J.5)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_knowledge_block_activates_search_tool() -> None:
    env = ToolEnv(knowledge_retriever=_knowledge_retriever())
    registry = await build_tool_registry(
        [], tool_env=env, knowledge=KnowledgeSpec(knowledge_base_refs=["hr", "eng"])
    )
    tool = registry.get("knowledge_search")
    assert isinstance(tool, KnowledgeSearchTool)
    assert tool.knowledge_base_refs == ("hr", "eng")


@pytest.mark.asyncio
async def test_knowledge_block_without_retriever_raises() -> None:
    with pytest.raises(AgentFactoryError, match="knowledge retriever"):
        await build_tool_registry(
            [], tool_env=ToolEnv(), knowledge=KnowledgeSpec(knowledge_base_refs=["hr"])
        )


@pytest.mark.asyncio
async def test_no_knowledge_block_registers_no_search_tool() -> None:
    env = ToolEnv(knowledge_retriever=_knowledge_retriever())
    registry = await build_tool_registry([], tool_env=env)
    assert registry.get("knowledge_search") is None


# ---------------------------------------------------------------------------
# vision — ask_image activation (Stream J.6 Path B)
# ---------------------------------------------------------------------------


async def _stub_vl_caller(*, messages: Sequence[BaseMessage], tools: Sequence[Any]) -> AIMessage:
    """Conforms to :class:`LLMCaller`; never invoked by assembly tests."""
    raise AssertionError("VL caller must not be called during assembly")


def _vision_spec() -> VisionSpec:
    return VisionSpec(model=ModelSpec(provider="qwen", name="qwen-vl-max"))


@pytest.mark.asyncio
async def test_vision_block_activates_ask_image_tool() -> None:
    env = ToolEnv(image_resolver=InMemoryImageResolver())
    registry = await build_tool_registry(
        [],
        tool_env=env,
        vision=_vision_spec(),
        vl_caller=_stub_vl_caller,
    )
    tool = registry.get("ask_image")
    assert isinstance(tool, AskImageTool)


@pytest.mark.asyncio
async def test_vision_block_without_image_resolver_raises() -> None:
    with pytest.raises(AgentFactoryError, match="image resolver"):
        await build_tool_registry(
            [],
            tool_env=ToolEnv(),
            vision=_vision_spec(),
            vl_caller=_stub_vl_caller,
        )


@pytest.mark.asyncio
async def test_vision_block_without_vl_caller_raises() -> None:
    env = ToolEnv(image_resolver=InMemoryImageResolver())
    with pytest.raises(AgentFactoryError, match="VL llm_caller"):
        await build_tool_registry([], tool_env=env, vision=_vision_spec(), vl_caller=None)


@pytest.mark.asyncio
async def test_no_vision_block_registers_no_ask_image_tool() -> None:
    env = ToolEnv(image_resolver=InMemoryImageResolver())
    registry = await build_tool_registry([], tool_env=env)
    assert registry.get("ask_image") is None


# ---------------------------------------------------------------------------
# tenant_mcp_pool — Stream V-D union registration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tenant_pool_tools_registered_alongside_platform() -> None:
    platform = MCPServerPool()
    await platform.add(
        "ops",
        RecordingMCPClient(tools=(MCPToolDef(name="deploy", description="", input_schema={}),)),
    )
    tenant = MCPServerPool()
    await tenant.add(
        "github",
        RecordingMCPClient(
            tools=(MCPToolDef(name="create_issue", description="", input_schema={}),)
        ),
    )
    registry = await build_tool_registry(
        [MCPToolSpec()], tool_env=ToolEnv(mcp_pool=platform, tenant_mcp_pool=tenant)
    )
    assert registry.get("mcp__ops__deploy") is not None
    assert registry.get("mcp__github__create_issue") is not None


@pytest.mark.asyncio
async def test_tenant_pool_not_filtered_by_platform_allowlist() -> None:
    # The allowlist gates the PLATFORM pool only; tenant servers are the
    # tenant's own and are always visible.
    platform = MCPServerPool()
    await platform.add(
        "ops",
        RecordingMCPClient(tools=(MCPToolDef(name="deploy", description="", input_schema={}),)),
    )
    tenant = MCPServerPool()
    await tenant.add(
        "github",
        RecordingMCPClient(
            tools=(MCPToolDef(name="create_issue", description="", input_schema={}),)
        ),
    )
    registry = await build_tool_registry(
        [MCPToolSpec()],
        tool_env=ToolEnv(mcp_pool=platform, tenant_mcp_pool=tenant, mcp_allowlist=("ops",)),
    )
    assert registry.get("mcp__ops__deploy") is not None  # platform, on allowlist
    assert registry.get("mcp__github__create_issue") is not None  # tenant, not gated


@pytest.mark.asyncio
async def test_allow_tools_filters_tenant_pool_too() -> None:
    tenant = MCPServerPool()
    await tenant.add(
        "github",
        RecordingMCPClient(
            tools=(
                MCPToolDef(name="create_issue", description="", input_schema={}),
                MCPToolDef(name="delete_repo", description="", input_schema={}),
            )
        ),
    )
    registry = await build_tool_registry(
        [MCPToolSpec(allow_tools=["create_issue"])],
        tool_env=ToolEnv(tenant_mcp_pool=tenant),
    )
    assert registry.get("mcp__github__create_issue") is not None
    assert registry.get("mcp__github__delete_repo") is None


@pytest.mark.asyncio
async def test_tenant_pool_only_no_platform_pool_ok() -> None:
    # mcp tool declared with only a tenant pool (no platform pool) must work.
    tenant = MCPServerPool()
    await tenant.add(
        "github",
        RecordingMCPClient(
            tools=(MCPToolDef(name="create_issue", description="", input_schema={}),)
        ),
    )
    registry = await build_tool_registry([MCPToolSpec()], tool_env=ToolEnv(tenant_mcp_pool=tenant))
    assert registry.get("mcp__github__create_issue") is not None


@pytest.mark.asyncio
async def test_name_collision_platform_wins() -> None:
    platform = MCPServerPool()
    await platform.add(
        "github",
        RecordingMCPClient(
            tools=(MCPToolDef(name="from_platform", description="", input_schema={}),)
        ),
    )
    tenant = MCPServerPool()
    await tenant.add(
        "github",
        RecordingMCPClient(
            tools=(MCPToolDef(name="from_tenant", description="", input_schema={}),)
        ),
    )
    registry = await build_tool_registry(
        [MCPToolSpec()], tool_env=ToolEnv(mcp_pool=platform, tenant_mcp_pool=tenant)
    )
    # platform registered first; the colliding tenant server is skipped.
    assert registry.get("mcp__github__from_platform") is not None
    assert registry.get("mcp__github__from_tenant") is None


# ---------------------------------------------------------------------------
# platform_mcp_pool — Stream MCP platform-servers (P1b) shared catalog pool
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_platform_db_pool_tools_registered() -> None:
    catalog = MCPServerPool()
    await catalog.add(
        "weather",
        RecordingMCPClient(tools=(MCPToolDef(name="forecast", description="", input_schema={}),)),
    )
    registry = await build_tool_registry(
        [MCPToolSpec()],
        tool_env=ToolEnv(platform_mcp_pool=catalog, mcp_allowlist=("weather",)),
    )
    assert registry.get("mcp__weather__forecast") is not None


@pytest.mark.asyncio
async def test_platform_db_pool_gated_by_allowlist() -> None:
    # Like the operator file pool, the shared catalog pool is gated by the
    # per-tenant allowlist: an unlisted server is hidden.
    catalog = MCPServerPool()
    await catalog.add(
        "weather",
        RecordingMCPClient(tools=(MCPToolDef(name="forecast", description="", input_schema={}),)),
    )
    await catalog.add(
        "secret",
        RecordingMCPClient(tools=(MCPToolDef(name="leak", description="", input_schema={}),)),
    )
    registry = await build_tool_registry(
        [MCPToolSpec()],
        tool_env=ToolEnv(platform_mcp_pool=catalog, mcp_allowlist=("weather",)),
    )
    assert registry.get("mcp__weather__forecast") is not None
    assert registry.get("mcp__secret__leak") is None


@pytest.mark.asyncio
async def test_platform_db_pool_empty_allowlist_sees_none() -> None:
    # Opt-in (P2): with no server enabled (empty allowlist), the shared catalog
    # pool contributes nothing — unlike the operator file pool.
    catalog = MCPServerPool()
    await catalog.add(
        "weather",
        RecordingMCPClient(tools=(MCPToolDef(name="forecast", description="", input_schema={}),)),
    )
    registry = await build_tool_registry(
        [MCPToolSpec()], tool_env=ToolEnv(platform_mcp_pool=catalog)
    )
    assert registry.get("mcp__weather__forecast") is None


@pytest.mark.asyncio
async def test_file_pool_wins_collision_over_db_pool() -> None:
    file_pool = MCPServerPool()
    await file_pool.add(
        "shared",
        RecordingMCPClient(tools=(MCPToolDef(name="from_file", description="", input_schema={}),)),
    )
    db_pool = MCPServerPool()
    await db_pool.add(
        "shared",
        RecordingMCPClient(tools=(MCPToolDef(name="from_db", description="", input_schema={}),)),
    )
    registry = await build_tool_registry(
        [MCPToolSpec()], tool_env=ToolEnv(mcp_pool=file_pool, platform_mcp_pool=db_pool)
    )
    # file pool registered first; the colliding DB server is skipped.
    assert registry.get("mcp__shared__from_file") is not None
    assert registry.get("mcp__shared__from_db") is None


@pytest.mark.asyncio
async def test_platform_db_pool_only_no_file_pool_ok() -> None:
    catalog = MCPServerPool()
    await catalog.add(
        "weather",
        RecordingMCPClient(tools=(MCPToolDef(name="forecast", description="", input_schema={}),)),
    )
    registry = await build_tool_registry(
        [MCPToolSpec()],
        tool_env=ToolEnv(platform_mcp_pool=catalog, mcp_allowlist=("weather",)),
    )
    assert registry.get("mcp__weather__forecast") is not None


@pytest.mark.asyncio
async def test_mcp_declared_but_no_pools_raises() -> None:
    with pytest.raises(AgentFactoryError, match="MCP server pool"):
        await build_tool_registry([MCPToolSpec()], tool_env=ToolEnv())


@pytest.mark.asyncio
async def test_servers_filter_restricts_to_named_servers() -> None:
    pool = MCPServerPool()
    await pool.add(
        "github",
        RecordingMCPClient(tools=(MCPToolDef(name="gh", description="", input_schema={}),)),
    )
    await pool.add(
        "linear",
        RecordingMCPClient(tools=(MCPToolDef(name="li", description="", input_schema={}),)),
    )
    registry = await build_tool_registry(
        [MCPToolSpec(servers=["github"])], tool_env=ToolEnv(mcp_pool=pool)
    )
    assert registry.get("mcp__github__gh") is not None
    assert registry.get("mcp__linear__li") is None


@pytest.mark.asyncio
async def test_empty_servers_means_all() -> None:
    pool = MCPServerPool()
    await pool.add(
        "github",
        RecordingMCPClient(tools=(MCPToolDef(name="gh", description="", input_schema={}),)),
    )
    await pool.add(
        "linear",
        RecordingMCPClient(tools=(MCPToolDef(name="li", description="", input_schema={}),)),
    )
    registry = await build_tool_registry([MCPToolSpec()], tool_env=ToolEnv(mcp_pool=pool))
    assert registry.get("mcp__github__gh") is not None
    assert registry.get("mcp__linear__li") is not None


@pytest.mark.asyncio
async def test_servers_filter_applies_to_tenant_pool() -> None:
    tenant = MCPServerPool()
    await tenant.add(
        "github",
        RecordingMCPClient(tools=(MCPToolDef(name="gh", description="", input_schema={}),)),
    )
    await tenant.add(
        "postgres",
        RecordingMCPClient(tools=(MCPToolDef(name="pg", description="", input_schema={}),)),
    )
    registry = await build_tool_registry(
        [MCPToolSpec(servers=["github"])], tool_env=ToolEnv(tenant_mcp_pool=tenant)
    )
    assert registry.get("mcp__github__gh") is not None
    assert registry.get("mcp__postgres__pg") is None


@pytest.mark.asyncio
async def test_servers_filter_composes_with_platform_and_tenant() -> None:
    platform = MCPServerPool()
    await platform.add(
        "ops",
        RecordingMCPClient(tools=(MCPToolDef(name="deploy", description="", input_schema={}),)),
    )
    tenant = MCPServerPool()
    await tenant.add(
        "github",
        RecordingMCPClient(tools=(MCPToolDef(name="gh", description="", input_schema={}),)),
    )
    # select only the tenant's github; the platform ops server is excluded.
    registry = await build_tool_registry(
        [MCPToolSpec(servers=["github"])],
        tool_env=ToolEnv(mcp_pool=platform, tenant_mcp_pool=tenant),
    )
    assert registry.get("mcp__github__gh") is not None
    assert registry.get("mcp__ops__deploy") is None


@pytest.mark.asyncio
async def test_platform_reserves_name_even_when_all_its_tools_filtered() -> None:
    # Platform reserves the server NAME unconditionally even when allow_tools
    # filters out every one of its tools for this build.  A tenant server with
    # the same name must NOT be registered — otherwise a tenant could shadow a
    # platform server by crafting allow_tools to exclude all platform tools.
    platform = MCPServerPool()
    await platform.add(
        "github",
        RecordingMCPClient(
            tools=(MCPToolDef(name="platform_tool", description="", input_schema={}),)
        ),
    )
    tenant = MCPServerPool()
    await tenant.add(
        "github",
        RecordingMCPClient(
            tools=(MCPToolDef(name="tenant_tool", description="", input_schema={}),)
        ),
    )
    # allow_tools excludes the platform server's only tool; tenant has a matching name.
    registry = await build_tool_registry(
        [MCPToolSpec(allow_tools=["tenant_tool"])],
        tool_env=ToolEnv(mcp_pool=platform, tenant_mcp_pool=tenant),
    )
    # Platform reserved "github" → tenant's mcp__github__tenant_tool is NOT registered.
    assert registry.get("mcp__github__tenant_tool") is None
    assert registry.get("mcp__github__platform_tool") is None  # filtered out by allow_tools


# --- Stream TE-7/TE-8/TE-9a file-op builtins ---


@pytest.mark.parametrize(
    ("name", "cls"),
    [
        ("read_file", ReadFileTool),
        ("write_file", WriteFileTool),
        ("edit_file", EditFileTool),
        ("list_dir", ListDirTool),
    ],
)
async def test_file_op_builtin_assembles(name: str, cls: type) -> None:
    registry = await build_tool_registry(
        [BuiltinToolSpec(name=name)],
        tool_env=ToolEnv(sandbox_runtime=RecordingSandboxRuntime()),
    )
    assert isinstance(registry.get(name), cls)


async def test_file_op_write_tools_receive_workspace_lock() -> None:
    lock = RecordingWorkspaceLock()
    env = ToolEnv(sandbox_runtime=RecordingSandboxRuntime(), workspace_lock=lock)
    registry = await build_tool_registry(
        [BuiltinToolSpec(name="write_file"), BuiltinToolSpec(name="edit_file")],
        tool_env=env,
    )
    write_tool = registry.get("write_file")
    edit_tool = registry.get("edit_file")
    assert isinstance(write_tool, WriteFileTool)
    assert isinstance(edit_tool, EditFileTool)
    assert write_tool.workspace_lock is lock
    assert edit_tool.workspace_lock is lock


async def test_file_op_without_supervisor_raises() -> None:
    with pytest.raises(AgentFactoryError):
        await build_tool_registry([BuiltinToolSpec(name="edit_file")], tool_env=ToolEnv())


# ---------------------------------------------------------------------------
# Tier 1 base capabilities — code execution + file + artifact tooling is
# assembled for EVERY agent when the platform wires the sandbox / artifact
# deps, regardless of the manifest ``tools:`` list. A complete agent is not a
# per-manifest opt-in; the sandbox is the security boundary (not the absence
# of a tool). See docs/design/agent-base-capabilities-and-form.md.
# ---------------------------------------------------------------------------

_BASE_SANDBOX_TOOLS = (
    "exec_python",
    "bash",
    "read_file",
    "write_file",
    "edit_file",
    "list_dir",
    "read_document",
)
_BASE_ARTIFACT_TOOLS = ("save_artifact", "list_artifacts")


def _names(registry: object) -> set[str]:
    return {s.name for s in registry.all_specs()}  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_base_capabilities_assembled_with_no_manifest_tools() -> None:
    # An empty ``tools:`` list still yields a capable agent: exec_python /
    # bash / file ops (supervisor) + artifacts (artifact store).
    env = ToolEnv(
        sandbox_runtime=RecordingSandboxRuntime(),
        artifact_store=InMemoryArtifactStore(),
    )
    registry = await build_tool_registry([], tool_env=env)
    assert isinstance(registry.get("exec_python"), ExecPythonTool)
    assert isinstance(registry.get("bash"), BashTool)
    assert isinstance(registry.get("read_file"), ReadFileTool)
    assert isinstance(registry.get("write_file"), WriteFileTool)
    assert isinstance(registry.get("edit_file"), EditFileTool)
    assert isinstance(registry.get("list_dir"), ListDirTool)
    assert isinstance(registry.get("read_document"), ReadDocumentTool)
    assert isinstance(registry.get("save_artifact"), SaveArtifactTool)
    assert isinstance(registry.get("list_artifacts"), ListArtifactsTool)


@pytest.mark.asyncio
async def test_base_capabilities_absent_without_deps_no_raise() -> None:
    # A deployment that wires NO sandbox / artifact store (bare ToolEnv) is a
    # platform-level choice — the implicit base set is simply skipped, never
    # raised (the raise is reserved for an EXPLICIT manifest declaration).
    # Preserves the "empty ToolEnv() builds a pure-LLM agent" invariant.
    registry = await build_tool_registry([], tool_env=ToolEnv())
    assert len(registry) == 0


@pytest.mark.asyncio
async def test_base_capabilities_gated_per_dependency() -> None:
    # Each base tool registers only when ITS dependency is wired: an
    # artifact store but no supervisor yields artifacts, not exec/bash/files.
    env = ToolEnv(artifact_store=InMemoryArtifactStore())
    registry = await build_tool_registry([], tool_env=env)
    for name in _BASE_SANDBOX_TOOLS:
        assert registry.get(name) is None, name
    for name in _BASE_ARTIFACT_TOOLS:
        assert registry.get(name) is not None, name


@pytest.mark.asyncio
async def test_explicit_base_tool_not_double_registered() -> None:
    # A manifest that still lists a base tool explicitly must not register it
    # twice — the implicit pass dedups against what the manifest loop added.
    env = ToolEnv(sandbox_runtime=RecordingSandboxRuntime())
    registry = await build_tool_registry([BuiltinToolSpec(name="exec_python")], tool_env=env)
    occurrences = [s for s in registry.all_specs() if s.name == "exec_python"]
    assert len(occurrences) == 1


@pytest.mark.asyncio
async def test_base_capabilities_coexist_with_opt_in_tools() -> None:
    # Declaring an opt-in tool (web_search) does not suppress the base set.
    env = ToolEnv(
        web_search_client=RecordingTavilyClient(),
        sandbox_runtime=RecordingSandboxRuntime(),
        artifact_store=InMemoryArtifactStore(),
    )
    registry = await build_tool_registry([BuiltinToolSpec(name="web_search")], tool_env=env)
    assert registry.get("web_search") is not None
    for name in (*_BASE_SANDBOX_TOOLS, *_BASE_ARTIFACT_TOOLS):
        assert registry.get(name) is not None, name
