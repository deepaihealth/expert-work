"""Endpoint tests for ``GET /v1/platform/embedding-config`` — Stream T (PR C).

Mirrors ``test_platform_config_api.py``: the full ``create_app`` harness wires
``platform_embedding_config_service`` + ``platform_secrets_service`` onto
``app.state``; a system_admin ``role_binding`` is seeded and the principal is
delivered via a real JWT.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from control_plane.app import create_app
from control_plane.auth import JWTVerifier
from control_plane.settings import Settings
from expert_work.common.lifecycle import Lifecycle
from expert_work.protocol import Role
from tests.auth_fixtures import make_test_jwt


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
    # Configure ``qwen`` so it is "configured" in the platform secrets overlay,
    # and pin the effective embedding/rerank to known catalog models.
    settings = settings.model_copy(
        update={
            "supported_providers": ["qwen"],
            "platform_provider_credentials": {"qwen": "secret://x"},
            "embedding_provider": "qwen",
            "embedding_model": "text-embedding-v4",
            "rerank_provider": "qwen",
            "rerank_model": "qwen3-vl-rerank",
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
    resp = await client.get("/v1/platform/embedding-config", headers=_headers(uuid4()))
    assert resp.status_code == 403
    assert resp.json()["detail"]["code"] == "PLATFORM_SCOPE_FORBIDDEN"


@pytest.mark.asyncio
async def test_get_returns_selection_and_options(
    admin_client: tuple[AsyncClient, UUID],
) -> None:
    client, admin = admin_client
    resp = await client.get("/v1/platform/embedding-config", headers=_headers(admin))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["success"] is True
    assert body["error"] is None
    data = body["data"]

    # Current effective selection reflects the service (env fallback here).
    assert data["embedding"] == {"provider": "qwen", "model": "text-embedding-v4"}
    assert data["rerank"] == {"provider": "qwen", "model": "qwen3-vl-rerank"}

    # Options: only configured providers' catalog entries surface.
    assert {"provider": "qwen", "model": "text-embedding-v4"} in data["available_embedding"]
    assert {"provider": "qwen", "model": "qwen3-vl-rerank"} in data["available_rerank"]

    # Unconfigured providers (e.g. openai) must NOT contribute options.
    configured = {opt["provider"] for opt in data["available_embedding"]}
    assert configured == {"qwen"}
    assert all(opt["provider"] == "qwen" for opt in data["available_rerank"])


@pytest.mark.asyncio
async def test_put_embedding_only_then_get_reflects(
    admin_client: tuple[AsyncClient, UUID],
) -> None:
    client, admin = admin_client
    resp = await client.put(
        "/v1/platform/embedding-config",
        headers=_headers(admin),
        json={"embedding_provider": "qwen", "embedding_model": "text-embedding-v4"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["success"] is True
    assert body["error"] is None
    assert body["data"]["embedding"] == {"provider": "qwen", "model": "text-embedding-v4"}
    # A DB row exists now → rerank is off (no fallback to env once a row exists).
    assert body["data"]["rerank"] is None

    get_resp = await client.get("/v1/platform/embedding-config", headers=_headers(admin))
    assert get_resp.status_code == 200, get_resp.text
    get_data = get_resp.json()["data"]
    assert get_data["embedding"] == {"provider": "qwen", "model": "text-embedding-v4"}
    assert get_data["rerank"] is None


@pytest.mark.asyncio
async def test_put_emits_audit_without_secret(
    admin_client: tuple[AsyncClient, UUID],
) -> None:
    client, admin = admin_client
    resp = await client.put(
        "/v1/platform/embedding-config",
        headers=_headers(admin),
        json={"embedding_provider": "qwen", "embedding_model": "text-embedding-v4"},
    )
    assert resp.status_code == 200, resp.text

    store = client._transport.app.state.audit_logger._store  # type: ignore[attr-defined]
    entries = list(store._rows.values())
    matched = [e for e in entries if e.action.value == "platform_embedding_config:updated"]
    assert matched, "expected a PLATFORM_EMBEDDING_CONFIG_UPDATED audit row"
    serialized = matched[0].model_dump_json()
    assert "secret://" not in serialized


@pytest.mark.asyncio
async def test_put_embedding_provider_key_missing(
    admin_client: tuple[AsyncClient, UUID],
) -> None:
    client, admin = admin_client
    resp = await client.put(
        "/v1/platform/embedding-config",
        headers=_headers(admin),
        json={"embedding_provider": "openai", "embedding_model": "text-embedding-3-large"},
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"]["code"] == "EMBEDDING_PROVIDER_KEY_MISSING"


@pytest.mark.asyncio
async def test_put_invalid_embedding_model(
    admin_client: tuple[AsyncClient, UUID],
) -> None:
    client, admin = admin_client
    # qwen3.7-max is a chat model (not an embedding model).
    resp = await client.put(
        "/v1/platform/embedding-config",
        headers=_headers(admin),
        json={"embedding_provider": "qwen", "embedding_model": "qwen3.7-max"},
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"]["code"] == "INVALID_EMBEDDING_MODEL"


@pytest.mark.asyncio
async def test_put_embedding_and_rerank(
    admin_client: tuple[AsyncClient, UUID],
) -> None:
    client, admin = admin_client
    resp = await client.put(
        "/v1/platform/embedding-config",
        headers=_headers(admin),
        json={
            "embedding_provider": "qwen",
            "embedding_model": "text-embedding-v4",
            "rerank_provider": "qwen",
            "rerank_model": "qwen3-vl-rerank",
        },
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["embedding"] == {"provider": "qwen", "model": "text-embedding-v4"}
    assert data["rerank"] == {"provider": "qwen", "model": "qwen3-vl-rerank"}


@pytest.mark.asyncio
async def test_put_invalid_rerank_pair(
    admin_client: tuple[AsyncClient, UUID],
) -> None:
    client, admin = admin_client
    resp = await client.put(
        "/v1/platform/embedding-config",
        headers=_headers(admin),
        json={
            "embedding_provider": "qwen",
            "embedding_model": "text-embedding-v4",
            "rerank_provider": "qwen",
        },
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"]["code"] == "INVALID_RERANK_PAIR"


@pytest.mark.asyncio
async def test_put_non_admin_forbidden(
    admin_client: tuple[AsyncClient, UUID],
) -> None:
    client, _ = admin_client
    resp = await client.put(
        "/v1/platform/embedding-config",
        headers=_headers(uuid4()),
        json={"embedding_provider": "qwen", "embedding_model": "text-embedding-v4"},
    )
    assert resp.status_code == 403
    assert resp.json()["detail"]["code"] == "PLATFORM_SCOPE_FORBIDDEN"


@pytest.mark.asyncio
async def test_status_configured_true(
    admin_client: tuple[AsyncClient, UUID],
) -> None:
    client, admin = admin_client
    resp = await client.get("/v1/platform/embedding-config/status", headers=_headers(admin))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["success"] is True
    assert body["error"] is None
    assert body["data"] == {"configured": True}


@pytest.mark.asyncio
async def test_status_configured_false(
    settings: Settings,
    lifecycle: Lifecycle,
    jwt_verifier: JWTVerifier,
) -> None:
    # No configured providers (no credentials, none supported) → the env
    # embedding fallback resolves to None → status reports configured: False.
    settings = settings.model_copy(
        update={
            "supported_providers": [],
            "platform_provider_credentials": {},
        }
    )
    app = create_app(settings=settings, lifecycle=lifecycle, jwt_verifier=jwt_verifier)
    admin = await _seed_admin(app)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://control-plane.test") as client:
        resp = await client.get("/v1/platform/embedding-config/status", headers=_headers(admin))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["success"] is True
    assert body["error"] is None
    assert body["data"] == {"configured": False}


@pytest.mark.asyncio
async def test_status_non_admin_allowed(
    admin_client: tuple[AsyncClient, UUID],
) -> None:
    # A non-system-admin principal (not seeded) can read the boolean status,
    # unlike the full GET which 403s for the same principal.
    client, _ = admin_client
    resp = await client.get("/v1/platform/embedding-config/status", headers=_headers(uuid4()))
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"] == {"configured": True}


# ─── PR-E3b — invalidation-bus broadcast on the write endpoint ─────────────


class _SpyBus:
    def __init__(self) -> None:
        self.events: list[object] = []

    async def publish(self, event: object) -> None:
        self.events.append(event)

    def publish_soon(self, event: object) -> None:
        self.events.append(event)


@pytest.mark.asyncio
async def test_put_broadcasts_platform_embedding(
    settings: Settings,
    lifecycle: Lifecycle,
    jwt_verifier: JWTVerifier,
) -> None:
    """``put`` self-invalidates THIS pod's cache; the endpoint must also
    broadcast ``platform_embedding`` so peer replicas drop theirs."""
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
            "/v1/platform/embedding-config",
            headers=_headers(admin),
            json={"embedding_provider": "qwen", "embedding_model": "text-embedding-v4"},
        )
    assert resp.status_code == 200, resp.text
    assert len(spy_bus.events) == 1
    assert spy_bus.events[0].kind == "platform_embedding"  # type: ignore[attr-defined]
