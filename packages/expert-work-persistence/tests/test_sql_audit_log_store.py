"""Integration tests for :class:`SqlAuditLogStore` against a real Postgres."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.ext.asyncio import AsyncEngine
from testcontainers.postgres import PostgresContainer

from expert_work.persistence import (
    DatabaseConfig,
    SqlAuditLogStore,
    create_async_engine_from_config,
    create_async_session_factory,
)
from expert_work.protocol import AuditAction, AuditEntry, AuditQuery, AuditResult

pytestmark = pytest.mark.integration

ALEMBIC_INI = Path(__file__).resolve().parent.parent / "alembic.ini"

SqlStoreFixture = tuple[SqlAuditLogStore, AsyncEngine]


def _sync_dsn(container: PostgresContainer) -> str:
    url = str(container.get_connection_url())
    return url.replace("+psycopg2", "+psycopg").replace("postgresql://", "postgresql+psycopg://", 1)


def _async_dsn(container: PostgresContainer) -> str:
    url = str(container.get_connection_url())
    return url.replace("+psycopg2", "+asyncpg").replace("postgresql://", "postgresql+asyncpg://", 1)


def _entry(
    tenant_id: UUID,
    *,
    actor_id: str = "alice",
    action: AuditAction = AuditAction.MANIFEST_WRITE,
    resource_type: str = "manifest",
    resource_id: str | None = "demo@1",
    result: AuditResult = AuditResult.SUCCESS,
) -> AuditEntry:
    return AuditEntry(
        tenant_id=tenant_id,
        actor_type="user",
        actor_id=actor_id,
        action=action,
        resource_type=resource_type,  # type: ignore[arg-type]
        resource_id=resource_id,
        result=result,
        details={"k": "v"},
    )


@pytest.fixture
def sql_store(postgres_container: PostgresContainer) -> Iterator[SqlStoreFixture]:
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", _sync_dsn(postgres_container))
    command.upgrade(cfg, "head")

    engine = create_async_engine_from_config(DatabaseConfig(dsn=_async_dsn(postgres_container)))
    session_factory = create_async_session_factory(engine)
    yield SqlAuditLogStore(session_factory), engine


@pytest.mark.asyncio
async def test_append_round_trip(sql_store: SqlStoreFixture) -> None:
    store, engine = sql_store
    try:
        tenant_id = uuid4()
        written = await store.append(_entry(tenant_id))

        assert written.id is not None
        assert written.occurred_at is not None
        assert written.action == AuditAction.MANIFEST_WRITE
        assert written.details == {"k": "v"}

        fetched = await store.get_by_id(written.id, tenant_id=tenant_id)
        assert fetched is not None
        assert fetched.id == written.id
        assert fetched.details == {"k": "v"}
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_get_by_id_tenant_isolated(sql_store: SqlStoreFixture) -> None:
    store, engine = sql_store
    try:
        owner, other = uuid4(), uuid4()
        written = await store.append(_entry(owner))

        assert await store.get_by_id(written.id or 0, tenant_id=owner) is not None
        assert await store.get_by_id(written.id or 0, tenant_id=other) is None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_query_filters_and_ordering(sql_store: SqlStoreFixture) -> None:
    store, engine = sql_store
    try:
        tenant = uuid4()
        for action in (
            AuditAction.AUTH_LOGIN,
            AuditAction.MANIFEST_WRITE,
            AuditAction.AUTH_LOGIN,
        ):
            await store.append(_entry(tenant, action=action))

        logins = await store.query(AuditQuery(tenant_id=tenant, action=AuditAction.AUTH_LOGIN))
        assert [e.action for e in logins.entries] == [
            AuditAction.AUTH_LOGIN,
            AuditAction.AUTH_LOGIN,
        ]
        # Newest first within the filtered set.
        assert logins.entries[0].id is not None
        assert logins.entries[1].id is not None
        assert logins.entries[0].id > logins.entries[1].id
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_query_pagination_via_cursor(sql_store: SqlStoreFixture) -> None:
    store, engine = sql_store
    try:
        tenant = uuid4()
        for _ in range(5):
            await store.append(_entry(tenant))

        page_one = await store.query(AuditQuery(tenant_id=tenant, limit=2))
        assert len(page_one.entries) == 2
        assert page_one.next_cursor is not None

        page_two = await store.query(
            AuditQuery(tenant_id=tenant, limit=2, cursor=page_one.next_cursor)
        )
        assert len(page_two.entries) == 2
        assert page_two.next_cursor is not None

        page_three = await store.query(
            AuditQuery(tenant_id=tenant, limit=2, cursor=page_two.next_cursor)
        )
        assert len(page_three.entries) == 1
        assert page_three.next_cursor is None

        all_ids = {e.id for p in (page_one, page_two, page_three) for e in p.entries}
        assert len(all_ids) == 5
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_query_wildcard_tenant_returns_all(sql_store: SqlStoreFixture) -> None:
    store, engine = sql_store
    try:
        tenant_a, tenant_b = uuid4(), uuid4()
        await store.append(_entry(tenant_a))
        await store.append(_entry(tenant_b))

        all_rows = await store.query(AuditQuery(tenant_id="*"))
        assert {e.tenant_id for e in all_rows.entries} >= {tenant_a, tenant_b}
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_document_uploads_for_thread_is_targeted(sql_store: SqlStoreFixture) -> None:
    """B-262 P3 review 3 —— 与内存实现同一谓词(``details->>'thread_id'``)。"""
    from datetime import UTC, datetime, timedelta

    store, engine = sql_store
    try:
        tenant, thread = uuid4(), uuid4()

        async def _put(
            *,
            tenant_id: UUID = tenant,
            thread_id: UUID = thread,
            action: AuditAction = AuditAction.DOCUMENT_UPLOAD,
        ) -> AuditEntry:
            entry = _entry(tenant_id, action=action, resource_type="user_workspace")
            return await store.append(
                entry.model_copy(update={"details": {"thread_id": str(thread_id)}})
            )

        first = await _put()
        second = await _put()
        await _put(thread_id=uuid4())
        await _put(tenant_id=uuid4())
        await _put(action=AuditAction.MANIFEST_WRITE)

        got = await store.list_document_uploads_for_thread(tenant_id=tenant, thread_id=thread)
        assert [e.id for e in got] == [first.id, second.id]
        assert (
            await store.list_document_uploads_for_thread(
                tenant_id=tenant, thread_id=thread, since=datetime.now(UTC) + timedelta(minutes=1)
            )
            == []
        )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_document_uploads_for_threads_is_one_batched_window_read(
    sql_store: SqlStoreFixture,
) -> None:
    """B-271 —— 与内存实现同一谓词:本租户、``document:upload``、成功、``details->>'thread_id'``
    在这批会话里、``occurred_at`` 在 ``[since, until]``。"""
    from datetime import UTC, datetime, timedelta

    store, engine = sql_store
    try:
        tenant, first_thread, second_thread = uuid4(), uuid4(), uuid4()

        async def _put(
            thread_id: UUID,
            *,
            tenant_id: UUID = tenant,
            action: AuditAction = AuditAction.DOCUMENT_UPLOAD,
            result: AuditResult = AuditResult.SUCCESS,
        ) -> AuditEntry:
            entry = _entry(tenant_id, action=action, resource_type="user_workspace", result=result)
            return await store.append(
                entry.model_copy(update={"details": {"thread_id": str(thread_id)}})
            )

        a = await _put(first_thread)
        b = await _put(second_thread)
        await _put(uuid4())
        await _put(first_thread, tenant_id=uuid4())
        await _put(first_thread, action=AuditAction.MANIFEST_WRITE)
        await _put(first_thread, result=AuditResult.DENIED)
        now = datetime.now(UTC)

        got = await store.list_document_uploads_for_threads(
            tenant_id=tenant,
            thread_ids=[first_thread, second_thread],
            since=now - timedelta(minutes=5),
            until=now + timedelta(minutes=5),
        )
        assert [e.id for e in got] == [a.id, b.id]
        for since, until in (
            (now + timedelta(minutes=5), now + timedelta(minutes=10)),
            (now - timedelta(minutes=10), now - timedelta(minutes=5)),
        ):
            assert (
                await store.list_document_uploads_for_threads(
                    tenant_id=tenant, thread_ids=[first_thread], since=since, until=until
                )
                == []
            )
        assert (
            await store.list_document_uploads_for_threads(
                tenant_id=tenant, thread_ids=[], since=now - timedelta(minutes=5), until=now
            )
            == []
        )
    finally:
        await engine.dispose()
