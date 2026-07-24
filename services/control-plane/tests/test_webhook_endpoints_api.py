"""End-to-end tests for the HX-9 webhook-endpoint CRUD API (STREAM-HX § 13)."""

from __future__ import annotations

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
    make_test_jwt,
)

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
