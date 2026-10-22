"""B-206 K-7b — ``/v1/knowledge``: a base's rerank-score threshold is bound to
the platform rerank model it was set under. The settings page reads whether it
is in force; the retrieval test reports a search that left it out."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from control_plane.app import create_app
from control_plane.audit import build_default_audit_logger
from control_plane.knowledge.ingestion import KnowledgeIngestionRunner
from control_plane.settings import DEFAULT_DEV_TENANT_ID, Settings
from expert_work.persistence import InMemoryKnowledgeStore
from expert_work.persistence.audit_log import InMemoryAuditLogStore
from orchestrator.llm import FakeEmbedder
from orchestrator.tools import KnowledgeRetriever
from orchestrator.tools.knowledge_rerank import RerankRanking
from tests.auth_fixtures import (
    TEST_AUDIENCE,
    TEST_ISSUER,
    build_test_jwt_verifier,
    make_test_jwt,
)

_VL = ("qwen", "qwen3-vl-rerank")
_TEXT = ("qwen", "qwen3-rerank")


class _PlatformConfig:
    """Stand-in for ``PlatformEmbeddingConfigService``; tests flip ``rerank``."""

    def __init__(self) -> None:
        self.embedding: tuple[str, str] | None = ("qwen", "text-embedding-v4")
        self.rerank: tuple[str, str] | None = _VL

    async def effective_embedding_config(self) -> tuple[str, str] | None:
        return self.embedding

    async def effective_rerank_config(self) -> tuple[str, str] | None:
        return self.rerank


class _PlatformReranker:
    """Scores every passage 0.5 and names the platform's current rerank model,
    as the platform reranker's vendor rankings do."""

    def __init__(self, config: _PlatformConfig) -> None:
        self.config = config

    async def rerank(self, **kwargs: Any) -> list[int]:
        return [i for i, _ in await self.rerank_scored(**kwargs)]

    async def rerank_scored(
        self, *, documents: Sequence[str], top_k: int, **_: Any
    ) -> list[tuple[int, float | None]]:
        assert self.config.rerank is not None
        provider, model = self.config.rerank
        ranked = [(i, 0.5) for i in range(min(len(documents), top_k))]
        return RerankRanking(ranked, model=f"{provider}/{model}")


Setup = tuple[AsyncClient, _PlatformConfig, KnowledgeIngestionRunner]


@pytest.fixture
async def setup() -> AsyncIterator[Setup]:
    store = InMemoryKnowledgeStore()
    embedder = FakeEmbedder()
    runner = KnowledgeIngestionRunner(store=store, embedder=embedder)
    config = _PlatformConfig()
    app = create_app(
        settings=Settings(
            env="dev",
            auth_mode="dev",
            rate_limit_burst=10_000,
            rate_limit_per_second=10_000.0,
            oidc_issuer=TEST_ISSUER,
            oidc_audience=[TEST_AUDIENCE],
        ),
        knowledge_repo=store,
        knowledge_ingestion_runner=runner,
        audit_logger=build_default_audit_logger(InMemoryAuditLogStore()),
        jwt_verifier=build_test_jwt_verifier(),
    )
    app.state.knowledge_retriever = KnowledgeRetriever(
        store=store, embedder=embedder, reranker=_PlatformReranker(config)
    )
    app.state.platform_embedding_config_service = config
    token = make_test_jwt(tenant_id=DEFAULT_DEV_TENANT_ID, subject="user-a")
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://cp.test",
        headers={"Authorization": f"Bearer {token}"},
    ) as client:
        yield client, config, runner


async def _config(client: AsyncClient) -> dict[str, Any]:
    body: dict[str, Any] = (await client.get("/v1/knowledge/bases/kb")).json()
    return body["retrieval_config"]  # type: ignore[no-any-return]


@pytest.mark.asyncio
async def test_a_threshold_is_recorded_with_the_model_it_was_set_under(setup: Setup) -> None:
    client, _, _ = setup
    created = await client.post(
        "/v1/knowledge/bases", json={"name": "kb", "rerank_score_threshold": 0.7}
    )
    retrieval = created.json()["retrieval_config"]
    assert retrieval["rerank_threshold_model"] == "qwen/qwen3-vl-rerank"
    assert retrieval["rerank_threshold_paused"] is False
    assert retrieval["current_rerank_model"] == "qwen/qwen3-vl-rerank"


@pytest.mark.asyncio
async def test_no_threshold_records_no_model(setup: Setup) -> None:
    client, _, _ = setup
    await client.post("/v1/knowledge/bases", json={"name": "kb"})
    retrieval = await _config(client)
    assert retrieval["rerank_threshold_model"] is None
    assert retrieval["rerank_threshold_paused"] is False


@pytest.mark.asyncio
async def test_changing_the_platform_model_pauses_the_threshold(setup: Setup) -> None:
    client, config, _ = setup
    await client.post("/v1/knowledge/bases", json={"name": "kb", "rerank_score_threshold": 0.7})
    config.rerank = _TEXT
    retrieval = await _config(client)
    assert retrieval["rerank_score_threshold"] == 0.7  # kept, not cleared
    assert retrieval["rerank_threshold_model"] == "qwen/qwen3-vl-rerank"
    assert retrieval["rerank_threshold_paused"] is True
    assert retrieval["current_rerank_model"] == "qwen/qwen3-rerank"
    listed = (await client.get("/v1/knowledge/bases")).json()["bases"][0]
    assert listed["retrieval_config"]["rerank_threshold_paused"] is True
    config.rerank = _VL  # switched back: in force again
    assert (await _config(client))["rerank_threshold_paused"] is False


@pytest.mark.asyncio
async def test_a_patch_without_the_threshold_keeps_its_binding(setup: Setup) -> None:
    client, config, _ = setup
    await client.post("/v1/knowledge/bases", json={"name": "kb", "rerank_score_threshold": 0.7})
    config.rerank = _TEXT
    saved = await client.patch("/v1/knowledge/bases/kb", json={"description": "x"})
    retrieval = saved.json()["retrieval_config"]
    assert retrieval["rerank_threshold_model"] == "qwen/qwen3-vl-rerank"
    assert retrieval["rerank_threshold_paused"] is True


@pytest.mark.asyncio
async def test_sending_the_same_threshold_binds_it_to_the_current_model(setup: Setup) -> None:
    """Recalibrating can land on the same value: sending it is the confirmation
    (the settings page sends the field only when it was edited or confirmed)."""
    client, config, _ = setup
    await client.post("/v1/knowledge/bases", json={"name": "kb", "rerank_score_threshold": 0.7})
    config.rerank = _TEXT
    saved = await client.patch("/v1/knowledge/bases/kb", json={"rerank_score_threshold": 0.7})
    retrieval = saved.json()["retrieval_config"]
    assert retrieval["rerank_threshold_model"] == "qwen/qwen3-rerank"
    assert retrieval["rerank_threshold_paused"] is False


@pytest.mark.asyncio
async def test_a_new_threshold_is_bound_to_the_current_model(setup: Setup) -> None:
    client, config, _ = setup
    await client.post("/v1/knowledge/bases", json={"name": "kb", "rerank_score_threshold": 0.7})
    config.rerank = _TEXT
    saved = await client.patch("/v1/knowledge/bases/kb", json={"rerank_score_threshold": 0.4})
    retrieval = saved.json()["retrieval_config"]
    assert retrieval["rerank_threshold_model"] == "qwen/qwen3-rerank"
    assert retrieval["rerank_threshold_paused"] is False


@pytest.mark.asyncio
async def test_clearing_the_threshold_clears_its_model(setup: Setup) -> None:
    client, _, _ = setup
    await client.post("/v1/knowledge/bases", json={"name": "kb", "rerank_score_threshold": 0.7})
    saved = await client.patch("/v1/knowledge/bases/kb", json={"rerank_score_threshold": None})
    assert saved.json()["retrieval_config"]["rerank_threshold_model"] is None


@pytest.mark.asyncio
async def test_a_chat_model_reranker_pauses_nothing_and_binds_nothing(setup: Setup) -> None:
    """A chat model ranks without scores: no threshold applies under it, so it
    neither pauses one nor becomes one's calibration model."""
    client, config, _ = setup
    await client.post("/v1/knowledge/bases", json={"name": "kb", "rerank_score_threshold": 0.7})
    config.rerank = ("qwen", "qwen-plus")
    retrieval = await _config(client)
    assert (retrieval["rerank_threshold_paused"], retrieval["current_rerank_model"]) == (
        False,
        None,
    )
    saved = await client.patch("/v1/knowledge/bases/kb", json={"rerank_score_threshold": 0.6})
    assert saved.json()["retrieval_config"]["rerank_threshold_model"] is None


@pytest.mark.asyncio
async def test_without_a_platform_rerank_model_nothing_is_paused(setup: Setup) -> None:
    client, config, _ = setup
    await client.post("/v1/knowledge/bases", json={"name": "kb", "rerank_score_threshold": 0.7})
    config.rerank = None
    retrieval = await _config(client)
    assert (retrieval["rerank_threshold_paused"], retrieval["current_rerank_model"]) == (
        False,
        None,
    )


@pytest.mark.asyncio
async def test_the_retrieval_test_reports_a_threshold_left_out(setup: Setup) -> None:
    client, config, runner = setup
    await client.post("/v1/knowledge/bases", json={"name": "kb", "rerank_score_threshold": 0.9})
    await client.post(
        "/v1/knowledge/bases/kb/documents",
        files={"file": ("h.md", b"# H\n\nThe deductible is 500 dollars.", "text/markdown")},
    )
    await runner.drain()
    in_force = (await client.post("/v1/knowledge/bases/kb/test", json={"query": "x"})).json()
    config.rerank = _TEXT
    left_out = (await client.post("/v1/knowledge/bases/kb/test", json={"query": "x"})).json()
    # Every passage scores 0.5: under its own model the threshold turns them all down.
    assert (in_force["status"], in_force["count"], in_force["rerank_threshold_paused"]) == (
        "below_threshold",
        0,
        False,
    )
    assert (left_out["status"], left_out["rerank_threshold_paused"]) == ("ok", True)
    assert left_out["count"] >= 1
    # The notice names both models from this search, not from a page loaded earlier.
    assert (in_force["rerank_threshold_model"], in_force["rerank_model"]) == (
        "qwen/qwen3-vl-rerank",
        "qwen/qwen3-vl-rerank",
    )
    assert (left_out["rerank_threshold_model"], left_out["rerank_model"]) == (
        "qwen/qwen3-vl-rerank",
        "qwen/qwen3-rerank",
    )
    assert left_out["rerank_threshold_code_kept"] == 0


@pytest.mark.asyncio
async def test_the_retrieval_test_reports_results_kept_for_a_code(setup: Setup) -> None:
    client, _, runner = setup
    await client.post("/v1/knowledge/bases", json={"name": "kb", "rerank_score_threshold": 0.9})
    await client.post(
        "/v1/knowledge/bases/kb/documents",
        files={
            "file": ("h.md", b"# H\n\nModel ZX-9041 has an 18-month warranty.", "text/markdown")
        },
    )
    await runner.drain()
    body = (
        await client.post("/v1/knowledge/bases/kb/test", json={"query": "ZX-9041 warranty"})
    ).json()
    assert (body["status"], body["rerank_threshold_code_kept"]) == ("ok", body["count"])
    assert body["count"] >= 1
