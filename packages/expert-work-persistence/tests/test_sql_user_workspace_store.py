"""Integration tests for SqlUserWorkspaceStore against a real Postgres — J.15."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.ext.asyncio import AsyncEngine
from testcontainers.postgres import PostgresContainer

from expert_work.persistence import (
    DatabaseConfig,
    SqlUserWorkspaceStore,
    create_async_engine_from_config,
    create_async_session_factory,
    workspace_volume_name,
)

pytestmark = pytest.mark.integration

ALEMBIC_INI = Path(__file__).resolve().parent.parent / "alembic.ini"

SqlStoreFixture = tuple[SqlUserWorkspaceStore, AsyncEngine]


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
    yield SqlUserWorkspaceStore(session_factory), engine


@pytest.mark.asyncio
async def test_resolve_upsert_round_trip(sql_store: SqlStoreFixture) -> None:
    store, engine = sql_store
    try:
        tenant_id, user_id = uuid4(), uuid4()
        first = await store.resolve(tenant_id=tenant_id, user_id=user_id)
        assert first.tenant_id == tenant_id
        assert first.user_id == user_id
        assert first.volume_name == workspace_volume_name(tenant_id, user_id)
        assert first.size_bytes == 0

        again = await store.resolve(tenant_id=tenant_id, user_id=user_id)
        # ON CONFLICT path: same (tenant, user) → same surrogate id.
        assert again.id == first.id
        assert again.created_at == first.created_at
        assert again.last_accessed_at is not None
        assert first.last_accessed_at is not None
        assert again.last_accessed_at >= first.last_accessed_at
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_get_is_read_only(sql_store: SqlStoreFixture) -> None:
    store, engine = sql_store
    try:
        tenant_id, user_id = uuid4(), uuid4()
        # Read-only get on an absent (tenant, user) returns None without creating.
        assert await store.get(tenant_id=tenant_id, user_id=user_id) is None
        assert await store.get(tenant_id=tenant_id, user_id=user_id) is None

        created = await store.resolve(tenant_id=tenant_id, user_id=user_id)
        fetched = await store.get(tenant_id=tenant_id, user_id=user_id)
        assert fetched is not None
        assert fetched.id == created.id
        assert fetched.volume_name == created.volume_name
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_resolve_distinguishes_tenant_and_user(sql_store: SqlStoreFixture) -> None:
    store, engine = sql_store
    try:
        tenant_a, tenant_b = uuid4(), uuid4()
        user_x, user_y = uuid4(), uuid4()

        w1 = await store.resolve(tenant_id=tenant_a, user_id=user_x)
        w2 = await store.resolve(tenant_id=tenant_a, user_id=user_y)
        w3 = await store.resolve(tenant_id=tenant_b, user_id=user_x)

        assert len({w1.id, w2.id, w3.id}) == 3
        assert len({w1.volume_name, w2.volume_name, w3.volume_name}) == 3
    finally:
        await engine.dispose()


# --- add_size (沙箱迁移波 3 § 3.2 记账第 1 层) -------------------------------


@pytest.mark.asyncio
async def test_add_size_accumulates(sql_store: SqlStoreFixture) -> None:
    store, engine = sql_store
    try:
        tenant_id, user_id = uuid4(), uuid4()
        workspace = await store.resolve(tenant_id=tenant_id, user_id=user_id)

        await store.add_size(workspace_id=workspace.id, delta_bytes=100)
        await store.add_size(workspace_id=workspace.id, delta_bytes=50)

        got = await store.get(tenant_id=tenant_id, user_id=user_id)
        assert got is not None and got.size_bytes == 150
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_add_size_floors_at_zero(sql_store: SqlStoreFixture) -> None:
    store, engine = sql_store
    try:
        tenant_id, user_id = uuid4(), uuid4()
        workspace = await store.resolve(tenant_id=tenant_id, user_id=user_id)

        await store.add_size(workspace_id=workspace.id, delta_bytes=-999)

        got = await store.get(tenant_id=tenant_id, user_id=user_id)
        assert got is not None and got.size_bytes == 0
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_add_size_concurrent_increments_are_atomic(sql_store: SqlStoreFixture) -> None:
    store, engine = sql_store
    try:
        tenant_id, user_id = uuid4(), uuid4()
        workspace = await store.resolve(tenant_id=tenant_id, user_id=user_id)

        await asyncio.gather(
            *[store.add_size(workspace_id=workspace.id, delta_bytes=10) for _ in range(20)]
        )

        got = await store.get(tenant_id=tenant_id, user_id=user_id)
        assert got is not None and got.size_bytes == 200
    finally:
        await engine.dispose()


# --- Phase 3b 地基 (list_archived_expired + hard_delete) -------------------


@pytest.mark.asyncio
async def test_list_archived_expired_filters_correctly(sql_store: SqlStoreFixture) -> None:
    store, engine = sql_store
    try:
        tenant = uuid4()
        now = datetime.now(UTC)
        before = now - timedelta(days=90)

        active = await store.resolve(tenant_id=tenant, user_id=uuid4())

        hit = await store.resolve(tenant_id=tenant, user_id=uuid4())
        await store.soft_delete(workspace_id=hit.id, now=now - timedelta(days=100))
        await store.mark_archived(workspace_id=hit.id, archived_object_key="k-hit")

        stuck = await store.resolve(tenant_id=tenant, user_id=uuid4())
        await store.soft_delete(workspace_id=stuck.id, now=now - timedelta(days=100))
        # Archive job hasn't run yet — archived_object_key still None.

        recent = await store.resolve(tenant_id=tenant, user_id=uuid4())
        await store.soft_delete(workspace_id=recent.id, now=now - timedelta(days=10))
        await store.mark_archived(workspace_id=recent.id, archived_object_key="k-recent")

        rows = await store.list_archived_expired(before=before)
        ids = {r.id for r in rows}

        # postgres_container is session-scoped (shared across every test in
        # the file/session) and list_archived_expired has no tenant filter
        # (matches list_pending_archive/list_active precedent) — so assert
        # membership, not exact-set equality, to stay robust against rows
        # other tests leave behind.
        assert hit.id in ids
        assert active.id not in ids
        assert stuck.id not in ids
        assert recent.id not in ids
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_list_archived_expired_orders_by_deleted_at_ascending(
    sql_store: SqlStoreFixture,
) -> None:
    store, engine = sql_store
    try:
        tenant = uuid4()
        now = datetime.now(UTC)

        newer = await store.resolve(tenant_id=tenant, user_id=uuid4())
        await store.soft_delete(workspace_id=newer.id, now=now - timedelta(days=100))
        await store.mark_archived(workspace_id=newer.id, archived_object_key="k1")

        older = await store.resolve(tenant_id=tenant, user_id=uuid4())
        await store.soft_delete(workspace_id=older.id, now=now - timedelta(days=200))
        await store.mark_archived(workspace_id=older.id, archived_object_key="k2")

        rows = await store.list_archived_expired(before=now - timedelta(days=90))
        # Filter down to this test's own rows before checking relative
        # order — the shared session container may hold rows from other
        # tests, but the global ordering they don't disturb is theirs.
        mine = [r.id for r in rows if r.id in {older.id, newer.id}]
        assert mine == [older.id, newer.id]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_hard_delete_removes_row_and_is_idempotent(sql_store: SqlStoreFixture) -> None:
    store, engine = sql_store
    try:
        workspace = await store.resolve(tenant_id=uuid4(), user_id=uuid4())

        assert await store.hard_delete(workspace_id=workspace.id) is True
        assert await store.get(tenant_id=workspace.tenant_id, user_id=workspace.user_id) is None

        # Row is truly gone — a second delete is a no-op, not an error.
        assert await store.hard_delete(workspace_id=workspace.id) is False
    finally:
        await engine.dispose()
