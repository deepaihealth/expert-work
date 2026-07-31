"""End-to-end tests for the RT-5 ``/v1/quality`` dashboard read API.

Seeds the in-memory quality-score / drift-alert stores that ``create_app``
attaches to ``app.state`` (no repo-injection kwarg needed), then reads them
back through the authenticated, tenant-scoped router.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from control_plane.app import create_app
from control_plane.audit import build_default_audit_logger
from control_plane.settings import DEFAULT_DEV_TENANT_ID, Settings
from expert_work.persistence.audit_log import InMemoryAuditLogStore
from expert_work.protocol import QualityDriftAlertRecord, QualityScoreRecord, Role
from tests.agent_fixtures import stub_agent_runtime
from tests.auth_fixtures import (
    TEST_AUDIENCE,
    TEST_ISSUER,
    build_test_jwt_verifier,
    make_test_jwt,
)

_TENANT = DEFAULT_DEV_TENANT_ID


class _Ctx:
    def __init__(self, client: AsyncClient, app: object) -> None:
        self.client = client
        self.scores = app.state.quality_score_store  # type: ignore[attr-defined]
        self.alerts = app.state.quality_drift_alert_store  # type: ignore[attr-defined]


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
    app = create_app(
        settings=settings,
        audit_logger=build_default_audit_logger(InMemoryAuditLogStore()),
        jwt_verifier=build_test_jwt_verifier(),
        agent_runtime=stub_agent_runtime(),
        enable_scheduler=False,
    )
    transport = ASGITransport(app=app)
    headers = {"Authorization": f"Bearer {make_test_jwt(tenant_id=_TENANT)}"}
    async with AsyncClient(
        transport=transport, base_url="http://control-plane.test", headers=headers
    ) as client:
        yield _Ctx(client, app)


def _score(
    *, agent: str, overall: int, at: datetime, tenant: object = _TENANT
) -> QualityScoreRecord:
    return QualityScoreRecord(
        tenant_id=tenant,  # type: ignore[arg-type]
        agent_name=agent,
        agent_version="1",
        run_id=uuid4(),
        thread_id=uuid4(),
        overall=overall,
        dimensions={"addressed_request": overall, "coherence": overall, "safety": 5},
        rationale="ok",
        judge_model="claude-haiku-4-5-20251001",
        observed_at=at,
    )


@pytest.mark.asyncio
async def test_list_scores_newest_first_carries_drill_fields(ctx: _Ctx) -> None:
    now = datetime.now(tz=UTC)
    older = await ctx.scores.insert(_score(agent="a", overall=5, at=now - timedelta(hours=2)))
    newer = await ctx.scores.insert(_score(agent="a", overall=2, at=now - timedelta(minutes=5)))

    resp = await ctx.client.get("/v1/quality/scores")
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert [it["overall"] for it in items] == [2, 5]  # newest first
    top = items[0]
    # Drill fields present so the UI can link to run_detail.
    assert top["run_id"] == str(newer.run_id)
    assert top["thread_id"] == str(newer.thread_id)
    assert top["dimensions"]["addressed_request"] == 2
    assert top["rationale"] == "ok"
    assert items[1]["run_id"] == str(older.run_id)


@pytest.mark.asyncio
async def test_list_scores_filters_by_agent(ctx: _Ctx) -> None:
    now = datetime.now(tz=UTC)
    await ctx.scores.insert(_score(agent="a", overall=4, at=now))
    await ctx.scores.insert(_score(agent="b", overall=3, at=now))

    resp = await ctx.client.get("/v1/quality/scores", params={"agent_name": "b"})
    items = resp.json()["items"]
    assert [it["agent_name"] for it in items] == ["b"]


@pytest.mark.asyncio
async def test_list_scores_window_excludes_stale(ctx: _Ctx) -> None:
    now = datetime.now(tz=UTC)
    await ctx.scores.insert(_score(agent="a", overall=4, at=now - timedelta(hours=1)))
    await ctx.scores.insert(_score(agent="a", overall=4, at=now - timedelta(hours=200)))

    resp = await ctx.client.get("/v1/quality/scores", params={"window_h": 168})
    assert len(resp.json()["items"]) == 1


@pytest.mark.asyncio
async def test_list_scores_tenant_scoped(ctx: _Ctx) -> None:
    now = datetime.now(tz=UTC)
    await ctx.scores.insert(_score(agent="a", overall=4, at=now, tenant=uuid4()))

    resp = await ctx.client.get("/v1/quality/scores")
    assert resp.json()["items"] == []


@pytest.mark.asyncio
async def test_list_drift_alerts_newest_first(ctx: _Ctx) -> None:
    now = datetime.now(tz=UTC)
    await ctx.alerts.insert(
        QualityDriftAlertRecord(
            tenant_id=_TENANT,
            agent_name="a",
            recent_mean=3.0,
            baseline_mean=5.0,
            drift_pct=0.4,
            recent_count=10,
            baseline_count=40,
            detected_at=now - timedelta(hours=3),
        )
    )
    await ctx.alerts.insert(
        QualityDriftAlertRecord(
            tenant_id=_TENANT,
            agent_name="b",
            recent_mean=2.0,
            baseline_mean=4.0,
            drift_pct=0.5,
            recent_count=12,
            baseline_count=50,
            detected_at=now - timedelta(minutes=10),
        )
    )

    resp = await ctx.client.get("/v1/quality/drift-alerts")
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert [it["agent_name"] for it in items] == ["b", "a"]
    assert items[0]["drift_pct"] == 0.5
    assert items[0]["recent_mean"] == 2.0


@pytest.mark.asyncio
async def test_list_drift_alerts_tenant_scoped(ctx: _Ctx) -> None:
    await ctx.alerts.insert(
        QualityDriftAlertRecord(
            tenant_id=uuid4(),
            agent_name="a",
            recent_mean=3.0,
            baseline_mean=5.0,
            drift_pct=0.4,
            recent_count=10,
            baseline_count=40,
        )
    )
    resp = await ctx.client.get("/v1/quality/drift-alerts")
    assert resp.json()["items"] == []


# ---------------------------------------------------------------------------
# W3 — quality 读端点接跨租户 scope(系统管理员租户切换器)
#
# 三件套(列表无 "*" 详情拒绝项):system_admin 带目标租户 tenant_id →
# 200 命中目标租户数据;普通租户用户带他租户 tenant_id → 403
# TENANT_NOT_ALLOWED;"*" 无聚合读法 → 回落归属租户。照 W2 先例。
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


def _seed_alert(*, at: datetime) -> QualityDriftAlertRecord:
    return QualityDriftAlertRecord(
        tenant_id=_TENANT,
        agent_name="a",
        recent_mean=3.0,
        baseline_mean=5.0,
        drift_pct=0.4,
        recent_count=10,
        baseline_count=40,
        detected_at=at,
    )


@pytest.mark.asyncio
async def test_quality_system_admin_target_tenant_200(ctx: _Ctx) -> None:
    now = datetime.now(tz=UTC)
    await ctx.scores.insert(_score(agent="a", overall=4, at=now))
    await ctx.alerts.insert(_seed_alert(at=now))
    headers = await _grant_system_admin(ctx.client)
    params = {"tenant_id": str(_TENANT)}

    scores = await ctx.client.get("/v1/quality/scores", params=params, headers=headers)
    assert scores.status_code == 200, scores.text
    assert [it["agent_name"] for it in scores.json()["items"]] == ["a"]

    alerts = await ctx.client.get("/v1/quality/drift-alerts", params=params, headers=headers)
    assert alerts.status_code == 200, alerts.text
    assert [it["agent_name"] for it in alerts.json()["items"]] == ["a"]


@pytest.mark.asyncio
async def test_quality_star_falls_back_to_home_tenant(ctx: _Ctx) -> None:
    """``tenant_id=*``:quality store 无聚合读法(spec 非目标)→ 回落
    system_admin 归属租户(照前端 concreteTenantScope 口径),不 500/400。"""
    now = datetime.now(tz=UTC)
    await ctx.scores.insert(_score(agent="a", overall=4, at=now))
    headers = await _grant_system_admin(ctx.client)
    resp = await ctx.client.get("/v1/quality/scores", params={"tenant_id": "*"}, headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["items"] == []


@pytest.mark.asyncio
async def test_quality_foreign_tenant_user_403(ctx: _Ctx) -> None:
    foreign = {"Authorization": f"Bearer {make_test_jwt(tenant_id=uuid4())}"}
    for name, path in [
        ("list_scores", "/v1/quality/scores"),
        ("list_drift_alerts", "/v1/quality/drift-alerts"),
    ]:
        resp = await ctx.client.get(path, params={"tenant_id": str(_TENANT)}, headers=foreign)
        assert resp.status_code == 403, f"{name}: {resp.status_code} {resp.text}"
        assert resp.json()["detail"]["code"] == "TENANT_NOT_ALLOWED", name


@pytest.mark.asyncio
async def test_quality_drift_alerts_star_falls_back_to_home_tenant(ctx: _Ctx) -> None:
    """``tenant_id=*`` 回落断言补齐 /drift-alerts(与 /scores 同 helper,M-3)。"""
    now = datetime.now(tz=UTC)
    await ctx.alerts.insert(_seed_alert(at=now))
    headers = await _grant_system_admin(ctx.client)
    resp = await ctx.client.get(
        "/v1/quality/drift-alerts", params={"tenant_id": "*"}, headers=headers
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["items"] == []
