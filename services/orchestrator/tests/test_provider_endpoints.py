"""B-308 §4.4 / §7.1 (1, 2, 9) — where each provider's requests go.

* official addresses are byte-identical to what the platform sent before the
  endpoint table existed (asserted on the URL the real client puts on the wire);
* a custom request address is the admin's root + the same per-use path;
* a key with a custom address is sent through the address-checking client, never
  the shared one; sibling keys each use their own address and the router's
  failover rule between them is the one it had before (B-309 changes it).
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx
import pytest
from langchain_core.messages import HumanMessage

from expert_work.protocol import ModelSpec
from expert_work.runtime.middleware import (
    BreakerRegistry,
    CircuitOpenError,
    LLMError,
    LLMErrorHandlingMiddleware,
    MiddlewareChain,
)
from expert_work.runtime.secret_store import LocalDevSecretStore
from orchestrator import build_llm_router
from orchestrator.agent_factory import _build_provider
from orchestrator.errors import AgentFactoryError
from orchestrator.llm import AllProvidersExhaustedError
from orchestrator.llm.embedder import embedding_client_for
from orchestrator.llm.endpoints import (
    OFFICIAL_BASE_URLS,
    EndpointNotSupportedError,
    ProviderKey,
    endpoint_for,
    endpoint_host,
)
from orchestrator.llm.providers import _http
from orchestrator.llm.rerank import dashscope_rerank_client
from orchestrator.llm.rerank_cohere import cohere_style_client

#: What the platform sent before B-308 (design §2.5) — literal on purpose.
_LEGACY_CHAT_URLS = {
    "anthropic": "https://api.anthropic.com/v1/messages",
    "openai": "https://api.openai.com/v1/chat/completions",
    "kimi": "https://api.moonshot.cn/v1/chat/completions",
    "glm": "https://open.bigmodel.cn/api/paas/v4/chat/completions",
    "deepseek": "https://api.deepseek.com/v1/chat/completions",
    "qwen": "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
    "doubao": "https://ark.cn-beijing.volces.com/api/v3/chat/completions",
}
_LEGACY_QWEN_EMBEDDINGS = "https://dashscope.aliyuncs.com/compatible-mode/v1/embeddings"
_LEGACY_QWEN_RERANK = (
    "https://dashscope.aliyuncs.com/api/v1/services/rerank/text-rerank/text-rerank"
)
_LEGACY_GLM_RERANK = "https://open.bigmodel.cn/api/paas/v4/rerank"
_LEGACY_GLM_LAYOUT = "https://open.bigmodel.cn/api/paas/v4/layout_parsing"

_MODELS = {
    "anthropic": "claude-sonnet-4-5",
    "openai": "gpt-4o",
    "kimi": "moonshot-v1-8k",
    "glm": "glm-4",
    "deepseek": "deepseek-chat",
    "qwen": "qwen-plus",
    "doubao": "ep-test",
}

_OPENAI_REPLY = {
    "id": "c1",
    "model": "m",
    "choices": [
        {"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}
    ],
    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
}
_ANTHROPIC_REPLY = {
    "id": "m1",
    "type": "message",
    "role": "assistant",
    "model": "m",
    "content": [{"type": "text", "text": "ok"}],
    "stop_reason": "end_turn",
    "usage": {"input_tokens": 1, "output_tokens": 1},
}


def _reply_for(request: httpx.Request) -> httpx.Response:
    if request.url.path.endswith("/messages"):
        return httpx.Response(200, json=_ANTHROPIC_REPLY)
    if request.url.path.endswith("/embeddings"):
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1.0]}]})
    if "rerank" in request.url.path:
        return httpx.Response(200, json={"output": {"results": []}, "results": []})
    return httpx.Response(200, json=_OPENAI_REPLY)


def _recording_client(seen: list[str]) -> httpx.AsyncClient:
    def _handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return _reply_for(request)

    return httpx.AsyncClient(transport=httpx.MockTransport(_handler))


@pytest.fixture
async def checked_client_calls() -> AsyncIterator[list[str]]:
    """Stand in for the address-checking client; yields the URLs sent through it."""
    seen: list[str] = []
    async with _recording_client(seen) as client:
        _http.register_checked_client(client)
        try:
            yield seen
        finally:
            _http.unregister_checked_client(client)


async def _chat_once(provider: str, *, base_url: str | None, http: httpx.AsyncClient) -> None:
    built = _build_provider(
        ModelSpec(provider=provider, name=_MODELS[provider]),  # type: ignore[arg-type]
        "sk-test",
        http_client=http,
        base_url=base_url,
    )
    await built.complete(messages=[HumanMessage(content="hi")], tools=[])


# --- 1. official addresses are unchanged -----------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(("provider", "expected"), sorted(_LEGACY_CHAT_URLS.items()))
async def test_official_chat_url_is_byte_identical(
    provider: str, expected: str, checked_client_calls: list[str]
) -> None:
    seen: list[str] = []
    async with _recording_client(seen) as shared:
        await _chat_once(provider, base_url=None, http=shared)

    assert seen == [expected]
    assert checked_client_calls == []  # official calls never use the checking client


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", sorted(set(_LEGACY_CHAT_URLS) - {"anthropic"}))
async def test_official_chat_stream_url_is_byte_identical(provider: str) -> None:
    seen: list[str] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, text="data: [DONE]\n\n")

    async with httpx.AsyncClient(transport=httpx.MockTransport(_handler)) as shared:
        built = _build_provider(
            ModelSpec(provider=provider, name=_MODELS[provider]),  # type: ignore[arg-type]
            "sk-test",
            http_client=shared,
        )
        client: Any = built.client  # type: ignore[attr-defined]
        async for _chunk in client.stream_chat_completions(
            model="m", messages=[{"role": "user", "content": "hi"}], tools=None
        ):
            pass

    assert seen == [_LEGACY_CHAT_URLS[provider]]


@pytest.mark.asyncio
async def test_official_anthropic_stream_url_is_byte_identical() -> None:
    seen: list[str] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, text="")

    async with httpx.AsyncClient(transport=httpx.MockTransport(_handler)) as shared:
        built = _build_provider(
            ModelSpec(provider="anthropic", name=_MODELS["anthropic"]),
            "sk-test",
            http_client=shared,
        )
        client: Any = built.client  # type: ignore[attr-defined]
        async for _event in client.stream_messages(
            model="m", system=None, messages=[], tools=None, max_tokens=8
        ):
            pass

    assert seen == [_LEGACY_CHAT_URLS["anthropic"]]


async def _call_embeddings(base_url: str | None, http: httpx.AsyncClient | None) -> None:
    client = embedding_client_for("qwen", api_key="k", base_url=base_url, http=http)
    await client.embeddings(model="m", texts=["x"])


async def _call_qwen_rerank(base_url: str | None, http: httpx.AsyncClient | None) -> None:
    client = dashscope_rerank_client(api_key="k", base_url=base_url, http=http)
    await client.rerank(model="m", query="q", documents=["a", "b"], top_n=2)


async def _call_glm_rerank(base_url: str | None, http: httpx.AsyncClient | None) -> None:
    client = cohere_style_client("glm", api_key="k", base_url=base_url, http=http)
    await client.rerank(model="m", query="q", documents=["a", "b"], top_n=2)


_Call = Callable[[str | None, httpx.AsyncClient | None], Any]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("call", "expected"),
    [
        (_call_embeddings, _LEGACY_QWEN_EMBEDDINGS),
        (_call_qwen_rerank, _LEGACY_QWEN_RERANK),
        (_call_glm_rerank, _LEGACY_GLM_RERANK),
    ],
)
async def test_official_embeddings_and_rerank_urls_are_byte_identical(
    call: _Call, expected: str
) -> None:
    seen: list[str] = []
    async with _recording_client(seen) as shared:
        await call(None, shared)
    assert seen == [expected]


def test_official_layout_parsing_url_is_byte_identical() -> None:
    assert endpoint_for("glm", "layout_parsing").url == _LEGACY_GLM_LAYOUT
    assert endpoint_for("glm", "layout_parsing").custom is False


# --- 2. a custom request address -------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", sorted(_LEGACY_CHAT_URLS))
async def test_custom_chat_url_is_root_plus_the_same_path(
    provider: str, checked_client_calls: list[str]
) -> None:
    official_root = OFFICIAL_BASE_URLS[provider]
    version_path = httpx.URL(official_root).path
    custom_root = f"https://p.example.com/x{version_path}"
    suffix = _LEGACY_CHAT_URLS[provider].removeprefix(official_root)
    shared_seen: list[str] = []
    async with _recording_client(shared_seen) as shared:
        # A trailing slash on the saved root is dropped.
        await _chat_once(provider, base_url=f"{custom_root}/", http=shared)

    assert checked_client_calls == [f"{custom_root}{suffix}"]
    assert shared_seen == []  # the shared client is ignored for a custom address


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("call", "base_url", "expected"),
    [
        (
            _call_embeddings,
            "https://p.example.com/x/compatible-mode/v1",
            "https://p.example.com/x/compatible-mode/v1/embeddings",
        ),
        (
            _call_qwen_rerank,
            "https://p.example.com/x/compatible-mode/v1",
            "https://p.example.com/x/api/v1/services/rerank/text-rerank/text-rerank",
        ),
        (
            _call_qwen_rerank,
            "https://p.example.com/compatible-mode/v1/",
            "https://p.example.com/api/v1/services/rerank/text-rerank/text-rerank",
        ),
        (
            _call_glm_rerank,
            "https://p.example.com/api/paas/v4",
            "https://p.example.com/api/paas/v4/rerank",
        ),
    ],
)
async def test_custom_embeddings_and_rerank_urls(
    call: _Call, base_url: str, expected: str, checked_client_calls: list[str]
) -> None:
    shared_seen: list[str] = []
    async with _recording_client(shared_seen) as shared:
        await call(base_url, shared)
    assert checked_client_calls == [expected]
    assert shared_seen == []


def test_custom_endpoint_pieces() -> None:
    chat = endpoint_for("openai", "chat", "https://p.example.com/v1/")
    assert chat.url == "https://p.example.com/v1/chat/completions"
    assert chat.custom is True
    layout = endpoint_for("glm", "layout_parsing", "https://p.example.com/api/paas/v4")
    assert layout.url == "https://p.example.com/api/paas/v4/layout_parsing"
    assert endpoint_for("openai", "chat").custom is False
    assert endpoint_for("openai", "chat", None).url == _LEGACY_CHAT_URLS["openai"]
    with pytest.raises(EndpointNotSupportedError):
        endpoint_for("kimi", "embeddings", "https://p.example.com/v1")
    with pytest.raises(EndpointNotSupportedError):
        endpoint_for("self-hosted", "chat", "https://p.example.com/v1")


def test_endpoint_host_is_name_and_port_only() -> None:
    assert endpoint_host("https://P.Example.com/secret-path/v1") == "p.example.com"
    assert endpoint_host("https://p.example.com:8443/secret-path/v1") == "p.example.com:8443"


@pytest.mark.parametrize("provider", ["self-hosted", "azure"])
def test_manifest_addressed_providers_refuse_a_credential_address(provider: str) -> None:
    spec = ModelSpec(
        provider=provider,  # type: ignore[arg-type]
        name="m",
        base_url="https://llm.example.com",
        azure_deployment="d",
        azure_api_version="2024-06-01",
    )
    with pytest.raises(AgentFactoryError):
        _build_provider(spec, "k", base_url="https://p.example.com/v1")
    _build_provider(spec, "k")  # without one they build as before


# --- 9. sibling keys: own address each, failover rule unchanged ------------

_CUSTOM = "https://p.example.com/v1"
_PRIMARY_REF = "secret://expert-work/dev/llm/primary"
_BACKUP_REF = "secret://expert-work/dev/llm/backup"


def _store() -> LocalDevSecretStore:
    return LocalDevSecretStore.from_mapping(
        {"expert-work/dev/llm/primary": "sk-primary", "expert-work/dev/llm/backup": "sk-backup"}
    )


def _fixed(keys: list[ProviderKey]) -> Callable[[str], Any]:
    async def _resolve(_provider: str) -> list[ProviderKey]:
        return keys

    return _resolve


async def _router_with_primary_reply(
    primary: httpx.Response | Exception, shared_seen: list[tuple[str, str]], **spec_kwargs: Any
) -> tuple[Any, httpx.AsyncClient, httpx.AsyncClient]:
    def _checked(request: httpx.Request) -> httpx.Response:
        shared_seen.append(("checked", str(request.url)))
        if isinstance(primary, Exception):
            raise primary
        return primary

    def _shared(request: httpx.Request) -> httpx.Response:
        key = request.headers["authorization"].removeprefix("Bearer ")
        shared_seen.append((key, str(request.url)))
        return httpx.Response(200, json=_OPENAI_REPLY)

    checked = httpx.AsyncClient(transport=httpx.MockTransport(_checked))
    shared = httpx.AsyncClient(transport=httpx.MockTransport(_shared))
    _http.register_checked_client(checked)
    router = await build_llm_router(
        ModelSpec(provider="openai", name="gpt-4o", **spec_kwargs),
        secret_store=_store(),
        provider_key_resolver=_fixed(
            [ProviderKey(_PRIMARY_REF, _CUSTOM), ProviderKey(_BACKUP_REF, None)]
        ),
        http_client=shared,
    )
    return router, checked, shared


async def _close(checked: httpx.AsyncClient, shared: httpx.AsyncClient) -> None:
    _http.unregister_checked_client(checked)
    await checked.aclose()
    await shared.aclose()


@pytest.mark.asyncio
async def test_sibling_keys_carry_their_own_address_and_breaker() -> None:
    seen: list[tuple[str, str]] = []
    router, checked, shared = await _router_with_primary_reply(httpx.Response(200), seen)
    try:
        primary, backup = router.providers
        assert primary.breaker_key != backup.breaker_key

        async def _single(base_url: str | None) -> str:
            built = await build_llm_router(
                ModelSpec(provider="openai", name="gpt-4o"),
                secret_store=_store(),
                provider_key_resolver=_fixed([ProviderKey(_PRIMARY_REF, base_url)]),
            )
            [handle] = built.providers
            return str(handle.breaker_key)

        # One credential, one handle key: only the address tells the breakers apart.
        official, custom = await _single(None), await _single(_CUSTOM)
        assert official != custom
        assert custom == await _single(_CUSTOM)
        assert official.split("@")[0] == custom.split("@")[0] == "openai:gpt-4o"
        assert "p.example.com" not in custom
    finally:
        await _close(checked, shared)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status_and_body",
    [
        (429, {"error": {"message": "rate limited", "type": "rate_limit_error"}}),
        (402, {"error": {"message": "insufficient balance"}}),
        (401, {"error": {"message": "invalid api key"}}),
    ],
)
async def test_key_level_failure_on_the_custom_address_tries_the_backup_key_at_its_own_address(
    status_and_body: tuple[int, dict[str, Any]],
) -> None:
    status, body = status_and_body
    seen: list[tuple[str, str]] = []
    router, checked, shared = await _router_with_primary_reply(
        httpx.Response(status, content=json.dumps(body)), seen
    )
    try:
        await router(messages=[HumanMessage(content="hi")], tools=[])
    finally:
        await _close(checked, shared)

    assert seen == [
        ("checked", "https://p.example.com/v1/chat/completions"),
        ("sk-backup", _LEGACY_CHAT_URLS["openai"]),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "primary",
    [
        httpx.ConnectError("endpoint address is not allowed"),
        httpx.ReadTimeout("timed out"),
        httpx.Response(503, text="upstream down"),
    ],
)
async def test_provider_level_failure_on_the_custom_address_skips_the_backup_key(
    primary: httpx.Response | Exception,
) -> None:
    """Pins today's rule (design §4.3): a network error / 5xx skips every other
    key of the same provider and model — the official-address backup included.
    B-309 is where this changes; this test goes red first."""
    seen: list[tuple[str, str]] = []
    router, checked, shared = await _router_with_primary_reply(primary, seen)
    try:
        with pytest.raises(LLMError):
            await router(messages=[HumanMessage(content="hi")], tools=[])
    finally:
        await _close(checked, shared)

    assert seen == [("checked", "https://p.example.com/v1/chat/completions")]


@pytest.mark.asyncio
async def test_provider_level_failure_moves_to_the_fallback_model() -> None:
    seen: list[tuple[str, str]] = []

    def _checked(request: httpx.Request) -> httpx.Response:
        seen.append(("checked", str(request.url)))
        return httpx.Response(503, text="upstream down")

    def _shared(request: httpx.Request) -> httpx.Response:
        seen.append((request.headers["authorization"].removeprefix("Bearer "), str(request.url)))
        return httpx.Response(200, json=_OPENAI_REPLY)

    async def _resolve(provider: str) -> list[ProviderKey]:
        if provider == "openai":
            return [ProviderKey(_PRIMARY_REF, _CUSTOM), ProviderKey(_BACKUP_REF, None)]
        return [ProviderKey(_BACKUP_REF, None)]

    checked = httpx.AsyncClient(transport=httpx.MockTransport(_checked))
    shared = httpx.AsyncClient(transport=httpx.MockTransport(_shared))
    _http.register_checked_client(checked)
    try:
        router = await build_llm_router(
            ModelSpec(
                provider="openai",
                name="gpt-4o",
                fallback=[ModelSpec(provider="deepseek", name="deepseek-chat")],
            ),
            secret_store=_store(),
            provider_key_resolver=_resolve,
            http_client=shared,
        )
        await router(messages=[HumanMessage(content="hi")], tools=[])
    finally:
        await _close(checked, shared)

    assert seen == [
        ("checked", "https://p.example.com/v1/chat/completions"),
        ("sk-backup", _LEGACY_CHAT_URLS["deepseek"]),
    ]


# --- 9b. the breaker those failures open does not hand the turn to the backup key
# (test-env acceptance 120.10, 5908fe554: three 503s opened the proxy key's
# breaker, the retried turn met CircuitOpenError and was served by the
# official-address backup key) ---------------------------------------------------

_CUSTOM_CHAT = "https://p.example.com/v1/chat/completions"


async def _no_wait(_delay: float) -> None:
    return None


async def _breaker_router(
    primary_ref_url: str | None,
    primary_reply: httpx.Response | Exception,
    seen: list[tuple[str, str]],
    registry: BreakerRegistry,
    *,
    fallback: bool = False,
) -> tuple[Any, httpx.AsyncClient, httpx.AsyncClient]:
    """Primary key (at ``primary_ref_url``) + an official-address backup key,
    under the real E.4 retry + breaker. Every request is recorded as
    ``(key, url)``; the primary key gets ``primary_reply``, any other key 200."""

    def _answer(request: httpx.Request) -> httpx.Response:
        key = request.headers["authorization"].removeprefix("Bearer ")
        seen.append((key, str(request.url)))
        if key != "sk-primary":
            return httpx.Response(200, json=_OPENAI_REPLY)
        if isinstance(primary_reply, Exception):
            raise primary_reply
        return primary_reply

    async def _resolve(provider: str) -> list[ProviderKey]:
        if provider == "openai":
            return [ProviderKey(_PRIMARY_REF, primary_ref_url), ProviderKey(_BACKUP_REF, None)]
        return [ProviderKey(_BACKUP_REF, None)]

    checked = httpx.AsyncClient(transport=httpx.MockTransport(_answer))
    shared = httpx.AsyncClient(transport=httpx.MockTransport(_answer))
    _http.register_checked_client(checked)
    router = await build_llm_router(
        ModelSpec(
            provider="openai",
            name="gpt-4o",
            fallback=[ModelSpec(provider="deepseek", name="deepseek-chat")] if fallback else [],
        ),
        secret_store=_store(),
        provider_key_resolver=_resolve,
        http_client=shared,
        around_llm_chain=MiddlewareChain.from_middlewares(
            "around_llm_call",
            [LLMErrorHandlingMiddleware(breaker_registry=registry, sleeper=_no_wait)],
        ),
    )
    return router, checked, shared


async def _force_open(registry: BreakerRegistry, breaker_key: str) -> None:
    breaker = await registry.get(breaker_key)
    for _ in range(3):
        await breaker.acquire()
        await breaker.record_failure()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "primary",
    [
        httpx.Response(503, text="upstream down"),
        httpx.ConnectError("connection refused"),
        httpx.ConnectError("name or service not known"),
        httpx.ReadTimeout("timed out"),
    ],
    ids=["503", "refused", "dns", "timeout"],
)
async def test_open_breaker_on_the_custom_address_does_not_try_the_backup_key(
    primary: httpx.Response | Exception,
) -> None:
    """Turn 1: the proxy fails three times, the key's breaker opens, the turn
    errors (no fallback model). Turn 2: the open breaker must skip the
    official-address backup key exactly as the errors that opened it did."""
    seen: list[tuple[str, str]] = []
    router, checked, shared = await _breaker_router(_CUSTOM, primary, seen, BreakerRegistry())
    try:
        with pytest.raises(LLMError):
            await router(messages=[HumanMessage(content="hi")], tools=[])
        with pytest.raises(AllProvidersExhaustedError) as turn_two:
            await router(messages=[HumanMessage(content="hi")], tools=[])
    finally:
        await _close(checked, shared)

    assert isinstance(turn_two.value.last_exc, CircuitOpenError)  # met the open breaker
    assert seen == [("sk-primary", _CUSTOM_CHAT)] * 3  # turn 2 sent nothing


@pytest.mark.asyncio
async def test_open_breaker_on_the_custom_address_moves_to_the_fallback_model() -> None:
    seen: list[tuple[str, str]] = []
    registry = BreakerRegistry()
    router, checked, shared = await _breaker_router(
        _CUSTOM, httpx.Response(503), seen, registry, fallback=True
    )
    try:
        await _force_open(registry, router.providers[0].breaker_key)
        await router(messages=[HumanMessage(content="hi")], tools=[])
    finally:
        await _close(checked, shared)

    assert seen == [("sk-backup", _LEGACY_CHAT_URLS["deepseek"])]


@pytest.mark.asyncio
async def test_open_breaker_on_an_official_key_still_tries_the_sibling_key() -> None:
    """Unchanged for keys without a request address: an open breaker is
    key-level (Stream Y-MK) and the next key of the same model serves."""
    seen: list[tuple[str, str]] = []
    registry = BreakerRegistry()
    router, checked, shared = await _breaker_router(None, httpx.Response(503), seen, registry)
    try:
        await _force_open(registry, router.providers[0].breaker_key)
        await router(messages=[HumanMessage(content="hi")], tools=[])
    finally:
        await _close(checked, shared)

    assert seen == [("sk-backup", _LEGACY_CHAT_URLS["openai"])]


# --- 8 (orchestrator half) — every router build_agent builds gets the address ---


@pytest.mark.asyncio
async def test_every_router_the_agent_build_makes_gets_the_key_resolver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Main model, per-step routes and the vision model all resolve their keys
    through the one resolver handed to ``build_agent`` — so each of them sends
    a custom-address key to its address, with nothing to wire per router."""
    from expert_work.protocol import AgentSpec
    from expert_work.runtime.checkpointer import make_checkpointer
    from orchestrator import ToolEnv, agent_factory, build_agent
    from orchestrator.multimodal import InMemoryImageResolver, ResolvedImage

    spec = AgentSpec.model_validate(
        {
            "apiVersion": "expert_work.io/v1",
            "kind": "Agent",
            "metadata": {"name": "routed", "version": "1.0.0", "tenant": "t"},
            "spec": {
                "tenant_config": {},
                "model": {"provider": "openai", "name": "gpt-4o"},
                "vision": {"model": {"provider": "openai", "name": "gpt-4o"}},
                "routing": {
                    "rules": [
                        {"when": "planning", "model": {"provider": "openai", "name": "gpt-4o"}},
                        {"when": "reflection", "model": {"provider": "openai", "name": "gpt-4o"}},
                    ]
                },
                "system_prompt": {"template": "t"},
                "sandbox": {
                    "resources": {"cpu": "1.0", "memory": "1Gi"},
                    "network": {"egress": "proxy", "allowlist": []},
                    "filesystem": {"readonly_root": True, "writable": ["/workspace"]},
                },
            },
        }
    )
    resolver = _fixed([ProviderKey(_PRIMARY_REF, _CUSTOM)])
    real = agent_factory.build_llm_router
    routers: list[Any] = []

    async def _spy(model: ModelSpec, **kwargs: Any) -> Any:
        assert kwargs.get("provider_key_resolver") is resolver
        router = await real(model, **kwargs)
        routers.append(router)
        return router

    monkeypatch.setattr(agent_factory, "build_llm_router", _spy)
    async with make_checkpointer("memory") as checkpointer:
        await build_agent(
            spec,
            secret_store=_store(),
            checkpointer=checkpointer,
            provider_key_resolver=resolver,
            tool_env=ToolEnv(
                image_resolver=InMemoryImageResolver(
                    images={"any": ResolvedImage(media_type="image/png", data=b"PNG")}
                )
            ),
        )

    assert len(routers) >= 4  # default + planning + reflection + vision (and derived ones)
    for router in routers:
        for handle in router.providers:
            client = handle.provider.inner.client
            assert client.custom_endpoint is True
            assert client.base_url == _CUSTOM


@pytest.mark.asyncio
async def test_fixed_key_resolver_only_answers_for_its_own_provider() -> None:
    """A key pinned for one provider must never be handed to another one's
    router (a fallback entry, a mis-wired caller): that would send it to the
    other vendor's address."""
    from orchestrator.llm.endpoints import fixed_provider_key_resolver

    key = ProviderKey(_PRIMARY_REF, _CUSTOM)
    resolve = fixed_provider_key_resolver("openai", key)
    assert await resolve("openai") == [key]
    with pytest.raises(ValueError, match="openai"):
        await resolve("deepseek")
