"""``GET /v1/agents/{agent_code}/runs`` —— 阶段 3 (3.2) 的对外 run 列表。

现在第三方只能按 ``run_id`` 拿事件,列不出「这个用户在这个 agent 上跑过
哪些任务」,客户端要做「我的任务」列表只能自己在本地记 run_id。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from control_plane.app import create_app
from control_plane.audit import build_default_audit_logger
from control_plane.settings import Settings
from expert_work.common.lifecycle import Lifecycle
from expert_work.persistence.audit_log import InMemoryAuditLogStore
from expert_work.persistence.thread_meta import InMemoryThreadMetaStore
from expert_work.protocol import AgentSpec
from expert_work.runtime.runs import InMemoryRunEventStore, InMemoryRunStore, RunStatus
from tests.agent_fixtures import stub_agent_runtime
from tests.auth_fixtures import (
    TEST_AUDIENCE,
    TEST_ISSUER,
    build_test_jwt_verifier,
    make_test_jwt,
)

_SPEC: dict[str, Any] = {
    "apiVersion": "expert_work.io/v1",
    "kind": "Agent",
    "metadata": {"name": "support-bot", "version": "1.0.0", "tenant": "acme"},
    "spec": {
        "tenant_config": {},
        "model": {"provider": "anthropic", "name": "claude-sonnet-4-5"},
        "system_prompt": {"template": "you are support"},
        "sandbox": {
            "resources": {"cpu": "1.0", "memory": "1Gi"},
            "network": {"egress": "proxy", "allowlist": ["api.anthropic.com"]},
            "filesystem": {"readonly_root": True, "writable": ["/workspace"]},
        },
    },
}


def _spec(name: str = "support-bot") -> AgentSpec:
    raw = deepcopy(_SPEC)
    raw["metadata"]["name"] = name
    return AgentSpec.model_validate(raw)


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
    def __init__(
        self,
        client: AsyncClient,
        app: Any,
        tenant_id: UUID,
        headers: dict[str, str],
        run_store: InMemoryRunStore,
    ):
        self.client = client
        self.app = app
        self.tenant_id = tenant_id
        self.headers = headers
        self.run_store = run_store

    async def seed_agent(self) -> None:
        await self.app.state.agent_spec_repo.create(
            tenant_id=self.tenant_id, spec=_spec(), spec_sha256="a" * 64, created_by="seed"
        )

    async def seed_agent_named(self, name: str) -> None:
        await self.app.state.agent_spec_repo.create(
            tenant_id=self.tenant_id, spec=_spec(name), spec_sha256="b" * 64, created_by="seed"
        )


def _ctx_factory(scopes: tuple[str, ...]) -> Any:
    """Scope-parameterized fixture builder — mirrors
    ``test_external_agent_catalog.py``'s ``_ctx_factory`` so the scope-gate
    tests below (review Minor: this router's ``require("session", "read")``
    had zero test coverage — a refactor could delete that ``dependencies=``
    entirely and none of the original 10 tests, all run on an ``admin``-scope
    key, would notice) can each get their own key without duplicating the
    whole app-wiring block.
    """

    async def _make() -> AsyncIterator[_Ctx]:
        lifecycle = Lifecycle()
        lifecycle.mark_ready()
        # ``list_for_tenant(agent_name=...)`` (Task 5) is a join against
        # thread_meta on the SQL backend; the in-memory double emulates it by
        # holding the SAME ThreadMetaStore instance the app itself writes
        # sessions to (store.py:549's docstring) — without this, the filter
        # silently returns [] for every query (no join source wired).
        threads = InMemoryThreadMetaStore()
        run_store = InMemoryRunStore(thread_meta_store=threads)
        run_event_store = InMemoryRunEventStore()
        app = create_app(
            settings=_build_settings(),
            lifecycle=lifecycle,
            jwt_verifier=build_test_jwt_verifier(),
            audit_logger=build_default_audit_logger(InMemoryAuditLogStore()),
            agent_runtime=stub_agent_runtime(run_store=run_store, run_event_store=run_event_store),
            run_repo=run_store,
            run_event_repo=run_event_store,
            thread_meta_repo=threads,
        )
        tenant_id = uuid4()
        jwt = make_test_jwt(
            tenant_id=tenant_id,
            subject="sa-test",
            sub_type="service_account",
            roles=(),
            scopes=scopes,
        )
        headers = {"Authorization": f"Bearer {jwt}"}
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://cp.test") as client:
            yield _Ctx(client, app, tenant_id, headers, run_store)

    return _make


ctx = pytest.fixture(_ctx_factory(("admin",)))
ctx_no_scope = pytest.fixture(_ctx_factory(()))
ctx_read = pytest.fixture(_ctx_factory(("read",)))


@pytest.mark.asyncio
async def test_missing_user_id_is_422(ctx: _Ctx) -> None:
    """``user_id`` 必填、无默认 —— 漏传必须是 422,绝不能降级成
    「列出整个租户的 run」。与 ``GET .../sessions`` 同一条铁律。"""
    await ctx.seed_agent()
    resp = await ctx.client.get("/v1/agents/support-bot/runs", headers=ctx.headers)
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_lists_only_this_users_runs_on_this_agent(ctx: _Ctx) -> None:
    await ctx.seed_agent()
    mine = await ctx.client.post(
        "/v1/agents/support-bot/runs",
        json={"user_id": "cust-77", "input": "hi", "mode": "queue"},
        headers=ctx.headers,
    )
    assert mine.status_code == 202, mine.text
    await ctx.client.post(
        "/v1/agents/support-bot/runs",
        json={"user_id": "cust-99", "input": "hi", "mode": "queue"},
        headers=ctx.headers,
    )

    resp = await ctx.client.get(
        "/v1/agents/support-bot/runs", params={"user_id": "cust-77"}, headers=ctx.headers
    )

    assert resp.status_code == 200, resp.text
    runs = resp.json()["data"]["runs"]
    assert len(runs) == 1
    assert runs[0]["run_id"] == mine.json()["data"]["run_id"]
    assert runs[0]["session_id"] == mine.json()["data"]["thread_id"]


@pytest.mark.asyncio
async def test_unknown_user_returns_empty_and_mints_no_row(ctx: _Ctx) -> None:
    """读路径 ``mint=False``:一个这个租户从没见过的 ``user_id`` 返回空列表,
    **且不留下一行 ``tenant_user``**。第三方喷任意 user_id 不该在用户维度
    运维页上攒出一堆幽灵行(P1 复审 T3)。"""
    await ctx.seed_agent()
    before = await ctx.app.state.tenant_user_repo.list_by_tenant(ctx.tenant_id, limit=500)

    resp = await ctx.client.get(
        "/v1/agents/support-bot/runs", params={"user_id": "never-seen"}, headers=ctx.headers
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["runs"] == []
    after = await ctx.app.state.tenant_user_repo.list_by_tenant(ctx.tenant_id, limit=500)
    assert len(after) == len(before), "读路径不该 mint tenant_user 行"


@pytest.mark.asyncio
async def test_runs_of_another_agent_are_not_listed(ctx: _Ctx) -> None:
    """同一个用户在**别的 agent** 上的 run 不能出现在这里 —— 这是
    ``agent_name`` 过滤(Task 5 那个 join)真的生效的证明。"""
    await ctx.seed_agent()
    await ctx.seed_agent_named("other-bot")
    await ctx.client.post(
        "/v1/agents/support-bot/runs",
        json={"user_id": "cust-77", "input": "hi", "mode": "queue"},
        headers=ctx.headers,
    )
    await ctx.client.post(
        "/v1/agents/other-bot/runs",
        json={"user_id": "cust-77", "input": "hi", "mode": "queue"},
        headers=ctx.headers,
    )

    resp = await ctx.client.get(
        "/v1/agents/support-bot/runs", params={"user_id": "cust-77"}, headers=ctx.headers
    )

    assert len(resp.json()["data"]["runs"]) == 1


@pytest.mark.asyncio
async def test_session_id_filter_404s_on_someone_elses_session(ctx: _Ctx) -> None:
    """``session_id`` 指向别人的会话 → 404(不是 403、不是空列表)——
    响应不能携带存在性信息。"""
    await ctx.seed_agent()
    theirs = await ctx.client.post(
        "/v1/agents/support-bot/runs",
        json={"user_id": "cust-99", "input": "hi", "mode": "queue"},
        headers=ctx.headers,
    )
    their_session = theirs.json()["data"]["thread_id"]

    resp = await ctx.client.get(
        "/v1/agents/support-bot/runs",
        params={"user_id": "cust-77", "session_id": their_session},
        headers=ctx.headers,
    )

    assert resp.status_code == 404, resp.text
    assert resp.json()["error"]["code"] == "SESSION_NOT_FOUND"


@pytest.mark.asyncio
async def test_status_filter(ctx: _Ctx) -> None:
    await ctx.seed_agent()
    r = await ctx.client.post(
        "/v1/agents/support-bot/runs",
        json={"user_id": "cust-77", "input": "hi", "mode": "queue"},
        headers=ctx.headers,
    )
    run_id = UUID(r.json()["data"]["run_id"])
    await ctx.run_store.set_status(
        run_id=run_id,
        tenant_id=ctx.tenant_id,
        status=RunStatus.SUCCESS,
        updated_at=datetime.now(UTC),
        error=None,
        finished_at=datetime.now(UTC),
    )

    hit = await ctx.client.get(
        "/v1/agents/support-bot/runs",
        params={"user_id": "cust-77", "status": "success"},
        headers=ctx.headers,
    )
    miss = await ctx.client.get(
        "/v1/agents/support-bot/runs",
        params={"user_id": "cust-77", "status": "error"},
        headers=ctx.headers,
    )

    assert len(hit.json()["data"]["runs"]) == 1
    assert miss.json()["data"]["runs"] == []


@pytest.mark.asyncio
async def test_error_is_returned_verbatim_without_enrichment(ctx: _Ctx) -> None:
    """列表里的 ``error`` 必须是 ``agent_run.error`` 的**原样**,不加工。

    **这条断言的范围要说清楚**:它证明的是「端点没在存的东西之外再添
    内容」,**不是**「列表 error 与 SSE error 帧同源」。后者是代码层面的
    论证 —— 两边都是 ``str(exc)``(``orchestrator/sse.py:745`` 与
    ``:779``),同两处赋值 —— 这个 stub 测试环境不跑真 ``sse.py``,
    抓不到真实的 error 帧,所以那一半在这里**无法被断言**,只能靠代码
    阅读和真栈验收。

    它仍然有价值:哪天有人给列表里的 error 拼上堆栈、内部路径或
    ``claimed_by`` 实例 id,这条就红。
    """
    await ctx.seed_agent()
    r = await ctx.client.post(
        "/v1/agents/support-bot/runs",
        json={"user_id": "cust-77", "input": "hi", "mode": "queue"},
        headers=ctx.headers,
    )
    run_id = UUID(r.json()["data"]["run_id"])
    failure = "boom: upstream refused"
    await ctx.run_store.set_status(
        run_id=run_id,
        tenant_id=ctx.tenant_id,
        status=RunStatus.ERROR,
        updated_at=datetime.now(UTC),
        error=failure,
        finished_at=datetime.now(UTC),
    )

    resp = await ctx.client.get(
        "/v1/agents/support-bot/runs", params={"user_id": "cust-77"}, headers=ctx.headers
    )

    assert resp.json()["data"]["runs"][0]["error"] == failure


@pytest.mark.asyncio
async def test_pagination(ctx: _Ctx) -> None:
    await ctx.seed_agent()
    for _ in range(3):
        await ctx.client.post(
            "/v1/agents/support-bot/runs",
            json={"user_id": "cust-77", "input": "hi", "mode": "queue"},
            headers=ctx.headers,
        )

    page = await ctx.client.get(
        "/v1/agents/support-bot/runs",
        params={"user_id": "cust-77", "limit": 2, "offset": 0},
        headers=ctx.headers,
    )
    rest = await ctx.client.get(
        "/v1/agents/support-bot/runs",
        params={"user_id": "cust-77", "limit": 2, "offset": 2},
        headers=ctx.headers,
    )

    assert len(page.json()["data"]["runs"]) == 2
    assert len(rest.json()["data"]["runs"]) == 1


@pytest.mark.asyncio
async def test_response_shape_is_a_whitelist(ctx: _Ctx) -> None:
    """逐字段白名单 —— 往 DTO 里多塞一个字段(比如内部的 ``claimed_by``
    实例 id、``enqueued_input`` 原始输入)会立刻失败。"""
    await ctx.seed_agent()
    await ctx.client.post(
        "/v1/agents/support-bot/runs",
        json={"user_id": "cust-77", "input": "hi", "mode": "queue"},
        headers=ctx.headers,
    )

    resp = await ctx.client.get(
        "/v1/agents/support-bot/runs", params={"user_id": "cust-77"}, headers=ctx.headers
    )

    runs = resp.json()["data"]["runs"]
    assert runs, "fixture must actually produce a run or the loop below asserts nothing"
    for run in runs:
        assert set(run.keys()) == {
            "run_id",
            "session_id",
            "status",
            "created_at",
            "finished_at",
            "error",
        }


@pytest.mark.asyncio
async def test_session_id_filter_narrows_to_that_session(ctx: _Ctx) -> None:
    """``session_id`` is a real filter, not just an ownership check — a run on
    the SAME user's OTHER session must not appear (review Important I-1: with
    ``thread_ids = [session_id]`` deleted, all 9 original tests stayed green,
    because none of them ever had a second session to leak from)."""
    await ctx.seed_agent()
    first = await ctx.client.post(
        "/v1/agents/support-bot/runs",
        json={"user_id": "cust-77", "input": "hi", "mode": "queue"},
        headers=ctx.headers,
    )
    first_session = first.json()["data"]["thread_id"]
    second = await ctx.client.post(
        "/v1/agents/support-bot/runs",
        json={"user_id": "cust-77", "input": "hi", "mode": "queue", "session_id": None},
        headers=ctx.headers,
    )
    # Two POSTs with no session_id each mint their own new session — assert
    # that actually happened, or this test would silently degrade to
    # re-testing "only this user's runs" instead of "only this session's".
    assert second.json()["data"]["thread_id"] != first_session

    resp = await ctx.client.get(
        "/v1/agents/support-bot/runs",
        params={"user_id": "cust-77", "session_id": first_session},
        headers=ctx.headers,
    )

    assert resp.status_code == 200, resp.text
    runs = resp.json()["data"]["runs"]
    assert len(runs) == 1
    assert runs[0]["session_id"] == first_session


@pytest.mark.asyncio
async def test_zero_scope_key_is_denied(ctx_no_scope: _Ctx) -> None:
    """A zero-scope key must not read the run list — the
    ``dependencies=[Depends(require("session", "read"))]`` on this route is
    otherwise load-bearing code with no test watching it (review Minor:
    delete that dependency and every other test in this file, all run on an
    ``admin``-scope key, stays green)."""
    await ctx_no_scope.seed_agent()

    resp = await ctx_no_scope.client.get(
        "/v1/agents/support-bot/runs",
        params={"user_id": "cust-77"},
        headers=ctx_no_scope.headers,
    )

    assert resp.status_code == 403, resp.text


@pytest.mark.asyncio
async def test_read_scope_key_is_allowed(ctx_read: _Ctx) -> None:
    """A ``read``-scope key IS let through — proving the gate above sits at
    the documented level (``read`` → VIEWER passes ``session:read``), not
    that it happens to reject everyone."""
    await ctx_read.seed_agent()

    resp = await ctx_read.client.get(
        "/v1/agents/support-bot/runs",
        params={"user_id": "cust-77"},
        headers=ctx_read.headers,
    )

    assert resp.status_code == 200, resp.text
