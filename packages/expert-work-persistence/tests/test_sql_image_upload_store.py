"""Integration tests for SqlImageUploadStore against a real Postgres.

Mini-ADR J-32 (删除接口卫生修复 PR1 / Task 3) — ``list_reapable`` predicate
must be byte-identical to :class:`InMemoryImageUploadStore`'s.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import insert, text
from sqlalchemy.ext.asyncio import AsyncEngine
from testcontainers.postgres import PostgresContainer

from expert_work.persistence import (
    DatabaseConfig,
    SqlImageUploadStore,
    create_async_engine_from_config,
    create_async_session_factory,
)
from expert_work.persistence.models import ImageUploadRow

pytestmark = pytest.mark.integration

ALEMBIC_INI = Path(__file__).resolve().parent.parent / "alembic.ini"

SqlStoreFixture = tuple[SqlImageUploadStore, AsyncEngine]


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
    yield SqlImageUploadStore(session_factory), engine


@pytest.mark.asyncio
async def test_list_reapable_predicate_covers_old_and_soft_deleted(
    sql_store: SqlStoreFixture,
) -> None:
    """``list_reapable`` matches ``(created_at < before) OR (deleted_at IS
    NOT NULL)`` — old rows are swept regardless of delete state, and a
    freshly soft-deleted row is swept even though it's too young to be
    caught by the age predicate alone."""
    store, engine = sql_store
    try:
        tenant_id = uuid4()
        now = datetime.now(UTC)
        cutoff = now - timedelta(days=90)
        old_time = now - timedelta(days=200)

        rows = {}
        for key in ("new_active", "old_active", "new_soft_deleted", "old_soft_deleted"):
            image_id = uuid4()
            rows[key] = image_id
            await store.insert(
                image_id=image_id,
                tenant_id=tenant_id,
                thread_id=uuid4(),
                user_id=None,
                object_key=f"tenants/x/uploads/{key}.png",
                size_bytes=1,
                mime_type="image/png",
                sha256="x",
            )

        async with engine.begin() as conn:
            for key in ("old_active", "old_soft_deleted"):
                await conn.execute(
                    text("UPDATE image_upload SET created_at = :ts WHERE id = :id"),
                    {"ts": old_time, "id": rows[key]},
                )
            for key in ("new_soft_deleted", "old_soft_deleted"):
                await conn.execute(
                    text("UPDATE image_upload SET deleted_at = :ts WHERE id = :id"),
                    {"ts": now, "id": rows[key]},
                )

        reapable = await store.list_reapable(before=cutoff)

        reapable_ids = {r.id for r in reapable}
        assert reapable_ids == {
            rows["old_active"],
            rows["new_soft_deleted"],
            rows["old_soft_deleted"],
        }
        assert rows["new_active"] not in reapable_ids
        # created_at ascending.
        assert [r.created_at for r in reapable] == sorted(r.created_at for r in reapable)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_list_reapable_respects_limit(sql_store: SqlStoreFixture) -> None:
    store, engine = sql_store
    try:
        tenant_id = uuid4()
        old_time = datetime.now(UTC) - timedelta(days=200)
        image_ids = [uuid4() for _ in range(3)]
        for image_id in image_ids:
            await store.insert(
                image_id=image_id,
                tenant_id=tenant_id,
                thread_id=uuid4(),
                user_id=None,
                object_key=f"tenants/x/uploads/{image_id}.png",
                size_bytes=1,
                mime_type="image/png",
                sha256="x",
            )
        async with engine.begin() as conn:
            await conn.execute(
                text("UPDATE image_upload SET created_at = :ts WHERE id = ANY(:ids)"),
                {"ts": old_time, "ids": image_ids},
            )

        cutoff = datetime.now(UTC) - timedelta(days=90)
        reapable = await store.list_reapable(before=cutoff, limit=2)
        assert len(reapable) == 2
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_list_for_user_includes_soft_deleted_rows_and_scopes_by_tenant_and_user(
    sql_store: SqlStoreFixture,
) -> None:
    """Deletion-hygiene purge (Task 8) — ``list_for_user`` must return
    soft-deleted rows too (purge needs their ``object_key``), and its
    predicate must be byte-identical to the in-memory implementation's:
    tenant- and user-scoped, no ``deleted_at`` filter."""
    store, engine = sql_store
    try:
        tenant_id = uuid4()
        user_id = uuid4()
        active_id = uuid4()
        soft_deleted_id = uuid4()
        other_user_id = uuid4()
        other_tenant_id = uuid4()

        await store.insert(
            image_id=active_id,
            tenant_id=tenant_id,
            thread_id=uuid4(),
            user_id=user_id,
            object_key="tenants/x/uploads/active.png",
            size_bytes=1,
            mime_type="image/png",
            sha256="x",
        )
        await store.insert(
            image_id=soft_deleted_id,
            tenant_id=tenant_id,
            thread_id=uuid4(),
            user_id=user_id,
            object_key="tenants/x/uploads/soft-deleted.png",
            size_bytes=1,
            mime_type="image/png",
            sha256="x",
        )
        await store.soft_delete(
            image_id=soft_deleted_id, tenant_id=tenant_id, now=datetime.now(UTC)
        )
        # Another user in the same tenant — must not appear.
        await store.insert(
            image_id=other_user_id,
            tenant_id=tenant_id,
            thread_id=uuid4(),
            user_id=uuid4(),
            object_key="tenants/x/uploads/other-user.png",
            size_bytes=1,
            mime_type="image/png",
            sha256="x",
        )
        # Same user id, different tenant — must not appear.
        await store.insert(
            image_id=other_tenant_id,
            tenant_id=uuid4(),
            thread_id=uuid4(),
            user_id=user_id,
            object_key="tenants/x/uploads/other-tenant.png",
            size_bytes=1,
            mime_type="image/png",
            sha256="x",
        )

        rows = await store.list_for_user(tenant_id=tenant_id, user_id=user_id)
        assert {r.id for r in rows} == {active_id, soft_deleted_id}
        # created_at ascending.
        assert [r.created_at for r in rows] == sorted(r.created_at for r in rows)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_hard_delete_takes_more_ids_than_one_statement_can_bind(
    sql_store: SqlStoreFixture,
) -> None:
    """B-280 —— an agent purge passes every image of the agent to ``hard_delete``;
    asyncpg refuses a statement with more than 32,767 bind parameters, so the
    ids go in bounded ``IN`` lists. Only the given ids go."""
    store, engine = sql_store
    try:
        tenant, thread = uuid4(), uuid4()
        many = 33_000
        ids = [uuid4() for _ in range(many)]
        async with engine.begin() as conn:
            for start in range(0, many, 5_000):
                await conn.execute(
                    insert(ImageUploadRow),
                    [
                        {
                            "id": image_id,
                            "tenant_id": tenant,
                            "user_id": None,
                            "thread_id": thread,
                            "object_key": f"{tenant}/uploads/{thread}/{image_id}.png",
                            "size_bytes": 1,
                            "mime_type": "image/png",
                            "sha256": "x",
                        }
                        for image_id in ids[start : start + 5_000]
                    ],
                )
        keep = uuid4()
        await store.insert(
            image_id=keep,
            tenant_id=tenant,
            thread_id=thread,
            user_id=None,
            object_key=f"{tenant}/uploads/{thread}/{keep}.png",
            size_bytes=1,
            mime_type="image/png",
            sha256="x",
        )

        assert await store.hard_delete(image_ids=ids) == many
        left = await store.list_for_threads(tenant_id=tenant, thread_ids=[thread])
        assert [row.id for row in left] == [keep]
    finally:
        await engine.dispose()
