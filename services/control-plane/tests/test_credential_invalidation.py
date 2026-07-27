"""凭据写入口连动失效 built-agent / secret 缓存 — 二期 PR2 T2.

修「换 LLM key 要重启容器才生效」:凭据轮换是同 secret_ref 原地覆写值
(``_canonical_secret_name`` 对同 (provider,key_id) 恒同 slot 名),已构建
agent 里烤住的旧明文 key 无从发现 —— 唯一解是每个写入口显式失效缓存。

覆盖:

* ``/v1/platform/credentials`` 全部 10 个写入口 → ``AgentRuntime`` +
  ``credential_value_cache``(T3 产物,duck-typed spy)的对应失效调用;
* ``AgentRuntime.register_user_invalidation_hook`` → ``invalidate_user``
  fan-out;
* ``make_child_agent_builder(register_user_invalidation=...)`` 只清匹配
  (tenant, oauth_user) 的 5-tuple 子缓存条目;
* ``McpOAuthRefresher`` 刷新 token 后回调 ``invalidate_user``;
* ``app.state`` 无 runtime / cache 时 getattr 兜底不 500。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from langgraph.checkpoint.memory import InMemorySaver

from control_plane.app import create_app
from control_plane.auth import JWTVerifier
from control_plane.mcp_oauth_refresh import McpOAuthRefresher
from control_plane.runtime import AgentRuntime
from control_plane.settings import Settings
from control_plane.subagent_runtime import make_child_agent_builder
from control_plane.tenant_scope import bypass_rls_session
from expert_work.common.lifecycle import Lifecycle
from expert_work.persistence import (
    InMemoryMcpConnectorCatalogStore,
    InMemoryMcpOAuthConnectionStore,
)
from expert_work.persistence.agent_spec import InMemoryAgentSpecStore
from expert_work.protocol import (
    AgentSpec,
    McpConnectorAuthSchema,
    McpConnectorCatalogUpsert,
    McpOAuthConnectionPatch,
    McpOAuthConnectionRecord,
    Role,
)
from expert_work.runtime.runs import RunManager
from expert_work.runtime.stream_bridge import InMemoryStreamBridge
from expert_work.testing import InMemorySecretStore
from orchestrator import BuiltAgent, ToolEnv
from tests.auth_fixtures import make_test_jwt

# ---------------------------------------------------------------------------
# API 层:10 个写入口 → spy runtime / spy value-cache
# ---------------------------------------------------------------------------

_BASE = "/v1/platform/credentials"


class _SpyInvalidator:
    """Duck-typed spy——同时充当 AgentRuntime 与 credential_value_cache
    (helper 只 getattr + 调 ``invalidate_all`` / ``invalidate_tenant``)。"""

    def __init__(self) -> None:
        self.all_calls = 0
        self.tenant_calls: list[UUID] = []

    def invalidate_all(self) -> None:
        self.all_calls += 1

    def invalidate_tenant(self, tenant_id: UUID) -> None:
        self.tenant_calls.append(tenant_id)

    def reset(self) -> None:
        self.all_calls = 0
        self.tenant_calls = []


def _headers(subject: UUID) -> dict[str, str]:
    return {"Authorization": f"Bearer {make_test_jwt(tenant_id=uuid4(), subject=str(subject))}"}


async def _seed_admin(app: object) -> UUID:
    sys_admin_id = uuid4()
    await app.state.role_binding_repo.create(  # type: ignore[attr-defined]
        subject_type="user",
        subject_id=sys_admin_id,
        tenant_id=None,
        role=Role.SYSTEM_ADMIN,
        platform_scope=True,
        granted_by="seed",
    )
    return sys_admin_id


async def _seed_tenant(app: object) -> UUID:
    tenant_id = uuid4()
    await app.state.tenant_config_repo.create(  # type: ignore[attr-defined]
        tenant_id=tenant_id, display_name="T", actor_id="seed"
    )
    return tenant_id


@pytest.fixture
async def spy_app(
    settings: Settings,
    lifecycle: Lifecycle,
    jwt_verifier: JWTVerifier,
) -> AsyncIterator[tuple[AsyncClient, UUID, Any, _SpyInvalidator, _SpyInvalidator]]:
    app = create_app(settings=settings, lifecycle=lifecycle, jwt_verifier=jwt_verifier)
    admin = await _seed_admin(app)
    runtime_spy = _SpyInvalidator()
    cache_spy = _SpyInvalidator()
    app.state.agent_runtime = runtime_spy
    app.state.credential_value_cache = cache_spy
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://control-plane.test") as client:
        yield client, admin, app, runtime_spy, cache_spy


_WRITE = {"secret_ref": "kms://platform/x", "enabled": True}

# 平台级 upsert 3 入口(upsert_provider / upsert_provider_key / upsert_tool)。
_PLATFORM_PUTS = [
    pytest.param(f"{_BASE}/providers/anthropic", id="upsert_provider"),
    pytest.param(f"{_BASE}/providers/anthropic/keys/backup", id="upsert_provider_key"),
    pytest.param(f"{_BASE}/tools/web_search", id="upsert_tool"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("path", _PLATFORM_PUTS)
async def test_platform_put_invalidates_all_built_agents(
    spy_app: tuple[AsyncClient, UUID, Any, _SpyInvalidator, _SpyInvalidator], path: str
) -> None:
    client, admin, _app, runtime_spy, cache_spy = spy_app
    resp = await client.put(path, json=_WRITE, headers=_headers(admin))
    assert resp.status_code == 200, resp.text
    assert runtime_spy.all_calls == 1
    assert runtime_spy.tenant_calls == []
    assert cache_spy.all_calls == 1
    assert cache_spy.tenant_calls == []


# 平台级 delete 3 入口(delete_provider / delete_provider_key / delete_tool)。
_PLATFORM_DELETES = [
    pytest.param(f"{_BASE}/providers/openai", f"{_BASE}/providers/openai", id="delete_provider"),
    pytest.param(
        f"{_BASE}/providers/openai/keys/backup",
        f"{_BASE}/providers/openai/keys/backup",
        id="delete_provider_key",
    ),
    pytest.param(f"{_BASE}/tools/web_search", f"{_BASE}/tools/web_search", id="delete_tool"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("seed_path", "delete_path"), _PLATFORM_DELETES)
async def test_platform_delete_invalidates_all_built_agents(
    spy_app: tuple[AsyncClient, UUID, Any, _SpyInvalidator, _SpyInvalidator],
    seed_path: str,
    delete_path: str,
) -> None:
    client, admin, _app, runtime_spy, cache_spy = spy_app
    seeded = await client.put(seed_path, json=_WRITE, headers=_headers(admin))
    assert seeded.status_code == 200, seeded.text
    runtime_spy.reset()
    cache_spy.reset()

    resp = await client.delete(delete_path, headers=_headers(admin))
    assert resp.status_code == 204, resp.text
    assert runtime_spy.all_calls == 1
    assert runtime_spy.tenant_calls == []
    assert cache_spy.all_calls == 1
    assert cache_spy.tenant_calls == []


# 租户级 4 入口(upsert / delete 各配 provider / tool)→ 只清该租户。
_TENANT_SUFFIXES = [
    pytest.param("providers/anthropic", id="tenant_provider"),
    pytest.param("tools/web_search", id="tenant_tool"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("suffix", _TENANT_SUFFIXES)
async def test_tenant_put_invalidates_only_that_tenant(
    spy_app: tuple[AsyncClient, UUID, Any, _SpyInvalidator, _SpyInvalidator], suffix: str
) -> None:
    client, admin, app, runtime_spy, cache_spy = spy_app
    tenant_id = await _seed_tenant(app)
    resp = await client.put(
        f"{_BASE}/tenants/{tenant_id}/{suffix}", json=_WRITE, headers=_headers(admin)
    )
    assert resp.status_code == 200, resp.text
    assert runtime_spy.tenant_calls == [tenant_id]
    assert runtime_spy.all_calls == 0
    assert cache_spy.tenant_calls == [tenant_id]
    assert cache_spy.all_calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("suffix", _TENANT_SUFFIXES)
async def test_tenant_delete_invalidates_only_that_tenant(
    spy_app: tuple[AsyncClient, UUID, Any, _SpyInvalidator, _SpyInvalidator], suffix: str
) -> None:
    client, admin, app, runtime_spy, cache_spy = spy_app
    tenant_id = await _seed_tenant(app)
    seeded = await client.put(
        f"{_BASE}/tenants/{tenant_id}/{suffix}", json=_WRITE, headers=_headers(admin)
    )
    assert seeded.status_code == 200, seeded.text
    runtime_spy.reset()
    cache_spy.reset()

    resp = await client.delete(f"{_BASE}/tenants/{tenant_id}/{suffix}", headers=_headers(admin))
    assert resp.status_code == 204, resp.text
    assert runtime_spy.tenant_calls == [tenant_id]
    assert runtime_spy.all_calls == 0
    assert cache_spy.tenant_calls == [tenant_id]
    assert cache_spy.all_calls == 0


@pytest.mark.asyncio
async def test_put_without_runtime_or_cache_still_succeeds(
    settings: Settings,
    lifecycle: Lifecycle,
    jwt_verifier: JWTVerifier,
) -> None:
    """getattr 兜底:部分测试 app 不装 runtime / T3 前没有 value cache——写
    入口不能因此 500。"""
    app = create_app(settings=settings, lifecycle=lifecycle, jwt_verifier=jwt_verifier)
    admin = await _seed_admin(app)
    del app.state.agent_runtime  # 模拟不装 runtime 的测试 app
    # T3 起 cache 挂在 lifespan 里——不跑 lifespan 的测试 app 天然没有它。
    assert not hasattr(app.state, "credential_value_cache")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://control-plane.test") as client:
        resp = await client.put(
            f"{_BASE}/providers/anthropic", json=_WRITE, headers=_headers(admin)
        )
    assert resp.status_code == 200, resp.text


# ---------------------------------------------------------------------------
# AgentRuntime.register_user_invalidation_hook → invalidate_user fan-out
# ---------------------------------------------------------------------------


def _bare_runtime() -> AgentRuntime:
    async def _builder(
        spec: AgentSpec, *, tenant_id: UUID | None = None, user_id: str | None = None
    ) -> object:
        return object()

    return AgentRuntime(
        run_manager=RunManager(store=None),  # type: ignore[arg-type]
        stream_bridge=InMemoryStreamBridge(),
        agent_builder=_builder,  # type: ignore[arg-type]
    )


def test_invalidate_user_fans_out_to_registered_user_hooks() -> None:
    runtime = _bare_runtime()
    seen: list[tuple[UUID, str]] = []
    runtime.register_user_invalidation_hook(lambda t, u: seen.append((t, u)))
    t = uuid4()
    runtime.invalidate_user(t, "u1")
    assert seen == [(t, "u1")]


def test_invalidate_user_without_hooks_is_a_noop() -> None:
    runtime = _bare_runtime()
    runtime.invalidate_user(uuid4(), "u1")  # 未注册任何 hook——不炸


# ---------------------------------------------------------------------------
# make_child_agent_builder(register_user_invalidation=...) — 5-tuple 定点清除
# ---------------------------------------------------------------------------

_SHA = "a" * 64


def _spec(name: str, version: str = "1.0.0") -> AgentSpec:
    return AgentSpec.model_validate(
        {
            "apiVersion": "expert_work.io/v1",
            "kind": "Agent",
            "metadata": {"name": name, "version": version, "tenant": "t"},
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


@pytest.fixture
def build_calls(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    async def _fake_build_agent(spec: AgentSpec, **kwargs: Any) -> BuiltAgent:
        calls.append({"spec": spec, **kwargs})
        return BuiltAgent(graph=object(), system_prompt="", max_steps=1)  # type: ignore[arg-type]

    monkeypatch.setattr("control_plane.subagent_runtime.build_agent", _fake_build_agent)
    return calls


@pytest.mark.asyncio
async def test_user_invalidator_clears_only_matching_user_entries(
    build_calls: list[dict[str, Any]],
) -> None:
    """user invalidator 只清匹配 (tenant, oauth_user) 的 5-tuple 条目;
    共享 4-tuple 与他人的 5-tuple 条目保留。"""
    from orchestrator.tools import MCPServerPool, RecordingMCPClient

    pools: dict[str, MCPServerPool] = {}

    async def _user_provider(tid: object, uid: str) -> MCPServerPool:
        if uid not in pools:
            p = MCPServerPool()
            await p.add(f"srv-{uid}", RecordingMCPClient())
            pools[uid] = p
        return pools[uid]

    tenant = uuid4()
    store = InMemoryAgentSpecStore()
    await store.create(
        tenant_id=tenant, spec=_spec("researcher"), spec_sha256=_SHA, created_by="test"
    )
    user_invalidators: list[Any] = []
    builder = make_child_agent_builder(
        spec_store=store,
        secret_store=InMemorySecretStore(),
        checkpointer=InMemorySaver(),
        base_tool_env=ToolEnv(),
        user_mcp_oauth_pool_provider=_user_provider,
        register_user_invalidation=user_invalidators.append,
    )
    assert len(user_invalidators) == 1
    invalidate_user = user_invalidators[0]

    # A / B 各一个 5-tuple 条目 + 一个共享 4-tuple 条目。
    await builder(tenant_id=tenant, name="researcher", version="1.0.0", depth=1, oauth_user_id="A")
    await builder(tenant_id=tenant, name="researcher", version="1.0.0", depth=1, oauth_user_id="B")
    await builder(tenant_id=tenant, name="researcher", version="1.0.0", depth=1)
    assert len(build_calls) == 3

    invalidate_user(tenant, "A")

    # A 重建(第 4 次 build);B 与共享条目仍命中缓存。
    await builder(tenant_id=tenant, name="researcher", version="1.0.0", depth=1, oauth_user_id="A")
    await builder(tenant_id=tenant, name="researcher", version="1.0.0", depth=1, oauth_user_id="B")
    await builder(tenant_id=tenant, name="researcher", version="1.0.0", depth=1)
    assert len(build_calls) == 4


@pytest.mark.asyncio
async def test_user_invalidator_other_tenant_untouched(
    build_calls: list[dict[str, Any]],
) -> None:
    from orchestrator.tools import MCPServerPool, RecordingMCPClient

    async def _user_provider(tid: object, uid: str) -> MCPServerPool:
        p = MCPServerPool()
        await p.add("srv", RecordingMCPClient())
        return p

    t1, t2 = uuid4(), uuid4()
    store = InMemoryAgentSpecStore()
    for tid in (t1, t2):
        await store.create(
            tenant_id=tid, spec=_spec("researcher"), spec_sha256=_SHA, created_by="test"
        )
    user_invalidators: list[Any] = []
    builder = make_child_agent_builder(
        spec_store=store,
        secret_store=InMemorySecretStore(),
        checkpointer=InMemorySaver(),
        base_tool_env=ToolEnv(),
        user_mcp_oauth_pool_provider=_user_provider,
        register_user_invalidation=user_invalidators.append,
    )

    await builder(tenant_id=t1, name="researcher", version="1.0.0", depth=1, oauth_user_id="A")
    await builder(tenant_id=t2, name="researcher", version="1.0.0", depth=1, oauth_user_id="A")
    assert len(build_calls) == 2

    user_invalidators[0](t1, "A")  # 同名 user、另一租户的条目不受影响

    await builder(tenant_id=t2, name="researcher", version="1.0.0", depth=1, oauth_user_id="A")
    assert len(build_calls) == 2  # t2 仍命中缓存


# ---------------------------------------------------------------------------
# McpOAuthRefresher — token 刷新(同 ref 原地覆写)后回调 invalidate_user
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 6, 6, 12, 0, tzinfo=UTC)
_MCP_URL = "https://mcp.linear.app/sse"


def _token_handler() -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/.well-known/oauth-protected-resource":
            return httpx.Response(
                200,
                json={"authorization_servers": ["https://auth.linear.app"], "resource": _MCP_URL},
            )
        if path == "/.well-known/oauth-authorization-server":
            return httpx.Response(
                200,
                json={
                    "authorization_endpoint": "https://auth.linear.app/authorize",
                    "token_endpoint": "https://auth.linear.app/token",
                },
            )
        if path == "/token":
            return httpx.Response(200, json={"access_token": "AT2", "expires_in": 3600})
        return httpx.Response(404)

    return handler


async def _seed_oauth(
    *,
    tenant_id: UUID,
    user_id: str,
    expires_at: datetime,
) -> tuple[
    InMemoryMcpConnectorCatalogStore,
    InMemoryMcpOAuthConnectionStore,
    InMemorySecretStore,
    McpOAuthConnectionRecord,
]:
    cat = InMemoryMcpConnectorCatalogStore()
    oauth = InMemoryMcpOAuthConnectionStore()
    sec = InMemorySecretStore()
    async with bypass_rls_session():
        entry = await cat.create(
            upsert=McpConnectorCatalogUpsert(
                name="linear",
                display_name="Linear",
                transport="sse",
                url_template=_MCP_URL,
                auth_type="oauth2",
                auth_schema=McpConnectorAuthSchema(),
                oauth_client_id="cid",
                oauth_scopes="read",
            ),
            actor_id="seed",
        )
    rec = await oauth.create(
        tenant_id=tenant_id,
        user_id=user_id,
        catalog_id=entry.id,
        name="linear",
        resolved_url=_MCP_URL,
        oauth_state="st",
        pkce_verifier="pv",
    )
    access_ref = f"secret://expert-work/tenant/{tenant_id}/mcp-oauth/{rec.id}/access"
    refresh_ref = f"secret://expert-work/tenant/{tenant_id}/mcp-oauth/{rec.id}/refresh"
    await sec.put(access_ref.removeprefix("secret://"), "AT1")
    await sec.put(refresh_ref.removeprefix("secret://"), "RT1")
    connected = await oauth.update(
        connection_id=rec.id,
        tenant_id=tenant_id,
        user_id=user_id,
        patch=McpOAuthConnectionPatch(
            status="connected",
            access_token_ref=access_ref,
            refresh_token_ref=refresh_ref,
            token_expires_at=expires_at,
            clear_flow_state=True,
        ),
    )
    return cat, oauth, sec, connected


def _refresher_with_spy(
    cat: InMemoryMcpConnectorCatalogStore,
    oauth: InMemoryMcpOAuthConnectionStore,
    sec: InMemorySecretStore,
    seen: list[tuple[UUID, str]],
) -> McpOAuthRefresher:
    def factory() -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(_token_handler()))

    return McpOAuthRefresher(
        oauth_store=oauth,
        catalog_store=cat,
        secret_store=sec,
        http_factory=factory,
        clock=lambda: _NOW,
        invalidate_user=lambda t, u: seen.append((t, u)),
    )


@pytest.mark.asyncio
async def test_refresh_invalidates_user_built_agents() -> None:
    """token 刷新原地覆写 secret 值——刷新成功后必须回调 invalidate_user,
    否则缓存的 per-user built agent 还烤着旧 token。"""
    tid, uid = uuid4(), "kc-user"
    cat, oauth, sec, rec = await _seed_oauth(
        tenant_id=tid, user_id=uid, expires_at=_NOW + timedelta(seconds=30)
    )
    seen: list[tuple[UUID, str]] = []
    out = await _refresher_with_spy(cat, oauth, sec, seen).ensure_fresh(rec)
    assert out is not None and out.status == "connected"
    assert await sec.get(rec.access_token_ref.removeprefix("secret://")) == "AT2"
    assert seen == [(tid, uid)]


@pytest.mark.asyncio
async def test_no_refresh_no_invalidation() -> None:
    tid, uid = uuid4(), "kc-user"
    cat, oauth, sec, rec = await _seed_oauth(
        tenant_id=tid, user_id=uid, expires_at=_NOW + timedelta(hours=1)
    )
    seen: list[tuple[UUID, str]] = []
    out = await _refresher_with_spy(cat, oauth, sec, seen).ensure_fresh(rec)
    assert out is not None
    assert await sec.get(rec.access_token_ref.removeprefix("secret://")) == "AT1"
    assert seen == []  # 没刷新就没失效
