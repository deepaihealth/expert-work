"""P-1 —— ``POST /v1/agents/{code}/runs/{run_id}:regenerate`` 与 ``:edit``。

夹具照 ``test_external_runs_cancel.py``(内存 store + ``stub_agent_runtime`` + 服务账号
JWT)。``stub_agent_runtime`` 的图跑在 ``InMemorySaver`` 上,而
``AgentRuntime.get_agent`` 按 ``(tenant, name, version, spec sha, oauth_subject)``
缓存 —— 同一个 app 里每次拿到的是**同一个** ``BuiltAgent``、同一个 saver,所以第一轮
落下的检查点在 ``:regenerate`` 那一刻还在。``aupdate_state`` 一样可用,
``locate_turn`` 会因为拿不到 psycopg 池而走 ``_bounds_history`` 回退,所以「先跑一轮
再 :regenerate」是**真** supersede 内核 + 假 LLM 的端到端。

**这套测试覆盖不到生产用的 ``_bounds_sql`` 那条路** —— 那条由
``test_supersede_kernel_integration.py`` 的真 Postgres 集成测覆盖(它带
``assert _checkpoint_pool(...) is not None``),两边缺一不可。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from copy import deepcopy
from typing import Any
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from control_plane.api import external_runs as external_runs_mod
from control_plane.app import create_app
from control_plane.audit import build_default_audit_logger
from control_plane.settings import Settings
from control_plane.supersede import SupersedeError
from expert_work.common.conversation_channel import SUPERSEDED_BY
from expert_work.common.lifecycle import Lifecycle
from expert_work.persistence.audit_log import InMemoryAuditLogStore
from expert_work.protocol import AgentSpec
from expert_work.runtime.runs import (
    InMemoryRunEventStore,
    InMemoryRunStore,
    RunStatus,
)
from expert_work.runtime.runs.schemas import TERMINAL_RUN_STATUSES
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


def _spec() -> AgentSpec:
    return AgentSpec.model_validate(deepcopy(_SPEC))


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
    ) -> None:
        self.client = client
        self.app = app
        self.tenant_id = tenant_id
        self.headers = headers
        self.run_store = run_store

    async def seed_agent(self) -> None:
        await self.app.state.agent_spec_repo.create(
            tenant_id=self.tenant_id, spec=_spec(), spec_sha256="a" * 64, created_by="seed"
        )

    async def checkpoint_messages(self, thread_id: UUID) -> list[Any]:
        """检查点里此刻的 ``messages`` 通道。

        不走 ``GET …/sessions/{id}/messages``:那个读面拿的是
        ``AgentRuntime.durable_checkpointer``,而 ``stub_agent_runtime`` 没有装
        (每次 ``_build`` 自己 new 一个 ``InMemorySaver`` 挂在 ``BuiltAgent`` 上),
        所以在这套内存夹具里它恒定返回空列表 —— 读面本身由 PR1 的
        ``test_read_faces_superseded.py`` 覆盖。这里要证的是标记**真的落进了
        检查点**,直接读那个 saver 才是这件事的第一手证据。
        """
        records = await self.app.state.agent_spec_repo.list_by_tenant(
            tenant_id=self.tenant_id, name="support-bot", limit=1
        )
        built = await self.app.state.agent_runtime.get_agent(
            tenant_id=self.tenant_id,
            name="support-bot",
            version=records[0].version,
            spec=records[0].spec,
            user_id=None,
        )
        snapshot = await built.graph.aget_state(
            {"configurable": {"thread_id": str(thread_id), "tenant_id": str(self.tenant_id)}}
        )
        return list((snapshot.values or {}).get("messages") or [])

    async def wait_terminal(self, run_id: UUID, *, timeout: float = 5.0) -> RunStatus:
        """轮询直到这一轮落终局。

        ``:regenerate`` 的前提是「会话里没有 run 在跑」(THREAD_BUSY),所以每个用例
        都必须先等首轮真的落地,不能只等 SSE 流关掉 —— 状态是在流关掉之后写的。
        """
        import asyncio

        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            row = await self.run_store.get(run_id=run_id, tenant_id=self.tenant_id)
            if row is not None and row.status in TERMINAL_RUN_STATUSES:
                return row.status
            if asyncio.get_running_loop().time() > deadline:
                raise AssertionError(f"run {run_id} did not reach a terminal status: {row}")
            await asyncio.sleep(0.01)


@pytest.fixture
async def ctx() -> AsyncIterator[_Ctx]:
    lifecycle = Lifecycle()
    lifecycle.mark_ready()
    run_store = InMemoryRunStore()
    run_event_store = InMemoryRunEventStore()
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
        subject="sa-test",
        sub_type="service_account",
        roles=(),
        scopes=("admin",),
    )
    headers = {"Authorization": f"Bearer {jwt}"}
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://cp.test") as client:
        yield _Ctx(client, app, tenant_id, headers, run_store)


async def _turn(ctx: _Ctx, *, text: str, session_id: UUID | None = None) -> tuple[UUID, UUID]:
    """stream 模式起一轮并等它落终局;返回 ``(thread_id, run_id)``。

    stream 而不是 queue:queue 模式的 QUEUED 行在这个进程里没有 worker 去认领,会永远
    停在 QUEUED 上,而 QUEUED 属于 ``SUPERSEDE_BUSY_STATUSES`` —— 那样每个用例都只能
    拿到 ``THREAD_BUSY``,一条真路径都测不到。
    """
    body: dict[str, Any] = {"user_id": "cust-77", "input": text, "mode": "stream"}
    if session_id is not None:
        body["session_id"] = str(session_id)
    resp = await ctx.client.post("/v1/agents/support-bot/runs", json=body, headers=ctx.headers)
    assert resp.status_code == 200, resp.text
    async for _line in resp.aiter_lines():
        pass
    run_id = UUID(resp.headers["X-Expert-Work-Run-Id"])
    thread_id = UUID(resp.headers["X-Expert-Work-Session-Id"])
    assert await ctx.wait_terminal(run_id) is RunStatus.SUCCESS
    return thread_id, run_id


@pytest.mark.asyncio
async def test_regenerate_marks_old_turn_and_runs_a_new_one(ctx: _Ctx) -> None:
    await ctx.seed_agent()
    thread_id, r1 = await _turn(ctx, text="hello")

    resp = await ctx.client.post(
        f"/v1/agents/support-bot/runs/{r1}:regenerate",
        json={"user_id": "cust-77", "mode": "queue"},
        headers=ctx.headers,
    )
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["success"] is True and body["error"] is None
    assert body["data"]["thread_id"] == str(thread_id)
    r2 = UUID(body["data"]["run_id"])

    old = await ctx.run_store.get(run_id=r1, tenant_id=ctx.tenant_id)
    new = await ctx.run_store.get(run_id=r2, tenant_id=ctx.tenant_id)
    assert old is not None and new is not None
    assert old.superseded_by_run_id == r2
    assert new.regenerated_from_run_id == r1
    # ``replay=True`` —— 新一轮的输入是旧轮的两条原件,不是调用方给的 input。
    assert set(new.enqueued_input or {}) == {"replay_messages"}

    # 旧轮的每一条都留在历史里、并且都带上了指向新 run 的取代标记。
    messages = await ctx.checkpoint_messages(thread_id)
    assert messages, "首轮的消息不该消失 —— 取代是打标,不是删除"
    assert all(m.additional_kwargs.get(SUPERSEDED_BY) == str(r2) for m in messages), [
        (type(m).__name__, m.additional_kwargs) for m in messages
    ]


@pytest.mark.asyncio
async def test_edit_uses_the_new_input(ctx: _Ctx) -> None:
    await ctx.seed_agent()
    _thread_id, r1 = await _turn(ctx, text="hello")

    resp = await ctx.client.post(
        f"/v1/agents/support-bot/runs/{r1}:edit",
        json={"user_id": "cust-77", "input": "hello again", "mode": "queue"},
        headers=ctx.headers,
    )
    assert resp.status_code == 202, resp.text
    r2 = UUID(resp.json()["data"]["run_id"])
    new = await ctx.run_store.get(run_id=r2, tenant_id=ctx.tenant_id)
    assert new is not None
    assert (new.enqueued_input or {})["input"] == "hello again"
    assert "replay_messages" not in (new.enqueued_input or {})
    assert new.regenerated_from_run_id == r1


@pytest.mark.asyncio
async def test_edit_requires_input(ctx: _Ctx) -> None:
    await ctx.seed_agent()
    _thread_id, r1 = await _turn(ctx, text="hello")
    resp = await ctx.client.post(
        f"/v1/agents/support-bot/runs/{r1}:edit",
        json={"user_id": "cust-77"},
        headers=ctx.headers,
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == "INVALID_REQUEST"


@pytest.mark.asyncio
async def test_regenerate_rejects_an_input_field(ctx: _Ctx) -> None:
    """``:regenerate`` 是 ``extra="forbid"``:要改输入就得用 ``:edit``,不能悄悄
    在 ``:regenerate`` 上塞一个 ``input`` 然后以为它生效了。"""
    await ctx.seed_agent()
    _thread_id, r1 = await _turn(ctx, text="hello")
    resp = await ctx.client.post(
        f"/v1/agents/support-bot/runs/{r1}:regenerate",
        json={"user_id": "cust-77", "input": "sneaky"},
        headers=ctx.headers,
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == "INVALID_REQUEST"


@pytest.mark.asyncio
async def test_other_user_and_other_agent_and_unknown_run_are_404_envelopes(ctx: _Ctx) -> None:
    await ctx.seed_agent()
    _thread_id, r1 = await _turn(ctx, text="hello")
    for path, body in (
        (f"/v1/agents/support-bot/runs/{r1}:regenerate", {"user_id": "someone-else"}),
        (f"/v1/agents/other-bot/runs/{r1}:regenerate", {"user_id": "cust-77"}),
        (f"/v1/agents/support-bot/runs/{uuid4()}:edit", {"user_id": "cust-77", "input": "x"}),
    ):
        resp = await ctx.client.post(path, json=body, headers=ctx.headers)
        assert resp.status_code == 404, (path, resp.text)
        assert resp.json() == {
            "success": False,
            "data": None,
            "error": {"code": "RUN_NOT_FOUND", "message": "run not found"},
        }


@pytest.mark.asyncio
async def test_not_last_run_is_422(ctx: _Ctx) -> None:
    await ctx.seed_agent()
    thread_id, r1 = await _turn(ctx, text="first")
    await _turn(ctx, text="second", session_id=thread_id)

    resp = await ctx.client.post(
        f"/v1/agents/support-bot/runs/{r1}:regenerate",
        json={"user_id": "cust-77"},
        headers=ctx.headers,
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == "RUN_NOT_LAST"


@pytest.mark.asyncio
async def test_already_superseded_run_is_409(ctx: _Ctx) -> None:
    """内核真路径(不是 monkeypatch):取代过一次的轮不能再取代一次。"""
    await ctx.seed_agent()
    _thread_id, r1 = await _turn(ctx, text="hello")
    first = await ctx.client.post(
        f"/v1/agents/support-bot/runs/{r1}:regenerate",
        json={"user_id": "cust-77", "mode": "queue"},
        headers=ctx.headers,
    )
    assert first.status_code == 202, first.text

    second = await ctx.client.post(
        f"/v1/agents/support-bot/runs/{r1}:regenerate",
        json={"user_id": "cust-77", "mode": "queue"},
        headers=ctx.headers,
    )
    assert second.status_code == 409, second.text
    assert second.json()["error"]["code"] == "RUN_ALREADY_SUPERSEDED"


@pytest.mark.asyncio
async def test_thread_busy_is_409(ctx: _Ctx) -> None:
    """内核真路径:会话里有一轮排队中(queue 模式建了 QUEUED 行、本进程没有
    worker 去认领)→ 409,不许再取代。"""
    await ctx.seed_agent()
    thread_id, r1 = await _turn(ctx, text="hello")
    queued = await ctx.client.post(
        "/v1/agents/support-bot/runs",
        json={
            "user_id": "cust-77",
            "session_id": str(thread_id),
            "input": "queued",
            "mode": "queue",
        },
        headers=ctx.headers,
    )
    assert queued.status_code == 202, queued.text

    resp = await ctx.client.post(
        f"/v1/agents/support-bot/runs/{r1}:regenerate",
        json={"user_id": "cust-77"},
        headers=ctx.headers,
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "THREAD_BUSY"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("code", "status"),
    [
        ("THREAD_BUSY", 409),
        ("RUN_AWAITING_APPROVAL", 409),
        ("RUN_ALREADY_SUPERSEDED", 409),
        ("RUN_INPUT_UNAVAILABLE", 422),
        ("RUN_NOT_LAST", 422),
    ],
)
async def test_kernel_errors_map_to_envelopes(
    ctx: _Ctx, monkeypatch: pytest.MonkeyPatch, code: str, status: int
) -> None:
    """五个错误码的信封渲染 —— 端点必须把 ``SupersedeError`` 的 code / message /
    status_code 原样搬进对外信封,不能自己改写成别的码或别的 HTTP 状态。

    内核抛这五个码的真条件里有两个(PAUSED 目标轮、检查点缺失)在内存栈里搭不
    出来,所以这条用替身盖住渲染这一层;``THREAD_BUSY`` /
    ``RUN_ALREADY_SUPERSEDED`` / ``RUN_NOT_LAST`` 另有走真内核的用例。
    """
    await ctx.seed_agent()
    _thread_id, r1 = await _turn(ctx, text="hello")

    async def boom(**kwargs: Any) -> Any:
        del kwargs
        raise SupersedeError(code, "why", status)

    monkeypatch.setattr(external_runs_mod, "spawn_run", boom)
    resp = await ctx.client.post(
        f"/v1/agents/support-bot/runs/{r1}:regenerate",
        json={"user_id": "cust-77"},
        headers=ctx.headers,
    )
    assert resp.status_code == status, resp.text
    assert resp.json() == {
        "success": False,
        "data": None,
        "error": {"code": code, "message": "why"},
    }


@pytest.mark.asyncio
async def test_idempotency_key_replays_instead_of_creating_a_second_run(ctx: _Ctx) -> None:
    await ctx.seed_agent()
    thread_id, r1 = await _turn(ctx, text="hello")
    headers = {**ctx.headers, "Idempotency-Key": "regen-1"}
    body = {"user_id": "cust-77", "mode": "queue"}

    first = await ctx.client.post(
        f"/v1/agents/support-bot/runs/{r1}:regenerate", json=body, headers=headers
    )
    second = await ctx.client.post(
        f"/v1/agents/support-bot/runs/{r1}:regenerate", json=body, headers=headers
    )
    assert first.status_code == 202, first.text
    assert second.status_code == 202, second.text
    assert first.json()["data"]["run_id"] == second.json()["data"]["run_id"]

    rows = await ctx.run_store.list_by_thread(thread_id=thread_id, tenant_id=ctx.tenant_id)
    # 首轮 + 一条重发。第二次调用必须命中幂等而不是再取代一次 —— 若它真跑了,
    # 这里会是 3 行(而且第一条重发也会被标成已被取代)。
    assert len(rows) == 2, [(r.run_id, r.status) for r in rows]

    # 同一个 key 换操作(:regenerate → :edit)= 另一个请求 → 422,不是幂等命中。
    reused = await ctx.client.post(
        f"/v1/agents/support-bot/runs/{r1}:edit",
        json={**body, "input": "x"},
        headers=headers,
    )
    assert reused.status_code == 422, reused.text
    assert reused.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"


@pytest.mark.asyncio
async def test_idempotency_key_is_scoped_to_the_target_run(ctx: _Ctx) -> None:
    """同一个 key 对**另一个** run 调同一个操作也是 REUSED —— 指纹里折进了
    ``run_id``,否则一个 key 会把两轮的重发混成一次。"""
    await ctx.seed_agent()
    thread_id, r1 = await _turn(ctx, text="first")
    _thread_id, r2 = await _turn(ctx, text="second", session_id=thread_id)
    headers = {**ctx.headers, "Idempotency-Key": "regen-2"}
    body = {"user_id": "cust-77", "mode": "queue"}

    ok = await ctx.client.post(
        f"/v1/agents/support-bot/runs/{r2}:regenerate", json=body, headers=headers
    )
    assert ok.status_code == 202, ok.text
    reused = await ctx.client.post(
        f"/v1/agents/support-bot/runs/{r1}:regenerate", json=body, headers=headers
    )
    assert reused.status_code == 422, reused.text
    assert reused.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"


@pytest.mark.asyncio
async def test_both_runs_keep_their_billing_identity(ctx: _Ctx) -> None:
    """spec §7 PR3:两轮都计费,明确不回滚 —— 旧轮的行不因被取代而被改写。

    ``token_usage`` 表在内存栈不存在,能查的是「旧 run 行除了取代链接之外一字未
    动」:``trace_id`` 是 token 记账唯一的连表键(记账表没有 run_id 列),状态与
    时间戳都还是它自己跑完那一刻的值。
    """
    await ctx.seed_agent()
    _thread_id, r1 = await _turn(ctx, text="hello")
    old = await ctx.run_store.get(run_id=r1, tenant_id=ctx.tenant_id)
    assert old is not None

    resp = await ctx.client.post(
        f"/v1/agents/support-bot/runs/{r1}:regenerate",
        json={"user_id": "cust-77", "mode": "queue"},
        headers=ctx.headers,
    )
    assert resp.status_code == 202, resp.text

    still = await ctx.run_store.get(run_id=r1, tenant_id=ctx.tenant_id)
    assert still is not None
    assert still.trace_id == old.trace_id
    assert still.status == old.status
    assert still.finished_at == old.finished_at
    assert still.superseded_by_run_id is not None  # 只多了这一个链接
