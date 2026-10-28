"""B-175 P4 — the knowledge catalog as the system prompt's final section.

An agent binding two or more knowledge bases ends its system prompt with
``# Knowledge bases`` (rules + ``<knowledge_catalog>``); with 0 or 1 base none
of it appears — the strict-absence tests pin that, like the worker rubric's.
"""

from __future__ import annotations

import logging
from copy import deepcopy
from typing import Any
from uuid import UUID, uuid4

import pytest

from expert_work.persistence import InMemoryKnowledgeStore
from expert_work.protocol import AgentSpec, KnowledgeBase
from expert_work.runtime.checkpointer import make_checkpointer
from expert_work.runtime.secret_store import LocalDevSecretStore
from orchestrator import BuiltAgent, ToolEnv, build_agent
from orchestrator.agent_factory import _assemble_system_prompt
from orchestrator.llm import FakeEmbedder
from orchestrator.tools import KnowledgeRetriever
from orchestrator.tools.knowledge_catalog import (
    CATALOG_UNAVAILABLE,
    KNOWLEDGE_CATALOG_RULES,
    catalog_fingerprint,
)

_KEY_NAME = "expert-work/dev/llm/anthropic"
_LOGGER = "expert_work.orchestrator.agent_factory"
_MANIFEST: dict[str, Any] = {
    "apiVersion": "expert_work.io/v1",
    "kind": "Agent",
    "metadata": {"name": "kb-agent", "version": "1.0.0", "tenant": "t"},
    "spec": {
        "tenant_config": {},
        "model": {"provider": "anthropic", "name": "claude-sonnet-4-6"},
        "system_prompt": {"template": "you answer questions"},
        "sandbox": {
            "resources": {"cpu": "1.0", "memory": "1Gi"},
            "network": {"egress": "none", "allowlist": []},
            "filesystem": {"readonly_root": True, "writable": []},
        },
    },
}


def _spec(refs: list[str] | None) -> AgentSpec:
    doc = deepcopy(_MANIFEST)
    if refs is not None:
        doc["spec"]["knowledge"] = {"knowledge_base_refs": refs}
    return AgentSpec.model_validate(doc)


async def _key_resolver(provider: str) -> list[str]:
    del provider
    return [f"secret://{_KEY_NAME}"]


async def _build(spec: AgentSpec, store: InMemoryKnowledgeStore, tenant: UUID) -> BuiltAgent:
    env = ToolEnv(knowledge_retriever=KnowledgeRetriever(store=store, embedder=FakeEmbedder()))
    async with make_checkpointer("memory") as checkpointer:
        return await build_agent(
            spec,
            secret_store=LocalDevSecretStore.from_mapping({_KEY_NAME: "sk-ant-test"}),
            checkpointer=checkpointer,
            tool_env=env,
            tenant_id=tenant,
            provider_key_resolver=_key_resolver,
        )


async def _store(*bases: tuple[str, str | None]) -> tuple[InMemoryKnowledgeStore, UUID]:
    store = InMemoryKnowledgeStore()
    tenant = uuid4()
    for name, description in bases:
        await store.create_base(tenant_id=tenant, name=name, description=description)
    return store, tenant


def _search_parameters(built: BuiltAgent) -> dict[str, Any]:
    [entry] = [e for e in built.tool_catalog if e.name == "knowledge_search"]
    return dict(entry.parameters)


# ---------------------------------------------------------------------------
# _assemble_system_prompt
# ---------------------------------------------------------------------------


def test_catalog_is_the_final_section() -> None:
    prompt = _assemble_system_prompt(
        base="BASE",
        skill_fragments=[],
        memory_blocks=["<long-term-memory>M</long-term-memory>"],
        worker_delegation=True,
        knowledge_catalog="CATALOG",
    )
    assert prompt.endswith("\n\n# Knowledge bases\nCATALOG")
    assert prompt.index("# Knowledge bases") > prompt.index("# Subtask delegation")


def test_an_empty_catalog_adds_no_section() -> None:
    """No bare ``# Knowledge bases`` heading for an empty body."""
    kwargs: dict[str, Any] = {
        "base": "BASE",
        "skill_fragments": [],
        "current_date": "DATELINE",
        "spotlight": True,
        "worker_delegation": True,
    }
    assert _assemble_system_prompt(**kwargs, knowledge_catalog="") == (
        _assemble_system_prompt(**kwargs)
    )


# ---------------------------------------------------------------------------
# build_agent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_two_bases_put_the_catalog_in_the_prompt_and_suffix(
    caplog: pytest.LogCaptureFixture,
) -> None:
    store, tenant = await _store(("warranty", "Warranty and returns"), ("specs", "Model specs"))
    with caplog.at_level(logging.INFO, logger=_LOGGER):
        built = await _build(_spec(["warranty", "specs"]), store, tenant)
    for text in (built.system_prompt, built.prompt_suffix):
        assert text.rstrip().endswith("</knowledge_catalog>")
        assert '<base handle="K1" name="warranty"' in text
        assert '<base handle="K2" name="specs"' in text
        assert KNOWLEDGE_CATALOG_RULES in text
    assert "bases" in _search_parameters(built)["properties"]
    [record] = [r for r in caplog.records if r.getMessage().startswith("agent_factory.knowledge")]
    assert record.levelno == logging.INFO
    assert record.getMessage().startswith("agent_factory.knowledge_catalog entries=2 fingerprint=")
    assert "warranty" not in record.getMessage()


@pytest.mark.asyncio
async def test_build_records_the_fingerprint_the_cache_key_computes() -> None:
    """The control plane caches a build only when this equals its key's slot:
    if the two sides hashed differently, no catalog build would ever be cached."""
    store, tenant = await _store(("warranty", "Warranty"), ("specs", "Specs"))
    built = await _build(_spec(["warranty", "specs"]), store, tenant)
    keyed = await catalog_fingerprint(store, tenant_id=tenant, refs=("warranty", "specs"))
    assert keyed is not None
    assert built.knowledge_catalog_fingerprint == keyed


@pytest.mark.asyncio
@pytest.mark.parametrize("refs", [None, ["warranty"]], ids=["no-knowledge", "one-base"])
async def test_one_or_no_base_carries_no_catalog(refs: list[str] | None) -> None:
    store, tenant = await _store(("warranty", "Warranty and returns"), ("specs", "Model specs"))
    built = await _build(_spec(refs), store, tenant)
    for text in (built.system_prompt, built.prompt_suffix):
        assert "knowledge_catalog" not in text
        assert "# Knowledge bases" not in text
        assert "bases" not in text
    if refs is not None:
        assert set(_search_parameters(built)["properties"]) == {"query", "limit"}
    assert built.knowledge_catalog_fingerprint is None


@pytest.mark.asyncio
async def test_catalog_prompt_is_byte_stable_across_builds() -> None:
    """Spotlight is on (the default) and every build mints a new nonce: the
    catalog must not be fenced with it, or each build would bust the cache."""
    store, tenant = await _store(("warranty", "Warranty"), ("specs", "Specs"))
    spec = _spec(["warranty", "specs"])
    first = await _build(spec, store, tenant)
    second = await _build(spec, store, tenant)
    assert first.spotlight_nonce != second.spotlight_nonce
    assert first.system_prompt == second.system_prompt
    assert "<knowledge_catalog>" in first.system_prompt


class _FailingStore(InMemoryKnowledgeStore):
    async def list_bases(self, *, tenant_id: UUID) -> list[KnowledgeBase]:
        raise RuntimeError("database down")


@pytest.mark.asyncio
async def test_a_store_failure_builds_without_a_catalog(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger=_LOGGER):
        built = await _build(_spec(["warranty", "specs"]), _FailingStore(), uuid4())
    assert "knowledge_catalog" not in built.system_prompt
    # Not the key's real fingerprint: the control plane will not cache this build.
    assert built.knowledge_catalog_fingerprint == CATALOG_UNAVAILABLE
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert [r.getMessage() for r in warnings] == [
        "agent_factory.knowledge_catalog_unavailable — building without the catalog"
    ]
