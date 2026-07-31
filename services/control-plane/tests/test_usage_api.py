"""API tests for /v1/usage — Stream Z (Z-1).

Tenant-facing usage/cost reads (``billing:read``, RLS self-isolated):

* ``GET /v1/usage/cost``   — billed cost from the ledger, grouped.
* ``GET /v1/usage/tokens`` — current-month realtime token sums.

The load-bearing assertion is the **no-leak constraint**: the tenant cost
response must NEVER carry ``base_cost``/``markup``/``margin`` — only billed.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, date, datetime
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from control_plane.app import create_app
from control_plane.audit import build_default_audit_logger
from control_plane.settings import Settings
from expert_work.common.lifecycle import Lifecycle
from expert_work.persistence.audit_log import InMemoryAuditLogStore
from expert_work.persistence.billing.ledger import InMemoryTenantBillingLedgerStore
from expert_work.persistence.token_usage_store import InMemoryTokenUsageStore, TokenUsageRecord
from expert_work.protocol import Role, TenantBillingLedgerRecord
from tests.auth_fixtures import (
    TEST_AUDIENCE,
    TEST_ISSUER,
    build_test_jwt_verifier,
    make_test_jwt,
)

_THIS_MONTH = datetime.now(tz=UTC).date().replace(day=1)
_FORBIDDEN_KEYS = {"base_cost_micros", "markup_cost_micros", "margin_micros", "margin"}


def _settings() -> Settings:
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


def _ledger_row(
    *,
    tenant_id: UUID,
    agent: str,
    model: str,
    billed: int,
    priced: bool = True,
    month: date = _THIS_MONTH,
) -> TenantBillingLedgerRecord:
    now = datetime.now(tz=UTC)
    return TenantBillingLedgerRecord(
        id=uuid4(),
        tenant_id=tenant_id,
        month=month,
        provider="anthropic",
        model=model,
        agent_name=agent,
        input_tokens=100,
        output_tokens=50,
        cache_creation_tokens=0,
        cache_read_tokens=0,
        base_cost_micros=billed // 2 if priced else 0,
        markup_cost_micros=billed - (billed // 2) if priced else 0,
        billed_cost_micros=billed if priced else 0,
        priced=priced,
        rate_card_priced_at=now,
        created_at=now,
        updated_at=now,
    )


class _Ctx:
    def __init__(
        self,
        client: AsyncClient,
        tenant_id: UUID,
        headers: dict[str, str],
        ledger: InMemoryTenantBillingLedgerStore,
        usage: InMemoryTokenUsageStore,
    ) -> None:
        self.client = client
        self.tenant_id = tenant_id
        self.headers = headers
        self.ledger = ledger
        self.usage = usage


@pytest.fixture
async def ctx() -> AsyncIterator[_Ctx]:
    lifecycle = Lifecycle()
    lifecycle.mark_ready()
    ledger = InMemoryTenantBillingLedgerStore()
    usage = InMemoryTokenUsageStore()
    app = create_app(
        settings=_settings(),
        lifecycle=lifecycle,
        jwt_verifier=build_test_jwt_verifier(),
        audit_logger=build_default_audit_logger(InMemoryAuditLogStore()),
        token_usage_repo=usage,
    )
    # The ledger store isn't a create_app kwarg — swap the in-memory default the
    # app wired so the test seeds the same instance the router reads.
    app.state.tenant_billing_ledger_store = ledger
    tenant_id = uuid4()
    jwt = make_test_jwt(tenant_id=tenant_id, subject=str(uuid4()), roles=("admin",))
    headers = {"Authorization": f"Bearer {jwt}"}
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://cp.test") as client:
        yield _Ctx(client, tenant_id, headers, ledger, usage)


# ---------------------------------------------------------------------------
# cost — billed only, grouping, no-leak
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cost_groups_by_agent_and_never_leaks_base_markup(ctx: _Ctx) -> None:
    await ctx.ledger.upsert(
        _ledger_row(tenant_id=ctx.tenant_id, agent="a1", model="m1", billed=300)
    )
    await ctx.ledger.upsert(
        _ledger_row(tenant_id=ctx.tenant_id, agent="a2", model="m1", billed=700)
    )

    resp = await ctx.client.get("/v1/usage/cost", headers=ctx.headers)
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["group_by"] == "agent"
    assert data["total_billed_cost_micros"] == 1000
    groups = {g["key"]: g for g in data["groups"]}
    assert set(groups) == {"a1", "a2"}
    assert groups["a2"]["billed_cost_micros"] == 700
    # No-leak: NO group (nor the envelope) may carry base/markup/margin.
    for g in data["groups"]:
        assert _FORBIDDEN_KEYS.isdisjoint(g.keys()), g
    assert _FORBIDDEN_KEYS.isdisjoint(data.keys())


@pytest.mark.asyncio
async def test_cost_group_by_model_and_none(ctx: _Ctx) -> None:
    await ctx.ledger.upsert(
        _ledger_row(tenant_id=ctx.tenant_id, agent="a1", model="m1", billed=100)
    )
    await ctx.ledger.upsert(
        _ledger_row(tenant_id=ctx.tenant_id, agent="a2", model="m1", billed=200)
    )

    by_model = (await ctx.client.get("/v1/usage/cost?group_by=model", headers=ctx.headers)).json()
    assert {g["key"] for g in by_model["data"]["groups"]} == {"m1"}
    assert by_model["data"]["groups"][0]["billed_cost_micros"] == 300

    none = (await ctx.client.get("/v1/usage/cost?group_by=none", headers=ctx.headers)).json()
    # group_by=none keeps the raw bucket identity (provider/model/agent_name).
    keys = {(g["provider"], g["model"], g["agent_name"]) for g in none["data"]["groups"]}
    assert keys == {("anthropic", "m1", "a1"), ("anthropic", "m1", "a2")}
    for g in none["data"]["groups"]:
        assert _FORBIDDEN_KEYS.isdisjoint(g.keys())


@pytest.mark.asyncio
async def test_cost_unpriced_bucket_flagged(ctx: _Ctx) -> None:
    await ctx.ledger.upsert(
        _ledger_row(tenant_id=ctx.tenant_id, agent="a1", model="m1", billed=0, priced=False)
    )
    data = (await ctx.client.get("/v1/usage/cost", headers=ctx.headers)).json()["data"]
    assert data["groups"][0]["unpriced"] is True
    assert data["groups"][0]["billed_cost_micros"] == 0
    assert data["groups"][0]["input_tokens"] == 100  # tokens still visible
    assert _FORBIDDEN_KEYS.isdisjoint(data["groups"][0].keys())  # no-leak holds here too


@pytest.mark.asyncio
async def test_cost_respects_month_param(ctx: _Ctx) -> None:
    may = date(2026, 5, 1)
    await ctx.ledger.upsert(
        _ledger_row(tenant_id=ctx.tenant_id, agent="a1", model="m1", billed=42, month=may)
    )
    hit = (await ctx.client.get("/v1/usage/cost?month=2026-05", headers=ctx.headers)).json()["data"]
    assert hit["month"] == "2026-05"
    assert hit["total_billed_cost_micros"] == 42
    # A different month sees none of it.
    miss = (await ctx.client.get("/v1/usage/cost?month=2026-06", headers=ctx.headers)).json()[
        "data"
    ]
    assert miss["total_billed_cost_micros"] == 0
    assert miss["groups"] == []
    assert miss["as_of"] is None


@pytest.mark.asyncio
async def test_cost_self_isolated_per_tenant(ctx: _Ctx) -> None:
    other = uuid4()
    await ctx.ledger.upsert(
        _ledger_row(tenant_id=ctx.tenant_id, agent="mine", model="m1", billed=5)
    )
    await ctx.ledger.upsert(_ledger_row(tenant_id=other, agent="theirs", model="m1", billed=999))
    data = (await ctx.client.get("/v1/usage/cost", headers=ctx.headers)).json()["data"]
    assert {g["key"] for g in data["groups"]} == {"mine"}
    assert data["total_billed_cost_micros"] == 5


@pytest.mark.asyncio
async def test_cost_invalid_month_and_group_by_422(ctx: _Ctx) -> None:
    bad_month = await ctx.client.get("/v1/usage/cost?month=2026-13", headers=ctx.headers)
    assert bad_month.status_code == 422
    bad_group = await ctx.client.get("/v1/usage/cost?group_by=tenant", headers=ctx.headers)
    assert bad_group.status_code == 422


# ---------------------------------------------------------------------------
# tokens — realtime, no cost
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tokens_realtime_totals_and_splits(ctx: _Ctx) -> None:
    for agent, model, inp in (("a1", "m1", 10), ("a1", "m2", 20), ("a2", "m1", 30)):
        await ctx.usage.insert(
            TokenUsageRecord(
                tenant_id=ctx.tenant_id,
                agent_name=agent,
                agent_version="1",
                model=model,
                provider="anthropic",
                input_tokens=inp,
                output_tokens=1,
            )
        )
    data = (await ctx.client.get("/v1/usage/tokens", headers=ctx.headers)).json()["data"]
    assert data["realtime"] is True
    assert data["total"]["input_tokens"] == 60
    by_agent = {g["key"]: g for g in data["by_agent"]}
    assert by_agent["a1"]["input_tokens"] == 30
    assert by_agent["a2"]["input_tokens"] == 30
    by_model = {g["key"]: g for g in data["by_model"]}
    assert by_model["m1"]["input_tokens"] == 40
    # tokens endpoint carries NO cost fields at all.
    assert "billed_cost_micros" not in data
    assert _FORBIDDEN_KEYS.isdisjoint(data.keys())


@pytest.mark.asyncio
async def test_usage_requires_auth(ctx: _Ctx) -> None:
    unauth = await ctx.client.get("/v1/usage/cost")
    assert unauth.status_code in (401, 403)
    unauth_tokens = await ctx.client.get("/v1/usage/tokens")
    assert unauth_tokens.status_code in (401, 403)


@pytest.mark.asyncio
async def test_tokens_by_kind_split_and_filter(ctx: _Ctx) -> None:
    """SE-16 (SE-A43) — evolution spend is separable from conversation."""
    for kind, inp in (("conversation", 10), ("skill_evolution", 40)):
        await ctx.usage.insert(
            TokenUsageRecord(
                tenant_id=ctx.tenant_id,
                agent_name="a1",
                agent_version="1",
                model="m1",
                usage_kind=kind,
                input_tokens=inp,
                output_tokens=1,
            )
        )
    data = (await ctx.client.get("/v1/usage/tokens", headers=ctx.headers)).json()["data"]
    by_kind = {g["key"]: g for g in data["by_kind"]}
    assert by_kind["conversation"]["input_tokens"] == 10
    assert by_kind["skill_evolution"]["input_tokens"] == 40

    filtered = (
        await ctx.client.get("/v1/usage/tokens?kind=skill_evolution", headers=ctx.headers)
    ).json()["data"]
    assert filtered["total"]["input_tokens"] == 40
    assert {g["key"] for g in filtered["by_kind"]} == {"skill_evolution"}


# ---------------------------------------------------------------------------
# W3 — usage 读端点接跨租户 scope(系统管理员租户切换器)
#
# 三件套(列表性读,无 "*" 详情拒绝项):system_admin 带目标租户 tenant_id
# → 200 命中目标租户数据;普通租户用户带他租户 tenant_id → 403
# TENANT_NOT_ALLOWED;"*" 无聚合读法 → 回落归属租户。照 W2 先例。
# ---------------------------------------------------------------------------


async def _grant_system_admin(client: AsyncClient) -> dict[str, str]:
    """Seed a platform-scope binding; return headers for a system_admin whose
    HOME tenant differs from the tenant under test."""
    sys_admin_id = uuid4()
    app = client._transport.app  # type: ignore[attr-defined,union-attr]
    await app.state.role_binding_repo.create(
        subject_type="user",
        subject_id=sys_admin_id,
        tenant_id=None,
        role=Role.SYSTEM_ADMIN,
        platform_scope=True,
        granted_by="seed",
    )
    token = make_test_jwt(tenant_id=uuid4(), subject=str(sys_admin_id))
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_usage_system_admin_target_tenant_200(ctx: _Ctx) -> None:
    await ctx.ledger.upsert(
        _ledger_row(tenant_id=ctx.tenant_id, agent="a1", model="m1", billed=300)
    )
    await ctx.usage.insert(
        TokenUsageRecord(
            tenant_id=ctx.tenant_id,
            agent_name="a1",
            agent_version="1",
            model="m1",
            provider="anthropic",
            input_tokens=10,
            output_tokens=1,
        )
    )
    headers = await _grant_system_admin(ctx.client)
    params = {"tenant_id": str(ctx.tenant_id)}

    cost = await ctx.client.get("/v1/usage/cost", params=params, headers=headers)
    assert cost.status_code == 200, cost.text
    assert cost.json()["data"]["total_billed_cost_micros"] == 300

    tokens = await ctx.client.get("/v1/usage/tokens", params=params, headers=headers)
    assert tokens.status_code == 200, tokens.text
    assert tokens.json()["data"]["total"]["input_tokens"] == 10


@pytest.mark.asyncio
async def test_usage_star_falls_back_to_home_tenant(ctx: _Ctx) -> None:
    """``tenant_id=*``:usage 无聚合读法(spec 非目标)→ 回落 system_admin
    归属租户(照前端 concreteTenantScope 口径),不 500/400。"""
    await ctx.ledger.upsert(
        _ledger_row(tenant_id=ctx.tenant_id, agent="a1", model="m1", billed=300)
    )
    headers = await _grant_system_admin(ctx.client)
    resp = await ctx.client.get("/v1/usage/cost", params={"tenant_id": "*"}, headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["total_billed_cost_micros"] == 0


@pytest.mark.asyncio
async def test_usage_foreign_tenant_user_403(ctx: _Ctx) -> None:
    foreign_jwt = make_test_jwt(tenant_id=uuid4(), subject=str(uuid4()), roles=("admin",))
    foreign = {"Authorization": f"Bearer {foreign_jwt}"}
    for name, path in [("cost", "/v1/usage/cost"), ("tokens", "/v1/usage/tokens")]:
        resp = await ctx.client.get(
            path, params={"tenant_id": str(ctx.tenant_id)}, headers=foreign
        )
        assert resp.status_code == 403, f"{name}: {resp.status_code} {resp.text}"
        assert resp.json()["detail"]["code"] == "TENANT_NOT_ALLOWED", name
