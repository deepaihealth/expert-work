"""``python -m expert_work.runtime.dr`` — one-shot backup runner.

Designed to be invoked from a cron / Kubernetes CronJob. Reads
configuration from CLI flags + environment variables (the cron caller
already lives in a shell-friendly layer; no need for an extra YAML
loader in M0).

Exit codes:

- ``0`` — backup succeeded; ``backup_record`` row carries SUCCESS
- ``1`` — backup failed; ``backup_record`` row carries FAILED, stderr
  has the exception. Cron alert wiring (Stream A.9) triggers on the
  ``expert_work_dr_backup_age_seconds`` metric + ``backup_record.status``.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from collections.abc import Sequence

from expert_work.persistence import (
    DatabaseConfig,
    SqlBackupRecordStore,
    create_async_engine_from_config,
    create_async_session_factory,
)
from expert_work.runtime.dr.postgres_backup import (
    BackupError,
    PostgresBackupConfig,
    PostgresFullBackup,
)
from expert_work.runtime.storage import S3CompatibleConfig, make_object_store

logger = logging.getLogger("expert_work.runtime.dr.cli")


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="expert-work-pg-backup",
        description="Run one Postgres full backup → object storage.",
    )
    p.add_argument(
        "--source-dsn",
        required=True,
        help="libpq DSN for the database to dump (postgresql://...)",
    )
    p.add_argument(
        "--meta-dsn",
        required=True,
        help="SQLAlchemy asyncpg DSN for the database that holds backup_record "
        "(postgresql+asyncpg://...). Usually the same physical DB as --source-dsn.",
    )
    p.add_argument(
        "--bucket-prefix",
        default="backups/postgres",
        help="Object-store key prefix (default: backups/postgres)",
    )
    p.add_argument("--region", default="local", help="Region tag for the record")
    p.add_argument(
        "--storage-endpoint",
        default=os.environ.get("EXPERT_WORK_STORAGE_ENDPOINT", "http://localhost:9000"),
    )
    p.add_argument(
        "--storage-bucket",
        default=os.environ.get("EXPERT_WORK_STORAGE_BUCKET", "expert-work-dev"),
    )
    p.add_argument(
        "--storage-region",
        default=os.environ.get("EXPERT_WORK_STORAGE_REGION", "us-east-1"),
        help="S3 region — for MinIO this can be any non-empty string",
    )
    return p


async def _run(args: argparse.Namespace) -> int:
    # Credentials come from env vars per § 4.1 (M0 = secret_ref via proxy is
    # deferred — env is the dogfood path; document in runbook).
    access_key = os.environ.get("EXPERT_WORK_STORAGE_ACCESS_KEY")
    secret_key = os.environ.get("EXPERT_WORK_STORAGE_SECRET_KEY")
    if not access_key or not secret_key:
        logger.error(
            "missing EXPERT_WORK_STORAGE_ACCESS_KEY / EXPERT_WORK_STORAGE_SECRET_KEY env vars"
        )
        return 1

    storage_cfg = S3CompatibleConfig(
        endpoint_url=args.storage_endpoint,
        region=args.storage_region,
        bucket=args.storage_bucket,
        access_key=access_key,
        secret_key=secret_key,
        addressing_style="path",
    )

    engine = create_async_engine_from_config(DatabaseConfig(dsn=args.meta_dsn))
    session_factory = create_async_session_factory(engine)
    try:
        async with make_object_store("s3-compatible", storage_cfg) as object_store:
            job = PostgresFullBackup(
                config=PostgresBackupConfig(
                    dsn=args.source_dsn,
                    bucket_prefix=args.bucket_prefix,
                    region=args.region,
                ),
                object_store=object_store,
                record_store=SqlBackupRecordStore(session_factory),
            )
            try:
                record = await job.run()
            except BackupError as exc:
                logger.error("backup failed: %s", exc)
                return 1
            logger.info(
                "backup ok asset_ref=%s size=%d sha256=%s",
                record.asset_ref,
                record.size_bytes,
                record.sha256,
            )
    finally:
        await engine.dispose()
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    args = _parser().parse_args(argv)
    return asyncio.run(_run(args))


if __name__ == "__main__":  # pragma: no cover — CLI entrypoint
    sys.exit(main())
