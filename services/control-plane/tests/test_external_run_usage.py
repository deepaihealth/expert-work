"""B-52 —— ``GET /v1/agents/{agent_code}/runs/{run_id}/usage``。

对账兜底端点:日常扣账走 ``end`` 帧,这里给没接住 end 帧、要补数、或月底对账的
场景。返回体的 ``usage_by_model`` 与 ``end`` 帧**逐字段同形** —— 两条路分叉过一次
(``end_frame_data`` 的 docstring 记着),所以这里有一条看门狗直接比对两者。
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
from expert_work.persistence.token_usage_store import (
    InMemoryTokenUsageStore,
    TokenUsageRecord,
)
from expert_work.protocol import AgentSpec
from expert_work.runtime.runs import InMemoryRunEventStore, InMemoryRunStore, RunStatus
from tests.agent_fixtures import stub_agent_runtime
from tests.auth_fixtures import (
    TEST_AUDIENCE,
    TEST_ISSUER,
    build_test_jwt_verifier,
    make_test_jwt,
)

# 与 ``test_external_runs_list.py`` 同一份 —— 自己编一份会被 AgentSpec 的必填
# 字段(``metadata.tenant`` / ``spec.tenant_config`` / ``system_prompt``)打回。
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

_TRACE = "b" * 32


def _spec() -> AgentSpec:
    return AgentSpec.model_validate(deepcopy(_SPEC))


class _Ctx:
    def __init__(
        self,
        client: AsyncClient,
        app: Any,
        tenant_id: UUID,
        headers: dict[str, str],
        run_store: InMemoryRunStore,
        usage_store: InMemoryTokenUsageStore,
    ) -> None:
        self.client = client
        self.app = app
        self.tenant_id = tenant_id
        self.headers = headers
        self.run_store = run_store
        self.usage_store = usage_store

    async def seed_agent(self) -> None:
        await self.app.state.agent_spec_repo.create(
            tenant_id=self.tenant_id, spec=_spec(), spec_sha256="a" * 64, created_by="seed"
        )

    async def start_run(self, *, user_id: str) -> UUID:
        resp = await self.client.post(
            "/v1/agents/support-bot/runs",
            json={"user_id": user_id, "input": "hi", "mode": "queue"},
            headers=self.headers,
        )
        assert resp.status_code == 202, resp.text
        return UUID(resp.json()["data"]["run_id"])

    async def bind_trace(self, run_id: UUID, trace_id: str = _TRACE) -> None:
        await self.run_store.set_trace_id(
            run_id=run_id, tenant_id=self.tenant_id, trace_id=trace_id
        )

    async def add_usage(
        self, *, model: str, provider: str, inp: int, out: int, kind: str = "conversation"
    ) -> None:
        await self.usage_store.insert(
            TokenUsageRecord(
                tenant_id=self.tenant_id,
                agent_name="support-bot",
                agent_version="1.0.0",
                model=model,
                provider=provider,
                trace_id=_TRACE,
                input_tokens=inp,
                output_tokens=out,
                cache_read_tokens=inp // 2,
                usage_kind=kind,
            )
        )


@pytest.fixture
async def ctx() -> AsyncIterator[_Ctx]:
    lifecycle = Lifecycle()
    lifecycle.mark_ready()
    threads = InMemoryThreadMetaStore()
    run_store = InMemoryRunStore(thread_meta_store=threads)
    run_event_store = InMemoryRunEventStore()
    usage_store = InMemoryTokenUsageStore()
    app = create_app(
        settings=Settings(
            service_name="control_plane_test",
            env="dev",
            auth_mode="dev",
            db_dsn="postgresql+asyncpg://test@localhost/test",
            rate_limit_burst=10_000,
            rate_limit_per_second=10_000.0,
            oidc_issuer=TEST_ISSUER,
            oidc_audience=[TEST_AUDIENCE],
        ),
        lifecycle=lifecycle,
        jwt_verifier=build_test_jwt_verifier(),
        audit_logger=build_default_audit_logger(InMemoryAuditLogStore()),
        agent_runtime=stub_agent_runtime(run_store=run_store, run_event_store=run_event_store),
        run_repo=run_store,
        run_event_repo=run_event_store,
        thread_meta_repo=threads,
        token_usage_repo=usage_store,
    )
    tenant_id = uuid4()
    headers = {
        "Authorization": "Bearer "
        + make_test_jwt(
            tenant_id=tenant_id,
            subject="sa-test",
            sub_type="service_account",
            roles=(),
            scopes=("admin",),
        )
    }
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://cp.test") as client:
        yield _Ctx(client, app, tenant_id, headers, run_store, usage_store)


@pytest.mark.asyncio
async def test_missing_user_id_is_422(ctx: _Ctx) -> None:
    """``user_id`` 必填无默认 —— 漏传是 422,绝不降级成「随便谁的 run 都能查」。
    与 ``GET .../runs`` 同一条铁律。"""
    await ctx.seed_agent()
    run_id = await ctx.start_run(user_id="cust-77")
    resp = await ctx.client.get(f"/v1/agents/support-bot/runs/{run_id}/usage", headers=ctx.headers)
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_another_users_run_is_404_not_empty(ctx: _Ctx) -> None:
    """别人的 run 返 404 —— 返回空结果会泄露「这个 run_id 确实存在」。"""
    await ctx.seed_agent()
    run_id = await ctx.start_run(user_id="cust-77")
    resp = await ctx.client.get(
        f"/v1/agents/support-bot/runs/{run_id}/usage",
        params={"user_id": "cust-99"},
        headers=ctx.headers,
    )
    assert resp.status_code == 404, resp.text


@pytest.mark.asyncio
async def test_unknown_run_is_404(ctx: _Ctx) -> None:
    await ctx.seed_agent()
    resp = await ctx.client.get(
        f"/v1/agents/support-bot/runs/{uuid4()}/usage",
        params={"user_id": "cust-77"},
        headers=ctx.headers,
    )
    assert resp.status_code == 404, resp.text


@pytest.mark.asyncio
async def test_returns_buckets_with_four_token_fields_and_no_llm_calls(ctx: _Ctx) -> None:
    """四档 token 全给(计价口径是调用方的事);``llm_calls`` 不对外。"""
    await ctx.seed_agent()
    run_id = await ctx.start_run(user_id="cust-77")
    await ctx.bind_trace(run_id)
    await ctx.add_usage(model="glm-5.3", provider="glm", inp=100, out=20)

    resp = await ctx.client.get(
        f"/v1/agents/support-bot/runs/{run_id}/usage",
        params={"user_id": "cust-77"},
        headers=ctx.headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["run_id"] == str(run_id)
    # 与 ``GET .../runs`` 的 ``status`` 同一套词表(``RunStatus`` 原值),不是
    # ``end`` 帧那套四值终态 —— 本端点能在 run 未结束时被调用。
    assert body["run_status"] == "queued"
    bucket = body["usage_by_model"][0]
    assert bucket == {
        "provider": "glm",
        "model": "glm-5.3",
        "input_tokens": 100,
        "output_tokens": 20,
        "cache_read_tokens": 50,
        "cache_creation_tokens": 0,
    }
    assert "llm_calls" not in bucket


@pytest.mark.asyncio
async def test_buckets_split_per_provider_and_model(ctx: _Ctx) -> None:
    """一个 run 跨多个模型是常态(主线 glm、worker 可能是 kimi)—— 必须分桶,
    否则调用方没法各按各价算。"""
    await ctx.seed_agent()
    run_id = await ctx.start_run(user_id="cust-77")
    await ctx.bind_trace(run_id)
    await ctx.add_usage(model="glm-5.3", provider="glm", inp=100, out=20)
    await ctx.add_usage(model="kimi-k3", provider="kimi", inp=40, out=8)

    body = (
        await ctx.client.get(
            f"/v1/agents/support-bot/runs/{run_id}/usage",
            params={"user_id": "cust-77"},
            headers=ctx.headers,
        )
    ).json()
    assert {(b["provider"], b["model"]) for b in body["usage_by_model"]} == {
        ("glm", "glm-5.3"),
        ("kimi", "kimi-k3"),
    }


@pytest.mark.asyncio
async def test_platform_own_spend_is_not_billed_to_the_caller(ctx: _Ctx) -> None:
    """``quality_sampling`` / ``skill_evolution`` 是平台自身开销 —— 端点只取
    ``conversation``,否则就是替我们的内部流程向调用方收钱。"""
    await ctx.seed_agent()
    run_id = await ctx.start_run(user_id="cust-77")
    await ctx.bind_trace(run_id)
    await ctx.add_usage(model="glm-5.3", provider="glm", inp=100, out=20)
    await ctx.add_usage(model="glm-5.2", provider="glm", inp=7, out=1, kind="quality_sampling")
    await ctx.add_usage(model="glm-5.1", provider="glm", inp=3, out=1, kind="skill_evolution")

    body = (
        await ctx.client.get(
            f"/v1/agents/support-bot/runs/{run_id}/usage",
            params={"user_id": "cust-77"},
            headers=ctx.headers,
        )
    ).json()
    assert [b["model"] for b in body["usage_by_model"]] == ["glm-5.3"]


@pytest.mark.asyncio
async def test_run_status_uses_the_same_vocabulary_as_the_runs_list(ctx: _Ctx) -> None:
    """``run_status`` 必须与 ``GET .../runs`` 的 ``status`` 同一套词表。

    对外平面并存两套:``end`` 帧只认四个终态(``EXTERNAL_END_STATUSES``,
    ``timeout`` **被折成** ``error``),run 列表给 ``RunStatus`` 原值。本端点若
    错用帧那套,同一个超时 run 在列表里是 ``timeout``、在这里就成了 ``error``
    —— 调用方对账时会把两条记录当成两回事。``TIMEOUT`` 是唯一能分开这两套的
    状态,所以钉它。
    """
    await ctx.seed_agent()
    run_id = await ctx.start_run(user_id="cust-77")
    await ctx.run_store.set_status(
        run_id=run_id,
        tenant_id=ctx.tenant_id,
        status=RunStatus.TIMEOUT,
        updated_at=datetime.now(UTC),
        finished_at=datetime.now(UTC),
    )

    listed = (
        await ctx.client.get(
            "/v1/agents/support-bot/runs",
            params={"user_id": "cust-77"},
            headers=ctx.headers,
        )
    ).json()["data"]["runs"][0]["status"]
    usage = (
        await ctx.client.get(
            f"/v1/agents/support-bot/runs/{run_id}/usage",
            params={"user_id": "cust-77"},
            headers=ctx.headers,
        )
    ).json()["run_status"]

    assert usage == listed == "timeout"


@pytest.mark.asyncio
async def test_run_without_usage_omits_the_field_rather_than_returning_zero(
    ctx: _Ctx,
) -> None:
    """无记录 → 字段**缺席**,不是空数组、不是零。

    缺席 = 无记录(这个 run 还没绑 trace,或历史 run 没有用量行),空数组 = 确有
    其事的零用量。调用方据此区分「查不到」和「确实没花钱」。
    """
    await ctx.seed_agent()
    run_id = await ctx.start_run(user_id="cust-77")

    body = (
        await ctx.client.get(
            f"/v1/agents/support-bot/runs/{run_id}/usage",
            params={"user_id": "cust-77"},
            headers=ctx.headers,
        )
    ).json()
    assert "usage_by_model" not in body
    assert body["run_status"]  # 终局状态照给 —— 查不到用量不影响这一条
