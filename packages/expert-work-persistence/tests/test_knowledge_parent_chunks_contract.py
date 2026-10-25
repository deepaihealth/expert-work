"""父段行的存储契约(B-206 K-1)—— **两个后端跑同一组断言**。

父段(``ChunkType.PARENT``)只存来读:没有向量、没有关键词索引,向量召回、关键词召回、
复用旧向量都不给它,关键词打分的段总数(BM25 的 N)也不数它;``list_chunks`` 默认只列
可检索类型,点名才列父段;``get_chunks`` 按 id 取任意类型、按请求顺序、跳过不存在的、
带租户条件。父段与子段各自编号(可以同号),父子关系(``parent_chunk_id``)原样往返。

内存档进单元 CI,SQL 档标 ``integration``,在容器里另建一个库。

本地跑 SQL 档前: export DOCKER_HOST=unix:///Users/mac/.docker/run/docker.sock
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, uuid4

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.exc import IntegrityError
from testcontainers.postgres import PostgresContainer

from expert_work.persistence import (
    DatabaseConfig,
    InMemoryKnowledgeStore,
    SqlKnowledgeStore,
    create_async_engine_from_config,
    create_async_session_factory,
)
from expert_work.persistence.embedding import EMBEDDING_DIM
from expert_work.persistence.knowledge.base import KnowledgeStore
from expert_work.protocol import ChunkType, KnowledgeChunk

ALEMBIC_INI = Path(__file__).resolve().parent.parent / "alembic.ini"
_DB = "knowledge_parent_chunks_contract"
_MODEL = "qwen/text-embedding-v4"


@pytest.fixture(
    params=[
        "memory",
        pytest.param("sql", marks=pytest.mark.integration),
    ]
)
async def store(request: pytest.FixtureRequest) -> AsyncIterator[KnowledgeStore]:
    if request.param == "memory":
        yield InMemoryKnowledgeStore()
        return

    container: PostgresContainer = request.getfixturevalue("postgres_container")
    admin = str(container.get_connection_url()).replace("+psycopg2", "")
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{_DB}" WITH (FORCE)')
        conn.execute(f'CREATE DATABASE "{_DB}"')
    base, _, _ = admin.rpartition("/")
    dsn = f"{base}/{_DB}"
    cfg = Config(str(ALEMBIC_INI))
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


def _vec(*head: float) -> tuple[float, ...]:
    return tuple(head) + (0.0,) * (EMBEDDING_DIM - len(head))


@dataclass(frozen=True)
class _Doc:
    tenant: UUID
    kb_id: UUID
    id: UUID

    def child(
        self,
        index: int,
        content: str,
        *,
        parent: KnowledgeChunk | None = None,
        embedding: tuple[float, ...] | None = None,
        content_hash: str | None = None,
    ) -> KnowledgeChunk:
        return KnowledgeChunk(
            id=uuid4(),
            tenant_id=self.tenant,
            kb_id=self.kb_id,
            document_id=self.id,
            chunk_index=index,
            content=content,
            embedding=embedding or _vec(1.0, 0.1 * index),
            parent_chunk_id=parent.id if parent is not None else None,
            content_hash=content_hash,
            embedding_model=_MODEL,
        )

    def parent(
        self,
        index: int,
        content: str,
        *,
        embedding: tuple[float, ...] | None = None,
        content_hash: str | None = None,
    ) -> KnowledgeChunk:
        return KnowledgeChunk(
            id=uuid4(),
            tenant_id=self.tenant,
            kb_id=self.kb_id,
            document_id=self.id,
            chunk_index=index,
            content=content,
            embedding=embedding,
            chunk_type=ChunkType.PARENT,
            heading_path=("条款",),
            content_hash=content_hash,
            embedding_model=_MODEL,
        )


async def _doc(store: KnowledgeStore, tenant: UUID | None = None, name: str = "a.md") -> _Doc:
    tenant = tenant or uuid4()
    base = await store.get_base(tenant_id=tenant, name="kb") or await store.create_base(
        tenant_id=tenant, name="kb"
    )
    document = await store.upsert_document(
        tenant_id=tenant, kb_id=base.id, filename=name, content=name.encode()
    )
    return _Doc(tenant, base.id, document.id)


async def _write(store: KnowledgeStore, doc: _Doc, chunks: list[KnowledgeChunk]) -> None:
    assert await store.replace_chunks(tenant_id=doc.tenant, document_id=doc.id, chunks=chunks)


async def _parented(
    store: KnowledgeStore,
) -> tuple[_Doc, list[KnowledgeChunk], list[KnowledgeChunk]]:
    """Two parents over three children; each numbering starts at 0. Children
    are written first, to show the write order is the store's business."""
    doc = await _doc(store)
    first, second = doc.parent(0, "苹果 适合 老年 人群"), doc.parent(1, "香蕉 甜点")
    children = [
        doc.child(0, "苹果 适合", parent=first),
        doc.child(1, "老年 人群", parent=first),
        doc.child(2, "香蕉 甜点", parent=second),
    ]
    await _write(store, doc, [*children, first, second])
    return doc, [first, second], children


# -- writes and reads ---------------------------------------------------------


async def test_parents_and_children_round_trip(store: KnowledgeStore) -> None:
    doc, parents, children = await _parented(store)

    listed, total = await store.list_chunks(tenant_id=doc.tenant, document_id=doc.id)
    assert total == 3
    assert [chunk.id for chunk in listed] == [chunk.id for chunk in children]
    assert [chunk.parent_chunk_id for chunk in listed] == [
        parents[0].id,
        parents[0].id,
        parents[1].id,
    ]
    assert {chunk.chunk_type for chunk in listed} == {ChunkType.TEXT}
    assert all(chunk.embedding == () for chunk in listed)  # the vector is not loaded

    listed, total = await store.list_chunks(
        tenant_id=doc.tenant, document_id=doc.id, chunk_types={ChunkType.PARENT}
    )
    assert total == 2
    assert [(chunk.id, chunk.chunk_index) for chunk in listed] == [
        (parents[0].id, 0),
        (parents[1].id, 1),
    ]
    assert [chunk.content for chunk in listed] == ["苹果 适合 老年 人群", "香蕉 甜点"]
    assert all(chunk.chunk_type is ChunkType.PARENT for chunk in listed)
    assert all(chunk.embedding is None and chunk.parent_chunk_id is None for chunk in listed)
    assert listed[0].heading_path == ("条款",)


async def test_list_chunks_pages_within_the_types_asked_for(store: KnowledgeStore) -> None:
    doc, parents, children = await _parented(store)
    page, total = await store.list_chunks(
        tenant_id=doc.tenant, document_id=doc.id, offset=1, limit=1
    )
    assert ([chunk.id for chunk in page], total) == ([children[1].id], 3)
    page, total = await store.list_chunks(
        tenant_id=doc.tenant,
        document_id=doc.id,
        chunk_types={ChunkType.PARENT},
        offset=1,
        limit=5,
    )
    assert ([chunk.id for chunk in page], total) == ([parents[1].id], 2)
    both, total = await store.list_chunks(
        tenant_id=doc.tenant,
        document_id=doc.id,
        chunk_types={ChunkType.PARENT, ChunkType.TEXT},
    )
    assert total == 5
    assert [(chunk.chunk_index, chunk.chunk_type) for chunk in both] == [
        (0, ChunkType.PARENT),
        (0, ChunkType.TEXT),
        (1, ChunkType.PARENT),
        (1, ChunkType.TEXT),
        (2, ChunkType.TEXT),
    ]


async def test_a_parent_is_stored_without_a_vector(store: KnowledgeStore) -> None:
    """A parent handed in with a vector is stored without one — never embedded."""
    doc = await _doc(store)
    parent = doc.parent(0, "苹果", embedding=_vec(1.0))
    await _write(store, doc, [parent, doc.child(0, "苹果", parent=parent)])
    (read,) = await store.get_chunks(tenant_id=doc.tenant, chunk_ids=[parent.id])
    assert read.embedding is None
    hits = await store.search_scored(
        tenant_id=doc.tenant, kb_ids=[doc.kb_id], query_embedding=_vec(1.0), limit=10
    )
    assert [hit.chunk.chunk_type for hit in hits] == [ChunkType.TEXT]


async def test_a_chunk_that_is_not_a_parent_needs_a_vector(store: KnowledgeStore) -> None:
    doc = await _doc(store)
    missing = doc.child(0, "苹果").model_copy(update={"embedding": None})
    with pytest.raises((ValueError, IntegrityError)):
        await store.replace_chunks(tenant_id=doc.tenant, document_id=doc.id, chunks=[missing])


async def test_chunk_index_is_unique_per_type(store: KnowledgeStore) -> None:
    doc = await _doc(store)
    with pytest.raises((ValueError, IntegrityError)):
        await store.replace_chunks(
            tenant_id=doc.tenant,
            document_id=doc.id,
            chunks=[doc.parent(0, "一"), doc.parent(0, "二"), doc.child(0, "一")],
        )


async def test_get_chunks_keeps_the_order_asked_and_skips_missing(store: KnowledgeStore) -> None:
    doc, parents, children = await _parented(store)
    asked = [children[2].id, uuid4(), parents[1].id, children[0].id, parents[0].id, children[2].id]
    found = await store.get_chunks(tenant_id=doc.tenant, chunk_ids=asked)
    assert [chunk.id for chunk in found] == [
        children[2].id,
        parents[1].id,
        children[0].id,
        parents[0].id,
    ]
    assert [chunk.content for chunk in found] == [
        "香蕉 甜点",
        "香蕉 甜点",
        "苹果 适合",
        "苹果 适合 老年 人群",
    ]
    assert found[0].parent_chunk_id == parents[1].id
    assert found[0].embedding == ()  # vectors are not loaded
    assert found[1].embedding is None
    assert await store.get_chunks(tenant_id=doc.tenant, chunk_ids=[]) == []


async def test_get_chunks_never_reads_another_tenants_rows(store: KnowledgeStore) -> None:
    doc, parents, children = await _parented(store)
    other = await _doc(store)
    stranger = other.child(0, "别人的")
    await _write(store, other, [stranger])
    ids = [parents[0].id, children[0].id, stranger.id]
    assert [c.id for c in await store.get_chunks(tenant_id=doc.tenant, chunk_ids=ids)] == ids[:2]
    assert [c.id for c in await store.get_chunks(tenant_id=other.tenant, chunk_ids=ids)] == [
        stranger.id
    ]


async def test_chunks_contain_reads_the_listed_chunks_only(store: KnowledgeStore) -> None:
    """``chunks_contain`` sees what ``list_chunks`` lists by default: a text
    only a parent row holds does not count."""
    doc = await _doc(store)
    parent = doc.parent(0, "见 [图 1] 与 [图 2]")
    await _write(store, doc, [parent, doc.child(0, "见 [图 1]", parent=parent)])
    assert await store.chunks_contain(tenant_id=doc.tenant, document_id=doc.id, text="[图 1]")
    assert not await store.chunks_contain(tenant_id=doc.tenant, document_id=doc.id, text="[图 2]")


# -- recall --------------------------------------------------------------------


async def test_vector_recall_never_returns_a_parent(store: KnowledgeStore) -> None:
    doc, _, children = await _parented(store)
    hits = await store.search_scored(
        tenant_id=doc.tenant, kb_ids=[doc.kb_id], query_embedding=_vec(1.0), limit=50
    )
    assert sorted(hit.chunk.id for hit in hits) == sorted(chunk.id for chunk in children)
    plain = await store.search(
        tenant_id=doc.tenant, kb_ids=[doc.kb_id], query_embedding=_vec(1.0), limit=50
    )
    assert {chunk.chunk_type for chunk in plain} == {ChunkType.TEXT}


async def test_keyword_recall_never_returns_a_parent_even_when_it_matches_best(
    store: KnowledgeStore,
) -> None:
    """The first parent holds every query word, the children only part of
    them: still only children come back."""
    doc, _, children = await _parented(store)
    hits = await store.keyword_search_scored(
        tenant_id=doc.tenant, kb_ids=[doc.kb_id], query="苹果适合哪些人群", limit=50
    )
    assert len(hits) == 2
    assert {hit.chunk.id for hit in hits} == {children[0].id, children[1].id}
    plain = await store.keyword_search(
        tenant_id=doc.tenant, kb_ids=[doc.kb_id], query="苹果适合哪些人群", limit=50
    )
    assert {chunk.chunk_type for chunk in plain} == {ChunkType.TEXT}


async def test_parents_do_not_change_keyword_scores(store: KnowledgeStore) -> None:
    """BM25's idf counts the chunks in scope (N): adding parent rows must
    leave every score as it was. The query's two words have different
    document frequencies, so a different N would move the coverage."""
    doc = await _doc(store)
    children = [
        doc.child(0, "苹果 沙拉"),
        doc.child(1, "苹果 派"),
        doc.child(2, "香蕉 甜点"),
        doc.child(3, "面包 牛奶"),
    ]
    await _write(store, doc, children)
    before = await store.keyword_search_scored(
        tenant_id=doc.tenant, kb_ids=[doc.kb_id], query="苹果和香蕉", limit=10
    )
    parents = [doc.parent(0, "苹果 沙拉 苹果 派"), doc.parent(1, "香蕉 甜点 面包 牛奶")]
    linked = [
        child.model_copy(update={"parent_chunk_id": parents[child.chunk_index // 2].id})
        for child in children
    ]
    await _write(store, doc, [*parents, *linked])
    after = await store.keyword_search_scored(
        tenant_id=doc.tenant, kb_ids=[doc.kb_id], query="苹果和香蕉", limit=10
    )
    assert [hit.chunk.chunk_type for hit in after] == [ChunkType.TEXT] * 3
    scores_before = {hit.chunk.content: hit.score for hit in before}
    assert {hit.chunk.content: hit.score for hit in after} == pytest.approx(scores_before, abs=1e-9)
    # The fixture is sensitive to N: the single-word hits score below 1.
    assert all(0 < hit.score < 1 for hit in before)


async def test_a_parent_vector_is_never_offered_for_reuse(store: KnowledgeStore) -> None:
    """A re-ingest looks stored vectors up by content hash; a parent sharing
    the hash (same text and breadcrumb as a child) has none to give."""
    doc = await _doc(store)
    parent = doc.parent(0, "苹果", content_hash="shared")
    child = doc.child(0, "苹果", parent=parent, embedding=_vec(0.5, 0.5), content_hash="shared")
    lone = doc.parent(1, "香蕉", content_hash="parent-only")
    await _write(store, doc, [child, parent, lone])
    found = await store.chunk_embeddings_by_hash(
        tenant_id=doc.tenant,
        document_id=doc.id,
        content_hashes=["shared", "parent-only"],
        embedding_model=_MODEL,
    )
    assert set(found) == {"shared"}
    assert list(found["shared"])[:2] == pytest.approx([0.5, 0.5])
