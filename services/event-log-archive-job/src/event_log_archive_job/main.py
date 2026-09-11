"""Entrypoint: ``uv run python -m event_log_archive_job``.

Runs one archival sweep and exits — cron / a Kubernetes CronJob drives
scheduling. The sweep is idempotent; running it twice in a row is fine
(the second pass usually archives nothing).
"""

from __future__ import annotations

import asyncio
import logging
from typing import IO

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from event_log_archive_job.job import EventLogArchiveJob
from event_log_archive_job.settings import EventLogArchiveSettings
from expert_work.common.observability import init_logging
from expert_work.persistence import (
    DatabaseConfig,
    create_async_engine_from_config,
    create_async_session_factory,
)
from expert_work.persistence.rls import build_rls_sessionmaker
from expert_work.runtime.storage.factory import S3CompatibleConfig, make_object_store

logger = logging.getLogger(__name__)


def build_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """B-45 —— 与 control-plane / retention-cleanup-job 同款:session factory
    必须经过 ``build_rls_sessionmaker``,``after_begin`` listener 才在本进程
    存在。此前这个 job 从不调它,RLS Detect 信号在这里根本不会出现,生产
    enforce 之前也就没人知道它哪条路径会 fail-closed。每条 pass 自己的作用域
    在 :func:`event_log_archive_job.job._bypass_rls`(跨租户的可归档分组扫描)
    与 :func:`event_log_archive_job.job._tenant_scope`(单组读 / 删)里声明。"""
    return build_rls_sessionmaker(create_async_session_factory(engine))


def configure_logging(settings: EventLogArchiveSettings, *, stream: IO[str] | None = None) -> None:
    """B-45 —— 装平台的 JSON formatter,而不是 ``logging.basicConfig``。

    Detect 信号的归因(``rls_caller`` / ``rls_caller_outer`` / ``rls_suppressed``)
    是 ``extra=`` 结构化字段。``basicConfig`` 的默认 format 只渲染 message,这些
    字段一个都不落盘 —— 于是「信号打了」而「归因看不见」,与 #1443 里 JSON
    formatter 把 ``stack_info`` 列进跳过属性是同一类失效:接上 listener 而日志
    渲染不出归因,等于没接。
    """
    init_logging(
        service=settings.service_name, env=settings.env, level=settings.log_level, stream=stream
    )


async def _amain() -> None:
    settings = EventLogArchiveSettings()
    configure_logging(settings)

    engine = create_async_engine_from_config(DatabaseConfig(dsn=settings.db_dsn))
    session_factory = build_session_factory(engine)

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
