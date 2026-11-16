"""Integration test for :class:`SqlPlatformSecretStore` — Stream P (P-7).

Platform secret rows are tenant-less / RLS-exempt, so this test uses the
container's default connection directly (no app-role / RLS dance needed).
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.ext.asyncio import AsyncEngine
from testcontainers.postgres import PostgresContainer

from expert_work.persistence import (
    DatabaseConfig,
    SqlPlatformSecretStore,
    create_async_engine_from_config,
    create_async_session_factory,
)

pytestmark = pytest.mark.integration

ALEMBIC_INI = Path(__file__).resolve().parent.parent / "alembic.ini"


def _sync_dsn(container: PostgresContainer) -> str:
    url = str(container.get_connection_url())
    return url.replace("+psycopg2", "+psycopg").replace("postgresql://", "postgresql+psycopg://", 1)


def _async_dsn(container: PostgresContainer) -> str:
    url = str(container.get_connection_url())
    return url.replace("+psycopg2", "+asyncpg").replace("postgresql://", "postgresql+asyncpg://", 1)


@pytest.fixture
def platform_secret_store(
    postgres_container: PostgresContainer,
) -> Iterator[tuple[SqlPlatformSecretStore, AsyncEngine]]:
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", _sync_dsn(postgres_container))
    command.upgrade(cfg, "head")

    engine = create_async_engine_from_config(DatabaseConfig(dsn=_async_dsn(postgres_container)))
    sf = create_async_session_factory(engine)
    yield SqlPlatformSecretStore(sf), engine


@pytest.mark.asyncio
async def test_provider_round_trip(
    platform_secret_store: tuple[SqlPlatformSecretStore, AsyncEngine],
) -> None:
    store, engine = platform_secret_store
    try:
        created = await store.upsert_provider(
            provider="anthropic",
            secret_ref="kms://platform/anthropic",
            enabled=True,
            actor_id="admin",
        )
        assert created.provider == "anthropic"
        assert created.enabled is True

        fetched = await store.get_provider("anthropic")
        assert fetched is not None
        assert fetched.secret_ref == "kms://platform/anthropic"

        # Upsert preserves created_at, can disable.
        updated = await store.upsert_provider(
            provider="anthropic",
            secret_ref="secret://anthropic-rotated",
            enabled=False,
            actor_id="admin2",
        )
        assert updated.created_at == created.created_at
        assert updated.enabled is False
        assert [p.provider for p in await store.list_providers()] == ["anthropic"]

        assert await store.delete_provider("anthropic") is True
        assert await store.delete_provider("anthropic") is False
        assert await store.get_provider("anthropic") is None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_tool_round_trip(
    platform_secret_store: tuple[SqlPlatformSecretStore, AsyncEngine],
) -> None:
    store, engine = platform_secret_store
    try:
        await store.upsert_tool(
            tool="web_search", secret_ref="kms://tavily", enabled=True, actor_id="a"
        )
        fetched = await store.get_tool("web_search")
        assert fetched is not None
        assert fetched.secret_ref == "kms://tavily"
        assert await store.delete_tool("web_search") is True
        assert await store.get_tool("web_search") is None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_tenant_override_round_trip(
    platform_secret_store: tuple[SqlPlatformSecretStore, AsyncEngine],
) -> None:
    """Stream HX-8: tenant override CRUD against real PG (owner connection —
    ENABLE-only RLS exempts the owner, mirroring the service bypass path)."""
    from uuid import UUID

    tenant_a = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
    tenant_b = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
    store, engine = platform_secret_store
    try:
        created = await store.upsert_tenant_provider(
            tenant_id=tenant_a,
            provider="anthropic",
            secret_ref="kms://tenant-a/anthropic",
            enabled=True,
            actor_id="admin",
        )
        assert created.tenant_id == tenant_a

        # Upsert preserves created_at; can disable (HX-H2 suppress marker).
        updated = await store.upsert_tenant_provider(
            tenant_id=tenant_a,
            provider="anthropic",
            secret_ref="kms://tenant-a/rotated",
            enabled=False,
            actor_id="admin2",
        )
        assert updated.created_at == created.created_at
        assert updated.enabled is False

        await store.upsert_tenant_tool(
            tenant_id=tenant_b,
            tool="web_search",
            secret_ref="kms://tenant-b/tavily",
            enabled=True,
            actor_id="admin",
        )
        # All-tenants load (service cache) vs per-tenant filter.
        assert len(await store.list_tenant_providers()) == 1
        assert await store.list_tenant_providers(tenant_b) == []
        assert len(await store.list_tenant_tools(tenant_b)) == 1

        assert await store.delete_tenant_provider(tenant_id=tenant_a, provider="anthropic") is True
        assert await store.delete_tenant_provider(tenant_id=tenant_a, provider="anthropic") is False
        assert await store.delete_tenant_tool(tenant_id=tenant_b, tool="web_search") is True
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_provider_multikey_round_trip(
    platform_secret_store: tuple[SqlPlatformSecretStore, AsyncEngine],
) -> None:
    """Stream Y-MK — multiple keys per provider against real PG (migration
    0084 composite PK ``(provider, key_id)`` exercised via the fixture's
    ``upgrade(head)``)."""
    store, engine = platform_secret_store
    try:
        await store.upsert_provider(
            provider="deepseek",
            key_id="acct-a",
            secret_ref="kms://a",
            enabled=True,
            priority=10,
            actor_id="admin",
        )
        await store.upsert_provider(
            provider="deepseek",
            key_id="acct-b",
            secret_ref="kms://b",
            enabled=True,
            priority=20,
            actor_id="admin",
        )
        rows = [r for r in await store.list_providers() if r.provider == "deepseek"]
        assert {r.key_id for r in rows} == {"acct-a", "acct-b"}
        a = await store.get_provider("deepseek", "acct-a")
        assert a is not None and a.priority == 10

        # Deleting one key leaves the sibling intact.
        assert await store.delete_provider("deepseek", "acct-a") is True
        remaining = [r for r in await store.list_providers() if r.provider == "deepseek"]
        assert [r.key_id for r in remaining] == ["acct-b"]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_base_url_columns_round_trip_and_are_overwritten(
    platform_secret_store: tuple[SqlPlatformSecretStore, AsyncEngine],
) -> None:
    """B-308 §4.1 — both tables store ``base_url`` / ``base_url_unverified``;
    an upsert that omits them writes the defaults (whole-row overwrite)."""
    from uuid import UUID

    tenant = UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")
    url = "https://proxy.example.com/api/paas/v4"
    store, engine = platform_secret_store
    try:
        created = await store.upsert_provider(
            provider="glm",
            key_id="b308",
            secret_ref="secret://glm",
            enabled=True,
            actor_id="admin",
            base_url=url,
            base_url_unverified=True,
        )
        assert (created.base_url, created.base_url_unverified) == (url, True)
        fetched = await store.get_provider("glm", "b308")
        assert fetched is not None
        assert (fetched.base_url, fetched.base_url_unverified) == (url, True)
        cleared = await store.upsert_provider(
            provider="glm", key_id="b308", secret_ref="secret://glm", enabled=True, actor_id="a"
        )
        assert (cleared.base_url, cleared.base_url_unverified) == (None, False)

        override = await store.upsert_tenant_provider(
            tenant_id=tenant,
            provider="glm",
            secret_ref="secret://t/glm",
            enabled=True,
            actor_id="admin",
            base_url=url,
            base_url_unverified=True,
        )
        assert (override.base_url, override.base_url_unverified) == (url, True)
        assert await store.list_tenant_providers(tenant) == [override]
        reset = await store.upsert_tenant_provider(
            tenant_id=tenant,
            provider="glm",
            secret_ref="secret://t/glm",
            enabled=True,
            actor_id="a",
        )
        assert (reset.base_url, reset.base_url_unverified) == (None, False)
    finally:
        await store.delete_provider("glm", "b308")
        await store.delete_tenant_provider(tenant_id=tenant, provider="glm")
        await engine.dispose()
