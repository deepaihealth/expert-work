"""GET /v1/model-catalog — Stream S PR B (Mini-ADR S-4)."""

from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from control_plane.api.model_catalog import build_model_catalog_router
from control_plane.audit import build_default_audit_logger
from control_plane.platform_secrets import PlatformSecretsService
from control_plane.settings import Settings
from expert_work.persistence.audit_log import InMemoryAuditLogStore
from expert_work.persistence.platform_secrets import InMemoryPlatformSecretStore
from expert_work.protocol import Principal


class _FakeProviders:
    def __init__(self, enabled: set[str]) -> None:
        self._enabled = enabled

    async def configured_enabled_providers(self) -> set[str]:
        return self._enabled


def _client(enabled: set[str]) -> TestClient:
    app = FastAPI()
    app.state.model_catalog_providers = _FakeProviders(enabled)
    # 阶段 1.2 — the router now carries ``console_only()``. That dependency
    # resolves the principal (401 when nothing did) and writes an audit row on
    # denial, neither of which a bare ``FastAPI()`` provides; the real app has
    # ``AuthMiddleware`` upstream. Stand in for both. An employee principal is
    # what the console model picker actually presents.
    app.state.audit_logger = build_default_audit_logger(InMemoryAuditLogStore())

    @app.middleware("http")
    async def _stub_principal(request: Request, call_next):  # type: ignore[no-untyped-def]
        request.state.principal = Principal(
            subject_id="picker@test",
            subject_type="user",
            tenant_id=uuid4(),
            roles=("viewer",),
        )
        return await call_next(request)

    app.include_router(build_model_catalog_router())
    return TestClient(app)


def test_lists_only_configured_enabled_providers_with_models() -> None:
    resp = _client({"deepseek"}).get("/v1/model-catalog")
    assert resp.status_code == 200
    data = resp.json()["data"]
    provs = {row["provider"] for row in data["providers"]}
    assert provs == {"deepseek"}
    ds = next(r for r in data["providers"] if r["provider"] == "deepseek")
    names = {m["name"]: m for m in ds["models"]}
    # Serves the current versioned models (deprecated legacy aliases excluded).
    assert "deepseek-v4-flash" in names
    assert names["deepseek-v4-flash"]["vision"] is False
    assert "deepseek-chat" not in names


def test_empty_when_no_provider_configured() -> None:
    resp = _client(set()).get("/v1/model-catalog")
    assert resp.json()["data"]["providers"] == []


# ---------------------------------------------------------------------------
# PlatformConfiguredProviders adapter — integration against InMemoryStore
# ---------------------------------------------------------------------------


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "env": "dev",
        "auth_mode": "dev",
        "db_dsn": "postgresql+asyncpg://test@localhost/test",
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_platform_configured_providers_db_enabled() -> None:
    """PlatformConfiguredProviders returns DB-enabled providers."""
    from control_plane.api.model_catalog import PlatformConfiguredProviders

    store = InMemoryPlatformSecretStore()
    await store.upsert_provider(
        provider="anthropic", secret_ref="kms://test/anthropic", enabled=True, actor_id="a"
    )
    svc = PlatformSecretsService(store=store, settings=_settings())
    adapter = PlatformConfiguredProviders(svc)
    result = await adapter.configured_enabled_providers()
    assert "anthropic" in result


@pytest.mark.asyncio
async def test_platform_configured_providers_disabled_excluded() -> None:
    """PlatformConfiguredProviders excludes disabled DB rows."""
    from control_plane.api.model_catalog import PlatformConfiguredProviders

    store = InMemoryPlatformSecretStore()
    await store.upsert_provider(
        provider="openai", secret_ref="kms://test/openai", enabled=False, actor_id="a"
    )
    svc = PlatformSecretsService(store=store, settings=_settings())
    adapter = PlatformConfiguredProviders(svc)
    result = await adapter.configured_enabled_providers()
    assert "openai" not in result


@pytest.mark.asyncio
async def test_platform_configured_providers_env_seed() -> None:
    """PlatformConfiguredProviders includes env-seeded providers."""
    from control_plane.api.model_catalog import PlatformConfiguredProviders

    settings = _settings(
        supported_providers=["deepseek"],
        platform_provider_credentials={"deepseek": "secret://env-deepseek"},
    )
    store = InMemoryPlatformSecretStore()
    svc = PlatformSecretsService(store=store, settings=settings)
    adapter = PlatformConfiguredProviders(svc)
    result = await adapter.configured_enabled_providers()
    assert "deepseek" in result
