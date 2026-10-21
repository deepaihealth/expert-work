"""Exact nearest neighbours for the snapshot's vector probes (B-207).

Runbook: ``docs/runbooks/rds-pg-major-upgrade.md`` §6. When the vector probe
lines of the before / after snapshots differ, this is the referee: the same
seeds and query as ``snapshot.py``, but with index scans disabled so Postgres
computes the true top-10 by sequential scan. Whichever snapshot matches this
is the correct one. Ids only, never row content; read-only.

Test 2026-10-21: the PG 16 HNSW results overlapped the exact top-10 by only
1-6/10 (the seed was missing from its own neighbours in 5 of 6 probes); after
the upgrade all six matched 10/10 — the difference was a fix, not damage.
"""

from __future__ import annotations

import asyncio
import os

import asyncpg

DSN = os.environ["EXPERT_WORK_DB_DSN"].replace("postgresql+asyncpg://", "postgresql://")
TABLES = ("knowledge_chunk", "memory_item")


async def main() -> None:
    conn = await asyncpg.connect(DSN)
    for table in TABLES:
        seeds = [
            r["id"]
            for r in await conn.fetch(
                # ``table`` is one of the TABLES constants; same seeds as snapshot.py.
                f"select id from {table} where embedding is not null order by id limit 3"  # noqa: S608
            )
        ]
        for seed in seeds:
            async with conn.transaction():
                await conn.execute("set local enable_indexscan = off")
                await conn.execute("set local enable_bitmapscan = off")
                rows = await conn.fetch(
                    f"select id from {table} order by embedding <=>"  # noqa: S608
                    f" (select embedding from {table} where id = $1) limit 10",
                    seed,
                )
            ids = ",".join(str(r["id"]) for r in rows)
            print(f"exact\t{table}:{seed}\t{ids}")
    await conn.close()


asyncio.run(main())
