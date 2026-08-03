"""Integration (RLS) tests for the SQL tenant MCP server store.

Mirrors test_sql_tenant_config_store.py fixture setup: postgres_container
is session-scoped (root conftest.py), app role provisioned via psycopg sync
engine, RLS sessionmaker built from the app-role async DSN.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlunparse
from uuid import UUID, uuid4

import pytest
import sqlalchemy.exc
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
from expert_work.persistence.mcp_connector_catalog import SqlMcpConnectorCatalogStore
from expert_work.persistence.rls import build_rls_sessionmaker, current_tenant_id_var
from expert_work.persistence.tenant_mcp_server import (
    SqlTenantMcpServerStore,
    TenantMcpServerAlreadyExistsError,
)
from expert_work.protocol import McpConnectorCatalogUpsert, TenantMcpServerPatch

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
def tenant_mcp_server_store(
    postgres_container: PostgresContainer,
) -> Iterator[tuple[SqlTenantMcpServerStore, AsyncEngine]]:
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", _sync_dsn(postgres_container))
    command.upgrade(cfg, "head")
    _provision_app_role(_sync_dsn(postgres_container))

    app_dsn = _rewrite_credentials(_async_dsn(postgres_container), APP_ROLE, APP_PASSWORD)
    engine = create_async_engine_from_config(DatabaseConfig(dsn=app_dsn))
    sf = build_rls_sessionmaker(create_async_session_factory(engine))
    yield SqlTenantMcpServerStore(sf), engine


@pytest.fixture
def tenant_mcp_server_with_catalog(
    postgres_container: PostgresContainer,
) -> Iterator[tuple[SqlTenantMcpServerStore, SqlMcpConnectorCatalogStore, AsyncEngine]]:
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", _sync_dsn(postgres_container))
    command.upgrade(cfg, "head")
    _provision_app_role(_sync_dsn(postgres_container))

    app_dsn = _rewrite_credentials(_async_dsn(postgres_container), APP_ROLE, APP_PASSWORD)
    engine = create_async_engine_from_config(DatabaseConfig(dsn=app_dsn))
    sf = build_rls_sessionmaker(create_async_session_factory(engine))
    yield SqlTenantMcpServerStore(sf), SqlMcpConnectorCatalogStore(sf), engine


@pytest.fixture
def tenant_mcp_server_platform_scope(
    postgres_container: PostgresContainer,
) -> Iterator[tuple[SqlTenantMcpServerStore, SqlMcpConnectorCatalogStore, AsyncEngine]]:
    """Superuser-DSN session, no RLS wiring — mirrors
    ``test_sql_mcp_oauth_connection_store.py``'s ``sql_store`` fixture. The
    platform-scope ``count_for_catalog`` method (Task 8 review I-1) carries no
    ``tenant_id`` predicate by design and needs a session that actually sees
    every tenant's rows (an app-role RLS session would filter them out)."""
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", _sync_dsn(postgres_container))
    command.upgrade(cfg, "head")

    engine = create_async_engine_from_config(DatabaseConfig(dsn=_async_dsn(postgres_container)))
    sf = create_async_session_factory(engine)
    yield SqlTenantMcpServerStore(sf), SqlMcpConnectorCatalogStore(sf), engine


async def _make_catalog_entry(catalog_store: SqlMcpConnectorCatalogStore, *, name: str) -> UUID:
    entry = await catalog_store.create(
        upsert=McpConnectorCatalogUpsert(
            name=name,
            display_name=name.title(),
            transport="streamable_http",
            url_template=f"https://mcp.example.com/{name}",
            auth_type="none",
        ),
        actor_id="sysadmin",
    )
    return entry.id


@pytest.mark.asyncio
async def test_count_for_catalog_cross_tenant(
    tenant_mcp_server_platform_scope: tuple[
        SqlTenantMcpServerStore, SqlMcpConnectorCatalogStore, AsyncEngine
    ],
) -> None:
    """Platform-scope catalog method (MCP catalog delete-guard, Task 8 review
    I-1) counts across every tenant — no tenant_id predicate."""
    servers, catalog, engine = tenant_mcp_server_platform_scope
    try:
        cat_a = await _make_catalog_entry(catalog, name=f"conn-a-{uuid4().hex[:12]}")
        cat_b = await _make_catalog_entry(catalog, name=f"conn-b-{uuid4().hex[:12]}")
        tid_1, tid_2 = uuid4(), uuid4()

        await servers.create(
            tenant_id=tid_1,
            name="github",
            transport="streamable_http",
            url="https://mcp.example.com/a",
            auth_type="none",
            token_secret_ref=None,
            timeout_s=30.0,
            created_by="a@x",
            catalog_id=cat_a,
        )
        await servers.create(
            tenant_id=tid_2,
            name="github",
            transport="streamable_http",
            url="https://mcp.example.com/a",
            auth_type="none",
            token_secret_ref=None,
            timeout_s=30.0,
            created_by="b@x",
            catalog_id=cat_a,
        )
        await servers.create(
            tenant_id=tid_1,
            name="other",
            transport="streamable_http",
            url="https://mcp.example.com/b",
            auth_type="none",
            token_secret_ref=None,
            timeout_s=30.0,
            created_by="a@x",
            catalog_id=cat_b,
        )

        assert await servers.count_for_catalog(catalog_id=cat_a) == 2
        assert await servers.count_for_catalog(catalog_id=cat_b) == 1
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_list_all_tenants_spans_tenants_ordered(
    tenant_mcp_server_platform_scope: tuple[
        SqlTenantMcpServerStore, SqlMcpConnectorCatalogStore, AsyncEngine
    ],
) -> None:
    """W4 — the aggregate reader spans tenants, ordered ``(name, tenant_id)``
    (the same name may exist in several tenants). Superuser session mirrors
    the production posture (bypass GUC on the app's superuser connection)."""
    servers, _catalog, engine = tenant_mcp_server_platform_scope
    try:
        tid_1, tid_2 = uuid4(), uuid4()
        shared_name = f"agg-{uuid4().hex[:12]}"
        for tid in (tid_1, tid_2):
            await servers.create(
                tenant_id=tid,
                name=shared_name,
                transport="streamable_http",
                url="https://mcp.example.com/a",
                auth_type="none",
                token_secret_ref=None,
                timeout_s=30.0,
                created_by="a@x",
            )

        rows = await servers.list_all_tenants()
        mine = [r for r in rows if r.tenant_id in {tid_1, tid_2}]
        assert [r.name for r in mine] == [shared_name, shared_name]
        # tenant_id tiebreak: Postgres uuid byte order == UUID.int order.
        assert [r.tenant_id for r in mine] == sorted((tid_1, tid_2), key=lambda u: u.int)
        # Per-tenant sibling stays scoped.
        assert [r.tenant_id for r in await servers.list_for_tenant(tenant_id=tid_1)] == [tid_1]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_count_for_catalog_empty(
    tenant_mcp_server_platform_scope: tuple[
        SqlTenantMcpServerStore, SqlMcpConnectorCatalogStore, AsyncEngine
    ],
) -> None:
    servers, catalog, engine = tenant_mcp_server_platform_scope
    try:
        empty_cat = await _make_catalog_entry(catalog, name=f"conn-empty-{uuid4().hex[:12]}")
        assert await servers.count_for_catalog(catalog_id=empty_cat) == 0
    finally:
        await engine.dispose()


@pytest.fixture(autouse=True)
def reset_rls() -> Iterator[None]:
    tok = current_tenant_id_var.set(None)
    try:
        yield
    finally:
        current_tenant_id_var.reset(tok)


@pytest.mark.asyncio
async def test_create_get_round_trip(
    tenant_mcp_server_store: tuple[SqlTenantMcpServerStore, AsyncEngine],
) -> None:
    store, engine = tenant_mcp_server_store
    try:
        tid = uuid4()
        current_tenant_id_var.set(tid)
        created = await store.create(
            tenant_id=tid,
            name="github",
            transport="streamable_http",
            url="https://mcp.example.com/mcp",
            auth_type="bearer",
            token_secret_ref="secret://expert-work/t/mcp/github/token",
            timeout_s=30.0,
            created_by="admin@acme",
        )
        assert created.name == "github"
        got = await store.get(tenant_id=tid, name="github")
        assert got is not None and got.id == created.id
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_rls_isolation_between_tenants(
    tenant_mcp_server_store: tuple[SqlTenantMcpServerStore, AsyncEngine],
) -> None:
    store, engine = tenant_mcp_server_store
    a, b = uuid4(), uuid4()

    current_tenant_id_var.set(a)
    try:
        await store.create(
            tenant_id=a,
            name="github",
            transport="streamable_http",
            url="https://a.example.com/mcp",
            auth_type="none",
            token_secret_ref=None,
            timeout_s=30.0,
            created_by="a@x",
        )
    finally:
        current_tenant_id_var.set(None)

    # Tenant B must NOT see tenant A's row.
    current_tenant_id_var.set(b)
    try:
        assert await store.get(tenant_id=a, name="github") is None
        assert await store.list_for_tenant(tenant_id=a) == []
    finally:
        current_tenant_id_var.set(None)
        await engine.dispose()


@pytest.mark.asyncio
async def test_duplicate_name_rejected(
    tenant_mcp_server_store: tuple[SqlTenantMcpServerStore, AsyncEngine],
) -> None:
    store, engine = tenant_mcp_server_store
    try:
        tid = uuid4()
        current_tenant_id_var.set(tid)
        kwargs: dict[str, Any] = {
            "tenant_id": tid,
            "name": "github",
            "transport": "streamable_http",
            "url": "https://a.example.com/mcp",
            "auth_type": "none",
            "token_secret_ref": None,
            "timeout_s": 30.0,
            "created_by": "a@x",
        }
        await store.create(**kwargs)
        with pytest.raises(TenantMcpServerAlreadyExistsError):
            await store.create(**kwargs)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_update_and_delete(
    tenant_mcp_server_store: tuple[SqlTenantMcpServerStore, AsyncEngine],
) -> None:
    store, engine = tenant_mcp_server_store
    try:
        tid = uuid4()
        current_tenant_id_var.set(tid)
        await store.create(
            tenant_id=tid,
            name="github",
            transport="streamable_http",
            url="https://a.example.com/mcp",
            auth_type="none",
            token_secret_ref=None,
            timeout_s=30.0,
            created_by="a@x",
        )
        updated = await store.update(
            tenant_id=tid,
            name="github",
            patch=TenantMcpServerPatch(enabled=False, url="https://b.example.com/mcp"),
        )
        assert updated.enabled is False
        assert updated.url == "https://b.example.com/mcp"
        await store.delete(tenant_id=tid, name="github")
        assert await store.get(tenant_id=tid, name="github") is None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_update_invalid_patch_rejects_atomically(
    tenant_mcp_server_store: tuple[SqlTenantMcpServerStore, AsyncEngine],
) -> None:
    store, engine = tenant_mcp_server_store
    tid = uuid4()
    current_tenant_id_var.set(tid)
    try:
        await store.create(
            tenant_id=tid,
            name="github",
            transport="streamable_http",
            url="https://a.example.com/mcp",
            auth_type="none",
            token_secret_ref=None,
            timeout_s=30.0,
            created_by="a@x",
        )
        with pytest.raises(ValueError):
            await store.update(
                tenant_id=tid,
                name="github",
                patch=TenantMcpServerPatch(token_secret_ref="secret://x"),
            )
        # atomic reject: the row must be unchanged
        unchanged = await store.get(tenant_id=tid, name="github")
        assert unchanged is not None
        assert unchanged.token_secret_ref is None
        assert unchanged.auth_type == "none"
    finally:
        current_tenant_id_var.set(None)
        await engine.dispose()


@pytest.mark.asyncio
async def test_create_with_catalog_id_round_trips(
    tenant_mcp_server_with_catalog: tuple[
        SqlTenantMcpServerStore, SqlMcpConnectorCatalogStore, AsyncEngine
    ],
) -> None:
    servers, catalog, engine = tenant_mcp_server_with_catalog
    try:
        # Platform catalog row written under an UNSCOPED (NULL-tenant) session.
        current_tenant_id_var.set(None)
        entry = await catalog.create(
            upsert=McpConnectorCatalogUpsert(
                name="github",
                display_name="GitHub",
                transport="streamable_http",
                url_template="https://api.github.com/mcp",
                auth_type="none",
            ),
            actor_id="sysadmin",
        )

        tid = uuid4()
        current_tenant_id_var.set(tid)
        created = await servers.create(
            tenant_id=tid,
            name="github",
            transport="streamable_http",
            url="https://api.github.com/mcp",
            auth_type="none",
            token_secret_ref=None,
            timeout_s=30.0,
            created_by="admin@acme",
            catalog_id=entry.id,
        )
        assert created.catalog_id == entry.id
        got = await servers.get(tenant_id=tid, name="github")
        assert got is not None and got.catalog_id == entry.id
    finally:
        current_tenant_id_var.set(None)
        await engine.dispose()


@pytest.mark.asyncio
async def test_rls_blocks_cross_tenant_write(
    tenant_mcp_server_store: tuple[SqlTenantMcpServerStore, AsyncEngine],
) -> None:
    store, engine = tenant_mcp_server_store
    a, b = uuid4(), uuid4()
    current_tenant_id_var.set(b)  # session scoped to tenant B
    try:
        with pytest.raises(sqlalchemy.exc.DBAPIError):  # INSERT WITH CHECK rejects tenant_id=a
            await store.create(
                tenant_id=a,  # claims tenant A while the session is B
                name="injected",
                transport="streamable_http",
                url="https://evil.example.com/mcp",
                auth_type="none",
                token_secret_ref=None,
                timeout_s=30.0,
                created_by="attacker",
            )
    finally:
        current_tenant_id_var.set(None)
        await engine.dispose()


@pytest.mark.asyncio
async def test_record_probe_result_persists_health(
    tenant_mcp_server_store: tuple[SqlTenantMcpServerStore, AsyncEngine],
) -> None:
    """record_probe_result persists health columns (migration 0064 + CHECK) and
    leaves updated_at untouched (a probe is not a config change)."""
    from datetime import UTC, datetime

    store, engine = tenant_mcp_server_store
    try:
        tid = uuid4()
        current_tenant_id_var.set(tid)
        created = await store.create(
            tenant_id=tid,
            name="github",
            transport="streamable_http",
            url="https://mcp.example.com/mcp",
            auth_type="none",
            token_secret_ref=None,
            timeout_s=30.0,
            created_by="admin@acme",
        )
        assert created.last_probe_status is None  # never probed yet

        at_err = datetime(2026, 6, 6, 10, 0, tzinfo=UTC)
        await store.record_probe_result(
            tenant_id=tid, name="github", status="error", probed_at=at_err, error="MCP_PROBE_X"
        )
        at_ok = datetime(2026, 6, 6, 11, 0, tzinfo=UTC)
        ok = await store.record_probe_result(
            tenant_id=tid, name="github", status="ok", probed_at=at_ok
        )
        assert ok.last_probe_status == "ok"
        assert ok.last_probe_error is None  # ok clears the prior error
        assert ok.updated_at == created.updated_at  # not bumped by a probe

        got = await store.get(tenant_id=tid, name="github")
        assert got is not None and got.last_probe_status == "ok"
    finally:
        await engine.dispose()
