"""B-175 P4 (10-27 decision) — dynamic workers inherit the parent's knowledge bases.

Real worker builds (``make_worker_build_fn`` → ``build_agent``), with the
worker's tool registry captured so its ``knowledge_search`` can be called: the
tenant also holds a base the parent does not bind, which the worker must never
reach. A parent without knowledge gives a worker with neither the tool nor the
catalog.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from control_plane.subagent_runtime import make_worker_build_fn
from expert_work.common.credentials import CredentialsResolver
from expert_work.persistence import InMemoryKnowledgeStore
from expert_work.protocol import (
    AgentSpec,
    KnowledgeChunk,
    RetrievalMethod,
    TenantConfigRecord,
    TenantPlan,
)
from expert_work.runtime.secret_store import LocalDevSecretStore
from orchestrator import BuiltAgent, ToolEnv
from orchestrator.llm import FakeEmbedder
from orchestrator.tools import (
    KnowledgeRetriever,
    ToolContext,
    ToolRegistry,
    build_tool_registry,
)
from orchestrator.tools.error_classifier import GuidedToolError

_KEY_NAME = "anthropic-test"
#: A base the tenant has but the parent does not bind.
_UNBOUND = "zz-unbound"


class _StubTenantConfig:
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


def _credentials() -> CredentialsResolver:
    return CredentialsResolver(
        platform_provider_credentials={"anthropic": f"secret://{_KEY_NAME}"},
        platform_tool_credentials={},
        tenant_config_getter=_StubTenantConfig(),
    )


def _parent(refs: list[str] | None) -> AgentSpec:
    spec: dict[str, Any] = {
        "tenant_config": {},
        "model": {"provider": "anthropic", "name": "claude-haiku-4-5"},
        "system_prompt": {"template": "You are the parent."},
        "sandbox": {
            "resources": {"cpu": "1", "memory": "1Gi"},
            "network": {"egress": "none", "allowlist": []},
            "filesystem": {},
        },
    }
    if refs is not None:
        spec["knowledge"] = {"knowledge_base_refs": refs}
    return AgentSpec.model_validate(
        {
            "apiVersion": "expert_work.io/v1",
            "kind": "Agent",
            "metadata": {"name": "parent", "version": "1.0.0", "tenant": "t"},
            "spec": spec,
        }
    )


async def _seed(names: Sequence[str]) -> tuple[InMemoryKnowledgeStore, UUID]:
    """One tenant; each base holds one keyword-searchable chunk naming it."""
    store = InMemoryKnowledgeStore()
    tenant = uuid4()
    for name in names:
        base = await store.create_base(
            tenant_id=tenant,
            name=name,
            description=f"about {name}",
            retrieval_method=RetrievalMethod.KEYWORD,
        )
        doc = await store.upsert_document(tenant_id=tenant, kb_id=base.id, filename=f"{name}.md")
        await store.replace_chunks(
            tenant_id=tenant,
            document_id=doc.id,
            chunks=[
                KnowledgeChunk(
                    id=uuid4(),
                    tenant_id=tenant,
                    kb_id=base.id,
                    document_id=doc.id,
                    chunk_index=0,
                    content=f"policy text from base {name}",
                    embedding=(1.0, 0.0),
                )
            ],
        )
    return store, tenant


@pytest.fixture
def registries(monkeypatch: pytest.MonkeyPatch) -> list[ToolRegistry]:
    """Every tool registry ``build_agent`` assembles (the real one, recorded)."""
    seen: list[ToolRegistry] = []

    async def _recording(*args: Any, **kwargs: Any) -> ToolRegistry:
        registry = await build_tool_registry(*args, **kwargs)
        seen.append(registry)
        return registry

    monkeypatch.setattr("orchestrator.agent_factory.build_tool_registry", _recording)
    return seen


async def _build_worker(
    store: InMemoryKnowledgeStore, tenant: UUID, parent: AgentSpec
) -> BuiltAgent:
    build_fn = make_worker_build_fn(
        secret_store=LocalDevSecretStore.from_mapping({_KEY_NAME: "sk-ant-test"}),
        checkpointer=InMemorySaver(),
        base_tool_env=ToolEnv(
            knowledge_retriever=KnowledgeRetriever(store=store, embedder=FakeEmbedder())
        ),
        max_iterations=8,
        allowed_toolsets=[],
        credentials_resolver=_credentials(),
    )
    return await build_fn(parent, tenant_id=tenant, role="probe", depth=1)


async def _search(registry: ToolRegistry, tenant: UUID, **args: Any) -> str:
    tool = registry.get("knowledge_search")
    assert tool is not None
    result = await tool.call({"query": "policy", **args}, ctx=ToolContext(tenant_id=tenant))
    return str(result.content)


def _search_parameters(built: BuiltAgent) -> set[str]:
    [entry] = [e for e in built.tool_catalog if e.name == "knowledge_search"]
    return set(entry.parameters["properties"])


@pytest.mark.asyncio
async def test_worker_of_a_one_base_parent_searches_only_that_base(
    registries: list[ToolRegistry],
) -> None:
    store, tenant = await _seed(["a", _UNBOUND])
    built = await _build_worker(store, tenant, _parent(["a"]))
    [registry] = registries

    content = await _search(registry, tenant)
    assert "[D1#0] a.md" in content and _UNBOUND not in content
    # One base: ``bases`` is not offered and is ignored when passed anyway.
    assert _UNBOUND not in await _search(registry, tenant, bases=[_UNBOUND])
    assert _search_parameters(built) == {"query", "limit"}
    assert "knowledge_catalog" not in built.system_prompt


@pytest.mark.asyncio
async def test_worker_of_a_two_base_parent_gets_the_catalog(
    registries: list[ToolRegistry],
) -> None:
    store, tenant = await _seed(["a", "b", _UNBOUND])
    built = await _build_worker(store, tenant, _parent(["a", "b"]))
    [registry] = registries

    assert built.system_prompt.rstrip().endswith("</knowledge_catalog>")
    assert '<base handle="K1" name="a"' in built.system_prompt
    assert '<base handle="K2" name="b"' in built.system_prompt
    assert _UNBOUND not in built.system_prompt
    assert "bases" in _search_parameters(built)

    content = await _search(registry, tenant)
    assert "[D1#0] a.md" in content and "[D2#0] b.md" in content and _UNBOUND not in content
    assert "[D2#0] b.md" in await _search(registry, tenant, bases=["K2"])
    with pytest.raises(GuidedToolError, match="K1 \\(a\\), K2 \\(b\\)"):
        await _search(registry, tenant, bases=[_UNBOUND])


@pytest.mark.asyncio
async def test_worker_of_a_parent_without_knowledge_has_neither(
    registries: list[ToolRegistry],
) -> None:
    store, tenant = await _seed(["a", "b"])
    built = await _build_worker(store, tenant, _parent(None))
    [registry] = registries

    assert registry.get("knowledge_search") is None
    assert registry.get("knowledge_read") is None
    assert {"knowledge_search", "knowledge_read"}.isdisjoint(e.name for e in built.tool_catalog)
    for text in ("knowledge_catalog", "# Knowledge bases", "knowledge_search", "knowledge_read"):
        assert text not in built.system_prompt


async def _read(registry: ToolRegistry, tenant: UUID, document: str) -> str:
    tool = registry.get("knowledge_read")
    assert tool is not None
    result = await tool.call({"document": document}, ctx=ToolContext(tenant_id=tenant))
    return str(result.content)


@pytest.mark.asyncio
async def test_worker_reads_the_parents_documents_and_no_others(
    registries: list[ToolRegistry],
) -> None:
    """B-206 K-2 — ``knowledge_read`` comes with the inherited block: the
    worker reads the parent's bases, and a document of a base the parent does
    not bind reads like one that does not exist — by number or by name."""
    store, tenant = await _seed(["a", _UNBOUND])
    built = await _build_worker(store, tenant, _parent(["a"]))
    [registry] = registries

    assert "knowledge_read" in {e.name for e in built.tool_catalog}
    assert "policy text from base a" in await _read(registry, tenant, "D1")
    with pytest.raises(GuidedToolError, match=r"^no document D2 in this agent's knowledge bases$"):
        await _read(registry, tenant, "D2")
    with pytest.raises(GuidedToolError, match="no document with that file name or title"):
        await _read(registry, tenant, f"{_UNBOUND}.md")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("allowed", "has_knowledge"), [(["knowledge_search"], True), (["web_search"], False)]
)
async def test_the_worker_allowlist_keys_both_knowledge_tools_on_knowledge_search(
    registries: list[ToolRegistry], allowed: list[str], has_knowledge: bool
) -> None:
    store, tenant = await _seed(["a"])
    build_fn = make_worker_build_fn(
        secret_store=LocalDevSecretStore.from_mapping({_KEY_NAME: "sk-ant-test"}),
        checkpointer=InMemorySaver(),
        base_tool_env=ToolEnv(
            knowledge_retriever=KnowledgeRetriever(store=store, embedder=FakeEmbedder())
        ),
        max_iterations=8,
        allowed_toolsets=allowed,
        credentials_resolver=_credentials(),
    )
    await build_fn(_parent(["a"]), tenant_id=tenant, role="probe", depth=1)
    [registry] = registries

    assert (registry.get("knowledge_search") is not None) is has_knowledge
    assert (registry.get("knowledge_read") is not None) is has_knowledge
