"""B-206 K-2 — ``knowledge_search`` hands the model references ``knowledge_read`` takes.

Each result is headed ``[D<doc_seq>#<chunk index>] <file name>`` (the document's
tenant-wide number, the best-ranked chunk's index), the reference
``knowledge_read`` reads it whole by; a document with no number keeps the old
``[<file name>#<chunk index>]``. A result cut to its matched chunk says, in the
trusted notice, that ``knowledge_read`` returns the whole passage — with
constant text only.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

from expert_work.persistence import InMemoryKnowledgeStore
from expert_work.protocol import ChunkType, KnowledgeChunk
from orchestrator.tools import KnowledgeRetriever, KnowledgeSearchTool, ToolContext
from orchestrator.tools.knowledge import (
    KNOWLEDGE_SEARCH_GUIDANCE,
    KnowledgeSearchOutcome,
    RetrievedChunk,
    _format_results,
)
from orchestrator.tools.knowledge_results import TRUNCATED_MARK, ResultText, lay_out


class _Embedder:
    async def embed(self, texts: Sequence[str], *, tenant_id: UUID) -> list[tuple[float, ...]]:
        return [(1.0, 0.0) for _ in texts]


async def _seeded(
    parent_bodies: Sequence[str], filename: str = "manual.md"
) -> tuple[KnowledgeSearchTool, UUID, int]:
    """Base ``kb``, one document of ``parent_bodies`` parents, each with one
    child (``child i``); returns the tool, the tenant and the document's number."""
    store = InMemoryKnowledgeStore()
    tenant = uuid4()
    # Another document first, so the searched one is not D1.
    base = await store.create_base(tenant_id=tenant, name="kb")
    await store.upsert_document(tenant_id=tenant, kb_id=base.id, filename="first.md")
    document = await store.upsert_document(tenant_id=tenant, kb_id=base.id, filename=filename)
    assert document.doc_seq is not None
    rows: list[KnowledgeChunk] = []
    for i, body in enumerate(parent_bodies):
        parent = KnowledgeChunk(
            id=uuid4(),
            tenant_id=tenant,
            kb_id=base.id,
            document_id=document.id,
            chunk_index=i,
            content=body,
            embedding=None,
            chunk_type=ChunkType.PARENT,
        )
        child = KnowledgeChunk(
            id=uuid4(),
            tenant_id=tenant,
            kb_id=base.id,
            document_id=document.id,
            chunk_index=i,
            content=f"child {i}",
            embedding=(1.0, i / 10),
            parent_chunk_id=parent.id,
        )
        rows += [parent, child]
    await store.replace_chunks(tenant_id=tenant, document_id=document.id, chunks=rows)
    retriever = KnowledgeRetriever(store=store, embedder=_Embedder())
    tool = KnowledgeSearchTool(retriever=retriever, knowledge_base_refs=("kb",))
    return tool, tenant, document.doc_seq


async def test_a_result_is_headed_by_its_document_number_and_chunk_index() -> None:
    tool, tenant, seq = await _seeded(["PARENT BODY"])

    result = await tool.call({"query": "q"}, ctx=ToolContext(tenant_id=tenant))

    assert seq == 2
    assert result.content == "[D2#0] manual.md\nPARENT BODY"


def test_a_document_without_a_number_keeps_the_old_header() -> None:
    numbered = RetrievedChunk(content="one", filename="a.md", chunk_index=3, doc_seq=7)
    legacy = RetrievedChunk(content="two", filename="b.md", chunk_index=4)

    assert _format_results([numbered, legacy]) == "[D7#3] a.md\none\n\n---\n\n[b.md#4]\ntwo"


async def test_a_cut_result_points_at_knowledge_read_in_the_notice() -> None:
    bodies = ["A" * 9000, "B" * 9000]
    tool, tenant, _seq = await _seeded(bodies)

    result = await tool.call({"query": "q"}, ctx=ToolContext(tenant_id=tenant))

    assert TRUNCATED_MARK in result.content
    assert result.notice is not None
    assert "knowledge_read" in result.notice
    assert result.meta["truncated"] is True


async def test_an_uncut_result_has_the_plain_notice() -> None:
    tool, tenant, _seq = await _seeded(["short parent"])

    result = await tool.call({"query": "q"}, ctx=ToolContext(tenant_id=tenant))

    assert TRUNCATED_MARK not in result.content
    assert result.notice is not None
    assert "knowledge_read" not in result.notice
    assert "truncated" not in result.meta


async def test_the_notice_carries_no_document_text_or_file_name() -> None:
    bodies = ["SECRET-PARENT-" + "x" * 9000, "SECRET-OTHER-" + "y" * 9000]
    tool, tenant, _seq = await _seeded(bodies, filename="secret-name.md")

    result = await tool.call({"query": "q"}, ctx=ToolContext(tenant_id=tenant))

    assert result.notice is not None
    for tenant_text in ("SECRET", "secret-name", "child 0"):
        assert tenant_text not in result.notice


def test_the_search_guidance_names_knowledge_read_for_checking_candidates() -> None:
    assert "read each candidate (knowledge_read)" in KNOWLEDGE_SEARCH_GUIDANCE


async def _seeded_flat(bodies: Sequence[str]) -> tuple[KnowledgeSearchTool, UUID]:
    """Base ``kb``, document D2 of ``bodies`` chunks with no parent rows: a
    result too long for what is left of the budget has no shorter stand-in, so
    it and every lower result are dropped."""
    store = InMemoryKnowledgeStore()
    tenant = uuid4()
    base = await store.create_base(tenant_id=tenant, name="kb")
    await store.upsert_document(tenant_id=tenant, kb_id=base.id, filename="first.md")
    document = await store.upsert_document(tenant_id=tenant, kb_id=base.id, filename="manual.md")
    rows = [
        KnowledgeChunk(
            id=uuid4(),
            tenant_id=tenant,
            kb_id=base.id,
            document_id=document.id,
            chunk_index=i,
            content=body,
            embedding=(1.0, i / 10),
        )
        for i, body in enumerate(bodies)
    ]
    await store.replace_chunks(tenant_id=tenant, document_id=document.id, chunks=rows)
    retriever = KnowledgeRetriever(store=store, embedder=_Embedder())
    return KnowledgeSearchTool(retriever=retriever, knowledge_base_refs=("kb",)), tenant


async def test_meta_records_the_arguments_the_search_ran_with() -> None:
    """B-206 track D — what an old search's reference line names: the stripped
    query, and the limit only when the model passed one (coerced as it ran)."""
    tool, tenant, _seq = await _seeded(["short parent"])

    plain = await tool.call({"query": "  pump torque "}, ctx=ToolContext(tenant_id=tenant))
    limited = await tool.call({"query": "q", "limit": 99}, ctx=ToolContext(tenant_id=tenant))

    assert plain.meta["args"] == {"query": "pump torque"}
    assert limited.meta["args"] == {"query": "q", "limit": 20}


async def test_meta_lists_the_references_of_the_passages_shown() -> None:
    tool, tenant, _seq = await _seeded(["one", "two"])

    result = await tool.call({"query": "q"}, ctx=ToolContext(tenant_id=tenant))

    assert result.meta["refs"] == ["D2#0", "D2#1"]
    assert "unnumbered" not in result.meta


async def test_meta_leaves_out_passages_dropped_for_the_budget() -> None:
    tool, tenant = await _seeded_flat(["A" * 9000, "B" * 9000, "C" * 9000])

    result = await tool.call({"query": "q"}, ctx=ToolContext(tenant_id=tenant))

    assert result.meta["hits"] == 3
    assert "B" * 100 not in result.content
    assert result.meta["refs"] == ["D2#0"]


def test_lay_out_counts_the_blocks_it_shows() -> None:
    fits = [ResultText(header=f"[D1#{i}] a.md", text="t") for i in range(3)]
    big = [ResultText(header=f"[D1#{i}] a.md", text="x" * 6000) for i in range(3)]

    assert lay_out(fits).shown == 3
    assert lay_out(big).shown == 1
    assert lay_out([]).shown == 0


@dataclass(frozen=True)
class _CannedRetriever(KnowledgeRetriever):
    """Answers every search with ``outcome``."""

    outcome: KnowledgeSearchOutcome | None = None

    async def search_detailed(self, **kwargs: Any) -> KnowledgeSearchOutcome:
        del kwargs
        assert self.outcome is not None
        return self.outcome


async def test_meta_counts_hits_without_a_reference_and_unsearched_bases() -> None:
    """A hit with no document number would be referenced by its file name, so
    meta only counts it; an unsearched base is counted, never named."""
    outcome = KnowledgeSearchOutcome(
        results=(
            RetrievedChunk(content="one", filename="a.md", chunk_index=3, doc_seq=7),
            RetrievedChunk(content="two", filename="secret-name.md", chunk_index=4),
        ),
        status="ok",
        unsearched_bases=("secret-base",),
    )
    retriever = _CannedRetriever(
        store=InMemoryKnowledgeStore(), embedder=_Embedder(), outcome=outcome
    )
    tool = KnowledgeSearchTool(retriever=retriever, knowledge_base_refs=("kb", "secret-base"))

    result = await tool.call({"query": "q"}, ctx=ToolContext(tenant_id=uuid4()))

    assert (result.meta["refs"], result.meta["unnumbered"]) == (["D7#3"], 1)
    assert result.meta["unsearched"] == 1
    assert "secret" not in json.dumps({k: result.meta[k] for k in ("args", "refs")})
