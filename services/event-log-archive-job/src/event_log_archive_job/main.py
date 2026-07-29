"""Entrypoint: ``uv run python -m event_log_archive_job``.

Runs one archival sweep and exits — cron / a Kubernetes CronJob drives
scheduling. The sweep is idempotent; running it twice in a row is fine
(the second pass usually archives nothing).
"""

from __future__ import annotations

import asyncio
import logging

from event_log_archive_job.job import EventLogArchiveJob
from event_log_archive_job.settings import EventLogArchiveSettings
from expert_work.persistence import (
    DatabaseConfig,
    create_async_engine_from_config,
    create_async_session_factory,
)
from expert_work.runtime.storage.factory import S3CompatibleConfig, make_object_store

logger = logging.getLogger(__name__)


async def _amain() -> None:
    settings = EventLogArchiveSettings()
    logging.basicConfig(level=settings.log_level)

    engine = create_async_engine_from_config(DatabaseConfig(dsn=settings.db_dsn))
    session_factory = create_async_session_factory(engine)

    # ``config`` is ignored by the in-memory backend; always built so
    # the s3-compatible branch has it.
    s3_config = S3CompatibleConfig(
        endpoint_url=settings.s3_endpoint_url,
        region=settings.s3_region,
        bucket=settings.s3_bucket,
        access_key=settings.s3_access_key,
        secret_key=settings.s3_secret_key,
        addressing_style=settings.effective_s3_addressing_style,
    )
    logger.info(
        "event_log_archive_job.start age_days=%d batch=%d backend=%s",
        settings.archive_age_days,
        settings.batch_size,
        settings.object_store_backend,
    )
    try:
        async with make_object_store(settings.object_store_backend, s3_config) as store:
            job = EventLogArchiveJob(
                db_session_factory=session_factory,
                object_store=store,
                archive_age_days=settings.archive_age_days,
                batch_size=settings.batch_size,
            )
            report = await job.run_once()
    finally:
        await engine.dispose()

    logger.info(
        "event_log_archive_job.done objects=%d rows=%d duration=%.2fs",
        report.archived_objects,
        report.archived_rows,
        report.duration_seconds,
    )


def main() -> None:
    asyncio.run(_amain())


if __name__ == "__main__":
    main()
