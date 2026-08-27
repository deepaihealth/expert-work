"""Endpoint tests for ``/v1/platform/quality-config`` — Stream RT-5 (PR-3b).

Mirrors ``test_platform_judge_config_api.py``: ``create_app`` wires
``quality_config_service`` + ``platform_secrets_service`` onto ``app.state``; a
system_admin role_binding is seeded; principal via JWT.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from control_plane.app import create_app
from control_plane.auth import JWTVerifier
from control_plane.settings import Settings
from expert_work.common.lifecycle import Lifecycle
from expert_work.protocol import Role
from tests.auth_fixtures import make_test_jwt


def _valid_payload(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "enabled": True,
        "sampling_rate_pct": 100,
        "daily_cap": 500,
        "monitor_interval_s": 15,
        "monitor_batch_size": 200,
        "judge_provider": "qwen",
        "judge_model": "qwen3.7-max",
        "drift_interval_s": 20,
        "drift_recent_window_h": 24,
        "drift_baseline_window_h": 168,
        "drift_min_samples": 10,
        "drift_threshold": 0.15,
        "drift_cooldown_h": 24,
    }
    base.update(over)
    return base


async def _seed_admin(app: object) -> UUID:
    sys_admin_id = uuid4()
    await app.state.role_binding_repo.create(  # type: ignore[attr-defined]
        subject_type="user",
        subject_id=sys_admin_id,
        tenant_id=None,
        role=Role.SYSTEM_ADMIN,
        platform_scope=True,
        granted_by="seed",
    )
    return sys_admin_id


def _headers(subject: UUID) -> dict[str, str]:
    return {"Authorization": f"Bearer {make_test_jwt(tenant_id=uuid4(), subject=str(subject))}"}


@pytest.fixture
async def admin_client(
    settings: Settings,
    lifecycle: Lifecycle,
    jwt_verifier: JWTVerifier,
) -> AsyncIterator[tuple[AsyncClient, UUID]]:
    settings = settings.model_copy(
        update={
            "supported_providers": ["qwen"],
            "platform_provider_credentials": {"qwen": "secret://x"},
        }
    )
    app = create_app(settings=settings, lifecycle=lifecycle, jwt_verifier=jwt_verifier)
    sys_admin_id = await _seed_admin(app)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://control-plane.test") as client:
        yield client, sys_admin_id


@pytest.mark.asyncio
async def test_non_admin_forbidden(admin_client: tuple[AsyncClient, UUID]) -> None:
    client, _ = admin_client
    resp = await client.get("/v1/platform/quality-config", headers=_headers(uuid4()))
    assert resp.status_code == 403
    assert resp.json()["detail"]["code"] == "PLATFORM_SCOPE_FORBIDDEN"


@pytest.mark.asyncio
async def test_get_unset_returns_disabled_default(admin_client: tuple[AsyncClient, UUID]) -> None:
    client, admin = admin_client
    resp = await client.get("/v1/platform/quality-config", headers=_headers(admin))
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["is_default"] is True
    # No row → disabled (opt-in) with env-default params.
    assert data["config"]["enabled"] is False
    assert data["config"]["sampling_rate_pct"] == 5
    assert data["config"]["judge_provider"] == "anthropic"


@pytest.mark.asyncio
async def test_put_valid_then_get_reflects(admin_client: tuple[AsyncClient, UUID]) -> None:
    client, admin = admin_client
    resp = await client.put(
        "/v1/platform/quality-config", headers=_headers(admin), json=_valid_payload()
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["config"]["enabled"] is True
    get_resp = await client.get("/v1/platform/quality-config", headers=_headers(admin))
    data = get_resp.json()["data"]
    assert data["is_default"] is False
    assert data["config"]["enabled"] is True
    assert data["config"]["sampling_rate_pct"] == 100
    assert data["config"]["judge_provider"] == "qwen"


@pytest.mark.asyncio
async def test_put_judge_provider_key_missing(admin_client: tuple[AsyncClient, UUID]) -> None:
    client, admin = admin_client
    resp = await client.put(
        "/v1/platform/quality-config",
        headers=_headers(admin),
        json=_valid_payload(judge_provider="openai", judge_model="gpt-4o"),
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"]["code"] == "JUDGE_PROVIDER_KEY_MISSING"


@pytest.mark.asyncio
async def test_put_invalid_judge_model(admin_client: tuple[AsyncClient, UUID]) -> None:
    client, admin = admin_client
    # text-embedding-v4 is an embedding model, not a chat model → would drop
    # every sample; the endpoint rejects it (parity with judge-config).
    resp = await client.put(
        "/v1/platform/quality-config",
        headers=_headers(admin),
        json=_valid_payload(judge_model="text-embedding-v4"),
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"]["code"] == "INVALID_JUDGE_MODEL"


@pytest.mark.asyncio
async def test_put_range_validation(admin_client: tuple[AsyncClient, UUID]) -> None:
    client, admin = admin_client
    resp = await client.put(
        "/v1/platform/quality-config",
        headers=_headers(admin),
        json=_valid_payload(sampling_rate_pct=200),  # > 100
    )
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_put_non_admin_forbidden(admin_client: tuple[AsyncClient, UUID]) -> None:
    client, _ = admin_client
    resp = await client.put(
        "/v1/platform/quality-config", headers=_headers(uuid4()), json=_valid_payload()
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_put_emits_audit(admin_client: tuple[AsyncClient, UUID]) -> None:
    client, admin = admin_client
    resp = await client.put(
        "/v1/platform/quality-config", headers=_headers(admin), json=_valid_payload()
    )
    assert resp.status_code == 200, resp.text
    store = client._transport.app.state.audit_logger._store  # type: ignore[attr-defined]
    entries = list(store._rows.values())
    matched = [e for e in entries if e.action.value == "platform_quality_config:updated"]
    assert matched, "expected a PLATFORM_QUALITY_CONFIG_UPDATED audit row"


# ─── PR-E3b — invalidation-bus broadcast on the write endpoint ─────────────


class _SpyBus:
    def __init__(self) -> None:
        self.events: list[object] = []

    async def publish(self, event: object) -> None:
        self.events.append(event)

    def publish_soon(self, event: object) -> None:
        self.events.append(event)


@pytest.mark.asyncio
async def test_put_broadcasts_platform_quality(
    settings: Settings,
    lifecycle: Lifecycle,
    jwt_verifier: JWTVerifier,
) -> None:
    """``put`` self-invalidates THIS pod's cache; the endpoint must also
    broadcast ``platform_quality`` so peer replicas drop theirs."""
    settings = settings.model_copy(
        update={
            "supported_providers": ["qwen"],
            "platform_provider_credentials": {"qwen": "secret://x"},
        }
    )
    app = create_app(settings=settings, lifecycle=lifecycle, jwt_verifier=jwt_verifier)
    admin = await _seed_admin(app)
    spy_bus = _SpyBus()
    app.state.invalidation_bus = spy_bus
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://control-plane.test") as client:
        resp = await client.put(
            "/v1/platform/quality-config",
            headers=_headers(admin),
            json=_valid_payload(),
        )
    assert resp.status_code == 200, resp.text
    assert len(spy_bus.events) == 1
    assert spy_bus.events[0].kind == "platform_quality"  # type: ignore[attr-defined]
