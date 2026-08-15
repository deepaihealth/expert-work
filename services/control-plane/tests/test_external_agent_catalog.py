"""``GET /v1/agent-catalog`` —— 阶段 3 (3.1) 的对外 agent 目录。

对外平面新前缀:``/v1/agents`` 已被控制台面占死(``85abdb39`` 给它下面
9 条路由补了 ``console_only()``),它吐完整 manifest(系统提示词 / 工具
清单 / 模型配置),不能给第三方。这里只给四个字段。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from copy import deepcopy
from typing import Any
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from control_plane.app import create_app
from control_plane.audit import build_default_audit_logger
from control_plane.settings import Settings
from expert_work.common.lifecycle import Lifecycle
from expert_work.persistence.audit_log import InMemoryAuditLogStore
from expert_work.protocol import AgentSpec, AgentSpecStatus
from expert_work.runtime.runs import InMemoryRunEventStore, InMemoryRunStore
from tests.agent_fixtures import stub_agent_runtime
from tests.auth_fixtures import (
    TEST_AUDIENCE,
    TEST_ISSUER,
    build_test_jwt_verifier,
    make_test_jwt,
)


def _spec_dict(name: str, *, display_name: str = "", description: str = "") -> dict[str, Any]:
    return {
        "apiVersion": "expert_work.io/v1",
        "kind": "Agent",
        "metadata": {"name": name, "version": "1.0.0", "tenant": "acme"},
        "spec": {
            "display_name": display_name,
            "description": description,
            "tenant_config": {},
            "model": {"provider": "anthropic", "name": "claude-sonnet-4-5"},
            "system_prompt": {"template": "you are support"},
            # ``sandbox`` 在 ``AgentSpecBody`` 上**没有默认值**(必填)——
            # 漏了它 ``model_validate`` 会因为 ``spec.sandbox`` 缺失而炸,
            # 报的错跟 display_name 毫无关系,很浪费排查时间。这一块照抄
            # ``tests/test_external_sessions.py`` 的 ``_SPEC``。
            "sandbox": {
                "resources": {"cpu": "1.0", "memory": "1Gi"},
                "network": {"egress": "proxy", "allowlist": ["api.anthropic.com"]},
                "filesystem": {"readonly_root": True, "writable": ["/workspace"]},
            },
        },
    }


def _build_settings() -> Settings:
    return Settings(
        service_name="control_plane_test",
        env="dev",
        auth_mode="dev",
        db_dsn="postgresql+asyncpg://test@localhost/test",
        rate_limit_burst=10_000,
        rate_limit_per_second=10_000.0,
        oidc_issuer=TEST_ISSUER,
        oidc_audience=[TEST_AUDIENCE],
    )


class _Ctx:
    def __init__(self, client: AsyncClient, app: Any, tenant_id: UUID, headers: dict[str, str]):
        self.client = client
        self.app = app
        self.tenant_id = tenant_id
        self.headers = headers

    async def seed_agent(self, name: str, *, display_name: str = "", description: str = "") -> None:
        await self.app.state.agent_spec_repo.create(
            tenant_id=self.tenant_id,
            spec=AgentSpec.model_validate(
                deepcopy(_spec_dict(name, display_name=display_name, description=description))
            ),
            spec_sha256="a" * 64,
            created_by="seed",
        )

    async def disable_agent(self, name: str) -> None:
        await self.app.state.agent_disable_repo.set_disabled(
            tenant_id=self.tenant_id,
            agent_name=name,
            disabled=True,
            reason="kill switch",
            disabled_by="admin",
        )


def _ctx_factory(scopes: tuple[str, ...], *, with_run_store: bool = False) -> Any:
    async def _make() -> AsyncIterator[_Ctx]:
        lifecycle = Lifecycle()
        lifecycle.mark_ready()
        # ``with_run_store`` wires a durable ``RunStore`` (mirrors
        # ``test_external_idempotency.py``'s ``_external_ctx``) — only needed
        # by the one test that actually calls ``POST .../runs`` with
        # ``mode: "queue"``; every other test in this module never reaches
        # the run endpoint, so it stays off the plain ``stub_agent_runtime()``
        # path used by the fixture this was copied from
        # (``test_external_sessions.py``).
        run_store = InMemoryRunStore() if with_run_store else None
        run_event_store = InMemoryRunEventStore() if with_run_store else None
        app = create_app(
            settings=_build_settings(),
            lifecycle=lifecycle,
            jwt_verifier=build_test_jwt_verifier(),
            audit_logger=build_default_audit_logger(InMemoryAuditLogStore()),
            agent_runtime=stub_agent_runtime(run_store=run_store, run_event_store=run_event_store),
            run_repo=run_store,
            run_event_repo=run_event_store,
        )
        tenant_id = uuid4()
        jwt = make_test_jwt(
            tenant_id=tenant_id,
            subject="sa-catalog",
            sub_type="service_account",
            roles=(),
            scopes=scopes,
        )
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://cp.test") as client:
            yield _Ctx(client, app, tenant_id, {"Authorization": f"Bearer {jwt}"})

    return _make


ctx = pytest.fixture(_ctx_factory(("read",)))
ctx_no_scope = pytest.fixture(_ctx_factory(()))
# ``POST /v1/agents/{code}/runs`` requires ``session:write`` (OPERATOR role);
# ``read`` scope only maps to VIEWER, which would 403 on RBAC alone before the
# catalog's availability logic is ever exercised. Only the cross-endpoint
# consistency test below needs to actually invoke the run endpoint, which is
# also why it's the only fixture wired with a durable ``RunStore``
# (``mode: "queue"`` 500s without one — ``RunManager.enqueue`` requires it).
ctx_write = pytest.fixture(_ctx_factory(("write",), with_run_store=True))


@pytest.mark.asyncio
async def test_catalog_returns_the_four_fields(ctx: _Ctx) -> None:
    await ctx.seed_agent("report-writer", display_name="报表助手", description="生成周报")

    resp = await ctx.client.get("/v1/agent-catalog", headers=ctx.headers)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["success"] is True and body["error"] is None
    agents = body["data"]["agents"]
    assert agents == [
        {
            "agent_code": "report-writer",
            "display_name": "报表助手",
            "description": "生成周报",
            "available": True,
        }
    ]


@pytest.mark.asyncio
async def test_display_name_falls_back_to_agent_code_when_blank(ctx: _Ctx) -> None:
    """对外响应里 ``display_name`` 永远非空 —— 客户端不用做空判断。"""
    await ctx.seed_agent("no-display-name")

    resp = await ctx.client.get("/v1/agent-catalog", headers=ctx.headers)

    assert resp.json()["data"]["agents"][0]["display_name"] == "no-display-name"


@pytest.mark.asyncio
async def test_disabled_agent_is_listed_but_unavailable(ctx: _Ctx) -> None:
    """禁用的 agent 出现在列表里、``available: false`` —— 客户端界面上
    置灰比「凭空消失」好排查。"""
    await ctx.seed_agent("killed")
    await ctx.disable_agent("killed")

    resp = await ctx.client.get("/v1/agent-catalog", headers=ctx.headers)

    agents = {a["agent_code"]: a for a in resp.json()["data"]["agents"]}
    assert agents["killed"]["available"] is False


@pytest.mark.asyncio
async def test_available_matches_what_the_run_endpoint_actually_does(ctx_write: _Ctx) -> None:
    """**这条是本 task 的核心断言。**

    目录说 ``available: false``,那么直接发 run 就必须真的被拒;目录说
    ``true``,run 就必须被接受。两处判据各自漂移,会让客户端列出一个
    「点了就 403」的 agent —— 最难排查的那类不一致。

    所以这里不是分别测两个端点,而是在**同一个测试**里把两边对起来。

    用 ``ctx_write`` 而非共享的 ``ctx``:run 端点要求 ``session:write``
    (OPERATOR),``ctx`` 的 ``read`` scope(VIEWER)在到达可用性判据之前就会
    被 RBAC 挡在门外,那样测的是权限矩阵而不是本测试要证的东西。
    """
    ctx = ctx_write
    await ctx.seed_agent("healthy")
    await ctx.seed_agent("killed")
    await ctx.disable_agent("killed")

    catalog = await ctx.client.get("/v1/agent-catalog", headers=ctx.headers)
    by_code = {a["agent_code"]: a["available"] for a in catalog.json()["data"]["agents"]}

    for code, available in by_code.items():
        run = await ctx.client.post(
            f"/v1/agents/{code}/runs",
            json={"user_id": "cust-1", "input": "hi", "mode": "queue"},
            headers=ctx.headers,
        )
        accepted = run.status_code == 202
        assert accepted == available, (
            f"{code}: 目录说 available={available},但 run 端点回 {run.status_code} "
            f"({run.text[:200]})"
        )


@pytest.mark.asyncio
async def test_agent_with_no_active_version_is_absent_from_the_catalog(ctx: _Ctx) -> None:
    """目录 = 「能调什么」。一个 code 只剩 DEPRECATED 版本时没有任何可调版本
    (run 端点会 404 AGENT_NOT_FOUND),所以它不出现在目录里 —— 而不是以
    ``available: false`` 出现。

    与 kill-switch 禁用**刻意不同**:那是可逆的临时状态,置灰等它回来是对的;
    只剩 deprecated 是终态,列一个永远 false 的条目对客户端是噪音,而且
    deprecated 属于租户内部的版本管理状态,不该对第三方暴露。
    """
    await ctx.seed_agent("healthy")
    await ctx.seed_agent("deprecated-only")
    await ctx.app.state.agent_spec_repo.update_status(
        tenant_id=ctx.tenant_id,
        name="deprecated-only",
        version="1.0.0",
        status=AgentSpecStatus.DEPRECATED,
    )

    resp = await ctx.client.get("/v1/agent-catalog", headers=ctx.headers)

    codes = {a["agent_code"] for a in resp.json()["data"]["agents"]}
    assert codes == {"healthy"}, f"deprecated-only 不该出现在目录里,实际: {codes}"


@pytest.mark.asyncio
async def test_pagination(ctx: _Ctx) -> None:
    for i in range(3):
        await ctx.seed_agent(f"agent-{i}")

    page = await ctx.client.get(
        "/v1/agent-catalog", params={"limit": 2, "offset": 0}, headers=ctx.headers
    )
    assert len(page.json()["data"]["agents"]) == 2
    assert page.json()["data"]["limit"] == 2
    assert page.json()["data"]["offset"] == 0

    rest = await ctx.client.get(
        "/v1/agent-catalog", params={"limit": 2, "offset": 2}, headers=ctx.headers
    )
    assert len(rest.json()["data"]["agents"]) == 1


@pytest.mark.asyncio
async def test_zero_scope_key_is_denied(ctx_no_scope: _Ctx) -> None:
    """零 scope 的 key 读不到目录 —— #1153 那轮堵的就是零权限 key 的读写绕行。"""
    resp = await ctx_no_scope.client.get("/v1/agent-catalog", headers=ctx_no_scope.headers)
    assert resp.status_code == 403, resp.text


@pytest.mark.asyncio
async def test_employee_jwt_is_denied(ctx: _Ctx) -> None:
    """对外平面必须拒绝人类 —— ``external_only()`` 的既有铁律。"""
    employee = make_test_jwt(tenant_id=ctx.tenant_id, subject=str(uuid4()), roles=("admin",))
    resp = await ctx.client.get(
        "/v1/agent-catalog", headers={"Authorization": f"Bearer {employee}"}
    )
    assert resp.status_code == 403, resp.text


@pytest.mark.asyncio
async def test_catalog_never_leaks_the_manifest(ctx: _Ctx) -> None:
    """响应里只能有那四个字段 —— 系统提示词 / 工具清单 / 模型配置属于
    控制台面,``85abdb39`` 刻意对第三方关死的。逐字段白名单断言,
    这样将来往 DTO 里多塞一个字段会立刻失败。"""
    await ctx.seed_agent("report-writer", display_name="x", description="y")

    resp = await ctx.client.get("/v1/agent-catalog", headers=ctx.headers)

    for agent in resp.json()["data"]["agents"]:
        assert set(agent.keys()) == {"agent_code", "display_name", "description", "available"}
    assert "claude-sonnet" not in resp.text
    assert "you are support" not in resp.text
