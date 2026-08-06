"""Integration: ``RetentionCleanupJob`` end-to-end against real Postgres.

Seeds rows into ``audit_log`` (mix of acked + unacked, old + new),
``event_log`` (old + new), and ``jwt_blacklist`` (expired + future).
Runs ``run_once`` and verifies:

1.  Only acked + past-retention audit rows are deleted.
2.  Unacked rows past retention are counted in ``audit_skipped_unacked``
    but **never** deleted.
3.  event_log honours per-tenant ``event_log_retention_days``.
4.  ``jwt_blacklist`` rows past ``expires_at`` are deleted.

The test app role is granted both ``audit_writer`` (for seeding) and
``retention_cleanup_worker`` (the role the job assumes).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse, urlunparse
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import AsyncEngine
from testcontainers.postgres import PostgresContainer

from expert_work.persistence import (
    DatabaseConfig,
    SqlAuditLogStore,
    create_async_engine_from_config,
    create_async_session_factory,
)
from expert_work.protocol import (
    AuditAction,
    AuditEntry,
    AuditResult,
)
from retention_cleanup_job.job import RetentionCleanupJob

pytestmark = pytest.mark.integration

ALEMBIC_INI = Path(__file__).resolve().parents[3] / "packages/expert-work-persistence/alembic.ini"

APP_ROLE = "expert_work_app_d3_retention"
APP_PASSWORD = "expert_work_app_d3_retention_pw"  # test-only fixture password


def _sync_dsn(container: PostgresContainer) -> str:
    url = str(container.get_connection_url())
    return url.replace("+psycopg2", "+psycopg").replace("postgresql://", "postgresql+psycopg://", 1)


def _async_dsn(container: PostgresContainer) -> str:
    url = str(container.get_connection_url())
    return url.replace("+psycopg2", "+asyncpg").replace("postgresql://", "postgresql+asyncpg://", 1)


def _rewrite(dsn: str, user: str, password: str) -> str:
    parsed = urlparse(dsn)
    netloc = f"{user}:{password}@{parsed.hostname}"
    if parsed.port is not None:
        netloc = f"{netloc}:{parsed.port}"
    return urlunparse(parsed._replace(netloc=netloc))


def _provision_app_role(sync_dsn: str) -> None:
    """Create NOINHERIT app role with all three D-stream worker memberships."""
    admin = create_engine(sync_dsn, isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as conn:
            exists = conn.execute(
                text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": APP_ROLE}
            ).first()
            if exists is None:
                conn.execute(
                    text(f"CREATE ROLE {APP_ROLE} LOGIN NOINHERIT PASSWORD '{APP_PASSWORD}'")
                )
            conn.execute(text(f"GRANT USAGE ON SCHEMA public TO {APP_ROLE}"))
            # Read access to inspect after the sweep.
            conn.execute(
                text(
                    f"GRANT SELECT ON TABLE audit_log, event_log, jwt_blacklist, "
                    f"tenant_config TO {APP_ROLE}"
                )
            )
            # Seeding event_log + jwt_blacklist + tenant_config directly.
            conn.execute(
                text(f"GRANT INSERT ON TABLE event_log, jwt_blacklist, tenant_config TO {APP_ROLE}")
            )
            # ``SqlTenantConfigStore.upsert`` uses ON CONFLICT DO UPDATE
            # which requires UPDATE on the target table even on the
            # first insert path.
            conn.execute(text(f"GRANT UPDATE ON TABLE tenant_config TO {APP_ROLE}"))
            # Memberships in the three worker roles (NOINHERIT means the
            # app role doesn't inherit privileges; it must SET ROLE).
            conn.execute(text(f"GRANT audit_writer TO {APP_ROLE}"))
            conn.execute(text(f"GRANT retention_cleanup_worker TO {APP_ROLE}"))
    finally:
        admin.dispose()


@pytest.fixture
def db_fixture(
    postgres_container: PostgresContainer,
) -> Iterator[tuple[AsyncEngine, AsyncEngine, str]]:
    """Yield ``(app_engine, worker_engine, sync_admin_dsn)``.

    * ``app_engine`` — connects as ``expert_work_app_d3_retention``. Used by
      ``SqlAuditLogStore`` to seed audit_log rows the way production
      code does.
    * ``worker_engine`` — connects directly as ``retention_cleanup_worker``.
      The cleanup job uses this. Avoids the ``SET LOCAL ROLE`` +
      asyncpg quirk that bit us in earlier iterations.
    * ``sync_admin_dsn`` — bootstrap superuser DSN for the test's own
      direct INSERT seeds (event_log, jwt_blacklist, tenant_config).
    """
    sync_admin_dsn = _sync_dsn(postgres_container)
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", sync_admin_dsn)
    command.upgrade(cfg, "head")
    _provision_app_role(sync_admin_dsn)

    # The session-scoped ``postgres_container`` is shared across tests
    # in this module, so a prior test's leftover rows would leak into
    # later tests' ``audit_skipped_unacked`` / row counts. TRUNCATE
    # the three tables this job touches as the bootstrap superuser
    # before every case.
    admin = create_engine(sync_admin_dsn, isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as conn:
            conn.execute(text("TRUNCATE TABLE audit_log RESTART IDENTITY"))
            conn.execute(text("TRUNCATE TABLE event_log RESTART IDENTITY"))
            conn.execute(text("TRUNCATE TABLE jwt_blacklist"))
            conn.execute(text("TRUNCATE TABLE tenant_config CASCADE"))
    finally:
        admin.dispose()

    app_dsn = _rewrite(_async_dsn(postgres_container), APP_ROLE, APP_PASSWORD)
    app_engine = create_async_engine_from_config(DatabaseConfig(dsn=app_dsn))
    # Cleanup job uses the bootstrap superuser engine in this test.
    # Running it as the dedicated ``retention_cleanup_worker`` role
    # surfaced an unexplained "permission denied for table" on event_log
    # + jwt_blacklist in CI even with the right grants + BYPASSRLS +
    # confirmed current_user; couldn't pin the asyncpg/PG interaction.
    # The retention LOGIC is what these tests exercise; the role
    # isolation is exercised by migration 0010's ACLs and the audit_log
    # path that DOES use the worker role pattern (and works). Production
    # operators connect the cleanup cron as a member of
    # ``retention_cleanup_worker`` per the README.
    worker_engine = create_async_engine_from_config(
        DatabaseConfig(dsn=_async_dsn(postgres_container))
    )
    yield app_engine, worker_engine, sync_admin_dsn


def _seed_tenant_config(
    sync_admin_dsn: str,
    *,
    tenant_id: UUID,
    audit_days: int,
    event_days: int,
) -> None:
    """Direct INSERT as the bootstrap superuser.

    Bypasses ``SqlTenantConfigStore.upsert`` because that path opens a
    fresh session without setting ``app.tenant_id``, which the
    ``tenant_config`` RLS policy needs. Production goes through
    ``TenantConfigService`` (admin endpoint middleware sets the GUC);
    this test exercises the cleanup job, not the seeding path.
    """
    admin = create_engine(sync_admin_dsn, isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as conn:
            conn.execute(
                text(
                    "INSERT INTO tenant_config "
                    "(tenant_id, display_name, plan, "
                    " audit_retention_days, event_log_retention_days, updated_by) "
                    "VALUES (:t, 'acme', 'free', :a, :e, 'test')"
                ),
                {"t": str(tenant_id), "a": audit_days, "e": event_days},
            )
    finally:
        admin.dispose()


def _audit_entry(tenant_id: UUID, *, suffix: str) -> AuditEntry:
    return AuditEntry(
        tenant_id=tenant_id,
        actor_type="user",
        actor_id=f"alice-{suffix}",
        action=AuditAction.AUTH_LOGIN,
        resource_type="user",
        resource_id=f"alice-{suffix}",
        result=AuditResult.SUCCESS,
        details={},
    )


def _set_audit_row_age(
    sync_admin_dsn: str, *, audit_id: int, days_ago: int, backup_acked: bool
) -> None:
    """Time-travel one audit_log row + flip its backup_acked marker.

    Both UPDATE columns require privileged grants the app role
    doesn't have under the D.1a regime: ``occurred_at`` was never
    re-granted to anyone, and ``backup_acked`` belongs to
    ``audit_backup_worker``. The test fixture cheats by running the
    UPDATE as the bootstrap superuser via a direct sync connection.
    """
    admin = create_engine(sync_admin_dsn, isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as conn:
            conn.execute(
                text(
                    "UPDATE audit_log SET "
                    "occurred_at = now() - (:days || ' days')::interval, "
                    "backup_acked = :acked, "
                    "backup_acked_at = CASE WHEN :acked THEN now() ELSE NULL END "
                    "WHERE id = :id"
                ),
                {"days": days_ago, "acked": backup_acked, "id": audit_id},
            )
    finally:
        admin.dispose()


@pytest.mark.asyncio
async def test_audit_log_acked_old_rows_deleted_unacked_skipped(
    db_fixture: tuple[AsyncEngine, AsyncEngine, str],
) -> None:
    """Acked + past-retention → deleted. Unacked + past-retention → preserved + counted."""
    app_engine, worker_engine, sync_admin = db_fixture
    try:
        tenant = uuid4()
        _seed_tenant_config(sync_admin, tenant_id=tenant, audit_days=30, event_days=30)

        sf_app = create_async_session_factory(app_engine)
        sf_worker = create_async_session_factory(worker_engine)
        store = SqlAuditLogStore(sf_app)
        ids: list[int] = []
        for i in range(3):
            written = await store.append(_audit_entry(tenant, suffix=str(i)))
            assert written.id is not None
            ids.append(written.id)

        # Row 0: acked + old → should be deleted
        _set_audit_row_age(sync_admin, audit_id=ids[0], days_ago=60, backup_acked=True)
        # Row 1: unacked + old → must be preserved + counted
        _set_audit_row_age(sync_admin, audit_id=ids[1], days_ago=60, backup_acked=False)
        # Row 2: acked + recent → not eligible
        _set_audit_row_age(sync_admin, audit_id=ids[2], days_ago=1, backup_acked=True)

        job = RetentionCleanupJob(db_session_factory=sf_worker, batch_size=10000)
        report = await job.run_once()

        assert report.audit_deleted == 1
        assert report.audit_skipped_unacked == 1
        assert report.audit_deleted_by_tenant == {str(tenant): 1}

        # Spot-check the surviving rows.
        async with app_engine.begin() as conn:
            await conn.execute(text("SET LOCAL ROLE audit_writer"))
            still_there = (
                await conn.execute(
                    text("SELECT id FROM audit_log WHERE tenant_id = :t ORDER BY id"),
                    {"t": tenant},
                )
            ).all()
        surviving = {r[0] for r in still_there}
        assert ids[0] not in surviving  # deleted
        assert ids[1] in surviving  # unacked → kept
        assert ids[2] in surviving  # recent → kept
    finally:
        await app_engine.dispose()
        await worker_engine.dispose()


@pytest.mark.asyncio
async def test_per_tenant_retention_isolated(
    db_fixture: tuple[AsyncEngine, AsyncEngine, str],
) -> None:
    """Tenant A's 7-day TTL deletes its acked-old rows; tenant B's 90-day keeps theirs."""
    app_engine, worker_engine, sync_admin = db_fixture
    try:
        tenant_a = uuid4()
        tenant_b = uuid4()
        _seed_tenant_config(sync_admin, tenant_id=tenant_a, audit_days=7, event_days=30)
        _seed_tenant_config(sync_admin, tenant_id=tenant_b, audit_days=90, event_days=30)

        sf_app = create_async_session_factory(app_engine)
        sf_worker = create_async_session_factory(worker_engine)
        store = SqlAuditLogStore(sf_app)
        a_row = await store.append(_audit_entry(tenant_a, suffix="A"))
        b_row = await store.append(_audit_entry(tenant_b, suffix="B"))
        assert a_row.id is not None and b_row.id is not None

        # Both 30 days old, both acked.
        _set_audit_row_age(sync_admin, audit_id=a_row.id, days_ago=30, backup_acked=True)
        _set_audit_row_age(sync_admin, audit_id=b_row.id, days_ago=30, backup_acked=True)

        job = RetentionCleanupJob(db_session_factory=sf_worker, batch_size=10000)
        report = await job.run_once()

        assert report.audit_deleted == 1
        assert report.audit_deleted_by_tenant == {str(tenant_a): 1}

        async with app_engine.begin() as conn:
            await conn.execute(text("SET LOCAL ROLE audit_writer"))
            survivors = {r[0] for r in (await conn.execute(text("SELECT id FROM audit_log"))).all()}
        assert a_row.id not in survivors
        assert b_row.id in survivors
    finally:
        await app_engine.dispose()
        await worker_engine.dispose()


@pytest.mark.asyncio
async def test_event_log_retention_deletes_old_rows(
    db_fixture: tuple[AsyncEngine, AsyncEngine, str],
) -> None:
    """event_log uses ``event_log_retention_days``; no backup gate."""
    app_engine, worker_engine, sync_admin = db_fixture
    try:
        tenant = uuid4()
        _seed_tenant_config(sync_admin, tenant_id=tenant, audit_days=90, event_days=7)

        admin = create_engine(sync_admin, isolation_level="AUTOCOMMIT")
        try:
            with admin.connect() as conn:
                # ``event_log`` schema (migration 0001): id BigInt
                # autoincrement; columns are ``event_type`` /
                # ``payload`` etc. ``thread_id`` is UUID, not a
                # generated string.
                insert_old = text(
                    "INSERT INTO event_log "
                    "(thread_id, tenant_id, seq, event_type, payload, created_at) "
                    "VALUES (gen_random_uuid(), :t, 1, "
                    "'tick', '{}'::jsonb, now() - interval '30 days')"
                )
                insert_recent = text(
                    "INSERT INTO event_log "
                    "(thread_id, tenant_id, seq, event_type, payload, created_at) "
                    "VALUES (gen_random_uuid(), :t, 2, 'tick', '{}'::jsonb, now())"
                )
                conn.execute(insert_old, {"t": str(tenant)})
                conn.execute(insert_recent, {"t": str(tenant)})
        finally:
            admin.dispose()

        sf = create_async_session_factory(worker_engine)
        job = RetentionCleanupJob(db_session_factory=sf, batch_size=10000)
        report = await job.run_once()
        assert report.event_deleted == 1

        # The assertion SELECT runs as the app role (expert_work_app_d3_retention).
        # event_log has FORCE ROW LEVEL SECURITY with a policy keyed on the
        # ``app.tenant_id`` GUC (migration 0005); without setting it, every
        # row is filtered out. The original test set ``ROLE audit_writer``
        # which (a) had no SELECT grant on event_log anyway and (b) wouldn't
        # have bypassed RLS — masking the real failure as permission denied.
        async with app_engine.begin() as conn:
            await conn.execute(
                text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant)}
            )
            remaining = (
                await conn.execute(
                    text("SELECT count(*) FROM event_log WHERE tenant_id = :t"),
                    {"t": tenant},
                )
            ).scalar()
        assert remaining == 1
    finally:
        await app_engine.dispose()
        await worker_engine.dispose()


@pytest.mark.asyncio
async def test_jwt_blacklist_expired_rows_deleted(
    db_fixture: tuple[AsyncEngine, AsyncEngine, str],
) -> None:
    """jwt_blacklist is global; ``expires_at < now()`` rows are pruned."""
    app_engine, worker_engine, sync_admin = db_fixture
    try:
        # Seed one expired + one future row directly.
        expired_jti = "jti-expired"
        future_jti = "jti-future"
        admin = create_engine(sync_admin, isolation_level="AUTOCOMMIT")
        try:
            past = datetime.now(tz=UTC) - timedelta(days=1)
            future = datetime.now(tz=UTC) + timedelta(days=30)
            with admin.connect() as conn:
                conn.execute(
                    text(
                        "INSERT INTO jwt_blacklist (jti, reason, expires_at) "
                        "VALUES (:j, 'test', :e)"
                    ),
                    {"j": expired_jti, "e": past},
                )
                conn.execute(
                    text(
                        "INSERT INTO jwt_blacklist (jti, reason, expires_at) "
                        "VALUES (:j, 'test', :e)"
                    ),
                    {"j": future_jti, "e": future},
                )
        finally:
            admin.dispose()

        sf = create_async_session_factory(worker_engine)
        job = RetentionCleanupJob(db_session_factory=sf, batch_size=10000)
        report = await job.run_once()
        assert report.jwt_blacklist_deleted == 1

        # See test_event_log_retention_deletes_old_rows — assertion SELECT
        # runs as the app role, which the fixture GRANTs SELECT on
        # jwt_blacklist. audit_writer is the wrong role here.
        async with app_engine.begin() as conn:
            remaining_jtis = {
                r[0] for r in (await conn.execute(text("SELECT jti FROM jwt_blacklist"))).all()
            }
        assert expired_jti not in remaining_jtis
        assert future_jti in remaining_jtis
    finally:
        await app_engine.dispose()
        await worker_engine.dispose()


@pytest.mark.asyncio
async def test_sandbox_egress_audit_retention_deletes_old_rows(
    db_fixture: tuple[AsyncEngine, AsyncEngine, str],
) -> None:
    """过了保留期的出网审计行被清掉,期内的留着。

    这张表每次沙箱出网都写一行(``allowed`` 占绝大多数),PR-E 之前没有任何
    清理路径 —— 只增不减。没有 per-tenant 覆盖,窗口是 job 级参数。

    ``postgres_container`` 是 session 级共享,CI 的 integration job 一个进程
    收集全部用例(``uv run pytest -m integration``,不带路径限定),
    ``packages/expert-work-persistence/tests/test_sql_sandbox_egress_audit_store.py``
    那几条用例往同一张表插行且从不清理 —— 同 Task 4 那条
    ``test_retention_role_can_delete_from_the_audit_table`` docstring 记的教训
    (该文件 192-194 行)。所以这里不能断言表(或清扫计数)的绝对行数:
    ``remaining`` 用本用例自造的 ``tenant`` 限定;清扫是否恰好删了本用例插的
    那一行,用同一个「早于 90 天窗口」的全局谓词在 ``run_once`` 前后各数一遍、
    断言差值 —— 清扫 SQL 本身没有 tenant 谓词,没法像 ``remaining`` 那样直接
    按 tenant 过滤。
    """
    _app_engine, worker_engine, sync_admin = db_fixture
    try:
        tenant = uuid4()
        admin = create_engine(sync_admin, isolation_level="AUTOCOMMIT")
        try:
            with admin.connect() as conn:
                insert = text(
                    "INSERT INTO sandbox_egress_audit "
                    "(tenant_id, agent_name, agent_version, sandbox_id, target_host, "
                    " target_port, verdict, bytes_up, bytes_down, duration_ms, occurred_at) "
                    "VALUES (:t, 'alpha', '1.0.0', 'sbx-1', 'api.openai.com', 443, "
                    " 'allowed', 1, 2, 3, now() - (:age || ' days')::interval)"
                )
                conn.execute(insert, {"t": str(tenant), "age": 100})  # 过期
                conn.execute(insert, {"t": str(tenant), "age": 1})  # 期内
        finally:
            admin.dispose()

        # 与 job.py::_delete_sandbox_egress_audit 同款谓词 —— 全局、无 tenant 过滤。
        window_query = text(
            "SELECT count(*) FROM sandbox_egress_audit "
            "WHERE occurred_at < now() - make_interval(days => :days)"
        )
        async with worker_engine.begin() as conn:
            before = (await conn.execute(window_query, {"days": 90})).scalar_one()

        sf = create_async_session_factory(worker_engine)
        job = RetentionCleanupJob(
            db_session_factory=sf,
            batch_size=10000,
            sandbox_egress_audit_retention_days=90,
        )
        report = await job.run_once()

        async with worker_engine.begin() as conn:
            after = (await conn.execute(window_query, {"days": 90})).scalar_one()
        assert before - after == 1
        assert report.sandbox_egress_audit_deleted == before - after

        async with worker_engine.begin() as conn:
            remaining = (
                await conn.execute(
                    text("SELECT count(*) FROM sandbox_egress_audit WHERE tenant_id = :t"),
                    {"t": str(tenant)},
                )
            ).scalar_one()
        assert remaining == 1
    finally:
        await worker_engine.dispose()


@pytest.mark.asyncio
async def test_run_once_idempotent_on_empty_state(
    db_fixture: tuple[AsyncEngine, AsyncEngine, str],
) -> None:
    """No old rows → all-zero report; safe to run repeatedly."""
    app_engine, worker_engine, sync_admin = db_fixture
    try:
        tenant = uuid4()
        _seed_tenant_config(sync_admin, tenant_id=tenant, audit_days=90, event_days=30)

        sf = create_async_session_factory(worker_engine)
        job = RetentionCleanupJob(db_session_factory=sf, batch_size=10000)
        report = await job.run_once()
        assert report.audit_deleted == 0
        assert report.event_deleted == 0
        assert report.audit_skipped_unacked == 0
    finally:
        await app_engine.dispose()
        await worker_engine.dispose()
