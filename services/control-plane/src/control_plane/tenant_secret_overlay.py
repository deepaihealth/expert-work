"""Tenant-aware credentials resolution — Stream HX-8 (Mini-ADR HX-H3).

:class:`TenantOverlayCredentialsResolver` subclasses the shared
``CredentialsResolver`` and swaps the *platform-global* effective view for
the *tenant-effective* one (``PlatformSecretsService.effective_*_for``):
tenant override rows win, disabled rows suppress the key for that tenant
(Mini-ADR HX-H2), tenants without rows see the unchanged platform view —
so every resolve call keeps its exact platform-mode error contract.

Design note (revises §9.2.3 of STREAM-HX-DESIGN): the design sketched new
optional getter kwargs on ``CredentialsResolver`` itself, but the
``expert-work-common/credentials`` path is harness-blocked for edits. A subclass
in control-plane is semantically equivalent — the merge logic still lives
in the service layer and expert-work-common stays untouched (arguably a closer
fit to HX-H3's "resolver stays a pure resolver").

B-308 — a provider credential now carries a request address. The resolver hands
out the two together (:class:`~orchestrator.llm.endpoints.ProviderKey`) through
:meth:`TenantOverlayCredentialsResolver.resolve_provider_key` /
:meth:`~TenantOverlayCredentialsResolver.resolve_provider_key_list`, so whoever
holds the resolver gets the address with the key. The older ref-only methods
keep working for keys at the official address and **refuse** a key that has a
custom address (:class:`ProviderKeyNeedsEndpointError`): a caller that was not
moved over fails loudly instead of quietly sending that key to the official host.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Protocol
from uuid import UUID

from expert_work.common.credentials import CredentialsResolver, CredentialsResolverError
from expert_work.protocol import Provider, Tool
from orchestrator.llm.endpoints import ProviderKey

TenantToolViewGetter = Callable[[UUID], Awaitable[dict[Tool, str]]]
#: B-308 — tenant-effective provider → the one best key (with its address).
TenantProviderKeyViewGetter = Callable[[UUID], Awaitable[dict[Provider, ProviderKey]]]
#: Stream Y-MK / B-308 — tenant-effective per-provider ordered key list.
TenantProviderKeyListViewGetter = Callable[[UUID], Awaitable[dict[Provider, list[ProviderKey]]]]


class ProviderKeyNeedsEndpointError(RuntimeError):
    """A ref-only resolve hit a key that has a custom request address.

    Deliberately **not** a :class:`CredentialsResolverError`: that one is caught
    in several places and read as "no credential configured" (the call is then
    skipped or degraded). This is a wiring bug — the caller must resolve a
    :class:`ProviderKey` — and has to surface as one.
    """

    def __init__(self, provider: str) -> None:
        super().__init__(
            f"the credential for provider {provider!r} has a custom request address; "
            "the caller must use resolve_provider_key / resolve_provider_key_list"
        )
        self.provider = provider


class ProviderKeySource(Protocol):
    """What a caller that sends a provider key somewhere needs from a resolver."""

    async def resolve_provider_key(self, *, tenant_id: UUID, provider: Provider) -> ProviderKey:
        """The best key for ``provider`` with the request address saved on it."""


class ProviderKeyListSource(Protocol):
    """What the agent build needs: every key to try for a provider, in order."""

    async def resolve_provider_key_list(
        self, *, tenant_id: UUID, provider: Provider
    ) -> list[ProviderKey]:
        """Every key for ``provider``, in the order to try them, each with its address."""


class TenantOverlayCredentialsResolver(CredentialsResolver):
    """``CredentialsResolver`` that resolves through the tenant-effective view."""

    def __init__(
        self,
        *,
        tenant_provider_key_view: TenantProviderKeyViewGetter,
        tenant_tool_view: TenantToolViewGetter,
        tenant_provider_key_list_view: TenantProviderKeyListViewGetter | None = None,
        **resolver_kwargs: Any,
    ) -> None:
        super().__init__(**resolver_kwargs)
        self._tenant_provider_key_view = tenant_provider_key_view
        self._tenant_tool_view = tenant_tool_view
        self._tenant_provider_key_list_view = tenant_provider_key_list_view

    async def resolve_provider_key(self, *, tenant_id: UUID, provider: Provider) -> ProviderKey:
        """B-308 — the best key for ``provider`` with the request address saved
        on that very row (``None`` = official).

        Tenant-existence validation mirrors the base class (raises on unknown
        id). The tenant view is the FINAL merged view — platform fallback
        included — so a miss here is a genuine "not configured for this tenant"
        (override-suppressed or platform-unset alike)."""
        await self._tenant_config.get(tenant_id=tenant_id)
        key = (await self._tenant_provider_key_view(tenant_id)).get(provider)
        if key is None:
            msg = (
                f"platform credentials missing for provider={provider} "
                f"(tenant-effective view). Configure the platform credential "
                "or remove the tenant's disabled override."
            )
            raise CredentialsResolverError(msg, mode="platform", kind="provider", key=provider)
        return key

    async def resolve_provider_key_list(
        self, *, tenant_id: UUID, provider: Provider
    ) -> list[ProviderKey]:
        """Stream Y-MK / B-308 — the ordered keys to try for ``provider``
        (priority asc), each with its own request address. A tenant override is
        a single key replacing the list; a disabled override / unset platform
        credential yields a miss, raised as a :class:`CredentialsResolverError`
        with the same contract as :meth:`resolve_provider_key`. Falls back to
        the single resolve (wrapped as a 1-key list) when no list view was
        injected."""
        await self._tenant_config.get(tenant_id=tenant_id)
        if self._tenant_provider_key_list_view is None:
            return [await self.resolve_provider_key(tenant_id=tenant_id, provider=provider)]
        keys = (await self._tenant_provider_key_list_view(tenant_id)).get(provider)
        if not keys:
            msg = (
                f"platform credentials missing for provider={provider} "
                f"(tenant-effective key list). Configure a platform credential "
                "or remove the tenant's disabled override."
            )
            raise CredentialsResolverError(msg, mode="platform", kind="provider", key=provider)
        return list(keys)

    async def resolve_provider(self, *, tenant_id: UUID, provider: Provider) -> str:
        """Ref-only resolve, kept for the base-class contract. Refuses a key
        that has a custom request address (see
        :class:`ProviderKeyNeedsEndpointError`)."""
        key = await self.resolve_provider_key(tenant_id=tenant_id, provider=provider)
        if key.base_url is not None:
            raise ProviderKeyNeedsEndpointError(provider)
        return key.secret_ref

    async def resolve_provider_keys(self, *, tenant_id: UUID, provider: Provider) -> list[str]:
        """Ref-only list resolve (Stream Y-MK), kept for the same reason and
        with the same refusal as :meth:`resolve_provider`."""
        keys = await self.resolve_provider_key_list(tenant_id=tenant_id, provider=provider)
        if any(key.base_url is not None for key in keys):
            raise ProviderKeyNeedsEndpointError(provider)
        return [key.secret_ref for key in keys]

    async def resolve_tool(self, *, tenant_id: UUID, tool: Tool) -> str:
        await self._tenant_config.get(tenant_id=tenant_id)
        secret_ref = (await self._tenant_tool_view(tenant_id)).get(tool)
        if secret_ref is None:
            msg = (
                f"platform credentials missing for tool={tool} "
                f"(tenant-effective view). Configure the platform credential "
                "or remove the tenant's disabled override."
            )
            raise CredentialsResolverError(msg, mode="platform", kind="tool", key=tool)
        return secret_ref
