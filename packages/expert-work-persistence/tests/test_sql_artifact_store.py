"""Integration tests for SqlArtifactStore against a real Postgres — J.9."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from testcontainers.postgres import PostgresContainer

from expert_work.persistence import (
    DatabaseConfig,
    SqlArtifactStore,
    create_async_engine_from_config,
    create_async_session_factory,
)

pytestmark = pytest.mark.integration

ALEMBIC_INI = Path(__file__).resolve().parent.parent / "alembic.ini"

SqlStoreFixture = tuple[SqlArtifactStore, AsyncEngine]


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
    yield SqlArtifactStore(session_factory), engine


@pytest.mark.asyncio
async def test_save_version_round_trip_and_bump(sql_store: SqlStoreFixture) -> None:
    store, engine = sql_store
    try:
        tenant_id, user_id = uuid4(), uuid4()
        v1 = await store.save_version(
            tenant_id=tenant_id,
            user_id=user_id,
            name="report.md",
            kind="document",
            path_in_workspace="report.md",
            created_in_thread="t-1",
        )
        assert v1.version == 1

        # ON CONFLICT path: same name → next version, same artifact id.
        v2 = await store.save_version(
            tenant_id=tenant_id,
            user_id=user_id,
            name="report.md",
            kind="document",
            path_in_workspace="report.md",
            created_in_thread="t-2",
        )
        assert v2.version == 2
        assert v2.artifact_id == v1.artifact_id

        artifacts = await store.list_for_user(tenant_id=tenant_id, user_id=user_id)
        assert len(artifacts) == 1
        assert artifacts[0].latest_version == 2
        assert artifacts[0].kind == "document"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_list_for_user_isolates_tenant_and_user(sql_store: SqlStoreFixture) -> None:
    store, engine = sql_store
    try:
        tenant_a, tenant_b = uuid4(), uuid4()
        user_x, user_y = uuid4(), uuid4()
        for tenant_id, user_id in ((tenant_a, user_x), (tenant_a, user_y), (tenant_b, user_x)):
            await store.save_version(
                tenant_id=tenant_id,
                user_id=user_id,
                name="shared-name",
                kind="data",
                path_in_workspace="shared-name",
                created_in_thread="t",
            )
        assert len(await store.list_for_user(tenant_id=tenant_a, user_id=user_x)) == 1
        assert len(await store.list_for_user(tenant_id=tenant_b, user_id=user_x)) == 1
        assert await store.list_for_user(tenant_id=uuid4(), user_id=user_x) == []
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_get_latest_version_and_digest_backfill(sql_store: SqlStoreFixture) -> None:
    store, engine = sql_store
    try:
        tenant_id, user_id = uuid4(), uuid4()
        await store.save_version(
            tenant_id=tenant_id,
            user_id=user_id,
            name="report.md",
            kind="document",
            path_in_workspace="v1.md",
            created_in_thread="t-1",
        )
        await store.save_version(
            tenant_id=tenant_id,
            user_id=user_id,
            name="report.md",
            kind="document",
            path_in_workspace="v2.md",
            created_in_thread="t-2",
        )
        latest = await store.get_latest_version(
            tenant_id=tenant_id, user_id=user_id, name="report.md"
        )
        assert latest is not None
        assert latest.version == 2
        assert latest.path_in_workspace == "v2.md"
        assert latest.size_bytes is None

        await store.set_version_digest(version_id=latest.id, size_bytes=4096, sha256="deadbeef")
        refreshed = await store.get_latest_version(
            tenant_id=tenant_id, user_id=user_id, name="report.md"
        )
        assert refreshed is not None
        assert refreshed.size_bytes == 4096
        assert refreshed.sha256 == "deadbeef"

        assert (
            await store.get_latest_version(tenant_id=tenant_id, user_id=user_id, name="nope")
            is None
        )
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# Mini-ADR J-25 (J.9-step1) — lifecycle: soft-delete / list_expired / hard-delete
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_soft_delete_hides_from_list_and_get(sql_store: SqlStoreFixture) -> None:
    store, engine = sql_store
    try:
        tenant_id, user_id = uuid4(), uuid4()
        await store.save_version(
            tenant_id=tenant_id,
            user_id=user_id,
            name="report.md",
            kind="document",
            path_in_workspace="report.md",
            created_in_thread="t-1",
        )
        now = datetime.now(UTC)
        hit = await store.soft_delete(
            tenant_id=tenant_id, user_id=user_id, name="report.md", now=now
        )
        assert hit is True
        # Default list hides; include_deleted=True reveals + ``deleted_at``
        # round-trips.
        assert await store.list_for_user(tenant_id=tenant_id, user_id=user_id) == []
        deleted = await store.list_for_user(
            tenant_id=tenant_id, user_id=user_id, include_deleted=True
        )
        assert len(deleted) == 1
        assert deleted[0].deleted_at is not None
        # get_latest_version hides soft-deleted.
        assert (
            await store.get_latest_version(tenant_id=tenant_id, user_id=user_id, name="report.md")
            is None
        )
        # Second soft-delete is a no-op miss.
        assert not await store.soft_delete(
            tenant_id=tenant_id, user_id=user_id, name="report.md", now=now
        )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_save_version_undeletes_soft_deleted_row(
    sql_store: SqlStoreFixture,
) -> None:
    """A re-save on a soft-deleted name un-deletes it (Mini-ADR J-25)."""
    store, engine = sql_store
    try:
        tenant_id, user_id = uuid4(), uuid4()
        await store.save_version(
            tenant_id=tenant_id,
            user_id=user_id,
            name="report.md",
            kind="document",
            path_in_workspace="v1.md",
            created_in_thread="t-1",
        )
        await store.soft_delete(
            tenant_id=tenant_id,
            user_id=user_id,
            name="report.md",
            now=datetime.now(UTC),
        )
        v2 = await store.save_version(
            tenant_id=tenant_id,
            user_id=user_id,
            name="report.md",
            kind="document",
            path_in_workspace="v2.md",
            created_in_thread="t-2",
        )
        assert v2.version == 2
        active = await store.list_for_user(tenant_id=tenant_id, user_id=user_id)
        assert len(active) == 1
        assert active[0].deleted_at is None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_list_expired_returns_soft_deleted_past_horizon(
    sql_store: SqlStoreFixture,
) -> None:
    store, engine = sql_store
    try:
        tenant_id, user_id = uuid4(), uuid4()
        for name in ("old.md", "recent.md"):
            await store.save_version(
                tenant_id=tenant_id,
                user_id=user_id,
                name=name,
                kind="document",
                path_in_workspace=name,
                created_in_thread="t",
            )
        old_time = datetime.now(UTC) - timedelta(days=70)
        recent_time = datetime.now(UTC) - timedelta(days=10)
        await store.soft_delete(tenant_id=tenant_id, user_id=user_id, name="old.md", now=old_time)
        await store.soft_delete(
            tenant_id=tenant_id, user_id=user_id, name="recent.md", now=recent_time
        )
        cutoff = datetime.now(UTC) - timedelta(days=60)
        expired = await store.list_expired(before=cutoff)
        assert [a.name for a in expired] == ["old.md"]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_list_active_past_retention_picks_stale_active_only(
    sql_store: SqlStoreFixture,
) -> None:
    store, engine = sql_store
    try:
        tenant_id, user_id = uuid4(), uuid4()
        await store.save_version(
            tenant_id=tenant_id,
            user_id=user_id,
            name="stale.md",
            kind="document",
            path_in_workspace="stale.md",
            created_in_thread="t-old",
        )
        # Backdate ``updated_at`` directly via SQL (the SET LOCAL RLS
        # role is already configured by the fixture).
        backdated = datetime.now(UTC) - timedelta(days=100)
        engine_for_update = engine
        async with engine_for_update.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE artifact SET updated_at = :ts "
                    "WHERE tenant_id = :t AND user_id = :u AND name = 'stale.md'"
                ),
                {"ts": backdated, "t": tenant_id, "u": user_id},
            )
        await store.save_version(
            tenant_id=tenant_id,
            user_id=user_id,
            name="fresh.md",
            kind="document",
            path_in_workspace="fresh.md",
            created_in_thread="t-new",
        )
        cutoff = datetime.now(UTC) - timedelta(days=90)
        rows = await store.list_active_past_retention(before=cutoff)
        assert [a.name for a in rows] == ["stale.md"]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_update_kind_changes_kind_and_hides_when_soft_deleted(
    sql_store: SqlStoreFixture,
) -> None:
    store, engine = sql_store
    try:
        tenant_id, user_id = uuid4(), uuid4()
        await store.save_version(
            tenant_id=tenant_id,
            user_id=user_id,
            name="report.md",
            kind="document",
            path_in_workspace="report.md",
            created_in_thread="t-1",
        )
        updated = await store.update_kind(
            tenant_id=tenant_id, user_id=user_id, name="report.md", kind="code"
        )
        assert updated is not None
        assert updated.kind == "code"
        # Soft-delete then attempt → None (hidden).
        await store.soft_delete(
            tenant_id=tenant_id,
            user_id=user_id,
            name="report.md",
            now=datetime.now(UTC),
        )
        assert (
            await store.update_kind(
                tenant_id=tenant_id, user_id=user_id, name="report.md", kind="data"
            )
            is None
        )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_list_versions_desc_or_none(sql_store: SqlStoreFixture) -> None:
    store, engine = sql_store
    try:
        tenant_id, user_id = uuid4(), uuid4()
        for path in ("v1.md", "v2.md", "v3.md"):
            await store.save_version(
                tenant_id=tenant_id,
                user_id=user_id,
                name="report.md",
                kind="document",
                path_in_workspace=path,
                created_in_thread="t",
            )
        versions = await store.list_versions(tenant_id=tenant_id, user_id=user_id, name="report.md")
        assert versions is not None
        assert [v.version for v in versions] == [3, 2, 1]
        # Unknown name → None.
        assert (
            await store.list_versions(tenant_id=tenant_id, user_id=user_id, name="missing") is None
        )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_hard_delete_removes_artifact_and_versions(
    sql_store: SqlStoreFixture,
) -> None:
    store, engine = sql_store
    try:
        tenant_id, user_id = uuid4(), uuid4()
        await store.save_version(
            tenant_id=tenant_id,
            user_id=user_id,
            name="doomed.md",
            kind="document",
            path_in_workspace="v1.md",
            created_in_thread="t-1",
        )
        await store.save_version(
            tenant_id=tenant_id,
            user_id=user_id,
            name="doomed.md",
            kind="document",
            path_in_workspace="v2.md",
            created_in_thread="t-2",
        )
        artifacts = await store.list_for_user(tenant_id=tenant_id, user_id=user_id)
        assert len(artifacts) == 1
        artifact_id = artifacts[0].id
        removed = await store.hard_delete(artifact_ids=[artifact_id])
        assert removed == 1
        # Both artifact and version rows gone.
        assert (
            await store.list_for_user(tenant_id=tenant_id, user_id=user_id, include_deleted=True)
            == []
        )
        # Empty list is a no-op.
        assert await store.hard_delete(artifact_ids=[]) == 0
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# 留存链 B-28 —— 按版本 90 天(SQL)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_version_retention_methods_round_trip(sql_store: SqlStoreFixture) -> None:
    """list_versions_expired 按版本 created_at 跨租户取;delete_versions 只删版本行;
    mark_expired_if_versionless 只在版本清空且行仍活着时翻 deleted_at;
    list_versions_by_artifact 不管父行软删与否都能列。"""
    store, engine = sql_store
    try:
        tenant_a, user_a = uuid4(), uuid4()
        tenant_b, user_b = uuid4(), uuid4()
        for path in ("a.v1", "a.v2", "a.v3"):
            v = await store.save_version(
                tenant_id=tenant_a,
                user_id=user_a,
                name="a.md",
                kind="document",
                path_in_workspace=path,
                created_in_thread="t",
            )
        artifact_a = v.artifact_id
        vb = await store.save_version(
            tenant_id=tenant_b,
            user_id=user_b,
            name="b.md",
            kind="document",
            path_in_workspace="b.v1",
            created_in_thread="t",
        )
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE artifact_version SET created_at = now() - make_interval(days => :d) "
                    "WHERE artifact_id = :a AND version <= 2"
                ),
                {"d": 120, "a": artifact_a},
            )
            await conn.execute(
                text(
                    "UPDATE artifact_version SET created_at = now() - make_interval(days => :d) "
                    "WHERE id = :v"
                ),
                {"d": 200, "v": vb.id},
            )
        cutoff = datetime.now(UTC) - timedelta(days=90)

        expired = await store.list_versions_expired(before=cutoff)
        mine = [(x.artifact_id, x.version) for x in expired if x.tenant_id in {tenant_a, tenant_b}]
        assert mine == [(vb.artifact_id, 1), (artifact_a, 1), (artifact_a, 2)]
        assert len(await store.list_versions_expired(before=cutoff, limit=2)) == 2

        now = datetime.now(UTC)
        a_old = [x.id for x in expired if x.artifact_id == artifact_a]
        assert await store.delete_versions(version_ids=a_old) == 2
        assert await store.delete_versions(version_ids=a_old) == 0
        assert await store.delete_versions(version_ids=[]) == 0
        assert await store.mark_expired_if_versionless(artifact_id=artifact_a, now=now) is False
        remaining = await store.list_versions_by_artifact(artifact_id=artifact_a)
        assert [x.version for x in remaining] == [3]
        latest = await store.get_latest_version(tenant_id=tenant_a, user_id=user_a, name="a.md")
        assert latest is not None and latest.version == 3

        assert await store.delete_versions(version_ids=[vb.id]) == 1
        assert await store.mark_expired_if_versionless(artifact_id=vb.artifact_id, now=now) is True
        assert await store.mark_expired_if_versionless(artifact_id=vb.artifact_id, now=now) is False
        assert (
            await store.get_latest_version(tenant_id=tenant_b, user_id=user_b, name="b.md") is None
        )
        deleted = await store.list_for_user(
            tenant_id=tenant_b, user_id=user_b, include_deleted=True
        )
        assert len(deleted) == 1 and deleted[0].deleted_at is not None
        # Soft-deleted parent: list_versions hides it, list_versions_by_artifact does not.
        assert await store.list_versions(tenant_id=tenant_b, user_id=user_b, name="b.md") is None
        assert await store.list_versions_by_artifact(artifact_id=vb.artifact_id) == []
        # A soft-deleted parent that still has versions is listable too.
        await store.soft_delete(tenant_id=tenant_a, user_id=user_a, name="a.md", now=now)
        still = await store.list_versions_by_artifact(artifact_id=artifact_a)
        assert [x.version for x in still] == [3]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_list_versions_expired_orders_same_timestamp_by_version(
    sql_store: SqlStoreFixture,
) -> None:
    """CI 复盘:同一 ``created_at`` 的版本此前按 ``id``(uuid4)tiebreak,同一产物
    v1 / v2 谁先谁后是随机的(三跑两红)。ids 故意改成与 version 反序,
    ``version`` tiebreak 不在就必红。"""
    store, engine = sql_store
    try:
        tenant_id, user_id = uuid4(), uuid4()
        ids: list[UUID] = []
        for path in ("t.v1", "t.v2", "t.v3"):
            v = await store.save_version(
                tenant_id=tenant_id,
                user_id=user_id,
                name="tie.md",
                kind="document",
                path_in_workspace=path,
                created_in_thread="t",
            )
            ids.append(v.id)
        artifact_id = v.artifact_id
        # Same timestamp for all three, and ids that sort in REVERSE version order.
        reversed_ids = [
            UUID("ffffffff-0000-4000-8000-000000000001"),
            UUID("88888888-0000-4000-8000-000000000002"),
            UUID("00000000-0000-4000-8000-000000000003"),
        ]
        async with engine.begin() as conn:
            for old_id, new_id in zip(ids, reversed_ids, strict=True):
                await conn.execute(
                    text(
                        "UPDATE artifact_version SET id = :new, "
                        "created_at = now() - interval '100 days' WHERE id = :old"
                    ),
                    {"new": new_id, "old": old_id},
                )
        cutoff = datetime.now(UTC) - timedelta(days=90)
        expired = [
            (x.artifact_id, x.version)
            for x in await store.list_versions_expired(before=cutoff)
            if x.artifact_id == artifact_id
        ]
        assert expired == [(artifact_id, 1), (artifact_id, 2), (artifact_id, 3)]
    finally:
        await engine.dispose()
