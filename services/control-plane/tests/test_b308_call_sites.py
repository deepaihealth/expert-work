"""B-308 §4.6 / §7.1 (8) — every place that uses a platform credential sends it
to the request address saved on that credential.

Each test drives one entry point with a credential whose address is a custom
one and asserts where the request actually went: the address-checking client is
replaced by a recorder, so "it arrived there" also means "it did not use the
shared client". Two structural tests keep later code from resolving a key
without its address.
"""

from __future__ import annotations

import ast
import pathlib
import sys
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from langchain_core.messages import HumanMessage

from control_plane.aux_model_adapter import make_llm_router_aux_model
from control_plane.delegation_policy import make_delegation_policy_aux
from control_plane.knowledge.recognition.clients import RecognitionCredential
from control_plane.knowledge.recognition.figure_pipeline import default_figure_client
from control_plane.knowledge.recognition.recognizer import ModelChoice, default_client
from control_plane.platform_secrets import PlatformSecretsService
from control_plane.quality_judge import QualityJudge
from control_plane.runtime import (
    DynamicResolvingEmbedder,
    DynamicResolvingReranker,
    ResolvingEmbedder,
    ResolvingProviderKeys,
    ResolvingReranker,
    _build_judge_caller,
    make_provider_key_resolver,
)
from control_plane.settings import Settings
from control_plane.tenant_secret_overlay import ProviderKeyNeedsEndpointError
from expert_work.persistence.platform_secrets import InMemoryPlatformSecretStore
from expert_work.protocol import AgentSpec, ModelSpec, TenantConfigRecord, TenantPlan
from expert_work.runtime.secret_store import LocalDevSecretStore
from orchestrator import build_llm_router
from orchestrator.llm.endpoints import ProviderKey
from orchestrator.llm.providers import _http
from tests.credential_fixtures import static_credentials_resolver

_TENANT = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
_REPO = pathlib.Path(__file__).resolve().parents[3]

_CHAT_REPLY = {
    "id": "c1",
    "model": "m",
    "choices": [
        {"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}
    ],
    "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
}
_ANTHROPIC_REPLY = {
    "id": "m1",
    "type": "message",
    "role": "assistant",
    "model": "m",
    "content": [{"type": "text", "text": "ok"}],
    "stop_reason": "end_turn",
    "usage": {"input_tokens": 3, "output_tokens": 1},
}


def _reply(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path.endswith("/messages"):
        return httpx.Response(200, json=_ANTHROPIC_REPLY)
    if path.endswith("/embeddings"):
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1.0]}]})
    if path.endswith("/layout_parsing"):
        return httpx.Response(200, json={"md_results": "page text"})
    if "rerank" in path:
        results = [{"index": 0, "relevance_score": 0.9}, {"index": 1, "relevance_score": 0.1}]
        return httpx.Response(200, json={"output": {"results": results}, "results": results})
    return httpx.Response(200, json=_CHAT_REPLY)


@pytest.fixture
async def custom_calls() -> AsyncIterator[list[str]]:
    """URLs of the requests sent through the address-checking client."""
    seen: list[str] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return _reply(request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(_handler))
    _http.register_checked_client(client)
    try:
        yield seen
    finally:
        _http.unregister_checked_client(client)
        await client.aclose()


@pytest.fixture
async def shared_http() -> AsyncIterator[httpx.AsyncClient]:
    """The process-level shared client; a custom-address call must never use it."""

    def _handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"custom-address call used the shared client: {request.url.host}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(_handler)) as client:
        yield client


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


def _custom(provider: str) -> str:
    return {
        "openai": "https://p.example.com/v1",
        "anthropic": "https://p.example.com/v1",
        "glm": "https://p.example.com/api/paas/v4",
        "qwen": "https://p.example.com/x/compatible-mode/v1",
    }[provider]


def _resolver(provider: str, base_url: str | None = None) -> Any:
    key = ProviderKey(f"secret://k/{provider}", base_url or _custom(provider))
    return static_credentials_resolver(
        platform_provider_credentials={provider: key},
        platform_tool_credentials={},
        tenant_config_getter=_TenantConfig(),
    )


def _secrets() -> LocalDevSecretStore:
    return LocalDevSecretStore.from_mapping(
        {f"k/{p}": f"sk-{p}" for p in ("openai", "anthropic", "glm", "qwen")}
    )


# --- #1 / #2 / #3 / #4 — the agent build's key resolver -------------------------


@pytest.mark.asyncio
async def test_agent_key_resolver_hands_the_router_each_keys_own_address(
    custom_calls: list[str], shared_http: httpx.AsyncClient
) -> None:
    keys = [ProviderKey("secret://k/openai", _custom("openai")), ProviderKey("secret://k/glm")]
    resolver = static_credentials_resolver(
        platform_provider_credentials={"openai": keys},
        platform_tool_credentials={},
        tenant_config_getter=_TenantConfig(),
    )
    resolve = make_provider_key_resolver(resolver=resolver, tenant_id=_TENANT)

    assert await resolve("openai") == keys
    router = await build_llm_router(
        ModelSpec(provider="openai", name="gpt-4o"),
        secret_store=_secrets(),
        provider_key_resolver=resolve,
        http_client=shared_http,
    )
    await router(messages=[HumanMessage(content="hi")], tools=[])

    assert custom_calls == ["https://p.example.com/v1/chat/completions"]


def _agent_spec(name: str = "a") -> AgentSpec:
    return AgentSpec.model_validate(
        {
            "apiVersion": "expert_work.io/v1",
            "kind": "Agent",
            "metadata": {"name": name, "version": "1.0.0", "tenant": "t"},
            "spec": {
                "tenant_config": {},
                "model": {"provider": "openai", "name": "gpt-4o"},
                "system_prompt": {"template": "x"},
                "defenses": {"output_judge": "block", "action_screen": "block"},
                "sandbox": {
                    "resources": {"cpu": "1.0", "memory": "1Gi"},
                    "network": {"egress": "proxy", "allowlist": []},
                    "filesystem": {"readonly_root": True, "writable": ["/workspace"]},
                },
            },
        }
    )


@pytest.fixture
def builds(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Record what the three agent build entry points hand to ``build_agent``,
    and every ``build_llm_router`` call the control-plane makes around it (the
    defense judges')."""
    from orchestrator import BuiltAgent

    calls: list[dict[str, Any]] = []

    async def _fake_build_agent(spec: AgentSpec, **kwargs: Any) -> BuiltAgent:
        calls.append({"entry": "build_agent", **kwargs})
        return BuiltAgent(graph=object(), system_prompt="", max_steps=1)  # type: ignore[arg-type]

    real_router = build_llm_router

    async def _spy_router(model: ModelSpec, **kwargs: Any) -> Any:
        calls.append({"entry": "judge_router", "model": model, **kwargs})
        return await real_router(model, **kwargs)

    monkeypatch.setattr("control_plane.runtime.build_agent", _fake_build_agent)
    monkeypatch.setattr("control_plane.subagent_runtime.build_agent", _fake_build_agent)
    monkeypatch.setattr("control_plane.runtime.build_llm_router", _spy_router)
    return calls


async def _assert_build_carries_the_address(calls: list[dict[str, Any]]) -> None:
    expected = [ProviderKey("secret://k/openai", _custom("openai"))]
    [agent] = [c for c in calls if c["entry"] == "build_agent"]
    assert await agent["provider_key_resolver"]("openai") == expected
    # resolve_defenses: the judges' router is pinned to the same key + address.
    judges = [c for c in calls if c["entry"] == "judge_router"]
    assert judges
    for judge in judges:
        assert judge["model"].api_key_ref is None
        assert await judge["provider_key_resolver"]("openai") == expected


@pytest.mark.asyncio
async def test_main_agent_build_and_its_judges_get_the_address(
    builds: list[dict[str, Any]],
) -> None:
    from langgraph.checkpoint.memory import InMemorySaver

    from control_plane.runtime import make_agent_builder

    builder = make_agent_builder(
        _secrets(), InMemorySaver(), credentials_resolver=_resolver("openai")
    )
    await builder(_agent_spec(), tenant_id=_TENANT)
    await _assert_build_carries_the_address(builds)


@pytest.mark.asyncio
async def test_child_agent_build_and_its_judges_get_the_address(
    builds: list[dict[str, Any]],
) -> None:
    from langgraph.checkpoint.memory import InMemorySaver

    from control_plane.subagent_runtime import make_child_agent_builder
    from expert_work.persistence.agent_spec import InMemoryAgentSpecStore
    from orchestrator import ToolEnv

    store = InMemoryAgentSpecStore()
    await store.create(
        tenant_id=_TENANT, spec=_agent_spec("child"), spec_sha256="a" * 64, created_by="t"
    )
    builder = make_child_agent_builder(
        spec_store=store,
        secret_store=_secrets(),
        checkpointer=InMemorySaver(),
        base_tool_env=ToolEnv(),
        credentials_resolver=_resolver("openai"),
    )
    await builder(tenant_id=_TENANT, name="child", version="1.0.0", depth=1)
    await _assert_build_carries_the_address(builds)


@pytest.mark.asyncio
async def test_worker_agent_build_and_its_judges_get_the_address(
    builds: list[dict[str, Any]],
) -> None:
    from langgraph.checkpoint.memory import InMemorySaver

    from control_plane.subagent_runtime import make_worker_build_fn
    from orchestrator import ToolEnv

    build_fn = make_worker_build_fn(
        secret_store=_secrets(),
        checkpointer=InMemorySaver(),
        base_tool_env=ToolEnv(),
        max_iterations=8,
        allowed_toolsets=[],
        credentials_resolver=_resolver("openai"),
    )
    await build_fn(_agent_spec("parent"), tenant_id=_TENANT, role="probe", depth=1)
    await _assert_build_carries_the_address(builds)


@pytest.mark.asyncio
async def test_memory_writeback_router_gets_the_address(
    custom_calls: list[str], shared_http: httpx.AsyncClient
) -> None:
    from control_plane.memory.writeback_processor import make_router_caller_factory

    factory = make_router_caller_factory(
        secret_store=_secrets(),
        credentials_resolver=_resolver("openai"),
        middleware_env=None,
        http_client=shared_http,
        rate_limiter_factory=None,
    )
    spec = _agent_spec()
    router = await factory(spec, spec.spec.model, _TENANT)
    await router(messages=[HumanMessage(content="hi")], tools=[])

    assert custom_calls == ["https://p.example.com/v1/chat/completions"]


# --- #5 — the defense judge caller ----------------------------------------------


@pytest.mark.asyncio
async def test_defense_judge_call_goes_to_the_address(
    custom_calls: list[str], shared_http: httpx.AsyncClient
) -> None:
    caller = await _build_judge_caller(
        _agent_spec(),
        tenant_id=_TENANT,
        credentials_resolver=_resolver("openai"),
        secret_store=_secrets(),
        judge_config_service=None,
        http_client=shared_http,
    )
    await caller(messages=[HumanMessage(content="judge this")], tools=[])

    assert custom_calls == ["https://p.example.com/v1/chat/completions"]


# --- #6 — the quality judge -----------------------------------------------------


@pytest.mark.asyncio
async def test_quality_judge_call_goes_to_the_address(custom_calls: list[str]) -> None:
    judge = QualityJudge(resolver=_resolver("anthropic"), secret_store=_secrets())
    # The canned reply is not a valid verdict: the score is dropped, the call was made.
    await judge.score(
        tenant_id=_TENANT, prompt="p", reply="r", provider="anthropic", model="claude-haiku-4-5"
    )

    assert custom_calls
    assert set(custom_calls) == {"https://p.example.com/v1/messages"}


# --- #7 — the auxiliary model and its entry points -------------------------------


@pytest.mark.asyncio
async def test_aux_model_call_goes_to_the_address(custom_calls: list[str]) -> None:
    aux = make_llm_router_aux_model(
        resolver=_resolver("openai"),
        secret_store=_secrets(),
        default_provider="openai",
        default_model="gpt-4o",
    )
    await aux(prompt="summarise", model=None, tenant_id=_TENANT)

    assert custom_calls == ["https://p.example.com/v1/chat/completions"]


@pytest.mark.asyncio
async def test_delegation_policy_aux_call_goes_to_the_address(custom_calls: list[str]) -> None:
    aux = make_delegation_policy_aux(
        resolver=_resolver("openai"),
        secret_store=_secrets(),
        default_provider="openai",
        default_model="gpt-4o",
    )
    await aux(prompt="policy", tenant_id=_TENANT)
    assert custom_calls == ["https://p.example.com/v1/chat/completions"]


def _calls_named(tree: ast.AST, name: str) -> list[ast.Call]:
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            called = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            if called == name:
                found.append(node)
    return found


def test_every_aux_model_built_in_the_app_uses_the_credentials_resolver() -> None:
    """The knowledge-profile, memory-consolidation and skill-evolution aux
    models are built inside the app lifespan (it needs a database to run). They
    get the address the same way the two tests above do — through the resolver
    — so what is checked here is that each of them is handed the app's resolver."""
    source = (_REPO / "services/control-plane/src/control_plane/app.py").read_text()
    builders = [
        call
        for name in (
            "make_llm_router_aux_model",
            "make_delegation_policy_aux",
            "LLMRouterAuxModelAdapter",
        )
        for call in _calls_named(ast.parse(source), name)
    ]
    assert len(builders) >= 4  # delegation policy, knowledge profile, consolidation, skills
    for call in builders:
        resolver = next(kw.value for kw in call.keywords if kw.arg == "resolver")
        assert isinstance(resolver, ast.Name)
        assert resolver.id == "credentials_resolver"


# --- #8 — rerank with a chat model -----------------------------------------------


class _RerankConfig:
    def __init__(self, provider: str, model: str) -> None:
        self._pair = (provider, model)

    async def effective_rerank_config(self) -> tuple[str, str]:
        return self._pair

    async def effective_embedding_config(self) -> tuple[str, str]:
        return self._pair


def _rerankers(provider: str, model: str, http: httpx.AsyncClient) -> list[Any]:
    return [
        ResolvingReranker(
            resolver=_resolver(provider),
            secret_store=_secrets(),
            provider=provider,  # type: ignore[arg-type]
            model=model,
            http=http,
        ),
        DynamicResolvingReranker(
            config_service=_RerankConfig(provider, model),  # type: ignore[arg-type]
            resolver=_resolver(provider),
            secret_store=_secrets(),
            http=http,
        ),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("which", [0, 1])
async def test_chat_model_rerank_goes_to_the_address(
    which: int, custom_calls: list[str], shared_http: httpx.AsyncClient
) -> None:
    reranker = _rerankers("openai", "gpt-4o", shared_http)[which]
    await reranker.rerank_scored(query="q", documents=["a", "b"], top_k=2, tenant_id=_TENANT)

    assert custom_calls == ["https://p.example.com/v1/chat/completions"]


# --- #9 — embeddings ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_embedders_go_to_the_address(
    custom_calls: list[str], shared_http: httpx.AsyncClient
) -> None:
    static = ResolvingEmbedder(
        resolver=_resolver("qwen"),
        secret_store=_secrets(),
        provider="qwen",
        model="text-embedding-v4",
        http=shared_http,
    )
    dynamic = DynamicResolvingEmbedder(
        config_service=_RerankConfig("glm", "embedding-3"),  # type: ignore[arg-type]
        resolver=_resolver("glm"),
        secret_store=_secrets(),
        http=shared_http,
    )
    await static.embed(["hi"], tenant_id=_TENANT)
    await dynamic.embed(["hi"], tenant_id=_TENANT)

    assert custom_calls == [
        "https://p.example.com/x/compatible-mode/v1/embeddings",
        "https://p.example.com/api/paas/v4/embeddings",
    ]


# --- #10 — the vendors' own rerank endpoints ----------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider", "model", "expected"),
    [
        (
            "qwen",
            "qwen3-vl-rerank",
            "https://p.example.com/x/api/v1/services/rerank/text-rerank/text-rerank",
        ),
        ("glm", "rerank", "https://p.example.com/api/paas/v4/rerank"),
    ],
)
@pytest.mark.parametrize("which", [0, 1])
async def test_vendor_rerank_goes_to_the_address(
    provider: str,
    model: str,
    expected: str,
    which: int,
    custom_calls: list[str],
    shared_http: httpx.AsyncClient,
) -> None:
    reranker = _rerankers(provider, model, shared_http)[which]
    ranked = await reranker.rerank_scored(
        query="q", documents=["a", "b"], top_k=2, tenant_id=_TENANT
    )

    assert custom_calls == [expected]
    assert [index for index, _score in ranked] == [0, 1]


# --- #11 — scanned-page recognition and figure description ---------------------------


@pytest.mark.asyncio
async def test_recognition_keys_carry_the_address_and_the_clients_use_it(
    custom_calls: list[str], shared_http: httpx.AsyncClient
) -> None:
    keys = ResolvingProviderKeys(resolver=_resolver("glm"), secret_store=_secrets())
    credential = await keys(tenant_id=_TENANT, provider="glm")
    assert credential == RecognitionCredential(api_key="sk-glm", base_url=_custom("glm"))

    layout = default_client(shared_http)(ModelChoice("glm", "glm-ocr"), credential)
    await layout.read(b"\xff\xd8image")
    qwen = RecognitionCredential(api_key="sk-qwen", base_url=_custom("qwen"))
    chat = default_client(shared_http)(ModelChoice("qwen", "qwen3.7-flash"), qwen)
    await chat.read(b"\xff\xd8image")
    figure = default_figure_client(shared_http)(
        ModelChoice("qwen", "qwen3.7-flash"), qwen, "describe", "figure", 256
    )
    await figure.read(b"\xff\xd8image")

    assert custom_calls == [
        "https://p.example.com/api/paas/v4/layout_parsing",
        "https://p.example.com/x/compatible-mode/v1/chat/completions",
        "https://p.example.com/x/compatible-mode/v1/chat/completions",
    ]


@pytest.mark.asyncio
async def test_recognition_at_the_official_address_still_uses_the_shared_client() -> None:
    seen: list[str] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return _reply(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(_handler)) as shared:
        client = default_client(shared)(
            ModelChoice("glm", "glm-ocr"), RecognitionCredential(api_key="k")
        )
        await client.read(b"\xff\xd8image")

    assert seen == ["https://open.bigmodel.cn/api/paas/v4/layout_parsing"]


# --- #12 — the eval scripts that run on platform credentials ---------------------------


@pytest.fixture
def eval_tools() -> None:
    root = str(_REPO)
    if root not in sys.path:
        sys.path.insert(0, root)


def _settings() -> Settings:
    return Settings(env="dev", auth_mode="dev", db_dsn="postgresql+asyncpg://test@localhost/test")


async def _platform_secrets() -> PlatformSecretsService:
    store = InMemoryPlatformSecretStore()
    await store.upsert_provider(
        provider="openai",
        secret_ref="secret://k/openai",
        enabled=True,
        actor_id="t",
        base_url=_custom("openai"),
    )
    await store.upsert_provider(
        provider="glm",
        secret_ref="secret://k/glm",
        enabled=True,
        actor_id="t",
        base_url=_custom("glm"),
    )
    return PlatformSecretsService(store=store, settings=_settings())


@pytest.mark.asyncio
@pytest.mark.usefixtures("eval_tools")
async def test_inpod_eval_resolver_sends_embeddings_and_aux_calls_to_the_address(
    custom_calls: list[str], shared_http: httpx.AsyncClient
) -> None:
    from tools.eval.b300_inpod import platform_credentials_resolver

    secrets = await _platform_secrets()
    resolver = platform_credentials_resolver(secrets, _settings(), _TenantConfig())
    assert await resolver.resolve_provider_key(tenant_id=_TENANT, provider="openai") == (
        ProviderKey("secret://k/openai", _custom("openai"))
    )
    embedder = DynamicResolvingEmbedder(
        config_service=_RerankConfig("glm", "embedding-3"),  # type: ignore[arg-type]
        resolver=resolver,
        secret_store=_secrets(),
        http=shared_http,
    )
    await embedder.embed(["hi"], tenant_id=_TENANT)
    aux = make_llm_router_aux_model(
        resolver=resolver,
        secret_store=_secrets(),
        default_provider="openai",
        default_model="gpt-4o",
    )
    await aux(prompt="p", model=None, tenant_id=_TENANT)

    assert custom_calls == [
        "https://p.example.com/api/paas/v4/embeddings",
        "https://p.example.com/v1/chat/completions",
    ]


@pytest.mark.asyncio
@pytest.mark.usefixtures("eval_tools")
@pytest.mark.parametrize("tenant_id", [None, str(_TENANT)])
async def test_compaction_eval_key_resolver_sends_the_router_to_the_address(
    tenant_id: str | None, custom_calls: list[str], shared_http: httpx.AsyncClient
) -> None:
    from tools.eval.compaction_retention import platform_provider_key_resolver

    resolve = platform_provider_key_resolver(await _platform_secrets(), tenant_id)
    assert await resolve("openai") == [ProviderKey("secret://k/openai", _custom("openai"))]
    assert await resolve("kimi") == []
    router = await build_llm_router(
        ModelSpec(provider="openai", name="gpt-4o"),
        secret_store=_secrets(),
        provider_key_resolver=resolve,
        http_client=shared_http,
    )
    await router(messages=[HumanMessage(content="hi")], tools=[])

    assert custom_calls == ["https://p.example.com/v1/chat/completions"]


# --- an unconverted caller fails loudly ---------------------------------------------


class _UnconvertedResolver:
    """A resolver layer that still answers ref-only: with a custom-address key
    it raises instead of handing the key out without its address."""

    async def resolve_provider_key(self, *, tenant_id: UUID, provider: str) -> ProviderKey:
        raise ProviderKeyNeedsEndpointError(provider)

    async def resolve_provider_key_list(
        self, *, tenant_id: UUID, provider: str
    ) -> list[ProviderKey]:
        raise ProviderKeyNeedsEndpointError(provider)


@pytest.mark.asyncio
async def test_endpoint_error_is_not_read_as_no_credential_by_the_agent_build() -> None:
    resolve = make_provider_key_resolver(resolver=_UnconvertedResolver(), tenant_id=_TENANT)
    with pytest.raises(ProviderKeyNeedsEndpointError):
        await resolve("openai")


@pytest.mark.asyncio
@pytest.mark.parametrize("which", [0, 1])
async def test_endpoint_error_is_not_read_as_no_credential_by_the_rerankers(which: int) -> None:
    """Both rerankers catch ``CredentialsResolverError`` and fall back to the
    recall order ("no credential"). The endpoint error must not take that exit."""
    rerankers: list[Any] = [
        ResolvingReranker(
            resolver=_UnconvertedResolver(),
            secret_store=_secrets(),
            provider="qwen",
            model="qwen3-vl-rerank",
        ),
        DynamicResolvingReranker(
            config_service=_RerankConfig("qwen", "qwen3-vl-rerank"),  # type: ignore[arg-type]
            resolver=_UnconvertedResolver(),
            secret_store=_secrets(),
        ),
    ]
    reranker = rerankers[which]
    with pytest.raises(ProviderKeyNeedsEndpointError):
        await reranker.rerank_scored(query="q", documents=["a", "b"], top_k=2, tenant_id=uuid4())


# --- structural guards ----------------------------------------------------------------

_SRC = _REPO / "services/control-plane/src/control_plane"
_REF_ONLY_METHODS = {"resolve_provider", "resolve_provider_keys"}


def _source_trees() -> list[tuple[pathlib.Path, ast.AST]]:
    files = sorted(_SRC.rglob("*.py"))
    assert len(files) > 100  # the scan really covers the package
    return [(path, ast.parse(path.read_text())) for path in files]


def test_no_control_plane_code_resolves_a_provider_key_without_its_address() -> None:
    """Non-test control-plane code must not call the ref-only resolver methods:
    a key resolved that way has lost its request address. (The overlay
    resolver's own definitions of them are not calls.)"""
    offenders = [
        f"{path.relative_to(_REPO)}:{node.lineno}"
        for path, tree in _source_trees()
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in _REF_ONLY_METHODS
    ]
    assert offenders == []


def _pins_api_key_ref(call: ast.Call) -> bool:
    func = call.func
    if isinstance(func, ast.Name) and func.id == "ModelSpec":
        return any(kw.arg == "api_key_ref" for kw in call.keywords)
    if (
        isinstance(func, ast.Attribute)
        and func.attr == "model_validate"
        and isinstance(func.value, ast.Name)
        and func.value.id == "ModelSpec"
    ):
        return any(
            isinstance(arg, ast.Dict)
            and any(isinstance(k, ast.Constant) and k.value == "api_key_ref" for k in arg.keys)
            for arg in call.args
        )
    return False


def test_no_control_plane_code_pins_a_key_into_a_model_spec() -> None:
    """``ModelSpec.api_key_ref`` carries a ref and no address: a platform key
    pinned that way always goes to the official host. Callers pin a
    ``ProviderKey`` through ``provider_key_resolver`` instead."""
    offenders = [
        f"{path.relative_to(_REPO)}:{node.lineno}"
        for path, tree in _source_trees()
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and _pins_api_key_ref(node)
    ]
    assert offenders == []


def test_the_structural_guards_see_what_they_are_looking_for() -> None:
    sample = ast.parse(
        "ref = await resolver.resolve_provider(tenant_id=t, provider=p)\n"
        "refs = await self.resolver.resolve_provider_keys(tenant_id=t, provider=p)\n"
        "a = ModelSpec(provider=p, name=n, api_key_ref=ref)\n"
        "b = ModelSpec.model_validate({'provider': p, 'name': n, 'api_key_ref': ref})\n"
        "c = ModelSpec(provider=p, name=n)\n"
    )
    calls = [node for node in ast.walk(sample) if isinstance(node, ast.Call)]
    ref_only = [
        c for c in calls if isinstance(c.func, ast.Attribute) and c.func.attr in _REF_ONLY_METHODS
    ]
    assert len(ref_only) == 2
    assert len([c for c in calls if _pins_api_key_ref(c)]) == 2
