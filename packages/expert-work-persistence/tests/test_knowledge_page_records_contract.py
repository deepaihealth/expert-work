"""B-209 —— 逐页记录 + 识别缓存 + 平台「文档识别」配置:**两个后端跑同一组断言**。

同 ``test_knowledge_store_model_v2_contract``:内存档进单元 CI,SQL 档标 ``integration``,
在容器里另建一个库。

本地跑 SQL 档前: export DOCKER_HOST=unix:///Users/mac/.docker/run/docker.sock
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from testcontainers.postgres import PostgresContainer

from expert_work.persistence import (
    DatabaseConfig,
    InMemoryKnowledgeStore,
    SqlKnowledgeStore,
    create_async_engine_from_config,
    create_async_session_factory,
)
from expert_work.persistence.knowledge.base import KnowledgeStore, ParseCacheEntry
from expert_work.persistence.platform_recognition_config import (
    InMemoryPlatformRecognitionConfigStore,
    PlatformRecognitionConfigRecord,
    PlatformRecognitionConfigStore,
    SqlPlatformRecognitionConfigStore,
)
from expert_work.protocol import (
    DocumentPage,
    PageDecision,
    PageReason,
    PageStats,
    PageStatus,
)

ALEMBIC_INI = Path(__file__).resolve().parent.parent / "alembic.ini"
_DB = "knowledge_page_records_contract"


async def _session_factory(
    request: pytest.FixtureRequest,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
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
        yield create_async_session_factory(engine)
    finally:
        await engine.dispose()
        with psycopg.connect(admin, autocommit=True) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{_DB}" WITH (FORCE)')


@pytest.fixture(params=["memory", pytest.param("sql", marks=pytest.mark.integration)])
async def store(request: pytest.FixtureRequest) -> AsyncIterator[KnowledgeStore]:
    if request.param == "memory":
        yield InMemoryKnowledgeStore()
        return
    async for factory in _session_factory(request):
        yield SqlKnowledgeStore(factory)


@pytest.fixture(params=["memory", pytest.param("sql", marks=pytest.mark.integration)])
async def config_store(
    request: pytest.FixtureRequest,
) -> AsyncIterator[PlatformRecognitionConfigStore]:
    if request.param == "memory":
        yield InMemoryPlatformRecognitionConfigStore()
        return
    async for factory in _session_factory(request):
        yield SqlPlatformRecognitionConfigStore(factory)


_PAGES = (
    DocumentPage(page=1, decision=PageDecision.TEXT, chars=812),
    DocumentPage(
        page=2,
        decision=PageDecision.OCR,
        reason=PageReason.FULL_PAGE_IMAGE,
        status=PageStatus.RECOGNIZED,
        provider="glm",
        model="glm-ocr",
        chars=640,
        images=1,
    ),
    DocumentPage(
        page=3,
        decision=PageDecision.OCR,
        reason=PageReason.SPARSE_TEXT,
        status=PageStatus.FAILED,
        images=1,
        error="primary: empty output; fallback: repeated output",
    ),
    DocumentPage(page=4, decision=PageDecision.BLANK),
)


async def _document(store: KnowledgeStore, tenant: UUID) -> tuple[UUID, int]:
    base = await store.create_base(tenant_id=tenant, name="kb")
    document = await store.upsert_document(
        tenant_id=tenant, kb_id=base.id, filename="mixed.pdf", content=b"%PDF"
    )
    claim = await store.claim_document(
        tenant_id=tenant,
        document_id=document.id,
        now=datetime.now(UTC),
        lease_seconds=60,
        max_attempts=5,
    )
    assert claim is not None
    return document.id, claim.generation


async def test_page_records_round_trip_with_their_counts(store: KnowledgeStore) -> None:
    tenant = uuid4()
    document_id, generation = await _document(store, tenant)
    assert await store.set_document_pages(
        tenant_id=tenant, document_id=document_id, pages=_PAGES, generation=generation
    )
    assert await store.list_document_pages(tenant_id=tenant, document_id=document_id) == list(
        _PAGES
    )
    document = await store.get_document(tenant_id=tenant, document_id=document_id)
    assert document is not None
    assert document.page_count == 4
    assert document.page_stats == PageStats(pages_total=4, pages_ocr=1, pages_failed=1)
    listed = await store.list_documents(tenant_id=tenant, kb_id=document.kb_id)
    assert listed[0].page_stats == document.page_stats


async def test_page_records_write_is_fenced(store: KnowledgeStore) -> None:
    tenant = uuid4()
    document_id, generation = await _document(store, tenant)
    assert not await store.set_document_pages(
        tenant_id=tenant, document_id=document_id, pages=_PAGES, generation=generation + 1
    )
    assert await store.list_document_pages(tenant_id=tenant, document_id=document_id) == []
    document = await store.get_document(tenant_id=tenant, document_id=document_id)
    assert document is not None and document.page_stats is None


async def test_page_records_of_a_missing_or_foreign_document(store: KnowledgeStore) -> None:
    tenant = uuid4()
    document_id, generation = await _document(store, tenant)
    await store.set_document_pages(
        tenant_id=tenant, document_id=document_id, pages=_PAGES, generation=generation
    )
    assert await store.list_document_pages(tenant_id=uuid4(), document_id=document_id) is None
    assert await store.list_document_pages(tenant_id=tenant, document_id=uuid4()) is None


_RAW = {"raw": "第一页", "finish_reason": None, "layout_chars": 3}


async def _cache(store: KnowledgeStore, tenant: UUID, document: UUID, key: str = "k1") -> None:
    await store.put_parse_cache(
        tenant_id=tenant,
        document_id=document,
        kind="page_ocr",
        key=key,
        model="glm/glm-ocr",
        result=_RAW,
    )


async def _cached(store: KnowledgeStore, tenant: UUID, document: UUID) -> dict[str, object]:
    return dict(
        await store.get_parse_cache(
            tenant_id=tenant, document_id=document, kind="page_ocr", keys=["k1", "k2"]
        )
    )


async def test_parse_cache_is_per_document_and_first_reading_stands(store: KnowledgeStore) -> None:
    tenant, other = uuid4(), uuid4()
    document_id, _ = await _document(store, tenant)
    assert await _cached(store, tenant, document_id) == {}
    await _cache(store, tenant, document_id)
    await store.put_parse_cache(
        tenant_id=tenant,
        document_id=document_id,
        kind="page_ocr",
        key="k1",
        model="glm/glm-ocr",
        result={"raw": "另一份"},
    )
    assert await _cached(store, tenant, document_id) == {
        "k1": ParseCacheEntry(model="glm/glm-ocr", result=_RAW)
    }
    # Another tenant / another document holding the same page learn nothing.
    assert await _cached(store, other, document_id) == {}
    second = await store.upsert_document(
        tenant_id=tenant,
        kb_id=(await store.get_base(tenant_id=tenant, name="kb")).id,  # type: ignore[union-attr]
        filename="copy.pdf",
        content=b"%PDF",
    )
    assert await _cached(store, tenant, second.id) == {}
    # Kinds do not mix.
    assert (
        await store.get_parse_cache(
            tenant_id=tenant, document_id=document_id, kind="figure_desc", keys=["k1"]
        )
        == {}
    )


async def test_deleting_a_document_deletes_its_recognized_text(store: KnowledgeStore) -> None:
    tenant = uuid4()
    document_id, _ = await _document(store, tenant)
    await _cache(store, tenant, document_id)
    assert await store.delete_document(tenant_id=tenant, document_id=document_id)
    assert await _cached(store, tenant, document_id) == {}


async def test_deleting_a_base_deletes_its_documents_recognized_text(store: KnowledgeStore) -> None:
    tenant = uuid4()
    document_id, _ = await _document(store, tenant)
    await _cache(store, tenant, document_id)
    base = await store.get_base(tenant_id=tenant, name="kb")
    assert base is not None
    assert await store.delete_base(tenant_id=tenant, kb_id=base.id)
    assert await _cached(store, tenant, document_id) == {}


async def test_a_reading_for_a_deleted_document_is_refused(store: KnowledgeStore) -> None:
    # An in-flight recognition finishing after the delete must not leave text behind.
    tenant = uuid4()
    document_id, _ = await _document(store, tenant)
    await store.delete_document(tenant_id=tenant, document_id=document_id)
    with pytest.raises(Exception):  # noqa: B017 — KeyError in memory, IntegrityError in SQL
        await _cache(store, tenant, document_id)
    assert await _cached(store, tenant, document_id) == {}


async def test_recognition_config_round_trip(config_store: PlatformRecognitionConfigStore) -> None:
    assert await config_store.get() is None
    first = PlatformRecognitionConfigRecord(
        ocr_provider="glm",
        ocr_model="glm-ocr",
        fallback_provider="qwen",
        fallback_model="qwen3.7-flash",
        figure_provider="qwen",
        figure_model="qwen3.8-flash",
        max_pages=1000,
        updated_by="admin",
    )
    await config_store.put(first)
    assert await config_store.get() == first
    cleared = PlatformRecognitionConfigRecord(
        ocr_provider=None,
        ocr_model=None,
        fallback_provider=None,
        fallback_model=None,
        figure_provider=None,
        figure_model=None,
        max_pages=50,
        updated_by="admin2",
    )
    await config_store.put(cleared)
    assert await config_store.get() == cleared
