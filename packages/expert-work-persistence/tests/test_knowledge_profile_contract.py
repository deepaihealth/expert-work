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
from sqlalchemy import text
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
    PROFILE_HEADING_SCAN,
    PROFILE_LEASE,
    PROFILE_MAX_ATTEMPTS,
    PROFILE_MAX_WAIT,
    PROFILE_OPENING_CHARS,
    PROFILE_RETRY_AFTER,
    ProfileClaim,
    sample_positions,
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


async def _finish(
    store: KnowledgeStore,
    tenant: UUID,
    kb_id: UUID,
    claim: ProfileClaim,
    *,
    profile: KnowledgeProfile | None,
    calibration: ProbeCalibration | None = None,
    retry_calibration: bool = False,
) -> None:
    await store.finish_profile_refresh(
        tenant_id=tenant,
        kb_id=kb_id,
        holder=claim.holder,
        marks=claim.marks,
        profile=profile,
        calibration=calibration,
        now=T0 + PROFILE_DEBOUNCE,
        retry_calibration=retry_calibration,
    )


async def _with_profile(store: KnowledgeStore, tenant: UUID, kb_id: UUID, summary: str) -> None:
    await store.mark_profile_refresh(tenant_id=tenant, kb_id=kb_id, now=T0)
    [claim] = await store.claim_profile_refreshes(now=T0 + PROFILE_DEBOUNCE, limit=5)
    await _finish(store, tenant, kb_id, claim, profile=_profile(summary))


async def test_finishing_stores_the_profile_and_clears_the_refresh(store: KnowledgeStore) -> None:
    tenant, kb_id = await _base(store)
    await store.mark_profile_refresh(tenant_id=tenant, kb_id=kb_id, now=T0)
    [claim] = await store.claim_profile_refreshes(now=T0 + PROFILE_DEBOUNCE, limit=5)
    assert claim.holder
    await _finish(store, tenant, kb_id, claim, profile=_profile("one"), calibration=_CALIBRATION)
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
    await _finish(store, tenant, kb_id, claim, profile=_profile("one"))
    record = await _record(store, tenant)
    assert record.profile == _profile("one")  # type: ignore[attr-defined]
    assert record.refresh is not None  # type: ignore[attr-defined]
    assert record.refresh.marks == 2  # type: ignore[attr-defined]
    # Asked for again after the debounce, not held until the old lease ends.
    [again] = await store.claim_profile_refreshes(now=T0 + 3 * PROFILE_DEBOUNCE, limit=5)
    assert again.marks == 2


async def test_a_worker_whose_lease_ran_out_cannot_overwrite(store: KnowledgeStore) -> None:
    tenant, kb_id = await _base(store)
    await store.mark_profile_refresh(tenant_id=tenant, kb_id=kb_id, now=T0)
    [late] = await store.claim_profile_refreshes(now=T0 + PROFILE_DEBOUNCE, limit=5)
    [fresh] = await store.claim_profile_refreshes(
        now=T0 + PROFILE_DEBOUNCE + PROFILE_LEASE, limit=5
    )
    # The late worker reports while the fresh one still holds the refresh.
    await _finish(store, tenant, kb_id, late, profile=_profile("late"))
    await store.fail_profile_refresh(
        tenant_id=tenant, kb_id=kb_id, holder=late.holder, now=T0 + PROFILE_LEASE
    )
    held = await _record(store, tenant)
    assert held.profile is None  # type: ignore[attr-defined]
    assert held.refresh.holder == fresh.holder  # type: ignore[attr-defined]
    await _finish(store, tenant, kb_id, fresh, profile=_profile("fresh"))
    record = await _record(store, tenant)
    assert record.profile == _profile("fresh")  # type: ignore[attr-defined]
    assert record.refresh is None  # type: ignore[attr-defined]


async def test_calibration_only_finish_keeps_the_profile(store: KnowledgeStore) -> None:
    tenant, kb_id = await _base(store)
    await _with_profile(store, tenant, kb_id, "one")
    await store.mark_profile_refresh(tenant_id=tenant, kb_id=kb_id, now=T0, calibrate_only=True)
    [claim] = await store.claim_profile_refreshes(now=T0 + PROFILE_DEBOUNCE, limit=5)
    assert claim.calibrate_only is True
    await _finish(store, tenant, kb_id, claim, profile=None, calibration=_CALIBRATION)
    record = await _record(store, tenant)
    assert record.profile == _profile("one")  # type: ignore[attr-defined]
    assert record.calibration == _CALIBRATION  # type: ignore[attr-defined]


async def test_a_failed_calibration_keeps_the_paid_profile_and_retries_alone(
    store: KnowledgeStore,
) -> None:
    tenant, kb_id = await _base(store)
    await store.mark_profile_refresh(tenant_id=tenant, kb_id=kb_id, now=T0)
    [claim] = await store.claim_profile_refreshes(now=T0 + PROFILE_DEBOUNCE, limit=5)
    await _finish(store, tenant, kb_id, claim, profile=_profile("paid"), retry_calibration=True)
    record = await _record(store, tenant)
    assert record.profile == _profile("paid")  # type: ignore[attr-defined]
    [retry] = await store.claim_profile_refreshes(
        now=T0 + PROFILE_DEBOUNCE + PROFILE_RETRY_AFTER, limit=5
    )
    assert retry.calibrate_only is True


async def test_a_full_refresh_asked_for_wins_over_calibration_only(store: KnowledgeStore) -> None:
    tenant, kb_id = await _base(store)
    await store.mark_profile_refresh(tenant_id=tenant, kb_id=kb_id, now=T0, calibrate_only=True)
    await store.mark_profile_refresh(tenant_id=tenant, kb_id=kb_id, now=T0)
    [claim] = await store.claim_profile_refreshes(now=T0 + PROFILE_DEBOUNCE, limit=5)
    assert claim.calibrate_only is False


async def test_a_failure_keeps_the_old_profile_retries_once_then_gives_up(
    store: KnowledgeStore,
) -> None:
    assert PROFILE_MAX_ATTEMPTS == 2  # a billed but unusable answer is paid for twice at most
    tenant, kb_id = await _base(store)
    await _with_profile(store, tenant, kb_id, "old")
    await store.mark_profile_refresh(tenant_id=tenant, kb_id=kb_id, now=T0)
    now = T0 + PROFILE_DEBOUNCE
    for attempt in range(1, PROFILE_MAX_ATTEMPTS + 1):
        [claim] = await store.claim_profile_refreshes(now=now, limit=5)
        assert claim.attempts == attempt
        await store.fail_profile_refresh(
            tenant_id=tenant, kb_id=kb_id, holder=claim.holder, now=now
        )
        now += PROFILE_RETRY_AFTER
    assert await store.claim_profile_refreshes(now=now + PROFILE_MAX_WAIT, limit=5) == []
    record = await _record(store, tenant)
    assert record.profile == _profile("old")  # type: ignore[attr-defined]
    assert record.refresh is None  # type: ignore[attr-defined]


async def test_crashed_claims_count_as_attempts(store: KnowledgeStore) -> None:
    """A claim that never finishes or fails (a crash, a cancel) still uses an
    attempt: the refresh is dropped, not retried forever."""
    tenant, kb_id = await _base(store)
    await store.mark_profile_refresh(tenant_id=tenant, kb_id=kb_id, now=T0)
    now = T0 + PROFILE_DEBOUNCE
    for _ in range(PROFILE_MAX_ATTEMPTS):
        assert len(await store.claim_profile_refreshes(now=now, limit=5)) == 1
        now += PROFILE_LEASE
    assert await store.claim_profile_refreshes(now=now, limit=5) == []
    assert (await _record(store, tenant)) is None


async def test_claims_respect_the_limit(store: KnowledgeStore) -> None:
    for _ in range(3):
        tenant, kb_id = await _base(store)
        await store.mark_profile_refresh(tenant_id=tenant, kb_id=kb_id, now=T0)
    assert len(await store.claim_profile_refreshes(now=T0 + PROFILE_DEBOUNCE, limit=2)) == 2
    assert len(await store.claim_profile_refreshes(now=T0 + PROFILE_DEBOUNCE, limit=2)) == 1


# -- staleness ------------------------------------------------------------------------


async def test_deleting_a_document_with_passages_stales_the_profile(
    store: KnowledgeStore,
) -> None:
    tenant, kb_id = await _base(store)
    document_id = await _ready_document(store, tenant, kb_id, "a.md", [((), "x")])
    await _with_profile(store, tenant, kb_id, "one")
    unparsed = await store.upsert_document(
        tenant_id=tenant, kb_id=kb_id, filename="b.md", content=b"x"
    )
    await store.delete_document(tenant_id=tenant, document_id=unparsed.id)
    assert (await _record(store, tenant)).stale_since is None  # type: ignore[attr-defined]
    await store.delete_document(tenant_id=tenant, document_id=document_id)
    record = await _record(store, tenant)
    assert record.stale_since is not None  # type: ignore[attr-defined]
    assert record.shown_profile() is None  # type: ignore[attr-defined]


async def test_switching_a_document_off_stales_the_profile_on_does_not(
    store: KnowledgeStore,
) -> None:
    tenant, kb_id = await _base(store)
    document_id = await _ready_document(store, tenant, kb_id, "a.md", [((), "x")])
    await _with_profile(store, tenant, kb_id, "one")
    await store.set_documents_enabled(
        tenant_id=tenant, kb_id=kb_id, document_ids=[document_id], enabled=True
    )
    assert (await _record(store, tenant)).stale_since is None  # type: ignore[attr-defined]
    await store.set_documents_enabled(
        tenant_id=tenant, kb_id=kb_id, document_ids=[document_id], enabled=False
    )
    assert (await _record(store, tenant)).stale_since is not None  # type: ignore[attr-defined]


async def test_a_new_profile_clears_stale_a_calibration_does_not(store: KnowledgeStore) -> None:
    tenant, kb_id = await _base(store)
    document_id = await _ready_document(store, tenant, kb_id, "a.md", [((), "x")])
    await _with_profile(store, tenant, kb_id, "one")
    await store.set_documents_enabled(
        tenant_id=tenant, kb_id=kb_id, document_ids=[document_id], enabled=False
    )
    await store.mark_profile_refresh(tenant_id=tenant, kb_id=kb_id, now=T0, calibrate_only=True)
    [claim] = await store.claim_profile_refreshes(now=T0 + PROFILE_DEBOUNCE, limit=5)
    await _finish(store, tenant, kb_id, claim, profile=None, calibration=_CALIBRATION)
    assert (await _record(store, tenant)).stale_since is not None  # type: ignore[attr-defined]
    await _with_profile(store, tenant, kb_id, "two")
    record = await _record(store, tenant)
    assert record.stale_since is None  # type: ignore[attr-defined]
    assert record.shown_profile() == _profile("two")  # type: ignore[attr-defined]


@pytest.mark.parametrize("first", [True, False], ids=["first-profile", "replacing"])
async def test_a_document_deleted_during_a_refresh_keeps_the_new_profile_hidden(
    store: KnowledgeStore, first: bool
) -> None:
    """The refresh may have read the deleted document: its profile is stored
    but not shown, and nothing is spent again on its own."""
    tenant, kb_id = await _base(store)
    document_id = await _ready_document(store, tenant, kb_id, "a.md", [((), "x")])
    if not first:
        await _with_profile(store, tenant, kb_id, "old")
    await store.mark_profile_refresh(tenant_id=tenant, kb_id=kb_id, now=T0)
    [claim] = await store.claim_profile_refreshes(now=T0 + PROFILE_DEBOUNCE, limit=5)
    await store.delete_document(tenant_id=tenant, document_id=document_id)
    await _finish(store, tenant, kb_id, claim, profile=_profile("read-the-deleted"))
    record = await _record(store, tenant)
    assert record.profile == _profile("read-the-deleted")  # type: ignore[attr-defined]
    assert record.shown_profile() is None  # type: ignore[attr-defined]
    assert record.refresh is None  # type: ignore[attr-defined]


async def test_switching_off_a_document_already_off_changes_nothing(
    store: KnowledgeStore,
) -> None:
    tenant, kb_id = await _base(store)
    document_id = await _ready_document(store, tenant, kb_id, "a.md", [((), "x")])
    await store.set_documents_enabled(
        tenant_id=tenant, kb_id=kb_id, document_ids=[document_id], enabled=False
    )
    await _with_profile(store, tenant, kb_id, "one")
    await store.set_documents_enabled(
        tenant_id=tenant, kb_id=kb_id, document_ids=[document_id], enabled=False
    )
    assert (await _record(store, tenant)).shown_profile() == _profile("one")  # type: ignore[attr-defined]


async def test_no_profile_nothing_to_stale(store: KnowledgeStore) -> None:
    tenant, kb_id = await _base(store)
    document_id = await _ready_document(store, tenant, kb_id, "a.md", [((), "x")])
    await store.delete_document(tenant_id=tenant, document_id=document_id)
    assert (await _record(store, tenant)) is None


# -- digest ---------------------------------------------------------------------------


async def test_the_digest_describes_sampled_documents(store: KnowledgeStore) -> None:
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
        tenant_id=tenant, kb_id=kb_id, max_titles=5, detailed=5, max_headings=2
    )
    assert digest.ready_documents == 2
    newest, oldest = digest.documents
    assert (newest.title, newest.headings, newest.opening) == ("b", (), "多余 空白")
    assert oldest.title == "a"
    assert oldest.headings == ("第一章 > 第一节", "第二章")
    assert oldest.opening == long_opening[:PROFILE_OPENING_CHARS]


async def test_the_digest_lists_newest_first_and_samples_across(store: KnowledgeStore) -> None:
    tenant, kb_id = await _base(store)
    for number in range(9):
        await _ready_document(store, tenant, kb_id, f"d{number}.md", [(("h",), f"text {number}")])
    digest = await store.profile_digest(
        tenant_id=tenant, kb_id=kb_id, max_titles=7, detailed=3, max_headings=2
    )
    assert digest.ready_documents == 9
    assert [d.title for d in digest.documents] == [f"d{n}" for n in range(8, 1, -1)]
    described = [d.title for d in digest.documents if d.opening]
    assert described == ["d8", "d5", "d2"]  # newest, middle, oldest listed
    assert all(d.headings == () and d.opening == "" for d in digest.documents if not d.opening)


async def test_headings_come_from_the_first_chunks_only(store: KnowledgeStore) -> None:
    tenant, kb_id = await _base(store)
    empty: tuple[str, ...] = ()
    chunks = [(empty, "x")] * PROFILE_HEADING_SCAN + [(("late",), "y")]
    await _ready_document(store, tenant, kb_id, "a.md", chunks)
    digest = await store.profile_digest(
        tenant_id=tenant, kb_id=kb_id, max_titles=5, detailed=5, max_headings=5
    )
    assert digest.documents[0].headings == ()


async def test_the_opening_comes_from_the_first_chunks_only(store: KnowledgeStore) -> None:
    """A document whose only enabled passage lies past the first chunks: no
    opening is read (the scan is bounded)."""
    tenant, kb_id = await _base(store)
    document = await store.upsert_document(
        tenant_id=tenant, kb_id=kb_id, filename="late.md", content=b"x"
    )
    chunk = KnowledgeChunk(
        id=uuid4(),
        tenant_id=tenant,
        kb_id=kb_id,
        document_id=document.id,
        chunk_index=PROFILE_HEADING_SCAN,
        content="far down",
        embedding=(1.0,) + (0.0,) * (EMBEDDING_DIM - 1),
    )
    await store.replace_chunks(tenant_id=tenant, document_id=document.id, chunks=[chunk])
    await store.set_document_status(
        tenant_id=tenant, document_id=document.id, status=DocumentStatus.READY, chunk_count=1
    )
    digest = await store.profile_digest(
        tenant_id=tenant, kb_id=kb_id, max_titles=5, detailed=5, max_headings=5
    )
    assert (digest.documents[0].title, digest.documents[0].opening) == ("late", "")


def test_sample_positions_are_spread_and_stable() -> None:
    assert sample_positions(3, 5) == [0, 1, 2]
    assert sample_positions(10, 3) == [0, 4, 9] or sample_positions(10, 3) == [0, 5, 9]
    assert sample_positions(10, 3) == sample_positions(10, 3)
    assert sample_positions(5, 1) == [0]
    assert sample_positions(5, 0) == []


async def test_another_tenant_cannot_see_the_digest(store: KnowledgeStore) -> None:
    tenant, kb_id = await _base(store)
    await _ready_document(store, tenant, kb_id, "a.md", [((), "secret")])
    digest = await store.profile_digest(
        tenant_id=uuid4(), kb_id=kb_id, max_titles=5, detailed=5, max_headings=3
    )
    assert digest.documents == ()
    assert digest.ready_documents == 0


def test_timedelta_constants_are_ordered() -> None:
    """The debounce is shorter than the max wait, the lease than a retry gap
    plus the debounce: a held refresh is never claimed twice by the debounce."""
    assert PROFILE_DEBOUNCE < PROFILE_MAX_WAIT
    assert PROFILE_LEASE > PROFILE_DEBOUNCE
    assert isinstance(PROFILE_RETRY_AFTER, timedelta)


@pytest.mark.integration
async def test_an_unreadable_record_is_cleaned_not_claimed(
    postgres_container: PostgresContainer,
) -> None:
    async with _sql_store(postgres_container) as sql_store:
        tenant, kb_id = await _base(sql_store)
        await sql_store.mark_profile_refresh(tenant_id=tenant, kb_id=kb_id, now=T0)
        async with sql_store._sf() as session:
            await session.execute(
                text(
                    "UPDATE knowledge_base SET profile = jsonb_set(profile, '{refresh,marks}', "
                    "'\"many\"') || '{\"keep\": 1}' WHERE id = :id"
                ),
                {"id": kb_id},
            )
            await session.commit()
        assert await sql_store.claim_profile_refreshes(now=T0 + PROFILE_DEBOUNCE, limit=5) == []
        async with sql_store._sf() as session:
            raw = (
                await session.execute(
                    text("SELECT profile FROM knowledge_base WHERE id = :id"), {"id": kb_id}
                )
            ).scalar_one()
        assert raw == {"keep": 1}


@pytest.mark.integration
@pytest.mark.parametrize("due_at", ["garbage", "2026-11-04T09:00:00"], ids=["malformed", "naive"])
async def test_an_unreadable_due_time_is_cleaned(
    postgres_container: PostgresContainer, due_at: str
) -> None:
    async with _sql_store(postgres_container) as sql_store:
        tenant, kb_id = await _base(sql_store)
        due = await _base(sql_store)
        await sql_store.mark_profile_refresh(tenant_id=tenant, kb_id=kb_id, now=T0)
        await sql_store.mark_profile_refresh(tenant_id=due[0], kb_id=due[1], now=T0)
        async with sql_store._sf() as session:
            await session.execute(
                text(
                    "UPDATE knowledge_base SET profile = jsonb_set(profile, "
                    "'{refresh,due_at}', to_jsonb(CAST(:due AS text))) WHERE id = :id"
                ),
                {"id": kb_id, "due": due_at},
            )
            await session.commit()
        claims = await sql_store.claim_profile_refreshes(now=T0 + PROFILE_DEBOUNCE, limit=5)
        assert [claim.kb_id for claim in claims] == [due[1]]
        async with sql_store._sf() as session:
            raw = (
                await session.execute(
                    text("SELECT profile FROM knowledge_base WHERE id = :id"), {"id": kb_id}
                )
            ).scalar_one()
        assert raw is None
