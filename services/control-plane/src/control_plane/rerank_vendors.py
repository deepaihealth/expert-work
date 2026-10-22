"""Which rerank API serves the platform's rerank model, and its vendor
reranker — B-206 K-7c.

The model catalog's ``rerank_api`` decides (:func:`rerank_api`): DashScope's
native endpoint, a Cohere-style one, or ranking by a chat model (``llm``). A
model the catalog does not know keeps the rule that came before the catalog: a
DashScope (``qwen``) model named ``…rerank…`` (``gte-rerank``,
``text-rerank-*``) goes to the native endpoint — the OpenAI-compatible chat
endpoint 404s on it — and anything else is ranked by a chat model.

Whether the platform can call it at all is :func:`rerank_is_callable` (no side
effects — the settings page offers and accepts only such models): a ``vertex``
/ ``bedrock`` model, a Cohere-style one on a provider whose endpoint the
platform does not know, or a ``dashscope`` one off the ``qwen`` provider has no
client here. The resolving rerankers (:mod:`control_plane.runtime`) ask
:func:`callable_rerank_api` before reading a credential and skip such a model
(the search reports ``model_unsupported``, warned once per model) — never
sending it to the chat path, which would fail on every search. They then build
the vendor reranker with :func:`vendor_reranker`, behind the process's guard.
"""

from __future__ import annotations

import logging
from typing import Any, Literal, assert_never

import httpx

from expert_work.protocol.model_catalog import RerankApi, catalog_entry
from orchestrator.llm import DashScopeReranker, HTTPDashScopeRerankClient, RerankGuards
from orchestrator.llm.rerank_batches import CohereStyleReranker
from orchestrator.llm.rerank_cohere import RERANK_SHAPE_BY_PROVIDER, cohere_style_client

logger = logging.getLogger(__name__)

#: The rerank APIs this platform has a client for: a vendor endpoint, or the
#: chat-model ranking.
VendorRerankApi = Literal["dashscope", "cohere_style"]
CallableRerankApi = Literal["dashscope", "cohere_style", "llm"]

#: DashScope's native rerank endpoint is the ``qwen`` provider's.
_DASHSCOPE_PROVIDER = "qwen"

#: The (provider, model) pairs already warned about — one warning per process.
_warned_unsupported: set[tuple[str, str]] = set()


def is_dashscope_rerank_model(provider: str, model: str) -> bool:
    """DashScope (``qwen``) dedicated rerank models (``qwen3-vl-rerank``,
    ``gte-rerank``, ``text-rerank-*``) are NOT served over the OpenAI-compat
    chat endpoint, so the chat-prompt ``LLMReranker`` 404s on them. Route those
    to the native rerank API (:class:`DashScopeReranker`) instead."""
    return provider == _DASHSCOPE_PROVIDER and "rerank" in model.lower()


def rerank_api(provider: str, model: str) -> RerankApi:
    """The rerank API serving ``provider/model``: the catalog's, else by name
    (see the module docstring)."""
    entry = catalog_entry(provider, model)
    if entry is not None and entry.rerank_caps is not None:
        return entry.rerank_caps.rerank_api
    return "dashscope" if is_dashscope_rerank_model(provider, model) else "llm"


def _callable(provider: str, api: RerankApi) -> CallableRerankApi | None:
    """``api`` when this platform can call it for ``provider``, else ``None``."""
    if api == "dashscope":
        return api if provider == _DASHSCOPE_PROVIDER else None
    if api == "cohere_style":
        return api if provider in RERANK_SHAPE_BY_PROVIDER else None
    if api == "llm":
        return api
    return None  # vertex / bedrock: no client yet


def rerank_is_callable(provider: str, model: str) -> bool:
    """Whether this platform has a client for ``provider/model``'s rerank API."""
    return _callable(provider, rerank_api(provider, model)) is not None


def callable_rerank_api(provider: str, model: str) -> CallableRerankApi | None:
    """:func:`rerank_api` when :func:`rerank_is_callable`, else ``None`` —
    warned once per (provider, model) in this process."""
    api = rerank_api(provider, model)
    usable = _callable(provider, api)
    if usable is None and (provider, model) not in _warned_unsupported:
        _warned_unsupported.add((provider, model))
        logger.warning(
            "knowledge.rerank_skipped — rerank model %s/%s cannot be called: the platform "
            "has no client for its rerank API %r on provider %r (searches keep the recall "
            "order, rerank_outcome=model_unsupported)",
            provider,
            model,
            api,
            provider,
        )
    return usable


def instruction_kwargs(instruction: str | None) -> dict[str, Any]:
    """B-206 K-14 —— 任务说明只在有的时候才往下传(见 ``ScoredReranker``);LLM 重排分支不用它。"""
    return {} if instruction is None else {"instruction": instruction}


def vendor_reranker(
    api: VendorRerankApi,
    *,
    provider: str,
    model: str,
    api_key: str,
    secret_ref: str,
    http: httpx.AsyncClient | None,
    guards: RerankGuards,
) -> DashScopeReranker | CohereStyleReranker:
    """The reranker calling ``provider/model``'s own rerank endpoint (see
    :func:`callable_rerank_api`), its client behind ``guards``."""
    if api == "dashscope":
        if provider != _DASHSCOPE_PROVIDER:
            msg = f"DashScope's rerank endpoint is not served for provider {provider!r}"
            raise ValueError(msg)
        native = HTTPDashScopeRerankClient(api_key=api_key, http=http)
        client = guards.wrap(native, provider=provider, model=model, secret_ref=secret_ref)
        return DashScopeReranker(client=client, model=model)
    if api == "cohere_style":
        cohere = cohere_style_client(provider, api_key=api_key, http=http)
        entry = catalog_entry(provider, model)
        return CohereStyleReranker(
            client=guards.wrap(cohere, provider=provider, model=model, secret_ref=secret_ref),
            model=model,
            caps=entry.rerank_caps if entry is not None else None,
        )
    assert_never(api)
