"""End-to-end tests for the HX-9 webhook-endpoint CRUD API (STREAM-HX § 13)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from control_plane.app import create_app
from control_plane.audit import build_default_audit_logger
from control_plane.settings import DEFAULT_DEV_TENANT_ID, Settings
from expert_work.persistence.audit_log import InMemoryAuditLogStore
from expert_work.protocol import AuditAction, AuditQuery, WebhookDeliveryRecord
from tests.agent_fixtures import stub_agent_runtime
from tests.auth_fixtures import (
    TEST_AUDIENCE,
    TEST_ISSUER,
    build_test_jwt_verifier,
    grant_system_admin,
    make_test_jwt,
)
from tests.fake_advisory_lock import FakeAdvisoryLockSessionFactory

_DEFAULT_TENANT = DEFAULT_DEV_TENANT_ID


@pytest.fixture
def audit_store() -> InMemoryAuditLogStore:
    return InMemoryAuditLogStore()


@pytest.fixture
async def client(audit_store: InMemoryAuditLogStore) -> AsyncIterator[AsyncClient]:
    settings = Settings(
        env="dev",
        auth_mode="dev",
        rate_limit_burst=10_000,
        rate_limit_per_second=10_000.0,
        oidc_issuer=TEST_ISSUER,
        oidc_audience=[TEST_AUDIENCE],
        max_webhook_endpoints_per_tenant=2,  # low cap so the quota test is cheap
    )
    app = create_app(
        settings=settings,
        audit_logger=build_default_audit_logger(audit_store),
        jwt_verifier=build_test_jwt_verifier(),
        agent_runtime=stub_agent_runtime(),
        enable_scheduler=False,
    )
    transport = ASGITransport(app=app)
    headers = {"Authorization": f"Bearer {make_test_jwt(tenant_id=_DEFAULT_TENANT)}"}
    async with AsyncClient(
        transport=transport, base_url="http://control-plane.test", headers=headers
    ) as c:
        yield c


async def _create(
    client: AsyncClient,
    *,
    name: str = "ops",
    url: str = "https://hooks.example.com/ingest",
    event_types: list[str] | None = None,
) -> dict[str, object]:
    resp = await client.post(
        "/v1/webhook-endpoints",
        json={
            "name": name,
            "url": url,
            "event_types": event_types or ["run.completed"],
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


@pytest.mark.asyncio
async def test_create_shows_secret_once_then_never(client: AsyncClient) -> None:
    created = await _create(client)
    assert created["secret"]  # plaintext shown at creation
    assert created["enabled"] is True
    assert created["event_types"] == ["run.completed"]

    got = await client.get(f"/v1/webhook-endpoints/{created['id']}")
    assert got.status_code == 200
    assert "secret" not in got.json()  # never again


@pytest.mark.asyncio
async def test_create_rejects_ssrf_url(client: AsyncClient) -> None:
    resp = await client.post(
        "/v1/webhook-endpoints",
        json={
            "name": "evil",
            "url": "http://169.254.169.254/latest",
            "event_types": ["run.failed"],
        },
    )
    assert resp.status_code == 422
    assert "url" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_create_rejects_unknown_event_type(client: AsyncClient) -> None:
    resp = await client.post(
        "/v1/webhook-endpoints",
        json={"name": "x", "url": "https://h.example.com", "event_types": ["run.exploded"]},
    )
    assert resp.status_code == 422
    assert "unknown event_types" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_create_accepts_skill_promote_requested(client: AsyncClient) -> None:
    """SE-16 PR-8 — the new event type is subscribable."""
    resp = await client.post(
        "/v1/webhook-endpoints",
        json={
            "name": "skill-reviews",
            "url": "https://h.example.com/skills",
            "event_types": ["skill_promote.requested"],
        },
    )
    assert resp.status_code == 201
    assert resp.json()["event_types"] == ["skill_promote.requested"]


@pytest.mark.asyncio
async def test_duplicate_name_conflicts(client: AsyncClient) -> None:
    await _create(client, name="dup")
    resp = await client.post(
        "/v1/webhook-endpoints",
        json={"name": "dup", "url": "https://h.example.com", "event_types": ["run.completed"]},
    )
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_quota_exhausted(client: AsyncClient) -> None:
    await _create(client, name="a")
    await _create(client, name="b")
    resp = await client.post(
        "/v1/webhook-endpoints",
        json={"name": "c", "url": "https://h.example.com", "event_types": ["run.completed"]},
    )
    assert resp.status_code == 429


@pytest.mark.asyncio
async def test_concurrent_creates_do_not_overshoot_quota(
    audit_store: InMemoryAuditLogStore,
) -> None:
    """W1-PR2 Task 4 — count-then-insert TOCTOU regression.

    Two replicas serving the same tenant can both read the same under-cap
    count and both insert, overshooting the cap. ``count_by_tenant`` is
    wrapped with a delay *after* it computes the real count so two
    concurrent requests' count-checks genuinely overlap (asyncio's
    cooperative scheduler never preempts mid-coroutine — without a real
    await point between the two racing calls, ``asyncio.gather`` would
    just run them one at a time and never reproduce the hazard). A fake
    advisory-lock session factory stands in for Postgres so the fix's
    exclusion is provable without a real DB (``FakeAdvisoryLockSession``'s
    contract is exercised for real against Postgres separately in
    ``test_tenant_resource_lock_integration.py``).
    """
    settings = Settings(
        env="dev",
        auth_mode="dev",
        rate_limit_burst=10_000,
        rate_limit_per_second=10_000.0,
        oidc_issuer=TEST_ISSUER,
        oidc_audience=[TEST_AUDIENCE],
        max_webhook_endpoints_per_tenant=1,
    )
    app = create_app(
        settings=settings,
        audit_logger=build_default_audit_logger(audit_store),
        jwt_verifier=build_test_jwt_verifier(),
        agent_runtime=stub_agent_runtime(),
        enable_scheduler=False,
    )
    app.state.session_factory = FakeAdvisoryLockSessionFactory()

    store = app.state.webhook_endpoint_store
    real_count_by_tenant = store.count_by_tenant

    async def _slow_count_by_tenant(*, tenant_id: UUID) -> int:
        n = await real_count_by_tenant(tenant_id=tenant_id)
        await asyncio.sleep(0.2)  # widen the race window past the lock's retry budget
        return n

    store.count_by_tenant = _slow_count_by_tenant  # type: ignore[method-assign]

    transport = ASGITransport(app=app)
    headers = {"Authorization": f"Bearer {make_test_jwt(tenant_id=_DEFAULT_TENANT)}"}
    async with (
        AsyncClient(
            transport=transport, base_url="http://control-plane.test", headers=headers
        ) as client_a,
        AsyncClient(
            transport=transport, base_url="http://control-plane.test", headers=headers
        ) as client_b,
    ):

        async def _post(client: AsyncClient, name: str) -> int:
            resp = await client.post(
                "/v1/webhook-endpoints",
                json={
                    "name": name,
                    "url": "https://h.example.com",
                    "event_types": ["run.completed"],
                },
            )
            return resp.status_code

        status_a, status_b = await asyncio.gather(
            _post(client_a, "race-a"), _post(client_b, "race-b")
        )

    assert sorted([status_a, status_b]) == [201, 429]
    assert await real_count_by_tenant(tenant_id=_DEFAULT_TENANT) == 1


@pytest.mark.asyncio
async def test_list_and_get_404(client: AsyncClient) -> None:
    await _create(client, name="one")
    await _create(client, name="two")
    listed = await client.get("/v1/webhook-endpoints")
    assert listed.status_code == 200
    body = listed.json()
    assert body["total"] == 2
    assert body["cross_tenant"] is False

    missing = await client.get(f"/v1/webhook-endpoints/{uuid4()}")
    assert missing.status_code == 404


@pytest.mark.asyncio
async def test_patch_updates_fields(client: AsyncClient) -> None:
    created = await _create(client, name="patch-me")
    resp = await client.patch(
        f"/v1/webhook-endpoints/{created['id']}",
        json={"enabled": False, "event_types": ["run.completed", "approval.requested"]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["enabled"] is False
    assert set(body["event_types"]) == {"run.completed", "approval.requested"}


@pytest.mark.asyncio
async def test_patch_rejects_ssrf_url(client: AsyncClient) -> None:
    created = await _create(client, name="patch-ssrf")
    resp = await client.patch(
        f"/v1/webhook-endpoints/{created['id']}",
        json={"url": "http://127.0.0.1:8080/x"},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_delete(client: AsyncClient) -> None:
    created = await _create(client, name="bye")
    resp = await client.delete(f"/v1/webhook-endpoints/{created['id']}")
    assert resp.status_code == 200
    assert resp.json() == {"deleted": True}
    again = await client.delete(f"/v1/webhook-endpoints/{created['id']}")
    assert again.status_code == 404


@pytest.mark.asyncio
async def test_payload_format_roundtrip(client: AsyncClient) -> None:
    """Channel formats — create with feishu, read it back, patch to wecom;
    an unknown format is rejected at validation."""
    resp = await client.post(
        "/v1/webhook-endpoints",
        json={
            "name": "im-hook",
            "url": "https://open.feishu.example.com/bot/hook",
            "event_types": ["approval.requested", "skill_promote.requested"],
            "payload_format": "feishu",
        },
    )
    assert resp.status_code == 201, resp.text
    created = resp.json()
    assert created["payload_format"] == "feishu"

    got = await client.get(f"/v1/webhook-endpoints/{created['id']}")
    assert got.json()["payload_format"] == "feishu"

    patched = await client.patch(
        f"/v1/webhook-endpoints/{created['id']}", json={"payload_format": "wecom"}
    )
    assert patched.status_code == 200
    assert patched.json()["payload_format"] == "wecom"

    bad = await client.post(
        "/v1/webhook-endpoints",
        json={
            "name": "bad-format",
            "url": "https://hooks.example.com/x",
            "event_types": ["run.completed"],
            "payload_format": "slack",
        },
    )
    assert bad.status_code == 422


@pytest.mark.asyncio
async def test_default_payload_format_is_generic(client: AsyncClient) -> None:
    created = await _create(client, name="default-fmt")
    assert created["payload_format"] == "generic"


# --- 删除接口卫生 PR3 — 删 endpoint 级联清 webhook_delivery 孤儿行 -----------


async def _seed_delivery(app: object, *, endpoint_id: UUID, event_id: str) -> None:
    now = datetime.now(UTC)
    await app.state.webhook_delivery_store.create(  # type: ignore[attr-defined]
        WebhookDeliveryRecord(
            id=uuid4(),
            tenant_id=_DEFAULT_TENANT,
            endpoint_id=endpoint_id,
            event_id=event_id,
            event_type="run.completed",
            created_at=now,
            updated_at=now,
        )
    )


async def _delete_audit_entry(audit_store: InMemoryAuditLogStore, *, endpoint_id: str) -> object:
    page = await audit_store.query(AuditQuery(tenant_id=_DEFAULT_TENANT))
    return next(
        e
        for e in page.entries
        if e.action is AuditAction.WEBHOOK_ENDPOINT_DELETE and e.resource_id == endpoint_id
    )


@pytest.mark.asyncio
async def test_delete_endpoint_cascades_deliveries(
    client: AsyncClient, audit_store: InMemoryAuditLogStore
) -> None:
    """删 endpoint 连带删其 webhook_delivery;他 endpoint 的不动;审计计数正确。"""
    doomed = await _create(client, name="doomed")
    keeper = await _create(client, name="keeper")
    doomed_id = UUID(str(doomed["id"]))
    keeper_id = UUID(str(keeper["id"]))

    app = client._transport.app  # type: ignore[attr-defined,union-attr]
    await _seed_delivery(app, endpoint_id=doomed_id, event_id="run-a:1")
    await _seed_delivery(app, endpoint_id=doomed_id, event_id="run-a:2")
    await _seed_delivery(app, endpoint_id=keeper_id, event_id="run-a:1")

    resp = await client.delete(f"/v1/webhook-endpoints/{doomed_id}")
    assert resp.status_code == 200

    delivery_store = app.state.webhook_delivery_store
    assert (
        await delivery_store.list_by_endpoint(endpoint_id=doomed_id, tenant_id=_DEFAULT_TENANT)
        == []
    )
    kept = await delivery_store.list_by_endpoint(endpoint_id=keeper_id, tenant_id=_DEFAULT_TENANT)
    assert len(kept) == 1

    entry = await _delete_audit_entry(audit_store, endpoint_id=str(doomed_id))
    assert entry.details == {"deliveries_removed": 2}  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_delete_endpoint_without_deliveries_audits_zero(
    client: AsyncClient, audit_store: InMemoryAuditLogStore
) -> None:
    """0 子行时审计 details 记 deliveries_removed=0(而非缺字段)。"""
    created = await _create(client, name="childless")
    endpoint_id = str(created["id"])

    resp = await client.delete(f"/v1/webhook-endpoints/{endpoint_id}")
    assert resp.status_code == 200

    entry = await _delete_audit_entry(audit_store, endpoint_id=endpoint_id)
    assert entry.details == {"deliveries_removed": 0}  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# W3 — webhook 详情读端点接跨租户 scope(系统管理员租户切换器)
#
# 三件套:system_admin 带目标租户 tenant_id → 200;普通租户用户带他租户
# tenant_id → 403 TENANT_NOT_ALLOWED;tenant_id=* → 400
# SCOPE_ALL_NOT_SUPPORTED。照 test_agents_api.py W2 先例。
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_endpoint_system_admin_target_tenant_200(client: AsyncClient) -> None:
    created = await _create(client)
    headers = await grant_system_admin(client)
    resp = await client.get(
        f"/v1/webhook-endpoints/{created['id']}",
        params={"tenant_id": str(_DEFAULT_TENANT)},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["id"] == created["id"]


@pytest.mark.asyncio
async def test_get_endpoint_foreign_tenant_user_403(client: AsyncClient) -> None:
    created = await _create(client)
    foreign = {"Authorization": f"Bearer {make_test_jwt(tenant_id=uuid4())}"}
    resp = await client.get(
        f"/v1/webhook-endpoints/{created['id']}",
        params={"tenant_id": str(_DEFAULT_TENANT)},
        headers=foreign,
    )
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"]["code"] == "TENANT_NOT_ALLOWED"


@pytest.mark.asyncio
async def test_get_endpoint_tenant_id_star_400(client: AsyncClient) -> None:
    created = await _create(client)
    resp = await client.get(f"/v1/webhook-endpoints/{created['id']}", params={"tenant_id": "*"})
    assert resp.status_code == 400, resp.text
    assert resp.json()["detail"]["code"] == "SCOPE_ALL_NOT_SUPPORTED"


# ---------------------------------------------------------------------------
# Backlog Task 3 (security fix, spec/external-api-v1-p2) — this module's own
# docstring claims it mirrors the J.10 triggers CRUD, but all 5 routes were
# missing console_only(): a zero-scope API key could hit POST and register a
# delivery URL, after which the delivery worker fans every run.completed /
# artifact.saved event out to it — an ongoing exfiltration channel, not just
# an over-broad read. See ``api/triggers.py``'s identically-shaped gate.
# ---------------------------------------------------------------------------


def _service_account_headers(*, scopes: tuple[str, ...] = ()) -> dict[str, str]:
    """Headers for a service-account (API-key) principal. Zero-scope by
    default — the worst-case caller: a key with no scopes granted at all
    still reached every route here before console_only()."""
    token = make_test_jwt(
        tenant_id=_DEFAULT_TENANT,
        subject="sa-test",
        sub_type="service_account",
        roles=(),
        scopes=scopes,
    )
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_zero_scope_key_denied_on_every_route(client: AsyncClient) -> None:
    created = await _create(client)  # via the employee JWT, ahead of the sa probes below
    sa_headers = _service_account_headers()
    cases: list[tuple[str, str, dict[str, object] | None]] = [
        (
            "POST",
            "/v1/webhook-endpoints",
            {
                "name": "sa-try",
                "url": "https://hooks.example.com/x",
                "event_types": ["run.completed"],
            },
        ),
        ("GET", "/v1/webhook-endpoints", None),
        ("GET", f"/v1/webhook-endpoints/{created['id']}", None),
        ("PATCH", f"/v1/webhook-endpoints/{created['id']}", {"enabled": False}),
        ("DELETE", f"/v1/webhook-endpoints/{created['id']}", None),
    ]
    for method, path, body in cases:
        resp = await client.request(method, path, json=body, headers=sa_headers)
        assert resp.status_code == 403, f"{method} {path} -> {resp.status_code} {resp.text}"


@pytest.mark.asyncio
async def test_employee_jwt_still_reaches_every_route(client: AsyncClient) -> None:
    """console_only()'s predicate only rejects service_account principals —
    pin that the admin-ui's employee JWT (this file's default ``client``
    fixture) stays unaffected on all 5 routes, so a mutation that widens the
    predicate to "reject everyone" can't hide behind the zero-scope-key test
    above."""
    created = await _create(client)
    cases: list[tuple[str, str, dict[str, object] | None]] = [
        (
            "POST",
            "/v1/webhook-endpoints",
            {
                "name": "employee-try",
                "url": "https://hooks.example.com/y",
                "event_types": ["run.completed"],
            },
        ),
        ("GET", "/v1/webhook-endpoints", None),
        ("GET", f"/v1/webhook-endpoints/{created['id']}", None),
        ("PATCH", f"/v1/webhook-endpoints/{created['id']}", {"enabled": False}),
        ("DELETE", f"/v1/webhook-endpoints/{created['id']}", None),
    ]
    for method, path, body in cases:
        resp = await client.request(method, path, json=body)
        assert resp.status_code != 403, f"{method} {path} -> {resp.status_code} {resp.text}"
