"""A credentials resolver over fixed platform credentials, for tests.

Production code resolves a provider credential as a
:class:`~orchestrator.llm.endpoints.ProviderKey` (the secret ref together with
the request address saved on that credential — B-308) through
:class:`~control_plane.tenant_secret_overlay.TenantOverlayCredentialsResolver`.
The base ``CredentialsResolver`` only knows refs, so a test that drives that
code builds its resolver here: same keyword arguments as the base class, the
same error contract for a provider that is not configured.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from uuid import UUID

from control_plane.tenant_secret_overlay import TenantOverlayCredentialsResolver
from expert_work.protocol import Provider, Tool
from orchestrator.llm.endpoints import ProviderKey


def static_credentials_resolver(
    *,
    platform_provider_credentials: Mapping[Any, str | ProviderKey | list[ProviderKey]],
    platform_tool_credentials: Mapping[Any, str],
    tenant_config_getter: Any,
    **resolver_kwargs: Any,
) -> TenantOverlayCredentialsResolver:
    """An overlay resolver whose every tenant sees ``platform_provider_credentials``.

    A value may be a secret ref (official address), one :class:`ProviderKey`
    (a key with a request address), or a list of them (multi-key failover order).
    """
    key_lists: dict[Provider, list[ProviderKey]] = {}
    for provider, value in platform_provider_credentials.items():
        if isinstance(value, str):
            key_lists[provider] = [ProviderKey(value)]
        elif isinstance(value, ProviderKey):
            key_lists[provider] = [value]
        else:
            key_lists[provider] = list(value)

    async def _best(tenant_id: UUID) -> dict[Provider, ProviderKey]:
        return {provider: keys[0] for provider, keys in key_lists.items() if keys}

    async def _lists(tenant_id: UUID) -> dict[Provider, list[ProviderKey]]:
        return {provider: list(keys) for provider, keys in key_lists.items()}

    async def _tools(tenant_id: UUID) -> dict[Tool, str]:
        return dict(platform_tool_credentials)

    return TenantOverlayCredentialsResolver(
        tenant_provider_key_view=_best,
        tenant_tool_view=_tools,
        tenant_provider_key_list_view=_lists,
        platform_provider_credentials={
            provider: keys[0].secret_ref for provider, keys in key_lists.items() if keys
        },
        platform_tool_credentials=dict(platform_tool_credentials),
        tenant_config_getter=tenant_config_getter,
        **resolver_kwargs,
    )
