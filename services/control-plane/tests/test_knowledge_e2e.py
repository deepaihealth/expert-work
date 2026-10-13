"""End-to-end test for J.5 RAG — ingest a document, then retrieve it.

Exercises the whole pipeline joined together: the ingestion runner
(parse → structure/semantic chunk → embed → store) and the retrieval
path (hybrid search → knowledge_search tool). Deterministic via the
keyword-recall side — a query term present in the document is matched
regardless of the (hash-based) test embedder.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from control_plane.knowledge.ingestion import KnowledgeIngestionRunner
from expert_work.persistence import InMemoryKnowledgeStore
from expert_work.protocol import DocumentStatus
from orchestrator.llm import FakeEmbedder
from orchestrator.tools import KnowledgeRetriever, KnowledgeSearchTool, ToolContext

_DOCUMENT = (
    b"# Benefits\n\n"
    b"## Deductibles\n\n"
    b"The annual deductible is 500 dollars per member.\n\n"
    b"## Leave\n\n"
    b"Vacation grants 20 days of paid leave each year."
)


@pytest.mark.asyncio
async def test_ingest_then_knowledge_search_round_trip() -> None:
    store = InMemoryKnowledgeStore()
    embedder = FakeEmbedder()
    tenant = uuid4()
    base = await store.create_base(tenant_id=tenant, name="benefits")
    document = await store.upsert_document(
        tenant_id=tenant, kb_id=base.id, filename="handbook.md", content=_DOCUMENT
    )

    # Ingest the document through the async runner (bytes come from the claim).
    runner = KnowledgeIngestionRunner(store=store, embedder=embedder)
    await runner.submit(tenant_id=tenant, document_id=document.id)
    ready = await store.get_document(tenant_id=tenant, document_id=document.id)
    assert ready is not None
    assert ready.status is DocumentStatus.READY
    assert ready.chunk_count >= 1

    # Retrieve it through the knowledge_search tool.
    tool = KnowledgeSearchTool(
        retriever=KnowledgeRetriever(store=store, embedder=embedder),
        knowledge_base_refs=("benefits",),
    )
    result = await tool.call({"query": "deductible"}, ctx=ToolContext(tenant_id=tenant))

    assert "deductible" in result.content
    # Source attribution + the chunker's heading-path prefix survive.
    assert "[handbook.md#" in result.content
    assert "[Section: Benefits" in result.content
    assert result.meta["hits"] >= 1
