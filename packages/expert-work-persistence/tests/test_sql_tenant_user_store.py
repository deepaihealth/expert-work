"""Integration tests for SqlTenantUserStore against a real Postgres — J.14."""

from __future__ import annotations

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
    SqlTenantUserStore,
    create_async_engine_from_config,
    create_async_session_factory,
)

pytestmark = pytest.mark.integration

ALEMBIC_INI = Path(__file__).resolve().parent.parent / "alembic.ini"

SqlStoreFixture = tuple[SqlTenantUserStore, AsyncEngine]


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
    yield SqlTenantUserStore(session_factory), engine


@pytest.mark.asyncio
async def test_resolve_upsert_round_trip(sql_store: SqlStoreFixture) -> None:
    store, engine = sql_store
    try:
        tenant_id = uuid4()
        first = await store.resolve(tenant_id=tenant_id, subject_type="user", subject_id="oidc-1")
        assert first.tenant_id == tenant_id
        assert first.subject_id == "oidc-1"

        again = await store.resolve(tenant_id=tenant_id, subject_type="user", subject_id="oidc-1")
        # ON CONFLICT path: same identity → same surrogate id.
        assert again.id == first.id
        assert again.created_at == first.created_at
        assert again.last_active_at is not None
        assert first.last_active_at is not None
        assert again.last_active_at >= first.last_active_at
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_resolve_display_name_coalesce(sql_store: SqlStoreFixture) -> None:
    store, engine = sql_store
    try:
        tenant_id = uuid4()
        await store.resolve(
            tenant_id=tenant_id,
            subject_type="user",
            subject_id="u",
            display_name="Grace",
        )
        # A nameless resolve must keep the stored name (COALESCE).
        preserved = await store.resolve(tenant_id=tenant_id, subject_type="user", subject_id="u")
        assert preserved.display_name == "Grace"

        renamed = await store.resolve(
            tenant_id=tenant_id,
            subject_type="user",
            subject_id="u",
            display_name="Grace H.",
        )
        assert renamed.display_name == "Grace H."
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_get_filters_by_tenant(sql_store: SqlStoreFixture) -> None:
    store, engine = sql_store
    try:
        owner, other = uuid4(), uuid4()
        user = await store.resolve(tenant_id=owner, subject_type="user", subject_id="u")

        fetched = await store.get(user.id, tenant_id=owner)
        assert fetched is not None
        assert fetched.id == user.id

        assert await store.get(user.id, tenant_id=other) is None
        assert await store.get(uuid4(), tenant_id=owner) is None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_get_by_subject_hits_then_misses(sql_store: SqlStoreFixture) -> None:
    store, engine = sql_store
    try:
        tenant_id = uuid4()
        created = await store.resolve(tenant_id=tenant_id, subject_type="user", subject_id="u-1")

        found = await store.get_by_subject(
            tenant_id=tenant_id, subject_type="user", subject_id="u-1"
        )
        assert found is not None
        assert found.id == created.id

        assert (
            await store.get_by_subject(
                tenant_id=tenant_id, subject_type="user", subject_id="never-seen"
            )
            is None
        )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_get_by_subject_is_not_visible_cross_tenant(sql_store: SqlStoreFixture) -> None:
    store, engine = sql_store
    try:
        owner, other = uuid4(), uuid4()
        await store.resolve(tenant_id=owner, subject_type="user", subject_id="shared-id")

        # Same subject_id, different tenant → must not be found.
        assert (
            await store.get_by_subject(tenant_id=other, subject_type="user", subject_id="shared-id")
            is None
        )
        found = await store.get_by_subject(
            tenant_id=owner, subject_type="user", subject_id="shared-id"
        )
        assert found is not None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_get_by_subject_distinguishes_subject_type(sql_store: SqlStoreFixture) -> None:
    store, engine = sql_store
    try:
        tenant_id = uuid4()
        svc = await store.resolve(
            tenant_id=tenant_id, subject_type="service_account", subject_id="x"
        )

        assert (
            await store.get_by_subject(tenant_id=tenant_id, subject_type="user", subject_id="x")
            is None
        )
        found = await store.get_by_subject(
            tenant_id=tenant_id, subject_type="service_account", subject_id="x"
        )
        assert found is not None
        assert found.id == svc.id
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_get_by_subject_returns_deactivated_row_like_get(
    sql_store: SqlStoreFixture,
) -> None:
    """``get_by_subject`` mirrors ``get``'s ``deleted_at`` semantics — a
    purged row is still returned (not silently dropped like
    ``list_by_tenant``); the caller decides what to do with it."""
    store, engine = sql_store
    try:
        tenant_id = uuid4()
        user = await store.resolve(tenant_id=tenant_id, subject_type="user", subject_id="x")
        assert await store.deactivate(user.id, tenant_id=tenant_id, now=datetime.now(UTC)) is True

        found = await store.get_by_subject(tenant_id=tenant_id, subject_type="user", subject_id="x")
        assert found is not None
        assert found.id == user.id
        assert found.deleted_at is not None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_resolve_distinguishes_identity_axes(sql_store: SqlStoreFixture) -> None:
    store, engine = sql_store
    try:
        tenant_a, tenant_b = uuid4(), uuid4()
        u1 = await store.resolve(tenant_id=tenant_a, subject_type="user", subject_id="x")
        u2 = await store.resolve(tenant_id=tenant_b, subject_type="user", subject_id="x")
        u3 = await store.resolve(tenant_id=tenant_a, subject_type="service_account", subject_id="x")
        u4 = await store.resolve(tenant_id=tenant_a, subject_type="user", subject_id="y")
        assert len({u1.id, u2.id, u3.id, u4.id}) == 4
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_list_by_tenant_filters_orders_paginates(sql_store: SqlStoreFixture) -> None:
    store, engine = sql_store
    try:
        owner, other = uuid4(), uuid4()
        u1 = await store.resolve(tenant_id=owner, subject_type="user", subject_id="u1")
        await store.resolve(tenant_id=owner, subject_type="user", subject_id="u2")
        await store.resolve(tenant_id=owner, subject_type="service_account", subject_id="svc")
        await store.resolve(tenant_id=other, subject_type="user", subject_id="foreign")
        # Bump u1 to most-recently-active (same identity → same row, id unchanged).
        await store.resolve(tenant_id=owner, subject_type="user", subject_id="u1")

        rows = await store.list_by_tenant(owner, subject_type="user")
        # subject_type filter excludes the service account + the other tenant.
        assert {r.subject_id for r in rows} == {"u1", "u2"}
        # Ordered by last_active_at desc — u1 (just bumped) is first.
        assert rows[0].id == u1.id
        # No filter → includes the service account (3 rows this tenant).
        assert len(await store.list_by_tenant(owner)) == 3
        # Pagination.
        page = await store.list_by_tenant(owner, subject_type="user", limit=1, offset=1)
        assert len(page) == 1
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_deactivate_then_resolve_reactivates(sql_store: SqlStoreFixture) -> None:
    """Phase 3a: ``deactivate`` soft-deletes (hidden from the roster, still
    gettable for idempotent re-purge); a returning identity re-``resolve``s to the
    same row with ``deleted_at`` cleared — reactivated + visible again."""
    store, engine = sql_store
    try:
        tenant = uuid4()
        u = await store.resolve(tenant_id=tenant, subject_type="user", subject_id="x")
        assert await store.deactivate(u.id, tenant_id=tenant, now=datetime.now(UTC)) is True
        # Idempotent re-deactivate still True; wrong tenant False.
        assert await store.deactivate(u.id, tenant_id=tenant, now=datetime.now(UTC)) is True
        assert await store.deactivate(u.id, tenant_id=uuid4(), now=datetime.now(UTC)) is False
        # Hidden from the roster, but get still returns it (deleted_at set).
        assert await store.list_by_tenant(tenant, subject_type="user") == []
        got = await store.get(u.id, tenant_id=tenant)
        assert got is not None and got.deleted_at is not None
        # A returning identity reactivates cleanly.
        again = await store.resolve(tenant_id=tenant, subject_type="user", subject_id="x")
        assert again.id == u.id
        assert again.deleted_at is None
        assert {r.id for r in await store.list_by_tenant(tenant, subject_type="user")} == {u.id}
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# PR1 Task 5 — hard_delete_deactivated() retention sweep
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hard_delete_deactivated_only_reaps_old_deactivated(
    sql_store: SqlStoreFixture,
) -> None:
    """Only rows deactivated before the cutoff are physically removed — an
    active row and a recently-deactivated row are both left alone (the
    retention window has not closed for the latter yet)."""
    store, engine = sql_store
    try:
        tenant = uuid4()
        active = await store.resolve(tenant_id=tenant, subject_type="user", subject_id="active")
        old = await store.resolve(tenant_id=tenant, subject_type="user", subject_id="old")
        recent = await store.resolve(tenant_id=tenant, subject_type="user", subject_id="recent")

        assert (
            await store.deactivate(
                old.id, tenant_id=tenant, now=datetime.now(UTC) - timedelta(days=100)
            )
            is True
        )
        assert (
            await store.deactivate(
                recent.id, tenant_id=tenant, now=datetime.now(UTC) - timedelta(days=10)
            )
            is True
        )

        cutoff = datetime.now(UTC) - timedelta(days=90)
        assert await store.hard_delete_deactivated(before=cutoff, limit=100) == 1

        assert await store.get(active.id, tenant_id=tenant) is not None
        assert await store.get(old.id, tenant_id=tenant) is None
        got_recent = await store.get(recent.id, tenant_id=tenant)
        assert got_recent is not None
        assert got_recent.deleted_at is not None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_hard_delete_deactivated_respects_limit(sql_store: SqlStoreFixture) -> None:
    """Two rows are both past the cutoff; ``limit=1`` only physically
    removes the oldest one (``deleted_at`` ascending)."""
    store, engine = sql_store
    try:
        tenant = uuid4()
        first = await store.resolve(tenant_id=tenant, subject_type="user", subject_id="first")
        second = await store.resolve(tenant_id=tenant, subject_type="user", subject_id="second")
        assert (
            await store.deactivate(
                first.id, tenant_id=tenant, now=datetime.now(UTC) - timedelta(days=100)
            )
            is True
        )
        assert (
            await store.deactivate(
                second.id, tenant_id=tenant, now=datetime.now(UTC) - timedelta(days=95)
            )
            is True
        )

        cutoff = datetime.now(UTC) - timedelta(days=90)
        assert await store.hard_delete_deactivated(before=cutoff, limit=1) == 1

        assert await store.get(first.id, tenant_id=tenant) is None
        assert await store.get(second.id, tenant_id=tenant) is not None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_hard_delete_deactivated_sweeps_across_tenants(sql_store: SqlStoreFixture) -> None:
    """``hard_delete_deactivated`` has no tenant predicate — it is a
    cross-tenant retention sweep. Two different tenants each have one
    expired deactivated row; a single call reaps both."""
    store, engine = sql_store
    try:
        cutoff = datetime.now(UTC) - timedelta(days=90)
        # ``postgres_container`` is session-scoped and shared with sibling
        # hard_delete_deactivated tests in this module, which can leave
        # already-expired debris behind. Flush it first so the exact-count
        # assertion below is deterministic regardless of test order.
        await store.hard_delete_deactivated(before=cutoff, limit=10_000)

        tenant_a, tenant_b = uuid4(), uuid4()
        user_a = await store.resolve(tenant_id=tenant_a, subject_type="user", subject_id="a")
        user_b = await store.resolve(tenant_id=tenant_b, subject_type="user", subject_id="b")
        old = datetime.now(UTC) - timedelta(days=100)
        assert await store.deactivate(user_a.id, tenant_id=tenant_a, now=old) is True
        assert await store.deactivate(user_b.id, tenant_id=tenant_b, now=old) is True

        assert await store.hard_delete_deactivated(before=cutoff, limit=100) == 2

        assert await store.get(user_a.id, tenant_id=tenant_a) is None
        assert await store.get(user_b.id, tenant_id=tenant_b) is None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_hard_delete_deactivated_revival_not_swept(sql_store: SqlStoreFixture) -> None:
    """A deactivated identity that RE-``resolve``s before the sweep runs
    clears ``deleted_at`` (reactivation) — the row must not be swept even
    though it was deactivated long enough ago."""
    store, engine = sql_store
    try:
        tenant = uuid4()
        u = await store.resolve(tenant_id=tenant, subject_type="user", subject_id="x")
        old = datetime.now(UTC) - timedelta(days=100)
        assert await store.deactivate(u.id, tenant_id=tenant, now=old) is True

        # The identity returns — resolve() clears deleted_at (reactivation).
        revived = await store.resolve(tenant_id=tenant, subject_type="user", subject_id="x")
        assert revived.id == u.id
        assert revived.deleted_at is None

        cutoff = datetime.now(UTC) - timedelta(days=90)
        assert await store.hard_delete_deactivated(before=cutoff, limit=100) == 0
        assert await store.get(u.id, tenant_id=tenant) is not None
    finally:
        await engine.dispose()
