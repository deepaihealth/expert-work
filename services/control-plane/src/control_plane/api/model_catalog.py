"""``GET /v1/model-catalog`` — selectable models per usable provider (Stream S,
Mini-ADR S-4).

"Usable" = the provider has a configured + enabled platform credential, so the
agent build can actually resolve a key. The visual editor's model dropdown lists
exactly these providers + their (non-deprecated) models with capability flags.
Read-only.
"""

from __future__ import annotations

from typing import Annotated, Protocol

from fastapi import APIRouter, Depends, Request

from control_plane.api._authz import console_only
from control_plane.platform_secrets import PlatformSecretsService
from expert_work.protocol import models_for_provider
from expert_work.protocol.provider_catalog import PROVIDER_CATALOG


class ConfiguredProviders(Protocol):
    """Minimal interface: return the set of usable provider names."""

    async def configured_enabled_providers(self) -> set[str]:
        """Return the set of usable provider names."""


class PlatformConfiguredProviders:
    """Adapter wrapping :class:`PlatformSecretsService` for model-catalog use.

    ``PlatformSecretsService.effective_provider_credentials()`` already merges
    env-seed + DB overlay and suppresses disabled entries, so the key-set it
    returns is exactly the "configured + enabled" providers (Stream P P-12).
    """

    def __init__(self, service: PlatformSecretsService) -> None:
        self._service = service

    async def configured_enabled_providers(self) -> set[str]:
        creds = await self._service.effective_provider_credentials()
        return set(creds.keys())


def _get_providers(request: Request) -> ConfiguredProviders:
    return request.app.state.model_catalog_providers  # type: ignore[no-any-return]


def build_model_catalog_router() -> APIRouter:
    router = APIRouter(
        prefix="/v1/model-catalog", tags=["agents"], dependencies=[Depends(console_only())]
    )

    @router.get("")
    async def get_model_catalog(
        providers: Annotated[ConfiguredProviders, Depends(_get_providers)],
    ) -> dict[str, object]:
        usable = await providers.configured_enabled_providers()
        rows = [
            {
                "provider": p,
                # B-209: a layout-parsing recognition model (glm-ocr) is no agent model.
                # B-206 K-7c: embedding / rerank rows stay (the rate-card page lists
                # them) and say they are not chat models — the agent pickers filter.
                "models": [
                    m.model_dump(mode="json") | {"chat_model": m.chat_model}
                    for m in models_for_provider(p)
                    if not m.ocr_only
                ],
            }
            for p in PROVIDER_CATALOG
            if p in usable and models_for_provider(p)
        ]
        return {"success": True, "data": {"providers": rows}, "error": None}

    return router
