"""B-206 K-22 — the keyword path of a knowledge search fails in bounded time.

The keyword statement had no statement timeout (the database default, 30 s on
the deployed clusters, none on a stock server), and a plan gone wrong has taken
10 to 44 s (``sql.py``, the MATERIALIZED CTE note). It now runs under a
transaction-local ``statement_timeout``, and a database failure that says the
query could not finish (timeout, cancelled, lock / serialization, lost
connection, out of resources) surfaces as :class:`KnowledgeRecallError`, which
the retriever degrades on (vector results kept). Any other database error — a
bug in the SQL, a missing permission — propagates unchanged: degrading it would
hide it behind vector-only results on every search.

本地跑前: export DOCKER_HOST=unix:///Users/mac/.docker/run/docker.sock
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import func, text
from sqlalchemy.exc import DBAPIError, InterfaceError, OperationalError, ProgrammingError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.sql.elements import ColumnElement
from testcontainers.postgres import PostgresContainer

from expert_work.persistence import (
    DatabaseConfig,
    KnowledgeRecallError,
    SqlKnowledgeStore,
    create_async_engine_from_config,
    create_async_session_factory,
)
from expert_work.persistence.embedding import EMBEDDING_DIM
from expert_work.persistence.knowledge import KnowledgeRecallError as ReExported
from expert_work.persistence.knowledge import sql as sql_module
from expert_work.persistence.knowledge.text_search import KbKeywordQuery
from expert_work.protocol import KnowledgeChunk

ALEMBIC_INI = Path(__file__).resolve().parent.parent / "alembic.ini"

SqlStoreFixture = tuple[SqlKnowledgeStore, AsyncEngine, str]


def _sync_dsn(container: PostgresContainer) -> str:
    url = str(container.get_connection_url())
    return url.replace("+psycopg2", "+psycopg").replace("postgresql://", "postgresql+psycopg://", 1)


def _async_dsn(container: PostgresContainer) -> str:
    url = str(container.get_connection_url())
    return url.replace("+psycopg2", "+asyncpg").replace("postgresql://", "postgresql+asyncpg://", 1)


@pytest.fixture
def sql_store(postgres_container: PostgresContainer) -> Iterator[SqlStoreFixture]:
    """A store over a pool of exactly one connection, so whatever the keyword
    search leaves on its connection, the next checkout sees."""
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", _sync_dsn(postgres_container))
    command.upgrade(cfg, "head")
    dsn = _async_dsn(postgres_container)
    engine = create_async_engine_from_config(DatabaseConfig(dsn=dsn, pool_size=1, max_overflow=0))
    yield SqlKnowledgeStore(create_async_session_factory(engine)), engine, dsn


async def _seed(store: SqlKnowledgeStore) -> tuple[Any, Any]:
    tenant = uuid4()
    kb_id = (await store.create_base(tenant_id=tenant, name="kb")).id
    document = await store.upsert_document(tenant_id=tenant, kb_id=kb_id, filename="a.md")
    await store.replace_chunks(
        tenant_id=tenant,
        document_id=document.id,
        chunks=[
            KnowledgeChunk(
                id=uuid4(),
                tenant_id=tenant,
                kb_id=kb_id,
                document_id=document.id,
                chunk_index=0,
                content="苹果 沙拉",
                embedding=(1.0,) + (0.0,) * (EMBEDDING_DIM - 1),
            )
        ],
    )
    return tenant, kb_id


async def _session_settings(engine: AsyncEngine) -> tuple[int, str, str]:
    """``(backend pid, statement_timeout, plan_cache_mode)`` of the pool's connection."""
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT pg_backend_pid(), current_setting('statement_timeout'), "
                    "current_setting('plan_cache_mode')"
                )
            )
        ).one()
    return int(row[0]), str(row[1]), str(row[2])


def test_the_error_is_exported_from_both_packages() -> None:
    assert KnowledgeRecallError is ReExported


@pytest.mark.integration
async def test_a_blocked_keyword_search_fails_within_its_statement_timeout(
    sql_store: SqlStoreFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Another session holds ``knowledge_chunk`` exclusively (what a stuck plan
    or a long migration looks like from here): the keyword search gives up at
    its timeout with ``KnowledgeRecallError`` instead of waiting on the lock."""
    store, engine, dsn = sql_store
    holder_engine = create_async_engine_from_config(DatabaseConfig(dsn=dsn))
    try:
        tenant, kb_id = await _seed(store)
        monkeypatch.setattr(sql_module, "_KEYWORD_STATEMENT_TIMEOUT_MS", 200)
        async with holder_engine.connect() as holder:
            await holder.execute(text("LOCK TABLE knowledge_chunk IN ACCESS EXCLUSIVE MODE"))
            try:
                with pytest.raises(KnowledgeRecallError, match="statement timeout") as caught:
                    await asyncio.wait_for(
                        store.keyword_search_scored(
                            tenant_id=tenant, kb_ids=[kb_id], query="苹果", limit=5
                        ),
                        5,
                    )
            finally:
                await holder.rollback()
        assert caught.value.__cause__ is not None
        # The lock gone, the same search answers.
        hits = await store.keyword_search_scored(
            tenant_id=tenant, kb_ids=[kb_id], query="苹果", limit=5
        )
        assert [hit.chunk.content for hit in hits] == ["苹果 沙拉"]
    finally:
        await holder_engine.dispose()
        await engine.dispose()


@pytest.mark.integration
async def test_the_timeout_and_plan_mode_stay_inside_the_keyword_transaction(
    sql_store: SqlStoreFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On the one pooled connection, after a keyword search that answered and
    after one that timed out, the server's own settings are back: ingestion and
    migrations sharing the pool never run under a 5 s timeout."""
    store, engine, dsn = sql_store
    holder_engine = create_async_engine_from_config(DatabaseConfig(dsn=dsn))
    try:
        tenant, kb_id = await _seed(store)
        before = await _session_settings(engine)
        await store.keyword_search_scored(tenant_id=tenant, kb_ids=[kb_id], query="苹果", limit=5)
        assert await _session_settings(engine) == before
        monkeypatch.setattr(sql_module, "_KEYWORD_STATEMENT_TIMEOUT_MS", 200)
        async with holder_engine.connect() as holder:
            await holder.execute(text("LOCK TABLE knowledge_chunk IN ACCESS EXCLUSIVE MODE"))
            try:
                with pytest.raises(KnowledgeRecallError):
                    await store.keyword_search_scored(
                        tenant_id=tenant, kb_ids=[kb_id], query="苹果", limit=5
                    )
            finally:
                await holder.rollback()
        assert await _session_settings(engine) == before
    finally:
        await holder_engine.dispose()
        await engine.dispose()


@pytest.mark.integration
async def test_a_bug_in_the_keyword_sql_propagates_as_itself(
    sql_store: SqlStoreFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An undefined function (SQLSTATE 42883) is a bug, not a recall failure."""
    store, engine, _dsn = sql_store
    real = sql_module._kb_keyword_tsquery

    def broken(kb_query: KbKeywordQuery) -> ColumnElement[Any]:
        return cast("ColumnElement[Any]", func.no_such_function(real(kb_query)))

    try:
        tenant, kb_id = await _seed(store)
        monkeypatch.setattr(sql_module, "_kb_keyword_tsquery", broken)
        with pytest.raises(ProgrammingError) as caught:
            await store.keyword_search_scored(
                tenant_id=tenant, kb_ids=[kb_id], query="苹果", limit=5
            )
        assert getattr(caught.value.orig, "sqlstate", None) == "42883"
    finally:
        await engine.dispose()


class _DriverError(Exception):
    """A driver exception as SQLAlchemy wraps it: the message and ``sqlstate``."""

    def __init__(self, message: str, sqlstate: str | None) -> None:
        super().__init__(message)
        self.sqlstate = sqlstate


class _FailingSession:
    """A session whose every statement fails the way a driver error does."""

    def __init__(self, error: BaseException) -> None:
        self._error = error

    async def __aenter__(self) -> _FailingSession:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def execute(self, *args: object, **kwargs: object) -> object:
        raise self._error


def _store_failing_with(error: BaseException) -> SqlKnowledgeStore:
    def factory() -> _FailingSession:
        return _FailingSession(error)

    return SqlKnowledgeStore(cast(async_sessionmaker[AsyncSession], factory))


async def _search(store: SqlKnowledgeStore) -> None:
    await store.keyword_search_scored(tenant_id=uuid4(), kb_ids=[uuid4()], query="苹果", limit=5)


async def test_a_statement_timeout_becomes_a_recall_error_keeping_the_driver_text() -> None:
    """A real asyncpg timeout reaches SQLAlchemy as a plain ``DBAPIError`` whose
    driver error carries SQLSTATE 57014."""
    original = DBAPIError(
        "SELECT ...", {}, _DriverError("canceling statement due to statement timeout", "57014")
    )
    with pytest.raises(KnowledgeRecallError) as caught:
        await _search(_store_failing_with(original))
    assert caught.value.__cause__ is original
    # The error classifier keys on this text to call it transient.
    assert "statement timeout" in str(caught.value)


@pytest.mark.parametrize(
    "error",
    [
        DBAPIError("q", {}, _DriverError("lock not available", "55P03")),
        DBAPIError("q", {}, _DriverError("could not serialize access", "40001")),
        DBAPIError("q", {}, _DriverError("deadlock detected", "40P01")),
        DBAPIError("q", {}, _DriverError("terminating connection", "57P01")),
        DBAPIError("q", {}, _DriverError("terminating connection", "57P02")),
        DBAPIError("q", {}, _DriverError("cannot connect now", "57P03")),
        DBAPIError("q", {}, _DriverError("connection failure", "08006")),
        DBAPIError("q", {}, _DriverError("too many connections", "53300")),
        DBAPIError("q", {}, _DriverError("out of memory", "53200")),
        DBAPIError("q", {}, _DriverError("broken pipe", None), connection_invalidated=True),
        InterfaceError("q", {}, _DriverError("connection is closed", None)),
        OperationalError("q", {}, _DriverError("server closed the connection", None)),
    ],
    ids=[
        "lock-not-available",
        "serialization",
        "deadlock",
        "admin-shutdown",
        "crash-shutdown",
        "cannot-connect-now",
        "connection-class-08",
        "resources-class-53-connections",
        "resources-class-53-memory",
        "connection-invalidated",
        "interface-error-no-sqlstate",
        "operational-error-no-sqlstate",
    ],
)
async def test_failures_to_finish_the_query_become_recall_errors(error: DBAPIError) -> None:
    with pytest.raises(KnowledgeRecallError) as caught:
        await _search(_store_failing_with(error))
    assert caught.value.__cause__ is error


@pytest.mark.parametrize(
    "error",
    [
        ProgrammingError(
            "q", {}, _DriverError("function no_such_function does not exist", "42883")
        ),
        ProgrammingError("q", {}, _DriverError("permission denied for table", "42501")),
        ProgrammingError("q", {}, _DriverError('column "x" does not exist', "42703")),
        DBAPIError("q", {}, _DriverError("invalid input syntax", "22P02")),
        DBAPIError("q", {}, _DriverError("unknown driver failure", None)),
    ],
    ids=["undefined-function", "permission", "undefined-column", "bad-data", "no-sqlstate"],
)
async def test_other_database_errors_propagate_unchanged(error: DBAPIError) -> None:
    """A bug or a missing permission fails every search the same way; vector-only
    results flagged "degraded" would hide it."""
    with pytest.raises(DBAPIError) as caught:
        await _search(_store_failing_with(error))
    assert caught.value is error


async def test_an_error_that_is_not_the_databases_still_propagates() -> None:
    with pytest.raises(TypeError):
        await _search(_store_failing_with(TypeError("bad argument")))
