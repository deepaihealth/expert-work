"""B-175 P4 — ``knowledge_search``'s ``bases`` argument (agents with 2+ bases)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

import pytest

from expert_work.persistence import InMemoryKnowledgeStore
from expert_work.protocol import KnowledgeChunk, RetrievalMethod
from orchestrator.llm import FakeEmbedder
from orchestrator.tools import KnowledgeRetriever, KnowledgeSearchTool, ToolContext
from orchestrator.tools.error_classifier import GuidedToolError
from orchestrator.tools.knowledge import KnowledgeSearchOutcome


@dataclass(frozen=True)
class _RecordingRetriever(KnowledgeRetriever):
    """The real retriever, recording the base names each search asked for."""

    searched: list[tuple[str, ...]] = field(default_factory=list)

    async def search_detailed(
        self, *, tenant_id: UUID, base_names: Sequence[str], **kwargs: Any
    ) -> KnowledgeSearchOutcome:
        self.searched.append(tuple(base_names))
        return await super().search_detailed(tenant_id=tenant_id, base_names=base_names, **kwargs)


async def _seed(names: Sequence[str]) -> tuple[InMemoryKnowledgeStore, UUID]:
    """One tenant; each base holds one document whose only chunk names the base
    (keyword-only bases, so every search finds every base's chunk)."""
    store = InMemoryKnowledgeStore()
    tenant = uuid4()
    for name in names:
        base = await store.create_base(
            tenant_id=tenant, name=name, retrieval_method=RetrievalMethod.KEYWORD
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


def _tool(
    store: InMemoryKnowledgeStore, refs: tuple[str, ...]
) -> tuple[KnowledgeSearchTool, _RecordingRetriever]:
    retriever = _RecordingRetriever(store=store, embedder=FakeEmbedder())
    return KnowledgeSearchTool(retriever=retriever, knowledge_base_refs=refs), retriever


async def _call(tool: KnowledgeSearchTool, tenant: UUID, **args: Any) -> tuple[str, dict[str, Any]]:
    result = await tool.call({"query": "policy", **args}, ctx=ToolContext(tenant_id=tenant))
    return str(result.content), dict(result.meta or {})


def test_single_base_schema_is_unchanged() -> None:
    tool, _ = _tool(InMemoryKnowledgeStore(), ("a",))
    assert set(tool.spec.parameters["properties"]) == {"query", "limit"}


def test_two_bases_schema_offers_bases_without_naming_any() -> None:
    tool, _ = _tool(InMemoryKnowledgeStore(), ("warranty-rules", "product-specs"))
    properties = tool.spec.parameters["properties"]
    assert set(properties) == {"query", "limit", "bases"}
    bases = properties["bases"]
    assert bases["type"] == "array" and bases["items"] == {"type": "string"}
    assert "warranty-rules" not in bases["description"]
    assert "product-specs" not in bases["description"]
    assert tool.spec.parameters["required"] == ["query"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("bases", "searched"),
    [
        (["K2"], ("b",)),
        (["b"], ("b",)),
        (["k2"], ("b",)),
        ("K2", ("b",)),
        (["K2", "K1"], ("a", "b")),
        (["K2", "b", "K2"], ("b",)),
        (["K3", "a"], ("a", "c")),
    ],
)
async def test_bases_narrow_the_search_in_manifest_order(
    bases: Any, searched: tuple[str, ...]
) -> None:
    store, tenant = await _seed(["a", "b", "c"])
    tool, retriever = _tool(store, ("a", "b", "c"))
    await _call(tool, tenant, bases=bases)
    assert retriever.searched == [searched]


@pytest.mark.asyncio
async def test_a_chosen_base_returns_only_its_passages() -> None:
    store, tenant = await _seed(["a", "b"])
    tool, _ = _tool(store, ("a", "b"))
    content, meta = await _call(tool, tenant, bases=["K2"])
    assert "[b.md#0]" in content and "[a.md#0]" not in content
    assert meta["bases"] == ["b"]


@pytest.mark.asyncio
@pytest.mark.parametrize("args", [{}, {"bases": []}, {"bases": None}, {"bases": ["  "]}])
async def test_no_bases_searches_every_base(args: dict[str, Any]) -> None:
    store, tenant = await _seed(["a", "b"])
    tool, retriever = _tool(store, ("a", "b"))
    content, meta = await _call(tool, tenant, **args)
    assert retriever.searched == [("a", "b")]
    assert "[a.md#0]" in content and "[b.md#0]" in content
    assert "bases" not in meta


@pytest.mark.asyncio
@pytest.mark.parametrize("bases", [["K9"], ["secret"], ["K1", "secret"], ["K0"]])
async def test_an_unbound_or_unknown_base_is_refused_before_searching(bases: list[str]) -> None:
    """``secret`` exists in the tenant but this agent does not bind it: the
    argument must not widen what the agent can read."""
    store, tenant = await _seed(["a", "b", "secret"])
    tool, retriever = _tool(store, ("a", "b"))
    with pytest.raises(GuidedToolError) as raised:
        await _call(tool, tenant, bases=bases)
    message = str(raised.value)
    assert "K1 (a)" in message and "K2 (b)" in message
    assert retriever.searched == []


@pytest.mark.asyncio
@pytest.mark.parametrize("bases", [[1, 2], {"K1": True}, 3])
async def test_a_malformed_bases_argument_is_refused(bases: Any) -> None:
    store, tenant = await _seed(["a", "b"])
    tool, retriever = _tool(store, ("a", "b"))
    with pytest.raises(GuidedToolError, match="list of knowledge base handles"):
        await _call(tool, tenant, bases=bases)
    assert retriever.searched == []


@pytest.mark.asyncio
async def test_single_base_ignores_bases() -> None:
    store, tenant = await _seed(["a", "secret"])
    tool, retriever = _tool(store, ("a",))
    _content, meta = await _call(tool, tenant, bases=["secret"])
    assert retriever.searched == [("a",)]
    assert "bases" not in meta


@pytest.mark.asyncio
async def test_choosing_every_base_carries_no_bases_meta() -> None:
    store, tenant = await _seed(["a", "b"])
    tool, _ = _tool(store, ("a", "b"))
    _content, meta = await _call(tool, tenant, bases=["K1", "K2"])
    assert "bases" not in meta


@dataclass(frozen=True)
class _BelowThresholdRetriever(KnowledgeRetriever):
    """Every search ends ``below_threshold`` after judging four candidates."""

    async def search_detailed(
        self, *, tenant_id: UUID, base_names: Sequence[str], **kwargs: Any
    ) -> KnowledgeSearchOutcome:
        del tenant_id, base_names, kwargs
        return KnowledgeSearchOutcome(results=(), status="below_threshold", candidates=4)


async def _miss(tool: KnowledgeSearchTool, tenant: UUID, **args: Any) -> tuple[str, str]:
    result = await tool.call({"query": "zebra", **args}, ctx=ToolContext(tenant_id=tenant))
    return str(result.content), result.notice or ""


@pytest.mark.asyncio
async def test_a_narrowed_search_without_hits_makes_no_absence_claim() -> None:
    """Only the chosen bases were searched: the result names them and the ones
    left out, and does not say the knowledge bases lack the fact."""
    store, tenant = await _seed(["a", "b", "c"])
    tool, _ = _tool(store, ("a", "b", "c"))
    content, notice = await _miss(tool, tenant, bases=["K2"])
    assert "selected knowledge bases (b)" in content
    assert "not searched: a, c" in content
    assert "only the knowledge bases chosen in bases were searched" in notice
    assert "without bases" in notice
    assert "does not cover" not in content + notice


@pytest.mark.asyncio
async def test_a_narrowed_search_below_the_threshold_makes_no_absence_claim() -> None:
    store, tenant = await _seed(["a", "b"])
    retriever = _BelowThresholdRetriever(store=store, embedder=FakeEmbedder())
    tool = KnowledgeSearchTool(retriever=retriever, knowledge_base_refs=("a", "b"))
    content, notice = await _miss(tool, tenant, bases=["K1"])
    assert "selected knowledge bases (a)" in content and "not searched: b" in content
    assert "4 passages were retrieved" in content
    assert "only the knowledge bases chosen in bases were searched" in notice
    assert "does not cover" not in content + notice


@pytest.mark.asyncio
async def test_a_search_of_every_base_without_hits_keeps_its_wording() -> None:
    store, tenant = await _seed(["a", "b"])
    tool, _ = _tool(store, ("a", "b"))
    content, notice = await _miss(tool, tenant)
    assert content == "[no relevant knowledge found for the query]"
    assert notice.endswith("the knowledge base does not cover this.]")


@pytest.mark.asyncio
async def test_a_chosen_base_that_does_not_exist_names_the_selection() -> None:
    """``b`` is bound but was deleted: choosing it searches nothing, and the
    error names what was chosen and says the other bases are still there."""
    store, tenant = await _seed(["a"])
    tool, _ = _tool(store, ("a", "b"))
    with pytest.raises(GuidedToolError) as raised:
        await _call(tool, tenant, bases=["K2"])
    assert str(raised.value) == "none of the selected knowledge bases exists: b"
    assert "without bases" in raised.value.advice
