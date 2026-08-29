"""Tests for control-plane ↔ orchestrator runtime glue."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from control_plane.api.agents import _spec_sha256
from control_plane.runtime import (
    AgentRuntime,
    ResolvingEmbedder,
    ResolvingReranker,
    _make_action_judge,
    _make_output_judge,
    make_agent_builder,
    make_image_resolver,
    make_knowledge_retriever,
    resolve_embedder,
    resolve_object_store_config,
    resolve_reranker,
)
from expert_work.common.credentials import CredentialsResolver, CredentialsResolverError
from expert_work.persistence import InMemoryKnowledgeStore
from expert_work.persistence.platform_agent_template import compute_spec_sha256
from expert_work.persistence.skill import InMemorySkillStore
from expert_work.protocol import AgentSpec, SkillStatus, TenantConfigRecord, TenantPlan
from expert_work.runtime.runs import RunManager
from expert_work.runtime.secret_store import LocalDevSecretStore, parse_secret_ref
from expert_work.runtime.storage import InMemoryObjectStore
from expert_work.runtime.stream_bridge import InMemoryStreamBridge
from expert_work.testing import InMemorySecretStore
from orchestrator.llm import FakeEmbedder
from orchestrator.multimodal import CachingImageResolver, ObjectStoreImageResolver
from orchestrator.tools import KnowledgeRetriever

_MINIMAL_MANIFEST: dict[str, Any] = {
    "apiVersion": "expert_work.io/v1",
    "kind": "Agent",
    "metadata": {"name": "x", "version": "1", "tenant": "test-tenant"},
    "spec": {
        "tenant_config": {},
        "model": {"provider": "anthropic", "name": "claude-haiku-4-5"},
        "system_prompt": {"template": "you help"},
        "sandbox": {
            "resources": {"cpu": "1.0", "memory": "1Gi"},
            "network": {"egress": "none", "allowlist": []},
            "filesystem": {"readonly_root": True, "writable": []},
        },
    },
}


def _make_spec(*, name: str = "x", version: str = "1", prompt: str = "you help") -> AgentSpec:
    manifest = dict(_MINIMAL_MANIFEST)
    manifest["metadata"] = dict(manifest["metadata"], name=name, version=version)
    manifest["spec"] = dict(manifest["spec"], system_prompt={"template": prompt})
    return AgentSpec.model_validate(manifest)


class _NeverCalledTenantConfig:
    """``resolve_embedder`` / ``resolve_reranker`` are pure factories — they
    never touch the tenant config, so this getter is never invoked."""

    async def get(self, *, tenant_id: UUID) -> TenantConfigRecord:  # pragma: no cover
        raise AssertionError("factory must not resolve tenant config")


def _resolver() -> CredentialsResolver:
    return CredentialsResolver(
        platform_provider_credentials={},  # type: ignore[arg-type]
        platform_tool_credentials={},  # type: ignore[arg-type]
        tenant_config_getter=_NeverCalledTenantConfig(),
    )


@pytest.mark.asyncio
async def test_resolve_embedder_unsupported_provider_returns_none() -> None:
    """Embedding provider absent from the catalog → no embedder → long-term
    memory globally unavailable (build-time gate preserved, Mini-ADR O-11)."""
    embedder = await resolve_embedder(
        resolver=_resolver(),
        secret_store=InMemorySecretStore(),
        provider="qwen",
        model="text-embedding-v4",
        supported_providers=[],
    )
    assert embedder is None


@pytest.mark.asyncio
async def test_resolve_embedder_builds_resolving_embedder() -> None:
    embedder = await resolve_embedder(
        resolver=_resolver(),
        secret_store=InMemorySecretStore(),
        provider="qwen",
        model="text-embedding-v4",
        supported_providers=["qwen"],
    )
    assert isinstance(embedder, ResolvingEmbedder)
    assert embedder.model == "text-embedding-v4"
    assert embedder.provider == "qwen"


@pytest.mark.asyncio
async def test_resolve_reranker_unsupported_provider_returns_none() -> None:
    """Rerank provider absent from the catalog → no reranker → hybrid search
    returns the fused order."""
    reranker = await resolve_reranker(
        resolver=_resolver(),
        secret_store=InMemorySecretStore(),
        provider="qwen",
        model="qwen-plus",
        supported_providers=[],
    )
    assert reranker is None


@pytest.mark.asyncio
async def test_resolve_reranker_builds_resolving_reranker() -> None:
    reranker = await resolve_reranker(
        resolver=_resolver(),
        secret_store=InMemorySecretStore(),
        provider="qwen",
        model="qwen-plus",
        supported_providers=["qwen"],
    )
    assert isinstance(reranker, ResolvingReranker)
    assert reranker.provider == "qwen"


def test_make_knowledge_retriever_none_without_embedder() -> None:
    retriever = make_knowledge_retriever(
        store=InMemoryKnowledgeStore(), embedder=None, reranker=None
    )
    assert retriever is None


def test_make_knowledge_retriever_builds_with_embedder() -> None:
    retriever = make_knowledge_retriever(
        store=InMemoryKnowledgeStore(), embedder=FakeEmbedder(), reranker=None
    )
    assert isinstance(retriever, KnowledgeRetriever)


def test_make_image_resolver_builds_object_store_resolver() -> None:
    resolver = make_image_resolver(InMemoryObjectStore())
    # Wrapped in the bounded-LRU caching layer over the object-store resolver
    # (stops every LLM turn re-fetching the same image).
    assert isinstance(resolver, CachingImageResolver)
    assert isinstance(resolver.inner, ObjectStoreImageResolver)


@pytest.mark.asyncio
async def test_resolve_object_store_config_memory_returns_none() -> None:
    """The in-memory backend needs no S3 config."""
    config = await resolve_object_store_config(
        backend="memory",
        endpoint_url=None,
        region="us-east-1",
        bucket="expert-work",
        access_key_ref=None,
        secret_key_ref=None,
        secret_store=InMemorySecretStore(),
    )
    assert config is None


@pytest.mark.asyncio
async def test_resolve_object_store_config_s3_without_endpoint_raises() -> None:
    with pytest.raises(RuntimeError, match="s3-compatible"):
        await resolve_object_store_config(
            backend="s3-compatible",
            endpoint_url=None,
            region="us-east-1",
            bucket="expert-work",
            access_key_ref=None,
            secret_key_ref=None,
            secret_store=InMemorySecretStore(),
        )


@pytest.mark.asyncio
async def test_resolve_object_store_config_s3_resolves_keys() -> None:
    store = InMemorySecretStore()
    await store.put(parse_secret_ref("secret://expert-work/dev/s3-access"), "AKID")
    await store.put(parse_secret_ref("secret://expert-work/dev/s3-secret"), "SKEY")

    config = await resolve_object_store_config(
        backend="s3-compatible",
        endpoint_url="http://minio:9000",
        region="us-east-1",
        bucket="expert-work",
        access_key_ref="secret://expert-work/dev/s3-access",
        secret_key_ref="secret://expert-work/dev/s3-secret",
        secret_store=store,
    )
    assert config is not None
    assert config.endpoint_url == "http://minio:9000"
    assert config.access_key == "AKID"
    assert config.secret_key == "SKEY"
    # Default stays "path" — MinIO local dev keeps working unchanged.
    assert config.addressing_style == "path"


@pytest.mark.asyncio
async def test_resolve_object_store_config_passes_addressing_style() -> None:
    """OSS prod needs ``addressing_style="virtual"`` threaded through —
    W0 real-bucket finding: OSS rejects path-style addressing."""
    store = InMemorySecretStore()
    await store.put(parse_secret_ref("secret://expert-work/prod/s3-access"), "AKID")
    await store.put(parse_secret_ref("secret://expert-work/prod/s3-secret"), "SKEY")

    config = await resolve_object_store_config(
        backend="s3-compatible",
        endpoint_url="https://oss-cn-hangzhou.aliyuncs.com",
        region="cn-hangzhou",
        bucket="expert-work-prod",
        access_key_ref="secret://expert-work/prod/s3-access",
        secret_key_ref="secret://expert-work/prod/s3-secret",
        secret_store=store,
        addressing_style="virtual",
    )
    assert config is not None
    assert config.addressing_style == "virtual"


# ---------------------------------------------------------------------------
# AgentRuntime.invalidate_tenant (Stream V-D, Mini-ADR V-4)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_invalidate_tenant_drops_only_that_tenants_cached_agents() -> None:
    """invalidate_tenant(a) drops all entries for tenant a, leaves tenant b cached."""
    builds: list[tuple[UUID | None, str | None]] = []

    async def _builder(
        spec: AgentSpec, *, tenant_id: UUID | None = None, user_id: str | None = None
    ) -> object:
        builds.append((tenant_id, spec.metadata.name if spec is not None else None))
        return object()  # stand-in BuiltAgent

    runtime = AgentRuntime(
        run_manager=RunManager(store=None),  # type: ignore[arg-type]
        stream_bridge=InMemoryStreamBridge(),
        agent_builder=_builder,  # type: ignore[arg-type]
    )
    a, b = uuid4(), uuid4()
    spec = _make_spec(name="x", version="1")

    await runtime.get_agent(tenant_id=a, name="x", version="1", spec=spec)
    await runtime.get_agent(tenant_id=b, name="x", version="1", spec=spec)
    assert len(builds) == 2

    runtime.invalidate_tenant(a)

    # tenant a rebuilds; tenant b still cached (no new build)
    await runtime.get_agent(tenant_id=a, name="x", version="1", spec=spec)
    await runtime.get_agent(tenant_id=b, name="x", version="1", spec=spec)
    assert len(builds) == 3  # only a rebuilt


@pytest.mark.asyncio
async def test_invalidate_tenant_fans_out_to_registered_hooks() -> None:
    """Audit #1: invalidate_tenant fans out to registered hooks (e.g. the
    sub-agent builder cache, which caches built agents independently)."""

    async def _builder(
        spec: AgentSpec, *, tenant_id: UUID | None = None, user_id: str | None = None
    ) -> object:
        return object()

    runtime = AgentRuntime(
        run_manager=RunManager(store=None),  # type: ignore[arg-type]
        stream_bridge=InMemoryStreamBridge(),
        agent_builder=_builder,  # type: ignore[arg-type]
    )
    seen: list[UUID] = []
    runtime.register_invalidation_hook(seen.append)
    t = uuid4()
    runtime.invalidate_tenant(t)
    assert seen == [t]


# ---------------------------------------------------------------------------
# Content-addressed built-agent cache — an in-place manifest edit keeps
# ``(tenant, name, version)``, so identity alone cannot tell a stale build
# from a current one. Keying on the spec's content hash makes "the edit
# reaches the next run" structural instead of dependent on someone
# remembering to call ``invalidate_tenant`` (and on the Redis bus that
# broadcasts it to peer replicas actually being up).
# ---------------------------------------------------------------------------


def _recording_runtime(builds: list[str]) -> AgentRuntime:
    async def _builder(
        spec: AgentSpec, *, tenant_id: UUID | None = None, user_id: str | None = None
    ) -> object:
        builds.append(spec.spec.system_prompt.template)
        return object()  # stand-in BuiltAgent

    return AgentRuntime(
        run_manager=RunManager(store=None),  # type: ignore[arg-type]
        stream_bridge=InMemoryStreamBridge(),
        agent_builder=_builder,  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
async def test_get_agent_rebuilds_when_the_spec_content_changed() -> None:
    """Same ``(tenant, name, version)``, edited content → cache MISS.

    This is the in-place edit the config page performs: the caller has already
    read the current row from the DB, so serving it a build of the previous
    content is serving a config the operator has replaced."""
    builds: list[str] = []
    runtime = _recording_runtime(builds)
    t = uuid4()

    await runtime.get_agent(tenant_id=t, name="x", version="1", spec=_make_spec(prompt="old"))
    await runtime.get_agent(tenant_id=t, name="x", version="1", spec=_make_spec(prompt="new"))

    assert builds == ["old", "new"]


@pytest.mark.asyncio
async def test_get_agent_still_caches_identical_spec_content() -> None:
    """The other direction: content-keying must not degrade into "always
    rebuild" — an unchanged spec still hits the cache."""
    builds: list[str] = []
    runtime = _recording_runtime(builds)
    t = uuid4()

    await runtime.get_agent(tenant_id=t, name="x", version="1", spec=_make_spec(prompt="same"))
    await runtime.get_agent(tenant_id=t, name="x", version="1", spec=_make_spec(prompt="same"))

    assert builds == ["same"]


@pytest.mark.asyncio
async def test_get_agent_serves_the_edited_spec_after_a_revert() -> None:
    """Edit then revert: the pre-edit build may still be cached, and serving it
    is correct — but only because its content matches again, not because the
    identity does."""
    builds: list[str] = []
    runtime = _recording_runtime(builds)
    t = uuid4()

    await runtime.get_agent(tenant_id=t, name="x", version="1", spec=_make_spec(prompt="a"))
    await runtime.get_agent(tenant_id=t, name="x", version="1", spec=_make_spec(prompt="b"))
    await runtime.get_agent(tenant_id=t, name="x", version="1", spec=_make_spec(prompt="a"))

    assert builds == ["a", "b"]


def test_cache_key_hash_matches_the_stored_agent_spec_sha() -> None:
    """The cache key's hash and the ``agent_spec`` row's ``spec_sha256`` must be
    the same canonical form.

    Nothing breaks functionally if they drift — the cache would still be
    correct, keyed on its own hash. What breaks is every human use of the pair:
    the run row records the STORED sha (PR-B), so an operator asking "which
    config produced this run" compares two numbers that can no longer be
    compared. Pinning it here fails loudly at the moment either side changes
    its canonicalisation."""
    spec = _make_spec(prompt="canonical form check")
    assert compute_spec_sha256(spec) == _spec_sha256(spec.model_dump(by_alias=True, mode="json"))


# ---------------------------------------------------------------------------
# AgentRuntime.invalidate_all (Stream MCP platform-servers, P1b)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_invalidate_all_drops_every_tenants_cached_agents() -> None:
    """invalidate_all() drops the whole cache — a platform-pool change feeds
    every tenant, so every cached build must rebuild."""
    builds: list[UUID | None] = []

    async def _builder(
        spec: AgentSpec, *, tenant_id: UUID | None = None, user_id: str | None = None
    ) -> object:
        builds.append(tenant_id)
        return object()

    runtime = AgentRuntime(
        run_manager=RunManager(store=None),  # type: ignore[arg-type]
        stream_bridge=InMemoryStreamBridge(),
        agent_builder=_builder,  # type: ignore[arg-type]
    )
    a, b = uuid4(), uuid4()
    spec = _make_spec(name="x", version="1")
    await runtime.get_agent(tenant_id=a, name="x", version="1", spec=spec)
    await runtime.get_agent(tenant_id=b, name="x", version="1", spec=spec)
    assert len(builds) == 2

    runtime.invalidate_all()

    # both tenants rebuild
    await runtime.get_agent(tenant_id=a, name="x", version="1", spec=spec)
    await runtime.get_agent(tenant_id=b, name="x", version="1", spec=spec)
    assert len(builds) == 4


@pytest.mark.asyncio
async def test_invalidate_all_fans_out_to_registered_clear_hooks() -> None:
    """invalidate_all fans out to clear-all hooks (the sub-agent builder cache)."""

    async def _builder(
        spec: AgentSpec, *, tenant_id: UUID | None = None, user_id: str | None = None
    ) -> object:
        return object()

    runtime = AgentRuntime(
        run_manager=RunManager(store=None),  # type: ignore[arg-type]
        stream_bridge=InMemoryStreamBridge(),
        agent_builder=_builder,  # type: ignore[arg-type]
    )
    cleared: list[bool] = []
    runtime.register_invalidation_all(lambda: cleared.append(True))
    runtime.invalidate_all()
    assert cleared == [True]


# ---------------------------------------------------------------------------
# Stream MCP-OAUTH (OA-3b) — per-user build keying via user_oauth_pool_provider
# ---------------------------------------------------------------------------


def _runtime_with_oauth(builds: list[tuple[str | None, ...]], users_with_oauth: set[str]):  # type: ignore[no-untyped-def]
    from orchestrator.tools.mcp import MCPServerPool, MCPToolDef, RecordingMCPClient

    async def _builder(
        spec: AgentSpec, *, tenant_id: UUID | None = None, user_id: str | None = None
    ) -> object:
        builds.append((str(tenant_id), user_id))
        return object()

    async def _provider(tenant_id: UUID, user_id: str) -> MCPServerPool:
        pool = MCPServerPool()
        if user_id in users_with_oauth:
            await pool.add(
                "linear",
                RecordingMCPClient(tools=(MCPToolDef(name="t", description="", input_schema={}),)),
            )
        return pool

    return AgentRuntime(
        run_manager=RunManager(store=None),  # type: ignore[arg-type]
        stream_bridge=InMemoryStreamBridge(),
        agent_builder=_builder,  # type: ignore[arg-type]
        user_oauth_pool_provider=_provider,
    )


@pytest.mark.asyncio
async def test_get_agent_per_user_key_only_when_oauth_present() -> None:
    builds: list[tuple[str | None, ...]] = []
    runtime = _runtime_with_oauth(builds, users_with_oauth={"u-oauth"})
    t = uuid4()
    spec = _make_spec(name="x", version="1")

    # User WITH oauth → per-user build; same user re-uses the per-user cache.
    await runtime.get_agent(tenant_id=t, name="x", version="1", spec=spec, user_id="u-oauth")
    await runtime.get_agent(tenant_id=t, name="x", version="1", spec=spec, user_id="u-oauth")
    # User WITHOUT oauth → shared (no-oauth) build; another no-oauth user reuses it.
    await runtime.get_agent(tenant_id=t, name="x", version="1", spec=spec, user_id="u-plain")
    await runtime.get_agent(tenant_id=t, name="x", version="1", spec=spec, user_id="u-plain2")

    # 2 builds total: one per-user (u-oauth) + one shared (no-oauth users).
    assert len(builds) == 2


@pytest.mark.asyncio
async def test_invalidate_user_drops_only_that_users_per_user_agents() -> None:
    builds: list[tuple[str | None, ...]] = []
    runtime = _runtime_with_oauth(builds, users_with_oauth={"u1", "u2"})
    t = uuid4()
    spec = _make_spec(name="x", version="1")
    await runtime.get_agent(tenant_id=t, name="x", version="1", spec=spec, user_id="u1")
    await runtime.get_agent(tenant_id=t, name="x", version="1", spec=spec, user_id="u2")
    assert len(builds) == 2

    runtime.invalidate_user(t, "u1")

    # u1 rebuilds; u2 still cached.
    await runtime.get_agent(tenant_id=t, name="x", version="1", spec=spec, user_id="u1")
    await runtime.get_agent(tenant_id=t, name="x", version="1", spec=spec, user_id="u2")
    assert len(builds) == 3


# ---------------------------------------------------------------------------
# Stream X (Mini-ADR X-4) — make_agent_builder threads make_skill_resolver
# end-to-end so a skills manifest actually builds (it hard-failed before).
# ---------------------------------------------------------------------------

_ANTHROPIC_KEY_NAME = "anthropic-test"


class _StubTenantConfig:
    """Minimal tenant-config service returning FREE — exercises the real
    ``make_skill_resolver`` plan lookup without a DB."""

    async def get(self, *, tenant_id: UUID, actor_id: str | None = None) -> TenantConfigRecord:
        now = datetime.now(UTC)
        return TenantConfigRecord(
            tenant_id=tenant_id,
            display_name="t",
            plan=TenantPlan.FREE,
            created_at=now,
            updated_at=now,
            updated_by="test",
        )


def _spec_with_skills(skills: list[str] | None) -> AgentSpec:
    manifest = dict(_MINIMAL_MANIFEST)
    manifest["spec"] = dict(
        manifest["spec"],
        model={
            "provider": "anthropic",
            "name": "claude-haiku-4-5",
            "api_key_ref": f"secret://{_ANTHROPIC_KEY_NAME}",
        },
    )
    if skills is not None:
        manifest["spec"] = dict(manifest["spec"], skills=skills)
    return AgentSpec.model_validate(manifest)


def _anthropic_credentials_resolver() -> CredentialsResolver:
    """Stream Y-2 — agent builds resolve the LLM key via the platform
    credential, so the builder needs a resolver carrying the anthropic key
    (the manifest ``api_key_ref`` is ignored for agent builds)."""
    return CredentialsResolver(
        platform_provider_credentials={"anthropic": f"secret://{_ANTHROPIC_KEY_NAME}"},  # type: ignore[arg-type]
        platform_tool_credentials={},  # type: ignore[arg-type]
        tenant_config_getter=_StubTenantConfig(),  # type: ignore[arg-type]
    )


async def _seed_active_tenant_skill(
    store: InMemorySkillStore, *, tenant_id: UUID, name: str
) -> None:
    skill_id = uuid4()
    await store.create_skill(skill_id=skill_id, tenant_id=tenant_id, name=name)
    await store.add_version(
        version_id=uuid4(),
        skill_id=skill_id,
        tenant_id=tenant_id,
        prompt_fragment="SKILL-BODY",
        # Eager so the body is inlined — this test proves the injection wiring
        # (RT-ADR-11 made lazy the default, which would withhold the body).
        lazy_load=False,
    )
    await store.set_status(skill_id=skill_id, tenant_id=tenant_id, status=SkillStatus.ACTIVE)


@pytest.mark.asyncio
async def test_make_agent_builder_builds_agent_with_skill_injected() -> None:
    """End-to-end: a manifest declaring a skill now BUILDS and injects the
    skill body — proving the X-4 wiring through ``make_skill_resolver``."""
    tenant_id = uuid4()
    store = InMemorySkillStore()
    await _seed_active_tenant_skill(store, tenant_id=tenant_id, name="foo")
    builder = make_agent_builder(
        LocalDevSecretStore.from_mapping({_ANTHROPIC_KEY_NAME: "sk-ant-test"}),
        InMemorySaver(),
        skill_store=store,
        tenant_config_service=_StubTenantConfig(),  # type: ignore[arg-type]
        credentials_resolver=_anthropic_credentials_resolver(),
    )
    built = await builder(_spec_with_skills(["foo"]), tenant_id=tenant_id)
    assert "SKILL-BODY" in built.system_prompt


@pytest.mark.asyncio
async def test_make_agent_builder_no_skills_still_builds() -> None:
    """Regression — a no-skills manifest builds cleanly with the skill deps
    wired in."""
    store = InMemorySkillStore()
    builder = make_agent_builder(
        LocalDevSecretStore.from_mapping({_ANTHROPIC_KEY_NAME: "sk-ant-test"}),
        InMemorySaver(),
        skill_store=store,
        tenant_config_service=_StubTenantConfig(),  # type: ignore[arg-type]
        credentials_resolver=_anthropic_credentials_resolver(),
    )
    built = await builder(_spec_with_skills(None), tenant_id=uuid4())
    # PI-1: spotlighting on by default appends the untrusted-content clause.
    assert built.system_prompt.startswith("you help")


@pytest.mark.asyncio
async def test_make_agent_builder_skills_manifest_without_tenant_errors() -> None:
    """A preview / validation build (tenant_id None) gets no resolvers, so the
    build still hard-fails. Stream Y-2: with the manifest ``api_key_ref``
    ignored and no ``provider_key_resolver`` (no tenant), the credential gate
    is the first hard failure — the build is refused either way."""
    from orchestrator.errors import AgentFactoryError

    store = InMemorySkillStore()
    builder = make_agent_builder(
        LocalDevSecretStore.from_mapping({_ANTHROPIC_KEY_NAME: "sk-ant-test"}),
        InMemorySaver(),
        skill_store=store,
        tenant_config_service=_StubTenantConfig(),  # type: ignore[arg-type]
        credentials_resolver=_anthropic_credentials_resolver(),
    )
    with pytest.raises(AgentFactoryError, match="no platform credential"):
        await builder(_spec_with_skills(["foo"]), tenant_id=None)


# ---------------------------------------------------------------------------
# Stream PI-1c — BuiltAgent.spotlight_nonce is exposed so the control-plane
# seed assembler can fence structured untrusted_content with the build nonce.
# ---------------------------------------------------------------------------


def _spec_with_defenses(defenses: dict[str, str]) -> AgentSpec:
    manifest = dict(_MINIMAL_MANIFEST)
    manifest["spec"] = dict(
        manifest["spec"],
        model={
            "provider": "anthropic",
            "name": "claude-haiku-4-5",
            "api_key_ref": f"secret://{_ANTHROPIC_KEY_NAME}",
        },
        defenses=defenses,
    )
    return AgentSpec.model_validate(manifest)


def _spotlight_builder() -> Any:
    return make_agent_builder(
        LocalDevSecretStore.from_mapping({_ANTHROPIC_KEY_NAME: "sk-ant-test"}),
        InMemorySaver(),
        skill_store=InMemorySkillStore(),
        tenant_config_service=_StubTenantConfig(),  # type: ignore[arg-type]
        credentials_resolver=_anthropic_credentials_resolver(),
    )


@pytest.mark.asyncio
async def test_built_agent_exposes_spotlight_nonce_when_on() -> None:
    """Spotlighting on (default) → a non-empty build nonce is exposed for the
    PI-1c untrusted_content seed fence."""
    built = await _spotlight_builder()(
        _spec_with_defenses({"prompt_injection": "spotlight"}), tenant_id=uuid4()
    )
    assert built.spotlight_nonce
    assert len(built.spotlight_nonce) == 12


@pytest.mark.asyncio
async def test_built_agent_spotlight_nonce_none_when_off() -> None:
    built = await _spotlight_builder()(
        _spec_with_defenses({"prompt_injection": "off"}), tenant_id=uuid4()
    )
    assert built.spotlight_nonce is None


# ---------------------------------------------------------------------------
# Stream PI-2b-3 — _make_output_judge gating + construction
# ---------------------------------------------------------------------------


def _spec_with_judge(mode: str) -> AgentSpec:
    manifest = dict(_MINIMAL_MANIFEST)
    manifest["spec"] = dict(
        manifest["spec"],
        model={
            "provider": "anthropic",
            "name": "claude-haiku-4-5",
            "api_key_ref": f"secret://{_ANTHROPIC_KEY_NAME}",
        },
        defenses={"output_judge": mode},
    )
    return AgentSpec.model_validate(manifest)


@pytest.mark.asyncio
async def test_make_output_judge_off_returns_none() -> None:
    judge = await _make_output_judge(
        _spec_with_judge("off"),
        tenant_id=uuid4(),
        credentials_resolver=_anthropic_credentials_resolver(),
        secret_store=LocalDevSecretStore.from_mapping({_ANTHROPIC_KEY_NAME: "sk-ant-test"}),
    )
    assert judge is None


@pytest.mark.asyncio
async def test_make_output_judge_block_builds_llm_judge() -> None:
    from orchestrator import LLMOutputJudge

    judge = await _make_output_judge(
        _spec_with_judge("block"),
        tenant_id=uuid4(),
        credentials_resolver=_anthropic_credentials_resolver(),
        secret_store=LocalDevSecretStore.from_mapping({_ANTHROPIC_KEY_NAME: "sk-ant-test"}),
    )
    assert isinstance(judge, LLMOutputJudge)


class _FakeJudgeConfig:
    """Stub PlatformJudgeConfigService — returns a fixed (provider, model)."""

    def __init__(self, pair: tuple[str, str] | None) -> None:
        self._pair = pair

    async def effective_judge_config(self) -> tuple[str, str] | None:
        return self._pair


@pytest.mark.asyncio
async def test_make_output_judge_falls_back_to_agent_model_when_config_unset() -> None:
    from orchestrator import LLMOutputJudge

    # Platform config unset → uses the agent's own (anthropic) model, which has
    # a credential → builds fine.
    judge = await _make_output_judge(
        _spec_with_judge("block"),
        tenant_id=uuid4(),
        credentials_resolver=_anthropic_credentials_resolver(),
        secret_store=LocalDevSecretStore.from_mapping({_ANTHROPIC_KEY_NAME: "sk-ant-test"}),
        judge_config_service=_FakeJudgeConfig(None),  # type: ignore[arg-type]
    )
    assert isinstance(judge, LLMOutputJudge)


@pytest.mark.asyncio
async def test_make_output_judge_uses_platform_config_provider() -> None:
    # Platform config points the judge at a provider WITHOUT a credential, so
    # the resolve fails — proving the platform provider (not the agent's
    # credentialed one) was used.
    with pytest.raises(CredentialsResolverError):
        await _make_output_judge(
            _spec_with_judge("block"),
            tenant_id=uuid4(),
            credentials_resolver=_anthropic_credentials_resolver(),
            secret_store=LocalDevSecretStore.from_mapping({_ANTHROPIC_KEY_NAME: "sk-ant-test"}),
            judge_config_service=_FakeJudgeConfig(("openai", "gpt-4o")),  # type: ignore[arg-type]
        )


# ---------------------------------------------------------------------------
# Stream PI-3b-2 — _make_action_judge gating + construction
# ---------------------------------------------------------------------------


def _spec_with_action(mode: str) -> AgentSpec:
    manifest = dict(_MINIMAL_MANIFEST)
    manifest["spec"] = dict(
        manifest["spec"],
        model={
            "provider": "anthropic",
            "name": "claude-haiku-4-5",
            "api_key_ref": f"secret://{_ANTHROPIC_KEY_NAME}",
        },
        defenses={"action_screen": mode},
    )
    return AgentSpec.model_validate(manifest)


@pytest.mark.asyncio
async def test_make_action_judge_off_returns_none() -> None:
    judge = await _make_action_judge(
        _spec_with_action("off"),
        tenant_id=uuid4(),
        credentials_resolver=_anthropic_credentials_resolver(),
        secret_store=LocalDevSecretStore.from_mapping({_ANTHROPIC_KEY_NAME: "sk-ant-test"}),
    )
    assert judge is None


@pytest.mark.asyncio
async def test_make_action_judge_block_builds_llm_action_judge() -> None:
    from orchestrator import LLMActionJudge

    judge = await _make_action_judge(
        _spec_with_action("block"),
        tenant_id=uuid4(),
        credentials_resolver=_anthropic_credentials_resolver(),
        secret_store=LocalDevSecretStore.from_mapping({_ANTHROPIC_KEY_NAME: "sk-ant-test"}),
    )
    assert isinstance(judge, LLMActionJudge)


@pytest.mark.asyncio
async def test_make_action_judge_approval_also_builds() -> None:
    from orchestrator import LLMActionJudge

    judge = await _make_action_judge(
        _spec_with_action("approval"),
        tenant_id=uuid4(),
        credentials_resolver=_anthropic_credentials_resolver(),
        secret_store=LocalDevSecretStore.from_mapping({_ANTHROPIC_KEY_NAME: "sk-ant-test"}),
    )
    assert isinstance(judge, LLMActionJudge)


@pytest.mark.asyncio
async def test_make_action_judge_uses_platform_config_provider() -> None:
    with pytest.raises(CredentialsResolverError):
        await _make_action_judge(
            _spec_with_action("block"),
            tenant_id=uuid4(),
            credentials_resolver=_anthropic_credentials_resolver(),
            secret_store=LocalDevSecretStore.from_mapping({_ANTHROPIC_KEY_NAME: "sk-ant-test"}),
            judge_config_service=_FakeJudgeConfig(("openai", "gpt-4o")),  # type: ignore[arg-type]
        )
