"""委派增强层 3 — POST /v1/agents/{name}/{version}/delegation-policy:generate。

辅助 LLM 读该 Agent 的 manifest 起草「委派策略」草稿(不落库);
dynamic_workers 关闭的 Agent 400;权限照配置写面(manifest:write);
purpose 注册进 LLM_SPAN_PURPOSES 单源契约;工具名单必须整体塞进给辅助
模型的 prompt(禁编造约束)。
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from control_plane.app import create_app
from control_plane.delegation_policy import (
    MAX_SYSTEM_PROMPT_CHARS,
    MAX_TOOLS,
    ToolBrief,
    build_delegation_policy_prompt,
)
from control_plane.runtime import AgentRuntime
from control_plane.settings import Settings
from expert_work.common.observability import LLM_SPAN_PURPOSES
from expert_work.runtime.runs import InMemoryRunStore, RunManager
from expert_work.runtime.stream_bridge import InMemoryStreamBridge
from orchestrator.agent_factory import BuiltAgent
from orchestrator.tools.registry import ToolCatalogEntry
from tests.auth_fixtures import TEST_AUDIENCE, TEST_ISSUER, build_test_jwt_verifier, make_test_jwt
from tests.test_agents_api import _VALID_YAML  # 同目录既有的合法 manifest(dynamic_workers 默认开)

# dynamic_workers 显式关闭的变体 —— 400 守卫用。
_DISABLED_YAML = _VALID_YAML + "  dynamic_workers:\n    enabled: false\n"

_CATALOG = (
    ToolCatalogEntry(
        name="bash",
        description="run a shell command",
        parameters={"type": "object"},
        source="builtin",
        from_skill=None,
        deferred=False,
    ),
    ToolCatalogEntry(
        name="mcp__gh__create_issue",
        description="create an issue on GitHub",
        parameters={"type": "object"},
        source="mcp:gh",
        from_skill=None,
        deferred=True,
    ),
)


class _FakeAux:
    """记录收到的 prompt,返回固定草稿。"""

    def __init__(self, draft: str = "五段式委派策略草稿") -> None:
        self.draft = draft
        self.prompts: list[str] = []
        self.tenant_ids: list[UUID] = []

    async def __call__(self, *, prompt: str, tenant_id: UUID) -> str:
        self.prompts.append(prompt)
        self.tenant_ids.append(tenant_id)
        return self.draft


class _RaisingAux:
    async def __call__(self, *, prompt: str, tenant_id: UUID) -> str:
        raise RuntimeError("provider exploded")


def _runtime() -> AgentRuntime:
    async def _build(
        spec: object, *, tenant_id: object | None = None, user_id: str | None = None
    ) -> BuiltAgent:
        del spec, tenant_id, user_id
        return BuiltAgent(graph=object(), system_prompt="", max_steps=1, tool_catalog=_CATALOG)  # type: ignore[arg-type]

    return AgentRuntime(
        run_manager=RunManager(store=InMemoryRunStore()),
        stream_bridge=InMemoryStreamBridge(),
        agent_builder=_build,
    )


@pytest.fixture
async def make_client():
    async def make(*, aux: object | None) -> tuple[AsyncClient, UUID]:
        settings = Settings(
            env="dev",
            auth_mode="dev",
            rate_limit_burst=10_000,
            rate_limit_per_second=10_000.0,
            oidc_issuer=TEST_ISSUER,
            oidc_audience=[TEST_AUDIENCE],
        )
        app = create_app(
            settings=settings,
            jwt_verifier=build_test_jwt_verifier(),
            agent_runtime=_runtime(),
        )
        if aux is not None:
            app.state.delegation_policy_aux = aux
        tenant_id = uuid4()
        headers = {"Authorization": f"Bearer {make_test_jwt(tenant_id=tenant_id)}"}
        client = AsyncClient(
            transport=ASGITransport(app=app), base_url="http://control-plane.test", headers=headers
        )
        return client, tenant_id

    return make


_GENERATE = "/v1/agents/code-reviewer/1.0.0/delegation-policy:generate"


@pytest.mark.asyncio
async def test_generate_returns_draft_and_feeds_manifest_to_aux(make_client) -> None:
    aux = _FakeAux(draft="  # 委派策略\n下放同构批量活。\n")
    client, _tenant = await make_client(aux=aux)
    async with client:
        assert (
            await client.post("/v1/agents", json={"manifest_yaml": _VALID_YAML})
        ).status_code == 201
        r = await client.post(_GENERATE)
        assert r.status_code == 200, r.text
        # 草稿原文返回(仅 strip),不落库。
        assert r.json() == {"success": True, "data": {"draft": "# 委派策略\n下放同构批量活。"}}
        # 工具名约束 — 构建出的工具名单(含 MCP wire 名 + 描述)整体进 prompt,
        # 并附带「只引用名单内名字/禁止编造」的硬性指令。
        assert len(aux.prompts) == 1
        prompt = aux.prompts[0]
        assert "- bash: run a shell command" in prompt
        assert "- mcp__gh__create_issue: create an issue on GitHub" in prompt
        assert "ONLY names from the TOOLS list" in prompt
        assert "Never invent a tool" in prompt
        # manifest 素材面 — system prompt 现文 + agent 名进 prompt。
        assert "you are a reviewer" in prompt
        assert "code-reviewer" in prompt


@pytest.mark.asyncio
async def test_generate_400_when_dynamic_workers_disabled(make_client) -> None:
    aux = _FakeAux()
    client, _tenant = await make_client(aux=aux)
    async with client:
        assert (
            await client.post("/v1/agents", json={"manifest_yaml": _DISABLED_YAML})
        ).status_code == 201
        r = await client.post(_GENERATE)
        assert r.status_code == 400, r.text
        detail = r.json()["detail"]
        assert detail["code"] == "DYNAMIC_WORKERS_DISABLED"
        assert "dynamic_workers" in detail["message"]
        # 守卫在 LLM 调用之前 — 辅助模型一次都不该被打到。
        assert aux.prompts == []


@pytest.mark.asyncio
async def test_generate_404_unknown_agent(make_client) -> None:
    client, _tenant = await make_client(aux=_FakeAux())
    async with client:
        assert (await client.post(_GENERATE)).status_code == 404


@pytest.mark.asyncio
async def test_generate_401_without_token(make_client) -> None:
    client, _tenant = await make_client(aux=_FakeAux())
    async with client:
        assert (
            await client.post("/v1/agents", json={"manifest_yaml": _VALID_YAML})
        ).status_code == 201
        r = await client.post(_GENERATE, headers={"Authorization": ""})
        assert r.status_code == 401


@pytest.mark.asyncio
async def test_generate_403_for_viewer_role(make_client) -> None:
    """权限照配置写面 — manifest:write;同租户 viewer 只有 read,403。"""
    aux = _FakeAux()
    client, tenant_id = await make_client(aux=aux)
    async with client:
        assert (
            await client.post("/v1/agents", json={"manifest_yaml": _VALID_YAML})
        ).status_code == 201
        viewer_jwt = make_test_jwt(tenant_id=tenant_id, subject="viewer-1", roles=("viewer",))
        r = await client.post(_GENERATE, headers={"Authorization": f"Bearer {viewer_jwt}"})
        assert r.status_code == 403, r.text
        assert aux.prompts == []


@pytest.mark.asyncio
async def test_generate_502_when_aux_fails(make_client) -> None:
    client, _tenant = await make_client(aux=_RaisingAux())
    async with client:
        assert (
            await client.post("/v1/agents", json={"manifest_yaml": _VALID_YAML})
        ).status_code == 201
        r = await client.post(_GENERATE)
        assert r.status_code == 502
        assert r.json()["detail"]["code"] == "DELEGATION_POLICY_GENERATION_FAILED"


@pytest.mark.asyncio
async def test_generate_503_when_aux_not_wired(make_client) -> None:
    """注入 runtime 的测试 app 不跑生产 lifespan 接线 — 明确 503 而非 500。"""
    client, _tenant = await make_client(aux=None)
    async with client:
        assert (
            await client.post("/v1/agents", json={"manifest_yaml": _VALID_YAML})
        ).status_code == 201
        r = await client.post(_GENERATE)
        assert r.status_code == 503
        assert r.json()["detail"]["code"] == "AUX_MODEL_UNAVAILABLE"


def test_purpose_registered_in_single_source_contract() -> None:
    """用途标记走单源契约,不绕开 — facade 测试另行断言 label 平价。"""
    assert LLM_SPAN_PURPOSES["expert_work.control_plane.delegation_policy"] == "delegation_policy"


def test_prompt_builder_truncates_long_fields_and_bounds_tools() -> None:
    from control_plane.manifest import ManifestLoader

    long_prompt_yaml = _VALID_YAML.replace(
        'template: "you are a reviewer"',
        f'template: "{"x" * (MAX_SYSTEM_PROMPT_CHARS + 500)}"',
    )
    spec = ManifestLoader().load_from_string(long_prompt_yaml)
    tools = [ToolBrief(name=f"tool_{i}", description="d " * 300) for i in range(MAX_TOOLS + 5)]
    prompt = build_delegation_policy_prompt(spec=spec, tools=tools)
    # system prompt 截断:全文不在,截断标记在。
    assert "x" * (MAX_SYSTEM_PROMPT_CHARS + 500) not in prompt
    assert "…(truncated)" in prompt
    # 工具数量有界:第 MAX_TOOLS 个之后的名字不再出现,但有省略行。
    assert f"- tool_{MAX_TOOLS - 1}:" in prompt
    assert f"- tool_{MAX_TOOLS}:" not in prompt
    assert "more tools omitted" in prompt
