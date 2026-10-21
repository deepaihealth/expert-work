"""PG major-version upgrade data snapshot — read-only (B-207).

Runbook: ``docs/runbooks/rds-pg-major-upgrade.md`` §3 / §5. Taken once with
every writer stopped right before the upgrade and once right after it; the
two outputs must ``diff`` clean. Passed in as an argument like precheck.py.

Prints, per database, the exact row count of every user table; and for the
expert-work database, fixed vector (HNSW) and keyword (tsvector) probes on
``knowledge_chunk`` / ``memory_item``: the top-10 ids for query inputs taken
from existing rows, so no model is called. Ids only, never row content.
"""

from __future__ import annotations

import asyncio
import os

import asyncpg

DSN = os.environ["EXPERT_WORK_DB_DSN"].replace("postgresql+asyncpg://", "postgresql://")
SKIP_DBS = ("rdsadmin", "postgres")

TABLES_SQL = (
    "select n.nspname, c.relname from pg_class c join pg_namespace n on n.oid = c.relnamespace"
    " where c.relkind in ('r', 'p') and n.nspname not in ('pg_catalog', 'information_schema')"
    " and n.nspname not like 'pg_toast%' order by 1, 2"
)

#: (table, seed query) — three fixed seed rows per HNSW-indexed table.
VECTOR_PROBES = (
    (
        "knowledge_chunk",
        "select id from knowledge_chunk where embedding is not null order by id limit 3",
    ),
    (
        "memory_item",
        "select id from memory_item where embedding is not null order by id limit 3",
    ),
)

KEYWORD_SEEDS_SQL = (
    "select id, (tsvector_to_array(content_tsv))[1] lexeme from knowledge_chunk"
    " where content_tsv is not null and length(content_tsv) > 0 order by id limit 3"
)
KEYWORD_PROBE_SQL = (
    "select id from knowledge_chunk where content_tsv @@ plainto_tsquery('simple', $1)"
    " order by ts_rank_cd(content_tsv, plainto_tsquery('simple', $1)) desc, id limit 10"
)


async def _counts(conn: asyncpg.Connection, db: str) -> None:
    for schema, table in await conn.fetch(TABLES_SQL):
        try:
            # Identifiers come from pg_class, not from input; quoted as-is.
            value = await conn.fetchval(f'select count(*) from "{schema}"."{table}"')  # noqa: S608
        except Exception as exc:  # report and keep going
            value = f"ERROR {type(exc).__name__}"
        print(f"{db}\tcount\t{schema}.{table}\t{value}")


async def _vector_probes(conn: asyncpg.Connection, app_db: str) -> None:
    for table, seed_sql in VECTOR_PROBES:
        try:
            seeds = [r["id"] for r in await conn.fetch(seed_sql)]
        except Exception as exc:
            print(f"{app_db}\tvector\t{table}\tERROR {type(exc).__name__}")
            continue
        for seed in seeds:
            async with conn.transaction():
                # Force the HNSW index so both runs take the same path.
                await conn.execute("set local enable_seqscan = off")
                rows = await conn.fetch(
                    # ``table`` is one of the two VECTOR_PROBES constants.
                    f"select id from {table} order by embedding <=>"  # noqa: S608
                    f" (select embedding from {table} where id = $1) limit 10",
                    seed,
                )
            ids = ",".join(str(r["id"]) for r in rows)
            print(f"{app_db}\tvector\t{table}:{seed}\t{ids}")


async def _keyword_probes(conn: asyncpg.Connection, app_db: str) -> None:
    for seed in await conn.fetch(KEYWORD_SEEDS_SQL):
        hits = await conn.fetch(KEYWORD_PROBE_SQL, seed["lexeme"])
        ids = ",".join(str(r["id"]) for r in hits)
        print(f"{app_db}\tkeyword\tknowledge_chunk:{seed['id']}\t{ids}")


async def main() -> None:
    conn = await asyncpg.connect(DSN)
    print("server\tversion\t-\t" + str(await conn.fetchval("show server_version")))
    # The app database is whatever the DSN names (expert_work_dev on test).
    app_db = str(await conn.fetchval("select current_database()"))
    rows = await conn.fetch("select datname from pg_database where not datistemplate order by 1")
    await conn.close()
    for db in (r["datname"] for r in rows if r["datname"] not in SKIP_DBS):
        db_conn = await asyncpg.connect(DSN, database=db)
        await db_conn.execute("set statement_timeout = '120s'")
        await _counts(db_conn, db)
        if db == app_db:
            await _vector_probes(db_conn, app_db)
            await _keyword_probes(db_conn, app_db)
        await db_conn.close()


asyncio.run(main())
