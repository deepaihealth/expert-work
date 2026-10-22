"""B-206 K-4 / K-6 — what one ``knowledge_search`` result shows the model.

Results grouped by document under a short ``[doc N]`` id with the document's
title, upload date and recorded version details; at most three passages per
document, ten by default, within a token budget that drops the lowest-ranked
first and always keeps the first; a trailer only when passages were left out;
hit details for the trajectory panel in ``ToolResult.meta``, never in the text;
and the finalized tool description.
"""

from __future__ import annotations

import ast
import json
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import pytest

from expert_work.persistence import InMemoryKnowledgeStore
from expert_work.protocol import (
    DocumentSourceType,
    DocumentStatus,
    KnowledgeChunk,
    KnowledgeDocument,
)
from expert_work.runtime.tokens import default_estimator
from orchestrator.graph_builder.builder import _invoke_tool
from orchestrator.tools import (
    KnowledgeRetriever,
    KnowledgeSearchTool,
    RetrievedChunk,
    ToolContext,
    knowledge,
    knowledge_result,
    knowledge_types,
)
from orchestrator.tools.knowledge import (
    KNOWLEDGE_ANSWERABILITY_RULE,
    KNOWLEDGE_SEARCH_GUIDANCE,
    KnowledgeSearchOutcome,
    SearchStatus,
)
from orchestrator.tools.knowledge_result import RESULT_TOKEN_BUDGET
from orchestrator.tools.overflow import (
    EXTERNALIZE_MIN_CHARS,
    OVERFLOW_FOOTER_TAG_OPEN,
    TOOL_RESULT_PATH_ARTIFACT_KEY,
)

_UPLOADED = datetime(2026, 3, 12, 8, 30, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class _CharEstimator:
    """One token per character, so budget arithmetic in a test is exact."""

    def count(self, text: str) -> int:
        return max(1, len(text))


@dataclass
class _FixedRetriever:
    """Hands the tool fixed ranked results (cut to the ``limit`` it asks for)."""

    results: tuple[RetrievedChunk, ...]
    status: SearchStatus = "ok"
    candidates: int | None = None
    rerank_degraded: bool = False
    requested: list[int] = field(default_factory=list)

    async def search_detailed(self, *, limit: int, **_overrides: Any) -> KnowledgeSearchOutcome:
        self.requested.append(limit)
        return KnowledgeSearchOutcome(
            results=self.results[:limit],
            status=self.status,
            candidates=len(self.results) if self.candidates is None else self.candidates,
            rerank_degraded=self.rerank_degraded,
        )


@dataclass
class _RecordingWriter:
    writes: dict[str, str] = field(default_factory=dict)

    async def write(self, *, rel: str, content: str) -> None:
        self.writes[rel] = content

    async def read(self, rel: str) -> str | None:
        return self.writes.get(rel)


def _doc(
    seq: int | None = 1,
    *,
    filename: str = "storage_guide.docx",
    title: str | None = "storage_guide",
    chunk_count: int = 9,
    created_at: datetime | None = _UPLOADED,
    metadata: dict[str, Any] | None = None,
    source_type: DocumentSourceType = DocumentSourceType.FILE,
) -> KnowledgeDocument:
    return KnowledgeDocument(
        id=uuid4(),
        tenant_id=uuid4(),
        kb_id=uuid4(),
        filename=filename,
        status=DocumentStatus.READY,
        chunk_count=chunk_count,
        doc_seq=seq,
        title=title,
        metadata=metadata or {},
        source_type=source_type,
        created_at=created_at,
    )


def _hit(
    document: KnowledgeDocument | None,
    index: int,
    content: str,
    *,
    heading: tuple[str, ...] = (),
    rerank: float | None = None,
    filename: str | None = None,
) -> RetrievedChunk:
    return RetrievedChunk(
        content=content,
        filename=filename or (document.filename if document is not None else "unknown.md"),
        chunk_index=index,
        score=0.61,
        recall_source="both",
        heading_path=heading,
        chunk_id=uuid4(),
        vector_score=0.61,
        keyword_score=3.25,
        fused_score=0.0325,
        rerank_score=rerank,
        document=document,
    )


def _tool(
    results: Sequence[RetrievedChunk], *, estimator: Any = None, **outcome: Any
) -> tuple[KnowledgeSearchTool, _FixedRetriever]:
    retriever = _FixedRetriever(results=tuple(results), **outcome)
    tool = KnowledgeSearchTool(
        retriever=cast(KnowledgeRetriever, retriever),
        knowledge_base_refs=("kb",),
        estimator=estimator or _CharEstimator(),
    )
    return tool, retriever


async def _search(tool: KnowledgeSearchTool, **args: Any) -> Any:
    return await tool.call({"query": "q", **args}, ctx=ToolContext(tenant_id=uuid4()))


def _distinct_docs(n: int, *, body: str = "passage") -> list[RetrievedChunk]:
    """``n`` results, each from its own document ``doc_<i>.md``."""
    return [
        _hit(_doc(i, filename=f"doc_{i}.md", title=f"doc_{i}"), 0, f"{body} {i}")
        for i in range(1, n + 1)
    ]


# ---------------------------------------------------------------------------
# What each result shows
# ---------------------------------------------------------------------------


async def test_document_header_shows_each_recorded_field_once() -> None:
    document = _doc(
        12,
        title="Storage Guide",
        metadata={"version": "v3", "effective_date": "2026-04-01", "source": "Quality team"},
        source_type=DocumentSourceType.URL,
    )
    tool, _ = _tool([_hit(document, 1, "Keep dry."), _hit(document, 4, "Keep cool.")])

    result = await _search(tool)

    assert result.content.splitlines()[0] == (
        "[doc 12] Storage Guide (storage_guide.docx) · uploaded 2026-03-12 · version v3 · "
        "effective 2026-04-01 · source Quality team · source type url"
    )
    assert result.content.count("[doc 12]") == 1
    assert result.content.count("uploaded") == 1


async def test_header_leaves_out_what_is_not_recorded() -> None:
    document = _doc(3, filename="notes.txt", title="notes", metadata={"owner": "someone"})
    tool, _ = _tool([_hit(document, 0, "Body.")])

    header = (await _search(tool)).content.splitlines()[0]

    assert header == "[doc 3] notes.txt · uploaded 2026-03-12"
    for absent in ("version", "effective", "source", "owner", "None", "{}"):
        assert absent not in header


async def test_header_without_an_upload_date_or_short_id() -> None:
    document = _doc(None, filename="legacy.md", title=None, created_at=None)
    tool, _ = _tool([_hit(document, 0, "Body.")])

    content = (await _search(tool)).content

    assert content.splitlines()[0] == "[legacy.md]"
    assert "[legacy.md · passage 1/9]" in content
    assert "uploaded" not in content


async def test_metadata_values_are_one_bounded_line_and_only_scalars() -> None:
    document = _doc(
        5,
        metadata={
            "version": "v2\n  final",
            "source": "x" * 200,
            "effective_date": {"from": "2026-01-01"},
        },
    )
    tool, _ = _tool([_hit(document, 0, "Body.")])

    header = (await _search(tool)).content.splitlines()[0]

    assert "version v2 final" in header
    assert "source " + "x" * 79 + "…" in header
    assert "x" * 80 not in header
    assert "effective" not in header


@pytest.mark.parametrize("flag", [True, False])
async def test_boolean_metadata_is_not_shown(flag: bool) -> None:
    tool, _ = _tool([_hit(_doc(5, metadata={"version": flag}), 0, "Body.")])
    assert "version" not in (await _search(tool)).content


async def test_each_passage_shows_its_position_and_section() -> None:
    document = _doc(12, chunk_count=9)
    tool, _ = _tool([_hit(document, 1, "Keep below 25 C.", heading=("Storage", "Room"))])

    content = (await _search(tool)).content

    assert "[doc 12 · passage 2/9]\n[Section: Storage > Room]\n\nKeep below 25 C." in content


async def test_position_without_a_known_total_shows_only_the_number() -> None:
    document = _doc(12, chunk_count=0)  # count unknown / out of step with the chunks
    tool, _ = _tool([_hit(document, 4, "Body.")])

    content = (await _search(tool)).content

    assert "[doc 12 · passage 5]" in content
    assert "passage 5/" not in content


async def test_model_visible_text_carries_no_ids_or_scores() -> None:
    results = [_hit(_doc(12), 0, "Body.", rerank=0.8731)]
    tool, _ = _tool(results)

    content = (await _search(tool)).content

    hit = results[0]
    assert hit.document is not None
    for hidden in (str(hit.chunk_id), str(hit.document.id), "0.8731", "0.61", "3.25", "0.0325"):
        assert hidden not in content


async def test_results_are_grouped_by_document_and_read_in_document_order() -> None:
    first = _doc(7, filename="a.md", title="a")
    second = _doc(8, filename="b.md", title="b")
    tool, _ = _tool(
        [
            _hit(first, 5, "a-six"),
            _hit(second, 0, "b-one"),
            _hit(first, 1, "a-two"),
        ]
    )

    content = (await _search(tool)).content

    order = [content.index(s) for s in ("[doc 7]", "a-two", "a-six", "[doc 8]", "b-one")]
    assert order == sorted(order)
    assert content.count("[doc 7]") == 1
    assert "\n\n---\n\n[doc 8]" in content


async def test_results_carry_their_source_document_from_the_store() -> None:
    store = InMemoryKnowledgeStore()
    tenant = uuid4()
    base = await store.create_base(tenant_id=tenant, name="kb")
    document = await store.upsert_document(tenant_id=tenant, kb_id=base.id, filename="guide.md")
    await store.replace_chunks(
        tenant_id=tenant,
        document_id=document.id,
        chunks=[
            KnowledgeChunk(
                id=uuid4(),
                tenant_id=tenant,
                kb_id=base.id,
                document_id=document.id,
                chunk_index=i,
                content=f"policy clause {i}",
                embedding=(1.0,),
            )
            for i in range(2)
        ],
    )
    await store.set_document_status(
        tenant_id=tenant, document_id=document.id, status=DocumentStatus.READY, chunk_count=2
    )
    retriever = KnowledgeRetriever(store=store, embedder=_OneVectorEmbedder())

    results = await retriever.search(tenant_id=tenant, base_names=["kb"], query="policy", limit=5)

    assert {r.document.id for r in results if r.document is not None} == {document.id}
    assert all(r.document is not None and r.document.chunk_count == 2 for r in results)
    tool = KnowledgeSearchTool(retriever=retriever, knowledge_base_refs=("kb",))
    content = (await tool.call({"query": "policy"}, ctx=ToolContext(tenant_id=tenant))).content
    assert content.startswith(f"[doc {document.doc_seq}] guide.md · uploaded ")
    assert f"[doc {document.doc_seq} · passage 1/2]" in content


class _OneVectorEmbedder:
    async def embed(self, texts: Sequence[str], *, tenant_id: UUID) -> list[tuple[float, ...]]:
        del tenant_id
        return [(1.0,) for _ in texts]


# ---------------------------------------------------------------------------
# How many: default, limit, per-document cap, token budget
# ---------------------------------------------------------------------------


async def test_the_tool_ranks_from_twenty_and_shows_ten_by_default() -> None:
    tool, retriever = _tool(_distinct_docs(20))

    result = await _search(tool)

    assert retriever.requested == [20]
    assert result.meta["hits"] == 10
    assert "passage 10" in result.content
    assert "passage 11" not in result.content and "[doc 11]" not in result.content


@pytest.mark.parametrize(("limit", "shown"), [(3, 3), (20, 20), (50, 20), (0, 1), ("x", 10)])
async def test_limit_is_clamped_to_one_through_twenty(limit: Any, shown: int) -> None:
    tool, retriever = _tool(_distinct_docs(20))

    result = await _search(tool, limit=limit)

    assert retriever.requested == [20]
    assert result.meta["hits"] == shown


async def test_each_document_shows_at_most_three_passages() -> None:
    crowded = _doc(1, filename="crowded.md", title="crowded")
    other = _doc(2, filename="other.md", title="other")
    results = [_hit(crowded, i, f"crowded {i}") for i in range(5)] + [_hit(other, 0, "other 0")]
    tool, _ = _tool(results)

    result = await _search(tool)

    assert [f"crowded {i}" in result.content for i in range(5)] == [True] * 3 + [False] * 2
    assert "other 0" in result.content
    assert [h["dropped"] for h in result.meta["results"]] == [None] * 3 + ["per_document"] * 2 + [
        None
    ]


async def test_the_budget_drops_the_lowest_ranked_first(monkeypatch: pytest.MonkeyPatch) -> None:
    """A big passage that does not fit takes every lower-ranked one with it,
    even small ones that would: passages are dropped from the bottom."""
    monkeypatch.setattr(knowledge_result, "RESULT_TOKEN_BUDGET", 400)
    results = [
        _hit(_doc(1, filename="a.md", title="a"), 0, "a" * 100),
        _hit(_doc(2, filename="b.md", title="b"), 0, "b" * 300),
        _hit(_doc(3, filename="c.md", title="c"), 0, "c" * 10),
    ]
    tool, _ = _tool(results)

    result = await _search(tool)

    assert "a" * 100 in result.content
    assert "b" * 300 not in result.content and "c" * 10 not in result.content
    assert [h["dropped"] for h in result.meta["results"]] == [None, "budget", "budget"]
    assert result.meta["result_tokens"] <= 400


async def test_the_budget_counts_what_the_model_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    """Measured on the rendered text by the platform estimator: the size the
    meta reports is what the model reads, and it is within the budget."""
    monkeypatch.setattr(knowledge_result, "RESULT_TOKEN_BUDGET", 500)
    words = " ".join(f"note{i}" for i in range(30))
    tool = _platform_tool(_distinct_docs(20, body=words))

    result = await _search(tool)

    assert 1 < result.meta["hits"] < 10
    assert result.meta["result_tokens"] == default_estimator().count(result.content)
    assert result.meta["result_tokens"] <= 500
    assert result.meta["result_cut"] is False


async def test_the_first_passage_is_kept_and_cut_to_fit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(knowledge_result, "RESULT_TOKEN_BUDGET", 300)
    huge = " ".join(f"word{i}" for i in range(3_000))
    tool = _platform_tool([_hit(_doc(1), 0, huge), _hit(_doc(2, filename="b.md"), 0, "small")])

    result = await _search(tool)

    assert result.meta["hits"] == 1
    assert result.meta["first_passage_cut"] is True
    assert "word0 word1 word2" in result.content
    assert "passage cut here to fit the result size" in result.content
    assert default_estimator().count(result.content) <= 300
    assert result.meta["result_cut"] is False  # the passage cut fits; no last-resort trim
    assert "small" not in result.content


async def test_nothing_is_cut_when_everything_fits() -> None:
    tool, _ = _tool(_distinct_docs(4))

    result = await _search(tool)

    assert result.meta["hits"] == 4
    assert result.meta["first_passage_cut"] is False
    assert result.meta["result_cut"] is False
    assert "cut here" not in result.content


async def test_the_budget_is_counted_by_the_platform_estimator() -> None:
    tool = KnowledgeSearchTool(
        retriever=cast(KnowledgeRetriever, _FixedRetriever(results=())),
        knowledge_base_refs=("kb",),
    )
    assert tool.estimator is default_estimator()


# ---------------------------------------------------------------------------
# The trailer: only when something was left out
# ---------------------------------------------------------------------------


async def test_no_trailer_when_nothing_was_left_out() -> None:
    tool, _ = _tool(_distinct_docs(4))

    result = await _search(tool)

    assert result.notice == knowledge._RESULTS_NOTICE
    assert "showing" not in result.notice
    assert result.meta["truncated"] is False


async def test_trailer_when_the_limit_left_passages_out() -> None:
    tool, _ = _tool(_distinct_docs(5))

    result = await _search(tool, limit=3)

    assert result.notice.startswith(knowledge._RESULTS_NOTICE)
    trailer = result.notice[len(knowledge._RESULTS_NOTICE) :]
    assert "showing 3 of the 5 top passages this search found" in trailer
    assert "2 beyond the 3 requested" in trailer
    assert "A larger limit (up to 20)" in trailer
    assert "not every match in the knowledge base" in trailer
    assert "per document" not in trailer and "result size" not in trailer
    assert result.meta["truncated"] is True


async def test_trailer_when_the_per_document_cap_left_passages_out() -> None:
    crowded = _doc(1, filename="crowded.md", title="crowded")
    tool, _ = _tool([_hit(crowded, i, f"c{i}") for i in range(4)])

    trailer = (await _search(tool)).notice[len(knowledge._RESULTS_NOTICE) :]

    assert "showing 3 of the 4 top passages this search found" in trailer
    assert "1 beyond the 3 passages one search shows per document" in trailer
    assert (
        "A narrower search (for example in one document's own terms) can show the ones left out"
        in trailer
    )
    assert "larger limit" not in trailer


async def test_trailer_when_the_budget_left_passages_out(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(knowledge_result, "RESULT_TOKEN_BUDGET", 200)
    tool, _ = _tool(_distinct_docs(3, body="z" * 60))

    trailer = (await _search(tool)).notice[len(knowledge._RESULTS_NOTICE) :]

    assert "beyond the result size" in trailer
    assert "A narrower search" in trailer


async def test_the_trailer_is_platform_text_outside_the_passages() -> None:
    tool, _ = _tool(_distinct_docs(5))

    result = await _search(tool, limit=3)

    assert "showing 3 of" not in result.content


# ---------------------------------------------------------------------------
# Every other outcome keeps its own message
# ---------------------------------------------------------------------------


async def test_below_threshold_and_no_hits_keep_distinct_messages() -> None:
    below, _ = _tool((), status="below_threshold", candidates=3)
    empty, _ = _tool((), status="no_hits", candidates=0)

    turned_down = await _search(below)
    nothing = await _search(empty)

    assert "3 passages were retrieved but none was relevant enough" in turned_down.content
    assert nothing.content == "[no relevant knowledge found for the query]"
    assert "were retrieved" not in nothing.content
    for result in (turned_down, nothing):
        assert result.notice == knowledge._NO_HITS_NOTICE
        assert "showing" not in result.content


# ---------------------------------------------------------------------------
# Hit details for the trajectory panel — in meta, not in the text
# ---------------------------------------------------------------------------


async def test_meta_carries_each_hit_for_the_trajectory_panel() -> None:
    crowded = _doc(4, filename="crowded.md", title="crowded")
    results = [_hit(crowded, i, f"c{i}", rerank=0.9 - i / 10) for i in range(4)]
    tool, _ = _tool(results, candidates=17, rerank_degraded=True)

    meta = (await _search(tool, limit=5)).meta

    assert meta["status"] == "ok"
    assert (meta["hits"], meta["relevant"], meta["candidates"], meta["limit"]) == (3, 4, 17, 5)
    assert meta["rerank_degraded"] is True
    assert meta["token_budget"] == RESULT_TOKEN_BUDGET
    assert 0 < meta["result_tokens"] <= RESULT_TOKEN_BUDGET
    first = meta["results"][0]
    assert first == {
        "rank": 1,
        "shown": True,
        "dropped": None,
        "chunk_id": str(results[0].chunk_id),
        "chunk_index": 0,
        "document_id": str(crowded.id),
        "doc_seq": 4,
        "filename": "crowded.md",
        "recall_source": "both",
        "vector_score": 0.61,
        "keyword_score": 3.25,
        "fused_score": 0.0325,
        "rerank_score": 0.9,
    }
    assert [(h["rank"], h["shown"], h["dropped"]) for h in meta["results"]] == [
        (1, True, None),
        (2, True, None),
        (3, True, None),
        (4, False, "per_document"),
    ]
    json.dumps(meta)  # rides ToolMessage.artifact into the checkpoint / audit / trace


# ---------------------------------------------------------------------------
# Overflow: a budgeted result reaches the model whole
# ---------------------------------------------------------------------------


async def test_a_budgeted_result_is_never_cut_to_a_preview() -> None:
    """A full result is well over the 12,000-char externalization threshold in
    English; the tools node keeps it whole (and still persists a copy above the
    persist floor, so a later prune can collapse it losslessly)."""
    words = " ".join(f"word{i}" for i in range(160))
    results = _distinct_docs(10, body=words)
    tool = KnowledgeSearchTool(
        retriever=cast(KnowledgeRetriever, _FixedRetriever(results=tuple(results))),
        knowledge_base_refs=("kb",),
    )
    direct = await tool.call({"query": "q"}, ctx=ToolContext(tenant_id=uuid4()))
    assert len(direct.content) > EXTERNALIZE_MIN_CHARS
    assert direct.meta["hits"] == 10
    writer = _RecordingWriter()

    message, _updates, _refund, error = await _invoke_tool(
        tool, {"query": "q"}, "call-1", ToolContext(tenant_id=uuid4()), overflow_writer=writer
    )

    assert error is None
    assert message.content == f"{direct.content}\n\n{direct.notice}"
    assert "chars elided" not in message.content
    assert OVERFLOW_FOOTER_TAG_OPEN not in message.content
    assert list(writer.writes.values()) == [direct.content]
    assert message.artifact[TOOL_RESULT_PATH_ARTIFACT_KEY] in writer.writes


async def test_a_full_result_stays_within_the_token_budget() -> None:
    words = " ".join(f"term{i}" for i in range(700))
    tool = KnowledgeSearchTool(
        retriever=cast(
            KnowledgeRetriever, _FixedRetriever(results=tuple(_distinct_docs(20, body=words)))
        ),
        knowledge_base_refs=("kb",),
    )

    result = await tool.call({"query": "q", "limit": 20}, ctx=ToolContext(tenant_id=uuid4()))

    assert 1 < result.meta["hits"] < 20
    assert default_estimator().count(result.content) <= RESULT_TOKEN_BUDGET


# ---------------------------------------------------------------------------
# K-6 — the tool description
# ---------------------------------------------------------------------------


def _description() -> str:
    tool, _ = _tool(())
    return tool.spec.description


def test_the_description_states_how_to_search_and_conclude() -> None:
    description = _description()
    for phrase in (
        KNOWLEDGE_ANSWERABILITY_RULE,
        KNOWLEDGE_SEARCH_GUIDANCE,
        "several small, targeted searches rather than one broad one",
        "search again in the documents' own terms",
        "do not repeat the same or a very similar query",
        "all / every / how many",
        "until a search returns no document you have not seen",
        "against every condition in the question before counting it",
        "verify each condition in the question against text you have actually read",
        "conflicting documents are reported as conflicting, not resolved by guessing",
        "Say you searched and found nothing only when a search actually ran",
        "[doc N] is a document's short id",
        "'passage n/N'",
    ):
        assert phrase in description, phrase


def test_the_description_names_no_tool_or_mode_that_does_not_exist() -> None:
    description = _description()
    for absent in ("knowledge_read", "knowledge_list_documents", "list_documents", "mode="):
        assert absent not in description, absent


def test_the_limit_parameter_states_the_defaults() -> None:
    tool, _ = _tool(())
    limit = tool.spec.parameters["properties"]["limit"]
    assert (limit["minimum"], limit["maximum"]) == (1, 20)
    assert "default 10" in limit["description"]
    assert "at most 3 per document" in limit["description"]


# ---------------------------------------------------------------------------
# Review follow-up: the budget is a hard bound, safe arithmetic, safe meta,
# the title rule, and no import cycle between the knowledge modules
# ---------------------------------------------------------------------------


def _platform_tool(results: Sequence[RetrievedChunk]) -> KnowledgeSearchTool:
    """The tool as shipped: the platform estimator counts the budget."""
    return KnowledgeSearchTool(
        retriever=cast(KnowledgeRetriever, _FixedRetriever(results=tuple(results))),
        knowledge_base_refs=("kb",),
    )


class _QuarterEstimator:
    """``len // 4`` with no floor: a body of up to 3 characters counts 0 tokens."""

    def count(self, text: str) -> int:
        return len(text) // 4


async def test_long_names_and_section_paths_are_capped() -> None:
    document = _doc(5, filename="report " + "x" * 5_000 + ".md", title="Summary " + "t" * 5_000)
    hit = _hit(document, 0, "Body.", heading=("Part " + "h" * 3_000, "Sub " + "g" * 3_000))
    tool, _ = _tool([hit])

    content = (await _search(tool)).content

    header = content.splitlines()[0]
    assert header.startswith("[doc 5] Summary tttt")
    assert len(header) < 400
    assert "x" * 200 not in content and "t" * 200 not in content
    assert "[Section: Part hhhh" in content
    assert "h" * 300 not in content and "g" * 300 not in content


async def test_a_long_file_name_is_capped_where_it_labels_passages() -> None:
    document = _doc(None, filename="y" * 5_000 + ".md", title=None)
    tool, _ = _tool([_hit(document, 0, "Body.")])

    content = (await _search(tool)).content

    assert all(len(line) < 200 for line in content.splitlines())
    assert "· passage 1/9]" in content


async def test_the_rendered_result_never_exceeds_the_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fixed overhead the selection cannot cut (a long header, the cut note)
    is trimmed off the rendered text: the result never exceeds the budget by
    the platform estimator, and nothing downstream cuts this tool's result."""
    monkeypatch.setattr(knowledge_result, "RESULT_TOKEN_BUDGET", 40)
    name = " ".join(f"name{i}" for i in range(40)) + ".md"
    tool = _platform_tool([_hit(_doc(5, filename=name, title=None), 0, "Body words here.")])

    result = await _search(tool)

    assert default_estimator().count(result.content) <= 40
    assert result.meta["result_tokens"] == default_estimator().count(result.content)
    assert result.meta["result_cut"] is True
    assert result.content.endswith("[... result cut here to fit the result size]")


async def test_the_selection_counts_separators_so_no_last_resort_trim_is_needed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With an estimator that adds up exactly, the selection's estimate covers
    every separator the rendered text has: it stops at four documents (111
    tokens each) rather than taking a fifth that fits only when a separator is
    left uncounted (five render to 548), so the result is never trimmed
    mid-passage."""
    monkeypatch.setattr(knowledge_result, "RESULT_TOKEN_BUDGET", 546)
    tool, _ = _tool(_distinct_docs(6, body="b" * 40))

    result = await _search(tool)

    assert result.meta["hits"] == 4
    assert result.meta["result_cut"] is False


async def test_a_first_passage_whose_body_counts_no_tokens_is_still_cut(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(knowledge_result, "RESULT_TOKEN_BUDGET", 20)
    document = _doc(1, filename="storage_guide_" + "z" * 60 + ".docx")
    tool, _ = _tool([_hit(document, 0, "abc")], estimator=_QuarterEstimator())

    result = await _search(tool)

    assert result.meta["hits"] == 1
    assert result.meta["first_passage_cut"] is True
    assert result.meta["result_tokens"] <= 20


@pytest.mark.parametrize(
    ("filename", "title", "name"),
    [
        ("storage_guide.docx", "storage_guide", "storage_guide.docx"),
        ("storage_guide.docx", "storage_guide.docx", "storage_guide.docx"),
        ("archive.tar.gz", "archive.tar", "archive.tar.gz"),
        ("dept/notes.txt", "dept/notes", "dept/notes.txt"),
        ("dept/notes.txt", "notes", "dept/notes.txt"),
        ("storage_guide.docx", "storage", "storage (storage_guide.docx)"),
        ("storage_guide.docx", "Storage Guide", "Storage Guide (storage_guide.docx)"),
    ],
)
async def test_the_title_shows_only_when_it_is_more_than_the_file_name(
    filename: str, title: str, name: str
) -> None:
    tool, _ = _tool([_hit(_doc(2, filename=filename, title=title), 0, "Body.")])

    header = (await _search(tool)).content.splitlines()[0]

    assert header == f"[doc 2] {name} · uploaded 2026-03-12"


async def test_non_finite_scores_are_recorded_as_none() -> None:
    hit = replace(
        _hit(_doc(1), 0, "Body.", rerank=float("nan")),
        vector_score=float("inf"),
        keyword_score=float("-inf"),
    )
    tool, _ = _tool([hit])

    meta = (await _search(tool)).meta

    first = meta["results"][0]
    assert first["vector_score"] is None
    assert first["keyword_score"] is None
    assert first["rerank_score"] is None
    assert first["fused_score"] == 0.0325
    json.dumps(meta, allow_nan=False)  # Postgres JSONB rejects NaN / Infinity


def _imported_modules(path: Path) -> set[str]:
    """Every module a file imports, anywhere in it (module level, under
    ``TYPE_CHECKING``, inside functions)."""
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            names |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            names.add(node.module)
    return names


def test_the_knowledge_modules_import_in_one_direction_only() -> None:
    """CodeQL ``py/unsafe-cyclic-import``: ``knowledge`` imports the result and
    type modules, so neither imports ``knowledge`` back, and none of the three
    goes through the ``orchestrator.tools`` package, which imports ``knowledge``."""
    for module in (knowledge_result, knowledge_types):
        assert module.__file__ is not None
        imported = _imported_modules(Path(module.__file__))
        assert "orchestrator.tools.knowledge" not in imported, module.__name__
        assert "orchestrator.tools" not in imported, module.__name__
    assert knowledge.__file__ is not None
    assert "orchestrator.tools" not in _imported_modules(Path(knowledge.__file__))
    assert knowledge.RetrievedChunk is knowledge_types.RetrievedChunk
