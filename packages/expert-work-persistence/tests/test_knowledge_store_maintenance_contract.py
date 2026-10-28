"""知识维护的存储契约(B-175 P3-PR2)—— **两个后端跑同一组断言**。

* 停用真的不召回:停用的文档、停用的整块(整块行 + 它的小段)、停用的单独小段,向量召回和
  关键词召回都不返回;关键词打分的段总数(BM25 的 N)与含词段数只数启用的;向量召回先过滤
  再取前 N 个(最近的 10 个全停用,要 5 个仍给 5 个启用的);列表照常列出停用的,带
  ``enabled=False``。
* 文档启停、按状态 / 启停筛选、参数过期的文档计数。

内存档进单元 CI,SQL 档标 ``integration``,在容器里另建一个库。

本地跑 SQL 档前: export DOCKER_HOST=unix:///Users/mac/.docker/run/docker.sock
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import event
from sqlalchemy.ext.asyncio import create_async_engine
from testcontainers.postgres import PostgresContainer

from expert_work.persistence import (
    DatabaseConfig,
    InMemoryKnowledgeStore,
    SqlKnowledgeStore,
    create_async_engine_from_config,
    create_async_session_factory,
)
from expert_work.persistence.embedding import EMBEDDING_DIM
from expert_work.persistence.knowledge.base import (
    ChunkVector,
    KnowledgeStore,
    Passage,
    PassageChild,
    PassageConflictError,
    SwitchOffRecord,
)
from expert_work.protocol import ChunkType, DocumentStatus, KnowledgeChunk

ALEMBIC_INI = Path(__file__).resolve().parent.parent / "alembic.ini"
_DB = "knowledge_store_maintenance_contract"
_MODEL = "qwen/text-embedding-v4"


@contextmanager
def _migrated_db(container: PostgresContainer) -> Iterator[str]:
    """A fresh database of the container, migrated to head (its asyncpg DSN)."""
    admin = str(container.get_connection_url()).replace("+psycopg2", "")
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{_DB}" WITH (FORCE)')
        conn.execute(f'CREATE DATABASE "{_DB}"')
    base, _, _ = admin.rpartition("/")
    dsn = f"{base}/{_DB}"
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", dsn.replace("postgresql://", "postgresql+psycopg://", 1))
    command.upgrade(cfg, "head")
    try:
        yield dsn.replace("postgresql://", "postgresql+asyncpg://", 1)
    finally:
        with psycopg.connect(admin, autocommit=True) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{_DB}" WITH (FORCE)')


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
    with _migrated_db(container) as dsn:
        engine = create_async_engine_from_config(DatabaseConfig(dsn=dsn))
        try:
            yield SqlKnowledgeStore(create_async_session_factory(engine))
        finally:
            await engine.dispose()


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
        enabled: bool = True,
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
            embedding_model=_MODEL,
            enabled=enabled,
        )

    def parent(self, index: int, content: str, *, enabled: bool = True) -> KnowledgeChunk:
        return KnowledgeChunk(
            id=uuid4(),
            tenant_id=self.tenant,
            kb_id=self.kb_id,
            document_id=self.id,
            chunk_index=index,
            content=content,
            embedding=None,
            chunk_type=ChunkType.PARENT,
            enabled=enabled,
        )


async def _doc(
    store: KnowledgeStore, tenant: UUID | None = None, name: str = "a.md", kb: str = "kb"
) -> _Doc:
    tenant = tenant or uuid4()
    base = await store.get_base(tenant_id=tenant, name=kb) or await store.create_base(
        tenant_id=tenant, name=kb
    )
    document = await store.upsert_document(
        tenant_id=tenant, kb_id=base.id, filename=name, content=name.encode()
    )
    return _Doc(tenant, base.id, document.id)


async def _write(
    store: KnowledgeStore,
    doc: _Doc,
    chunks: list[KnowledgeChunk],
    *,
    params_hash: str | None = None,
) -> None:
    assert await store.replace_chunks(
        tenant_id=doc.tenant, document_id=doc.id, chunks=chunks, chunk_params_hash=params_hash
    )


async def _ready(store: KnowledgeStore, doc: _Doc, chunks: int = 1) -> None:
    assert await store.set_document_status(
        tenant_id=doc.tenant, document_id=doc.id, status=DocumentStatus.READY, chunk_count=chunks
    )


async def _vector_ids(store: KnowledgeStore, doc: _Doc, limit: int = 50) -> set[UUID]:
    hits = await store.search_scored(
        tenant_id=doc.tenant, kb_ids=[doc.kb_id], query_embedding=_vec(1.0), limit=limit
    )
    return {hit.chunk.id for hit in hits}


async def _keyword_ids(store: KnowledgeStore, doc: _Doc, query: str) -> set[UUID]:
    hits = await store.keyword_search_scored(
        tenant_id=doc.tenant, kb_ids=[doc.kb_id], query=query, limit=50
    )
    return {hit.chunk.id for hit in hits}


# -- recall skips what is disabled ---------------------------------------------


async def test_a_disabled_document_is_not_recalled(store: KnowledgeStore) -> None:
    doc = await _doc(store)
    other = await _doc(store, doc.tenant, name="b.md")
    mine = doc.child(0, "苹果 适合 老年 人群")
    theirs = other.child(0, "苹果 适合 儿童")
    await _write(store, doc, [mine])
    await _write(store, other, [theirs])

    assert await store.set_documents_enabled(
        tenant_id=doc.tenant, kb_id=doc.kb_id, document_ids=[doc.id], enabled=False
    ) == [doc.id]

    assert await _vector_ids(store, doc) == {theirs.id}
    assert await _keyword_ids(store, doc, "苹果适合哪些人群") == {theirs.id}
    plain = await store.search(
        tenant_id=doc.tenant, kb_ids=[doc.kb_id], query_embedding=_vec(1.0), limit=50
    )
    assert [chunk.id for chunk in plain] == [theirs.id]

    # Enabled again: back in recall, its chunks untouched.
    await store.set_documents_enabled(
        tenant_id=doc.tenant, kb_id=doc.kb_id, document_ids=[doc.id], enabled=True
    )
    assert await _vector_ids(store, doc) == {mine.id, theirs.id}
    assert await _keyword_ids(store, doc, "苹果适合哪些人群") == {mine.id, theirs.id}


async def test_a_disabled_passage_is_not_recalled(store: KnowledgeStore) -> None:
    """A disabled parent with its children, and a disabled lone chunk."""
    doc = await _doc(store)
    off = doc.parent(0, "苹果 适合 老年 人群", enabled=False)
    on = doc.parent(1, "苹果 甜点")
    children = [
        doc.child(0, "苹果 适合", parent=off, enabled=False),
        doc.child(1, "老年 人群", parent=off, enabled=False),
        doc.child(2, "苹果 甜点", parent=on),
    ]
    lone = doc.child(3, "苹果 派", enabled=False)
    await _write(store, doc, [off, on, *children, lone])

    assert await _vector_ids(store, doc) == {children[2].id}
    assert await _keyword_ids(store, doc, "苹果 老年 人群 派") == {children[2].id}


async def test_vector_recall_filters_before_the_limit(store: KnowledgeStore) -> None:
    """The 10 nearest chunks are all disabled: asking for 5 still gives the 5
    enabled ones (the filter runs in the scan, not on a cut-off top 5)."""
    doc = await _doc(store)
    near = [
        doc.child(index, f"近 {index}", embedding=_vec(1.0, 0.01 * index), enabled=False)
        for index in range(10)
    ]
    far = [
        doc.child(10 + index, f"远 {index}", embedding=_vec(1.0, 1.0 + index)) for index in range(5)
    ]
    await _write(store, doc, [*near, *far])
    assert await _vector_ids(store, doc, limit=5) == {chunk.id for chunk in far}


@pytest.mark.integration
async def test_vector_recall_filters_on_the_hnsw_scan(
    postgres_container: PostgresContainer,
) -> None:
    """The same on the HNSW index path: with sequential scans and sorts off
    (on a table this small the planner would otherwise scan and sort), the
    vector statement the store runs walks ``knowledge_chunk_embedding_idx``
    (its plan says so) and the disabled rows are filtered in that scan — the
    5 enabled ones still come back."""
    with _migrated_db(postgres_container) as dsn:
        engine = create_async_engine(
            dsn,
            connect_args={"server_settings": {"enable_seqscan": "off", "enable_sort": "off"}},
        )
        seen: list[tuple[str, Any]] = []

        def capture(*args: Any) -> None:
            statement, parameters = args[2], args[3]
            if "<=>" in statement:
                seen.append((statement, parameters))

        event.listen(engine.sync_engine, "before_cursor_execute", capture)
        try:
            store = SqlKnowledgeStore(create_async_session_factory(engine))
            doc = await _doc(store)
            near = [
                doc.child(index, f"近 {index}", embedding=_vec(1.0, 0.01 * index), enabled=False)
                for index in range(10)
            ]
            far = [
                doc.child(10 + index, f"远 {index}", embedding=_vec(1.0, 1.0 + index))
                for index in range(5)
            ]
            await _write(store, doc, [*near, *far])
            assert await _vector_ids(store, doc, limit=5) == {chunk.id for chunk in far}
            ((statement, parameters),) = seen
            async with engine.connect() as conn:
                plan = (
                    (await conn.exec_driver_sql(f"EXPLAIN {statement}", parameters)).scalars().all()
                )
            assert any("knowledge_chunk_embedding_idx" in line for line in plan), plan
            assert any("enabled" in line for line in plan), plan
        finally:
            await engine.dispose()


async def test_bm25_counts_only_enabled_chunks(store: KnowledgeStore) -> None:
    """A disabled chunk is not one of BM25's N nor of a term's df: the other
    chunks score as if it never existed. The two query words have different
    df, so a wrong N or df moves the coverage."""
    doc = await _doc(store)
    kept = [doc.child(0, "苹果 沙拉"), doc.child(2, "香蕉 甜点"), doc.child(3, "面包 牛奶")]
    await _write(store, doc, kept)
    before = await store.keyword_search_scored(
        tenant_id=doc.tenant, kb_ids=[doc.kb_id], query="苹果和香蕉", limit=10
    )
    await _write(store, doc, [*kept, doc.child(1, "苹果 派", enabled=False)])
    after = await store.keyword_search_scored(
        tenant_id=doc.tenant, kb_ids=[doc.kb_id], query="苹果和香蕉", limit=10
    )
    scores_before = {hit.chunk.content: hit.score for hit in before}
    assert {hit.chunk.content: hit.score for hit in after} == pytest.approx(scores_before, abs=1e-9)
    assert all(0 < hit.score < 1 for hit in before)  # the fixture is sensitive to N


async def test_bm25_counts_only_enabled_documents(store: KnowledgeStore) -> None:
    doc = await _doc(store)
    await _write(store, doc, [doc.child(0, "苹果 沙拉"), doc.child(1, "香蕉 甜点")])
    before = await store.keyword_search_scored(
        tenant_id=doc.tenant, kb_ids=[doc.kb_id], query="苹果和香蕉", limit=10
    )
    other = await _doc(store, doc.tenant, name="b.md")
    await _write(store, other, [other.child(0, "苹果 派"), other.child(1, "面包 牛奶")])
    await store.set_documents_enabled(
        tenant_id=doc.tenant, kb_id=doc.kb_id, document_ids=[other.id], enabled=False
    )
    after = await store.keyword_search_scored(
        tenant_id=doc.tenant, kb_ids=[doc.kb_id], query="苹果和香蕉", limit=10
    )
    scores_before = {hit.chunk.content: hit.score for hit in before}
    assert {hit.chunk.content: hit.score for hit in after} == pytest.approx(scores_before, abs=1e-9)


async def test_listing_still_shows_what_is_disabled(store: KnowledgeStore) -> None:
    doc = await _doc(store)
    await _write(store, doc, [doc.child(0, "苹果", enabled=False), doc.child(1, "香蕉")])
    await store.set_documents_enabled(
        tenant_id=doc.tenant, kb_id=doc.kb_id, document_ids=[doc.id], enabled=False
    )
    listed, total = await store.list_chunks(tenant_id=doc.tenant, document_id=doc.id)
    assert total == 2
    assert [chunk.enabled for chunk in listed] == [False, True]
    (document,) = await store.list_documents(tenant_id=doc.tenant, kb_id=doc.kb_id)
    assert document.enabled is False
    fetched = await store.get_document(tenant_id=doc.tenant, document_id=doc.id)
    assert fetched is not None and fetched.enabled is False


# -- document switches, filters, stale count -----------------------------------


async def test_set_documents_enabled_touches_only_this_bases_documents(
    store: KnowledgeStore,
) -> None:
    doc = await _doc(store)
    sibling = await _doc(store, doc.tenant, name="b.md")
    elsewhere = await _doc(store, doc.tenant, name="c.md", kb="other")
    stranger = await _doc(store)
    asked = [sibling.id, uuid4(), elsewhere.id, doc.id, stranger.id, doc.id]
    done = await store.set_documents_enabled(
        tenant_id=doc.tenant, kb_id=doc.kb_id, document_ids=asked, enabled=False
    )
    assert done == [sibling.id, doc.id]
    for target, enabled in ((doc, False), (sibling, False), (elsewhere, True)):
        got = await store.get_document(tenant_id=doc.tenant, document_id=target.id)
        assert got is not None and got.enabled is enabled
    theirs = await store.get_document(tenant_id=stranger.tenant, document_id=stranger.id)
    assert theirs is not None and theirs.enabled is True
    assert (
        await store.set_documents_enabled(
            tenant_id=doc.tenant, kb_id=doc.kb_id, document_ids=[], enabled=True
        )
        == []
    )


async def test_list_documents_filters_by_enabled_and_status(store: KnowledgeStore) -> None:
    doc = await _doc(store, name="a.md")
    ready = await _doc(store, doc.tenant, name="b.md")
    off = await _doc(store, doc.tenant, name="c.md")
    await _ready(store, ready)
    await _ready(store, off)
    await store.set_documents_enabled(
        tenant_id=doc.tenant, kb_id=doc.kb_id, document_ids=[off.id], enabled=False
    )

    async def names(**filters: object) -> tuple[set[UUID], int, set[UUID]]:
        page, total = await store.list_documents_page(
            tenant_id=doc.tenant,
            kb_id=doc.kb_id,
            limit=10,
            offset=0,
            **filters,  # type: ignore[arg-type]
        )
        everything = await store.list_documents(
            tenant_id=doc.tenant,
            kb_id=doc.kb_id,
            **filters,  # type: ignore[arg-type]
        )
        return {d.id for d in page}, total, {d.id for d in everything}

    assert await names() == ({doc.id, ready.id, off.id}, 3, {doc.id, ready.id, off.id})
    assert await names(enabled=False) == ({off.id}, 1, {off.id})
    assert await names(enabled=True) == ({doc.id, ready.id}, 2, {doc.id, ready.id})
    assert await names(statuses=[DocumentStatus.READY]) == (
        {ready.id, off.id},
        2,
        {ready.id, off.id},
    )
    assert await names(statuses=[DocumentStatus.PENDING], enabled=True) == (
        {doc.id},
        1,
        {doc.id},
    )
    assert await names(statuses=[]) == (set(), 0, set())


async def test_count_stale_documents(store: KnowledgeStore) -> None:
    """Stale = serving chunks cut by other chunking parameters: ready, and its
    ``chunk_params_hash`` differs from the base's or was never recorded."""
    current = await _doc(store, name="current.md")
    old = await _doc(store, current.tenant, name="old.md")
    unrecorded = await _doc(store, current.tenant, name="unrecorded.md")
    pending = await _doc(store, current.tenant, name="pending.md")
    for target, params in ((current, "now"), (old, "before"), (unrecorded, None)):
        await _write(store, target, [target.child(0, "苹果")], params_hash=params)
        await _ready(store, target)
    await _write(store, pending, [pending.child(0, "苹果")], params_hash="before")
    elsewhere = await _doc(store, current.tenant, name="x.md", kb="other")
    await _write(store, elsewhere, [elsewhere.child(0, "苹果")], params_hash="before")
    await _ready(store, elsewhere)

    assert (
        await store.count_stale_documents(
            tenant_id=current.tenant, kb_id=current.kb_id, params_hash="now"
        )
        == 2
    )
    stale = await store.list_stale_documents(
        tenant_id=current.tenant, kb_id=current.kb_id, params_hash="now"
    )
    assert {d.id for d in stale} == {old.id, unrecorded.id}
    assert (
        await store.count_stale_documents(tenant_id=uuid4(), kb_id=current.kb_id, params_hash="now")
        == 0
    )


# -- passages ------------------------------------------------------------------


def _kids(*texts: str, heading: tuple[str, ...] = ("条款",)) -> list[PassageChild]:
    return [
        PassageChild(
            content=text,
            heading_path=heading,
            content_hash=f"hash:{text}",
            embedding=_vec(1.0, 0.5),
            embedding_model=_MODEL,
        )
        for text in texts
    ]


@dataclass(frozen=True)
class _Passages:
    doc: _Doc
    first: KnowledgeChunk  # parent of children 0, 1
    lone: KnowledgeChunk  # chunk 2, no parent row
    second: KnowledgeChunk  # parent of children 3, 4
    children: tuple[KnowledgeChunk, ...]


async def _passages(
    store: KnowledgeStore, tenant: UUID | None = None, name: str = "a.md"
) -> _Passages:
    """A ready document: parent ``first`` (chunks 0, 1), lone chunk 2, parent
    ``second`` (chunks 3, 4). The parents' own numbers (0, 1) say nothing
    about where they sit; written in a shuffled order on purpose."""
    doc = await _doc(store, tenant, name=name)
    first, second = doc.parent(0, "苹果 适合\n\n老年 人群"), doc.parent(1, "香蕉 甜点\n\n面包 牛奶")
    lone = doc.child(2, "樱桃 果酱")
    children = (
        doc.child(0, "苹果 适合", parent=first),
        doc.child(1, "老年 人群", parent=first),
        lone,
        doc.child(3, "香蕉 甜点", parent=second),
        doc.child(4, "面包 牛奶", parent=second),
    )
    await _write(
        store, doc, [children[4], second, lone, children[0], first, children[1], children[3]]
    )
    await _ready(store, doc, chunks=5)
    return _Passages(doc, first, lone, second, children)


def _shape(passages: list[Passage]) -> list[tuple[UUID, list[UUID]]]:
    return [(p.id, [c.id for c in p.children]) for p in passages]


async def _listed(store: KnowledgeStore, doc: _Doc, **kwargs: object) -> list[Passage]:
    return await store.list_passages(
        tenant_id=doc.tenant,
        document_id=doc.id,
        **kwargs,  # type: ignore[arg-type]
    )


async def test_list_passages_in_document_order_with_children(store: KnowledgeStore) -> None:
    p = await _passages(store)
    c = p.children
    listed = await _listed(store, p.doc)
    assert _shape(listed) == [
        (p.first.id, [c[0].id, c[1].id]),
        (p.lone.id, [p.lone.id]),
        (p.second.id, [c[3].id, c[4].id]),
    ]
    assert listed[0].row.chunk_type is ChunkType.PARENT
    assert listed[0].row.content == "苹果 适合\n\n老年 人群"
    assert listed[1].row.chunk_type is ChunkType.TEXT
    assert all(child.embedding == () for passage in listed for child in passage.children)
    assert await store.count_passages(tenant_id=p.doc.tenant, document_id=p.doc.id) == 3
    assert _shape(await _listed(store, p.doc, offset=1, limit=1)) == [(p.lone.id, [p.lone.id])]
    assert _shape(await _listed(store, p.doc, offset=2)) == [(p.second.id, [c[3].id, c[4].id])]


async def test_list_passages_enabled_only_skips_what_recall_skips(store: KnowledgeStore) -> None:
    p = await _passages(store)
    await store.set_passage_enabled(
        tenant_id=p.doc.tenant, document_id=p.doc.id, passage_id=p.second.id, enabled=False
    )
    assert [x.id for x in await _listed(store, p.doc)] == [p.first.id, p.lone.id, p.second.id]
    assert [x.id for x in await _listed(store, p.doc, enabled_only=True)] == [
        p.first.id,
        p.lone.id,
    ]
    await store.set_documents_enabled(
        tenant_id=p.doc.tenant, kb_id=p.doc.kb_id, document_ids=[p.doc.id], enabled=False
    )
    assert await _listed(store, p.doc, enabled_only=True) == []
    assert len(await _listed(store, p.doc)) == 3


async def test_disabling_a_parent_switches_its_children(store: KnowledgeStore) -> None:
    p = await _passages(store)
    off = await store.set_passage_enabled(
        tenant_id=p.doc.tenant, document_id=p.doc.id, passage_id=p.first.id, enabled=False
    )
    assert off is not None
    assert off.row.enabled is False
    assert [child.enabled for child in off.children] == [False, False]
    assert await _vector_ids(store, p.doc) == {p.lone.id, p.children[3].id, p.children[4].id}
    lone = await store.set_passage_enabled(
        tenant_id=p.doc.tenant, document_id=p.doc.id, passage_id=p.lone.id, enabled=False
    )
    assert lone is not None and lone.row.enabled is False
    assert await _vector_ids(store, p.doc) == {p.children[3].id, p.children[4].id}
    on = await store.set_passage_enabled(
        tenant_id=p.doc.tenant, document_id=p.doc.id, passage_id=p.first.id, enabled=True
    )
    assert on is not None and [child.enabled for child in on.children] == [True, True]
    # A child is not a passage: it is switched with its parent only.
    assert (
        await store.set_passage_enabled(
            tenant_id=p.doc.tenant,
            document_id=p.doc.id,
            passage_id=p.children[0].id,
            enabled=False,
        )
        is None
    )


async def test_replace_passage_keeps_its_id_and_numbers(store: KnowledgeStore) -> None:
    p = await _passages(store)
    c = p.children
    edited = await store.replace_passage(
        tenant_id=p.doc.tenant,
        document_id=p.doc.id,
        passage_id=p.first.id,
        expected_revision=1,
        content="苹果 适合\n\n老人 小孩\n\n孕妇 慎用",
        heading_path=("条款", "人群"),
        children=_kids("苹果 适合", "老人 小孩", "孕妇 慎用", heading=("条款", "人群")),
        edited_by="user-a",
    )
    assert edited is not None
    assert edited.id == p.first.id
    assert edited.row.content == "苹果 适合\n\n老人 小孩\n\n孕妇 慎用"
    assert edited.row.heading_path == ("条款", "人群")
    assert (edited.row.revision, edited.row.edited) == (2, True)
    # Chunk numbers 0 and 1 are reused; the extra one comes after the last (4).
    assert [child.chunk_index for child in edited.children] == [0, 1, 5]
    assert [child.content for child in edited.children] == ["苹果 适合", "老人 小孩", "孕妇 慎用"]
    assert [child.content_hash for child in edited.children] == [
        "hash:苹果 适合",
        "hash:老人 小孩",
        "hash:孕妇 慎用",
    ]
    assert all(child.parent_chunk_id == p.first.id for child in edited.children)
    assert all(child.heading_path == ("条款", "人群") for child in edited.children)
    document = await store.get_document(tenant_id=p.doc.tenant, document_id=p.doc.id)
    assert document is not None and document.chunk_count == 6
    # The keyword index follows the new text.
    assert await _keyword_ids(store, p.doc, "孕妇") == {edited.children[2].id}
    assert await _keyword_ids(store, p.doc, "老年") == set()
    assert _shape(await _listed(store, p.doc))[1:] == [
        (p.lone.id, [p.lone.id]),
        (p.second.id, [c[3].id, c[4].id]),
    ]

    # Fewer chunks: the surplus ones go.
    shorter = await store.replace_passage(
        tenant_id=p.doc.tenant,
        document_id=p.doc.id,
        passage_id=p.first.id,
        expected_revision=2,
        content="苹果",
        heading_path=("条款",),
        children=_kids("苹果"),
        edited_by="user-a",
    )
    assert shorter is not None
    assert [child.chunk_index for child in shorter.children] == [0]
    document = await store.get_document(tenant_id=p.doc.tenant, document_id=p.doc.id)
    assert document is not None and document.chunk_count == 4
    assert await _keyword_ids(store, p.doc, "孕妇") == set()


async def test_a_lone_chunk_cut_into_several_becomes_a_parent(store: KnowledgeStore) -> None:
    p = await _passages(store)
    edited = await store.replace_passage(
        tenant_id=p.doc.tenant,
        document_id=p.doc.id,
        passage_id=p.lone.id,
        expected_revision=1,
        content="樱桃 果酱\n\n草莓 果酱",
        heading_path=(),
        children=_kids("樱桃 果酱", "草莓 果酱", heading=()),
        edited_by="user-a",
    )
    assert edited is not None
    assert edited.id == p.lone.id
    assert edited.row.chunk_type is ChunkType.PARENT
    assert edited.row.chunk_index == 2  # after the parents 0 and 1
    assert [child.chunk_index for child in edited.children] == [2, 5]
    assert all(child.id != p.lone.id for child in edited.children)
    assert [x.id for x in await _listed(store, p.doc)] == [p.first.id, p.lone.id, p.second.id]
    assert await _vector_ids(store, p.doc) >= {child.id for child in edited.children}
    assert p.lone.id not in await _vector_ids(store, p.doc)


async def test_replace_passage_refuses_a_stale_revision(store: KnowledgeStore) -> None:
    p = await _passages(store)
    with pytest.raises(PassageConflictError) as refused:
        await store.replace_passage(
            tenant_id=p.doc.tenant,
            document_id=p.doc.id,
            passage_id=p.lone.id,
            expected_revision=2,
            content="樱桃",
            heading_path=(),
            children=_kids("樱桃", heading=()),
            edited_by="user-a",
        )
    assert refused.value.reason == "revision_conflict"
    assert refused.value.current is not None
    assert refused.value.current.row.content == "樱桃 果酱"
    assert refused.value.current.row.revision == 1
    assert (
        await store.list_passage_revisions(
            tenant_id=p.doc.tenant, document_id=p.doc.id, passage_id=p.lone.id
        )
        == []
    )


async def test_passage_writes_wait_for_a_settled_document(store: KnowledgeStore) -> None:
    """While a new version is pending or being ingested, the passages are
    about to be replaced: every passage write is refused."""
    p = await _passages(store)
    await store.upsert_document(
        tenant_id=p.doc.tenant, kb_id=p.doc.kb_id, filename="a.md", content=b"new version"
    )
    attempts = [
        store.replace_passage(
            tenant_id=p.doc.tenant,
            document_id=p.doc.id,
            passage_id=p.lone.id,
            expected_revision=1,
            content="樱桃",
            heading_path=(),
            children=_kids("樱桃", heading=()),
            edited_by="user-a",
        ),
        store.add_passage(
            tenant_id=p.doc.tenant,
            document_id=p.doc.id,
            content="樱桃",
            heading_path=(),
            children=_kids("樱桃", heading=()),
            edited_by="user-a",
        ),
        store.set_passage_enabled(
            tenant_id=p.doc.tenant, document_id=p.doc.id, passage_id=p.lone.id, enabled=False
        ),
    ]
    for attempt in attempts:
        with pytest.raises(PassageConflictError) as refused:
            await attempt
        assert refused.value.reason == "document_busy"
    assert [x.row.revision for x in await _listed(store, p.doc)] == [1, 1, 1]


async def test_edit_history_and_restore(store: KnowledgeStore) -> None:
    p = await _passages(store)
    scope: dict[str, Any] = {
        "tenant_id": p.doc.tenant,
        "document_id": p.doc.id,
        "passage_id": p.lone.id,
    }
    await store.replace_passage(
        **scope,
        expected_revision=1,
        content="樱桃 酱",
        heading_path=("条款",),
        children=_kids("樱桃 酱"),
        edited_by="user-a",
    )
    history = await store.list_passage_revisions(**scope)
    assert [(r.revision, r.content, r.heading_path, r.edited_by) for r in history] == [
        (1, "樱桃 果酱", (), None),
        (2, "樱桃 酱", ("条款",), "user-a"),
    ]
    # Restoring revision 1 is a change of its own: revision 3, no longer edited.
    restored = await store.replace_passage(
        **scope,
        expected_revision=2,
        content=history[0].content,
        heading_path=history[0].heading_path,
        children=_kids("樱桃 果酱", heading=()),
        edited_by="user-b",
    )
    assert restored is not None
    assert (restored.row.revision, restored.row.edited) == (3, False)
    assert restored.row.content == "樱桃 果酱"
    assert [r.revision for r in await store.list_passage_revisions(**scope)] == [1, 2, 3]
    again = await store.replace_passage(
        **scope,
        expected_revision=3,
        content="樱桃",
        heading_path=(),
        children=_kids("樱桃", heading=()),
        edited_by="user-a",
    )
    assert again is not None and (again.row.revision, again.row.edited) == (4, True)


async def test_add_passage_goes_at_the_end(store: KnowledgeStore) -> None:
    p = await _passages(store)
    one = await store.add_passage(
        tenant_id=p.doc.tenant,
        document_id=p.doc.id,
        content="葡萄 干",
        heading_path=("附录",),
        children=_kids("葡萄 干", heading=("附录",)),
        edited_by="user-a",
    )
    two = await store.add_passage(
        tenant_id=p.doc.tenant,
        document_id=p.doc.id,
        content="西瓜\n\n冬瓜",
        heading_path=("附录",),
        children=_kids("西瓜", "冬瓜", heading=("附录",)),
        edited_by="user-b",
    )
    assert one is not None and two is not None
    # One chunk that is the whole text stands for itself; two get a parent row.
    assert one.row.chunk_type is ChunkType.TEXT and one.children == (one.row,)
    assert one.row.chunk_index == 5
    assert two.row.chunk_type is ChunkType.PARENT and two.row.chunk_index == 2
    assert [child.chunk_index for child in two.children] == [6, 7]
    for added, author in ((one, "user-a"), (two, "user-b")):
        assert (added.row.revision, added.row.edited, added.row.enabled) == (1, True, True)
        (first,) = await store.list_passage_revisions(
            tenant_id=p.doc.tenant, document_id=p.doc.id, passage_id=added.id
        )
        assert (first.revision, first.content, first.edited_by) == (1, added.row.content, author)
    assert [x.id for x in await _listed(store, p.doc)][-2:] == [one.id, two.id]
    document = await store.get_document(tenant_id=p.doc.tenant, document_id=p.doc.id)
    assert document is not None and document.chunk_count == 8
    assert await _keyword_ids(store, p.doc, "冬瓜") == {two.children[1].id}


async def test_manual_passages_and_unapplied_revisions(store: KnowledgeStore) -> None:
    p = await _passages(store)
    t, d = p.doc.tenant, p.doc.id
    await store.replace_passage(
        tenant_id=t,
        document_id=d,
        passage_id=p.first.id,
        expected_revision=1,
        content="苹果",
        heading_path=("条款",),
        children=_kids("苹果"),
        edited_by="user-a",
    )
    await store.set_passage_enabled(
        tenant_id=t, document_id=d, passage_id=p.second.id, enabled=False
    )
    added = await store.add_passage(
        tenant_id=t,
        document_id=d,
        content="葡萄",
        heading_path=(),
        children=_kids("葡萄", heading=()),
        edited_by="user-b",
    )
    assert added is not None

    manual = await store.list_manual_passages(tenant_id=t, document_id=d)
    assert [m.row.id for m in manual] == [p.first.id, p.second.id, added.id]
    edited, off, mine = manual
    assert (edited.anchor, edited.added, edited.row.content) == (
        "苹果 适合\n\n老年 人群",
        False,
        "苹果",
    )
    assert edited.original is not None and edited.original.edited_by is None
    assert (off.anchor, off.added, off.original, off.row.enabled) == (
        "香蕉 甜点\n\n面包 牛奶",
        False,
        None,
        False,
    )
    assert (mine.anchor, mine.added) == (None, True)
    assert await store.list_unapplied_revisions(tenant_id=t, document_id=d) == []

    # Chunks written without the manual changes (no re-ingest does that — it
    # carries them over or lists them): the edited and the added passage are
    # gone, their histories stay and their latest revisions are listed.
    doc = p.doc
    await _write(store, doc, [doc.child(0, "苹果 适合 老年 人群")])
    unapplied = await store.list_unapplied_revisions(tenant_id=t, document_id=d)
    assert [(r.passage_id, r.revision, r.content) for r in unapplied] == [
        (p.first.id, 2, "苹果"),
        (added.id, 1, "葡萄"),
    ]
    assert await store.list_manual_passages(tenant_id=t, document_id=d) == []

    # Still there, at an older revision than its history (chunks written back
    # from before the edit): listed too.
    await _write(store, doc, [p.first, *p.children])
    unapplied = await store.list_unapplied_revisions(tenant_id=t, document_id=d)
    assert [(r.passage_id, r.revision) for r in unapplied] == [(p.first.id, 2), (added.id, 1)]


async def test_a_restored_passage_that_is_gone_is_not_unapplied(store: KnowledgeStore) -> None:
    p = await _passages(store)
    scope: dict[str, Any] = {
        "tenant_id": p.doc.tenant,
        "document_id": p.doc.id,
        "passage_id": p.lone.id,
    }
    for revision, text in ((1, "樱桃"), (2, "樱桃 果酱")):
        await store.replace_passage(
            **scope,
            expected_revision=revision,
            content=text,
            heading_path=(),
            children=_kids(text, heading=()),
            edited_by="user-a",
        )
    # Back to its machine text, yet still carried over (it has a history).
    (manual,) = await store.list_manual_passages(tenant_id=p.doc.tenant, document_id=p.doc.id)
    assert (manual.row.id, manual.row.edited, manual.row.revision) == (p.lone.id, False, 3)
    await _write(store, p.doc, [p.doc.child(0, "别的")])
    assert await store.list_unapplied_revisions(tenant_id=p.doc.tenant, document_id=p.doc.id) == []


async def test_deleting_a_document_or_base_deletes_its_revisions(store: KnowledgeStore) -> None:
    p = await _passages(store)
    t = p.doc.tenant
    for passage in (p.first, p.lone):
        await store.replace_passage(
            tenant_id=t,
            document_id=p.doc.id,
            passage_id=passage.id,
            expected_revision=1,
            content="改",
            heading_path=(),
            children=_kids("改", heading=()),
            edited_by="user-a",
        )
    other = await _passages(store, t, name="b.md")  # same base
    added = await store.add_passage(
        tenant_id=t,
        document_id=other.doc.id,
        content="加",
        heading_path=(),
        children=_kids("加", heading=()),
        edited_by="user-a",
    )
    assert added is not None

    assert await store.delete_document(tenant_id=t, document_id=p.doc.id)
    for passage in (p.first, p.lone):
        assert (
            await store.list_passage_revisions(
                tenant_id=t, document_id=p.doc.id, passage_id=passage.id
            )
            == []
        )
    # Left behind, they would show as manual changes no chunk carries.
    assert await store.list_unapplied_revisions(tenant_id=t, document_id=p.doc.id) == []
    assert (
        len(
            await store.list_passage_revisions(
                tenant_id=t, document_id=other.doc.id, passage_id=added.id
            )
        )
        == 1
    )

    assert await store.delete_base(tenant_id=t, kb_id=other.doc.kb_id)
    assert await store.list_unapplied_revisions(tenant_id=t, document_id=other.doc.id) == []
    assert (
        await store.list_passage_revisions(
            tenant_id=t, document_id=other.doc.id, passage_id=added.id
        )
        == []
    )


async def test_passages_never_cross_tenants(store: KnowledgeStore) -> None:
    p = await _passages(store)
    stranger = uuid4()
    scope: dict[str, Any] = {"tenant_id": stranger, "document_id": p.doc.id}
    assert await store.list_passages(**scope) == []
    assert await store.count_passages(**scope) == 0
    assert await store.get_passage(**scope, passage_id=p.first.id) is None
    assert await store.set_passage_enabled(**scope, passage_id=p.first.id, enabled=False) is None
    assert (
        await store.replace_passage(
            **scope,
            passage_id=p.first.id,
            expected_revision=1,
            content="x",
            heading_path=(),
            children=_kids("x", heading=()),
            edited_by="user-a",
        )
        is None
    )
    assert (
        await store.add_passage(
            **scope, content="x", heading_path=(), children=_kids("x"), edited_by="user-a"
        )
        is None
    )
    assert await store.list_manual_passages(**scope) == []
    found = await store.get_passage(
        tenant_id=p.doc.tenant, document_id=p.doc.id, passage_id=p.first.id
    )
    assert found is not None and found.row.enabled is True and found.row.revision == 1
    # Another document's passage is not this document's.
    other = await _passages(store, p.doc.tenant, name="b.md")
    assert (
        await store.get_passage(
            tenant_id=p.doc.tenant, document_id=other.doc.id, passage_id=p.first.id
        )
        is None
    )


# -- why a passage is off --------------------------------------------------------


async def test_a_switch_off_records_why(store: KnowledgeStore) -> None:
    p = await _passages(store)
    scope: dict[str, Any] = {"tenant_id": p.doc.tenant, "document_id": p.doc.id}
    off = await store.set_passage_enabled(**scope, passage_id=p.first.id, enabled=False)
    assert off is not None
    assert [off.row.disabled_reason, *(c.disabled_reason for c in off.children)] == ["manual"] * 3
    # An edit keeps the switch and its reason.
    edited = await store.replace_passage(
        **scope,
        passage_id=p.first.id,
        expected_revision=1,
        content="苹果",
        heading_path=("条款",),
        children=_kids("苹果"),
        edited_by="user-a",
    )
    assert edited is not None
    assert (edited.row.enabled, edited.row.disabled_reason) == (False, "manual")
    assert [c.disabled_reason for c in edited.children] == ["manual"]
    on = await store.set_passage_enabled(**scope, passage_id=p.first.id, enabled=True)
    assert on is not None
    assert [on.row.disabled_reason, *(c.disabled_reason for c in on.children)] == [None, None]


async def test_inherited_switch_offs_round_trip_and_filter(store: KnowledgeStore) -> None:
    doc = await _doc(store)
    held = doc.parent(0, "苹果\n\n香蕉", enabled=False).model_copy(
        update={"disabled_reason": "inherited"}
    )
    kids = [
        doc.child(0, "苹果", parent=held, enabled=False).model_copy(
            update={"disabled_reason": "inherited"}
        ),
        doc.child(1, "香蕉", parent=held, enabled=False).model_copy(
            update={"disabled_reason": "inherited"}
        ),
    ]
    mine = doc.child(2, "樱桃", enabled=False).model_copy(update={"disabled_reason": "manual"})
    free = doc.child(3, "面包")
    await _write(store, doc, [held, *kids, mine, free])
    listed = await _listed(store, doc)
    assert [(p.id, p.row.disabled_reason) for p in listed] == [
        (held.id, "inherited"),
        (mine.id, "manual"),
        (free.id, None),
    ]
    assert [c.disabled_reason for c in listed[0].children] == ["inherited", "inherited"]
    assert [p.id for p in await _listed(store, doc, disabled_reason="inherited")] == [held.id]
    assert [p.id for p in await _listed(store, doc, disabled_reason="manual")] == [mine.id]


# -- re-index: vectors rewritten in place ------------------------------------------


async def test_update_chunk_embeddings_never_undoes_a_passage_write(store: KnowledgeStore) -> None:
    """A re-index read the chunks, then passage writes landed: its write-back
    rewrites only the vectors of chunks that still have the id and text it
    read — a switch-off stays off, an edit and an added passage stay."""
    p = await _passages(store)
    t, d = p.doc.tenant, p.doc.id
    document = await store.get_document(tenant_id=t, document_id=d)
    assert document is not None
    snapshot, _ = await store.list_chunks(tenant_id=t, document_id=d, limit=100)
    # Meanwhile: switch the second passage off, edit the first, add one.
    await store.set_passage_enabled(
        tenant_id=t, document_id=d, passage_id=p.second.id, enabled=False
    )
    edited = await store.replace_passage(
        tenant_id=t,
        document_id=d,
        passage_id=p.first.id,
        expected_revision=1,
        content="苹果 适合\n\n老人 小孩",
        heading_path=("条款",),
        children=_kids("苹果 适合", "老人 小孩"),
        edited_by="user-a",
    )
    added = await store.add_passage(
        tenant_id=t,
        document_id=d,
        content="葡萄",
        heading_path=(),
        children=_kids("葡萄", heading=()),
        edited_by="user-b",
    )
    assert edited is not None and added is not None

    updated = await store.update_chunk_embeddings(
        tenant_id=t,
        document_id=d,
        vectors=[ChunkVector(c.id, c.content_hash, _vec(0.0, 1.0)) for c in snapshot],
        embedding_model="new/model",
        generation=document.ingest_generation,
    )

    # The lone chunk and the second passage's two chunks were untouched.
    assert updated == 3
    listed = await _listed(store, p.doc)
    assert [x.id for x in listed] == [p.first.id, p.lone.id, p.second.id, added.id]
    first, lone, second, mine = listed
    assert first.row.content == "苹果 适合\n\n老人 小孩" and first.row.revision == 2
    assert [c.content for c in first.children] == ["苹果 适合", "老人 小孩"]
    assert second.row.enabled is False
    assert [(c.enabled, c.disabled_reason) for c in second.children] == [(False, "manual")] * 2
    models = {c.id: c.embedding_model for x in listed for c in x.children}
    assert {models[lone.id], *(models[c.id] for c in second.children)} == {"new/model"}
    assert {models[c.id] for c in (*first.children, *mine.children)} == {_MODEL}
    assert await _vector_ids(store, p.doc) == {lone.id} | {
        c.id for c in (*first.children, *mine.children)
    }
    assert await store.list_unapplied_revisions(tenant_id=t, document_id=d) == []


async def test_update_chunk_embeddings_is_fenced(store: KnowledgeStore) -> None:
    p = await _passages(store)
    t, d = p.doc.tenant, p.doc.id
    document = await store.get_document(tenant_id=t, document_id=d)
    assert document is not None
    vectors = [ChunkVector(p.lone.id, p.lone.content_hash, _vec(0.0, 1.0))]
    stale = await store.update_chunk_embeddings(
        tenant_id=t,
        document_id=d,
        vectors=vectors,
        embedding_model="new/model",
        generation=document.ingest_generation + 1,
    )
    assert stale is None
    await store.upsert_document(tenant_id=t, kb_id=p.doc.kb_id, filename="a.md", content=b"v2")
    busy = await store.update_chunk_embeddings(
        tenant_id=t,
        document_id=d,
        vectors=vectors,
        embedding_model="new/model",
        generation=document.ingest_generation + 1,
    )
    assert busy is None  # re-uploaded: no longer ready
    (lone,) = await store.get_chunks(tenant_id=t, chunk_ids=[p.lone.id])
    assert lone.embedding_model == _MODEL


# -- histories a re-ingest leaves behind ---------------------------------------------


async def test_a_reingest_marks_the_histories_it_leaves_behind(store: KnowledgeStore) -> None:
    p = await _passages(store)
    t, d = p.doc.tenant, p.doc.id
    for passage in (p.first, p.lone):
        await store.replace_passage(
            tenant_id=t,
            document_id=d,
            passage_id=passage.id,
            expected_revision=1,
            content="改",
            heading_path=(),
            children=_kids("改", heading=()),
            edited_by="user-a",
        )
    histories = await store.list_passage_revisions(tenant_id=t, document_id=d, passage_id=p.lone.id)
    assert {r.orphaned_at for r in histories} == {None}
    # Chunks written without the first passage (its edit was not put back).
    kept = await store.get_passage(tenant_id=t, document_id=d, passage_id=p.lone.id)
    assert kept is not None
    await _write(store, p.doc, [kept.row.model_copy(update={"embedding": _vec(1.0)})])

    gone = await store.list_passage_revisions(tenant_id=t, document_id=d, passage_id=p.first.id)
    assert len(gone) == 2 and all(r.orphaned_at is not None for r in gone)
    alive = await store.list_passage_revisions(tenant_id=t, document_id=d, passage_id=p.lone.id)
    assert {r.orphaned_at for r in alive} == {None}
    (unapplied,) = await store.list_unapplied_revisions(tenant_id=t, document_id=d)
    assert unapplied.passage_id == p.first.id and unapplied.orphaned_at is not None

    discarded = await store.discard_unapplied_revisions(tenant_id=t, document_id=d)
    assert discarded == 2
    assert await store.list_unapplied_revisions(tenant_id=t, document_id=d) == []
    assert (
        await store.list_passage_revisions(tenant_id=t, document_id=d, passage_id=p.first.id) == []
    )
    assert (
        len(await store.list_passage_revisions(tenant_id=t, document_id=d, passage_id=p.lone.id))
        == 2
    )
    assert await store.discard_unapplied_revisions(tenant_id=uuid4(), document_id=d) == 0


# -- finding a passage again ----------------------------------------------------------


async def test_manual_passages_know_which_copy_they_are(store: KnowledgeStore) -> None:
    """Identical passages (a template repeated under several headings, or the
    same heading twice): each manual passage says how many passages of the
    document before it share its machine text and heading — counted over all
    passages, manual or not."""
    doc = await _doc(store)
    rows = [
        doc.child(0, "饮食 睡眠").model_copy(update={"heading_path": ("第1周",)}),
        doc.child(1, "饮食 睡眠").model_copy(update={"heading_path": ("第2周",)}),
        doc.child(2, "饮食 睡眠").model_copy(update={"heading_path": ("附录",)}),
        doc.child(3, "饮食 睡眠").model_copy(update={"heading_path": ("附录",)}),
        doc.child(4, "只此一段").model_copy(update={"heading_path": ("附录",)}),
    ]
    await _write(store, doc, rows)
    await _ready(store, doc, chunks=5)
    scope: dict[str, Any] = {"tenant_id": doc.tenant, "document_id": doc.id}
    for off in (rows[1], rows[3], rows[4]):
        await store.set_passage_enabled(**scope, passage_id=off.id, enabled=False)
    await store.replace_passage(
        **scope,
        passage_id=rows[2].id,
        expected_revision=1,
        content="附录改",
        heading_path=("附录",),
        children=_kids("附录改", heading=("附录",)),
        edited_by="user-a",
    )
    manual = await store.list_manual_passages(**scope)
    assert [
        (m.row.id, m.anchor, m.anchor_heading, m.occurrence, m.unique_text) for m in manual
    ] == [
        (rows[1].id, "饮食 睡眠", ("第2周",), 0, False),
        (rows[2].id, "饮食 睡眠", ("附录",), 0, False),
        (rows[3].id, "饮食 睡眠", ("附录",), 1, False),
        (rows[4].id, "只此一段", ("附录",), 0, True),
    ]


async def test_manual_passages_know_the_heading_a_person_last_wrote(
    store: KnowledgeStore,
) -> None:
    """``user_heading``: the breadcrumb of the passage's latest revision by a
    person — not its row's, which a re-ingest moves when the file renames
    the section. ``None`` for a passage nobody wrote (switched off only)."""
    p = await _passages(store)
    scope: dict[str, Any] = {"tenant_id": p.doc.tenant, "document_id": p.doc.id}
    for revision, heading in ((1, ("改名",)), (2, ("条款",))):
        await store.replace_passage(
            **scope,
            passage_id=p.first.id,
            expected_revision=revision,
            content="改",
            heading_path=heading,
            children=_kids("改", "再改", heading=heading),
            edited_by="user-a",
        )
    await store.set_passage_enabled(**scope, passage_id=p.lone.id, enabled=False)
    manual = {m.row.id: m for m in await store.list_manual_passages(**scope)}
    assert manual[p.first.id].user_heading == ("条款",)
    assert manual[p.first.id].anchor_heading == ()
    assert manual[p.lone.id].user_heading is None


def _without_vectors(passage: Passage) -> list[KnowledgeChunk]:
    """A passage's rows as ``replace_chunks`` takes them (chunks need a vector)."""
    return [passage.row, *(c.model_copy(update={"embedding": _vec(1.0)}) for c in passage.children)]


async def test_switch_off_records_are_kept_apart_from_text_history(
    store: KnowledgeStore,
) -> None:
    """``replace_chunks(switch_offs=…)`` keeps a record of each switch-off a
    re-ingest could not keep on its own passage — the text it hides, apart
    from the passages' text histories (``list_switch_offs``). One that no
    passage holds (``placed`` false) is listed with the unapplied edits from
    then on (``orphaned_at``: since when, kept on later writes) and can be
    discarded; a placed one is not listed, and a write that does not name it
    leaves it alone."""
    p = await _passages(store)
    scope: dict[str, Any] = {"tenant_id": p.doc.tenant, "document_id": p.doc.id}
    await store.replace_passage(
        **scope,
        passage_id=p.first.id,
        expected_revision=1,
        content="改",
        heading_path=("条款",),
        children=_kids("改", "再改"),
        edited_by="user-a",
    )
    kept = await store.get_passage(**scope, passage_id=p.second.id)
    assert kept is not None
    chunks = _without_vectors(kept)
    placed = SwitchOffRecord(p.first.id, "苹果 适合\n\n老年 人群", (), placed=True, new=True)
    unplaced = SwitchOffRecord(p.lone.id, "樱桃 果酱", (), placed=False, new=True)

    assert await store.replace_chunks(**scope, chunks=chunks, switch_offs=[placed, unplaced])

    records = {r.passage_id: r for r in await store.list_switch_offs(**scope)}
    shapes = {k: (r.content, r.switch_off_lost, r.orphaned_at is None) for k, r in records.items()}
    assert shapes == {
        p.first.id: ("苹果 适合\n\n老年 人群", True, True),
        p.lone.id: ("樱桃 果酱", True, False),
    }
    unapplied = await store.list_unapplied_revisions(**scope)
    listed = sorted((r.passage_id == p.lone.id, r.revision, r.switch_off_lost) for r in unapplied)
    assert listed == [
        (False, 2, False),  # the first passage's edit, as before
        (True, 0, True),
    ]
    # Text histories never show a record.
    history = await store.list_passage_revisions(**scope, passage_id=p.first.id)
    assert [r.revision for r in history] == [1, 2]
    assert await store.list_passage_revisions(**scope, passage_id=p.lone.id) == []

    since = records[p.lone.id].orphaned_at
    earlier = [replace(record, new=False) for record in (placed, unplaced)]
    assert await store.replace_chunks(**scope, chunks=chunks, switch_offs=earlier)
    assert await store.replace_chunks(**scope, chunks=chunks)
    again = {r.passage_id: r for r in await store.list_switch_offs(**scope)}
    assert (again[p.lone.id].orphaned_at, again[p.first.id].orphaned_at) == (since, None)

    assert await store.discard_unapplied_revisions(**scope) == 3  # the edit's 2 + the record
    assert [r.passage_id for r in await store.list_switch_offs(**scope)] == [p.first.id]


async def test_a_record_discarded_during_an_ingest_stays_discarded(
    store: KnowledgeStore,
) -> None:
    """An ingest writes back the switch-off records it read at its start. One
    discarded meanwhile (or deleted by the retention job) does not come back:
    earlier records are only updated where they still exist; only the ones
    this ingest made (``new``) are inserted."""
    p = await _passages(store)
    scope: dict[str, Any] = {"tenant_id": p.doc.tenant, "document_id": p.doc.id}
    kept = await store.get_passage(**scope, passage_id=p.second.id)
    assert kept is not None
    chunks = _without_vectors(kept)
    first = SwitchOffRecord(p.lone.id, "樱桃 果酱", (), placed=False, new=True)
    assert await store.replace_chunks(**scope, chunks=chunks, switch_offs=[first])

    (read,) = await store.list_switch_offs(**scope)  # an ingest reads it ...
    assert await store.discard_unapplied_revisions(**scope) == 1  # ... it is discarded ...
    made = SwitchOffRecord(p.first.id, "苹果 适合", (), placed=False, new=True)
    again = replace(first, new=False)  # ... and the ingest writes back what it read
    assert await store.replace_chunks(**scope, chunks=chunks, switch_offs=[again, made])

    assert read.passage_id == p.lone.id
    assert [r.passage_id for r in await store.list_switch_offs(**scope)] == [p.first.id]


async def test_reviewing_an_inherited_switch_off_settles_its_records(
    store: KnowledgeStore,
) -> None:
    """A person switching an inherited passage on (it may show) or off (now
    their own switch-off, ``manual``) settles it: its paragraphs leave the
    switch-off records that hid it, so no later re-ingest hides them again
    for those records; a record left with nothing is gone. An inherited
    passage nobody touched is not a manual passage — it is derived."""
    doc = await _doc(store)
    off = {"enabled": False, "disabled_reason": "inherited"}
    one = doc.parent(0, "苹果 适合\n\n老年 人群").model_copy(update=off)
    two = doc.parent(1, "香蕉 甜点\n\n面包 牛奶").model_copy(update=off)
    rows = [
        one,
        doc.child(0, "苹果 适合", parent=one).model_copy(update=off),
        doc.child(1, "老年 人群", parent=one).model_copy(update=off),
        two,
        doc.child(2, "香蕉 甜点", parent=two).model_copy(update=off),
        doc.child(3, "面包 牛奶", parent=two).model_copy(update=off),
    ]
    scope: dict[str, Any] = {"tenant_id": doc.tenant, "document_id": doc.id}
    wide, narrow = uuid4(), uuid4()
    assert await store.replace_chunks(
        **scope,
        chunks=rows,
        switch_offs=[
            SwitchOffRecord(wide, "苹果 适合\n\n老年 人群\n\n香蕉 甜点", (), placed=True, new=True),
            SwitchOffRecord(narrow, "老年 人群", (), placed=True, new=True),
        ],
    )
    await _ready(store, doc, chunks=4)
    assert await store.list_manual_passages(**scope) == []

    await store.set_passage_enabled(**scope, passage_id=one.id, enabled=True)
    assert [(r.passage_id, r.content) for r in await store.list_switch_offs(**scope)] == [
        (wide, "香蕉 甜点")
    ]
    confirmed = await store.set_passage_enabled(**scope, passage_id=two.id, enabled=False)
    assert confirmed is not None and confirmed.row.disabled_reason == "manual"
    assert await store.list_switch_offs(**scope) == []
    assert [m.row.id for m in await store.list_manual_passages(**scope)] == [two.id]


async def test_update_chunk_embeddings_fences_on_the_status_asked_for(
    store: KnowledgeStore,
) -> None:
    """``expect_status`` (default ``ready``): a document that serves its
    chunks in another status (a failed re-upload keeps the last version) is
    re-embedded when asked for that status, refused otherwise."""
    p = await _passages(store)
    t, d = p.doc.tenant, p.doc.id
    assert await store.set_document_status(tenant_id=t, document_id=d, status=DocumentStatus.FAILED)
    document = await store.get_document(tenant_id=t, document_id=d)
    assert document is not None and document.status is DocumentStatus.FAILED
    write: dict[str, Any] = {
        "tenant_id": t,
        "document_id": d,
        "vectors": [ChunkVector(p.lone.id, p.lone.content_hash, _vec(0.0, 1.0))],
        "embedding_model": "new/model",
        "generation": document.ingest_generation,
    }
    assert await store.update_chunk_embeddings(**write) is None
    assert await store.update_chunk_embeddings(**write, expect_status=DocumentStatus.FAILED) == 1
    (lone,) = await store.get_chunks(tenant_id=t, chunk_ids=[p.lone.id])
    assert lone.embedding_model == "new/model"


async def test_list_chunks_pages_by_position(store: KnowledgeStore) -> None:
    """``after``: the chunks ordered after a ``(chunk_index, chunk_type)`` —
    a page that does not shift when rows before it go (a re-index reads a
    document this way while passages are edited)."""
    p = await _passages(store)
    scope: dict[str, Any] = {"tenant_id": p.doc.tenant, "document_id": p.doc.id}
    first, total = await store.list_chunks(**scope, limit=2)
    assert ([c.chunk_index for c in first], total) == ([0, 1], 5)
    last = first[-1]
    rest, _ = await store.list_chunks(**scope, limit=10, after=(last.chunk_index, last.chunk_type))
    assert [c.chunk_index for c in rest] == [2, 3, 4]
    parents, _ = await store.list_chunks(
        **scope, limit=10, chunk_types=[ChunkType.PARENT], after=(0, ChunkType.PARENT)
    )
    assert [c.id for c in parents] == [p.second.id]
