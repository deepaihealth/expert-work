"""Integration test: RLS isolation against a real Postgres.

Spins up the session-scoped ``postgres_container``, applies all
alembic migrations (so 0005_rls_baseline runs and enables RLS), then
verifies that:

* Two distinct tenant ids inserted with one factory + ContextVar
  setter pair are mutually invisible to each other.
* Setting the ContextVar to ``None`` returns zero rows (fail-closed).
* ``bypass_rls_var=True`` does NOT bypass RLS for a non-BYPASSRLS
  role — the policy still enforces; only the migration-level admin
  role attribute (BYPASSRLS) actually skips. This test pins that
  the application code can't accidentally subvert RLS by flipping a
  ContextVar.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse, urlunparse
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from testcontainers.postgres import PostgresContainer

from expert_work.persistence import (
    DatabaseConfig,
    create_async_engine_from_config,
    create_async_session_factory,
)
from expert_work.persistence.artifact import SqlArtifactStore
from expert_work.persistence.embedding import EMBEDDING_DIM
from expert_work.persistence.feedback_store import DbFeedbackStore, FeedbackRecord
from expert_work.persistence.memory import SqlMemoryStore
from expert_work.persistence.rls import (
    build_rls_sessionmaker,
    bypass_rls_var,
    current_tenant_id_var,
    current_user_id_var,
)
from expert_work.persistence.tenant_user import SqlTenantUserStore
from expert_work.persistence.thread_meta import SqlThreadMetaStore
from expert_work.protocol import MemoryItem

pytestmark = pytest.mark.integration

ALEMBIC_INI = Path(__file__).resolve().parent.parent / "alembic.ini"

# Non-superuser application role created per fixture call. Postgres
# superusers — which is what the testcontainers bootstrap user
# defaults to — bypass RLS unconditionally regardless of FORCE ROW
# LEVEL SECURITY. We must connect as a normal role so the policies
# actually run.
APP_ROLE = "expert_work_app"
APP_PASSWORD = "expert_work_app_test_pw"  # test-only fixture password


def _sync_dsn(container: PostgresContainer) -> str:
    url = str(container.get_connection_url())
    return url.replace("+psycopg2", "+psycopg").replace("postgresql://", "postgresql+psycopg://", 1)


def _async_dsn(container: PostgresContainer) -> str:
    url = str(container.get_connection_url())
    return url.replace("+psycopg2", "+asyncpg").replace("postgresql://", "postgresql+asyncpg://", 1)


def _rewrite_credentials(dsn: str, user: str, password: str) -> str:
    """Return ``dsn`` with userinfo replaced by ``user:password``."""
    parsed = urlparse(dsn)
    new_netloc = f"{user}:{password}@{parsed.hostname}"
    if parsed.port is not None:
        new_netloc = f"{new_netloc}:{parsed.port}"
    return urlunparse(parsed._replace(netloc=new_netloc))


def _provision_app_role(sync_dsn: str) -> None:
    """Create the non-superuser ``expert_work_app`` role and grant it CRUD on the schema.

    Idempotent: the existence check short-circuits if the same role
    has been provisioned earlier in the test session (session-scoped
    ``postgres_container`` may host multiple fixtures).
    """
    admin_engine = create_engine(sync_dsn, isolation_level="AUTOCOMMIT")
    try:
        with admin_engine.connect() as conn:
            exists = conn.execute(
                text("SELECT 1 FROM pg_roles WHERE rolname = :role"),
                {"role": APP_ROLE},
            ).first()
            if exists is None:
                # ``APP_ROLE`` / ``APP_PASSWORD`` are module-level
                # constants under our control, not external input —
                # safe to interpolate.
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
            # Membership lets the app role ``SET LOCAL ROLE audit_reader``
            # for cross-tenant scans (Stream HX-2 feedback worker) — the
            # per-deployment grant 0061 documents, mirrored here for the
            # test role (same as test_billing_ledger_rls_integration).
            conn.execute(text(f"GRANT audit_reader TO {APP_ROLE}"))
    finally:
        admin_engine.dispose()


@pytest.fixture
def rls_store(
    postgres_container: PostgresContainer,
) -> Iterator[tuple[SqlThreadMetaStore, AsyncEngine]]:
    # 1. Run migrations as the bootstrap (superuser) account so DDL —
    #    including ``CREATE POLICY`` / ``ALTER TABLE ... FORCE`` /
    #    ``CREATE ROLE audit_reader`` — is allowed.
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", _sync_dsn(postgres_container))
    command.upgrade(cfg, "head")

    # 2. Provision a non-superuser application role. RLS only enforces
    #    against non-superuser, non-BYPASSRLS roles; the testcontainers
    #    bootstrap user is superuser and would silently skip the
    #    policies.
    _provision_app_role(_sync_dsn(postgres_container))

    # 3. Build the application engine using the unprivileged role.
    app_async_dsn = _rewrite_credentials(_async_dsn(postgres_container), APP_ROLE, APP_PASSWORD)
    engine = create_async_engine_from_config(DatabaseConfig(dsn=app_async_dsn))
    session_factory = build_rls_sessionmaker(create_async_session_factory(engine))
    yield SqlThreadMetaStore(session_factory), engine


@pytest.fixture(autouse=True)
def reset_rls_context() -> Iterator[None]:
    t1 = current_tenant_id_var.set(None)
    t2 = bypass_rls_var.set(False)
    t3 = current_user_id_var.set(None)
    try:
        yield
    finally:
        current_tenant_id_var.reset(t1)
        bypass_rls_var.reset(t2)
        current_user_id_var.reset(t3)


async def _seed(store: SqlThreadMetaStore, tenant_id: UUID) -> UUID:
    """Insert one thread_meta row for ``tenant_id`` and return its thread_id."""
    thread_id = uuid4()
    await store.create(
        thread_id=thread_id,
        tenant_id=tenant_id,
        created_by="rls-test",
    )
    return thread_id


@pytest.mark.asyncio
async def test_tenants_cannot_see_each_other(
    rls_store: tuple[SqlThreadMetaStore, AsyncEngine],
) -> None:
    store, engine = rls_store
    try:
        tenant_a, tenant_b = uuid4(), uuid4()

        current_tenant_id_var.set(tenant_a)
        id_a = await _seed(store, tenant_a)

        current_tenant_id_var.set(tenant_b)
        id_b = await _seed(store, tenant_b)

        # A scoped to its own tenant: own row visible, other tenant invisible.
        current_tenant_id_var.set(tenant_a)
        assert await store.get(id_a, tenant_id=tenant_a) is not None
        assert await store.get(id_b, tenant_id=tenant_a) is None

        current_tenant_id_var.set(tenant_b)
        assert await store.get(id_b, tenant_id=tenant_b) is not None
        assert await store.get(id_a, tenant_id=tenant_b) is None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_unset_tenant_id_returns_no_rows(
    rls_store: tuple[SqlThreadMetaStore, AsyncEngine],
) -> None:
    store, engine = rls_store
    try:
        tenant_a = uuid4()
        current_tenant_id_var.set(tenant_a)
        await _seed(store, tenant_a)

        # Without a tenant in context, set_config is skipped. The
        # ``USING (tenant_id = current_setting('app.tenant_id', true)::uuid)``
        # predicate then evaluates to NULL → policy filters everything.
        current_tenant_id_var.set(None)
        listed = await store.list_by_tenant(tenant_a, limit=10, offset=0)
        assert listed == []
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_bypass_var_does_not_subvert_rls_for_application_role(
    rls_store: tuple[SqlThreadMetaStore, AsyncEngine],
) -> None:
    """``bypass_rls_var`` only skips the application-side ``SET LOCAL``;
    it does NOT change the connection's role. The default test user is
    not BYPASSRLS, so even with the flag the policy still applies.
    """
    store, engine = rls_store
    try:
        tenant_a, tenant_b = uuid4(), uuid4()

        current_tenant_id_var.set(tenant_a)
        await _seed(store, tenant_a)

        # Now claim "I want to bypass" but stay on the non-BYPASSRLS
        # application role. The set_config call is skipped, so
        # ``current_setting('app.tenant_id', true)`` is ``''`` → the
        # policy denies (zero rows seen — A's row is not visible from
        # an unset session even with bypass).
        current_tenant_id_var.set(tenant_b)
        bypass_rls_var.set(True)
        listed = await store.list_by_tenant(tenant_a, limit=10, offset=0)
        assert listed == []
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# feedback table — Stream G.6 (#64)
# ---------------------------------------------------------------------------


@pytest.fixture
def feedback_rls_store(
    postgres_container: PostgresContainer,
) -> Iterator[tuple[DbFeedbackStore, AsyncEngine]]:
    """A :class:`DbFeedbackStore` on the unprivileged role — RLS enforced."""
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", _sync_dsn(postgres_container))
    command.upgrade(cfg, "head")
    _provision_app_role(_sync_dsn(postgres_container))
    app_async_dsn = _rewrite_credentials(_async_dsn(postgres_container), APP_ROLE, APP_PASSWORD)
    engine = create_async_engine_from_config(DatabaseConfig(dsn=app_async_dsn))
    session_factory = build_rls_sessionmaker(create_async_session_factory(engine))
    yield DbFeedbackStore(session_factory), engine


@pytest.mark.asyncio
async def test_feedback_tenants_cannot_see_each_other(
    feedback_rls_store: tuple[DbFeedbackStore, AsyncEngine],
) -> None:
    """#64 — two tenants' feedback on the *same* thread id stays isolated.

    Using one shared ``thread_id`` proves it is RLS, not the thread
    filter, that isolates: ``list_for_thread`` carries no tenant
    predicate, so a leak would surface both rows.
    """
    store, engine = feedback_rls_store
    try:
        tenant_a, tenant_b = uuid4(), uuid4()
        thread_id = uuid4()

        current_tenant_id_var.set(tenant_a)
        await store.insert(
            FeedbackRecord(tenant_id=tenant_a, thread_id=thread_id, rating="up", actor_id="user-a")
        )

        current_tenant_id_var.set(tenant_b)
        await store.insert(
            FeedbackRecord(
                tenant_id=tenant_b, thread_id=thread_id, rating="down", actor_id="user-b"
            )
        )

        current_tenant_id_var.set(tenant_a)
        a_rows = await store.list_for_thread(thread_id=thread_id)
        assert [r.rating for r in a_rows] == ["up"]
        assert all(r.tenant_id == tenant_a for r in a_rows)

        current_tenant_id_var.set(tenant_b)
        b_rows = await store.list_for_thread(thread_id=thread_id)
        assert [r.rating for r in b_rows] == ["down"]
        assert all(r.tenant_id == tenant_b for r in b_rows)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_feedback_down_rated_threads_rls_scoped(
    feedback_rls_store: tuple[DbFeedbackStore, AsyncEngine],
) -> None:
    """Stream HX-2 (Mini-ADR HX-B2) — the rollback gate's 👎 join is
    tenant-scoped by RLS: tenant B's 👎 on the same thread id never
    bleeds into tenant A's window, and an up-only thread never matches."""
    store, engine = feedback_rls_store
    try:
        tenant_a, tenant_b = uuid4(), uuid4()
        shared, up_only = uuid4(), uuid4()

        current_tenant_id_var.set(tenant_a)
        await store.insert(
            FeedbackRecord(tenant_id=tenant_a, thread_id=up_only, rating="up", actor_id="user-a")
        )

        current_tenant_id_var.set(tenant_b)
        await store.insert(
            FeedbackRecord(tenant_id=tenant_b, thread_id=shared, rating="down", actor_id="user-b")
        )

        current_tenant_id_var.set(tenant_a)
        assert await store.down_rated_threads(thread_ids=[shared, up_only]) == set()

        current_tenant_id_var.set(tenant_b)
        assert await store.down_rated_threads(thread_ids=[shared, up_only]) == {shared}
        assert await store.down_rated_threads(thread_ids=[]) == set()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_feedback_upsert_and_scoped_reads_isolated_by_tenant(
    feedback_rls_store: tuple[DbFeedbackStore, AsyncEngine],
) -> None:
    """P-2 — 同一 (run, actor) 在两个租户各自 upsert 成一行。

    租户 A 的 scoped 读与 ``down_rated_thread_ids`` 都看不到 B 的 👎。
    """
    store, engine = feedback_rls_store
    try:
        tenant_a, tenant_b = uuid4(), uuid4()
        thread_id, run_id = uuid4(), uuid4()

        current_tenant_id_var.set(tenant_a)
        await store.upsert(
            FeedbackRecord(
                tenant_id=tenant_a, thread_id=thread_id, run_id=run_id, rating="up", actor_id="x"
            )
        )
        current_tenant_id_var.set(tenant_b)
        _, updated = await store.upsert(
            FeedbackRecord(
                tenant_id=tenant_b, thread_id=thread_id, run_id=run_id, rating="down", actor_id="x"
            )
        )
        assert updated is False  # 不是覆盖 A 的行

        current_tenant_id_var.set(tenant_a)
        a_rows = await store.list_for_thread_scoped(tenant_id=tenant_a, thread_id=thread_id)
        assert [r.rating for r in a_rows] == ["up"]
        assert await store.down_rated_thread_ids(tenant_id=tenant_a) == set()

        current_tenant_id_var.set(tenant_b)
        assert await store.down_rated_thread_ids(tenant_id=tenant_b) == {thread_id}
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_feedback_worker_scan_and_stamp_lifecycle(
    feedback_rls_store: tuple[DbFeedbackStore, AsyncEngine],
) -> None:
    """Stream HX-2 (Mini-ADR HX-B1) — the FeedbackConsumerWorker's SQL
    mechanics against real FORCE-RLS:

    * the cross-tenant unprocessed-👎 scan sees BOTH tenants' rows
      (``SET LOCAL ROLE audit_reader`` inside the store; a bare bypass
      would read zero rows silently),
    * ``mark_processed`` is a tenant-scoped write — and the stamped row
      drops out of the next scan.
    """
    store, engine = feedback_rls_store
    try:
        tenant_a, tenant_b = uuid4(), uuid4()
        thread_a, thread_b = uuid4(), uuid4()

        current_tenant_id_var.set(tenant_a)
        row_a = await store.insert(
            FeedbackRecord(tenant_id=tenant_a, thread_id=thread_a, rating="down", actor_id="a")
        )
        current_tenant_id_var.set(tenant_b)
        await store.insert(
            FeedbackRecord(tenant_id=tenant_b, thread_id=thread_b, rating="down", actor_id="b")
        )
        await store.insert(
            FeedbackRecord(tenant_id=tenant_b, thread_id=thread_b, rating="up", actor_id="b")
        )

        # Cross-tenant enumeration under a bypass scope (no tenant GUC).
        # The session-scoped container carries unprocessed 👎 rows from
        # earlier tests — filter to this test's tenants before asserting.
        current_tenant_id_var.set(None)
        bypass_token = bypass_rls_var.set(True)
        try:
            rows = await store.list_unprocessed_down_all_tenants(limit=50)
        finally:
            bypass_rls_var.reset(bypass_token)
        ours = [r for r in rows if r.tenant_id in {tenant_a, tenant_b}]
        assert {r.tenant_id for r in ours} == {tenant_a, tenant_b}
        assert all(r.rating == "down" for r in rows)

        # Stamp tenant A's row under its own scope; next scan excludes it.
        current_tenant_id_var.set(tenant_a)
        assert row_a.id is not None
        assert await store.mark_processed(feedback_id=row_a.id, processed_at=datetime.now(UTC))

        current_tenant_id_var.set(None)
        bypass_token = bypass_rls_var.set(True)
        try:
            remaining = await store.list_unprocessed_down_all_tenants(limit=50)
        finally:
            bypass_rls_var.reset(bypass_token)
        ours = [r for r in remaining if r.tenant_id in {tenant_a, tenant_b}]
        assert {r.tenant_id for r in ours} == {tenant_b}
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# tenant_user table — Stream J.14
# ---------------------------------------------------------------------------


@pytest.fixture
def tenant_user_rls_store(
    postgres_container: PostgresContainer,
) -> Iterator[tuple[SqlTenantUserStore, async_sessionmaker[AsyncSession], AsyncEngine]]:
    """A :class:`SqlTenantUserStore` on the unprivileged role — RLS enforced."""
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", _sync_dsn(postgres_container))
    command.upgrade(cfg, "head")
    _provision_app_role(_sync_dsn(postgres_container))
    app_async_dsn = _rewrite_credentials(_async_dsn(postgres_container), APP_ROLE, APP_PASSWORD)
    engine = create_async_engine_from_config(DatabaseConfig(dsn=app_async_dsn))
    session_factory = build_rls_sessionmaker(create_async_session_factory(engine))
    yield SqlTenantUserStore(session_factory), session_factory, engine


@pytest.mark.asyncio
async def test_tenant_user_tenants_cannot_see_each_other(
    tenant_user_rls_store: tuple[SqlTenantUserStore, async_sessionmaker[AsyncSession], AsyncEngine],
) -> None:
    """Two tenants resolving the *same* subject_id stay isolated.

    A raw ``SELECT count(*) FROM tenant_user`` carries no tenant
    predicate, so a leak would surface both rows — this proves the RLS
    policy isolates, not the store's ``get`` filter.
    """
    store, session_factory, engine = tenant_user_rls_store
    try:
        tenant_a, tenant_b = uuid4(), uuid4()

        current_tenant_id_var.set(tenant_a)
        user_a = await store.resolve(
            tenant_id=tenant_a, subject_type="user", subject_id="shared-sub"
        )

        current_tenant_id_var.set(tenant_b)
        user_b = await store.resolve(
            tenant_id=tenant_b, subject_type="user", subject_id="shared-sub"
        )
        assert user_b.id != user_a.id

        # Raw count — no tenant predicate; RLS must scope it to 1.
        current_tenant_id_var.set(tenant_a)
        async with session_factory() as session:
            count_a = await session.scalar(text("SELECT count(*) FROM tenant_user"))
        assert count_a == 1

        # ``get`` for the other tenant's user resolves to nothing.
        assert await store.get(user_b.id, tenant_id=tenant_a) is None
        assert await store.get(user_a.id, tenant_id=tenant_a) is not None
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# memory_item table — Stream J.3 (tenant + user RLS)
# ---------------------------------------------------------------------------


def _vec(*head: float) -> tuple[float, ...]:
    """An ``EMBEDDING_DIM``-wide vector with ``head`` as its leading values."""
    return tuple(head) + (0.0,) * (EMBEDDING_DIM - len(head))


@pytest.fixture
def memory_rls_store(
    postgres_container: PostgresContainer,
) -> Iterator[tuple[SqlMemoryStore, async_sessionmaker[AsyncSession], AsyncEngine]]:
    """A :class:`SqlMemoryStore` on the unprivileged role — RLS enforced."""
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", _sync_dsn(postgres_container))
    command.upgrade(cfg, "head")
    _provision_app_role(_sync_dsn(postgres_container))
    app_async_dsn = _rewrite_credentials(_async_dsn(postgres_container), APP_ROLE, APP_PASSWORD)
    engine = create_async_engine_from_config(DatabaseConfig(dsn=app_async_dsn))
    session_factory = build_rls_sessionmaker(create_async_session_factory(engine))
    yield SqlMemoryStore(session_factory), session_factory, engine


@pytest.mark.asyncio
async def test_memory_item_isolated_by_tenant_and_user(
    memory_rls_store: tuple[SqlMemoryStore, async_sessionmaker[AsyncSession], AsyncEngine],
) -> None:
    """memory_item RLS enforces both axes — a raw ``count(*)`` (no WHERE)
    is invisible to a different user even within the same tenant."""
    store, session_factory, engine = memory_rls_store
    try:
        tenant_a, user_a, user_b = uuid4(), uuid4(), uuid4()

        current_tenant_id_var.set(tenant_a)
        current_user_id_var.set(user_a)
        await store.write(
            [
                MemoryItem(
                    id=uuid4(),
                    tenant_id=tenant_a,
                    user_id=user_a,
                    kind="fact",
                    content="secret",
                    embedding=_vec(1.0),
                )
            ]
        )

        # Same tenant, different user → the row is invisible.
        current_user_id_var.set(user_b)
        async with session_factory() as session:
            count_other = await session.scalar(text("SELECT count(*) FROM memory_item"))
        assert count_other == 0

        # The owning user sees it.
        current_user_id_var.set(user_a)
        async with session_factory() as session:
            count_owner = await session.scalar(text("SELECT count(*) FROM memory_item"))
        assert count_owner == 1
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# artifact / artifact_version tables — Stream J.9 (tenant + user RLS)
# ---------------------------------------------------------------------------


@pytest.fixture
def artifact_rls_store(
    postgres_container: PostgresContainer,
) -> Iterator[tuple[SqlArtifactStore, async_sessionmaker[AsyncSession], AsyncEngine]]:
    """A :class:`SqlArtifactStore` on the unprivileged role — RLS enforced."""
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", _sync_dsn(postgres_container))
    command.upgrade(cfg, "head")
    _provision_app_role(_sync_dsn(postgres_container))
    app_async_dsn = _rewrite_credentials(_async_dsn(postgres_container), APP_ROLE, APP_PASSWORD)
    engine = create_async_engine_from_config(DatabaseConfig(dsn=app_async_dsn))
    session_factory = build_rls_sessionmaker(create_async_session_factory(engine))
    yield SqlArtifactStore(session_factory), session_factory, engine


@pytest.mark.asyncio
async def test_artifact_isolated_by_tenant_and_user(
    artifact_rls_store: tuple[SqlArtifactStore, async_sessionmaker[AsyncSession], AsyncEngine],
) -> None:
    """artifact + artifact_version RLS enforce both axes — a raw
    ``count(*)`` is invisible to a different user within the same tenant."""
    store, session_factory, engine = artifact_rls_store
    try:
        tenant_a, user_a, user_b = uuid4(), uuid4(), uuid4()

        current_tenant_id_var.set(tenant_a)
        current_user_id_var.set(user_a)
        await store.save_version(
            tenant_id=tenant_a,
            user_id=user_a,
            name="report.md",
            kind="document",
            path_in_workspace="report.md",
            created_in_thread="t-1",
        )

        # Same tenant, different user → both rows are invisible.
        current_user_id_var.set(user_b)
        async with session_factory() as session:
            artifacts_other = await session.scalar(text("SELECT count(*) FROM artifact"))
            versions_other = await session.scalar(text("SELECT count(*) FROM artifact_version"))
        assert artifacts_other == 0
        assert versions_other == 0

        # The owning user sees both.
        current_user_id_var.set(user_a)
        async with session_factory() as session:
            artifacts_owner = await session.scalar(text("SELECT count(*) FROM artifact"))
            versions_owner = await session.scalar(text("SELECT count(*) FROM artifact_version"))
        assert artifacts_owner == 1
        assert versions_owner == 1
    finally:
        await engine.dispose()
