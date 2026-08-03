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
from expert_work.protocol import QualityDriftAlertRecord, QualityScoreRecord
from tests.agent_fixtures import stub_agent_runtime
from tests.auth_fixtures import (
    TEST_AUDIENCE,
    TEST_ISSUER,
    build_test_jwt_verifier,
    grant_system_admin,
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
# W3/W4 — quality 读端点接跨租户 scope(系统管理员租户切换器)
#
# 三件套(列表无 "*" 详情拒绝项):system_admin 带目标租户 tenant_id →
# 200 命中目标租户数据;普通租户用户带他租户 tenant_id → 403
# TENANT_NOT_ALLOWED;tenant_id=* → W4 真聚合(全租户行,每行带
# tenant_id)。照 W2 先例。
# ---------------------------------------------------------------------------


def _seed_alert(
    *, at: datetime, tenant: object = _TENANT, agent: str = "a"
) -> QualityDriftAlertRecord:
    return QualityDriftAlertRecord(
        tenant_id=tenant,  # type: ignore[arg-type]
        agent_name=agent,
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
    headers = await grant_system_admin(ctx.client)
    params = {"tenant_id": str(_TENANT)}

    scores = await ctx.client.get("/v1/quality/scores", params=params, headers=headers)
    assert scores.status_code == 200, scores.text
    assert [it["agent_name"] for it in scores.json()["items"]] == ["a"]

    alerts = await ctx.client.get("/v1/quality/drift-alerts", params=params, headers=headers)
    assert alerts.status_code == 200, alerts.text
    assert [it["agent_name"] for it in alerts.json()["items"]] == ["a"]


@pytest.mark.asyncio
async def test_quality_scores_star_aggregates_all_tenants(ctx: _Ctx) -> None:
    """W4:system_admin ``tenant_id=*`` 真聚合——全租户行,每行带
    ``tenant_id``;非聚合分支的行同样带 ``tenant_id``(值=该租户)。"""
    now = datetime.now(tz=UTC)
    other_tenant = uuid4()
    await ctx.scores.insert(_score(agent="a", overall=4, at=now))
    await ctx.scores.insert(_score(agent="b", overall=3, at=now, tenant=other_tenant))

    # Non-aggregate branch: items carry tenant_id = the scoped tenant.
    plain = await ctx.client.get("/v1/quality/scores")
    assert [it["tenant_id"] for it in plain.json()["items"]] == [str(_TENANT)]

    headers = await grant_system_admin(ctx.client)
    resp = await ctx.client.get("/v1/quality/scores", params={"tenant_id": "*"}, headers=headers)
    assert resp.status_code == 200, resp.text
    by_agent = {it["agent_name"]: it for it in resp.json()["items"]}
    assert by_agent["a"]["tenant_id"] == str(_TENANT)
    assert by_agent["b"]["tenant_id"] == str(other_tenant)


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
async def test_quality_drift_alerts_star_aggregates_all_tenants(ctx: _Ctx) -> None:
    """W4 聚合断言补齐 /drift-alerts(与 /scores 同 helper)。"""
    now = datetime.now(tz=UTC)
    other_tenant = uuid4()
    await ctx.alerts.insert(_seed_alert(at=now))
    await ctx.alerts.insert(_seed_alert(at=now, tenant=other_tenant, agent="b"))

    # Non-aggregate branch: items carry tenant_id = the scoped tenant.
    plain = await ctx.client.get("/v1/quality/drift-alerts")
    assert [it["tenant_id"] for it in plain.json()["items"]] == [str(_TENANT)]

    headers = await grant_system_admin(ctx.client)
    resp = await ctx.client.get(
        "/v1/quality/drift-alerts", params={"tenant_id": "*"}, headers=headers
    )
    assert resp.status_code == 200, resp.text
    by_agent = {it["agent_name"]: it for it in resp.json()["items"]}
    assert by_agent["a"]["tenant_id"] == str(_TENANT)
    assert by_agent["b"]["tenant_id"] == str(other_tenant)
