"""B-222 PR-2 —— two re-index follow-ups from the PR-1 review.

(a) A re-index pays only for chunks not on the new model. A base left
    unstamped (a document re-uploaded during its re-index) is not re-embedded
    whole the next time: once every chunk is on the model — the re-upload came
    in with it — the next re-index embeds nothing and only stamps the base.
(b) A document being estimated or waiting (up to 7 days) for its confirmation
    keeps serving its previous version; a re-index re-embeds that version in
    place, so the wait does not hold the base back — also when the estimate
    finishes meanwhile. A confirmation meanwhile supersedes the write-back: the
    paid ingest embeds with the new model, and the next re-index only stamps.

Embedding models are fakes (no network); texts are synthesised here.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Collection, Iterator, Sequence
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from control_plane.knowledge.ingestion import KnowledgeIngestionRunner, shutdown_parse_pool
from expert_work.persistence import InMemoryKnowledgeStore
from expert_work.protocol import ChunkType, DocumentStatus, KnowledgeChunk
from expert_work.protocol.knowledge_estimate import EstimateOrigin
from orchestrator.llm import FakeEmbedder

_TENANT = uuid4()
_TEXT = "\n\n".join(f"## Part {i}\n\nThe deductible of plan {i} is {i}00 yuan." for i in range(4))


@pytest.fixture(scope="module", autouse=True)
def _parse_pool_teardown() -> Iterator[None]:
    yield
    shutdown_parse_pool()


class _Pinned(FakeEmbedder):
    """A fixed model (``model_key``) that counts the texts it embeds; ``hook``
    runs once, at its first call."""

    def __init__(self, model_key: str, hook: Callable[[], Awaitable[None]] | None = None) -> None:
        super().__init__()
        self.model_key = model_key
        self.embedded = 0
        self._hook = hook

    async def embed(self, texts: Sequence[str], *, tenant_id: UUID) -> list[tuple[float, ...]]:
        if self._hook is not None:
            hook, self._hook = self._hook, None
            await hook()
        self.embedded += len(texts)
        return await super().embed(texts, tenant_id=tenant_id)


async def _ready(store: InMemoryKnowledgeStore, kb_id: UUID, name: str, model: str) -> UUID:
    document = await store.upsert_document(
        tenant_id=_TENANT,
        kb_id=kb_id,
        filename=name,
        content=_TEXT.encode(),
        content_sha256=f"sha-{name}",
    )
    await KnowledgeIngestionRunner(store=store, embedder=_Pinned(model)).submit(
        tenant_id=_TENANT, document_id=document.id
    )
    ready = await store.get_document(tenant_id=_TENANT, document_id=document.id)
    assert ready is not None and ready.status is DocumentStatus.READY
    return document.id


async def _models(store: InMemoryKnowledgeStore, document_id: UUID) -> set[str | None]:
    chunks, _ = await store.list_chunks(tenant_id=_TENANT, document_id=document_id)
    return {chunk.embedding_model for chunk in chunks}


async def _chunk_count(store: InMemoryKnowledgeStore, document_id: UUID) -> int:
    _, total = await store.list_chunks(tenant_id=_TENANT, document_id=document_id)
    return total


async def _base_model(store: InMemoryKnowledgeStore) -> tuple[str | None, str | None]:
    [base] = await store.list_bases(tenant_id=_TENANT)
    return base.embedding_provider, base.embedding_model


async def _reindex(store: InMemoryKnowledgeStore, kb_id: UUID, embedder: _Pinned) -> None:
    runner = KnowledgeIngestionRunner(store=store, embedder=embedder)
    await runner.submit_reindex(
        tenant_id=_TENANT, kb_id=kb_id, embedding_provider="prov", embedding_model="new"
    )


@pytest.mark.usefixtures("no_semantic_breaks")
async def test_a_reindex_embeds_only_the_chunks_not_on_the_new_model() -> None:
    store = InMemoryKnowledgeStore()
    base = await store.create_base(tenant_id=_TENANT, name="kb")
    on_new = await _ready(store, base.id, "a.md", "prov/new")
    on_old = await _ready(store, base.id, "b.md", "prov/old")

    embedder = _Pinned("prov/new")
    await _reindex(store, base.id, embedder)

    assert embedder.embedded == await _chunk_count(store, on_old)
    assert await _models(store, on_old) == {"prov/new"}
    assert await _models(store, on_new) == {"prov/new"}
    assert await _base_model(store) == ("prov", "new")


class _CountingStore(InMemoryKnowledgeStore):
    """Records the documents whose chunks are read once ``reads`` is a list."""

    reads: list[UUID] | None = None

    async def list_chunks(
        self,
        *,
        tenant_id: UUID,
        document_id: UUID,
        offset: int = 0,
        limit: int = 50,
        chunk_types: Collection[ChunkType] | None = None,
        after: tuple[int, ChunkType] | None = None,
    ) -> tuple[list[KnowledgeChunk], int]:
        if self.reads is not None:
            self.reads.append(document_id)
        return await super().list_chunks(
            tenant_id=tenant_id,
            document_id=document_id,
            offset=offset,
            limit=limit,
            chunk_types=chunk_types,
            after=after,
        )


@pytest.mark.usefixtures("no_semantic_breaks")
async def test_once_everything_is_on_the_model_a_reindex_only_stamps() -> None:
    store = _CountingStore()
    base = await store.create_base(tenant_id=_TENANT, name="kb")
    document_id = await _ready(store, base.id, "a.md", "prov/old")

    async def reupload() -> None:
        await store.upsert_document(
            tenant_id=_TENANT,
            kb_id=base.id,
            filename="a.md",
            content=(_TEXT + "\n\nA new paragraph.").encode(),
            content_sha256="v2",
        )

    # The re-upload lands while the re-index embeds: superseded, unstamped.
    await _reindex(store, base.id, _Pinned("prov/new", hook=reupload))
    assert await _base_model(store) == (None, None)
    # Its ingest embeds with the live (new) model.
    await KnowledgeIngestionRunner(store=store, embedder=_Pinned("prov/new")).submit(
        tenant_id=_TENANT, document_id=document_id
    )
    assert await _models(store, document_id) == {"prov/new"}

    again = _Pinned("prov/new")
    store.reads = []
    await _reindex(store, base.id, again)

    assert again.embedded == 0  # the platform pays for nothing again
    assert store.reads == []  # nor reads a chunk: the base is stamped straight away
    assert await _base_model(store) == ("prov", "new")


async def _estimating(store: InMemoryKnowledgeStore, document_id: UUID) -> None:
    started = await store.begin_estimate(
        tenant_id=_TENANT, document_id=document_id, origin=EstimateOrigin.REINGEST
    )
    assert started is not None and started.status is DocumentStatus.ESTIMATING


async def _estimate(store: InMemoryKnowledgeStore, document_id: UUID, model: str) -> None:
    """The estimate phase, as the runner drives it (no model is called)."""
    await KnowledgeIngestionRunner(store=store, embedder=_Pinned(model)).submit(
        tenant_id=_TENANT, document_id=document_id
    )
    waiting = await store.get_document(tenant_id=_TENANT, document_id=document_id)
    assert waiting is not None and waiting.status is DocumentStatus.AWAITING_CONFIRMATION


@pytest.mark.usefixtures("no_semantic_breaks")
async def test_a_document_awaiting_confirmation_does_not_hold_the_base_back() -> None:
    store = InMemoryKnowledgeStore()
    base = await store.create_base(tenant_id=_TENANT, name="kb")
    document_id = await _ready(store, base.id, "a.md", "prov/old")
    await _estimating(store, document_id)
    await _estimate(store, document_id, "prov/old")

    await _reindex(store, base.id, _Pinned("prov/new"))

    assert await _models(store, document_id) == {"prov/new"}
    assert await _base_model(store) == ("prov", "new")
    after = await store.get_document(tenant_id=_TENANT, document_id=document_id)
    assert after is not None and after.status is DocumentStatus.AWAITING_CONFIRMATION


@pytest.mark.usefixtures("no_semantic_breaks")
async def test_an_estimate_finishing_during_the_reindex_does_not_spoil_it() -> None:
    store = InMemoryKnowledgeStore()
    base = await store.create_base(tenant_id=_TENANT, name="kb")
    document_id = await _ready(store, base.id, "a.md", "prov/old")
    await _estimating(store, document_id)

    async def finish() -> None:
        await _estimate(store, document_id, "prov/old")

    await _reindex(store, base.id, _Pinned("prov/new", hook=finish))

    assert await _models(store, document_id) == {"prov/new"}
    assert await _base_model(store) == ("prov", "new")


@pytest.mark.usefixtures("no_semantic_breaks")
async def test_a_confirmation_during_the_reindex_supersedes_it_and_the_next_one_only_stamps() -> (
    None
):
    store = InMemoryKnowledgeStore()
    base = await store.create_base(tenant_id=_TENANT, name="kb")
    document_id = await _ready(store, base.id, "a.md", "prov/old")
    await _estimating(store, document_id)
    await _estimate(store, document_id, "prov/new")  # estimated for the new model

    async def confirm() -> None:
        waiting = await store.get_document(tenant_id=_TENANT, document_id=document_id)
        assert waiting is not None and waiting.ingest_estimate is not None
        outcome = await store.confirm_documents(
            tenant_id=_TENANT,
            kb_id=base.id,
            estimates=[(document_id, waiting.ingest_estimate.id)],
            confirmed_by="user",
            now=datetime.now(UTC),
        )
        assert outcome == {document_id: "confirmed"}

    await _reindex(store, base.id, _Pinned("prov/new", hook=confirm))
    assert await _base_model(store) == (None, None)
    assert await _models(store, document_id) == {"prov/old"}  # still serving

    # The confirmed ingest embeds with the new model; then only a stamp is left.
    await KnowledgeIngestionRunner(store=store, embedder=_Pinned("prov/new")).submit(
        tenant_id=_TENANT, document_id=document_id
    )
    ingested = await store.get_document(tenant_id=_TENANT, document_id=document_id)
    assert ingested is not None and ingested.status is DocumentStatus.READY
    again = _Pinned("prov/new")
    await _reindex(store, base.id, again)
    assert again.embedded == 0
    assert await _base_model(store) == ("prov", "new")
