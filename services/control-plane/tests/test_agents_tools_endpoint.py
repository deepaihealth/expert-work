"""PR-A.3 §十.2 — GET /v1/agents/{name}/{version}/tools:整个工具注册表(含 deferred)。"""

from __future__ import annotations

from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from control_plane.app import create_app
from control_plane.runtime import AgentRuntime
from control_plane.settings import Settings
from expert_work.runtime.runs import InMemoryRunStore, RunManager
from expert_work.runtime.stream_bridge import InMemoryStreamBridge
from orchestrator import AgentFactoryError
from orchestrator.agent_factory import BuiltAgent
from orchestrator.tools.registry import ToolCatalogEntry
from tests.auth_fixtures import TEST_AUDIENCE, TEST_ISSUER, build_test_jwt_verifier, make_test_jwt
from tests.test_agents_api import _VALID_YAML  # 同目录既有的合法 manifest

_CATALOG = (
    ToolCatalogEntry(
        name="bash",
        description="run a shell command",
        parameters={
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
        },
        source="builtin",
        from_skill=None,
        deferred=False,
    ),
    ToolCatalogEntry(
        name="mcp__gh__create_issue",
        description="create an issue",
        parameters={"type": "object"},
        source="mcp:gh",
        from_skill=None,
        deferred=True,
    ),
)


def _runtime(*, fail: bool = False) -> AgentRuntime:
    async def _build(
        spec: object, *, tenant_id: object | None = None, user_id: str | None = None
    ) -> BuiltAgent:
        del spec, tenant_id, user_id
        if fail:
            raise AgentFactoryError("no model key")
        return BuiltAgent(graph=object(), system_prompt="", max_steps=1, tool_catalog=_CATALOG)  # type: ignore[arg-type]

    return AgentRuntime(
        run_manager=RunManager(store=InMemoryRunStore()),
        stream_bridge=InMemoryStreamBridge(),
        agent_builder=_build,
    )


@pytest.fixture
async def client_factory():
    async def make(runtime: AgentRuntime) -> AsyncClient:
        settings = Settings(
            env="dev",
            auth_mode="dev",
            rate_limit_burst=10_000,
            rate_limit_per_second=10_000.0,
            oidc_issuer=TEST_ISSUER,
            oidc_audience=[TEST_AUDIENCE],
        )
        app = create_app(
            settings=settings, jwt_verifier=build_test_jwt_verifier(), agent_runtime=runtime
        )
        headers = {"Authorization": f"Bearer {make_test_jwt(tenant_id=uuid4())}"}
        return AsyncClient(
            transport=ASGITransport(app=app), base_url="http://control-plane.test", headers=headers
        )

    return make


@pytest.mark.asyncio
async def test_tools_endpoint_returns_full_catalog(client_factory) -> None:
    async with await client_factory(_runtime()) as client:
        assert (
            await client.post("/v1/agents", json={"manifest_yaml": _VALID_YAML})
        ).status_code == 201
        r = await client.get("/v1/agents/code-reviewer/1.0.0/tools")
        assert r.status_code == 200, r.text
        data = r.json()["data"]
        assert data["total"] == 2
        assert data["items"][0] == {
            "name": "bash",
            "description": "run a shell command",
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
            },
            "source": "builtin",
            "from_skill": None,
            "deferred": False,
        }
        assert data["items"][1]["deferred"] is True and data["items"][1]["source"] == "mcp:gh"


@pytest.mark.asyncio
async def test_tools_endpoint_404_unknown_and_422_when_build_fails(client_factory) -> None:
    async with await client_factory(_runtime()) as client:
        assert (await client.get("/v1/agents/nope/1.0.0/tools")).status_code == 404
    async with await client_factory(_runtime(fail=True)) as client:
        assert (
            await client.post("/v1/agents", json={"manifest_yaml": _VALID_YAML})
        ).status_code == 201
        r = await client.get("/v1/agents/code-reviewer/1.0.0/tools")
        assert r.status_code == 422
        assert "cannot be built" in r.text


@pytest.mark.asyncio
async def test_tools_endpoint_is_console_only(client_factory) -> None:
    """API key(对外平面)不能读工具 schema —— 它是管理面产物。

    写法照 ``test_console_lockdown.py`` 的 ``ctx`` fixture抄:一个真实的
    API-key 调用方解析成 ``subject_type == "service_account"`` 的 Principal
    (``sub_type="service_account"``),``console_only()`` 只认这一个谓词,不
    看 scope,所以给它最宽的 ``admin`` scope 也照样被挡 —— 403 只可能来自
    console_only,不会跟 ``require_key_scope`` 的 scope 缺口混在一起。
    断言的状态码/消息与 ``test_console_lockdown.py::test_api_key_is_denied_on_the_console_plane``
    完全一致。
    """
    async with await client_factory(_runtime()) as client:
        assert (
            await client.post("/v1/agents", json={"manifest_yaml": _VALID_YAML})
        ).status_code == 201
        key_jwt = make_test_jwt(
            tenant_id=uuid4(),
            subject="sa-test",
            sub_type="service_account",
            roles=(),
            scopes=("admin",),
        )
        r = await client.get(
            "/v1/agents/code-reviewer/1.0.0/tools",
            headers={"Authorization": f"Bearer {key_jwt}"},
        )
        assert r.status_code == 403, r.text
        assert r.json()["detail"]["message"] == (
            "console API is not available to API keys; use /v1/agents/{agent_code}/…"
        ), r.text
