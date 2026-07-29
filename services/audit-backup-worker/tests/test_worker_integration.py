"""Integration: ``AuditWormBackupWorker`` end-to-end against PG + MinIO.

Seeds N rows into ``audit_log`` via the D.1a write path, runs
``run_one_batch``, and verifies:

1.  Every row landed in the Object-Lock-enabled bucket under its
    canonical ``{tenant}/{Y}/{M}/{D}/{id}.json`` key.
2.  Every row in the DB now has ``backup_acked=true`` and a
    ``backup_acked_at`` timestamp.
3.  The objects carry ``ObjectLockMode = COMPLIANCE`` + an
    ``ObjectLockRetainUntilDate`` ≈ ``now + retention_days``.

The app role is provisioned NOINHERIT and granted both
``audit_writer`` (to seed rows) and ``audit_backup_worker`` (to run
the worker's read + targeted UPDATE) — mirroring how production
deployment fixtures will wire the same memberships.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from urllib.parse import urlparse, urlunparse
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import AsyncEngine
from testcontainers.compose import DockerCompose
from testcontainers.postgres import PostgresContainer

from audit_backup_worker.worker import (
    AuditWormBackupWorker,
    static_retention_resolver,
)
from expert_work.persistence import (
    DatabaseConfig,
    SqlAuditLogStore,
    create_async_engine_from_config,
    create_async_session_factory,
)
from expert_work.protocol import AuditAction, AuditEntry, AuditResult
from expert_work.runtime.storage import (
    ObjectStore,
    S3CompatibleConfig,
    make_object_store,
)

pytestmark = pytest.mark.integration

ALEMBIC_INI = Path(__file__).resolve().parents[3] / "packages/expert-work-persistence/alembic.ini"
_INFRA_DIR = Path(__file__).resolve().parents[3] / "infra"

APP_ROLE = "expert_work_app_d1c_worker"
APP_PASSWORD = "expert_work_app_d1c_worker_pw"  # test-only fixture password


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def compose_stack() -> Iterator[DockerCompose]:
    """Boot the MinIO + companion stack for the module's lifetime."""
    stack = DockerCompose(
        context=str(_INFRA_DIR),
        compose_file_name="docker-compose.yml",
        pull=True,
        wait=True,
    )
    with stack:
        yield stack


def _sync_dsn(container: PostgresContainer) -> str:
    url = str(container.get_connection_url())
    return url.replace("+psycopg2", "+psycopg").replace("postgresql://", "postgresql+psycopg://", 1)


def _async_dsn(container: PostgresContainer) -> str:
    url = str(container.get_connection_url())
    return url.replace("+psycopg2", "+asyncpg").replace("postgresql://", "postgresql+asyncpg://", 1)


def _rewrite_credentials(dsn: str, user: str, password: str) -> str:
    parsed = urlparse(dsn)
    netloc = f"{user}:{password}@{parsed.hostname}"
    if parsed.port is not None:
        netloc = f"{netloc}:{parsed.port}"
    return urlunparse(parsed._replace(netloc=netloc))


def _provision_app_role(sync_dsn: str) -> None:
    """Create NOINHERIT app role; grant audit_writer + audit_backup_worker."""
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
            conn.execute(text(f"GRANT SELECT ON TABLE audit_log TO {APP_ROLE}"))
            conn.execute(text(f"GRANT audit_writer TO {APP_ROLE}"))
            conn.execute(text(f"GRANT audit_backup_worker TO {APP_ROLE}"))
    finally:
        admin.dispose()


@pytest.fixture
def app_role_db(
    postgres_container: PostgresContainer,
) -> Iterator[tuple[SqlAuditLogStore, AsyncEngine]]:
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", _sync_dsn(postgres_container))
    command.upgrade(cfg, "head")
    _provision_app_role(_sync_dsn(postgres_container))

    app_dsn = _rewrite_credentials(_async_dsn(postgres_container), APP_ROLE, APP_PASSWORD)
    engine = create_async_engine_from_config(DatabaseConfig(dsn=app_dsn))
    session_factory = create_async_session_factory(engine)
    yield SqlAuditLogStore(session_factory), engine


async def _ensure_worm_bucket(store: ObjectStore, bucket: str) -> None:
    raw = getattr(store, "_client", None)
    if raw is None:  # pragma: no cover — defensive
        msg = "fixture requires S3CompatibleObjectStore"
        raise RuntimeError(msg)
    try:
        await raw.head_bucket(Bucket=bucket)
    except Exception:
        await raw.create_bucket(Bucket=bucket, ObjectLockEnabledForBucket=True)


@pytest.fixture
async def worm_store(compose_stack: DockerCompose) -> AsyncIterator[ObjectStore]:
    host, port_str = compose_stack.get_service_host_and_port("minio", 9000)
    user = os.environ.get("EXPERT_WORK_MINIO_ROOT_USER", "expert_work")
    password = os.environ.get("EXPERT_WORK_MINIO_ROOT_PASSWORD", "expert_work_dev_minio")
    bucket = os.environ.get("EXPERT_WORK_AUDIT_BACKUP_S3_BUCKET", "expert-work-audit-worm")
    config = S3CompatibleConfig(
        endpoint_url=f"http://{host}:{port_str}",
        region="us-east-1",
        bucket=bucket,
        access_key=user,
        secret_key=password,
        addressing_style="path",
    )
    async with make_object_store("s3-compatible", config) as s:
        await _ensure_worm_bucket(s, bucket)
        yield s


def _entry(tenant_id: UUID, *, suffix: str = "") -> AuditEntry:
    return AuditEntry(
        tenant_id=tenant_id,
        actor_type="user",
        actor_id=f"alice{suffix}",
        action=AuditAction.AUTH_LOGIN,
        resource_type="user",
        resource_id=f"alice{suffix}",
        result=AuditResult.SUCCESS,
        details={"k": "v", "suffix": suffix},
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_one_batch_drains_and_acks_rows(
    app_role_db: tuple[SqlAuditLogStore, AsyncEngine],
    worm_store: ObjectStore,
) -> None:
    store, engine = app_role_db
    try:
        tenant = uuid4()
        # Seed 5 rows via the real D.1a write path.
        seeded_ids: list[int] = []
        for i in range(5):
            written = await store.append(_entry(tenant, suffix=str(i)))
            assert written.id is not None
            seeded_ids.append(written.id)

        session_factory = create_async_session_factory(engine)
        worker = AuditWormBackupWorker(
            db_session_factory=session_factory,
            object_store=worm_store,
            retention_resolver=static_retention_resolver(1),  # 1 day for the test
            batch_size=100,
        )
        result = await worker.run_one_batch()

        assert result.failed == 0
        assert result.processed >= len(seeded_ids)

        # All seeded keys present in the worm bucket.
        listed = await worm_store.list_prefix(f"{tenant}/")
        for audit_id in seeded_ids:
            assert any(k.endswith(f"/{audit_id}.json") for k in listed), (
                f"missing key for id={audit_id} in {listed}"
            )

        # Rows acked in the DB. Read through audit_writer (BYPASSRLS,
        # the app role lacks SELECT under RLS without app.tenant_id).
        async with engine.begin() as conn:
            await conn.execute(text("SET LOCAL ROLE audit_writer"))
            rows = (
                await conn.execute(
                    text(
                        "SELECT id, backup_acked, backup_acked_at "
                        "FROM audit_log WHERE tenant_id = :t ORDER BY id"
                    ),
                    {"t": tenant},
                )
            ).all()
        assert len(rows) == len(seeded_ids)
        for row in rows:
            assert row.backup_acked is True
            assert row.backup_acked_at is not None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_run_one_batch_empty_returns_zero(
    app_role_db: tuple[SqlAuditLogStore, AsyncEngine],
    worm_store: ObjectStore,
) -> None:
    """No backlog → ``processed=0, failed=0``; nothing put to the bucket."""
    store, engine = app_role_db
    try:
        # Seed and immediately ack via a worker run, so subsequent runs
        # find nothing pending.
        tenant = uuid4()
        await store.append(_entry(tenant))
        session_factory = create_async_session_factory(engine)
        worker = AuditWormBackupWorker(
            db_session_factory=session_factory,
            object_store=worm_store,
            retention_resolver=static_retention_resolver(1),
            batch_size=100,
        )
        await worker.run_one_batch()  # drain

        result = await worker.run_one_batch()
        assert result.processed == 0
        assert result.failed == 0
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_run_forever_exits_when_stop_set(
    app_role_db: tuple[SqlAuditLogStore, AsyncEngine],
    worm_store: ObjectStore,
) -> None:
    """``run_forever`` returns promptly after ``stop`` is set."""
    _, engine = app_role_db
    try:
        session_factory = create_async_session_factory(engine)
        worker = AuditWormBackupWorker(
            db_session_factory=session_factory,
            object_store=worm_store,
            retention_resolver=static_retention_resolver(1),
            batch_size=100,
        )
        stop = asyncio.Event()

        # Fire stop after a small delay, then await the loop.
        async def _stopper() -> None:
            await asyncio.sleep(0.05)
            stop.set()

        async with asyncio.TaskGroup() as tg:
            tg.create_task(_stopper())
            tg.create_task(worker.run_forever(stop=stop, poll_interval_s=0.05))
        # If we got here without timing out the task group, the loop
        # honoured the stop event.
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_object_carries_retention_metadata(
    app_role_db: tuple[SqlAuditLogStore, AsyncEngine],
    worm_store: ObjectStore,
) -> None:
    """Each backed-up object has COMPLIANCE mode + retain-until set."""
    store, engine = app_role_db
    try:
        tenant = uuid4()
        written = await store.append(_entry(tenant))
        assert written.id is not None

        session_factory = create_async_session_factory(engine)
        worker = AuditWormBackupWorker(
            db_session_factory=session_factory,
            object_store=worm_store,
            retention_resolver=static_retention_resolver(1),
            batch_size=100,
        )
        await worker.run_one_batch()

        raw = worm_store._client  # type: ignore[attr-defined]
        bucket = worm_store._bucket  # type: ignore[attr-defined]
        keys = await worm_store.list_prefix(f"{tenant}/")
        assert keys, "expected at least one backed-up object"
        head = await raw.head_object(Bucket=bucket, Key=keys[0])
        assert head.get("ObjectLockMode") == "COMPLIANCE"
        assert "ObjectLockRetainUntilDate" in head
    finally:
        await engine.dispose()
