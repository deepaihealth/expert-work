"""Integration tests for SqlUserUploadStore against a real Postgres.

附件模型统一(spec 2026-08-17) —— the SQL and in-memory backends' predicates
must be byte-identical: ``get`` filters only ``(id, tenant_id)``,
``delete_all_for_user`` filters only ``(tenant_id, user_id)``.
"""

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
    SqlUserUploadStore,
    create_async_engine_from_config,
    create_async_session_factory,
)

pytestmark = pytest.mark.integration

ALEMBIC_INI = Path(__file__).resolve().parent.parent / "alembic.ini"

SqlStoreFixture = tuple[SqlUserUploadStore, AsyncEngine]


def _sync_dsn(container: PostgresContainer) -> str:
    url = str(container.get_connection_url())
    return url.replace("+psycopg2", "+psycopg").replace("postgresql://", "postgresql+psycopg://", 1)


def _async_dsn(container: PostgresContainer) -> str:
    url = str(container.get_connection_url())
    return url.replace("+psycopg2", "+asyncpg").replace("postgresql://", "postgresql+asyncpg://", 1)


@pytest.fixture
def sql_store(postgres_container: PostgresContainer) -> Iterator[SqlStoreFixture]:
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", _sync_dsn(postgres_container))
    command.upgrade(cfg, "head")

    engine = create_async_engine_from_config(DatabaseConfig(dsn=_async_dsn(postgres_container)))
    session_factory = create_async_session_factory(engine)
    yield SqlUserUploadStore(session_factory), engine


@pytest.mark.asyncio
async def test_insert_then_get_same_tenant(sql_store: SqlStoreFixture) -> None:
    store, engine = sql_store
    try:
        tenant = uuid4()
        upload_id = uuid4()
        user = uuid4()
        thread = uuid4()
        row = await store.insert(
            upload_id=upload_id,
            tenant_id=tenant,
            user_id=user,
            thread_id=thread,
            kind="document",
            ref="uploads/report.pdf",
            mime_type="application/pdf",
            size_bytes=10,
            filename="report.pdf",
        )
        assert row.id == upload_id
        assert row.kind == "document"
        assert row.deleted_at is None
        fetched = await store.get(upload_id=upload_id, tenant_id=tenant)
        assert fetched == row
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_get_other_tenant_is_none(sql_store: SqlStoreFixture) -> None:
    store, engine = sql_store
    try:
        upload_id = uuid4()
        await store.insert(
            upload_id=upload_id,
            tenant_id=uuid4(),
            user_id=uuid4(),
            thread_id=uuid4(),
            kind="image",
            ref="expert_work://image/x/y/z.png",
            mime_type="image/png",
            size_bytes=1,
            filename="z.png",
        )
        assert await store.get(upload_id=upload_id, tenant_id=uuid4()) is None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_get_unknown_is_none(sql_store: SqlStoreFixture) -> None:
    store, engine = sql_store
    try:
        assert await store.get(upload_id=uuid4(), tenant_id=uuid4()) is None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_get_does_not_filter_user(sql_store: SqlStoreFixture) -> None:
    """``get`` returns the row regardless of ``user_id`` — the caller
    compares it itself (same rule as ``image_upload.get``)."""
    store, engine = sql_store
    try:
        tenant = uuid4()
        upload_id = uuid4()
        row = await store.insert(
            upload_id=upload_id,
            tenant_id=tenant,
            user_id=uuid4(),
            thread_id=uuid4(),
            kind="document",
            ref="uploads/other.pdf",
            mime_type="application/pdf",
            size_bytes=1,
            filename="other.pdf",
        )
        fetched = await store.get(upload_id=upload_id, tenant_id=tenant)
        assert fetched == row
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_delete_all_for_user_counts_and_scopes(sql_store: SqlStoreFixture) -> None:
    store, engine = sql_store
    try:
        tenant = uuid4()
        other_tenant = uuid4()
        user1 = uuid4()
        user2 = uuid4()

        async def _seed(*, tenant_id: UUID, user_id: UUID) -> UUID:
            uid = uuid4()
            await store.insert(
                upload_id=uid,
                tenant_id=tenant_id,
                user_id=user_id,
                thread_id=uuid4(),
                kind="document",
                ref="uploads/f.pdf",
                mime_type="application/pdf",
                size_bytes=1,
                filename="f.pdf",
            )
            return uid

        await _seed(tenant_id=tenant, user_id=user1)
        await _seed(tenant_id=tenant, user_id=user1)
        user2_id_a = await _seed(tenant_id=tenant, user_id=user2)
        user2_id_b = await _seed(tenant_id=tenant, user_id=user2)
        other_tenant_id = await _seed(tenant_id=other_tenant, user_id=user1)

        deleted = await store.delete_all_for_user(tenant_id=tenant, user_id=user1)
        assert deleted == 2

        assert await store.get(upload_id=user2_id_a, tenant_id=tenant) is not None
        assert await store.get(upload_id=user2_id_b, tenant_id=tenant) is not None
        assert await store.get(upload_id=other_tenant_id, tenant_id=other_tenant) is not None
        # Re-deleting is a safe no-op.
        assert await store.delete_all_for_user(tenant_id=tenant, user_id=user1) == 0
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_migration_downgrade_then_upgrade(postgres_container: PostgresContainer) -> None:
    """``downgrade -1`` then ``upgrade head`` round-trips without error."""
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", _sync_dsn(postgres_container))
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "-1")
    command.upgrade(cfg, "head")
