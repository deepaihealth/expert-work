"""Tests for :class:`PlatformSecretsService` merge logic — Stream P (P-7/P-9).

Stream HX-8 adds the tenant-effective view tests (per-tenant override rows
on top of the platform merge; disabled rows suppress — Mini-ADR HX-H2).
"""

from __future__ import annotations

from uuid import UUID

import pytest

from control_plane.platform_secrets import PlatformSecretsService
from control_plane.settings import Settings
from expert_work.persistence.platform_secrets import InMemoryPlatformSecretStore
from expert_work.protocol import Provider
from orchestrator.llm.endpoints import ProviderKey

_TENANT_A = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
_TENANT_B = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")


def _refs(keys: dict[Provider, list[ProviderKey]]) -> dict[str, list[str]]:
    return {provider: [key.secret_ref for key in listed] for provider, listed in keys.items()}


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "env": "dev",
        "auth_mode": "dev",
        "db_dsn": "postgresql+asyncpg://test@localhost/test",
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_db_enabled_row_appears_in_merged_view() -> None:
    store = InMemoryPlatformSecretStore()
    await store.upsert_provider(
        provider="anthropic", secret_ref="kms://db/anthropic", enabled=True, actor_id="a"
    )
    svc = PlatformSecretsService(store=store, settings=_settings())

    merged = await svc.effective_provider_credentials()
    assert merged.get("anthropic") == "kms://db/anthropic"


@pytest.mark.asyncio
async def test_db_overrides_env_per_key() -> None:
    settings = _settings(
        supported_providers=["openai"],
        platform_provider_credentials={"openai": "secret://env-openai"},
    )
    store = InMemoryPlatformSecretStore()
    svc = PlatformSecretsService(store=store, settings=settings)
    # Env seed visible first.
    assert (await svc.effective_provider_credentials()).get("openai") == "secret://env-openai"

    # A DB row overrides the env ref; invalidate to bypass the TTL cache.
    await store.upsert_provider(
        provider="openai", secret_ref="kms://db-openai", enabled=True, actor_id="a"
    )
    svc.invalidate()
    assert (await svc.effective_provider_credentials()).get("openai") == "kms://db-openai"


@pytest.mark.asyncio
async def test_disabled_db_row_suppresses_env_seed() -> None:
    settings = _settings(
        supported_providers=["openai"],
        platform_provider_credentials={"openai": "secret://env-openai"},
    )
    store = InMemoryPlatformSecretStore()
    await store.upsert_provider(
        provider="openai", secret_ref="secret://ignored", enabled=False, actor_id="a"
    )
    svc = PlatformSecretsService(store=store, settings=settings)
    # Disabled DB row wins → provider suppressed even though env seeds it (P-12).
    assert "openai" not in await svc.effective_provider_credentials()


@pytest.mark.asyncio
async def test_tool_merge_independent() -> None:
    store = InMemoryPlatformSecretStore()
    await store.upsert_tool(
        tool="web_search", secret_ref="kms://db/tavily", enabled=True, actor_id="a"
    )
    svc = PlatformSecretsService(store=store, settings=_settings())
    assert (await svc.effective_tool_credentials()).get("web_search") == "kms://db/tavily"
    assert await svc.effective_provider_credentials() == {}


# ─── tenant-effective view (Stream HX-8) ───────────────────────────────


@pytest.mark.asyncio
async def test_tenant_override_wins_over_platform_row() -> None:
    store = InMemoryPlatformSecretStore()
    await store.upsert_provider(
        provider="anthropic", secret_ref="kms://platform/anthropic", enabled=True, actor_id="a"
    )
    await store.upsert_tenant_provider(
        tenant_id=_TENANT_A,
        provider="anthropic",
        secret_ref="kms://tenant-a/anthropic",
        enabled=True,
        actor_id="a",
    )
    svc = PlatformSecretsService(store=store, settings=_settings())

    view_a = await svc.effective_provider_credentials_for(_TENANT_A)
    assert view_a.get("anthropic") == "kms://tenant-a/anthropic"
    # Another tenant (no rows) falls through to the platform view untouched.
    view_b = await svc.effective_provider_credentials_for(_TENANT_B)
    assert view_b.get("anthropic") == "kms://platform/anthropic"
    # The platform-global view never sees tenant rows.
    assert (await svc.effective_provider_credentials()).get(
        "anthropic"
    ) == "kms://platform/anthropic"


@pytest.mark.asyncio
async def test_disabled_tenant_row_suppresses_without_fallback() -> None:
    store = InMemoryPlatformSecretStore()
    await store.upsert_provider(
        provider="openai", secret_ref="kms://platform/openai", enabled=True, actor_id="a"
    )
    await store.upsert_tenant_provider(
        tenant_id=_TENANT_A,
        provider="openai",
        secret_ref="kms://tenant-a/openai",
        enabled=False,
        actor_id="a",
    )
    svc = PlatformSecretsService(store=store, settings=_settings())

    # HX-H2: disabled row = suppress for the tenant, NOT fall back.
    assert "openai" not in await svc.effective_provider_credentials_for(_TENANT_A)
    assert (await svc.effective_provider_credentials_for(_TENANT_B)).get(
        "openai"
    ) == "kms://platform/openai"


@pytest.mark.asyncio
async def test_tenant_tool_override_and_invalidate() -> None:
    store = InMemoryPlatformSecretStore()
    svc = PlatformSecretsService(store=store, settings=_settings())
    assert await svc.effective_tool_credentials_for(_TENANT_A) == {}

    await store.upsert_tenant_tool(
        tenant_id=_TENANT_A,
        tool="web_search",
        secret_ref="kms://tenant-a/tavily",
        enabled=True,
        actor_id="a",
    )
    # TTL cache still holds the old view until invalidated.
    assert await svc.effective_tool_credentials_for(_TENANT_A) == {}
    svc.invalidate()
    assert (await svc.effective_tool_credentials_for(_TENANT_A)).get(
        "web_search"
    ) == "kms://tenant-a/tavily"


# ---------------------------------------------------------------------------
# Stream Y-MK — per-provider multi-key effective views
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mk_refs_aggregates_multiple_keys_sorted_by_priority() -> None:
    store = InMemoryPlatformSecretStore()
    await store.upsert_provider(
        provider="deepseek",
        key_id="b",
        secret_ref="kms://b",
        enabled=True,
        priority=20,
        actor_id="a",
    )
    await store.upsert_provider(
        provider="deepseek",
        key_id="a",
        secret_ref="kms://a",
        enabled=True,
        priority=10,
        actor_id="a",
    )
    svc = PlatformSecretsService(store=store, settings=_settings())

    refs = _refs(await svc.effective_provider_keys())
    assert refs["deepseek"] == ["kms://a", "kms://b"]  # priority 10 before 20
    # Single view = best (first) key, preserved for embed/rerank callers.
    assert (await svc.effective_provider_credentials())["deepseek"] == "kms://a"


@pytest.mark.asyncio
async def test_mk_refs_excludes_disabled_keys() -> None:
    store = InMemoryPlatformSecretStore()
    await store.upsert_provider(
        provider="openai",
        key_id="live",
        secret_ref="kms://live",
        enabled=True,
        priority=10,
        actor_id="a",
    )
    await store.upsert_provider(
        provider="openai",
        key_id="dead",
        secret_ref="kms://dead",
        enabled=False,
        priority=5,
        actor_id="a",
    )
    svc = PlatformSecretsService(store=store, settings=_settings())

    refs = _refs(await svc.effective_provider_keys())
    assert refs["openai"] == ["kms://live"]  # disabled excluded despite lower priority


@pytest.mark.asyncio
async def test_mk_refs_all_disabled_suppresses_provider() -> None:
    settings = _settings(
        supported_providers=["openai"],
        platform_provider_credentials={"openai": "secret://env-openai"},
    )
    store = InMemoryPlatformSecretStore()
    await store.upsert_provider(
        provider="openai",
        key_id="default",
        secret_ref="kms://db",
        enabled=False,
        actor_id="a",
    )
    svc = PlatformSecretsService(store=store, settings=settings)

    refs = _refs(await svc.effective_provider_keys())
    assert "openai" not in refs  # all DB keys disabled → suppressed even with env seed


@pytest.mark.asyncio
async def test_mk_refs_env_seed_when_no_db_rows() -> None:
    settings = _settings(
        supported_providers=["openai"],
        platform_provider_credentials={"openai": "secret://env-openai"},
    )
    store = InMemoryPlatformSecretStore()
    svc = PlatformSecretsService(store=store, settings=settings)

    refs = _refs(await svc.effective_provider_keys())
    assert refs["openai"] == ["secret://env-openai"]


@pytest.mark.asyncio
async def test_mk_refs_for_tenant_override_replaces_list() -> None:
    store = InMemoryPlatformSecretStore()
    await store.upsert_provider(
        provider="openai",
        key_id="a",
        secret_ref="kms://plat-a",
        enabled=True,
        priority=10,
        actor_id="a",
    )
    await store.upsert_provider(
        provider="openai",
        key_id="b",
        secret_ref="kms://plat-b",
        enabled=True,
        priority=20,
        actor_id="a",
    )
    await store.upsert_tenant_provider(
        tenant_id=_TENANT_A,
        provider="openai",
        secret_ref="kms://tenant-a",
        enabled=True,
        actor_id="a",
    )
    svc = PlatformSecretsService(store=store, settings=_settings())

    # Tenant A: single override replaces the platform key list.
    assert _refs(await svc.effective_provider_keys_for(_TENANT_A))["openai"] == ["kms://tenant-a"]
    # Tenant B: no override → full platform list.
    assert _refs(await svc.effective_provider_keys_for(_TENANT_B))["openai"] == [
        "kms://plat-a",
        "kms://plat-b",
    ]


# ---------------------------------------------------------------------------
# B-308 — the request address travels with the selected row
# ---------------------------------------------------------------------------

_CUSTOM = "https://proxy.example.com/v1"


async def _two_platform_keys(store: InMemoryPlatformSecretStore) -> None:
    await store.upsert_provider(
        provider="openai",
        key_id="proxy",
        secret_ref="secret://plat-proxy",
        enabled=True,
        priority=10,
        actor_id="a",
        base_url=_CUSTOM,
    )
    await store.upsert_provider(
        provider="openai",
        key_id="direct",
        secret_ref="secret://plat-direct",
        enabled=True,
        priority=20,
        actor_id="a",
    )


@pytest.mark.asyncio
async def test_each_platform_key_carries_its_own_address() -> None:
    store = InMemoryPlatformSecretStore()
    await _two_platform_keys(store)
    svc = PlatformSecretsService(store=store, settings=_settings())

    assert (await svc.effective_provider_keys())["openai"] == [
        ProviderKey("secret://plat-proxy", _CUSTOM),
        ProviderKey("secret://plat-direct", None),
    ]
    best = await svc.effective_best_provider_keys_for(_TENANT_A)
    assert best["openai"] == ProviderKey("secret://plat-proxy", _CUSTOM)
    # The ref-only view is unchanged: "is this provider configured".
    assert (await svc.effective_provider_credentials())["openai"] == "secret://plat-proxy"


@pytest.mark.asyncio
async def test_tenant_override_with_an_address_uses_it() -> None:
    store = InMemoryPlatformSecretStore()
    await _two_platform_keys(store)
    await store.upsert_tenant_provider(
        tenant_id=_TENANT_A,
        provider="openai",
        secret_ref="secret://tenant-a",
        enabled=True,
        actor_id="a",
        base_url="https://tenant-proxy.example.com/v1",
    )
    svc = PlatformSecretsService(store=store, settings=_settings())

    override = ProviderKey("secret://tenant-a", "https://tenant-proxy.example.com/v1")
    assert (await svc.effective_provider_keys_for(_TENANT_A))["openai"] == [override]
    assert (await svc.effective_best_provider_keys_for(_TENANT_A))["openai"] == override


@pytest.mark.asyncio
async def test_tenant_override_without_an_address_is_official_not_the_platform_keys() -> None:
    """Decision 6: an override that has no address goes to the official one —
    it does not inherit the address of the platform key it replaces."""
    store = InMemoryPlatformSecretStore()
    await _two_platform_keys(store)
    await store.upsert_tenant_provider(
        tenant_id=_TENANT_A,
        provider="openai",
        secret_ref="secret://tenant-a",
        enabled=True,
        actor_id="a",
    )
    svc = PlatformSecretsService(store=store, settings=_settings())

    assert (await svc.effective_provider_keys_for(_TENANT_A))["openai"] == [
        ProviderKey("secret://tenant-a", None)
    ]
    # Another tenant still sees the platform keys with their addresses.
    assert (await svc.effective_provider_keys_for(_TENANT_B))["openai"][0].base_url == _CUSTOM


@pytest.mark.asyncio
async def test_disabled_override_with_the_platform_ref_still_suppresses() -> None:
    """A disabled override that reuses the platform key's ref (how an admin
    turns a provider off for one tenant) keeps suppressing it."""
    store = InMemoryPlatformSecretStore()
    await _two_platform_keys(store)
    await store.upsert_tenant_provider(
        tenant_id=_TENANT_A,
        provider="openai",
        secret_ref="secret://plat-proxy",
        enabled=False,
        actor_id="a",
    )
    svc = PlatformSecretsService(store=store, settings=_settings())

    assert "openai" not in await svc.effective_provider_keys_for(_TENANT_A)
    assert "openai" not in await svc.effective_best_provider_keys_for(_TENANT_A)
    assert "openai" not in await svc.effective_provider_credentials_for(_TENANT_A)
    assert "openai" in await svc.effective_provider_keys_for(_TENANT_B)


@pytest.mark.asyncio
async def test_env_seeded_key_has_no_address() -> None:
    settings = _settings(
        supported_providers=["openai"],
        platform_provider_credentials={"openai": "secret://env-openai"},
    )
    svc = PlatformSecretsService(store=InMemoryPlatformSecretStore(), settings=settings)
    assert (await svc.effective_provider_keys())["openai"] == [ProviderKey("secret://env-openai")]


def test_old_ref_list_methods_are_gone() -> None:
    # Renamed, not aliased (B-308 §4.3): an unconverted caller fails at once.
    assert not hasattr(PlatformSecretsService, "effective_provider_secret_refs")
    assert not hasattr(PlatformSecretsService, "effective_provider_secret_refs_for")
