"""B-294 —— 跳过了连不上的 MCP 服务的构建在构建缓存里只留 60 秒。

构建遇到连不上的 MCP 服务会跳过它、run 照常(``register_mcp_tools``)。这份「缺工具」
的构建要是按正常 TTL(30 分钟)缓存,服务恢复后整个 TTL 里这个智能体都没有那台服务的
工具 —— 池里的客户端明明能自己重连(B-177),只是没人再去 ``list_tools``;要是一点不
缓存,服务断着的时候每个 run / 每次委派都要整套重新构建、重拨一次。所以它只留
``DEGRADED_BUILD_TTL_S``(60 秒):窗口内复用,过了窗口重新构建,服务回来工具就回来。

主智能体缓存(``AgentRuntime.get_agent``)和子智能体缓存(``make_child_agent_builder``)
各有一份,两份都钉住;完整的构建照旧按正常 TTL。
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import httpx
import pytest
from langgraph.checkpoint.memory import InMemorySaver

from control_plane import runtime as runtime_mod
from control_plane import subagent_runtime as subagent_mod
from control_plane.runtime import AgentRuntime
from control_plane.subagent_runtime import make_child_agent_builder
from expert_work.persistence.agent_spec import InMemoryAgentSpecStore
from expert_work.protocol import AgentSpec, MCPToolSpec
from expert_work.runtime.runs import RunManager
from expert_work.runtime.stream_bridge import InMemoryStreamBridge
from expert_work.testing import InMemorySecretStore
from orchestrator import BuiltAgent, ToolEnv
from orchestrator.tools import (
    MCPServerPool,
    MCPToolDef,
    RecordingMCPClient,
    build_tool_registry,
)

_SPEC = AgentSpec.model_validate(
    {
        "apiVersion": "expert_work.io/v1",
        "kind": "Agent",
        "metadata": {"name": "researcher", "version": "1.0.0", "tenant": "t"},
        "spec": {
            "tenant_config": {},
            "model": {"provider": "anthropic", "name": "claude"},
            "system_prompt": {"template": "x"},
            "sandbox": {
                "resources": {"cpu": "1", "memory": "1Gi"},
                "network": {"egress": "proxy", "allowlist": ["a.com"]},
                "filesystem": {},
            },
        },
    }
)


class _Peer(RecordingMCPClient):
    """``down`` 时 ``list_tools`` 抛 SDK 的形态(TaskGroup 包着 httpx.ConnectError)。"""

    down: bool = False

    async def list_tools(self) -> Any:
        if self.down:
            req = httpx.Request("POST", "http://peer.test/mcp")
            raise ExceptionGroup("tg", [httpx.ConnectError("refused", request=req)])
        return await super().list_tools()


class _RegistryBuilder:
    """``agent_builder`` 桩:真的跑 ``build_tool_registry``,按 ``agent_factory`` 的
    口径投影出 ``tool_catalog`` / ``skipped_mcp_servers``。"""

    def __init__(self, env: ToolEnv) -> None:
        self.env = env
        self.calls = 0

    async def __call__(self, spec: AgentSpec, **_: Any) -> BuiltAgent:
        self.calls += 1
        registry = await build_tool_registry([MCPToolSpec()], tool_env=self.env)
        return BuiltAgent(
            graph=object(),  # type: ignore[arg-type]
            system_prompt=f"b{self.calls}",
            max_steps=1,
            tool_catalog=registry.catalog(),
            skipped_mcp_servers=registry.skipped_mcp_servers(),
        )


def _tools(built: BuiltAgent) -> set[str]:
    return {entry.name for entry in built.tool_catalog}


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_degraded_ttl_is_short_and_never_longer_than_the_normal_one() -> None:
    assert runtime_mod.DEGRADED_BUILD_TTL_S == 60.0
    degraded = BuiltAgent(
        graph=object(),  # type: ignore[arg-type]
        system_prompt="",
        max_steps=1,
        skipped_mcp_servers=("peer",),
    )
    healthy = BuiltAgent(graph=object(), system_prompt="", max_steps=1)  # type: ignore[arg-type]
    assert runtime_mod.build_cache_ttl_s(degraded, 1800.0) == 60.0
    assert runtime_mod.build_cache_ttl_s(degraded, 30.0) == 30.0
    assert runtime_mod.build_cache_ttl_s(healthy, 1800.0) == 1800.0


async def test_the_tool_comes_back_within_the_short_window_after_the_server_does() -> None:
    peer = _Peer(tools=(MCPToolDef(name="echo", description="", input_schema={}),))
    pool = MCPServerPool()
    await pool.add("peer", peer)
    builder = _RegistryBuilder(ToolEnv(tenant_mcp_pool=pool))
    clock = _Clock()
    rt = AgentRuntime(
        run_manager=RunManager(store=None),
        stream_bridge=InMemoryStreamBridge(),
        agent_builder=builder,
        _clock=clock,
    )
    tenant = uuid4()

    async def _get() -> BuiltAgent:
        return await rt.get_agent(tenant_id=tenant, name="researcher", version="1.0.0", spec=_SPEC)

    peer.down = True
    during = await _get()
    assert during.skipped_mcp_servers == ("peer",)
    assert "mcp__peer__echo" not in _tools(during)

    # 窗口内:复用缺工具的构建,不重新构建、不重拨(服务恢复了也一样)。
    clock.now += 59
    assert await _get() is during
    peer.down = False
    assert await _get() is during
    assert builder.calls == 1

    # 过了窗口:重新构建,工具回来。
    clock.now += 2
    after = await _get()
    assert builder.calls == 2
    assert "mcp__peer__echo" in _tools(after)

    # 完整的构建按正常 TTL 留着:过了 60 秒照样命中。
    clock.now += rt.cache_ttl_s - 1
    assert await _get() is after
    assert builder.calls == 2


async def test_the_child_build_cache_keeps_a_degraded_build_only_for_the_short_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    skipped: list[tuple[str, ...]] = [("peer",), ()]
    calls = 0

    async def _build_agent(spec: AgentSpec, **_: Any) -> BuiltAgent:
        nonlocal calls
        calls += 1
        return BuiltAgent(
            graph=object(),  # type: ignore[arg-type]
            system_prompt=f"b{calls}",
            max_steps=1,
            skipped_mcp_servers=skipped[min(calls, len(skipped)) - 1],
        )

    monkeypatch.setattr(subagent_mod, "build_agent", _build_agent)
    tenant = uuid4()
    store = InMemoryAgentSpecStore()
    await store.create(tenant_id=tenant, spec=_SPEC, spec_sha256="a" * 64, created_by="test")
    clock = _Clock()
    builder = make_child_agent_builder(
        spec_store=store,
        secret_store=InMemorySecretStore(),  # type: ignore[arg-type]
        checkpointer=InMemorySaver(),
        base_tool_env=ToolEnv(),
        clock=clock,
    )

    async def _delegate() -> str:
        built = await builder(tenant_id=tenant, name="researcher", version="1.0.0", depth=1)
        return built.system_prompt

    assert await _delegate() == "b1"  # 跳过了服务
    clock.now += 59
    assert await _delegate() == "b1"  # 窗口内复用
    clock.now += 2
    assert await _delegate() == "b2"  # 过了窗口重建;完整的这份按正常 TTL
    clock.now += 1800 - 1
    assert await _delegate() == "b2"
    assert calls == 2
