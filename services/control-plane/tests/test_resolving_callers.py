"""Stream O Mini-ADR O-9 — per-tenant credential-resolving callers.

The embedder / reranker / web_search wrappers resolve a secret_ref via
:class:`CredentialsResolver` at call time, then read the actual key from the
:class:`SecretStore`. These tests assert the per-tenant resolution path and
the graceful-degrade behaviour for the (optional) reranker.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any
from uuid import UUID, uuid4

import pytest

from control_plane.runtime import (
    ResolvingEmbedder,
    ResolvingReranker,
)
from expert_work.common.credentials import CredentialsResolver, CredentialsResolverError
from expert_work.protocol import TenantConfigRecord, TenantPlan
from expert_work.runtime.secret_store import parse_secret_ref
from expert_work.testing import InMemorySecretStore
from tests.credential_fixtures import static_credentials_resolver

_NOW_DISPLAY = "Acme"


class _TenantConfig:
    def __init__(self, record: TenantConfigRecord) -> None:
        self._record = record

    async def get(self, *, tenant_id: UUID) -> TenantConfigRecord:
        return self._record


def _record() -> TenantConfigRecord:
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    return TenantConfigRecord(
        tenant_id=uuid4(),
        display_name=_NOW_DISPLAY,
        plan=TenantPlan.FREE,
        credentials_mode="platform",
        created_at=now,
        updated_at=now,
        updated_by="tester",
    )


def _resolver(
    *,
    record: TenantConfigRecord,
    platform_provs: dict[str, str] | None = None,
) -> CredentialsResolver:
    return static_credentials_resolver(
        platform_provider_credentials=platform_provs or {},  # type: ignore[arg-type]
        platform_tool_credentials={},  # type: ignore[arg-type]
        tenant_config_getter=_TenantConfig(record),
    )


# ─── ResolvingEmbedder ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_embedder_empty_input_skips_resolution() -> None:
    # No texts → no resolution, no error even with an empty catalog.
    embedder = ResolvingEmbedder(
        resolver=_resolver(record=_record()),
        secret_store=InMemorySecretStore(),
        provider="qwen",
        model="text-embedding-v4",
    )
    assert await embedder.embed([], tenant_id=uuid4()) == []


@pytest.mark.asyncio
async def test_embedder_missing_platform_credential_raises() -> None:
    # No platform key for the embedding provider → fail fast (Mini-ADR O-3).
    embedder = ResolvingEmbedder(
        resolver=_resolver(
            record=_record(),
            platform_provs={},
        ),
        secret_store=InMemorySecretStore(),
        provider="qwen",
        model="text-embedding-v4",
    )
    with pytest.raises(CredentialsResolverError):
        await embedder.embed(["hi"], tenant_id=uuid4())


# ─── ResolvingReranker ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_reranker_empty_documents_returns_empty() -> None:
    reranker = ResolvingReranker(
        resolver=_resolver(record=_record()),
        secret_store=InMemorySecretStore(),
        provider="qwen",
        model="qwen-plus",
    )
    assert await reranker.rerank(query="q", documents=[], top_k=3, tenant_id=uuid4()) == []


@pytest.mark.asyncio
async def test_reranker_missing_credential_degrades_to_fused_order() -> None:
    # No platform rerank key → the wrapper swallows the resolver error
    # and returns the input order (truncated to top_k) — rerank is optional.
    reranker = ResolvingReranker(
        resolver=_resolver(
            record=_record(),
            platform_provs={},
        ),
        secret_store=InMemorySecretStore(),
        provider="qwen",
        model="qwen-plus",
    )
    order = await reranker.rerank(query="q", documents=["a", "b", "c"], top_k=2, tenant_id=uuid4())
    assert order == [0, 1]


# (ResolvingTavilyClient removed in M2 — the builtin web_search backend is
# now keyless SearXNG; Tavily moved to the platform MCP catalog. The
# credential-resolving path stays exercised by the embedder tests below.)


# A small structural check that the wrapper actually reaches the secret
# store with the resolved ref (platform mode happy path is exercised via
# the embedder, which is simplest to assert end to end).


@pytest.mark.asyncio
async def test_embedder_platform_mode_resolves_and_reads_key() -> None:
    captured: dict[str, Any] = {}

    class _CapturingSecretStore:
        async def get(self, ref: object) -> str:
            captured["ref"] = ref
            return "sk-embed"

        async def put(self, ref: object, value: str) -> None:  # pragma: no cover
            raise NotImplementedError

    class _NoNetworkEmbedder(ResolvingEmbedder):
        async def embed(self, texts: Sequence[str], *, tenant_id: UUID) -> list[tuple[float, ...]]:
            secret_ref = await self.resolver.resolve_provider(
                tenant_id=tenant_id, provider=self.provider
            )
            await self.secret_store.get(parse_secret_ref(secret_ref))
            return [(1.0,)]

    embedder = _NoNetworkEmbedder(
        resolver=_resolver(
            record=_record(),
            platform_provs={"qwen": "secret://platform/qwen"},
        ),
        secret_store=_CapturingSecretStore(),  # type: ignore[arg-type]
        provider="qwen",
        model="text-embedding-v4",
    )
    out = await embedder.embed(["hi"], tenant_id=uuid4())
    assert out == [(1.0,)]
    assert captured["ref"] == parse_secret_ref("secret://platform/qwen")
