"""B-209 PR-2 —— 文档内图片的计数与像素哈希:**两个后端跑同一组断言**。

同 ``test_knowledge_page_records_contract``:内存档进单元 CI,SQL 档标 ``integration``,
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
from expert_work.persistence.knowledge.base import UNSET, KnowledgeStore
from expert_work.protocol import FigureStats

ALEMBIC_INI = Path(__file__).resolve().parent.parent / "alembic.ini"
_DB = "knowledge_figures_contract"


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


_STATS = FigureStats(extracted=9, filtered=4, capped=1, unsupported=1, described=2, failed=1)


async def _document(store: KnowledgeStore, tenant: UUID, kb: UUID, name: str) -> UUID:
    document = await store.upsert_document(
        tenant_id=tenant, kb_id=kb, filename=name, content=name.encode()
    )
    return document.id


async def test_figure_stats_are_recorded_on_the_document(store: KnowledgeStore) -> None:
    tenant = uuid4()
    base = await store.create_base(tenant_id=tenant, name="kb")
    doc = await _document(store, tenant, base.id, "a.docx")
    assert await store.set_document_figures(
        tenant_id=tenant, document_id=doc, stats=_STATS, pixel_hashes=["h1", "h2"]
    )
    fetched = await store.get_document(tenant_id=tenant, document_id=doc)
    assert fetched is not None
    assert fetched.figure_stats == _STATS
    listed = await store.list_documents(tenant_id=tenant, kb_id=base.id)
    assert [d.figure_stats for d in listed] == [_STATS]


async def test_figure_stats_write_is_fenced_by_generation(store: KnowledgeStore) -> None:
    tenant = uuid4()
    base = await store.create_base(tenant_id=tenant, name="kb")
    doc = await _document(store, tenant, base.id, "a.docx")
    claim = await store.claim_document(
        tenant_id=tenant,
        document_id=doc,
        now=datetime.now(UTC),
        lease_seconds=300,
        max_attempts=5,
    )
    assert claim is not None
    stale = claim.generation - 1
    assert not await store.set_document_figures(
        tenant_id=tenant, document_id=doc, stats=_STATS, pixel_hashes=["h1"], generation=stale
    )
    fetched = await store.get_document(tenant_id=tenant, document_id=doc)
    assert fetched is not None and fetched.figure_stats is None
    assert await store.set_document_figures(
        tenant_id=tenant,
        document_id=doc,
        stats=_STATS,
        pixel_hashes=["h1"],
        generation=claim.generation,
    )


async def test_figure_documents_are_counted_per_tenant_excluding_the_document(
    store: KnowledgeStore,
) -> None:
    tenant, other_tenant = uuid4(), uuid4()
    base = await store.create_base(tenant_id=tenant, name="kb")
    other_base = await store.create_base(tenant_id=other_tenant, name="kb")
    docs = [await _document(store, tenant, base.id, f"{n}.pdf") for n in range(4)]
    foreign = await _document(store, other_tenant, other_base.id, "x.pdf")
    hashes = {docs[0]: ["logo", "chart"], docs[1]: ["logo"], docs[2]: ["logo", "photo"]}
    for doc, digests in hashes.items():
        await store.set_document_figures(
            tenant_id=tenant, document_id=doc, stats=_STATS, pixel_hashes=digests
        )
    await store.set_document_figures(
        tenant_id=other_tenant, document_id=foreign, stats=_STATS, pixel_hashes=["chart", "logo"]
    )
    counts = await store.count_figure_documents(
        tenant_id=tenant, pixel_hashes=["logo", "chart", "unseen"], exclude_document_id=docs[0]
    )
    # docs[0] itself and the other tenant's document are not counted.
    assert counts == {"logo": 2}
    counts = await store.count_figure_documents(
        tenant_id=tenant, pixel_hashes=["logo", "chart"], exclude_document_id=docs[3]
    )
    assert counts == {"logo": 3, "chart": 1}
    assert (
        await store.count_figure_documents(
            tenant_id=tenant, pixel_hashes=[], exclude_document_id=docs[3]
        )
        == {}
    )


async def test_a_deleted_document_no_longer_counts(store: KnowledgeStore) -> None:
    tenant = uuid4()
    base = await store.create_base(tenant_id=tenant, name="kb")
    first = await _document(store, tenant, base.id, "a.pdf")
    second = await _document(store, tenant, base.id, "b.pdf")
    await store.set_document_figures(
        tenant_id=tenant, document_id=first, stats=_STATS, pixel_hashes=["logo"]
    )
    assert await store.delete_document(tenant_id=tenant, document_id=first)
    assert (
        await store.count_figure_documents(
            tenant_id=tenant, pixel_hashes=["logo"], exclude_document_id=second
        )
        == {}
    )


async def test_rewriting_figures_replaces_the_hashes(store: KnowledgeStore) -> None:
    tenant = uuid4()
    base = await store.create_base(tenant_id=tenant, name="kb")
    first = await _document(store, tenant, base.id, "a.pdf")
    second = await _document(store, tenant, base.id, "b.pdf")
    await store.set_document_figures(
        tenant_id=tenant, document_id=first, stats=_STATS, pixel_hashes=["old"]
    )
    await store.set_document_figures(
        tenant_id=tenant, document_id=first, stats=_STATS, pixel_hashes=["new", "new"]
    )
    counts = await store.count_figure_documents(
        tenant_id=tenant, pixel_hashes=["old", "new"], exclude_document_id=second
    )
    assert counts == {"new": 1}


async def test_hashes_can_be_written_without_touching_the_stats(store: KnowledgeStore) -> None:
    # Review M5: hashes go in before the tenant-wide count; the counts after.
    tenant = uuid4()
    base = await store.create_base(tenant_id=tenant, name="kb")
    first = await _document(store, tenant, base.id, "a.pdf")
    second = await _document(store, tenant, base.id, "b.pdf")
    await store.set_document_figures(
        tenant_id=tenant, document_id=first, stats=_STATS, pixel_hashes=["old"]
    )
    await store.set_document_figures(
        tenant_id=tenant, document_id=first, stats=UNSET, pixel_hashes=["logo"]
    )
    fetched = await store.get_document(tenant_id=tenant, document_id=first)
    assert fetched is not None and fetched.figure_stats == _STATS
    assert await store.count_figure_documents(
        tenant_id=tenant, pixel_hashes=["logo", "old"], exclude_document_id=second
    ) == {"logo": 1}


async def test_stats_can_be_cleared(store: KnowledgeStore) -> None:
    # Review M3: a new version without pictures drops the old counts.
    tenant = uuid4()
    base = await store.create_base(tenant_id=tenant, name="kb")
    doc = await _document(store, tenant, base.id, "a.docx")
    await store.set_document_figures(
        tenant_id=tenant, document_id=doc, stats=_STATS, pixel_hashes=["h"]
    )
    await store.set_document_figures(tenant_id=tenant, document_id=doc, stats=None, pixel_hashes=[])
    fetched = await store.get_document(tenant_id=tenant, document_id=doc)
    assert fetched is not None and fetched.figure_stats is None


async def test_existing_document_ids_span_tenants(store: KnowledgeStore) -> None:
    # The reconciler's lookup (it runs with RLS bypassed).
    tenant, other = uuid4(), uuid4()
    base = await store.create_base(tenant_id=tenant, name="kb")
    other_base = await store.create_base(tenant_id=other, name="kb")
    mine = await _document(store, tenant, base.id, "a.pdf")
    theirs = await _document(store, other, other_base.id, "b.pdf")
    gone = await _document(store, tenant, base.id, "c.pdf")
    assert await store.delete_document(tenant_id=tenant, document_id=gone)
    found = await store.existing_document_ids(document_ids=[mine, theirs, gone, uuid4()])
    assert found == {mine, theirs}
    assert await store.existing_document_ids(document_ids=[]) == set()
