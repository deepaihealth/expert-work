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


def _spec_dict(
    name: str, *, version: str = "1.0.0", display_name: str = "", description: str = ""
) -> dict[str, Any]:
    return {
        "apiVersion": "expert_work.io/v1",
        "kind": "Agent",
        "metadata": {"name": name, "version": version, "tenant": "acme"},
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

    async def seed_agent(
        self,
        name: str,
        *,
        version: str = "1.0.0",
        display_name: str = "",
        description: str = "",
    ) -> None:
        await self.app.state.agent_spec_repo.create(
            tenant_id=self.tenant_id,
            spec=AgentSpec.model_validate(
                deepcopy(
                    _spec_dict(
                        name, version=version, display_name=display_name, description=description
                    )
                )
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

    I-2:此前的版本只验证了一个方向 —— 遍历「目录里有的」再去打 run,所以
    「run 端点会接受、但目录压根没列出来」这个方向完全不设防。而 C-1 正是
    这个形态:``multi`` 因为有两个 ACTIVE 版本行占了分页配额,能被 run 端点
    正常接受,却可能因为分页打在版本行上而从目录的某一页静默消失。

    所以这里:①用小 ``limit`` 强制走完整个分页(拿到的是「目录说的完整
    code 集合」,不是只看第一页);②``run_accepted_codes`` 由种子里已知的
    ground-truth code 集合独立算出(不是从目录响应里派生的,否则目录漏了
    什么,派生集合也会跟着漏,测试永远不咬);③两个独立算出的集合做
    **相等**断言,不是「目录 ⊆ run 接受集」这种单向包含——单向包含测不出
    「目录漏了一个 run 能接受的 code」。顺带 seed 一个多版本 agent,同时
    覆盖 I-1 的分页交互面。

    用 ``ctx_write`` 而非共享的 ``ctx``:run 端点要求 ``session:write``
    (OPERATOR),``ctx`` 的 ``read`` scope(VIEWER)在到达可用性判据之前就会
    被 RBAC 挡在门外,那样测的是权限矩阵而不是本测试要证的东西。
    """
    ctx = ctx_write
    # multi 的两个版本创建在最后 —— 在 C-1 的 bug 形态(分页打在版本行、按
    # created_at DESC 排序)下,它们是创建时间最新的两行,会落进同一页,
    # 那一页去重后短于 limit,触发客户端「不足一页即最后一页」的标准循环
    # 提前 break,healthy / killed 因此被静默漏掉。但下面两条断言**不依赖
    # 这个种子顺序**才能咬住 bug(复审二轮建议 3):就算以后有人把
    # seed_agent 调用挪了位置、bug 表现成"跨页重复"而不是"整页消失",
    # 「翻页过程中无重复」那条断言照样会红。
    await ctx.seed_agent("healthy")
    await ctx.seed_agent("killed")
    await ctx.disable_agent("killed")
    await ctx.seed_agent("multi", version="1.0.0", display_name="v1")
    await ctx.seed_agent("multi", version="1.0.1", display_name="v2")
    seeded_codes = {"healthy", "multi", "killed"}

    # 走完整个分页 —— limit=2 强制至少翻两页(3 个 name)。
    catalog_agents: list[dict[str, Any]] = []
    codes_seen_in_order: list[str] = []
    offset = 0
    limit = 2
    while True:
        page = await ctx.client.get(
            "/v1/agent-catalog", params={"limit": limit, "offset": offset}, headers=ctx.headers
        )
        page_agents = page.json()["data"]["agents"]
        catalog_agents.extend(page_agents)
        codes_seen_in_order.extend(a["agent_code"] for a in page_agents)
        if len(page_agents) < limit:
            break
        offset += limit

    # 与 seed 顺序无关的断言:不管 multi 的两个版本行落在分页的哪个位置,
    # C-1 的 bug 形态要么让某个 code 跨页/同页重复出现(这条断言咬),
    # 要么让某个 code 整页消失(下面的集合相等断言咬)——两条一起才不用
    # 靠特定的种子顺序去触发 bug。
    assert len(codes_seen_in_order) == len(set(codes_seen_in_order)), (
        f"翻页过程中有 code 重复出现: {codes_seen_in_order}"
    )

    by_code = {a["agent_code"]: a["available"] for a in catalog_agents}
    catalog_codes = set(by_code)
    # C-1 的核心断言:翻完页拿到的 code 集合必须等于种下去的全部 code——
    # 一个都不能因为分页打在版本行上而从某一页静默消失(不管它 available
    # 是 true 还是 false)。
    assert catalog_codes == seeded_codes, (
        f"翻完页拿到的目录 code 集合是 {catalog_codes},应该是 {seeded_codes}"
    )
    catalog_available_codes = {code for code, available in by_code.items() if available}

    run_accepted_codes: set[str] = set()
    for code in seeded_codes:
        run = await ctx.client.post(
            f"/v1/agents/{code}/runs",
            json={"user_id": "cust-1", "input": "hi", "mode": "queue"},
            headers=ctx.headers,
        )
        accepted = run.status_code == 202
        assert accepted == by_code[code], (
            f"{code}: 目录说 available={by_code[code]},但 run 端点回 {run.status_code} "
            f"({run.text[:200]})"
        )
        if accepted:
            run_accepted_codes.add(code)

    # 两个独立来源(目录里 available=true 的子集 / 逐个打 run 得到 202 的
    # 集合)必须相等,不是「目录 ⊆ run 接受集」——后者测不出「目录漏了一个
    # run 能接受的 code」这个方向(注意:被禁用的 killed 仍会出现在
    # catalog_codes 里,但 available=false,所以不在这个子集里 —— 那是
    # 设计意图,不是要修的 bug)。
    assert catalog_available_codes == run_accepted_codes, (
        f"目录里 available=true 的 code 集合 {catalog_available_codes} 与 "
        f"run 端点接受的集合 {run_accepted_codes} 不相等"
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
async def test_multi_version_agent_appears_once_with_newest_display_name(ctx: _Ctx) -> None:
    """C-1 / I-1:同一个 name 有多个 ACTIVE 版本行是常态(发新版本只
    ``create``,没有代码把旧版本降级)。去重前这条断言此前完全没有测试覆盖
    ——评审把 ``if record.name in seen: continue`` 整段删掉,原先那 9 条测试
    依然全绿。目录里这个 code 只能出现一次,且带的是**新版本**的
    ``display_name``(两个版本给不同的 display_name,这样"取错版本"也会
    被逮到)。"""
    await ctx.seed_agent("multi", version="1.0.0", display_name="v1 name")
    await ctx.seed_agent("multi", version="1.0.1", display_name="v2 name")

    resp = await ctx.client.get("/v1/agent-catalog", headers=ctx.headers)

    agents = resp.json()["data"]["agents"]
    matches = [a for a in agents if a["agent_code"] == "multi"]
    assert len(matches) == 1, f"multi 应该只出现一次,实际: {matches}"
    assert matches[0]["display_name"] == "v2 name"


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
    # M-3 — total(去重后的总数,不分页)让客户端能判断翻完没有,不用只靠
    # 「这页数量 < limit」去猜。
    assert page.json()["data"]["total"] == 3

    rest = await ctx.client.get(
        "/v1/agent-catalog", params={"limit": 2, "offset": 2}, headers=ctx.headers
    )
    assert len(rest.json()["data"]["agents"]) == 1
    assert rest.json()["data"]["total"] == 3


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
