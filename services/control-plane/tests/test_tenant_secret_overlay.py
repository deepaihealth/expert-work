"""Tests for :class:`TenantOverlayCredentialsResolver` — Stream HX-8 (HX-H3), B-308."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest

from control_plane.tenant_secret_overlay import (
    ProviderKeyNeedsEndpointError,
    TenantOverlayCredentialsResolver,
)
from expert_work.common.credentials import CredentialsResolverError
from expert_work.protocol import Provider, TenantConfigRecord, TenantPlan, Tool
from orchestrator.llm.endpoints import ProviderKey

_NOW = datetime.now(UTC)
_TENANT = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
_CUSTOM = "https://proxy.example.com/v1"


class _FakeTenantConfigGetter:
    def __init__(self) -> None:
        self.calls: list[UUID] = []

    async def get(self, *, tenant_id: UUID) -> TenantConfigRecord:
        self.calls.append(tenant_id)
        return TenantConfigRecord(
            tenant_id=tenant_id,
            display_name="Test",
            plan=TenantPlan.FREE,
            credentials_mode="platform",
            created_at=_NOW,
            updated_at=_NOW,
            updated_by="tester",
        )


def _resolver(
    *,
    provider_view: dict[Provider, ProviderKey] | None = None,
    tool_view: dict[Tool, str] | None = None,
    keys_view: dict[Provider, list[ProviderKey]] | None = None,
    tenant_config: _FakeTenantConfigGetter | None = None,
) -> TenantOverlayCredentialsResolver:
    async def _providers(tenant_id: UUID) -> dict[Provider, ProviderKey]:
        return dict(provider_view or {})

    async def _tools(tenant_id: UUID) -> dict[Tool, str]:
        return dict(tool_view or {})

    async def _keys(tenant_id: UUID) -> dict[Provider, list[ProviderKey]]:
        return {k: list(v) for k, v in (keys_view or {}).items()}

    return TenantOverlayCredentialsResolver(
        tenant_provider_key_view=_providers,
        tenant_tool_view=_tools,
        tenant_provider_key_list_view=_keys if keys_view is not None else None,
        platform_provider_credentials={},
        platform_tool_credentials={},
        tenant_config_getter=tenant_config or _FakeTenantConfigGetter(),
    )


@pytest.mark.asyncio
async def test_resolves_provider_key_through_tenant_view() -> None:
    resolver = _resolver(provider_view={"anthropic": ProviderKey("kms://tenant/anthropic")})
    key = await resolver.resolve_provider_key(tenant_id=_TENANT, provider="anthropic")
    assert key == ProviderKey("kms://tenant/anthropic", None)


@pytest.mark.asyncio
async def test_provider_key_carries_the_rows_request_address() -> None:
    resolver = _resolver(provider_view={"openai": ProviderKey("secret://k", _CUSTOM)})
    key = await resolver.resolve_provider_key(tenant_id=_TENANT, provider="openai")
    assert key == ProviderKey("secret://k", _CUSTOM)


@pytest.mark.asyncio
async def test_ref_only_resolve_still_works_for_an_official_address_key() -> None:
    resolver = _resolver(provider_view={"anthropic": ProviderKey("kms://tenant/anthropic")})
    ref = await resolver.resolve_provider(tenant_id=_TENANT, provider="anthropic")
    assert ref == "kms://tenant/anthropic"


@pytest.mark.asyncio
async def test_ref_only_resolve_refuses_a_key_with_a_custom_address() -> None:
    """A caller that was not moved to ``resolve_provider_key`` must fail loudly,
    never quietly send this key to the official host — and not with the error
    type that callers read as "no credential configured"."""
    resolver = _resolver(provider_view={"openai": ProviderKey("secret://k", _CUSTOM)})
    with pytest.raises(ProviderKeyNeedsEndpointError) as exc_info:
        await resolver.resolve_provider(tenant_id=_TENANT, provider="openai")
    assert not isinstance(exc_info.value, CredentialsResolverError)
    assert "proxy.example.com" not in str(exc_info.value)


@pytest.mark.asyncio
async def test_ref_only_list_resolve_refuses_when_any_key_has_a_custom_address() -> None:
    resolver = _resolver(
        keys_view={"openai": [ProviderKey("secret://a"), ProviderKey("secret://b", _CUSTOM)]}
    )
    with pytest.raises(ProviderKeyNeedsEndpointError) as exc_info:
        await resolver.resolve_provider_keys(tenant_id=_TENANT, provider="openai")
    assert not isinstance(exc_info.value, CredentialsResolverError)


@pytest.mark.asyncio
async def test_resolves_tool_through_tenant_view() -> None:
    resolver = _resolver(tool_view={"web_search": "kms://tenant/tavily"})
    ref = await resolver.resolve_tool(tenant_id=_TENANT, tool="web_search")
    assert ref == "kms://tenant/tavily"


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["resolve_provider", "resolve_provider_key"])
async def test_missing_provider_keeps_platform_error_contract(method: str) -> None:
    resolver = _resolver(provider_view={})
    with pytest.raises(CredentialsResolverError) as exc_info:
        await getattr(resolver, method)(tenant_id=_TENANT, provider="anthropic")
    assert exc_info.value.mode == "platform"
    assert exc_info.value.kind == "provider"
    assert exc_info.value.key == "anthropic"


@pytest.mark.asyncio
async def test_missing_tool_keeps_platform_error_contract() -> None:
    resolver = _resolver(tool_view={})
    with pytest.raises(CredentialsResolverError) as exc_info:
        await resolver.resolve_tool(tenant_id=_TENANT, tool="web_search")
    assert exc_info.value.mode == "platform"
    assert exc_info.value.kind == "tool"
    assert exc_info.value.key == "web_search"


@pytest.mark.asyncio
async def test_tenant_existence_validated_before_view_lookup() -> None:
    tenant_config = _FakeTenantConfigGetter()
    resolver = _resolver(
        provider_view={"anthropic": ProviderKey("kms://tenant/anthropic")},
        tenant_config=tenant_config,
    )
    await resolver.resolve_provider_key(tenant_id=_TENANT, provider="anthropic")
    assert tenant_config.calls == [_TENANT]


# ---------------------------------------------------------------------------
# Stream Y-MK / B-308 — the ordered multi-key list
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_provider_key_list_returns_each_key_with_its_own_address() -> None:
    keys = [ProviderKey("kms://a", _CUSTOM), ProviderKey("kms://b")]
    resolver = _resolver(keys_view={"deepseek": keys})
    resolved = await resolver.resolve_provider_key_list(tenant_id=_TENANT, provider="deepseek")
    assert resolved == keys


@pytest.mark.asyncio
async def test_ref_only_list_resolve_returns_refs_for_official_keys() -> None:
    resolver = _resolver(keys_view={"deepseek": [ProviderKey("kms://a"), ProviderKey("kms://b")]})
    keys = await resolver.resolve_provider_keys(tenant_id=_TENANT, provider="deepseek")
    assert keys == ["kms://a", "kms://b"]


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["resolve_provider_keys", "resolve_provider_key_list"])
async def test_key_list_missing_raises(method: str) -> None:
    resolver = _resolver(keys_view={"openai": [ProviderKey("kms://x")]})
    with pytest.raises(CredentialsResolverError):
        await getattr(resolver, method)(tenant_id=_TENANT, provider="deepseek")


@pytest.mark.asyncio
async def test_key_list_falls_back_to_single_when_no_list_view() -> None:
    """No list view injected → wrap the single resolve as a 1-key list."""
    resolver = _resolver(provider_view={"anthropic": ProviderKey("kms://single", _CUSTOM)})
    keys = await resolver.resolve_provider_key_list(tenant_id=_TENANT, provider="anthropic")
    assert keys == [ProviderKey("kms://single", _CUSTOM)]


def test_old_constructor_names_are_gone() -> None:
    """Renamed on purpose (B-308 §4.3): a caller still passing a ref-only view
    fails at construction instead of resolving keys without their address."""

    async def _refs(tenant_id: UUID) -> dict[Provider, str]:
        return {}

    async def _tools(tenant_id: UUID) -> dict[Tool, str]:
        return {}

    with pytest.raises(TypeError):
        TenantOverlayCredentialsResolver(  # type: ignore[call-arg]
            tenant_provider_view=_refs,
            tenant_tool_view=_tools,
            platform_provider_credentials={},
            platform_tool_credentials={},
            tenant_config_getter=_FakeTenantConfigGetter(),
        )
