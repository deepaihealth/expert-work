"""Integration tests for SqlImageUploadStore against a real Postgres.

Mini-ADR J-32 (删除接口卫生修复 PR1 / Task 3) — ``list_reapable`` predicate
must be byte-identical to :class:`InMemoryImageUploadStore`'s.

B-283 —— ``list_reapable`` 按**最后使用**到期(上传 / 下载 / 会话最后一次 run 最晚的那个,加
租户的 ``upload_retention_days``);下面 ``_LastUse`` 那一组在真 Postgres 上钉这个时钟。
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

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
    """``list_reapable`` matches ``(last use + days < now) OR (deleted_at IS
    NOT NULL)`` — old rows are swept regardless of delete state, and a
    freshly soft-deleted row is swept even though it's too young to be
    caught by the age predicate alone."""
    store, engine = sql_store
    try:
        tenant_id = uuid4()
        now = datetime.now(UTC)
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

        reapable = await store.list_reapable(now=now, default_days=90)

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

        reapable = await store.list_reapable(now=datetime.now(UTC), default_days=90, limit=2)
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


class _LastUse:
    """在真 Postgres 上造「很久以前上传」的图,再按需补一次 run、一次下载、一条租户配置。"""

    def __init__(self, store: SqlImageUploadStore, engine: AsyncEngine) -> None:
        self.store = store
        self.engine = engine

    async def image(self, *, tenant_id: UUID, age_days: float) -> UUID:
        image_id = uuid4()
        await self.store.insert(
            image_id=image_id,
            tenant_id=tenant_id,
            thread_id=uuid4(),
            user_id=None,
            object_key=f"tenants/x/uploads/{image_id}.png",
            size_bytes=1,
            mime_type="image/png",
            sha256="x",
        )
        await self._set(image_id, "created_at", age_days)
        return image_id

    async def _set(self, image_id: UUID, column: str, age_days: float) -> None:
        sql = {
            "created_at": "UPDATE image_upload SET created_at = now() - make_interval(secs => :s)",
            "last_used_at": (
                "UPDATE image_upload SET last_used_at = now() - make_interval(secs => :s)"
            ),
            "deleted_at": "UPDATE image_upload SET deleted_at = now() - make_interval(secs => :s)",
        }[column]
        async with self.engine.begin() as conn:
            await conn.execute(
                text(sql + " WHERE id = :i"),
                {"s": age_days * 86400, "i": image_id},
            )

    async def used(self, image_id: UUID, *, age_days: float) -> None:
        await self._set(image_id, "last_used_at", age_days)

    async def deleted(self, image_id: UUID, *, age_days: float) -> None:
        await self._set(image_id, "deleted_at", age_days)

    async def run_in_thread_of(
        self, image_id: UUID, *, age_days: float, tenant_id: UUID | None = None
    ) -> None:
        async with self.engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO agent_run (id, tenant_id, thread_id, status, on_disconnect, "
                    " created_at, updated_at) "
                    "SELECT :r, coalesce(CAST(:t AS uuid), tenant_id), thread_id, 'success', "
                    " 'continue', now() - make_interval(secs => :s), "
                    " now() - make_interval(secs => :s) "
                    "FROM image_upload WHERE id = :i"
                ),
                {"r": uuid4(), "t": tenant_id, "s": age_days * 86400, "i": image_id},
            )

    async def tenant_days(self, tenant_id: UUID, days: int | None) -> None:
        async with self.engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO tenant_config (tenant_id, display_name, updated_by, "
                    " upload_retention_days) VALUES (:t, 't', 'test', :d)"
                ),
                {"t": tenant_id, "d": days},
            )

    async def reapable(self, ids: set[UUID], *, now: datetime | None = None) -> set[UUID]:
        rows = await self.store.list_reapable(
            now=now or datetime.now(UTC), default_days=90, limit=100000
        )
        return {r.id for r in rows if r.id in ids}


@pytest.mark.asyncio
async def test_a_recent_run_in_the_conversation_keeps_an_old_image(
    sql_store: SqlStoreFixture,
) -> None:
    """B-283 —— 会话还在用(最近有 run),200 天前上传的图也不到期;会话 95 天没动的照常到期。"""
    store, engine = sql_store
    try:
        world = _LastUse(store, engine)
        tenant = uuid4()
        recent_run = await world.image(tenant_id=tenant, age_days=200)
        await world.run_in_thread_of(recent_run, age_days=10)
        old_run = await world.image(tenant_id=tenant, age_days=200)
        await world.run_in_thread_of(old_run, age_days=95)
        no_run = await world.image(tenant_id=tenant, age_days=200)

        assert await world.reapable({recent_run, old_run, no_run}) == {old_run, no_run}
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_recent_download_keeps_an_old_image(sql_store: SqlStoreFixture) -> None:
    store, engine = sql_store
    try:
        world = _LastUse(store, engine)
        tenant = uuid4()
        used_lately = await world.image(tenant_id=tenant, age_days=200)
        await world.used(used_lately, age_days=10)
        used_long_ago = await world.image(tenant_id=tenant, age_days=200)
        await world.used(used_long_ago, age_days=91)

        assert await world.reapable({used_lately, used_long_ago}) == {used_long_ago}
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_the_tenant_upload_retention_days_apply_to_images(
    sql_store: SqlStoreFixture,
) -> None:
    """图片也是上传:租户的 ``upload_retention_days`` 管它;没设(或没有配置行)用默认。"""
    store, engine = sql_store
    try:
        world = _LastUse(store, engine)
        short, long_, unset, plain = uuid4(), uuid4(), uuid4(), uuid4()
        await world.tenant_days(short, 30)
        await world.tenant_days(long_, 200)
        await world.tenant_days(unset, None)
        short_40 = await world.image(tenant_id=short, age_days=40)
        short_20 = await world.image(tenant_id=short, age_days=20)
        long_120 = await world.image(tenant_id=long_, age_days=120)
        long_210 = await world.image(tenant_id=long_, age_days=210)
        unset_100 = await world.image(tenant_id=unset, age_days=100)
        plain_40 = await world.image(tenant_id=plain, age_days=40)
        plain_100 = await world.image(tenant_id=plain, age_days=100)

        assert await world.reapable(
            {short_40, short_20, long_120, long_210, unset_100, plain_40, plain_100}
        ) == {short_40, long_210, unset_100, plain_100}
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_run_in_another_tenant_with_the_same_thread_id_does_not_count(
    sql_store: SqlStoreFixture,
) -> None:
    store, engine = sql_store
    try:
        world = _LastUse(store, engine)
        image = await world.image(tenant_id=uuid4(), age_days=200)
        await world.run_in_thread_of(image, age_days=0, tenant_id=uuid4())

        assert await world.reapable({image}) == {image}
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_touch_writes_at_most_once_a_day_and_only_for_its_tenant(
    sql_store: SqlStoreFixture,
) -> None:
    store, engine = sql_store
    try:
        world = _LastUse(store, engine)
        tenant = uuid4()
        image = await world.image(tenant_id=tenant, age_days=5)
        now = datetime.now(UTC)

        assert await store.touch(image_id=image, tenant_id=tenant, now=now) is True
        same_day = now + timedelta(hours=23)
        assert await store.touch(image_id=image, tenant_id=tenant, now=same_day) is False
        row = await store.get(image_id=image, tenant_id=tenant)
        assert row is not None and row.last_used_at == now
        next_day = now + timedelta(days=1, minutes=1)
        assert await store.touch(image_id=image, tenant_id=uuid4(), now=next_day) is False
        assert await store.touch(image_id=image, tenant_id=tenant, now=next_day) is True
        await store.soft_delete(image_id=image, tenant_id=tenant, now=next_day)
        later = next_day + timedelta(days=2)
        assert await store.touch(image_id=image, tenant_id=tenant, now=later) is False
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_the_last_use_rule_only_ever_deletes_later_than_the_created_at_rule(
    sql_store: SqlStoreFixture,
) -> None:
    """B-283 —— 新旧规则对照(租户没设天数,两边都是 90 天)。

    旧规则:``created_at < now - 90 天`` 或已软删。新规则在任何一个时刻删的,旧规则在那个
    时刻也一定删(只会更晚,不会更早);两者的差恰好是「最近被下载过 / 会话最近有 run」、
    按最后使用还没满 90 天的图;等所有时钟都过去之后,两边删的完全一样。
    """
    store, engine = sql_store
    try:
        world = _LastUse(store, engine)
        tenant = uuid4()
        created: dict[UUID, float] = {}
        last_use: dict[UUID, float] = {}  # 最后一次使用离现在几天
        deleted: set[UUID] = set()
        for age in (5, 60, 89.5, 90.5, 120, 200, 400):
            plain = await world.image(tenant_id=tenant, age_days=age)
            created[plain] = last_use[plain] = age
            downloaded = await world.image(tenant_id=tenant, age_days=age)
            await world.used(downloaded, age_days=min(age, 3))
            created[downloaded], last_use[downloaded] = age, min(age, 3)
            ran = await world.image(tenant_id=tenant, age_days=age)
            await world.run_in_thread_of(ran, age_days=min(age, 30))
            created[ran], last_use[ran] = age, min(age, 30)
            gone = await world.image(tenant_id=tenant, age_days=age)
            await world.used(gone, age_days=min(age, 2))
            await world.deleted(gone, age_days=1)
            created[gone] = last_use[gone] = age
            deleted.add(gone)
        ids = set(created)

        # 偏移量避开「最后使用 + 偏移 = 90 天」的边界(那一刻两边谁先谁后取决于毫秒)。
        for shift in (0, 10, 40, 59, 61, 75, 88, 100):
            at = datetime.now(UTC) + timedelta(days=shift)
            old_rule = {i for i in ids if created[i] + shift > 90 or i in deleted}
            new_rule = await world.reapable(ids, now=at)
            assert new_rule <= old_rule, shift
            kept = {i for i in old_rule if i not in deleted and last_use[i] + shift <= 90}
            assert old_rule - new_rule == kept, shift
        # 所有时钟都过去之后,两边一样。
        assert await world.reapable(ids, now=datetime.now(UTC) + timedelta(days=400)) == ids
    finally:
        await engine.dispose()
