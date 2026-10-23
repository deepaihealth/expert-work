"""``KnowledgeStore`` 数据模型 v2 契约(B-175 L2-PR3)—— **两个后端跑同一组断言**。

SQL 与内存两份 store 各写各的测试时,漏一边是静默的(仓库既有教训)。这里同一组用例
参数化跑两遍:内存档进单元 CI,SQL 档标 ``integration``,在容器里另建一个库(共享库
会被别的测试做 downgrade 往返)。

覆盖:上传时的文档事实(``doc_seq`` / 标题 / mime / 大小 / 来源 / 阶段)、``doc_seq``
重传保留与删除后不复用、阶段写回的围栏、READY / 失败 / 巡检 / 丢弃对阶段与错误码的
影响、分段新列往返、关键词索引含标题词、按内容指纹取旧向量。

本地跑 SQL 档前: export DOCKER_HOST=unix:///Users/mac/.docker/run/docker.sock
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import update
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
from expert_work.persistence.models import KnowledgeDocumentRow
from expert_work.protocol import (
    ChunkType,
    DocumentSourceType,
    DocumentStatus,
    IngestErrorCode,
    IngestStage,
    KnowledgeChunk,
)

ALEMBIC_INI = Path(__file__).resolve().parent.parent / "alembic.ini"
_DB = "knowledge_model_v2_contract"


def _vec(*head: float) -> tuple[float, ...]:
    return tuple(head) + (0.0,) * (EMBEDDING_DIM - len(head))


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


async def _base(store: KnowledgeStore, tenant: UUID, name: str = "kb") -> UUID:
    return (await store.create_base(tenant_id=tenant, name=name)).id


async def _upload(
    store: KnowledgeStore, tenant: UUID, kb_id: UUID, filename: str, content: bytes
) -> UUID:
    document = await store.upsert_document(
        tenant_id=tenant,
        kb_id=kb_id,
        filename=filename,
        content=content,
        content_sha256=hashlib.sha256(content).hexdigest(),
    )
    return document.id


async def _claim(store: KnowledgeStore, tenant: UUID, document_id: UUID) -> int:
    claim = await store.claim_document(
        tenant_id=tenant,
        document_id=document_id,
        now=datetime.now(UTC),
        lease_seconds=600,
        max_attempts=5,
    )
    assert claim is not None
    return claim.generation


async def _doc(store: KnowledgeStore, tenant: UUID, document_id: UUID):  # type: ignore[no-untyped-def]
    document = await store.get_document(tenant_id=tenant, document_id=document_id)
    assert document is not None
    return document


def _chunk(
    tenant: UUID,
    kb_id: UUID,
    document_id: UUID,
    index: int,
    content: str,
    heading_path: tuple[str, ...] = (),
    embedding: tuple[float, ...] | None = None,
    embedding_model: str | None = "qwen/text-embedding-v4",
) -> KnowledgeChunk:
    draft = KnowledgeChunk(
        id=uuid4(),
        tenant_id=tenant,
        kb_id=kb_id,
        document_id=document_id,
        chunk_index=index,
        content=content,
        embedding=embedding or _vec(1.0),
        heading_path=heading_path,
        embedding_model=embedding_model,
    )
    return draft.model_copy(
        update={"content_hash": hashlib.sha256(draft.retrieval_text.encode()).hexdigest()}
    )


# -- documents ---------------------------------------------------------------


async def test_upload_records_file_facts(store: KnowledgeStore) -> None:
    tenant = uuid4()
    kb_id = await _base(store, tenant)
    document_id = await _upload(store, tenant, kb_id, "季度报告.v2.pdf", b"%PDF-1.7 body")
    document = await _doc(store, tenant, document_id)
    assert document.doc_seq == 1
    assert document.title == "季度报告.v2"
    assert document.mime == "application/pdf"
    assert document.size_bytes == len(b"%PDF-1.7 body")
    assert document.source_type is DocumentSourceType.FILE
    assert document.stage is IngestStage.QUEUED
    assert document.error_code is None
    assert document.enabled is True
    assert document.metadata == {}


async def test_doc_seq_is_per_tenant_kept_on_reupload_never_reused(store: KnowledgeStore) -> None:
    alpha, beta = uuid4(), uuid4()
    kb_alpha, kb_beta = await _base(store, alpha), await _base(store, beta)
    first = await _upload(store, alpha, kb_alpha, "a.md", b"a")
    second = await _upload(store, alpha, kb_alpha, "b.md", b"b")
    other = await _upload(store, beta, kb_beta, "c.md", b"c")
    assert [(await _doc(store, alpha, d)).doc_seq for d in (first, second)] == [1, 2]
    assert (await _doc(store, beta, other)).doc_seq == 1

    again = await _upload(store, alpha, kb_alpha, "a.md", b"a, edited")
    assert again == first
    assert (await _doc(store, alpha, first)).doc_seq == 1

    assert await store.delete_document(tenant_id=alpha, document_id=second)
    third = await _upload(store, alpha, kb_alpha, "d.md", b"d")
    assert (await _doc(store, alpha, third)).doc_seq == 3


async def test_stage_writes_are_fenced(store: KnowledgeStore) -> None:
    tenant = uuid4()
    kb_id = await _base(store, tenant)
    document_id = await _upload(store, tenant, kb_id, "a.md", b"a")
    generation = await _claim(store, tenant, document_id)

    assert await store.set_document_stage(
        tenant_id=tenant, document_id=document_id, stage=IngestStage.PARSING, generation=generation
    )
    assert (await _doc(store, tenant, document_id)).stage is IngestStage.PARSING
    # A stale worker (older generation) is refused and changes nothing.
    assert not await store.set_document_stage(
        tenant_id=tenant,
        document_id=document_id,
        stage=IngestStage.EMBEDDING,
        generation=generation - 1,
    )
    assert (await _doc(store, tenant, document_id)).stage is IngestStage.PARSING
    # A re-upload resets the stage to queued.
    await _upload(store, tenant, kb_id, "a.md", b"a, new")
    assert (await _doc(store, tenant, document_id)).stage is IngestStage.QUEUED


async def test_ready_sets_stage_and_promotes_pending_size(store: KnowledgeStore) -> None:
    tenant = uuid4()
    kb_id = await _base(store, tenant)
    document_id = await _upload(store, tenant, kb_id, "a.md", b"v1")
    generation = await _claim(store, tenant, document_id)
    await store.replace_chunks(
        tenant_id=tenant,
        document_id=document_id,
        chunks=[_chunk(tenant, kb_id, document_id, 0, "v1")],
        generation=generation,
    )
    assert await store.set_document_status(
        tenant_id=tenant,
        document_id=document_id,
        status=DocumentStatus.READY,
        chunk_count=1,
        generation=generation,
    )
    ready = await _doc(store, tenant, document_id)
    assert ready.stage is IngestStage.READY
    assert ready.size_bytes == 2

    # v2 waits in the pending slot: size still describes the serving v1 …
    await _upload(store, tenant, kb_id, "a.md", b"version two")
    assert (await _doc(store, tenant, document_id)).size_bytes == 2
    generation = await _claim(store, tenant, document_id)
    await store.replace_chunks(
        tenant_id=tenant,
        document_id=document_id,
        chunks=[_chunk(tenant, kb_id, document_id, 0, "v2")],
        generation=generation,
    )
    await store.set_document_status(
        tenant_id=tenant,
        document_id=document_id,
        status=DocumentStatus.READY,
        chunk_count=1,
        generation=generation,
    )
    # … until it is promoted.
    assert (await _doc(store, tenant, document_id)).size_bytes == len(b"version two")


async def test_failure_codes(store: KnowledgeStore) -> None:
    tenant = uuid4()
    kb_id = await _base(store, tenant)
    broken = await _upload(store, tenant, kb_id, "a.md", b"a")
    generation = await _claim(store, tenant, broken)
    assert await store.mark_document_failed_terminal(
        tenant_id=tenant,
        document_id=broken,
        error="could not open",
        error_code=IngestErrorCode.INVALID_FILE,
        generation=generation,
    )
    failed = await _doc(store, tenant, broken)
    assert failed.status is DocumentStatus.FAILED
    assert failed.error_code is IngestErrorCode.INVALID_FILE

    exhausted = await _upload(store, tenant, kb_id, "b.md", b"b")
    past = datetime.now(UTC) - timedelta(seconds=10)
    for attempt in range(3):
        # Each claim lands after the previous zero-length lease expired.
        assert await store.claim_document(
            tenant_id=tenant,
            document_id=exhausted,
            now=past + timedelta(seconds=attempt),
            lease_seconds=0,
            max_attempts=3,
        )
    assert await store.sweep_terminal(now=datetime.now(UTC), max_attempts=3) == 1
    swept = await _doc(store, tenant, exhausted)
    assert swept.error_code is IngestErrorCode.ATTEMPTS_EXHAUSTED

    # A re-upload clears the old failure code.
    await _upload(store, tenant, kb_id, "a.md", b"a, fixed")
    assert (await _doc(store, tenant, broken)).error_code is None


async def test_every_failure_code_reads_back(store: KnowledgeStore) -> None:
    # A code the store did not know would read back as ``internal_error``
    # (e.g. ``text_too_large``, the parsed-size cap's refusal).
    tenant = uuid4()
    kb_id = await _base(store, tenant)
    for code in IngestErrorCode:
        document_id = await _upload(store, tenant, kb_id, f"{code.value}.md", b"x")
        generation = await _claim(store, tenant, document_id)
        assert await store.mark_document_failed_terminal(
            tenant_id=tenant,
            document_id=document_id,
            error=code.value,
            error_code=code,
            generation=generation,
        )
        assert (await _doc(store, tenant, document_id)).error_code is code


async def test_discard_restores_ready_stage(store: KnowledgeStore) -> None:
    tenant = uuid4()
    kb_id = await _base(store, tenant)
    document_id = await _upload(store, tenant, kb_id, "a.md", b"v1")
    generation = await _claim(store, tenant, document_id)
    await store.replace_chunks(
        tenant_id=tenant,
        document_id=document_id,
        chunks=[_chunk(tenant, kb_id, document_id, 0, "v1")],
        generation=generation,
    )
    await store.set_document_status(
        tenant_id=tenant,
        document_id=document_id,
        status=DocumentStatus.READY,
        chunk_count=1,
        generation=generation,
    )
    await _upload(store, tenant, kb_id, "a.md", b"broken v2")
    generation = await _claim(store, tenant, document_id)
    await store.set_document_stage(
        tenant_id=tenant, document_id=document_id, stage=IngestStage.PARSING, generation=generation
    )
    await store.mark_document_failed_terminal(
        tenant_id=tenant,
        document_id=document_id,
        error="parse failed",
        error_code=IngestErrorCode.PARSE_FAILED,
        generation=generation,
    )
    restored = await store.discard_pending(tenant_id=tenant, document_id=document_id)
    assert restored is not None
    assert restored.status is DocumentStatus.READY
    assert restored.stage is IngestStage.READY
    assert restored.error_code is None


# -- chunks ------------------------------------------------------------------


async def test_chunk_columns_round_trip(store: KnowledgeStore) -> None:
    tenant = uuid4()
    kb_id = await _base(store, tenant)
    document_id = await _upload(store, tenant, kb_id, "a.md", b"a")
    written = _chunk(tenant, kb_id, document_id, 0, "免赔额是 500 元。", ("健康保险", "免赔额"))
    assert await store.replace_chunks(tenant_id=tenant, document_id=document_id, chunks=[written])
    page, total = await store.list_chunks(tenant_id=tenant, document_id=document_id)
    assert total == 1
    (read,) = page
    assert read.content == "免赔额是 500 元。"
    assert read.heading_path == ("健康保险", "免赔额")
    assert read.content_hash == written.content_hash
    assert read.chunk_type is ChunkType.TEXT
    assert (read.enabled, read.edited, read.revision) == (True, False, 1)
    assert read.retrieval_text.startswith("[Section: 健康保险 > 免赔额]")


async def test_keyword_index_includes_heading_words(store: KnowledgeStore) -> None:
    """The body is stored clean, but the keyword side still matches words
    that only appear in the section breadcrumb."""
    tenant = uuid4()
    kb_id = await _base(store, tenant)
    document_id = await _upload(store, tenant, kb_id, "a.md", b"a")
    await store.replace_chunks(
        tenant_id=tenant,
        document_id=document_id,
        chunks=[_chunk(tenant, kb_id, document_id, 0, "金额是 500 元。", ("免赔额",))],
    )
    hits = await store.keyword_search_scored(tenant_id=tenant, kb_ids=[kb_id], query="免赔额")
    assert [hit.chunk.content for hit in hits] == ["金额是 500 元。"]


async def test_chunk_embeddings_by_hash(store: KnowledgeStore) -> None:
    tenant = uuid4()
    kb_id = await _base(store, tenant)
    mine = await _upload(store, tenant, kb_id, "a.md", b"a")
    other = await _upload(store, tenant, kb_id, "b.md", b"b")
    kept = _chunk(tenant, kb_id, mine, 0, "kept", embedding=_vec(0.5, 0.5))
    twin = _chunk(tenant, kb_id, other, 0, "kept", embedding=_vec(0.9))
    assert kept.content_hash == twin.content_hash
    await store.replace_chunks(tenant_id=tenant, document_id=mine, chunks=[kept])
    await store.replace_chunks(tenant_id=tenant, document_id=other, chunks=[twin])

    found = await store.chunk_embeddings_by_hash(
        tenant_id=tenant,
        document_id=mine,
        content_hashes=[kept.content_hash or "", "absent"],
        embedding_model="qwen/text-embedding-v4",
    )
    # Only this document's vectors — never another document's twin.
    assert set(found) == {kept.content_hash}
    assert found[kept.content_hash or ""][:2] == pytest.approx((0.5, 0.5))
    # A vector another model made is never handed out.
    assert (
        await store.chunk_embeddings_by_hash(
            tenant_id=tenant,
            document_id=mine,
            content_hashes=[kept.content_hash or ""],
            embedding_model="openai/text-embedding-3-large",
        )
        == {}
    )


async def test_chunk_embeddings_by_hash_handles_more_hashes_than_bind_params(
    store: KnowledgeStore,
) -> None:
    """asyncpg caps a statement at 32767 bind parameters; a big document can
    have more chunks than that, so the lookup must not bind one per hash."""
    tenant = uuid4()
    kb_id = await _base(store, tenant)
    document_id = await _upload(store, tenant, kb_id, "big.md", b"big")
    kept = _chunk(tenant, kb_id, document_id, 0, "kept")
    await store.replace_chunks(tenant_id=tenant, document_id=document_id, chunks=[kept])
    hashes = [f"{i:064x}" for i in range(40_000)] + [kept.content_hash or ""]
    found = await store.chunk_embeddings_by_hash(
        tenant_id=tenant,
        document_id=document_id,
        content_hashes=hashes,
        embedding_model="qwen/text-embedding-v4",
    )
    assert set(found) == {kept.content_hash}


async def test_duplicate_chunk_index_is_rejected(store: KnowledgeStore) -> None:
    tenant = uuid4()
    kb_id = await _base(store, tenant)
    document_id = await _upload(store, tenant, kb_id, "a.md", b"a")
    with pytest.raises((ValueError, IntegrityError)):
        await store.replace_chunks(
            tenant_id=tenant,
            document_id=document_id,
            chunks=[
                _chunk(tenant, kb_id, document_id, 0, "one"),
                _chunk(tenant, kb_id, document_id, 0, "two"),
            ],
        )


async def test_reset_numbers_a_document_from_before_doc_seq(store: KnowledgeStore) -> None:
    """A row the previous image inserted during the rollout has no doc_seq;
    its next re-upload gives it the tenant's next number."""
    tenant = uuid4()
    kb_id = await _base(store, tenant)
    first = await _upload(store, tenant, kb_id, "a.md", b"a")
    legacy = await _upload(store, tenant, kb_id, "old.md", b"old")
    await _clear_doc_seq(store, tenant, legacy)
    assert (await _doc(store, tenant, legacy)).doc_seq is None

    await _upload(store, tenant, kb_id, "old.md", b"old, re-uploaded")
    assert (await _doc(store, tenant, legacy)).doc_seq == 3
    assert (await _doc(store, tenant, first)).doc_seq == 1


async def _clear_doc_seq(store: KnowledgeStore, tenant: UUID, document_id: UUID) -> None:
    if isinstance(store, InMemoryKnowledgeStore):
        document = await _doc(store, tenant, document_id)
        store._documents[store._documents.index(document)] = document.model_copy(
            update={"doc_seq": None}
        )
        return
    assert isinstance(store, SqlKnowledgeStore)
    async with store._sf() as session:
        await session.execute(
            update(KnowledgeDocumentRow)
            .where(KnowledgeDocumentRow.id == document_id)
            .values(doc_seq=None)
        )
        await session.commit()


@pytest.mark.integration
async def test_concurrent_uploads_never_share_a_doc_seq(
    postgres_container: PostgresContainer,
) -> None:
    admin = str(postgres_container.get_connection_url()).replace("+psycopg2", "")
    db = f"{_DB}_seq"
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{db}" WITH (FORCE)')
        conn.execute(f'CREATE DATABASE "{db}"')
    base, _, _ = admin.rpartition("/")
    dsn = f"{base}/{db}"
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", dsn.replace("postgresql://", "postgresql+psycopg://", 1))
    command.upgrade(cfg, "head")
    engine = create_async_engine_from_config(
        DatabaseConfig(dsn=dsn.replace("postgresql://", "postgresql+asyncpg://", 1))
    )
    try:
        sql = SqlKnowledgeStore(create_async_session_factory(engine))
        tenant = uuid4()
        kb_id = await _base(sql, tenant)
        ids = await asyncio.gather(
            *(_upload(sql, tenant, kb_id, f"{i}.md", f"{i}".encode()) for i in range(10))
        )
        seqs = sorted([(await _doc(sql, tenant, d)).doc_seq for d in ids])
        assert seqs == list(range(1, 11))
    finally:
        await engine.dispose()
        with psycopg.connect(admin, autocommit=True) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{db}" WITH (FORCE)')
