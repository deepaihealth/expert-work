"""B-206 K-2 — the ``knowledge_read`` tool.

A document named by a ``knowledge_search`` reference (``D12`` / ``D12#7``), its
number, or its file name / title is read as its passages, in reading order: a
page at a time (``max_tokens`` per page, a page filled to 80% of it), the
passages around one (``D12#7`` + ``context``), those under a heading
(``section``), or those that contain every word given (``find``). Only documents
in the agent's own bases are readable, and only what recall could return
(switched-off passages and documents are not). Tenant text goes in the fenced
content only — never in the notice or an error, which reach the model unfenced.

Token counts here are characters (:class:`_Chars`): a block is its label line,
its text and the separator, so 1,000-character passages fill a 4,000-token page
three at a time (3 x 1,014 <= 3,200).
"""

from __future__ import annotations

import threading
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest

from expert_work.persistence import InMemoryKnowledgeStore
from expert_work.persistence.knowledge.passages import Passage as StoredPassage
from expert_work.protocol import ChunkType, DocumentStatus, KnowledgeChunk, KnowledgeDocument
from expert_work.runtime.tokens import TiktokenEstimator
from orchestrator.tools.error_classifier import GuidedToolError
from orchestrator.tools.knowledge_read import KnowledgeReadTool
from orchestrator.tools.knowledge_read_view import ReadPassage, haystack
from orchestrator.tools.knowledge_results import TRUNCATED_MARK
from orchestrator.tools.registry import ToolBlockedError, ToolContext, ToolResult

PassageSpec = tuple[tuple[str, ...], list[str]]


@dataclass
class _Chars:
    """One token per character; records the threads it counted on and what."""

    threads: set[int] = field(default_factory=set)
    texts: list[str] = field(default_factory=list)

    def count(self, text: str) -> int:
        self.threads.add(threading.get_ident())
        self.texts.append(text)
        return len(text)


async def _add(
    store: InMemoryKnowledgeStore,
    tenant: UUID,
    kb_id: UUID,
    filename: str,
    passages: Sequence[PassageSpec],
) -> KnowledgeDocument:
    """Upload and ingest ``filename`` (``ready``) with ``passages`` ``(breadcrumb,
    [chunk bodies])``: one chunk stands for itself; several get a parent row
    holding their text joined. Chunk numbers run through the document."""
    document = await store.upsert_document(
        tenant_id=tenant, kb_id=kb_id, filename=filename, content=b"v1", content_sha256="v1"
    )
    claim = await store.claim_document(
        tenant_id=tenant,
        document_id=document.id,
        now=datetime.now(UTC),
        lease_seconds=600,
        max_attempts=5,
    )
    assert claim is not None
    rows: list[KnowledgeChunk] = []
    index = 0
    for number, (path, bodies) in enumerate(passages):
        parent_id = uuid4() if len(bodies) > 1 else None
        if parent_id is not None:
            rows.append(
                KnowledgeChunk(
                    id=parent_id,
                    tenant_id=tenant,
                    kb_id=kb_id,
                    document_id=document.id,
                    chunk_index=number,
                    content="\n\n".join(bodies),
                    embedding=None,
                    chunk_type=ChunkType.PARENT,
                    heading_path=path,
                )
            )
        for body in bodies:
            rows.append(
                KnowledgeChunk(
                    id=uuid4(),
                    tenant_id=tenant,
                    kb_id=kb_id,
                    document_id=document.id,
                    chunk_index=index,
                    content=body,
                    embedding=(1.0, 0.0),
                    heading_path=path,
                    parent_chunk_id=parent_id,
                )
            )
            index += 1
    assert await store.replace_chunks(
        tenant_id=tenant, document_id=document.id, chunks=rows, generation=claim.generation
    )
    assert await store.set_document_status(
        tenant_id=tenant,
        document_id=document.id,
        status=DocumentStatus.READY,
        chunk_count=index,
        generation=claim.generation,
    )
    found = await store.get_document(tenant_id=tenant, document_id=document.id)
    assert found is not None and found.status is DocumentStatus.READY
    return found


def _flat(count: int, size: int = 1000, path: tuple[str, ...] = ()) -> list[PassageSpec]:
    """``count`` one-chunk passages ``P00…`` of ``size`` characters."""
    return [(path, [f"P{i:02d}" + "." * (size - 3)]) for i in range(count)]


@dataclass
class _Lib:
    store: InMemoryKnowledgeStore
    tenant: UUID
    kb_id: UUID
    chars: _Chars = field(default_factory=_Chars)

    def tool(self, refs: tuple[str, ...] = ("kb",)) -> KnowledgeReadTool:
        return KnowledgeReadTool(store=self.store, knowledge_base_refs=refs, estimator=self.chars)

    async def read(self, refs: tuple[str, ...] = ("kb",), **args: Any) -> ToolResult:
        return await self.tool(refs).call(args, ctx=ToolContext(tenant_id=self.tenant))

    async def add(self, filename: str, passages: Sequence[PassageSpec]) -> KnowledgeDocument:
        return await _add(self.store, self.tenant, self.kb_id, filename, passages)


async def _lib(store: InMemoryKnowledgeStore | None = None, base: str = "kb") -> _Lib:
    store = store if store is not None else InMemoryKnowledgeStore()
    tenant = uuid4()
    kb = await store.create_base(tenant_id=tenant, name=base)
    return _Lib(store, tenant, kb.id)


async def _manual(lib: _Lib, count: int = 10) -> KnowledgeDocument:
    """``first.md`` (D1), then ``manual.md`` (D2) of ``count`` passages."""
    await lib.add("first.md", _flat(1))
    return await lib.add("manual.md", _flat(count))


def _markers(content: str) -> list[str]:
    return [line[:3] for line in content.splitlines() if line[:1] == "P" and line[1:3].isdigit()]


# ---------------------------------------------------------------------------
# The tool
# ---------------------------------------------------------------------------


def test_the_tool_is_a_read_only_knowledge_read() -> None:
    spec = KnowledgeReadTool(store=InMemoryKnowledgeStore(), knowledge_base_refs=("kb",)).spec
    assert spec.name == "knowledge_read"
    assert spec.is_read_only
    assert spec.parameters["required"] == ["document"]
    props = spec.parameters["properties"]
    assert set(props) == {"document", "page", "section", "find", "context", "max_tokens"}
    assert props["context"]["maximum"] == 5
    assert (props["max_tokens"]["minimum"], props["max_tokens"]["maximum"]) == (4000, 16000)


def test_the_description_does_not_assume_the_answer_is_there() -> None:
    """B-206 track D — reading on is not presumed to reach an answer, and a
    passage that now shows only as a compressed reference is read again."""
    spec = KnowledgeReadTool(store=InMemoryKnowledgeStore(), knowledge_base_refs=("kb",)).spec
    description = " ".join(spec.description.split())
    assert "stops before the answer" not in description
    assert "or may continue in the passages around it," in description
    assert "unless they now show only as a compressed reference" in description


async def test_a_read_needs_a_tenant() -> None:
    lib = await _lib()
    with pytest.raises(ToolBlockedError):
        await lib.tool().call({"document": "D1"}, ctx=ToolContext())


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------


async def test_a_document_reads_from_its_first_page_with_its_details() -> None:
    lib = await _lib()
    await _manual(lib)

    result = await lib.read(document="D2")

    header = result.content.split("\n\n", 1)[0].splitlines()
    assert header[0] == "[D2] manual.md"
    assert "passages: 10" in header[1] and "uploaded: " in header[1]
    assert header[2] == "page 1 of 4 at max_tokens 4000"
    assert _markers(result.content) == ["P00", "P01", "P02"]
    assert "[D2#0]\nP00" in result.content
    assert result.notice is not None and "page 1 of 4" in result.notice
    assert "page=2" in result.notice
    assert result.meta["status"] == "ok" and result.meta["mode"] == "page"
    assert (result.meta["page"], result.meta["pages"]) == (1, 4)
    assert [p["ref"] for p in result.meta["passages"]] == ["D2#0", "D2#1", "D2#2"]
    assert all(p["role"] is None for p in result.meta["passages"])


async def test_pages_follow_one_another_and_the_last_says_so() -> None:
    lib = await _lib()
    await _manual(lib)

    second = await lib.read(document="D2", page=2)
    last = await lib.read(document="D2", page=4)

    assert _markers(second.content) == ["P03", "P04", "P05"]
    assert _markers(last.content) == ["P09"]
    assert last.notice is not None and "last page" in last.notice
    assert "page=5" not in last.notice


@pytest.mark.parametrize("page", [0, 5, 40])
async def test_a_page_out_of_range_names_the_valid_range(page: int) -> None:
    lib = await _lib()
    await _manual(lib)

    with pytest.raises(GuidedToolError) as raised:
        await lib.read(document="D2", page=page)

    assert str(raised.value) == (
        f"page {page} is out of range: D2 has 4 pages at max_tokens 4000 (valid pages 1..4)"
    )


async def test_a_larger_page_holds_more_and_max_tokens_is_clamped() -> None:
    lib = await _lib()
    await _manual(lib, 20)

    big = await lib.read(document="D2", max_tokens=16000)
    tiny = await lib.read(document="D2", max_tokens=10)
    huge = await lib.read(document="D2", max_tokens=10**6)

    assert len(_markers(big.content)) == 12  # 12 x 1,015 <= 12,800
    assert tiny.meta["max_tokens"] == 4000 and tiny.meta["pages"] == 7
    assert huge.meta["max_tokens"] == 16000


async def test_a_passage_larger_than_a_page_is_shown_whole_on_its_own_page() -> None:
    lib = await _lib()
    await lib.add("long.md", [((), ["BIG" + "x" * 5000]), *_flat(2)])

    first = await lib.read(document="D1")

    assert first.content.endswith("BIG" + "x" * 5000)
    assert first.meta["pages"] == 2
    assert TRUNCATED_MARK not in first.content


async def test_a_passage_over_the_ceiling_is_cut_and_marked() -> None:
    lib = await _lib()
    await lib.add("huge.md", [((), ["HUGE" + "y" * 20_000])])

    result = await lib.read(document="D1")

    assert result.content.endswith(TRUNCATED_MARK)
    assert result.meta["passages"][0]["truncated"] is True
    assert len(result.content) < 16_000 + 200
    assert result.notice is not None and "16000" in result.notice


# ---------------------------------------------------------------------------
# Around a passage
# ---------------------------------------------------------------------------


async def test_a_reference_reads_its_passage_with_one_on_each_side() -> None:
    lib = await _lib()
    await _manual(lib)

    result = await lib.read(document="D2#4")

    assert _markers(result.content) == ["P03", "P04", "P05"]
    assert "[D2#4] · match\nP04" in result.content
    assert [(p["ref"], p["role"]) for p in result.meta["passages"]] == [
        ("D2#3", "context_before"),
        ("D2#4", "match"),
        ("D2#5", "context_after"),
    ]
    assert result.meta["mode"] == "around"
    assert result.content.split("\n\n", 1)[0].splitlines()[2] == (
        "around D2#4: 1 passage before, 1 after"
    )


async def test_any_chunk_of_a_passage_points_at_it_and_labels_are_ranges() -> None:
    lib = await _lib()
    await lib.add("grouped.md", [((), ["a0", "a1"]), ((), ["b2", "b3", "b4"]), ((), ["c5"])])

    result = await lib.read(document="D1#3", context=0)

    assert result.content.endswith("[D1#2-4] · match\nb2\n\nb3\n\nb4")
    assert result.meta["passages"][0]["chunk_indexes"] == [2, 3, 4]


async def test_context_is_capped_at_five_and_stops_at_the_document_edges() -> None:
    lib = await _lib()
    await _manual(lib, 12)

    wide = await lib.read(document="D2#6", context=9, max_tokens=16000)
    edge = await lib.read(document="D2#0", context=2)

    assert _markers(wide.content) == [f"P{i:02d}" for i in range(1, 12)]
    assert wide.meta["context"] == 5
    assert _markers(edge.content) == ["P00", "P01", "P02"]


async def test_neighbours_that_do_not_fit_are_left_out_nearest_kept() -> None:
    lib = await _lib()
    await _manual(lib, 12)

    result = await lib.read(document="D2#6", context=3)

    # 3,200 tokens hold three 1,015-token blocks: the passage and one each side.
    assert _markers(result.content) == ["P05", "P06", "P07"]
    assert (result.meta["before"], result.meta["after"]) == (1, 1)
    assert result.notice is not None and "context 3" in result.notice


async def test_a_reference_is_not_paged() -> None:
    lib = await _lib()
    await _manual(lib)
    with pytest.raises(ValueError, match="page"):
        await lib.read(document="D2#4", page=2)


async def test_a_reference_to_no_readable_passage_says_so() -> None:
    lib = await _lib()
    await _manual(lib)

    with pytest.raises(GuidedToolError) as raised:
        await lib.read(document="D2#57")

    assert str(raised.value).startswith("D2 has no readable passage containing #57")
    assert "#0..#9" in str(raised.value)


# ---------------------------------------------------------------------------
# Naming a document
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "document", ["D2", "d2", " D2 ", "[D2]", "2", 2, "[D2#0] manual.md", "manual.md", "MANUAL"]
)
async def test_a_document_is_named_by_number_reference_file_name_or_title(document: Any) -> None:
    lib = await _lib()
    await _manual(lib)

    result = await lib.read(document=document)

    assert result.content.startswith("[D2] manual.md\n")


async def test_a_file_name_with_a_chunk_number_reads_around_it() -> None:
    lib = await _lib()
    await _manual(lib)

    result = await lib.read(document="manual.md#4")

    assert result.meta["mode"] == "around"
    assert _markers(result.content) == ["P03", "P04", "P05"]


async def test_a_name_in_two_bases_asks_which_document() -> None:
    lib = await _lib()
    other = await lib.store.create_base(tenant_id=lib.tenant, name="kb2")
    await lib.add("same.md", _flat(1))
    await _add(lib.store, lib.tenant, other.id, "same.md", _flat(1))

    result = await lib.read(refs=("kb", "kb2"), document="same.md")

    assert result.meta["status"] == "ambiguous"
    assert "D1 · same.md · kb" in result.content and "D2 · same.md · kb2" in result.content
    assert result.meta["passages"] == []
    assert "P00" not in result.content


async def test_a_partial_name_suggests_documents_without_reading_one() -> None:
    lib = await _lib()
    await lib.add("合同-2024.md", _flat(1))
    await lib.add("合同-2025.md", _flat(1))

    result = await lib.read(document="合同")

    assert result.meta["status"] == "no_match_name"
    assert "D1 · 合同-2024.md" in result.content and "D2 · 合同-2025.md" in result.content
    assert "P00" not in result.content


async def test_an_unknown_name_is_an_error() -> None:
    lib = await _lib()
    await _manual(lib)
    with pytest.raises(GuidedToolError, match="no document with that file name or title"):
        await lib.read(document="nothing-like-it.pdf")


async def test_a_switched_off_document_is_not_offered_by_name() -> None:
    """Out of the agent's knowledge, as for recall: its name, number and base are
    never listed, and it makes no name ambiguous."""
    lib = await _lib()
    other = await lib.store.create_base(tenant_id=lib.tenant, name="kb2")
    off = await lib.add("same.md", _flat(1))
    await _add(lib.store, lib.tenant, other.id, "same.md", _flat(1))
    alone = await lib.add("off-only.md", _flat(1))
    await lib.store.set_documents_enabled(
        tenant_id=lib.tenant, kb_id=lib.kb_id, document_ids=[off.id, alone.id], enabled=False
    )

    same = await lib.read(refs=("kb", "kb2"), document="same.md")
    partial = await lib.read(refs=("kb", "kb2"), document="same")
    with pytest.raises(GuidedToolError, match="no document with that file name or title"):
        await lib.read(refs=("kb", "kb2"), document="off-only.md")

    assert same.content.startswith("[D2] same.md\n")
    assert partial.content.startswith("[D2] same.md\n")


@pytest.mark.parametrize(
    "document", ["D2#3-5", "[D2#3-5] · match", "D2#3-5,9", "D2 # 3", "D2#3 · match", "d2#3,4"]
)
async def test_the_tools_own_labels_read_back_as_references(document: str) -> None:
    lib = await _lib()
    await _manual(lib)

    result = await lib.read(document=document)

    assert result.meta["mode"] == "around"
    assert [p["ref"] for p in result.meta["passages"] if p["role"] == "match"] == ["D2#3"]


async def test_a_reference_without_a_chunk_number_reads_by_page() -> None:
    lib = await _lib()
    await _manual(lib)

    result = await lib.read(document="D2#")

    assert result.meta["mode"] == "page"


async def test_an_unprefixed_number_that_finds_nothing_is_tried_as_a_name() -> None:
    lib = await _lib()
    await lib.add("first.md", _flat(1))
    await lib.add("2024.md", _flat(1))

    by_title = await lib.read(document="2024")
    with pytest.raises(GuidedToolError) as prefixed:
        await lib.read(document="D2024")
    with pytest.raises(GuidedToolError) as neither:
        await lib.read(document="77")

    assert by_title.content.startswith("[D2] 2024.md\n")
    assert str(prefixed.value) == "no document D2024 in this agent's knowledge bases"
    assert str(neither.value) == (
        "no document D77 in this agent's knowledge bases, and none with that file name or title"
    )


# ---------------------------------------------------------------------------
# Whose documents
# ---------------------------------------------------------------------------


async def test_a_document_outside_the_agents_bases_reads_like_one_that_does_not_exist() -> None:
    lib = await _lib()
    await lib.add("bound.md", _flat(1))
    unbound = await lib.store.create_base(tenant_id=lib.tenant, name="other")
    await _add(lib.store, lib.tenant, unbound.id, "unbound-secret.md", _flat(1))

    errors = []
    for document in ("D2", "D99", "unbound-secret.md"):
        with pytest.raises(GuidedToolError) as raised:
            await lib.read(document=document)
        errors.append(str(raised.value))

    assert errors[0] == "no document D2 in this agent's knowledge bases"
    assert errors[1] == "no document D99 in this agent's knowledge bases"
    assert errors[2] == "no document with that file name or title in this agent's knowledge bases"


async def test_another_tenants_document_is_not_readable() -> None:
    store = InMemoryKnowledgeStore()
    mine = await _lib(store)
    theirs = await _lib(store)
    await theirs.add("theirs.md", _flat(1))

    with pytest.raises(GuidedToolError, match="no document D1"):
        await mine.read(document="D1")


async def test_no_bound_base_is_an_error() -> None:
    lib = await _lib()
    with pytest.raises(GuidedToolError, match="none of this agent's knowledge bases exists"):
        await lib.read(refs=("gone",), document="D1")


# ---------------------------------------------------------------------------
# What recall leaves out, documents being ingested
# ---------------------------------------------------------------------------


async def test_a_switched_off_passage_is_not_read() -> None:
    lib = await _lib()
    document = await _manual(lib, 4)
    passages = await lib.store.list_passages(tenant_id=lib.tenant, document_id=document.id)
    await lib.store.set_passage_enabled(
        tenant_id=lib.tenant, document_id=document.id, passage_id=passages[1].id, enabled=False
    )

    page = await lib.read(document="D2")
    with pytest.raises(GuidedToolError, match="no readable passage containing #1"):
        await lib.read(document="D2#1")

    assert _markers(page.content) == ["P00", "P02", "P03"]
    assert "passages: 3" in page.content


async def test_a_switched_off_document_is_not_read() -> None:
    lib = await _lib()
    document = await _manual(lib)
    await lib.store.set_documents_enabled(
        tenant_id=lib.tenant, kb_id=lib.kb_id, document_ids=[document.id], enabled=False
    )

    with pytest.raises(GuidedToolError) as raised:
        await lib.read(document="D2")

    assert str(raised.value) == "D2 is switched off in its knowledge base and cannot be read"


async def test_a_document_without_text_yet_says_why() -> None:
    lib = await _lib()
    await lib.store.upsert_document(tenant_id=lib.tenant, kb_id=lib.kb_id, filename="new.md")

    with pytest.raises(GuidedToolError) as raised:
        await lib.read(document="D1")

    assert str(raised.value) == "D1 has no readable text yet: its first ingest has not finished"


async def test_a_document_being_ingested_again_reads_the_version_in_use() -> None:
    lib = await _lib()
    document = await _manual(lib)
    # A re-upload: the document goes back to pending, its chunks keep serving.
    await lib.store.upsert_document(
        tenant_id=lib.tenant,
        kb_id=lib.kb_id,
        filename="manual.md",
        content=b"v2",
        content_sha256="v2",
    )
    current = await lib.store.get_document(tenant_id=lib.tenant, document_id=document.id)
    assert current is not None and current.status is DocumentStatus.PENDING

    result = await lib.read(document="D2")

    assert _markers(result.content) == ["P00", "P01", "P02"]
    assert result.notice is not None
    assert "a newer version of this document is being ingested" in result.notice


async def test_a_newer_version_that_failed_is_not_said_to_be_ingesting() -> None:
    lib = await _lib()
    document = await _manual(lib)
    await lib.store.upsert_document(
        tenant_id=lib.tenant,
        kb_id=lib.kb_id,
        filename="manual.md",
        content=b"v2",
        content_sha256="v2",
    )
    claim = await lib.store.claim_document(
        tenant_id=lib.tenant,
        document_id=document.id,
        now=datetime.now(UTC),
        lease_seconds=600,
        max_attempts=5,
    )
    assert claim is not None
    assert await lib.store.set_document_status(
        tenant_id=lib.tenant,
        document_id=document.id,
        status=DocumentStatus.FAILED,
        error="parse failed",
        generation=claim.generation,
    )
    current = await lib.store.get_document(tenant_id=lib.tenant, document_id=document.id)
    assert current is not None and current.status is DocumentStatus.FAILED and current.has_pending

    result = await lib.read(document="D2")

    assert _markers(result.content) == ["P00", "P01", "P02"]
    assert result.notice is not None
    assert "being ingested" not in result.notice
    assert "a newer version of this document was uploaded but not ingested" in result.notice


class _TornStore(InMemoryKnowledgeStore):
    """The first passage read straddles a chunk swap: a parent row comes back
    without its chunks (they were replaced between the two statements)."""

    def __init__(self) -> None:
        super().__init__()
        self.reads = 0

    async def list_passages(self, **kw: Any) -> list[StoredPassage]:
        self.reads += 1
        passages = await super().list_passages(**kw)
        if self.reads == 1:
            return [StoredPassage(p.row, ()) if len(p.children) > 1 else p for p in passages]
        return passages


async def test_a_read_that_straddled_a_chunk_swap_is_read_again() -> None:
    store = _TornStore()
    lib = await _lib(store)
    await lib.add("grouped.md", [((), ["a0", "a1"]), ((), ["b2"])])

    result = await lib.read(document="D1")

    assert store.reads == 2
    assert "[D1#0-1]\na0" in result.content


# ---------------------------------------------------------------------------
# Trust boundary, metadata, counting
# ---------------------------------------------------------------------------


async def test_notices_and_errors_carry_no_tenant_text() -> None:
    lib = await _lib(base="SECRET-base")
    other = await lib.store.create_base(tenant_id=lib.tenant, name="SECRET-base-2")
    refs = ("SECRET-base", "SECRET-base-2")
    secret = [
        (("SECRET-H1", f"SECRET-H2-{i}"), [f"SECRET-body-{i} " + "z" * 1500]) for i in range(8)
    ]
    await lib.add("SECRET-file.md", secret)
    await _add(lib.store, lib.tenant, other.id, "SECRET-file.md", _flat(1))
    await lib.add("SECRET-huge.md", [((), ["SECRET-" + "h" * 20_000])])
    await lib.store.upsert_document(
        tenant_id=lib.tenant,
        kb_id=lib.kb_id,
        filename="SECRET-file.md",
        content=b"v2",
        content_sha256="v2",
    )

    results = [
        await lib.read(refs=refs, document="D1"),
        await lib.read(refs=refs, document="D1", page=2),
        await lib.read(refs=refs, document="D1#3", context=5),
        await lib.read(refs=refs, document="D1", section="SECRET-H1"),
        await lib.read(refs=refs, document="D1", section="SECRET-H2"),
        await lib.read(refs=refs, document="D1", section="nothing"),
        await lib.read(refs=refs, document="D1", find="SECRET-body"),
        await lib.read(refs=refs, document="D1", find="SECRET absent"),
        await lib.read(refs=refs, document="SECRET-file.md"),
        await lib.read(refs=refs, document="SECRET"),
        await lib.read(refs=refs, document="D3"),
    ]
    for result in results:
        assert "SECRET" in result.content
        assert result.notice is None or "SECRET" not in result.notice, result.notice

    for args in ({"document": "D1", "page": 9}, {"document": "D1#99"}):
        with pytest.raises(GuidedToolError) as raised:
            await lib.read(refs=refs, **args)
        assert "SECRET" not in str(raised.value) and "SECRET" not in raised.value.advice


class _DetailedStore(InMemoryKnowledgeStore):
    """Documents come back with a title of their own and metadata — or, with
    ``numbered=False``, without a number (a row from before numbers)."""

    def __init__(self, *, numbered: bool = True) -> None:
        super().__init__()
        self.numbered = numbered

    def _detailed(self, doc: KnowledgeDocument) -> KnowledgeDocument:
        if not self.numbered:
            return doc.model_copy(update={"doc_seq": None})
        metadata = {
            "version": "v3",
            "effective_date": "2026-01-01",
            "source": "s" * 300,
            "owner": {"nested": "not shown"},
        }
        return doc.model_copy(update={"title": "Spec Title", "metadata": metadata})

    async def get_document_by_seq(self, **kw: Any) -> KnowledgeDocument | None:
        doc = await super().get_document_by_seq(**kw)
        return None if doc is None or not self.numbered else self._detailed(doc)

    async def find_documents_by_name(self, **kw: Any) -> list[KnowledgeDocument]:
        return [self._detailed(doc) for doc in await super().find_documents_by_name(**kw)]


async def test_the_header_shows_the_documents_details() -> None:
    store = _DetailedStore()
    lib = await _lib(store)
    other = await store.create_base(tenant_id=lib.tenant, name="kb2")
    await lib.add("spec.md", _flat(1))

    single = await lib.read(document="D1")
    several = await lib.read(refs=("kb", "kb2"), document="D1")

    details = single.content.splitlines()[1].split(" · ")
    assert details[0] == "title: Spec Title"
    assert details[1].startswith("uploaded: ")
    assert details[2:5] == [
        "version: v3",
        "effective date: 2026-01-01",
        "source: " + "s" * 119 + "…",
    ]
    assert details[5] == "passages: 1"
    assert "nested" not in single.content
    assert "base: kb" in several.content.splitlines()[1]
    assert other.name == "kb2"


async def test_a_document_without_a_number_is_named_by_its_file() -> None:
    lib = await _lib(_DetailedStore(numbered=False))
    await lib.add("old.md", _flat(5))

    page = await lib.read(document="old.md")
    around = await lib.read(document="old.md#2")
    with pytest.raises(GuidedToolError) as raised:
        await lib.read(document="old.md", page=3)

    assert page.content.startswith("[old.md]\n")
    assert "[old.md#0]\nP00" in page.content
    assert around.meta["args"]["document"] == "old.md#2"
    assert str(raised.value).startswith("page 3 is out of range: this document has 2 pages")


async def test_metadata_records_what_was_read_and_how_to_read_it_again() -> None:
    lib = await _lib()
    document = await _manual(lib)

    result = await lib.read(document="manual.md", page=2)

    meta = result.meta
    assert meta["document"] == {
        "id": str(document.id),
        "ref": "D2",
        "kb_id": str(lib.kb_id),
        "status": "ready",
        "passages": 10,
    }
    assert meta["args"] == {"document": "D2", "page": 2, "max_tokens": 4000}
    assert meta["tokens"] > 0
    first = meta["passages"][0]
    assert set(first) == {"id", "ref", "chunk_indexes", "role", "tokens", "truncated"}
    assert first["ref"] == "D2#3" and first["chunk_indexes"] == [3]


async def test_tokens_are_counted_off_the_event_loop() -> None:
    lib = await _lib()
    await _manual(lib)

    await lib.read(document="D2")

    assert lib.chars.threads
    assert threading.get_ident() not in lib.chars.threads


async def test_find_normalises_the_document_off_the_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    threads: set[int] = set()

    def recording(passage: ReadPassage) -> str:
        threads.add(threading.get_ident())
        return haystack(passage)

    monkeypatch.setattr("orchestrator.tools.knowledge_read_reader.haystack", recording)
    lib = await _lib()
    await _manual(lib)

    await lib.read(document="D2", find="P05")
    await lib.read(document="D2", find="nowhere")

    assert threads and threading.get_ident() not in threads


async def test_reads_never_write_the_shared_estimators_cache() -> None:
    """The platform estimator's cache keeps every run's message prefixes warm; a
    document's passages and labels counted into it would evict them."""
    shared = TiktokenEstimator(encoding_name="no-such-encoding")  # chars // 4, no network
    lib = await _lib()
    await _manual(lib, 30)
    tool = KnowledgeReadTool(store=lib.store, knowledge_base_refs=("kb",), estimator=shared)
    ctx = ToolContext(tenant_id=lib.tenant)

    for args in ({"document": "D2"}, {"document": "D2#4"}, {"document": "D2", "find": "P1"}):
        result = await tool.call(args, ctx=ctx)
        assert result.meta["status"] == "ok"

    assert shared._memo == {}


async def test_a_documents_passages_are_counted_once_across_reads() -> None:
    lib = await _lib()
    await _manual(lib)
    tool = lib.tool()  # one tool, as one agent build holds it across its calls
    ctx = ToolContext(tenant_id=lib.tenant)

    for args in ({"document": "D2"}, {"document": "D2", "page": 2}, {"document": "D2#4"}):
        await tool.call(args, ctx=ctx)

    passages = [t for t in lib.chars.texts if t.startswith("P")]
    assert sorted(passages) == sorted(set(passages))
    assert len(passages) == 10
