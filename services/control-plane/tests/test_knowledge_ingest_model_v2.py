"""入库流水线与数据模型 v2(B-175 L2-PR3)。

* 分段正文存干净的、标题路径单独存;嵌入与关键词用的是「文档名 + 标题 + 正文」(B-175 波 2 PR2b
  加上文档名,入库前写入的旧段没有文档名、仍是「标题 + 正文」);
* ``stage`` 按 parsing → chunking → embedding → indexing → ready 推进;
* 失败写 ``error_code``:解析(打不开 / 解析失败 / 不支持)、嵌入、缺原件;
* 内容没变的段复用旧向量,不再调嵌入;
* 重建索引只换向量,标题路径 / 内容指纹原样留着;
* ``DocumentParseError`` 从解析进程池 pickle 回来时 ``code`` 不丢。
"""

from __future__ import annotations

import pickle
from collections.abc import Sequence
from uuid import UUID, uuid4

import httpx

from control_plane.knowledge import ingestion
from control_plane.knowledge.ingestion import KnowledgeIngestionRunner, content_hash
from control_plane.knowledge.parsing import DocumentParseError
from expert_work.persistence import InMemoryKnowledgeStore
from expert_work.protocol import (
    DocumentStatus,
    IngestErrorCode,
    IngestStage,
    KnowledgeDocument,
    chunk_retrieval_text,
)
from orchestrator.llm import FakeEmbedder

_DOC = b"# Alpha\n\nalpha paragraph stays the same.\n\n# Beta\n\nbeta paragraph, first version."
_DOC_EDITED = b"# Alpha\n\nalpha paragraph stays the same.\n\n# Beta\n\nbeta paragraph, edited."


class _RecordingEmbedder(FakeEmbedder):
    """FakeEmbedder that remembers every text it was asked to embed. A
    ``model_key`` makes it a fixed-model embedder (vector reuse applies)."""

    def __init__(self, model_key: str | None = None) -> None:
        super().__init__()
        self.texts: list[str] = []
        if model_key is not None:
            self.model_key = model_key

    async def embed(self, texts: Sequence[str], *, tenant_id: UUID) -> list[tuple[float, ...]]:
        self.texts.extend(texts)
        return await super().embed(texts, tenant_id=tenant_id)


class _FailingEmbedder:
    async def embed(self, texts: Sequence[str], *, tenant_id: UUID) -> list[tuple[float, ...]]:
        del texts, tenant_id
        request = httpx.Request("POST", "https://embed.example/v1")
        response = httpx.Response(400, request=request, text="input too long")
        raise httpx.HTTPStatusError("400 input too long", request=request, response=response)


class _StageRecordingStore(InMemoryKnowledgeStore):
    def __init__(self) -> None:
        super().__init__()
        self.stages: list[IngestStage] = []

    async def set_document_stage(
        self,
        *,
        tenant_id: UUID,
        document_id: UUID,
        stage: IngestStage,
        generation: int | None = None,
    ) -> bool:
        self.stages.append(stage)
        return await super().set_document_stage(
            tenant_id=tenant_id, document_id=document_id, stage=stage, generation=generation
        )


async def _upload_and_ingest(
    store: InMemoryKnowledgeStore,
    embedder: object,
    raw: bytes,
    *,
    filename: str = "guide.md",
    tenant: UUID | None = None,
) -> KnowledgeDocument:
    tenant = tenant or uuid4()
    base = await store.get_base(tenant_id=tenant, name="kb") or await store.create_base(
        tenant_id=tenant, name="kb"
    )
    document = await store.upsert_document(
        tenant_id=tenant, kb_id=base.id, filename=filename, content=raw
    )
    runner = KnowledgeIngestionRunner(store=store, embedder=embedder)  # type: ignore[arg-type]
    await runner.submit(tenant_id=tenant, document_id=document.id)
    fetched = await store.get_document(tenant_id=tenant, document_id=document.id)
    assert fetched is not None
    return fetched


async def test_chunks_store_clean_body_and_heading_path() -> None:
    store = InMemoryKnowledgeStore()
    document = await _upload_and_ingest(store, FakeEmbedder(), _DOC)
    assert document.status is DocumentStatus.READY
    chunks, _ = await store.list_chunks(tenant_id=document.tenant_id, document_id=document.id)
    assert [(c.heading_path, c.content) for c in chunks] == [
        (("Alpha",), "alpha paragraph stays the same."),
        (("Beta",), "beta paragraph, first version."),
    ]
    for chunk in chunks:
        assert not chunk.content.startswith("[Section:")
        assert chunk.content_hash == content_hash(chunk.retrieval_text)


async def test_embeds_document_title_breadcrumb_plus_body() -> None:
    store = InMemoryKnowledgeStore()
    embedder = _RecordingEmbedder()
    await _upload_and_ingest(store, embedder, _DOC)
    assert (
        "[Document: guide]\n[Section: Alpha]\n\nalpha paragraph stays the same." in embedder.texts
    )


async def test_chunks_carry_the_cleaned_document_title() -> None:
    store = InMemoryKnowledgeStore()
    document = await _upload_and_ingest(
        store, FakeEmbedder(), _DOC, filename="manuals/user_guide v2.md"
    )
    chunks, _ = await store.list_chunks(tenant_id=document.tenant_id, document_id=document.id)
    assert {c.doc_title for c in chunks} == {"user guide v2"}
    for chunk in chunks:
        assert chunk.retrieval_text.startswith("[Document: user guide v2]\n[Section: ")
        assert chunk.content_hash == content_hash(chunk.retrieval_text)


async def test_ingest_markdown_without_a_title_embeds_as_before() -> None:
    """``doc_title=None`` (callers that pass no title) keeps the old text."""
    store = InMemoryKnowledgeStore()
    tenant = uuid4()
    base = await store.create_base(tenant_id=tenant, name="kb")
    document = await store.upsert_document(
        tenant_id=tenant, kb_id=base.id, filename="guide.md", content=_DOC
    )
    embedder = _RecordingEmbedder()
    await ingestion.ingest_markdown(
        store=store,
        embedder=embedder,
        tenant_id=tenant,
        document_id=document.id,
        kb_id=base.id,
        markdown=_DOC.decode(),
        chunk_max_tokens=512,
        chunk_overlap_tokens=0,
    )
    chunks, _ = await store.list_chunks(tenant_id=tenant, document_id=document.id)
    assert {c.doc_title for c in chunks} == {None}
    assert "[Section: Alpha]\n\nalpha paragraph stays the same." in embedder.texts


async def test_stage_progression_ends_ready() -> None:
    store = _StageRecordingStore()
    document = await _upload_and_ingest(store, FakeEmbedder(), _DOC)
    assert store.stages == [
        IngestStage.PARSING,
        IngestStage.CHUNKING,
        IngestStage.EMBEDDING,
        IngestStage.INDEXING,
    ]
    assert document.stage is IngestStage.READY
    assert document.error_code is None


async def test_unchanged_chunks_reuse_their_vectors() -> None:
    store = InMemoryKnowledgeStore()
    tenant = uuid4()
    first = await _upload_and_ingest(store, _RecordingEmbedder("fake/a"), _DOC, tenant=tenant)
    before, _ = await store.list_chunks(tenant_id=tenant, document_id=first.id)
    assert {c.embedding_model for c in before} == {"fake/a"}

    embedder = _RecordingEmbedder("fake/a")
    again = await _upload_and_ingest(store, embedder, _DOC_EDITED, tenant=tenant)
    assert again.status is DocumentStatus.READY
    # Only the edited chunk went to the embedding API as a chunk text; the
    # unchanged one kept its stored vector (FakeEmbedder is deterministic, so
    # comparing vectors could not tell reuse from re-embedding — the calls can).
    assert "[Document: guide]\n[Section: Beta]\n\nbeta paragraph, edited." in embedder.texts
    assert (
        "[Document: guide]\n[Section: Alpha]\n\nalpha paragraph stays the same."
        not in embedder.texts
    )
    after, _ = await store.list_chunks(tenant_id=tenant, document_id=again.id)
    assert after[0].content_hash == before[0].content_hash


async def test_no_reuse_across_an_embedding_model_switch() -> None:
    """The platform model changed between uploads: every chunk is embedded
    again with the new model — vectors from two models never mix."""
    store = InMemoryKnowledgeStore()
    tenant = uuid4()
    await _upload_and_ingest(store, _RecordingEmbedder("fake/a"), _DOC, tenant=tenant)
    switched = _RecordingEmbedder("fake/b")
    again = await _upload_and_ingest(store, switched, _DOC_EDITED, tenant=tenant)
    assert (
        "[Document: guide]\n[Section: Alpha]\n\nalpha paragraph stays the same." in switched.texts
    )
    after, _ = await store.list_chunks(tenant_id=tenant, document_id=again.id)
    assert {c.embedding_model for c in after} == {"fake/b"}


async def test_no_reuse_without_a_known_model() -> None:
    """An embedder that cannot name its model (eval / test doubles) never
    reuses — there is nothing to prove the stored vector came from it."""
    store = InMemoryKnowledgeStore()
    tenant = uuid4()
    await _upload_and_ingest(store, FakeEmbedder(), _DOC, tenant=tenant)
    anonymous = _RecordingEmbedder()
    await _upload_and_ingest(store, anonymous, _DOC_EDITED, tenant=tenant)
    assert (
        "[Document: guide]\n[Section: Alpha]\n\nalpha paragraph stays the same." in anonymous.texts
    )


async def test_reingest_of_a_legacy_document_writes_the_new_shape() -> None:
    """Chunks from before 0165 (breadcrumb inside the body, no hash) are not
    reused; the re-ingest stores clean bodies with their heading paths."""
    store = InMemoryKnowledgeStore()
    tenant = uuid4()
    first = await _upload_and_ingest(store, _RecordingEmbedder("fake/a"), _DOC, tenant=tenant)
    legacy = [
        c.model_copy(
            update={
                "content": chunk_retrieval_text(c.heading_path, c.content),
                "heading_path": (),
                "doc_title": None,
                "content_hash": None,
            }
        )
        for c in store._chunks
        if c.document_id == first.id
    ]
    store._chunks = [c for c in store._chunks if c.document_id != first.id] + legacy

    embedder = _RecordingEmbedder("fake/a")
    again = await _upload_and_ingest(store, embedder, _DOC_EDITED, tenant=tenant)
    assert (
        "[Document: guide]\n[Section: Alpha]\n\nalpha paragraph stays the same." in embedder.texts
    )
    after, _ = await store.list_chunks(tenant_id=tenant, document_id=again.id)
    assert [(c.heading_path, c.content) for c in after] == [
        (("Alpha",), "alpha paragraph stays the same."),
        (("Beta",), "beta paragraph, edited."),
    ]


async def test_parse_failure_codes() -> None:
    store = InMemoryKnowledgeStore()
    broken = await _upload_and_ingest(store, FakeEmbedder(), b"not a pdf", filename="x.pdf")
    assert broken.status is DocumentStatus.FAILED
    assert broken.error_code is IngestErrorCode.INVALID_FILE
    assert broken.stage is IngestStage.PARSING

    empty = await _upload_and_ingest(store, FakeEmbedder(), b"   ", filename="empty.md")
    assert empty.error_code is IngestErrorCode.PARSE_FAILED


async def test_embedding_failure_code_keeps_vendor_message() -> None:
    store = InMemoryKnowledgeStore()
    document = await _upload_and_ingest(store, _FailingEmbedder(), _DOC)
    assert document.status is DocumentStatus.FAILED
    assert document.error_code is IngestErrorCode.EMBEDDING_FAILED
    assert document.error is not None
    assert "input too long" in document.error


async def test_missing_bytes_code() -> None:
    store = InMemoryKnowledgeStore()
    tenant = uuid4()
    base = await store.create_base(tenant_id=tenant, name="kb")
    legacy = await store.upsert_document(
        tenant_id=tenant, kb_id=base.id, filename="old.md", content=None
    )
    runner = KnowledgeIngestionRunner(store=store, embedder=FakeEmbedder())
    await runner.submit(tenant_id=tenant, document_id=legacy.id)
    fetched = await store.get_document(tenant_id=tenant, document_id=legacy.id)
    assert fetched is not None
    assert fetched.error_code is IngestErrorCode.CONTENT_MISSING


async def test_reindex_keeps_heading_path_and_hash() -> None:
    store = InMemoryKnowledgeStore()
    document = await _upload_and_ingest(store, FakeEmbedder(), _DOC)
    before, _ = await store.list_chunks(tenant_id=document.tenant_id, document_id=document.id)
    embedder = _RecordingEmbedder("fake/c")
    runner = KnowledgeIngestionRunner(store=store, embedder=embedder)
    await runner.submit_reindex(
        tenant_id=document.tenant_id,
        kb_id=document.kb_id,
        embedding_provider="fake",
        embedding_model="fake-2",
    )
    after, _ = await store.list_chunks(tenant_id=document.tenant_id, document_id=document.id)
    assert [(c.heading_path, c.content, c.content_hash, c.doc_title) for c in after] == [
        (c.heading_path, c.content, c.content_hash, c.doc_title) for c in before
    ]
    assert {c.doc_title for c in after} == {"guide"}
    # Re-embedded from the title + breadcrumb + body, as at ingest time, and
    # tagged with the model that did it.
    assert embedder.texts == [c.retrieval_text for c in before]
    assert {c.embedding_model for c in after} == {"fake/c"}


def test_parse_error_code_survives_pickling() -> None:
    """Parsing runs in a process pool — the code must make the round trip."""
    exc = DocumentParseError("could not open PDF: bad header", IngestErrorCode.INVALID_FILE)
    restored = pickle.loads(pickle.dumps(exc))  # noqa: S301 - our own object
    assert str(restored) == "could not open PDF: bad header"
    assert restored.code is IngestErrorCode.INVALID_FILE
    assert ingestion.failure_code(restored) is IngestErrorCode.INVALID_FILE
