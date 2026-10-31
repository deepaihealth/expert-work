"""Integration tests for the SQL MCP connector catalog store — Stream W.

The catalog is a platform (NULL-tenant) table. Under the ``IS NOT DISTINCT
FROM`` RLS policy, a session with **no** ``app.tenant_id`` set
(``current_tenant_id_var=None``) can read/write the NULL-tenant rows — that is
the path the control-plane uses via ``bypass_rls_session()``. These CRUD tests
therefore run on the unprivileged app role with the tenant context unset.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlunparse
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import AsyncEngine
from testcontainers.postgres import PostgresContainer

from expert_work.persistence import (
    DatabaseConfig,
    create_async_engine_from_config,
    create_async_session_factory,
)
from expert_work.persistence.mcp_connector_catalog import (
    McpConnectorCatalogAlreadyExistsError,
    McpConnectorCatalogNotFoundError,
    SqlMcpConnectorCatalogStore,
)
from expert_work.persistence.rls import build_rls_sessionmaker, current_tenant_id_var
from expert_work.protocol import (
    McpConnectorAuthField,
    McpConnectorAuthSchema,
    McpConnectorCatalogPatch,
    McpConnectorCatalogUpsert,
    TenantPlan,
)

pytestmark = pytest.mark.integration

ALEMBIC_INI = Path(__file__).resolve().parent.parent / "alembic.ini"

APP_ROLE = "expert_work_app"
APP_PASSWORD = "expert_work_app_test_pw"  # test-only fixture password


def _sync_dsn(container: PostgresContainer) -> str:
    url = str(container.get_connection_url())
    return url.replace("+psycopg2", "+psycopg").replace("postgresql://", "postgresql+psycopg://", 1)


def _async_dsn(container: PostgresContainer) -> str:
    url = str(container.get_connection_url())
    return url.replace("+psycopg2", "+asyncpg").replace("postgresql://", "postgresql+asyncpg://", 1)


def _rewrite_credentials(dsn: str, user: str, password: str) -> str:
    parsed = urlparse(dsn)
    new_netloc = f"{user}:{password}@{parsed.hostname}"
    if parsed.port is not None:
        new_netloc = f"{new_netloc}:{parsed.port}"
    return urlunparse(parsed._replace(netloc=new_netloc))


def _provision_app_role(sync_dsn: str) -> None:
    admin_engine = create_engine(sync_dsn, isolation_level="AUTOCOMMIT")
    try:
        with admin_engine.connect() as conn:
            exists = conn.execute(
                text("SELECT 1 FROM pg_roles WHERE rolname = :role"),
                {"role": APP_ROLE},
            ).first()
            if exists is None:
                conn.execute(text(f"CREATE ROLE {APP_ROLE} LOGIN PASSWORD '{APP_PASSWORD}'"))
            conn.execute(text(f"GRANT USAGE ON SCHEMA public TO {APP_ROLE}"))
            conn.execute(
                text(
                    f"GRANT SELECT, INSERT, UPDATE, DELETE "
                    f"ON ALL TABLES IN SCHEMA public TO {APP_ROLE}"
                )
            )
            conn.execute(
                text(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {APP_ROLE}")
            )
    finally:
        admin_engine.dispose()


@pytest.fixture
def catalog_store(
    postgres_container: PostgresContainer,
) -> Iterator[tuple[SqlMcpConnectorCatalogStore, AsyncEngine]]:
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", _sync_dsn(postgres_container))
    command.upgrade(cfg, "head")
    _provision_app_role(_sync_dsn(postgres_container))

    app_dsn = _rewrite_credentials(_async_dsn(postgres_container), APP_ROLE, APP_PASSWORD)
    engine = create_async_engine_from_config(DatabaseConfig(dsn=app_dsn))
    sf = build_rls_sessionmaker(create_async_session_factory(engine))
    yield SqlMcpConnectorCatalogStore(sf), engine


@pytest.fixture(autouse=True)
def reset_rls() -> Iterator[None]:
    # NULL tenant context = the platform read/write path for this table.
    tok = current_tenant_id_var.set(None)
    try:
        yield
    finally:
        current_tenant_id_var.reset(tok)


def _slug() -> str:
    """A unique, slug-rule-valid catalog name (session-scoped DB is shared)."""
    return f"conn-{uuid4().hex[:12]}"


def _bearer_upsert(**over: Any) -> McpConnectorCatalogUpsert:
    kwargs: dict[str, Any] = {
        "name": _slug(),
        "display_name": "GitHub",
        "transport": "streamable_http",
        "url_template": "https://api.github.com/{org}/mcp",
        "auth_type": "bearer",
        "auth_schema": McpConnectorAuthSchema(
            fields=[
                McpConnectorAuthField(key="token", label="API Token", kind="secret"),
                McpConnectorAuthField(key="org", label="Organization", kind="param"),
            ]
        ),
        "required_tier": TenantPlan.PRO,
    }
    kwargs.update(over)
    return McpConnectorCatalogUpsert(**kwargs)


@pytest.mark.asyncio
async def test_create_get_round_trip(
    catalog_store: tuple[SqlMcpConnectorCatalogStore, AsyncEngine],
) -> None:
    store, engine = catalog_store
    try:
        created = await store.create(upsert=_bearer_upsert(), actor_id="sysadmin")
        assert created.tenant_id is None
        assert created.required_tier is TenantPlan.PRO
        # auth_schema JSONB round-trips.
        assert [f.key for f in created.auth_schema.secret_fields()] == ["token"]
        got = await store.get_by_id(created.id)
        assert got is not None and got.id == created.id
        by_name = await store.get_by_name(created.name)
        assert by_name is not None and by_name.id == created.id
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_duplicate_name_rejected(
    catalog_store: tuple[SqlMcpConnectorCatalogStore, AsyncEngine],
) -> None:
    store, engine = catalog_store
    try:
        name = _slug()
        await store.create(upsert=_bearer_upsert(name=name), actor_id="sysadmin")
        with pytest.raises(McpConnectorCatalogAlreadyExistsError):
            await store.create(upsert=_bearer_upsert(name=name), actor_id="sysadmin")
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_list_and_category_filter(
    catalog_store: tuple[SqlMcpConnectorCatalogStore, AsyncEngine],
) -> None:
    store, engine = catalog_store
    try:
        # Unique category isolates these rows from any left by other tests on
        # the session-scoped DB; list() returns name-sorted.
        cat = f"cat-{uuid4().hex[:8]}"
        await store.create(upsert=_bearer_upsert(name="zeta-x", category=cat), actor_id="s")
        await store.create(upsert=_bearer_upsert(name="alpha-x", category=cat), actor_id="s")
        await store.create(upsert=_bearer_upsert(name="gamma-x", category="other"), actor_id="s")
        assert [r.name for r in await store.list(category=cat)] == ["alpha-x", "zeta-x"]
        all_in_cat = [r.name for r in await store.list() if r.category == cat]
        assert all_in_cat == ["alpha-x", "zeta-x"]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_update_and_delete(
    catalog_store: tuple[SqlMcpConnectorCatalogStore, AsyncEngine],
) -> None:
    store, engine = catalog_store
    try:
        created = await store.create(upsert=_bearer_upsert(), actor_id="sysadmin")
        updated = await store.update(
            catalog_id=created.id,
            patch=McpConnectorCatalogPatch(display_name="GitHub (Official)", enabled=False),
        )
        assert updated.display_name == "GitHub (Official)"
        assert updated.enabled is False
        await store.delete(created.id)
        assert await store.get_by_id(created.id) is None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_update_absent_raises(
    catalog_store: tuple[SqlMcpConnectorCatalogStore, AsyncEngine],
) -> None:
    store, engine = catalog_store
    try:
        with pytest.raises(McpConnectorCatalogNotFoundError):
            await store.update(catalog_id=uuid4(), patch=McpConnectorCatalogPatch(enabled=False))
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_delete_absent_raises(
    catalog_store: tuple[SqlMcpConnectorCatalogStore, AsyncEngine],
) -> None:
    store, engine = catalog_store
    try:
        with pytest.raises(McpConnectorCatalogNotFoundError):
            await store.delete(uuid4())
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_update_invalid_patch_rolls_back(
    catalog_store: tuple[SqlMcpConnectorCatalogStore, AsyncEngine],
) -> None:
    """A patch breaking the bearer↔secret-field invariant raises and the SQL
    transaction rolls back — the persisted row is untouched (validate-before-commit)."""
    store, engine = catalog_store
    try:
        created = await store.create(upsert=_bearer_upsert(), actor_id="sysadmin")
        with pytest.raises(ValueError, match="exactly one secret field"):
            await store.update(
                catalog_id=created.id,
                patch=McpConnectorCatalogPatch(auth_schema=McpConnectorAuthSchema()),
            )
        after = await store.get_by_id(created.id)
        assert after is not None
        assert [f.key for f in after.auth_schema.fields] == ["token", "org"]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_concurrent_updates_never_see_the_same_before(
    catalog_store: tuple[SqlMcpConnectorCatalogStore, AsyncEngine],
) -> None:
    """B-248: the API acts on a real ``enabled`` switch; two admins disabling
    at once must not both see ``enabled=True`` as ``before`` (row lock)."""
    import asyncio

    store, engine = catalog_store
    try:
        for _ in range(5):
            created = await store.create(upsert=_bearer_upsert(), actor_id="sysadmin")
            off = McpConnectorCatalogPatch(enabled=False)
            results = await asyncio.gather(
                *(store.update_with_previous(catalog_id=created.id, patch=off) for _ in range(3))
            )
            assert sorted(before.enabled for before, _ in results) == [False, False, True]
            assert all(after.enabled is False for _, after in results)
    finally:
        await engine.dispose()
