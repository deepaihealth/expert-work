"""B-308 §4.9 — the platform's embedders call the configured provider's own
embeddings address (they used to call one fixed vendor whatever was configured)."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import httpx
import pytest

from control_plane.runtime import DynamicResolvingEmbedder, ResolvingEmbedder
from control_plane.tenant_secret_overlay import TenantOverlayCredentialsResolver
from expert_work.protocol import Provider, TenantConfigRecord, TenantPlan
from tests.credential_fixtures import static_credentials_resolver

_OFFICIAL_EMBEDDINGS = {
    "openai": "https://api.openai.com/v1/embeddings",
    "glm": "https://open.bigmodel.cn/api/paas/v4/embeddings",
    "qwen": "https://dashscope.aliyuncs.com/compatible-mode/v1/embeddings",
}


class _TenantConfig:
    async def get(self, *, tenant_id: UUID) -> TenantConfigRecord:
        now = datetime.now(UTC)
        return TenantConfigRecord(
            tenant_id=tenant_id,
            display_name="Acme",
            plan=TenantPlan.FREE,
            credentials_mode="platform",
            created_at=now,
            updated_at=now,
            updated_by="tester",
        )


class _SecretStore:
    async def get(self, name: str, *, version: str | None = None) -> str:
        return "sk-test"


def _resolver(provider: str) -> TenantOverlayCredentialsResolver:
    return static_credentials_resolver(
        platform_provider_credentials={provider: f"secret://platform/{provider}"},
        platform_tool_credentials={},
        tenant_config_getter=_TenantConfig(),
    )


def _recording_http(seen: list[str]) -> httpx.AsyncClient:
    def _handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1.0]}]})

    return httpx.AsyncClient(transport=httpx.MockTransport(_handler))


@pytest.mark.asyncio
@pytest.mark.parametrize(("provider", "expected"), sorted(_OFFICIAL_EMBEDDINGS.items()))
async def test_resolving_embedder_calls_the_configured_provider(
    provider: Provider, expected: str
) -> None:
    seen: list[str] = []
    async with _recording_http(seen) as http:
        embedder = ResolvingEmbedder(
            resolver=_resolver(provider),
            secret_store=_SecretStore(),  # type: ignore[arg-type]
            provider=provider,
            model="m",
            http=http,
        )
        await embedder.embed(["hi"], tenant_id=uuid4())

    assert seen == [expected]


class _EmbeddingConfig:
    def __init__(self, provider: str) -> None:
        self._provider = provider

    async def effective_embedding_config(self) -> tuple[str, str]:
        return (self._provider, "m")


@pytest.mark.asyncio
@pytest.mark.parametrize(("provider", "expected"), sorted(_OFFICIAL_EMBEDDINGS.items()))
async def test_dynamic_embedder_calls_the_configured_provider(provider: str, expected: str) -> None:
    seen: list[str] = []
    async with _recording_http(seen) as http:
        embedder = DynamicResolvingEmbedder(
            config_service=_EmbeddingConfig(provider),  # type: ignore[arg-type]
            resolver=_resolver(provider),
            secret_store=_SecretStore(),  # type: ignore[arg-type]
            http=http,
        )
        await embedder.embed(["hi"], tenant_id=uuid4())

    assert seen == [expected]
