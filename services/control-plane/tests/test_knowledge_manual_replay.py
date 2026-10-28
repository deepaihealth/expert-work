"""B-175 P3-PR2 —— a re-ingest carries the document's manual changes over.

Re-ingesting a document (re-upload, 「重新摄取」, a re-ingest after the base's
chunking parameters changed) replaces all its chunks. The manual changes are
put back, found by the machine text they were cut as:

* same parameters, same file — every edit, switch-off and added passage is
  back (same ids, texts, revisions) and **nothing is embedded again**;
* other parameters — an edit whose passage text the file still gives is put
  back (its chunks cut by the new parameters); one whose text is gone is not,
  and is listed (``list_unapplied_revisions``) — never silently lost;
* a switched-off passage that can no longer be found switches off the new
  passages holding its text (product rule: err on hiding, mark for review);
* passages added by hand always go back at the end;
* re-indexing (a new embedding model) keeps edits and switch-offs.

The ingest tests run on the in-memory store and, marked ``integration``, on
Postgres (real migrations). Before running the Postgres half locally:
export DOCKER_HOST=unix:///Users/mac/.docker/run/docker.sock
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from alembic import command
from alembic.config import Config

from control_plane.knowledge.chunking import (
    ParentChunk,
    TextChunk,
    configure_semantic_breaks,
    semantic_breaks_enabled,
)
from control_plane.knowledge.ingestion import (
    KnowledgeIngestionRunner,
    embed_passage,
    ingest_markdown,
)
from control_plane.knowledge.passages import (
    PassageOverride,
    PassagePlan,
    replay_manual_changes,
)
from expert_work.persistence import (
    DatabaseConfig,
    InMemoryKnowledgeStore,
    SqlKnowledgeStore,
    create_async_engine_from_config,
    create_async_session_factory,
)
from expert_work.persistence.knowledge.base import (
    KnowledgeStore,
    ManualPassage,
    Passage,
    PassageRevision,
)
from expert_work.persistence.knowledge.passages import SwitchOffRecord
from expert_work.protocol import ChunkType, DocumentStatus, KnowledgeChunk, KnowledgeDocument
from orchestrator.llm import FakeEmbedder

_TITLE = "guide"  # knowledge_document_title("guide.md")
_DB = "knowledge_manual_replay"
_ALEMBIC_INI = Path(__file__).resolve().parents[3] / "packages/expert-work-persistence/alembic.ini"


@pytest.fixture(params=["memory", pytest.param("sql", marks=pytest.mark.integration)])
async def store(request: pytest.FixtureRequest) -> AsyncIterator[KnowledgeStore]:
    if request.param == "memory":
        yield InMemoryKnowledgeStore()
        return
    container = request.getfixturevalue("postgres_container")
    admin = str(container.get_connection_url()).replace("+psycopg2", "")
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{_DB}" WITH (FORCE)')
        conn.execute(f'CREATE DATABASE "{_DB}"')
    dsn = f"{admin.rpartition('/')[0]}/{_DB}"
    cfg = Config(str(_ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", dsn.replace("postgresql://", "postgresql+psycopg://", 1))
    command.upgrade(cfg, "head")
    engine = create_async_engine_from_config(
        DatabaseConfig(dsn=dsn.replace("postgresql://", "postgresql+asyncpg://", 1))
    )
    try:
        yield SqlKnowledgeStore(create_async_session_factory(engine))
    finally:
        await engine.dispose()
        with psycopg.connect(admin, autocommit=True) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{_DB}" WITH (FORCE)')


def _paragraph(tag: str, words: int = 60) -> str:
    return " ".join(f"{tag}{i}" for i in range(words)) + "."


def _markdown(**changed: str) -> str:
    """Three chapters: ``One`` and ``Two`` five paragraphs each (one parent
    each, several chunks), ``Three`` one short paragraph (a lone chunk).
    ``changed`` swaps a paragraph by its tag for another text."""
    blocks = ["# One", *(f"one{c}" for c in "abcde"), "# Two", *(f"two{c}" for c in "abcde")]
    blocks += ["# Three", "three"]
    text = [
        block
        if block.startswith("#")
        else changed.get(block, _paragraph(block, 35 if block == "three" else 60))
        for block in blocks
    ]
    return "\n\n".join(text)


class _Counting(FakeEmbedder):
    """A fixed model (``model_key``: stored vectors are reused) that records
    every text it embeds."""

    model_key = "fake/model"

    def __init__(self) -> None:
        super().__init__()
        self.texts: list[str] = []

    async def embed(self, texts: Sequence[str], *, tenant_id: UUID) -> list[tuple[float, ...]]:
        self.texts.extend(texts)
        return await super().embed(texts, tenant_id=tenant_id)


class _Hooked(FakeEmbedder):
    """Another model (``fake/new``, what a re-index switches to) whose first
    call runs ``hook`` before embedding — writes landing mid re-index."""

    model_key = "fake/new"

    def __init__(self, hook: Callable[[], Awaitable[None]]) -> None:
        super().__init__()
        self._hook: Callable[[], Awaitable[None]] | None = hook

    async def embed(self, texts: Sequence[str], *, tenant_id: UUID) -> list[tuple[float, ...]]:
        if self._hook is not None:
            hook, self._hook = self._hook, None
            await hook()
        return await super().embed(texts, tenant_id=tenant_id)


@pytest.fixture(params=[False, True], ids=["structural", "semantic"])
def semantic(request: pytest.FixtureRequest) -> Iterator[bool]:
    previous = semantic_breaks_enabled()
    configure_semantic_breaks(enabled=request.param)
    yield request.param
    configure_semantic_breaks(enabled=previous)


@dataclass
class _Doc:
    store: KnowledgeStore
    embedder: _Counting
    document: KnowledgeDocument

    @property
    def scope(self) -> dict[str, Any]:
        return {"tenant_id": self.document.tenant_id, "document_id": self.document.id}

    async def ingest(self, markdown: str, *, max_tokens: int = 400) -> None:
        count = await ingest_markdown(
            store=self.store,
            embedder=self.embedder,
            tenant_id=self.document.tenant_id,
            document_id=self.document.id,
            kb_id=self.document.kb_id,
            markdown=markdown,
            chunk_max_tokens=max_tokens,
            chunk_overlap_tokens=0,
            doc_title=_TITLE,
        )
        assert await self.store.set_document_status(
            **self.scope, status=DocumentStatus.READY, chunk_count=count
        )

    async def passages(self) -> list[Passage]:
        return await self.store.list_passages(**self.scope)

    async def passage(self, heading: str) -> Passage:
        found = [p for p in await self.passages() if p.row.heading_path == (heading,)]
        assert len(found) == 1, heading
        return found[0]

    async def edit(self, passage: Passage, content: str, *, max_tokens: int = 400) -> Passage:
        children = await embed_passage(
            store=self.store,
            embedder=self.embedder,
            document=self.document,
            content=content,
            heading_path=passage.row.heading_path,
            chunk_max_tokens=max_tokens,
            chunk_overlap_tokens=0,
        )
        edited = await self.store.replace_passage(
            **self.scope,
            passage_id=passage.id,
            expected_revision=passage.row.revision,
            content=content,
            heading_path=passage.row.heading_path,
            children=children,
            edited_by="user-a",
        )
        assert edited is not None
        return edited

    async def add(self, content: str) -> Passage:
        children = await embed_passage(
            store=self.store,
            embedder=self.embedder,
            document=self.document,
            content=content,
            heading_path=(),
            chunk_max_tokens=400,
            chunk_overlap_tokens=0,
        )
        added = await self.store.add_passage(
            **self.scope, content=content, heading_path=(), children=children, edited_by="user-b"
        )
        assert added is not None
        return added

    async def switch(self, passage: Passage, *, enabled: bool) -> None:
        assert await self.store.set_passage_enabled(
            **self.scope, passage_id=passage.id, enabled=enabled
        )

    async def recalled(self) -> set[UUID]:
        hits = await self.store.search_scored(
            tenant_id=self.document.tenant_id,
            kb_ids=[self.document.kb_id],
            query_embedding=await self._query(),
            limit=100,
        )
        return {hit.chunk.id for hit in hits}

    async def _query(self) -> tuple[float, ...]:
        (vector,) = await FakeEmbedder().embed(["anything"], tenant_id=uuid4())
        return vector


async def _doc(store: KnowledgeStore, markdown: str | None = None) -> _Doc:
    tenant = uuid4()
    base = await store.create_base(tenant_id=tenant, name="kb")
    document = await store.upsert_document(
        tenant_id=tenant, kb_id=base.id, filename="guide.md", content=b"x"
    )
    doc = _Doc(store, _Counting(), document)
    await doc.ingest(markdown if markdown is not None else _markdown())
    return doc


def _with(passage: Passage, tag: str, text: str) -> str:
    """The passage's text with the paragraph ``tag`` replaced."""
    return passage.row.content.replace(_paragraph(tag), text)


# -- same parameters ------------------------------------------------------------


async def test_a_same_parameters_reingest_puts_every_change_back(
    store: KnowledgeStore, semantic: bool
) -> None:
    doc = await _doc(store)
    one, two, three = await doc.passage("One"), await doc.passage("Two"), await doc.passage("Three")
    assert three.row.chunk_type is ChunkType.TEXT  # a lone chunk
    edited = await doc.edit(one, _with(one, "oneb", "Edited sentence about refunds."))
    await doc.switch(two, enabled=False)
    added = await doc.add("A passage added by hand.")
    before = await doc.passages()
    doc.embedder.texts.clear()

    await doc.ingest(_markdown())

    after = await doc.passages()
    assert [(p.row.content, p.row.revision, p.row.edited, p.row.enabled) for p in after] == [
        (p.row.content, p.row.revision, p.row.edited, p.row.enabled) for p in before
    ]
    # The changed passages keep their ids (an untouched one is new, as before).
    assert [after[0].id, after[1].id, after[-1].id] == [edited.id, two.id, added.id]
    assert "Edited sentence about refunds." in after[0].row.content
    assert [c.enabled for c in after[1].children] == [False] * len(after[1].children)
    assert [[c.content for c in p.children] for p in after] == [
        [c.content for c in p.children] for p in before
    ]
    assert doc.embedder.texts == []  # nothing embedded again
    assert await doc.store.list_unapplied_revisions(**doc.scope) == []
    assert not {c.id for c in after[1].children} & await doc.recalled()


async def test_an_emptied_document_keeps_its_added_passages(store: KnowledgeStore) -> None:
    doc = await _doc(store)
    added = await doc.add("A passage added by hand.")
    await doc.ingest("")
    assert [p.id for p in await doc.passages()] == [added.id]
    document = await doc.store.get_document(**doc.scope)
    assert document is not None and document.chunk_count == 1


# -- other parameters, other text -------------------------------------------------


async def test_other_parameters_put_back_the_edits_whose_text_is_unchanged(
    store: KnowledgeStore,
) -> None:
    doc = await _doc(store)
    one = await doc.passage("One")
    edited = await doc.edit(one, _with(one, "oneb", "Edited sentence about refunds."))

    await doc.ingest(_markdown(), max_tokens=300)

    again = await doc.passage("One")
    assert (again.id, again.row.content, again.row.revision, again.row.edited) == (
        edited.id,
        edited.row.content,
        2,
        True,
    )
    assert len(again.children) > len(edited.children)  # cut by the new, smaller cap
    assert any("Edited sentence about refunds." in c.content for c in again.children)
    assert await doc.store.list_unapplied_revisions(**doc.scope) == []


async def test_an_edit_whose_source_text_changed_is_listed_not_lost(store: KnowledgeStore) -> None:
    doc = await _doc(store)
    one = await doc.passage("One")
    edited = await doc.edit(one, _with(one, "oneb", "Edited sentence about refunds."))

    await doc.ingest(_markdown(onec="The file now says something else here."))

    again = await doc.passage("One")
    assert again.id != edited.id
    assert "Edited sentence about refunds." not in again.row.content
    assert "The file now says something else here." in again.row.content
    history = await doc.store.list_passage_revisions(**doc.scope, passage_id=edited.id)
    assert [r.revision for r in history] == [1, 2]
    unapplied = await doc.store.list_unapplied_revisions(**doc.scope)
    assert [(r.passage_id, r.revision, r.content) for r in unapplied] == [
        (edited.id, 2, edited.row.content)
    ]


async def test_a_switched_off_passage_that_moved_switches_off_its_new_passage(
    store: KnowledgeStore,
) -> None:
    doc = await _doc(store)
    two = await doc.passage("Two")
    await doc.switch(two, enabled=False)

    await doc.ingest(_markdown(twoc="Chapter two changed in the file."))

    one, again, three = (
        await doc.passage("One"),
        await doc.passage("Two"),
        await doc.passage("Three"),
    )
    assert "Chapter two changed in the file." in again.row.content
    # Inherited (it holds the other four paragraphs), and marked for review.
    assert (again.row.enabled, again.row.disabled_reason) == (False, "inherited")
    assert {(c.enabled, c.disabled_reason) for c in again.children} == {(False, "inherited")}
    assert one.row.enabled and three.row.enabled
    assert not {c.id for c in again.children} & await doc.recalled()
    review = await doc.store.list_passages(**doc.scope, disabled_reason="inherited")
    assert [p.id for p in review] == [again.id]


async def test_a_switched_off_passage_split_by_moved_boundaries_stays_hidden(
    store: KnowledgeStore,
) -> None:
    """A 30-paragraph section, its second passage switched off; the file adds
    a paragraph at the top, so every passage boundary moves and the old text
    lands in two new passages. Both are switched off and marked: none of it
    is searchable again unflagged."""
    paragraphs = [_paragraph(f"p{i:02d}w", 45) for i in range(30)]
    doc = await _doc(store, "## Long section\n\n" + "\n\n".join(paragraphs))
    target = (await doc.passages())[1]
    hidden = set(target.row.content.split("\n\n"))
    await doc.switch(target, enabled=False)

    await doc.ingest(
        "## Long section\n\n" + "\n\n".join([_paragraph("insertedw", 45), *paragraphs])
    )

    passages = await doc.passages()
    holders = [p for p in passages if hidden & set(p.row.content.split("\n\n"))]
    assert len(holders) == 2
    assert {(p.row.enabled, p.row.disabled_reason) for p in holders} == {(False, "inherited")}
    assert all(p.row.enabled for p in passages if p not in holders)
    assert not {c.id for p in holders for c in p.children} & await doc.recalled()
    review = await doc.store.list_passages(**doc.scope, disabled_reason="inherited")
    assert {p.id for p in review} == {p.id for p in holders}
    assert await doc.store.list_unapplied_revisions(**doc.scope) == []


async def test_a_lost_switch_off_with_no_text_left_is_listed_for_review(
    store: KnowledgeStore,
) -> None:
    """A one-paragraph switched-off passage the file reworded by one word:
    no new passage holds its text, so nothing is switched off — and the lost
    switch-off is not silent: it is listed with the unapplied edits (its old
    text, ``switch_off_lost``), and can be discarded like them."""
    doc = await _doc(store)
    three = await doc.passage("Three")
    await doc.switch(three, enabled=False)

    await doc.ingest(_markdown(three=_paragraph("three", 35).replace("three0 ", "THREE0 ")))

    assert (await doc.passage("Three")).row.enabled
    (lost,) = await doc.store.list_unapplied_revisions(**doc.scope)
    assert (lost.passage_id, lost.content, lost.edited_by, lost.switch_off_lost) == (
        three.id,
        three.row.content,
        None,
        True,
    )
    assert (lost.revision, lost.orphaned_at is not None) == (0, True)
    assert await doc.store.discard_unapplied_revisions(**doc.scope) == 1
    assert await doc.store.list_unapplied_revisions(**doc.scope) == []
    assert await doc.store.list_switch_offs(**doc.scope) == []


def _long_section(paragraphs: list[str], extra: int) -> str:
    """One section of ``paragraphs`` with ``extra`` new ones at the top."""
    top = [_paragraph(f"ins{j}w", 45) for j in range(extra)]
    return "## Long section\n\n" + "\n\n".join([*top, *paragraphs])


def _paragraphs_of(passage: Passage) -> set[str]:
    return set(passage.row.content.split("\n\n"))


async def test_an_inherited_switch_off_does_not_spread_on_later_uploads(
    store: KnowledgeStore,
) -> None:
    """A person hid one passage of a 40-paragraph section; five re-uploads
    each add a paragraph at the top, moving every boundary. Each time the
    passages off are exactly those holding the hidden text — the previous
    upload's inherited ones are derived again, never a source — so the
    hidden text does not grow."""
    paragraphs = [_paragraph(f"p{i:02d}w", 45) for i in range(40)]
    doc = await _doc(store, _long_section(paragraphs, 0))
    target = (await doc.passages())[3]
    hidden = _paragraphs_of(target)
    await doc.switch(target, enabled=False)

    for extra in range(1, 6):
        await doc.ingest(_long_section(paragraphs, extra))
        passages = await doc.passages()
        off = [p for p in passages if not p.row.enabled]
        holders = [p for p in passages if hidden & _paragraphs_of(p)]
        assert [p.id for p in off] == [p.id for p in holders], extra
        assert {p.row.disabled_reason for p in off} == {"inherited"}
        assert sum(len(_paragraphs_of(p)) for p in off) <= 2 * len(hidden), extra
    (record,) = await doc.store.list_switch_offs(**doc.scope)
    assert (record.passage_id, set(record.content.split("\n\n"))) == (target.id, hidden)


async def test_settling_an_inherited_switch_off_sticks(store: KnowledgeStore) -> None:
    """The hidden text sits in two inherited passages after a shift. The
    reviewer lets the first show and keeps the second off as their own: the
    same file again, and one more shift, never hide the first one's text for
    the old switch-off again; the second stays off, a manual switch-off."""
    paragraphs = [_paragraph(f"p{i:02d}w", 45) for i in range(30)]
    doc = await _doc(store, _long_section(paragraphs, 0))
    target = (await doc.passages())[1]
    hidden = _paragraphs_of(target)
    await doc.switch(target, enabled=False)
    await doc.ingest(_long_section(paragraphs, 1))
    first, second = [p for p in await doc.passages() if not p.row.enabled]
    shown = hidden & _paragraphs_of(first)
    assert shown and hidden & _paragraphs_of(second)

    await doc.switch(first, enabled=True)
    confirmed = await doc.store.set_passage_enabled(
        **doc.scope, passage_id=second.id, enabled=False
    )
    assert confirmed is not None and confirmed.row.disabled_reason == "manual"
    assert await doc.store.list_switch_offs(**doc.scope) == []  # nothing left to settle

    await doc.ingest(_long_section(paragraphs, 1))
    passages = await doc.passages()
    assert all(p.row.enabled for p in passages if shown & _paragraphs_of(p))
    kept = next(p for p in passages if p.id == second.id)
    assert (kept.row.enabled, kept.row.disabled_reason) == (False, "manual")

    await doc.ingest(_long_section(paragraphs, 2))
    mine = _paragraphs_of(second)
    for passage in await doc.passages():
        if shown & _paragraphs_of(passage) and not mine & _paragraphs_of(passage):
            assert passage.row.enabled, passage.row.content[:20]


async def test_templated_copies_keep_their_own_changes(store: KnowledgeStore) -> None:
    """Three weeks with the same body: week 3 edited, week 2 switched off.
    Re-ingesting the same file puts each change back on its own week."""
    template = "\n\n".join(_paragraph(t, 120) for t in ("diet", "sleep", "walk"))
    markdown = "\n\n".join(f"## Week {i}\n\n{template}" for i in range(1, 4))
    doc = await _doc(store, markdown)
    w1, w2, w3 = await doc.passages()
    assert w1.row.content == w2.row.content == w3.row.content
    new = w3.row.content.replace(_paragraph("diet", 120), "Week 3 only: reduce salt.")
    edited = await doc.edit(w3, new)
    await doc.switch(w2, enabled=False)
    doc.embedder.texts.clear()

    await doc.ingest(markdown)

    weeks = await doc.passages()
    assert [(p.row.heading_path, p.row.enabled, "salt" in p.row.content) for p in weeks] == [
        (("Week 1",), True, False),
        (("Week 2",), False, False),
        (("Week 3",), True, True),
    ]
    assert [weeks[1].id, weeks[2].id] == [w2.id, edited.id]
    assert weeks[1].row.disabled_reason == "manual"
    assert await doc.store.list_unapplied_revisions(**doc.scope) == []
    assert doc.embedder.texts == []


async def test_a_heading_renamed_in_the_file_takes_the_edit_along(store: KnowledgeStore) -> None:
    """The file renames a section, its text unchanged: the edit goes back on
    under the new heading — the old heading is not written back."""
    doc = await _doc(store)
    two = await doc.passage("Two")
    edited = await doc.edit(two, _with(two, "twob", "Edited under the old heading."))

    await doc.ingest(_markdown().replace("# Two", "# Second"))

    again = await doc.passage("Second")
    assert again.id == edited.id
    assert "Edited under the old heading." in again.row.content
    assert {c.heading_path for c in again.children} == {("Second",)}
    assert [p.row.heading_path for p in await doc.passages()] == [
        ("One",),
        ("Second",),
        ("Three",),
    ]


async def test_a_second_rename_in_the_file_takes_the_edit_along_again(
    store: KnowledgeStore,
) -> None:
    """``Two`` → ``Second`` → ``Later``: after the first re-ingest the row
    carries ``Second`` (the user never changed the heading); the second
    follows the file again — the stale ``Second`` is not kept."""
    doc = await _doc(store)
    two = await doc.passage("Two")
    edited = await doc.edit(two, _with(two, "twob", "Edited under the old heading."))

    for heading in ("Second", "Later"):
        await doc.ingest(_markdown().replace("# Two", f"# {heading}"))
        again = await doc.passage(heading)
        assert again.id == edited.id
        assert "Edited under the old heading." in again.row.content
        assert {c.heading_path for c in again.children} == {(heading,)}


async def test_reindex_never_undoes_a_passage_write(store: KnowledgeStore) -> None:
    """Passage writes land while a re-index (new embedding model) is
    embedding: its write-back rewrites vectors in place, so the switch-off,
    the edit and the added passage all stay, and the untouched chunks carry
    the new model."""
    doc = await _doc(store)
    one, two = await doc.passage("One"), await doc.passage("Two")
    added: list[Passage] = []

    async def meanwhile() -> None:
        await doc.switch(two, enabled=False)
        await doc.edit(one, "Edited while re-indexing.")
        added.append(await doc.add("Added while re-indexing."))

    hooked = _Hooked(meanwhile)
    runner = KnowledgeIngestionRunner(store=doc.store, embedder=hooked)
    await runner.submit_reindex(
        tenant_id=doc.document.tenant_id,
        kb_id=doc.document.kb_id,
        embedding_provider="fake",
        embedding_model="new",
    )

    passages = await doc.passages()
    by_id = {p.id: p for p in passages}
    assert by_id[one.id].row.content == "Edited while re-indexing."
    assert by_id[one.id].row.revision == 2
    assert by_id[two.id].row.enabled is False
    assert added[0].id in by_id
    three = await doc.passage("Three")
    assert {c.embedding_model for c in three.children} == {"fake/new"}
    assert {c.embedding_model for c in by_id[two.id].children} == {"fake/new"}
    assert await doc.store.list_unapplied_revisions(**doc.scope) == []
    base = await doc.store.get_base(tenant_id=doc.document.tenant_id, name="kb")
    assert base is not None and base.embedding_model == "new"


async def test_reindex_keeps_edits_and_switch_offs(store: KnowledgeStore) -> None:
    doc = await _doc(store)
    one, two = await doc.passage("One"), await doc.passage("Two")
    edited = await doc.edit(one, _with(one, "oneb", "Edited sentence about refunds."))
    await doc.switch(two, enabled=False)
    runner = KnowledgeIngestionRunner(store=doc.store, embedder=doc.embedder)

    await runner.submit_reindex(
        tenant_id=doc.document.tenant_id,
        kb_id=doc.document.kb_id,
        embedding_provider="fake",
        embedding_model="model",
    )

    again = await doc.passage("One")
    assert (again.id, again.row.content, again.row.revision) == (edited.id, edited.row.content, 2)
    assert (await doc.passage("Two")).row.enabled is False


async def _reindex(doc: _Doc, embedder: FakeEmbedder) -> None:
    runner = KnowledgeIngestionRunner(store=doc.store, embedder=embedder)
    await runner.submit_reindex(
        tenant_id=doc.document.tenant_id,
        kb_id=doc.document.kb_id,
        embedding_provider="fake",
        embedding_model="new",
    )


async def _models(doc: _Doc) -> tuple[set[str | None], str | None]:
    """The searchable chunks' models, and the model the base is stamped with."""
    chunks, _ = await doc.store.list_chunks(**doc.scope, limit=10_000)
    base = await doc.store.get_base(tenant_id=doc.document.tenant_id, name="kb")
    assert base is not None
    return {c.embedding_model for c in chunks}, base.embedding_model


async def test_reindex_reads_by_position_while_a_passage_shrinks(
    store: KnowledgeStore, caplog: pytest.LogCaptureFixture
) -> None:
    """More than a page of chunks; between the re-index's page reads a
    passage edit drops chunks before the next page. Reading by position, no
    chunk is skipped: everything carries the new model on the first pass."""
    markdown = "\n\n".join(
        f"## S{s}\n\n" + "\n\n".join(_paragraph(f"s{s}p{i}w", 45) for i in range(10))
        for s in range(25)
    )
    doc = await _doc(store, "")
    await doc.ingest(markdown, max_tokens=100)
    _, total = await doc.store.list_chunks(**doc.scope, limit=1)
    assert total > 200  # more than one page
    first = (await doc.passages())[0]
    assert len(first.children) > 1
    new = _Hooked(_no_hook)
    original = doc.store.list_chunks
    pending: list[bool] = [True]

    async def list_chunks(**kwargs: Any) -> tuple[list[KnowledgeChunk], int]:
        page = await original(**kwargs)
        if pending and kwargs.get("chunk_types") is None:
            pending.clear()
            children = await embed_passage(
                store=doc.store,
                embedder=new,
                document=doc.document,
                content="Short replacement.",
                heading_path=first.row.heading_path,
                chunk_max_tokens=100,
                chunk_overlap_tokens=0,
            )
            assert await doc.store.replace_passage(
                **doc.scope,
                passage_id=first.id,
                expected_revision=1,
                content="Short replacement.",
                heading_path=first.row.heading_path,
                children=children,
                edited_by="user-a",
            )
        return page

    doc.store.list_chunks = list_chunks  # type: ignore[method-assign]
    with caplog.at_level("INFO", logger="control_plane.knowledge.ingestion"):
        await _reindex(doc, new)

    assert not pending
    assert await _models(doc) == ({"fake/new"}, "new")
    assert "knowledge.reindex_document_stragglers" not in caplog.text


async def _no_hook() -> None:
    return None


async def test_reindex_re_embeds_chunks_written_with_the_old_model_meanwhile(
    store: KnowledgeStore, caplog: pytest.LogCaptureFixture
) -> None:
    """An edit embedded with the old model lands while the re-index embeds:
    its chunks were not in the re-index's read, so a further pass re-embeds
    every chunk not on the new model before the base is stamped."""
    doc = await _doc(store)
    one = await doc.passage("One")

    async def meanwhile() -> None:
        await doc.edit(one, "Edited with the old model meanwhile.")

    with caplog.at_level("INFO", logger="control_plane.knowledge.ingestion"):
        await _reindex(doc, _Hooked(meanwhile))

    assert await _models(doc) == ({"fake/new"}, "new")
    assert "knowledge.reindex_document_stragglers" in caplog.text
    assert (await doc.passage("One")).row.content == "Edited with the old model meanwhile."


async def test_a_reindex_that_cannot_catch_up_is_not_stamped(
    store: KnowledgeStore, caplog: pytest.LogCaptureFixture
) -> None:
    """Every pass, another old-model edit lands: after the bounded passes some
    chunks still carry the old model, so the re-index fails and the base is
    not stamped with the new model (it still needs a re-index)."""
    doc = await _doc(store)
    edits = iter(range(100))

    class _Racing(FakeEmbedder):
        model_key = "fake/new"

        async def embed(self, texts: Sequence[str], *, tenant_id: UUID) -> list[tuple[float, ...]]:
            one = await doc.passage("One")
            await doc.edit(one, f"Edit {next(edits)} with the old model.")
            return await super().embed(texts, tenant_id=tenant_id)

    with caplog.at_level("INFO", logger="control_plane.knowledge.ingestion"):
        await _reindex(doc, _Racing())

    models, stamped = await _models(doc)
    assert "fake/model" in models
    assert stamped != "new"
    assert "knowledge.reindex_document_failed" in caplog.text


async def test_a_reindex_refused_without_a_reupload_is_not_stamped(
    store: KnowledgeStore, caplog: pytest.LogCaptureFixture
) -> None:
    """The document leaves the status the re-index read it in (same
    generation — not a re-upload): the refused write-back is a failure, not
    "superseded", so the base is not stamped while it keeps old vectors."""
    doc = await _doc(store)

    async def meanwhile() -> None:
        assert await doc.store.set_document_status(**doc.scope, status=DocumentStatus.FAILED)

    with caplog.at_level("INFO", logger="control_plane.knowledge.ingestion"):
        await _reindex(doc, _Hooked(meanwhile))

    models, stamped = await _models(doc)
    assert (models, stamped != "new") == ({"fake/model"}, True)
    assert "knowledge.reindex_document_failed" in caplog.text
    assert "knowledge.reindex_document_superseded" not in caplog.text


async def test_a_reindex_superseded_by_a_reupload_still_stamps(
    store: KnowledgeStore, caplog: pytest.LogCaptureFixture
) -> None:
    """A re-upload lands while the re-index embeds (a new generation): its own
    ingest embeds the new version with the live model, so this document is
    skipped and the base is still stamped."""
    doc = await _doc(store)

    async def meanwhile() -> None:
        await doc.store.upsert_document(
            tenant_id=doc.document.tenant_id,
            kb_id=doc.document.kb_id,
            filename="guide.md",
            content=b"a new version",
        )

    with caplog.at_level("INFO", logger="control_plane.knowledge.ingestion"):
        await _reindex(doc, _Hooked(meanwhile))

    assert (await _models(doc))[1] == "new"
    assert "knowledge.reindex_document_superseded" in caplog.text


async def test_a_reindex_writes_with_the_status_it_read(store: KnowledgeStore) -> None:
    """A document serving its chunks in another status than ``ready`` (a
    failed re-upload keeps the last version) is re-embedded under that
    status — the write-back fences on it."""
    doc = await _doc(store)
    assert await doc.store.set_document_status(**doc.scope, status=DocumentStatus.FAILED)
    failed = await doc.store.get_document(**doc.scope)
    assert failed is not None
    runner = KnowledgeIngestionRunner(store=doc.store, embedder=doc.embedder)

    new = _Hooked(_no_hook)
    await runner._reindex_document(
        tenant_id=failed.tenant_id, document=failed, embedder=new, model_key=new.model_key
    )

    assert (await _models(doc))[0] == {"fake/new"}


# -- the replay rule on its own -----------------------------------------------------


def _parent(*paragraphs: str, heading: str = "H") -> ParentChunk:
    body = "\n\n".join(paragraphs)
    return ParentChunk(
        body=body,
        heading_path=(heading,),
        children=tuple(TextChunk(body=p, heading_path=(heading,)) for p in paragraphs),
    )


def _manual(
    content: str,
    *,
    original: str | None = None,
    added: bool = False,
    enabled: bool = True,
    revision: int = 1,
    heading: str = "H",
    original_heading: str | None = None,
    user_heading: str | None = None,
    occurrence: int = 0,
    unique_text: bool = True,
    inherited: bool = False,
) -> ManualPassage:
    """A manual passage as ``list_manual_passages`` returns it; an edited one's
    latest revision by a person carries ``user_heading`` (default: the row's).
    ``inherited``: switched off by inheritance, not by a person."""
    passage_id = uuid4()
    row = KnowledgeChunk(
        id=passage_id,
        tenant_id=uuid4(),
        kb_id=uuid4(),
        document_id=uuid4(),
        chunk_index=0,
        content=content,
        embedding=None,
        chunk_type=ChunkType.PARENT,
        heading_path=(heading,),
        enabled=enabled,
        disabled_reason=None if enabled else ("inherited" if inherited else "manual"),
        edited=original is not None or added,
        revision=revision,
    )
    first = (
        PassageRevision(
            passage_id=passage_id,
            revision=1,
            content=content if added else (original or content),
            heading_path=(original_heading or heading,),
            edited_by="user-b" if added else None,
        )
        if original is not None or added
        else None
    )
    written = (user_heading or heading,) if original is not None or added else None
    return ManualPassage(row, first, occurrence, unique_text, written)


def _replay(
    parents: list[ParentChunk],
    manual: list[ManualPassage],
    switch_offs: Sequence[PassageRevision] = (),
) -> PassagePlan:
    return replay_manual_changes(
        parents,
        manual,
        switch_offs=switch_offs,
        max_tokens=400,
        overlap_tokens=0,
        doc_title=None,
    )


def _record(text: str, heading: str = "H") -> PassageRevision:
    """A switch-off record as ``list_switch_offs`` returns it."""
    return PassageRevision(uuid4(), 0, text, (heading,), None, switch_off_lost=True)


def test_replay_counts_what_it_did() -> None:
    a = _parent("alpha one is a paragraph", "alpha two is a paragraph")
    b = _parent("beta one is a paragraph", "beta two is a paragraph")
    c = _parent("gamma one is a paragraph")
    edited = _manual("alpha edited text", original=a.body, revision=2)
    off = _manual(b.body, enabled=False)
    lost_edit = _manual("delta edited", original="delta is gone from the file", revision=3)
    lost_off = _manual("gamma one is a paragraph\n\nzeta was here", enabled=False)
    mine = _manual("my own passage", added=True)

    plan = replay_manual_changes(
        [a, b, c],
        [edited, off, lost_edit, lost_off, mine],
        max_tokens=400,
        overlap_tokens=0,
        doc_title=None,
    )

    assert [p.body for p in plan.parents] == ["alpha edited text", b.body, c.body, "my own passage"]
    assert plan.overrides == [
        PassageOverride(id=edited.row.id, enabled=True, edited=True, revision=2),
        PassageOverride(id=off.row.id, enabled=False, revision=1, disabled_reason="manual"),
        PassageOverride(id=None, enabled=False, disabled_reason="inherited"),
        PassageOverride(id=mine.row.id, enabled=True, edited=True, revision=1),
    ]
    assert (plan.edits_applied, plan.disabled_applied, plan.added, plan.disabled_inherited) == (
        1,
        1,
        1,
        1,
    )
    assert plan.unapplied == (lost_edit.row.id,)
    # Recorded, and its text went to ``c``.
    assert plan.switch_offs == (
        SwitchOffRecord(lost_off.row.id, lost_off.row.content, ("H",), placed=True),
    )
    assert plan.chunk_count == 1 + 2 + 1 + 1


def test_identical_copies_are_told_apart_by_heading_and_order() -> None:
    """Same body under two headings, and two copies under one heading: a
    manual change goes back to its own copy — the Children copy, and the
    second of the two."""
    dose = "Take 2 tablets twice a day after meals."
    adults, children = _parent(dose, heading="Adults"), _parent(dose, heading="Children")
    copy1, copy2 = _parent(dose, heading="Notes"), _parent(dose, heading="Notes")
    parents = [adults, children, copy1, copy2]
    fixed = _manual(
        "Take 1 tablet once a day.",
        original=dose,
        heading="Children",
        revision=2,
        unique_text=False,
    )
    off = _manual(dose, enabled=False, heading="Notes", occurrence=1, unique_text=False)

    plan = _replay(parents, [fixed, off])

    assert [p.body for p in plan.parents] == [dose, "Take 1 tablet once a day.", dose, dose]
    assert [o.id if o else None for o in plan.overrides] == [None, fixed.row.id, None, off.row.id]
    assert plan.unapplied == ()


def test_a_heading_renamed_in_the_file() -> None:
    """The file renamed the section, its text unchanged and found nowhere
    else: the edit goes back under the new heading — or the one the user
    chose, if they changed it. Text the document holds more than once is not
    guessed at: listed as unapplied instead."""
    old = "Section text that only this passage holds."
    renamed = _parent(old, heading="New name")
    kept = _manual("Edited text.", original=old, heading="Old name", revision=2)
    plan = _replay([renamed], [kept])
    assert [(p.heading_path, p.body) for p in plan.parents] == [(("New name",), "Edited text.")]
    assert {c.heading_path for c in plan.parents[0].children} == {("New name",)}
    assert plan.overrides[0] is not None and plan.overrides[0].id == kept.row.id

    chosen = _manual(
        "Edited text.", original=old, original_heading="Old name", heading="Mine", revision=2
    )
    plan = _replay([renamed], [chosen])
    assert plan.parents[0].heading_path == ("Mine",)

    shared = _manual(
        "Edited text.", original=old, heading="Old name", revision=2, unique_text=False
    )
    plan = _replay([renamed], [shared])
    assert (plan.parents[0].body, plan.unapplied) == (old, (shared.row.id,))


def test_an_inherited_switch_off_takes_every_passage_holding_its_distinctive_text() -> None:
    """A lost switched-off passage switches off every new passage holding any
    of its distinctive paragraphs — long, and found exactly once in the new
    document — so none of its text is searchable again unflagged. A repeated
    footer is not distinctive: it switches nothing off, and the lost
    switch-off is recorded for review instead."""
    footer = "Confidential - for internal use only, do not distribute."
    sections = [_parent(f"section {i} body text that is unique {i}", footer) for i in range(10)]
    lost = _manual(f"old outdated paragraph that the file reworded\n\n{footer}", enabled=False)
    plan = _replay(sections, [lost])
    assert plan.disabled_inherited == 0
    assert [r.placed for r in plan.switch_offs] == [False]

    four = [f"distinctive paragraph number {n} of the old passage" for n in range(4)]
    lost = _manual("\n\n".join(four), enabled=False)
    one_of_four = _parent(four[0], "something else entirely, at length")
    two_of_four = _parent(four[1], four[2], "and more new text after them")
    untouched = _parent("a passage that holds none of the old text")
    plan = _replay([one_of_four, two_of_four, untouched], [lost])
    inherited = PassageOverride(id=None, enabled=False, disabled_reason="inherited")
    assert plan.overrides == [inherited, inherited, None]
    assert plan.disabled_inherited == 2
    assert [r.placed for r in plan.switch_offs] == [True]


def test_a_lost_switch_off_that_matches_nothing_is_recorded() -> None:
    """A one-paragraph switched-off passage the file reworded matches no new
    passage: nothing is switched off, and its record says so (the store lists
    it where the unapplied edits are reviewed). One found again is no record."""
    old = "Take 2 tablets twice a day after meals, for adults over 60 years."
    reworded = _parent(old.replace("2 tablets", "two tablets"), heading="Dose")
    lost = _manual(old, enabled=False, heading="Dose")
    kept_off = _manual("a switched-off passage the file still has", enabled=False)

    plan = _replay([reworded, _parent(kept_off.row.content)], [lost, kept_off])

    assert plan.overrides[0] is None
    assert plan.switch_offs == (SwitchOffRecord(lost.row.id, old, ("Dose",), placed=False),)
    assert plan.switch_offs_unplaced == 1


def test_records_not_inherited_switch_offs_are_the_sources() -> None:
    """An inherited switch-off is derived, never a source: a passage off by
    inheritance that the new file moved switches nothing off and makes no
    record; one found again is not kept off for its old mark. The document's
    switch-off records are what hide text — on every ingest, afresh."""
    kept_text = "a passage the file still has, off by inheritance before"
    moved_text = "a paragraph inherited the last time, now somewhere else"
    hidden = "the paragraph a person actually switched off, long enough"
    kept = _manual(kept_text, enabled=False, inherited=True, original=kept_text, revision=2)
    moved = _manual(moved_text, enabled=False, inherited=True, original=moved_text, revision=2)
    parents = [
        _parent(kept_text),
        _parent(moved_text, "with a new neighbour after it"),
        _parent("new text", hidden),
    ]

    plan = _replay(parents, [kept, moved], [_record(hidden)])

    inherited = PassageOverride(id=None, enabled=False, disabled_reason="inherited")
    assert plan.overrides == [
        PassageOverride(id=kept.row.id, enabled=True, edited=True, revision=2),
        None,
        inherited,
    ]
    assert [(r.content, r.placed) for r in plan.switch_offs] == [(hidden, True)]
    assert (plan.disabled_applied, plan.disabled_inherited) == (0, 1)


def test_a_second_rename_follows_the_file_again() -> None:
    """Edited under ``Old``; the file renamed it ``Mid`` (the replay moved the
    row there) and now ``New``: the user never changed the heading (their
    revision says ``Old``, as revision 1 does), so it follows the file."""
    old = "Section text that only this passage holds."
    moved = _manual(
        "Edited text.",
        original=old,
        original_heading="Old",
        heading="Mid",
        user_heading="Old",
        revision=2,
    )
    plan = _replay([_parent(old, heading="New")], [moved])
    assert plan.parents[0].heading_path == ("New",)
