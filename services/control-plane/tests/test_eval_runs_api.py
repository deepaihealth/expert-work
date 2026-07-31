"""End-to-end tests for the P1-S2.1d ``/v1/eval-runs`` API.

Enqueue + read over the eval-run store, authenticated + tenant-scoped.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from control_plane.app import create_app
from control_plane.audit import build_default_audit_logger
from control_plane.settings import DEFAULT_DEV_TENANT_ID, Settings
from expert_work.persistence import InMemoryEvalRunStore
from expert_work.persistence.audit_log import InMemoryAuditLogStore
from expert_work.protocol import (
    EvalCaseResultRecord,
    EvalRunRecord,
    EvalRunStatus,
    EvalTriggeredBy,
    Role,
)
from tests.agent_fixtures import stub_agent_runtime
from tests.auth_fixtures import (
    TEST_AUDIENCE,
    TEST_ISSUER,
    build_test_jwt_verifier,
    make_test_jwt,
)

_TENANT = DEFAULT_DEV_TENANT_ID


class _Ctx:
    def __init__(self, client: AsyncClient, store: InMemoryEvalRunStore) -> None:
        self.client = client
        self.store = store


@pytest.fixture
async def ctx() -> AsyncIterator[_Ctx]:
    settings = Settings(
        env="dev",
        auth_mode="dev",
        rate_limit_burst=10_000,
        rate_limit_per_second=10_000.0,
        oidc_issuer=TEST_ISSUER,
        oidc_audience=[TEST_AUDIENCE],
    )
    store = InMemoryEvalRunStore()
    app = create_app(
        settings=settings,
        audit_logger=build_default_audit_logger(InMemoryAuditLogStore()),
        jwt_verifier=build_test_jwt_verifier(),
        agent_runtime=stub_agent_runtime(),
        enable_scheduler=False,
        eval_run_repo=store,
    )
    transport = ASGITransport(app=app)
    headers = {"Authorization": f"Bearer {make_test_jwt(tenant_id=_TENANT)}"}
    async with AsyncClient(
        transport=transport, base_url="http://control-plane.test", headers=headers
    ) as client:
        yield _Ctx(client, store)


@pytest.mark.asyncio
async def test_enqueue_creates_queued_run(ctx: _Ctx) -> None:
    resp = await ctx.client.post("/v1/eval-runs", json={"suite": "m0_baseline"})
    assert resp.status_code == 202
    body = resp.json()
    assert body["suite"] == "m0_baseline"
    assert body["status"] == "queued"
    assert body["triggered_by"] == "manual"
    # The row landed in the store under the caller's tenant.
    stored = await ctx.store.get_run(run_id=UUID(body["id"]), tenant_id=_TENANT)
    assert stored is not None and stored.status is EvalRunStatus.QUEUED


@pytest.mark.asyncio
async def test_enqueue_unknown_suite_is_422(ctx: _Ctx) -> None:
    resp = await ctx.client.post("/v1/eval-runs", json={"suite": "bogus"})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_get_run_reflects_status_and_summary(ctx: _Ctx) -> None:
    run_id = (await ctx.client.post("/v1/eval-runs", json={"suite": "m0_baseline"})).json()["id"]
    rid = UUID(run_id)
    await ctx.store.set_status(
        run_id=rid,
        tenant_id=_TENANT,
        status=EvalRunStatus.PASSED,
        summary={"pass_count": 15, "total": 15},
    )

    resp = await ctx.client.get(f"/v1/eval-runs/{run_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "passed"
    assert body["summary"] == {"pass_count": 15, "total": 15}
    assert body["finished_at"] is not None


@pytest.mark.asyncio
async def test_get_unknown_run_is_404(ctx: _Ctx) -> None:
    resp = await ctx.client.get(f"/v1/eval-runs/{uuid4()}")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_list_runs_returns_tenant_runs_newest_first(ctx: _Ctx) -> None:
    first = (await ctx.client.post("/v1/eval-runs", json={"suite": "m0_baseline"})).json()["id"]
    second = (await ctx.client.post("/v1/eval-runs", json={"suite": "m0_baseline"})).json()["id"]

    resp = await ctx.client.get("/v1/eval-runs")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 2
    # Newest first.
    assert [it["id"] for it in body["items"]] == [second, first]


@pytest.mark.asyncio
async def test_list_runs_filters_by_status(ctx: _Ctx) -> None:
    queued = (await ctx.client.post("/v1/eval-runs", json={"suite": "m0_baseline"})).json()["id"]
    other = (await ctx.client.post("/v1/eval-runs", json={"suite": "m0_baseline"})).json()["id"]
    await ctx.store.set_status(run_id=UUID(other), tenant_id=_TENANT, status=EvalRunStatus.PASSED)

    resp = await ctx.client.get("/v1/eval-runs", params={"status": "queued"})
    assert resp.status_code == 200
    body = resp.json()
    assert [it["id"] for it in body["items"]] == [queued]
    assert body["total"] == 1


@pytest.mark.asyncio
async def test_list_runs_is_tenant_scoped(ctx: _Ctx) -> None:
    # A run owned by another tenant must not surface in the caller's list.
    await ctx.store.create_run(
        EvalRunRecord(
            id=uuid4(),
            tenant_id=uuid4(),
            suite="m0_baseline",
            status=EvalRunStatus.QUEUED,
            triggered_by=EvalTriggeredBy.MANUAL,
            created_at=datetime.now(UTC),
        )
    )
    resp = await ctx.client.get("/v1/eval-runs")
    assert resp.status_code == 200
    assert resp.json()["total"] == 0


@pytest.mark.asyncio
async def test_list_cases(ctx: _Ctx) -> None:
    run_id = (await ctx.client.post("/v1/eval-runs", json={"suite": "m0_baseline"})).json()["id"]
    rid = UUID(run_id)
    await ctx.store.append_case_result(
        EvalCaseResultRecord(
            run_id=rid,
            tenant_id=_TENANT,
            capability="J.1_plan_execute",
            case_id="J.1_plan_execute",
            passed=True,
            scores={"pass_rate": 1.0},
        )
    )

    resp = await ctx.client.get(f"/v1/eval-runs/{run_id}/cases")
    assert resp.status_code == 200
    cases = resp.json()["cases"]
    assert len(cases) == 1
    assert cases[0]["capability"] == "J.1_plan_execute"
    assert cases[0]["passed"] is True
    assert cases[0]["scores"] == {"pass_rate": 1.0}


# ---------------------------------------------------------------------------
# W3 — eval-runs 读端点接跨租户 scope(系统管理员租户切换器)
#
# 三件套 per endpoint:system_admin 带目标租户 tenant_id → 200;普通租户
# 用户带他租户 tenant_id → 403 TENANT_NOT_ALLOWED;详情端点 tenant_id=* →
# 400 SCOPE_ALL_NOT_SUPPORTED。照 test_agents_api.py W2 先例。
# ---------------------------------------------------------------------------


async def _grant_system_admin(client: AsyncClient) -> dict[str, str]:
    """Seed a platform-scope binding; return headers for a system_admin whose
    HOME tenant differs from ``_TENANT`` (the tenant under test)."""
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
async def test_eval_runs_system_admin_target_tenant_200(ctx: _Ctx) -> None:
    created = await ctx.client.post("/v1/eval-runs", json={"suite": "m0_baseline"})
    run_id = created.json()["id"]
    headers = await _grant_system_admin(ctx.client)
    params = {"tenant_id": str(_TENANT)}

    listed = await ctx.client.get("/v1/eval-runs", params=params, headers=headers)
    assert listed.status_code == 200, listed.text
    assert [r["id"] for r in listed.json()["items"]] == [run_id]

    got = await ctx.client.get(f"/v1/eval-runs/{run_id}", params=params, headers=headers)
    assert got.status_code == 200, got.text
    assert got.json()["id"] == run_id

    cases = await ctx.client.get(f"/v1/eval-runs/{run_id}/cases", params=params, headers=headers)
    assert cases.status_code == 200, cases.text
    assert cases.json()["cases"] == []


@pytest.mark.asyncio
async def test_eval_runs_list_star_falls_back_to_home_tenant(ctx: _Ctx) -> None:
    """``tenant_id=*``:EvalRunStore 无聚合读法(spec 非目标)→ 回落
    system_admin 归属租户(照前端 concreteTenantScope 口径),不 500/400。"""
    await ctx.client.post("/v1/eval-runs", json={"suite": "m0_baseline"})
    headers = await _grant_system_admin(ctx.client)
    resp = await ctx.client.get("/v1/eval-runs", params={"tenant_id": "*"}, headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["items"] == []


@pytest.mark.asyncio
async def test_eval_runs_foreign_tenant_user_403(ctx: _Ctx) -> None:
    created = await ctx.client.post("/v1/eval-runs", json={"suite": "m0_baseline"})
    run_id = created.json()["id"]
    foreign = {"Authorization": f"Bearer {make_test_jwt(tenant_id=uuid4())}"}
    for name, path in [
        ("list_runs", "/v1/eval-runs"),
        ("get_run", f"/v1/eval-runs/{run_id}"),
        ("list_cases", f"/v1/eval-runs/{run_id}/cases"),
    ]:
        resp = await ctx.client.get(path, params={"tenant_id": str(_TENANT)}, headers=foreign)
        assert resp.status_code == 403, f"{name}: {resp.status_code} {resp.text}"
        assert resp.json()["detail"]["code"] == "TENANT_NOT_ALLOWED", name


@pytest.mark.asyncio
async def test_eval_runs_detail_tenant_id_star_400(ctx: _Ctx) -> None:
    created = await ctx.client.post("/v1/eval-runs", json={"suite": "m0_baseline"})
    run_id = created.json()["id"]
    for name, path in [
        ("get_run", f"/v1/eval-runs/{run_id}"),
        ("list_cases", f"/v1/eval-runs/{run_id}/cases"),
    ]:
        resp = await ctx.client.get(path, params={"tenant_id": "*"})
        assert resp.status_code == 400, f"{name}: {resp.status_code} {resp.text}"
        assert resp.json()["detail"]["code"] == "SCOPE_ALL_NOT_SUPPORTED", name
