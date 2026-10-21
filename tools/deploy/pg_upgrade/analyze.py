"""Refresh planner statistics after a PG major-version upgrade (B-207).

Runbook: ``docs/runbooks/rds-pg-major-upgrade.md`` §6. ``pg_upgrade`` does not
carry planner statistics over, and the Alibaba console docs do not say whether
the console upgrade collects them (only the API has ``CollectStatMode``). So
run ``ANALYZE`` per database ourselves, while writes are still stopped, and
report how many user tables still have no statistics: tables the app role
does not own are skipped by Postgres (``WARNING: skipping``) and are left to
autovacuum.
"""

from __future__ import annotations

import asyncio
import os
import time

import asyncpg

DSN = os.environ["EXPERT_WORK_DB_DSN"].replace("postgresql+asyncpg://", "postgresql://")
SKIP_DBS = ("rdsadmin",)

NEVER_ANALYZED_SQL = (
    "select count(*) from pg_stat_user_tables"
    " where last_analyze is null and last_autoanalyze is null"
)


async def main() -> None:
    conn = await asyncpg.connect(DSN)
    rows = await conn.fetch("select datname from pg_database where not datistemplate order by 1")
    await conn.close()
    for db in (r["datname"] for r in rows if r["datname"] not in SKIP_DBS):
        db_conn = await asyncpg.connect(DSN, database=db)
        await db_conn.execute("set statement_timeout = 0")
        started = time.monotonic()
        try:
            await db_conn.execute("analyze")
            result = f"ok {time.monotonic() - started:.1f}s"
        except Exception as exc:  # report and keep going
            result = f"ERROR {type(exc).__name__}: {str(exc).splitlines()[0][:120]}"
        never = await db_conn.fetchval(NEVER_ANALYZED_SQL)
        print(f"db:{db}\tanalyze\t{result}\ttables_without_stats={never}")
        await db_conn.close()


asyncio.run(main())
