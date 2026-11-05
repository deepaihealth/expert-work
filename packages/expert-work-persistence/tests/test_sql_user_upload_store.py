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


@pytest.mark.asyncio
async def test_list_expired_and_soft_delete(sql_store: SqlStoreFixture) -> None:
    """留存链 —— 上传 90 天:list_expired 跨租户取到期的活行(没有 run、没用过 → 按
    created_at);soft_delete 只翻活着的行,tenant 不对不翻。"""
    from datetime import UTC, datetime

    from sqlalchemy import text

    store, engine = sql_store
    try:
        tenant, other = uuid4(), uuid4()

        async def _seed(*, tenant_id: UUID, age_days: int) -> UUID:
            uid = uuid4()
            await store.insert(
                upload_id=uid,
                tenant_id=tenant_id,
                user_id=uuid4(),
                thread_id=uuid4(),
                kind="document",
                ref="uploads/f.pdf",
                mime_type="application/pdf",
                size_bytes=1,
                filename="f.pdf",
            )
            async with engine.begin() as conn:
                await conn.execute(
                    text(
                        "UPDATE user_upload SET created_at = now() - make_interval(days => :d) "
                        "WHERE id = :i"
                    ),
                    {"d": age_days, "i": uid},
                )
            return uid

        old = await _seed(tenant_id=tenant, age_days=120)
        older = await _seed(tenant_id=other, age_days=200)
        fresh = await _seed(tenant_id=tenant, age_days=1)
        now = datetime.now(UTC)

        expired = await store.list_expired(now=now, default_days=90)
        assert [e.upload.id for e in expired if e.upload.id in {old, older, fresh}] == [older, old]

        assert await store.soft_delete(upload_id=old, tenant_id=other, now=now) is False
        assert await store.soft_delete(upload_id=old, tenant_id=tenant, now=now) is True
        assert await store.soft_delete(upload_id=old, tenant_id=tenant, now=now) is False
        row = await store.get(upload_id=old, tenant_id=tenant)
        assert row is not None and row.deleted_at is not None
        assert old not in {e.upload.id for e in await store.list_expired(now=now, default_days=90)}
        fresh_row = await store.get(upload_id=fresh, tenant_id=tenant)
        assert fresh_row is not None and fresh_row.deleted_at is None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_list_documents_for_thread(sql_store: SqlStoreFixture) -> None:
    """B-262 P3 —— 与内存实现同一谓词:本租户、本会话、文档类、没删的,按上传先后。"""
    from datetime import UTC, datetime

    store, engine = sql_store
    try:
        tenant, thread, user = uuid4(), uuid4(), uuid4()

        async def _put(
            *, thread_id: UUID, tenant_id: UUID = tenant, kind: str = "document"
        ) -> UUID:
            uid = uuid4()
            await store.insert(
                upload_id=uid,
                tenant_id=tenant_id,
                user_id=user,
                thread_id=thread_id,
                kind=kind,  # type: ignore[arg-type]
                ref="uploads/a.pdf",
                mime_type="application/pdf",
                size_bytes=3,
                filename="a.pdf",
            )
            return uid

        first = await _put(thread_id=thread)
        second = await _put(thread_id=thread)
        await _put(thread_id=thread, kind="image")
        await _put(thread_id=uuid4())
        await _put(thread_id=thread, tenant_id=uuid4())
        gone = await _put(thread_id=thread)
        await store.soft_delete(upload_id=gone, tenant_id=tenant, now=datetime.now(UTC))

        rows = await store.list_documents_for_thread(tenant_id=tenant, thread_id=thread)
        assert [r.id for r in rows] == [first, second]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_sha256_round_trips_and_list_unexpired_by_ref(sql_store: SqlStoreFixture) -> None:
    """B-271 —— 与内存实现同一谓词:``sha256`` 落库读回(老调用点 ``None``);
    ``list_unexpired_by_ref`` 只认同租户、同用户、同 ``ref``、没删、没到期的行。"""
    from datetime import UTC, datetime

    from sqlalchemy import text

    store, engine = sql_store
    try:
        tenant, user = uuid4(), uuid4()

        async def _put(
            *,
            tenant_id: UUID = tenant,
            user_id: UUID = user,
            ref: str = "uploads/a.pdf",
            sha256: str | None = None,
        ) -> UUID:
            uid = uuid4()
            await store.insert(
                upload_id=uid,
                tenant_id=tenant_id,
                user_id=user_id,
                thread_id=uuid4(),
                kind="document",
                ref=ref,
                mime_type="application/pdf",
                size_bytes=3,
                filename="a.pdf",
                sha256=sha256,
            )
            return uid

        first = await _put(sha256="cd" * 32)
        second = await _put()
        await _put(ref="uploads/b.pdf")
        await _put(user_id=uuid4())
        await _put(tenant_id=uuid4())
        gone = await _put()
        await store.soft_delete(upload_id=gone, tenant_id=tenant, now=datetime.now(UTC))

        hashed = await store.get(upload_id=first, tenant_id=tenant)
        legacy = await store.get(upload_id=second, tenant_id=tenant)
        assert hashed is not None and hashed.sha256 == "cd" * 32
        assert legacy is not None and legacy.sha256 is None
        stale = await _put()
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE user_upload SET created_at = now() - interval '100 days' WHERE id = :i"
                ),
                {"i": stale},
            )
        rows = await store.list_unexpired_by_ref(
            tenant_id=tenant,
            user_id=user,
            ref="uploads/a.pdf",
            now=datetime.now(UTC),
            default_days=90,
        )
        assert sorted(r.id for r in rows) == sorted([first, second])
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# B-63 —— 按最后使用到期:created_at / last_used_at / 会话最后一次 run 三者最晚 + 租户保留期
# ---------------------------------------------------------------------------


class _LastUse:
    """在真 Postgres 上造「很久以前上传」的行,再按需补一次 run、一次使用、一条租户配置。"""

    def __init__(self, store: SqlUserUploadStore, engine: AsyncEngine) -> None:
        self.store = store
        self.engine = engine

    async def upload(self, *, tenant_id: UUID, age_days: int, user_id: UUID | None = None) -> UUID:
        from sqlalchemy import text

        uid = uuid4()
        await self.store.insert(
            upload_id=uid,
            tenant_id=tenant_id,
            user_id=user_id or uuid4(),
            thread_id=uuid4(),
            kind="document",
            ref="uploads/last-use.pdf",
            mime_type="application/pdf",
            size_bytes=1,
            filename="last-use.pdf",
        )
        async with self.engine.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE user_upload SET created_at = now() - make_interval(days => :d) "
                    "WHERE id = :i"
                ),
                {"d": age_days, "i": uid},
            )
        return uid

    async def run_in_thread_of(self, upload_id: UUID, *, age_days: int) -> None:
        from sqlalchemy import text

        async with self.engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO agent_run (id, tenant_id, thread_id, status, on_disconnect, "
                    " created_at, updated_at) "
                    "SELECT :r, tenant_id, thread_id, 'success', 'continue', "
                    " now() - make_interval(days => :d), now() - make_interval(days => :d) "
                    "FROM user_upload WHERE id = :i"
                ),
                {"r": uuid4(), "d": age_days, "i": upload_id},
            )

    async def used(self, upload_id: UUID, *, age_days: int) -> None:
        from sqlalchemy import text

        async with self.engine.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE user_upload SET last_used_at = now() - make_interval(days => :d) "
                    "WHERE id = :i"
                ),
                {"d": age_days, "i": upload_id},
            )

    async def tenant_days(self, tenant_id: UUID, days: int | None) -> None:
        from sqlalchemy import text

        async with self.engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO tenant_config (tenant_id, display_name, updated_by, "
                    " upload_retention_days) VALUES (:t, 't', 'test', :d)"
                ),
                {"t": tenant_id, "d": days},
            )

    async def expired_ids(self, ids: set[UUID]) -> set[UUID]:
        from datetime import UTC, datetime

        rows = await self.store.list_expired(now=datetime.now(UTC), default_days=90, limit=100000)
        return {e.upload.id for e in rows if e.upload.id in ids}


@pytest.mark.asyncio
async def test_a_recent_run_in_the_conversation_keeps_an_old_upload(
    sql_store: SqlStoreFixture,
) -> None:
    store, engine = sql_store
    try:
        world = _LastUse(store, engine)
        tenant = uuid4()
        recent_run = await world.upload(tenant_id=tenant, age_days=200)
        await world.run_in_thread_of(recent_run, age_days=10)
        old_run = await world.upload(tenant_id=tenant, age_days=200)
        await world.run_in_thread_of(old_run, age_days=95)
        no_run = await world.upload(tenant_id=tenant, age_days=200)

        assert await world.expired_ids({recent_run, old_run, no_run}) == {old_run, no_run}
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_recent_use_keeps_an_old_upload_and_sets_its_expiry(
    sql_store: SqlStoreFixture,
) -> None:
    from datetime import UTC, datetime, timedelta

    store, engine = sql_store
    try:
        world = _LastUse(store, engine)
        tenant = uuid4()
        used_lately = await world.upload(tenant_id=tenant, age_days=200)
        await world.used(used_lately, age_days=10)
        used_long_ago = await world.upload(tenant_id=tenant, age_days=200)
        await world.used(used_long_ago, age_days=91)

        assert await world.expired_ids({used_lately, used_long_ago}) == {used_long_ago}
        # The expiry moment follows the latest of the three clocks.
        later = datetime.now(UTC) + timedelta(days=81)
        rows = {e.upload.id: e for e in await store.list_expired(now=later, default_days=90)}
        row = rows[used_lately]
        assert row.upload.last_used_at is not None
        assert row.expires_at == row.upload.last_used_at + timedelta(days=90)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_the_tenant_retention_days_override_the_default_for_that_tenant_only(
    sql_store: SqlStoreFixture,
) -> None:
    store, engine = sql_store
    try:
        world = _LastUse(store, engine)
        short, long_, unset, plain = uuid4(), uuid4(), uuid4(), uuid4()
        await world.tenant_days(short, 30)
        await world.tenant_days(long_, 200)
        await world.tenant_days(unset, None)  # a config row without the knob → default
        short_40 = await world.upload(tenant_id=short, age_days=40)
        short_20 = await world.upload(tenant_id=short, age_days=20)
        long_120 = await world.upload(tenant_id=long_, age_days=120)
        long_210 = await world.upload(tenant_id=long_, age_days=210)
        unset_100 = await world.upload(tenant_id=unset, age_days=100)
        plain_40 = await world.upload(tenant_id=plain, age_days=40)  # no config row at all
        plain_100 = await world.upload(tenant_id=plain, age_days=100)

        assert await world.expired_ids(
            {short_40, short_20, long_120, long_210, unset_100, plain_40, plain_100}
        ) == {short_40, long_210, unset_100, plain_100}
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_reference_check_sees_rows_kept_alive_by_a_recent_run(
    sql_store: SqlStoreFixture,
) -> None:
    """引用检查与到期列表同一个时钟:按 created_at 老、但会话里最近有 run 的行仍算引用。"""
    from datetime import UTC, datetime

    store, engine = sql_store
    try:
        world = _LastUse(store, engine)
        tenant, user = uuid4(), uuid4()
        alive = await world.upload(tenant_id=tenant, age_days=200, user_id=user)
        await world.run_in_thread_of(alive, age_days=3)
        await world.upload(tenant_id=tenant, age_days=200, user_id=user)  # truly expired

        rows = await store.list_unexpired_by_ref(
            tenant_id=tenant,
            user_id=user,
            ref="uploads/last-use.pdf",
            now=datetime.now(UTC),
            default_days=90,
        )
        assert [r.id for r in rows] == [alive]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_touch_writes_at_most_once_a_day(sql_store: SqlStoreFixture) -> None:
    from datetime import UTC, datetime, timedelta

    store, engine = sql_store
    try:
        world = _LastUse(store, engine)
        tenant = uuid4()
        upload = await world.upload(tenant_id=tenant, age_days=5)
        now = datetime.now(UTC)

        assert await store.touch(upload_id=upload, tenant_id=tenant, now=now) is True
        same_day = now + timedelta(hours=23)
        assert await store.touch(upload_id=upload, tenant_id=tenant, now=same_day) is False
        row = await store.get(upload_id=upload, tenant_id=tenant)
        assert row is not None and row.last_used_at == now
        next_day = now + timedelta(days=1, minutes=1)
        assert await store.touch(upload_id=upload, tenant_id=uuid4(), now=next_day) is False
        assert await store.touch(upload_id=upload, tenant_id=tenant, now=next_day) is True
        await store.soft_delete(upload_id=upload, tenant_id=tenant, now=next_day)
        later = next_day + timedelta(days=2)
        assert await store.touch(upload_id=upload, tenant_id=tenant, now=later) is False
    finally:
        await engine.dispose()
