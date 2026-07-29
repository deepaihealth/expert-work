"""Entrypoint: ``uv run python -m audit_backup_worker``.

Boots the worker against the env-driven :class:`AuditBackupSettings`
and runs ``run_forever`` until SIGINT / SIGTERM.
"""

from __future__ import annotations

import asyncio
import logging
import signal

from audit_backup_worker.settings import AuditBackupSettings
from audit_backup_worker.worker import (
    AuditWormBackupWorker,
    static_retention_resolver,
)
from expert_work.persistence import (
    DatabaseConfig,
    create_async_engine_from_config,
    create_async_session_factory,
)
from expert_work.runtime.storage import S3CompatibleConfig, make_object_store

logger = logging.getLogger(__name__)


async def _amain() -> None:
    settings = AuditBackupSettings()
    logging.basicConfig(level=settings.log_level)

    engine = create_async_engine_from_config(DatabaseConfig(dsn=settings.db_dsn))
    session_factory = create_async_session_factory(engine)

    # Single-instance worker for M0. M1 may shard by tenant.
    s3_config = S3CompatibleConfig(
        endpoint_url=settings.s3_endpoint_url,
        region=settings.s3_region,
        bucket=settings.s3_bucket,
        access_key=settings.s3_access_key,
        secret_key=settings.s3_secret_key,
        addressing_style=settings.effective_s3_addressing_style,
    )

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    async with make_object_store(settings.object_store_backend, s3_config) as store:
        worker = AuditWormBackupWorker(
            db_session_factory=session_factory,
            object_store=store,
            retention_resolver=static_retention_resolver(settings.audit_retention_days_default),
            batch_size=settings.batch_size,
        )
        logger.info(
            "audit_backup_worker.start bucket=%s batch=%d poll=%ss",
            settings.s3_bucket,
            settings.batch_size,
            settings.poll_interval_s,
        )
        await worker.run_forever(stop=stop, poll_interval_s=settings.poll_interval_s)

    await engine.dispose()
    logger.info("audit_backup_worker.stop")


def main() -> None:
    asyncio.run(_amain())


if __name__ == "__main__":
    main()
