"""End-to-end ``PostgresFullBackup`` test against real pg_dump + MinIO.

Uses the same ``infra/docker-compose.yml`` stack as the PgBouncer and
MinIO integration tests. ``pg_dump`` runs from the host shell — the
Postgres docker image's client binary is on $PATH in CI runners
(``ubuntu-latest`` ships postgresql-client). The dev workstation may
need ``apt-get install postgresql-client`` / ``brew install libpq``.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.ext.asyncio import AsyncEngine
from testcontainers.compose import DockerCompose

from expert_work.persistence import (
    DatabaseConfig,
    SqlBackupRecordStore,
    create_async_engine_from_config,
    create_async_session_factory,
)
from expert_work.protocol import BackupAssetType, BackupStatus
from expert_work.runtime.dr import PostgresBackupConfig, PostgresFullBackup
from expert_work.runtime.storage import (
    ObjectStore,
    S3CompatibleConfig,
    make_object_store,
)

pytestmark = pytest.mark.integration

_INFRA_DIR = Path(__file__).resolve().parents[3] / "infra"
ALEMBIC_INI = Path(__file__).resolve().parents[2] / "expert-work-persistence" / "alembic.ini"


@pytest.fixture(scope="module")
def compose_stack() -> Iterator[DockerCompose]:
    """Reuses the same dev stack as the PgBouncer / MinIO tests."""
    stack = DockerCompose(
        context=str(_INFRA_DIR),
        compose_file_name="docker-compose.yml",
        pull=True,
        wait=True,
    )
    with stack:
        yield stack


@pytest.fixture(scope="module")
def _pg_dump_binary() -> str:
    """Find ``pg_dump`` on $PATH or skip — the test cannot run without it.

    GitHub Actions ``ubuntu-latest`` ships postgresql-client by default so
    this skip will only trigger on bare workstations.
    """
    cmd = shutil.which("pg_dump")
    if cmd is None:
        pytest.skip("pg_dump not on PATH; install postgresql-client to run")
    return cmd


def _direct_libpq_dsn(stack: DockerCompose) -> str:
    """pg_dump-compatible DSN. Talks **directly** to the postgres service
    (5432), not pgbouncer (6432) — transaction-mode pooling can interfere
    with pg_dump's long-running session."""
    host, port_str = stack.get_service_host_and_port("postgres", 5432)
    user = os.environ.get("EXPERT_WORK_DB_USER", "expert_work")
    password = os.environ.get("EXPERT_WORK_DB_PASSWORD", "expert_work_dev")
    name = os.environ.get("EXPERT_WORK_DB_NAME", "expert_work_dev")
    return f"postgresql://{user}:{password}@{host}:{port_str}/{name}"


def _async_dsn(stack: DockerCompose) -> str:
    host, port_str = stack.get_service_host_and_port("postgres", 5432)
    user = os.environ.get("EXPERT_WORK_DB_USER", "expert_work")
    password = os.environ.get("EXPERT_WORK_DB_PASSWORD", "expert_work_dev")
    name = os.environ.get("EXPERT_WORK_DB_NAME", "expert_work_dev")
    return f"postgresql+asyncpg://{user}:{password}@{host}:{port_str}/{name}"


def _sync_dsn(stack: DockerCompose) -> str:
    host, port_str = stack.get_service_host_and_port("postgres", 5432)
    user = os.environ.get("EXPERT_WORK_DB_USER", "expert_work")
    password = os.environ.get("EXPERT_WORK_DB_PASSWORD", "expert_work_dev")
    name = os.environ.get("EXPERT_WORK_DB_NAME", "expert_work_dev")
    return f"postgresql+psycopg://{user}:{password}@{host}:{port_str}/{name}"


def _minio_config(stack: DockerCompose) -> S3CompatibleConfig:
    host, port_str = stack.get_service_host_and_port("minio", 9000)
    return S3CompatibleConfig(
        endpoint_url=f"http://{host}:{port_str}",
        region=os.environ.get("EXPERT_WORK_STORAGE_REGION", "us-east-1"),
        bucket=os.environ.get("EXPERT_WORK_MINIO_BUCKET", "expert-work-dev"),
        access_key=os.environ.get("EXPERT_WORK_MINIO_ROOT_USER", "expert_work"),
        secret_key=os.environ.get("EXPERT_WORK_MINIO_ROOT_PASSWORD", "expert_work_dev_minio"),
        addressing_style="path",
    )


async def _ensure_bucket(store: ObjectStore, bucket: str) -> None:
    raw = getattr(store, "_client", None)
    if raw is None:  # pragma: no cover — defensive
        msg = "fixture requires S3CompatibleObjectStore"
        raise RuntimeError(msg)
    try:
        await raw.head_bucket(Bucket=bucket)
    except Exception:
        await raw.create_bucket(Bucket=bucket)


@pytest.fixture
async def backup_job(
    compose_stack: DockerCompose,
    _pg_dump_binary: str,
) -> AsyncIterator[tuple[PostgresFullBackup, AsyncEngine, ObjectStore]]:
    # Make sure the target DB has Expert Work schema so pg_dump captures
    # something meaningful.
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", _sync_dsn(compose_stack))
    command.upgrade(cfg, "head")

    minio_cfg = _minio_config(compose_stack)
    engine = create_async_engine_from_config(DatabaseConfig(dsn=_async_dsn(compose_stack)))
    session_factory = create_async_session_factory(engine)

    async with make_object_store("s3-compatible", minio_cfg) as object_store:
        await _ensure_bucket(object_store, minio_cfg.bucket)
        job = PostgresFullBackup(
            config=PostgresBackupConfig(
                dsn=_direct_libpq_dsn(compose_stack),
                bucket_prefix="backups/postgres-it",
                region="local",
                pg_dump_cmd=_pg_dump_binary,
            ),
            object_store=object_store,
            record_store=SqlBackupRecordStore(session_factory),
        )
        try:
            yield job, engine, object_store
        finally:
            await engine.dispose()


@pytest.mark.asyncio
async def test_real_pg_dump_round_trip(
    backup_job: tuple[PostgresFullBackup, AsyncEngine, ObjectStore],
) -> None:
    job, _engine, store = backup_job
    record = await job.run()

    assert record.status == BackupStatus.SUCCESS
    assert record.size_bytes is not None and record.size_bytes > 0
    assert record.sha256 is not None

    # The dump landed in object storage and round-trips by checksum.
    fetched = await store.get(record.asset_ref)
    import hashlib

    assert hashlib.sha256(fetched).hexdigest() == record.sha256
    # pg_dump's custom format starts with magic bytes ``PGDMP``.
    assert fetched.startswith(b"PGDMP")
    assert record.asset_type == BackupAssetType.POSTGRES_FULL
