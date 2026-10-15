"""SqlKnowledgeStore vector recall against real HNSW — B-175 wave 2 PR2.

``search_scored`` sets ``hnsw.ef_search`` (and, on pgvector >= 0.8,
``hnsw.iterative_scan``) transaction-locally before the vector SELECT. These
tests watch the SELECT on the wire: a ``before_cursor_execute`` probe reads,
on the same connection and inside the same transaction, the settings in
effect and the plan the statement gets.

Postgres picks the HNSW index on its own only for big tables; the tests that
need it force it with session settings on a dedicated engine (no sort, no
sequential or bitmap scan), and assert the plan really used the index.

Each test gets its own database (copied from a migrated template), so the
HNSW graph holds only that test's vectors. Every vector leans on one shared
dimension: mutually orthogonal clusters would leave the graph search on a
plateau of equal distances, which real embeddings never are.
"""

from __future__ import annotations

import logging
import random
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import event, text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from testcontainers.postgres import PostgresContainer

from expert_work.persistence import SqlKnowledgeStore, create_async_session_factory
from expert_work.persistence.embedding import EMBEDDING_DIM
from expert_work.persistence.knowledge import sql as knowledge_sql
from expert_work.protocol import KnowledgeChunk

pytestmark = pytest.mark.integration

ALEMBIC_INI = Path(__file__).resolve().parent.parent / "alembic.ini"

#: Session settings that leave the HNSW index scan as the only un-penalised
#: plan for ``ORDER BY embedding <=> q LIMIT n`` (the alternatives all sort or
#: scan the heap).
_FORCE_HNSW = {"enable_seqscan": "off", "enable_bitmapscan": "off", "enable_sort": "off"}
_INDEX = "knowledge_chunk_embedding_idx"
_SQL_LOGGER = "expert_work.persistence.knowledge.sql"
_TEMPLATE_DB = "kb_vector_search_template"
#: The dimension every test vector leans on (see module docstring).
_SHARED_DIM = 0


def _admin_dsn(container: PostgresContainer) -> str:
    return str(container.get_connection_url()).replace("+psycopg2", "")


def _dsn_for(container: PostgresContainer, database: str, driver: str) -> str:
    base, _, _ = _admin_dsn(container).rpartition("/")
    return f"{base}/{database}".replace("postgresql://", f"postgresql+{driver}://", 1)


@pytest.fixture(scope="module")
def template_db(postgres_container: PostgresContainer) -> Iterator[str]:
    admin = _admin_dsn(postgres_container)
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{_TEMPLATE_DB}" WITH (FORCE)')
        conn.execute(f'CREATE DATABASE "{_TEMPLATE_DB}"')
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", _dsn_for(postgres_container, _TEMPLATE_DB, "psycopg"))
    command.upgrade(cfg, "head")
    yield _TEMPLATE_DB
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{_TEMPLATE_DB}" WITH (FORCE)')


@pytest.fixture
def migrated(postgres_container: PostgresContainer, template_db: str) -> Iterator[str]:
    """Async DSN of a fresh, migrated, empty database for this test."""
    admin = _admin_dsn(postgres_container)
    database = f"kb_vector_search_{uuid4().hex[:12]}"
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{database}" TEMPLATE "{template_db}"')
    yield _dsn_for(postgres_container, database, "asyncpg")
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)')


def _engine(dsn: str, *, force_hnsw: bool = False) -> AsyncEngine:
    """One pooled connection, so every search in a test reuses one backend."""
    connect_args: dict[str, Any] = {"server_settings": _FORCE_HNSW} if force_hnsw else {}
    return create_async_engine(dsn, pool_size=1, max_overflow=0, connect_args=connect_args)


class _VectorSelectProbe:
    """Right before each vector SELECT (``<=>``) runs, records — on the same
    connection, inside its transaction — the HNSW settings in effect, the
    backend pid and the statement's plan. ``statements`` keeps every
    statement the store sends around it (HNSW settings, version read,
    savepoints)."""

    def __init__(self, engine: AsyncEngine) -> None:
        self.seen: list[dict[str, Any]] = []
        self.statements: list[str] = []
        event.listen(engine.sync_engine, "before_cursor_execute", self._before)

    def around_vector_search(self) -> list[str]:
        """The recorded statements that belong to the vector search path."""
        keep = ("hnsw", "extversion", "SAVEPOINT", "<=>")
        return [sql for sql in self.statements if any(mark in sql for mark in keep)]

    def _before(
        self,
        conn: Connection,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        del cursor, context, executemany
        self.statements.append(statement)
        if "<=>" not in statement:
            return
        probe = conn.connection.cursor()
        try:
            probe.execute(
                "SELECT current_setting('hnsw.ef_search', true),"
                " current_setting('hnsw.iterative_scan', true), pg_backend_pid()"
            )
            settings = probe.fetchone()
            assert settings is not None
            ef_search, iterative_scan, pid = settings
            probe.execute("EXPLAIN " + statement, parameters)
            plan = "\n".join(str(row[0]) for row in probe.fetchall())
        finally:
            probe.close()
        self.seen.append(
            {"ef_search": ef_search, "iterative_scan": iterative_scan, "pid": pid, "plan": plan}
        )


async def _settings_now(engine: AsyncEngine) -> tuple[str | None, str | None, int]:
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT current_setting('hnsw.ef_search', true),"
                    " current_setting('hnsw.iterative_scan', true), pg_backend_pid()"
                )
            )
        ).one()
    return row[0], row[1], row[2]


def _near(dim: int, rng: random.Random) -> tuple[float, ...]:
    """A chunk vector in the cluster around :func:`_query` ``(dim)``."""
    values = list(_query(dim))
    for d in range(dim + 1, dim + 9):
        values[d] = rng.uniform(0.0, 0.05)
    return tuple(values)


def _query(dim: int) -> tuple[float, ...]:
    values = [0.0] * EMBEDDING_DIM
    values[_SHARED_DIM] = 1.0
    values[dim] = 1.0
    return tuple(values)


async def _seed(
    store: SqlKnowledgeStore, tenant: UUID, embeddings: list[tuple[float, ...]]
) -> UUID:
    base = await store.create_base(tenant_id=tenant, name="kb")
    document = await store.upsert_document(tenant_id=tenant, kb_id=base.id, filename="d.pdf")
    await store.replace_chunks(
        tenant_id=tenant,
        document_id=document.id,
        chunks=[
            KnowledgeChunk(
                id=uuid4(),
                tenant_id=tenant,
                kb_id=base.id,
                document_id=document.id,
                chunk_index=i,
                content=f"chunk {i}",
                embedding=embedding,
            )
            for i, embedding in enumerate(embeddings)
        ],
    )
    return base.id


@pytest.mark.asyncio
async def test_hnsw_settings_are_local_to_the_vector_select_transaction(migrated: str) -> None:
    engine = _engine(migrated)
    probe = _VectorSelectProbe(engine)
    store = SqlKnowledgeStore(create_async_session_factory(engine))
    try:
        rng = random.Random(1)  # noqa: S311 — reproducible test data, not crypto
        tenant = uuid4()
        kb_id = await _seed(store, tenant, [_near(40, rng)] * 3)

        hits = await store.search_scored(
            tenant_id=tenant, kb_ids=[kb_id], query_embedding=_query(40), limit=5
        )
        assert len(hits) == 3
        assert len(probe.seen) == 1
        # Inside the SELECT's transaction: our values (ef_search >= 200).
        assert probe.seen[0]["ef_search"] == "200"
        assert probe.seen[0]["iterative_scan"] == "strict_order"

        # A deeper search raises ef_search to the limit.
        await store.search_scored(
            tenant_id=tenant, kb_ids=[kb_id], query_embedding=_query(40), limit=300
        )
        assert probe.seen[1]["ef_search"] == "300"

        # Wire shape: the version is read once per store, then one settings
        # statement per search right before the SELECT — no savepoint.
        sent = probe.around_vector_search()
        assert len(sent) == 5
        assert "extversion" in sent[0]
        for settings_sql, select_sql in (sent[1:3], sent[3:5]):
            assert "hnsw.ef_search" in settings_sql
            assert "hnsw.iterative_scan" in settings_sql
            assert "<=>" in select_sql

        # Same backend, next transaction: back to the server defaults
        # (transaction-local, so nothing leaks into pooled connections).
        ef_after, mode_after, pid_after = await _settings_now(engine)
        assert pid_after == probe.seen[0]["pid"]
        assert ef_after == "40"
        assert mode_after == "off"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_small_tenant_in_big_table_still_gets_limit_rows_via_hnsw(migrated: str) -> None:
    # 600 chunks of another tenant sit right next to the query; ours (30) are
    # further away. An HNSW scan that stops at ef_search candidates sees only
    # the other tenant's rows and the tenant filter leaves nothing; the
    # iterative scan keeps going until ``limit`` rows survive the filter.
    engine = _engine(migrated, force_hnsw=True)
    probe = _VectorSelectProbe(engine)
    store = SqlKnowledgeStore(create_async_session_factory(engine))
    try:
        rng = random.Random(2)  # noqa: S311 — reproducible test data, not crypto
        crowd = uuid4()
        await _seed(store, crowd, [_near(60, rng) for _ in range(600)])
        tenant = uuid4()
        kb_id = await _seed(store, tenant, [_near(70, rng) for _ in range(30)])

        hits = await store.search_scored(
            tenant_id=tenant, kb_ids=[kb_id], query_embedding=_query(60), limit=20
        )
        assert _INDEX in probe.seen[-1]["plan"]
        assert len(hits) == 20
        assert {hit.chunk.tenant_id for hit in hits} == {tenant}
        scores = [hit.score for hit in hits]
        assert scores == sorted(scores, reverse=True)  # strict_order keeps exact order
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("version", [(0, 7, 4), ()], ids=["0.7.4", "unreadable"])
async def test_pre_08_pgvector_skips_iterative_scan_and_ef_search_alone_fills_limit(
    migrated: str, version: tuple[int, ...]
) -> None:
    # pgvector < 0.8 (or a version that cannot be read) gets no iterative scan;
    # an HNSW scan then returns at most ef_search rows (server default 40).
    # ef_search = max(200, limit) must still return a full ``limit`` of 100.
    engine = _engine(migrated, force_hnsw=True)
    probe = _VectorSelectProbe(engine)
    store = SqlKnowledgeStore(create_async_session_factory(engine))
    store._vector_version = version  # what the store would have read
    try:
        rng = random.Random(3)  # noqa: S311 — reproducible test data, not crypto
        tenant = uuid4()
        kb_id = await _seed(store, tenant, [_near(90, rng) for _ in range(150)])

        hits = await store.search_scored(
            tenant_id=tenant, kb_ids=[kb_id], query_embedding=_query(90), limit=100
        )
        assert _INDEX in probe.seen[-1]["plan"]
        assert probe.seen[-1]["iterative_scan"] == "off"
        assert probe.seen[-1]["ef_search"] == "200"
        assert len(hits) == 100
        assert not any("hnsw.iterative_scan" in sql for sql in probe.statements)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("constant", "value", "marker"),
    [
        # 22023 invalid_parameter_value: the build rejects the value.
        ("_HNSW_ITERATIVE_SCAN", "no_such_mode", "hnsw.iterative_scan"),
        # 42602 invalid_name: an ``hnsw.*`` parameter this build does not
        # define (what pgvector < 0.8 answers once its library is loaded).
        ("_HNSW_ITERATIVE_SCAN_GUC", "hnsw.no_such_setting", "hnsw.no_such_setting"),
        # 42704 undefined_object: an unrecognized parameter.
        ("_HNSW_ITERATIVE_SCAN_GUC", "no_such_setting", "no_such_setting"),
    ],
    ids=["22023", "42602", "42704"],
)
async def test_rejected_iterative_scan_is_dropped_for_good_and_ef_search_still_applies(
    migrated: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    constant: str,
    value: str,
    marker: str,
) -> None:
    # Stand-in for a build that rejects ``hnsw.iterative_scan``: Postgres
    # refuses the setting, which aborts the search's transaction. The search
    # is redone in a fresh transaction with ef_search only, and the store
    # never sends iterative_scan again.
    monkeypatch.setattr(knowledge_sql, constant, value)
    engine = _engine(migrated)
    probe = _VectorSelectProbe(engine)
    store = SqlKnowledgeStore(create_async_session_factory(engine))
    try:
        rng = random.Random(4)  # noqa: S311 — reproducible test data, not crypto
        tenant = uuid4()
        near, far = _near(100, rng), _query(110)
        kb_id = await _seed(store, tenant, [far, near])
        # Load pgvector into the backend first: before that, an unknown value
        # is only stored as a placeholder and nothing fails.
        async with engine.connect() as conn:
            await conn.execute(text("SELECT '[1]'::vector"))

        caplog.set_level(logging.WARNING, logger=_SQL_LOGGER)
        for _ in range(3):
            hits = await store.search_scored(
                tenant_id=tenant, kb_ids=[kb_id], query_embedding=_query(100), limit=5
            )
            assert [hit.chunk.chunk_index for hit in hits] == [1, 0]
        # Every vector SELECT ran with ef_search raised and no iterative scan.
        assert [(seen["ef_search"], seen["iterative_scan"]) for seen in probe.seen] == [
            ("200", "off")
        ] * 3
        # iterative_scan was sent once (rejected) and never again.
        settings_sent = [sql for sql in probe.statements if "hnsw.ef_search" in sql]
        assert [marker in sql for sql in settings_sent] == [
            True,
            False,
            False,
            False,
        ]
        warnings = [r for r in caplog.records if r.name == _SQL_LOGGER]
        assert len(warnings) == 1
        assert "iterative_scan" in warnings[0].getMessage()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_other_db_error_on_settings_propagates_and_keeps_iterative_scan(
    migrated: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # A failure that is not the setting being rejected (here 22012, standing in
    # for a dropped connection or a timeout) fails that search as before and
    # turns nothing off: the next search still sends iterative_scan.
    real_set_hnsw_scan = knowledge_sql._set_hnsw_scan
    fail_next = [True]

    async def _fails_once(session: Any, limit: int, *, iterative: bool) -> None:
        if fail_next:
            fail_next.clear()
            await session.execute(text("SELECT 1 / 0"))
        await real_set_hnsw_scan(session, limit, iterative=iterative)

    monkeypatch.setattr(knowledge_sql, "_set_hnsw_scan", _fails_once)
    engine = _engine(migrated)
    probe = _VectorSelectProbe(engine)
    store = SqlKnowledgeStore(create_async_session_factory(engine))
    try:
        rng = random.Random(5)  # noqa: S311 — reproducible test data, not crypto
        tenant = uuid4()
        kb_id = await _seed(store, tenant, [_query(130), _near(120, rng)])

        caplog.set_level(logging.WARNING, logger=_SQL_LOGGER)
        with pytest.raises(DBAPIError) as failure:
            await store.search_scored(
                tenant_id=tenant, kb_ids=[kb_id], query_embedding=_query(120), limit=5
            )
        assert getattr(failure.value.orig, "sqlstate", None) == "22012"
        assert probe.seen == []  # the search itself never ran

        hits = await store.search_scored(
            tenant_id=tenant, kb_ids=[kb_id], query_embedding=_query(120), limit=5
        )
        assert [hit.chunk.chunk_index for hit in hits] == [1, 0]
        assert (probe.seen[-1]["ef_search"], probe.seen[-1]["iterative_scan"]) == (
            "200",
            "strict_order",
        )
        assert [r for r in caplog.records if r.name == _SQL_LOGGER] == []
    finally:
        await engine.dispose()
