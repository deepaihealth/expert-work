"""embed/rerank/aux/judge 的 vault 读接 ``CredentialValueCache`` — 二期 PR2 T3.

4 个 Resolving 类的 ``secret_store.get`` 换 ``_cached_secret``(命中不打
vault);aux_model_adapter / quality_judge 没有直读 ``secret_store.get``
——它们的 vault 读在 ``build_llm_router`` 内部(agent_factory.py:1956),
所以经 :class:`CachingSecretStore`(tenant 绑定的 SecretStore 包装)接入。

覆盖:

* embed(静态 + Dynamic)两次调用只打一次 vault;TTL 过期 / 显式
  ``invalidate_all`` 后重新拉;cache=None(未接线)行为与现状一致;
* rerank DashScope 分支同断言;LLM-rerank 分支不经 cache(``build_llm_router``
  路径照旧直读,cache 保持空);
* :class:`CachingSecretStore` 命中 / version 直通 / 写透传后驱逐 /
  repr 不泄漏 inner;vault 抛错不污染缓存(两条读路径);
* aux adapter / quality judge 两次调用只打一次 vault;
* 与 T2 的失效链闭环:平台 PUT → cache 清空 → 下次 ``_cached_secret`` miss
  重新打 vault。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from langchain_core.messages import AIMessage

from control_plane.app import create_app
from control_plane.auth import JWTVerifier
from control_plane.aux_model_adapter import make_llm_router_aux_model
from control_plane.credential_value_cache import CachingSecretStore, CredentialValueCache
from control_plane.quality_judge import QualityJudge
from control_plane.runtime import (
    DynamicResolvingEmbedder,
    DynamicResolvingReranker,
    ResolvingEmbedder,
    ResolvingReranker,
    _cached_secret,
)
from control_plane.settings import Settings
from expert_work.common.credentials import CredentialsResolver
from expert_work.common.lifecycle import Lifecycle
from expert_work.protocol import Role, TenantConfigRecord, TenantPlan
from expert_work.runtime.secret_store import parse_secret_ref
from tests.auth_fixtures import make_test_jwt

_REF = "secret://platform/qwen"


# ─── fakes ─────────────────────────────────────────────────────────────


class _CountingSecretStore:
    """记录每次 vault 读的 fake——计数即断言。"""

    def __init__(self, value: str = "sk-vault") -> None:
        self.value = value
        self.gets: list[str] = []
        self.puts: list[tuple[str, str]] = []
        self.deletes: list[str] = []

    async def get(self, name: str, *, version: str | None = None) -> str:
        self.gets.append(name)
        return self.value

    async def put(self, name: str, value: str) -> None:
        self.puts.append((name, value))

    async def list_versions(self, name: str) -> list[str]:
        return ["v1"]

    async def delete(self, name: str) -> None:
        self.deletes.append(name)


class _RaisingSecretStore:
    """vault 永远抛错的 fake——验证异常不污染缓存。"""

    async def get(self, name: str, *, version: str | None = None) -> str:
        raise RuntimeError("vault down")

    async def put(self, name: str, value: str) -> None:
        raise RuntimeError("vault down")

    async def list_versions(self, name: str) -> list[str]:
        raise RuntimeError("vault down")

    async def delete(self, name: str) -> None:
        raise RuntimeError("vault down")


class _Clock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class _TenantConfig:
    def __init__(self, record: TenantConfigRecord) -> None:
        self._record = record

    async def get(self, *, tenant_id: UUID) -> TenantConfigRecord:
        return self._record


def _resolver() -> CredentialsResolver:
    now = datetime.now(UTC)
    record = TenantConfigRecord(
        tenant_id=uuid4(),
        display_name="Acme",
        plan=TenantPlan.FREE,
        credentials_mode="platform",
        created_at=now,
        updated_at=now,
        updated_by="tester",
    )
    return CredentialsResolver(
        platform_provider_credentials={"qwen": _REF},  # type: ignore[arg-type]
        platform_tool_credentials={},  # type: ignore[arg-type]
        tenant_config_getter=_TenantConfig(record),
    )


class _FakeEmbedDelegate:
    """替身 ``OpenAICompatibleEmbedder``——不碰网络。"""

    def __init__(self, *, client: Any, model: str) -> None:
        self.client = client
        self.model = model

    async def embed(self, texts: Sequence[str], *, tenant_id: UUID) -> list[tuple[float, ...]]:
        return [(1.0,)] * len(texts)


class _FakeDashScopeReranker:
    def __init__(self, *, client: Any, model: str) -> None:
        self.client = client
        self.model = model

    async def rerank(
        self, *, query: str, documents: Sequence[str], top_k: int, tenant_id: UUID
    ) -> list[int]:
        return list(range(len(documents)))[:top_k]


class _FakeLLMReranker:
    def __init__(self, *, llm_caller: Any) -> None:
        self.llm_caller = llm_caller

    async def rerank(
        self, *, query: str, documents: Sequence[str], top_k: int, tenant_id: UUID
    ) -> list[int]:
        return list(range(len(documents)))[:top_k]


@pytest.fixture
def no_network_embed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("control_plane.runtime.OpenAICompatibleEmbedder", _FakeEmbedDelegate)


@pytest.fixture
def no_network_rerank(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("control_plane.runtime.DashScopeReranker", _FakeDashScopeReranker)
    monkeypatch.setattr("control_plane.runtime.LLMReranker", _FakeLLMReranker)


def _cache(clock: _Clock) -> CredentialValueCache:
    return CredentialValueCache(clock=clock)


# ─── ResolvingEmbedder ─────────────────────────────────────────────────


def _embedder(store: _CountingSecretStore, cache: CredentialValueCache | None) -> ResolvingEmbedder:
    return ResolvingEmbedder(
        resolver=_resolver(),
        secret_store=store,  # type: ignore[arg-type]
        provider="qwen",
        model="text-embedding-v4",
        secret_cache=cache,
    )


@pytest.mark.asyncio
@pytest.mark.usefixtures("no_network_embed")
async def test_embedder_second_call_hits_cache() -> None:
    store = _CountingSecretStore()
    embedder = _embedder(store, _cache(_Clock()))
    tenant = uuid4()
    await embedder.embed(["a"], tenant_id=tenant)
    await embedder.embed(["b"], tenant_id=tenant)
    assert store.gets == [parse_secret_ref(_REF)]  # 第二次命中缓存,vault 只打一次


@pytest.mark.asyncio
@pytest.mark.usefixtures("no_network_embed")
async def test_embedder_ttl_expiry_refetches() -> None:
    store = _CountingSecretStore()
    clock = _Clock()
    embedder = _embedder(store, _cache(clock))
    tenant = uuid4()
    await embedder.embed(["a"], tenant_id=tenant)
    clock.now += 300.0  # 默认 TTL 300s,拨表过期
    await embedder.embed(["b"], tenant_id=tenant)
    assert len(store.gets) == 2


@pytest.mark.asyncio
@pytest.mark.usefixtures("no_network_embed")
async def test_embedder_without_cache_reads_vault_every_call() -> None:
    # cache=None(未接线)= 现状行为:每次都打 vault。
    store = _CountingSecretStore()
    embedder = _embedder(store, None)
    tenant = uuid4()
    await embedder.embed(["a"], tenant_id=tenant)
    await embedder.embed(["b"], tenant_id=tenant)
    assert len(store.gets) == 2


@pytest.mark.asyncio
@pytest.mark.usefixtures("no_network_embed")
async def test_embedder_invalidate_all_forces_refetch() -> None:
    store = _CountingSecretStore()
    cache = _cache(_Clock())
    embedder = _embedder(store, cache)
    tenant = uuid4()
    await embedder.embed(["a"], tenant_id=tenant)
    cache.invalidate_all()
    await embedder.embed(["b"], tenant_id=tenant)
    assert len(store.gets) == 2


# ─── DynamicResolvingEmbedder / DynamicResolvingReranker ───────────────


class _FakeEmbeddingConfig:
    async def effective_embedding_config(self) -> tuple[str, str]:
        return ("qwen", "text-embedding-v4")

    async def effective_rerank_config(self) -> tuple[str, str]:
        return ("qwen", "gte-rerank")


@pytest.mark.asyncio
@pytest.mark.usefixtures("no_network_embed")
async def test_dynamic_embedder_second_call_hits_cache() -> None:
    store = _CountingSecretStore()
    embedder = DynamicResolvingEmbedder(
        config_service=_FakeEmbeddingConfig(),  # type: ignore[arg-type]
        resolver=_resolver(),
        secret_store=store,  # type: ignore[arg-type]
        secret_cache=_cache(_Clock()),
    )
    tenant = uuid4()
    await embedder.embed(["a"], tenant_id=tenant)
    await embedder.embed(["b"], tenant_id=tenant)
    assert store.gets == [parse_secret_ref(_REF)]


@pytest.mark.asyncio
@pytest.mark.usefixtures("no_network_rerank")
async def test_dynamic_reranker_dashscope_branch_hits_cache() -> None:
    store = _CountingSecretStore()
    reranker = DynamicResolvingReranker(
        config_service=_FakeEmbeddingConfig(),  # type: ignore[arg-type]
        resolver=_resolver(),
        secret_store=store,  # type: ignore[arg-type]
        secret_cache=_cache(_Clock()),
    )
    tenant = uuid4()
    await reranker.rerank(query="q", documents=["a", "b"], top_k=2, tenant_id=tenant)
    await reranker.rerank(query="q", documents=["a", "b"], top_k=2, tenant_id=tenant)
    assert store.gets == [parse_secret_ref(_REF)]


# ─── ResolvingReranker(DashScope 分支缓存;LLM 分支不经 cache)────────


@pytest.mark.asyncio
@pytest.mark.usefixtures("no_network_rerank")
async def test_reranker_dashscope_branch_hits_cache() -> None:
    store = _CountingSecretStore()
    reranker = ResolvingReranker(
        resolver=_resolver(),
        secret_store=store,  # type: ignore[arg-type]
        provider="qwen",
        model="gte-rerank",
        secret_cache=_cache(_Clock()),
    )
    tenant = uuid4()
    await reranker.rerank(query="q", documents=["a", "b"], top_k=2, tenant_id=tenant)
    await reranker.rerank(query="q", documents=["a", "b"], top_k=2, tenant_id=tenant)
    assert store.gets == [parse_secret_ref(_REF)]


@pytest.mark.asyncio
@pytest.mark.usefixtures("no_network_rerank")
async def test_reranker_llm_branch_not_cached() -> None:
    """LLM-rerank 分支走 ``build_llm_router`` 直读(plan 拍定不穿透)——
    即使配了 secret_cache,vault 每次照打,cache 保持空。"""
    store = _CountingSecretStore()
    cache = _cache(_Clock())
    reranker = ResolvingReranker(
        resolver=_resolver(),
        secret_store=store,  # type: ignore[arg-type]
        provider="qwen",
        model="qwen-plus",  # 无 "rerank" → LLM 分支
        secret_cache=cache,
    )
    tenant = uuid4()
    await reranker.rerank(query="q", documents=["a", "b"], top_k=2, tenant_id=tenant)
    await reranker.rerank(query="q", documents=["a", "b"], top_k=2, tenant_id=tenant)
    assert len(store.gets) == 2  # build_llm_router 路径照旧直读
    assert len(cache) == 0  # 不污染 cache


# ─── CachingSecretStore ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_caching_secret_store_second_read_hits_cache() -> None:
    inner = _CountingSecretStore()
    tenant = uuid4()
    store = CachingSecretStore(inner=inner, cache=_cache(_Clock()), tenant_id=tenant)  # type: ignore[arg-type]
    assert await store.get("platform/qwen") == "sk-vault"
    assert await store.get("platform/qwen") == "sk-vault"
    assert inner.gets == ["platform/qwen"]


@pytest.mark.asyncio
async def test_caching_secret_store_version_pinned_bypasses_cache() -> None:
    inner = _CountingSecretStore()
    cache = _cache(_Clock())
    store = CachingSecretStore(inner=inner, cache=cache, tenant_id=uuid4())  # type: ignore[arg-type]
    await store.get("platform/qwen", version="v1")
    await store.get("platform/qwen", version="v1")
    assert len(inner.gets) == 2  # 指定 version ≠ latest,不缓存
    assert len(cache) == 0


@pytest.mark.asyncio
async def test_caching_secret_store_writes_pass_through() -> None:
    inner = _CountingSecretStore()
    store = CachingSecretStore(inner=inner, cache=_cache(_Clock()), tenant_id=uuid4())  # type: ignore[arg-type]
    await store.put("n", "v")
    await store.delete("n")
    assert inner.puts == [("n", "v")]
    assert inner.deletes == ["n"]
    assert await store.list_versions("n") == ["v1"]


@pytest.mark.asyncio
async def test_caching_secret_store_put_and_delete_evict_cached_value() -> None:
    """写后驱逐:put/delete 透传后清本 tenant 缓存,读不到写前旧值。"""
    inner = _CountingSecretStore()
    cache = _cache(_Clock())
    store = CachingSecretStore(inner=inner, cache=cache, tenant_id=uuid4())  # type: ignore[arg-type]
    assert await store.get("platform/qwen") == "sk-vault"
    inner.value = "sk-rotated"
    await store.put("platform/qwen", "sk-rotated")
    assert await store.get("platform/qwen") == "sk-rotated"  # 驱逐 → miss → 重读
    assert len(inner.gets) == 2
    await store.delete("platform/qwen")
    assert len(cache) == 0


def test_caching_secret_store_repr_does_not_leak_inner() -> None:
    """dataclass 合成 repr 会递归吐 inner 的明文——repr=False 掐掉。"""

    class _LeakyReprStore(_CountingSecretStore):
        def __repr__(self) -> str:
            return "LeakyStore({'platform/qwen': 'sk-vault'})"

    store = CachingSecretStore(inner=_LeakyReprStore(), cache=_cache(_Clock()), tenant_id=uuid4())  # type: ignore[arg-type]
    assert "sk-vault" not in repr(store)


@pytest.mark.asyncio
async def test_caching_secret_store_error_does_not_pollute_cache() -> None:
    cache = _cache(_Clock())
    store = CachingSecretStore(inner=_RaisingSecretStore(), cache=cache, tenant_id=uuid4())  # type: ignore[arg-type]
    with pytest.raises(RuntimeError, match="vault down"):
        await store.get("platform/qwen")
    assert len(cache) == 0


@pytest.mark.asyncio
async def test_cached_secret_error_does_not_pollute_cache() -> None:
    cache = _cache(_Clock())
    with pytest.raises(RuntimeError, match="vault down"):
        await _cached_secret(cache, _RaisingSecretStore(), uuid4(), _REF)  # type: ignore[arg-type]
    assert len(cache) == 0


# ─── aux adapter / quality judge(vault 读在 build_llm_router 内部)────


class _FakeAuxResolver:
    async def resolve_provider(self, *, tenant_id: Any, provider: Any) -> str:
        return "secret://provider/anthropic"


def _patch_store_reading_router(monkeypatch: pytest.MonkeyPatch, response: AIMessage) -> None:
    """替身 ``build_llm_router``——照真身(agent_factory.py:1956)读一次
    ``secret_store``,返回 fake router。"""

    async def _fake_router(*, messages: Any, tools: Any, output_schema: Any = None) -> AIMessage:
        return response

    async def _fake_build(spec: Any, *, secret_store: Any, **kwargs: Any) -> Any:
        await secret_store.get(parse_secret_ref(spec.api_key_ref))
        return _fake_router

    monkeypatch.setattr("orchestrator.build_llm_router", _fake_build, raising=True)


@pytest.mark.asyncio
async def test_aux_adapter_second_call_hits_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _CountingSecretStore()
    _patch_store_reading_router(
        monkeypatch,
        AIMessage(
            content="ok",
            usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
        ),
    )
    aux = make_llm_router_aux_model(
        resolver=_FakeAuxResolver(),  # type: ignore[arg-type]
        secret_store=store,  # type: ignore[arg-type]
        default_provider="anthropic",
        default_model="claude-haiku-4-5-20251001",
        secret_cache=_cache(_Clock()),
    )
    tenant = uuid4()
    reply = await aux(prompt="p", model=None, tenant_id=tenant)
    assert reply.text == "ok"
    await aux(prompt="p2", model=None, tenant_id=tenant)
    assert store.gets == [parse_secret_ref("secret://provider/anthropic")]


@pytest.mark.asyncio
async def test_aux_adapter_without_cache_reads_every_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _CountingSecretStore()
    _patch_store_reading_router(monkeypatch, AIMessage(content="ok"))
    aux = make_llm_router_aux_model(
        resolver=_FakeAuxResolver(),  # type: ignore[arg-type]
        secret_store=store,  # type: ignore[arg-type]
        default_provider="anthropic",
        default_model="m",
    )
    tenant = uuid4()
    await aux(prompt="p", model=None, tenant_id=tenant)
    await aux(prompt="p", model=None, tenant_id=tenant)
    assert len(store.gets) == 2


_VERDICT = AIMessage(
    content="",
    additional_kwargs={
        "parsed": {
            "overall": 4,
            "addressed_request": 5,
            "coherence": 4,
            "safety": 5,
            "rationale": "fine",
        }
    },
    usage_metadata={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
)


@pytest.mark.asyncio
async def test_quality_judge_second_score_hits_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _CountingSecretStore()
    _patch_store_reading_router(monkeypatch, _VERDICT)
    judge = QualityJudge(
        resolver=_FakeAuxResolver(),  # type: ignore[arg-type]
        secret_store=store,  # type: ignore[arg-type]
        secret_cache=_cache(_Clock()),
    )
    tenant = uuid4()
    first = await judge.score(
        tenant_id=tenant, prompt="q", reply="a", provider="anthropic", model="m"
    )
    assert first is not None  # score 吞异常返 None——必须防「静默全跳」假绿
    second = await judge.score(
        tenant_id=tenant, prompt="q", reply="a", provider="anthropic", model="m"
    )
    assert second is not None
    assert store.gets == [parse_secret_ref("secret://provider/anthropic")]


# ─── 与 T2 失效链闭环:平台 PUT → cache 清空 → 下次 miss ────────────────


@pytest.mark.asyncio
async def test_platform_put_clears_cache_and_next_read_refetches(
    settings: Settings,
    lifecycle: Lifecycle,
    jwt_verifier: JWTVerifier,
) -> None:
    app = create_app(settings=settings, lifecycle=lifecycle, jwt_verifier=jwt_verifier)
    admin = uuid4()
    await app.state.role_binding_repo.create(
        subject_type="user",
        subject_id=admin,
        tenant_id=None,
        role=Role.SYSTEM_ADMIN,
        platform_scope=True,
        granted_by="seed",
    )
    cache = CredentialValueCache()
    app.state.credential_value_cache = cache
    tenant = uuid4()
    cache.put(tenant, _REF, "old-key")
    assert cache.get(tenant, _REF) == "old-key"

    headers = {"Authorization": f"Bearer {make_test_jwt(tenant_id=uuid4(), subject=str(admin))}"}
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://control-plane.test") as client:
        resp = await client.put(
            "/v1/platform/credentials/providers/anthropic",
            json={"secret_ref": "kms://platform/x", "enabled": True},
            headers=headers,
        )
    assert resp.status_code == 200, resp.text
    assert len(cache) == 0  # T2 的 _invalidate_built_agents 连动清空
    assert cache.get(tenant, _REF) is None  # 下次 miss

    store = _CountingSecretStore(value="new-key")
    assert await _cached_secret(cache, store, tenant, _REF) == "new-key"  # type: ignore[arg-type]
    assert store.gets == [parse_secret_ref(_REF)]  # miss 后重新打 vault 并回填
    assert cache.get(tenant, _REF) == "new-key"
