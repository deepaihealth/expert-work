"""B-267 step 2 —— the base-profile refresh bookkeeping, **both stores, one set
of assertions**: debounce, waiting for in-flight ingests, the lease, ``marks``
keeping a newer request, failure keeping the old profile, and the document
digest the profile is generated from.

SQL 档标 ``integration``,本地跑前: export DOCKER_HOST=unix:///Users/mac/.docker/run/docker.sock
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import psycopg
import pytest
from alembic import command
from alembic.config import Config
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
from expert_work.persistence.knowledge.profiles import (
    PROFILE_DEBOUNCE,
    PROFILE_LEASE,
    PROFILE_MAX_ATTEMPTS,
    PROFILE_MAX_WAIT,
    PROFILE_OPENING_CHARS,
    PROFILE_RETRY_AFTER,
)
from expert_work.protocol import DocumentStatus, KnowledgeChunk
from expert_work.protocol.knowledge_profile import KnowledgeProfile, ProbeCalibration

ALEMBIC_INI = Path(__file__).resolve().parent.parent / "alembic.ini"
_DB = "knowledge_profile_contract"
T0 = datetime(2026, 11, 4, 9, 0, tzinfo=UTC)


@pytest.fixture(params=["memory", pytest.param("sql", marks=pytest.mark.integration)])
async def store(request: pytest.FixtureRequest) -> AsyncIterator[KnowledgeStore]:
    if request.param == "memory":
        yield InMemoryKnowledgeStore()
        return
    async with _sql_store(request.getfixturevalue("postgres_container")) as sql_store:
        yield sql_store


@asynccontextmanager
async def _sql_store(container: PostgresContainer) -> AsyncIterator[SqlKnowledgeStore]:
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


async def _base(store: KnowledgeStore) -> tuple[UUID, UUID]:
    tenant = uuid4()
    return tenant, (await store.create_base(tenant_id=tenant, name="kb")).id


async def _ready_document(
    store: KnowledgeStore,
    tenant: UUID,
    kb_id: UUID,
    filename: str,
    chunks: list[tuple[tuple[str, ...], str]],
) -> UUID:
    document = await store.upsert_document(
        tenant_id=tenant, kb_id=kb_id, filename=filename, content=b"x"
    )
    rows = [
        KnowledgeChunk(
            id=uuid4(),
            tenant_id=tenant,
            kb_id=kb_id,
            document_id=document.id,
            chunk_index=index,
            content=text,
            heading_path=path,
            embedding=(1.0,) + (0.0,) * (EMBEDDING_DIM - 1),
        )
        for index, (path, text) in enumerate(chunks)
    ]
    await store.replace_chunks(tenant_id=tenant, document_id=document.id, chunks=rows)
    await store.set_document_status(
        tenant_id=tenant,
        document_id=document.id,
        status=DocumentStatus.READY,
        chunk_count=len(rows),
    )
    return document.id


async def _record(store: KnowledgeStore, tenant: UUID) -> object:
    return (await store.get_base(tenant_id=tenant, name="kb")).profile  # type: ignore[union-attr]


def _profile(summary: str) -> KnowledgeProfile:
    return KnowledgeProfile(
        summary=summary,
        topics=("a",),
        generated_at=T0,
        source_doc_count=1,
        model="p/m",
    )


_CALIBRATION = ProbeCalibration(
    embedding_model="p/e", vector_threshold=0.42, queries=16, computed_at=T0
)


async def test_a_mark_is_due_after_the_debounce(store: KnowledgeStore) -> None:
    tenant, kb_id = await _base(store)
    assert await store.mark_profile_refresh(tenant_id=tenant, kb_id=kb_id, now=T0)
    assert await store.claim_profile_refreshes(now=T0 + PROFILE_DEBOUNCE / 2, limit=5) == []
    [claim] = await store.claim_profile_refreshes(now=T0 + PROFILE_DEBOUNCE, limit=5)
    assert (claim.tenant_id, claim.kb_id, claim.marks, claim.attempts) == (tenant, kb_id, 1, 1)
    assert claim.calibrate_only is False


async def test_mark_of_an_unknown_base_is_false(store: KnowledgeStore) -> None:
    assert not await store.mark_profile_refresh(tenant_id=uuid4(), kb_id=uuid4(), now=T0)


async def test_a_later_change_pushes_the_refresh_back(store: KnowledgeStore) -> None:
    tenant, kb_id = await _base(store)
    await store.mark_profile_refresh(tenant_id=tenant, kb_id=kb_id, now=T0)
    later = T0 + PROFILE_DEBOUNCE / 2
    await store.mark_profile_refresh(tenant_id=tenant, kb_id=kb_id, now=later)
    assert await store.claim_profile_refreshes(now=T0 + PROFILE_DEBOUNCE, limit=5) == []
    [claim] = await store.claim_profile_refreshes(now=later + PROFILE_DEBOUNCE, limit=5)
    assert claim.marks == 2


async def test_an_ingesting_document_holds_the_refresh_up_to_the_max_wait(
    store: KnowledgeStore,
) -> None:
    tenant, kb_id = await _base(store)
    await store.upsert_document(tenant_id=tenant, kb_id=kb_id, filename="new.md", content=b"x")
    await store.mark_profile_refresh(tenant_id=tenant, kb_id=kb_id, now=T0)
    assert await store.claim_profile_refreshes(now=T0 + PROFILE_DEBOUNCE, limit=5) == []
    assert len(await store.claim_profile_refreshes(now=T0 + PROFILE_MAX_WAIT, limit=5)) == 1


async def test_a_claimed_refresh_is_leased(store: KnowledgeStore) -> None:
    tenant, kb_id = await _base(store)
    await store.mark_profile_refresh(tenant_id=tenant, kb_id=kb_id, now=T0)
    claimed_at = T0 + PROFILE_DEBOUNCE
    assert len(await store.claim_profile_refreshes(now=claimed_at, limit=5)) == 1
    # A change while it is held does not hand it to a second worker before the lease ends.
    await store.mark_profile_refresh(tenant_id=tenant, kb_id=kb_id, now=claimed_at)
    assert await store.claim_profile_refreshes(now=claimed_at + PROFILE_DEBOUNCE, limit=5) == []
    [again] = await store.claim_profile_refreshes(now=claimed_at + PROFILE_LEASE, limit=5)
    assert again.attempts == 1  # the new mark reset the attempts


async def test_finishing_stores_the_profile_and_clears_the_refresh(store: KnowledgeStore) -> None:
    tenant, kb_id = await _base(store)
    await store.mark_profile_refresh(tenant_id=tenant, kb_id=kb_id, now=T0)
    [claim] = await store.claim_profile_refreshes(now=T0 + PROFILE_DEBOUNCE, limit=5)
    await store.finish_profile_refresh(
        tenant_id=tenant,
        kb_id=kb_id,
        marks=claim.marks,
        profile=_profile("one"),
        calibration=_CALIBRATION,
    )
    record = await _record(store, tenant)
    assert record.profile == _profile("one")  # type: ignore[attr-defined]
    assert record.calibration == _CALIBRATION  # type: ignore[attr-defined]
    assert record.refresh is None  # type: ignore[attr-defined]
    far = T0 + PROFILE_MAX_WAIT * 10
    assert await store.claim_profile_refreshes(now=far, limit=5) == []


async def test_a_change_during_the_refresh_keeps_it_asked_for(store: KnowledgeStore) -> None:
    tenant, kb_id = await _base(store)
    await store.mark_profile_refresh(tenant_id=tenant, kb_id=kb_id, now=T0)
    [claim] = await store.claim_profile_refreshes(now=T0 + PROFILE_DEBOUNCE, limit=5)
    await store.mark_profile_refresh(tenant_id=tenant, kb_id=kb_id, now=T0 + PROFILE_DEBOUNCE)
    await store.finish_profile_refresh(
        tenant_id=tenant, kb_id=kb_id, marks=claim.marks, profile=_profile("one"), calibration=None
    )
    record = await _record(store, tenant)
    assert record.profile == _profile("one")  # type: ignore[attr-defined]
    assert record.refresh is not None  # type: ignore[attr-defined]
    assert record.refresh.marks == 2  # type: ignore[attr-defined]


async def test_calibration_only_finish_keeps_the_profile(store: KnowledgeStore) -> None:
    tenant, kb_id = await _base(store)
    await store.mark_profile_refresh(tenant_id=tenant, kb_id=kb_id, now=T0)
    [claim] = await store.claim_profile_refreshes(now=T0 + PROFILE_DEBOUNCE, limit=5)
    await store.finish_profile_refresh(
        tenant_id=tenant, kb_id=kb_id, marks=claim.marks, profile=_profile("one"), calibration=None
    )
    await store.mark_profile_refresh(tenant_id=tenant, kb_id=kb_id, now=T0, calibrate_only=True)
    [claim] = await store.claim_profile_refreshes(now=T0 + PROFILE_DEBOUNCE, limit=5)
    assert claim.calibrate_only is True
    await store.finish_profile_refresh(
        tenant_id=tenant, kb_id=kb_id, marks=claim.marks, profile=None, calibration=_CALIBRATION
    )
    record = await _record(store, tenant)
    assert record.profile == _profile("one")  # type: ignore[attr-defined]
    assert record.calibration == _CALIBRATION  # type: ignore[attr-defined]


async def test_a_full_refresh_asked_for_wins_over_calibration_only(store: KnowledgeStore) -> None:
    tenant, kb_id = await _base(store)
    await store.mark_profile_refresh(tenant_id=tenant, kb_id=kb_id, now=T0, calibrate_only=True)
    await store.mark_profile_refresh(tenant_id=tenant, kb_id=kb_id, now=T0)
    [claim] = await store.claim_profile_refreshes(now=T0 + PROFILE_DEBOUNCE, limit=5)
    assert claim.calibrate_only is False


async def test_a_failure_keeps_the_old_profile_retries_then_gives_up(
    store: KnowledgeStore,
) -> None:
    tenant, kb_id = await _base(store)
    await store.mark_profile_refresh(tenant_id=tenant, kb_id=kb_id, now=T0)
    [claim] = await store.claim_profile_refreshes(now=T0 + PROFILE_DEBOUNCE, limit=5)
    await store.finish_profile_refresh(
        tenant_id=tenant, kb_id=kb_id, marks=claim.marks, profile=_profile("old"), calibration=None
    )
    await store.mark_profile_refresh(tenant_id=tenant, kb_id=kb_id, now=T0)
    now = T0 + PROFILE_DEBOUNCE
    for attempt in range(1, PROFILE_MAX_ATTEMPTS + 1):
        [claim] = await store.claim_profile_refreshes(now=now, limit=5)
        assert claim.attempts == attempt
        await store.fail_profile_refresh(tenant_id=tenant, kb_id=kb_id, now=now)
        now += PROFILE_RETRY_AFTER
    assert await store.claim_profile_refreshes(now=now + PROFILE_MAX_WAIT, limit=5) == []
    record = await _record(store, tenant)
    assert record.profile == _profile("old")  # type: ignore[attr-defined]
    assert record.refresh is None  # type: ignore[attr-defined]


async def test_claims_respect_the_limit(store: KnowledgeStore) -> None:
    for _ in range(3):
        tenant, kb_id = await _base(store)
        await store.mark_profile_refresh(tenant_id=tenant, kb_id=kb_id, now=T0)
    assert len(await store.claim_profile_refreshes(now=T0 + PROFILE_DEBOUNCE, limit=2)) == 2
    assert len(await store.claim_profile_refreshes(now=T0 + PROFILE_DEBOUNCE, limit=2)) == 1


async def test_the_digest_lists_titles_headings_and_openings(store: KnowledgeStore) -> None:
    tenant, kb_id = await _base(store)
    long_opening = "开头" * PROFILE_OPENING_CHARS
    await _ready_document(
        store,
        tenant,
        kb_id,
        "a.md",
        [
            (("第一章", "第一节", "小节"), long_opening),
            (("第一章", "第一节"), "x"),
            (("第二章",), "y"),
            ((), "z"),
            (("第三章",), "w"),
        ],
    )
    await _ready_document(store, tenant, kb_id, "b.md", [((), "  多余   空白  ")])
    await store.upsert_document(tenant_id=tenant, kb_id=kb_id, filename="c.md", content=b"x")
    digest = await store.profile_digest(
        tenant_id=tenant, kb_id=kb_id, max_documents=5, max_headings=2
    )
    assert digest.ready_documents == 2
    first, second = digest.documents
    assert first.title == "a"
    assert first.headings == ("第一章 > 第一节", "第二章")
    assert first.opening == long_opening[:PROFILE_OPENING_CHARS]
    assert (second.title, second.headings, second.opening) == ("b", (), "多余 空白")


async def test_the_digest_caps_documents_but_counts_them_all(store: KnowledgeStore) -> None:
    tenant, kb_id = await _base(store)
    for name in ("a.md", "b.md", "c.md"):
        await _ready_document(store, tenant, kb_id, name, [((), name)])
    digest = await store.profile_digest(
        tenant_id=tenant, kb_id=kb_id, max_documents=2, max_headings=3
    )
    assert digest.ready_documents == 3
    assert len(digest.documents) == 2


async def test_another_tenant_cannot_see_the_digest(store: KnowledgeStore) -> None:
    tenant, kb_id = await _base(store)
    await _ready_document(store, tenant, kb_id, "a.md", [((), "secret")])
    digest = await store.profile_digest(
        tenant_id=uuid4(), kb_id=kb_id, max_documents=5, max_headings=3
    )
    assert digest.documents == ()
    assert digest.ready_documents == 0


def test_timedelta_constants_are_ordered() -> None:
    """The debounce is shorter than the max wait, the lease than a retry gap
    plus the debounce: a held refresh is never claimed twice by the debounce."""
    assert PROFILE_DEBOUNCE < PROFILE_MAX_WAIT
    assert PROFILE_LEASE > PROFILE_DEBOUNCE
    assert isinstance(PROFILE_RETRY_AFTER, timedelta)
