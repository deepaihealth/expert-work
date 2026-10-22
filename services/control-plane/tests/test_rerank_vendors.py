"""B-206 K-7c — the resolving rerankers pick the rerank API from the model
catalog: DashScope's native one, a Cohere-style one, or the chat-model ranking;
a model whose API the platform has no client for is skipped, never crashed on
or sent to the chat path."""

from __future__ import annotations

import json
import logging
import math
from typing import Any
from uuid import uuid4

import httpx
import pytest

from control_plane import rerank_vendors
from control_plane.rerank_vendors import rerank_api, rerank_is_callable
from control_plane.runtime import DynamicResolvingReranker, ResolvingReranker
from expert_work.common.credentials import CredentialsResolverError
from expert_work.protocol import ModelEntry
from expert_work.protocol.model_catalog import MODEL_CATALOG, RerankCaps
from orchestrator.llm import RerankGuards
from orchestrator.llm.rerank_cohere import COHERE_STYLE_SHAPES, RERANK_SHAPE_BY_PROVIDER
from orchestrator.tools.knowledge_rerank import RerankRanking


class _Cfg:
    def __init__(self, rerank: tuple[str, str]) -> None:
        self.rerank = rerank

    async def effective_rerank_config(self) -> tuple[str, str] | None:
        return self.rerank


class _Resolver:
    def __init__(self) -> None:
        self.resolved: list[str] = []

    async def resolve_provider(self, *, tenant_id: Any, provider: str) -> str:
        self.resolved.append(provider)
        return f"secret://{provider}"


class _SecretStore:
    async def get(self, name: Any) -> str:
        return "fake-key"


@pytest.fixture(autouse=True)
def _fresh_warnings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each test starts with no model already warned about."""
    monkeypatch.setattr(rerank_vendors, "_warned_unsupported", set())


def _no_chat_router(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*_a: Any, **_k: Any) -> None:
        raise AssertionError("the chat-model (LLM) rerank path was used")

    monkeypatch.setattr("control_plane.runtime.build_llm_router", _boom)


def _both(
    provider: str,
    model: str,
    *,
    http: httpx.AsyncClient | None = None,
    guards: RerankGuards | None = None,
    resolver: Any = None,
) -> list[Any]:
    """The dynamic (platform config) and the static (settings) reranker for one model."""
    shared = guards if guards is not None else RerankGuards()
    resolver = resolver if resolver is not None else _Resolver()
    return [
        DynamicResolvingReranker(
            config_service=_Cfg((provider, model)),  # type: ignore[arg-type]
            resolver=resolver,
            secret_store=_SecretStore(),  # type: ignore[arg-type]
            http=http,
            rerank_guards=shared,
        ),
        ResolvingReranker(
            resolver=resolver,
            secret_store=_SecretStore(),  # type: ignore[arg-type]
            provider=provider,  # type: ignore[arg-type]
            model=model,
            http=http,
            rerank_guards=shared,
        ),
    ]


def test_the_catalog_decides_the_rerank_api() -> None:
    assert rerank_api("qwen", "qwen3-vl-rerank") == "dashscope"
    assert rerank_api("glm", "rerank") == "cohere_style"
    # Off the catalog, the old rule: a DashScope model named "rerank" goes native …
    assert rerank_api("qwen", "gte-rerank-v2") == "dashscope"
    # … anything else is ranked by a chat model.
    assert rerank_api("qwen", "qwen-plus") == "llm"
    assert rerank_api("doubao", "doubao-rerank") == "llm"
    assert rerank_api("anthropic", "claude-haiku-4-5") == "llm"


def _sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))


@pytest.mark.asyncio
async def test_a_cohere_style_model_is_called_at_its_provider_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _no_chat_router(monkeypatch)
    seen: list[dict[str, Any]] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        seen.append(
            {
                "url": str(request.url),
                "auth": request.headers.get("Authorization"),
                "body": json.loads(request.content),
            }
        )
        # Zhipu's raw logits (the 2026-10-22 live answer's shape).
        return httpx.Response(
            200,
            json={
                "results": [
                    {"index": 1, "relevance_score": 22.265625},
                    {"index": 0, "relevance_score": -1.5},
                ],
                "usage": {"total_tokens": 9},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(_handler)) as http:
        for reranker in _both("glm", "rerank", http=http):
            ranked = await reranker.rerank_scored(
                query="q", documents=["a", "b"], top_k=2, tenant_id=uuid4()
            )
            assert [index for index, _ in ranked] == [1, 0]
            assert [score for _, score in ranked] == pytest.approx(
                [_sigmoid(22.265625), _sigmoid(-1.5)]
            )
            assert isinstance(ranked, RerankRanking)
            assert ranked.outcome == "ok"
    assert [request["url"] for request in seen] == [
        "https://open.bigmodel.cn/api/paas/v4/rerank"
    ] * 2
    assert {request["auth"] for request in seen} == {"Bearer fake-key"}
    assert seen[0]["body"]["model"] == "rerank"
    assert seen[0]["body"]["return_raw_scores"] is True


@pytest.mark.asyncio
async def test_the_cohere_style_branch_is_retried_by_the_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Zhipu 429 for load (code 1302) is retried by the vendor-neutral guard."""
    _no_chat_router(monkeypatch)
    calls = 0

    def _handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls % 2 == 1:
            return httpx.Response(429, json={"error": {"code": "1302", "message": "busy"}})
        return httpx.Response(200, json={"results": [{"index": 0, "relevance_score": 0.0}]})

    async def _no_sleep(_delay: float) -> None:
        return None

    guards = RerankGuards(sleep=_no_sleep)
    async with httpx.AsyncClient(transport=httpx.MockTransport(_handler)) as http:
        for reranker in _both("glm", "rerank", http=http, guards=guards):
            ranked = await reranker.rerank_scored(
                query="q", documents=["a"], top_k=1, tenant_id=uuid4()
            )
            assert ranked == [(0, 0.5)]
    assert calls == 4  # each reranker: a 429, then its retry


def _catalog_with(provider: str, model: str, api: str) -> Any:
    entry = ModelEntry(name=model, rerank=True, rerank_caps=RerankCaps(rerank_api=api))  # type: ignore[arg-type]

    def _lookup(p: str, m: str) -> ModelEntry | None:
        return entry if (p, m) == (provider, model) else None

    return _lookup


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider", "model", "api", "why"),
    [
        ("qwen", "some-vertex-rerank", "vertex", "'vertex'"),
        ("qwen", "some-bedrock-rerank", "bedrock", "'bedrock'"),
        # Cohere-style, but the platform knows no such endpoint for the provider.
        ("openai", "some-rerank", "cohere_style", "'openai'"),
        # DashScope's native endpoint belongs to the qwen provider only.
        ("doubao", "some-rerank", "dashscope", "'doubao'"),
    ],
)
async def test_a_model_without_a_client_here_is_skipped(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    provider: str,
    model: str,
    api: str,
    why: str,
) -> None:
    _no_chat_router(monkeypatch)
    monkeypatch.setattr(
        "control_plane.rerank_vendors.catalog_entry", _catalog_with(provider, model, api)
    )
    resolver = _Resolver()
    with caplog.at_level(logging.WARNING, logger="control_plane.rerank_vendors"):
        for reranker in _both(provider, model, resolver=resolver):
            ranked = await reranker.rerank_scored(
                query="q", documents=["a", "b", "c"], top_k=2, tenant_id=uuid4()
            )
            assert ranked == [(0, None), (1, None)]
            assert isinstance(ranked, RerankRanking)
            assert ranked.outcome == "model_unsupported"
    assert resolver.resolved == []  # skipped before any credential is read
    assert not rerank_is_callable(provider, model)
    # Warned once for the model, however many searches (and rerankers) skip it.
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert f"{provider}/{model}" in warnings[0] and why in warnings[0]


def test_every_catalog_rerank_model_can_be_called() -> None:
    """Every row the settings page could offer has a client here."""
    rows = [(p, e.name) for p, entries in MODEL_CATALOG.items() for e in entries if e.rerank]
    assert rows, "no rerank model in the catalog — the check would pass vacuously"
    for provider, model in rows:
        assert rerank_is_callable(provider, model), f"{provider}/{model}"


def test_catalog_rerank_rows_match_their_clients() -> None:
    """``dashscope`` rows sit on ``qwen``; a Cohere-style row declared
    ``score_scale="logit"`` is served by a family that asks for raw scores."""
    seen = 0
    for provider, entries in MODEL_CATALOG.items():
        for entry in entries:
            caps = entry.rerank_caps
            if caps is None:
                continue
            seen += 1
            if caps.rerank_api == "dashscope":
                assert provider == "qwen", f"{provider}/{entry.name}"
            if caps.rerank_api == "cohere_style" and caps.score_scale == "logit":
                shape = COHERE_STYLE_SHAPES[RERANK_SHAPE_BY_PROVIDER[provider]]
                raw = [name for name, value in shape.fixed if "raw" in name and value is True]
                assert raw, f"{provider}/{entry.name}: the family returns no raw logits"
    assert seen >= 2


class _RaisingResolver:
    async def resolve_provider(self, *, tenant_id: Any, provider: str) -> str:
        raise CredentialsResolverError(
            "no credential", mode="platform", kind="provider", key=provider
        )


@pytest.mark.asyncio
async def test_a_cohere_style_model_without_a_credential_is_unavailable() -> None:
    for reranker in _both("glm", "rerank", resolver=_RaisingResolver()):
        ranked = await reranker.rerank_scored(
            query="q", documents=["a"], top_k=1, tenant_id=uuid4()
        )
        assert isinstance(ranked, RerankRanking)
        assert ranked.outcome == "model_unavailable"


def test_the_dashscope_client_is_only_built_for_qwen() -> None:
    with pytest.raises(ValueError, match="doubao"):
        rerank_vendors.vendor_reranker(
            "dashscope",
            provider="doubao",
            model="some-rerank",
            api_key="k",
            secret_ref="secret://doubao",
            http=None,
            guards=RerankGuards(),
        )
