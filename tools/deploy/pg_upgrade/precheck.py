"""PG major-version upgrade precheck — read-only (B-207).

Runbook: ``docs/runbooks/rds-pg-major-upgrade.md`` §1. Runs inside a pod that
carries the control-plane DB config; the source is passed in as an argument
(``python -c "$(cat tools/deploy/pg_upgrade/precheck.py)"``), so the image does
not need to ship this file. Visits every non-template database the app role
can connect to and prints one ``scope<TAB>check<TAB>value`` line per fact —
counts and settings only, never row data or credentials.

The checks mirror what blocks or warns in the Alibaba RDS upgrade check report
(``pg_upgrade`` limits, pgcrypto in ``pg_catalog``, replication slots,
subscriptions). The zero-downtime-mode items (``wal_level``, tables without a
primary / unique key, materialized views, foreign tables, large objects,
sequence count) are printed too, as the recorded reason that mode is not used
(runbook §2). The console check report is still the gate; this only lets us
fix things before asking for it.
"""

from __future__ import annotations

import asyncio
import os

import asyncpg

DSN = os.environ["EXPERT_WORK_DB_DSN"].replace("postgresql+asyncpg://", "postgresql://")
SKIP_DBS = ("rdsadmin",)

SERVER_SETTINGS = (
    "server_version",
    "rds_release_date",
    "wal_level",
    "shared_preload_libraries",
    "max_replication_slots",
    "max_wal_senders",
)

SERVER_QUERIES = (
    ("replication_slots", "select count(*) from pg_replication_slots"),
    ("prepared_xacts", "select count(*) from pg_prepared_xacts"),
    (
        "long_tx_over_5min",
        "select count(*) from pg_stat_activity where xact_start < now() - interval '5 min'",
    ),
    (
        "client_connections",
        "select count(*) from pg_stat_activity where backend_type = 'client backend'",
    ),
)

DB_QUERIES = (
    ("size", "select pg_size_pretty(pg_database_size(current_database()))"),
    (
        "extensions",
        "select string_agg(extname || ' ' || extversion || '@' || extnamespace::regnamespace, ', '"
        " order by extname) from pg_extension",
    ),
    ("pg_class_objects", "select count(1) from pg_class"),
    ("publications", "select count(*) from pg_publication"),
    (
        "subscriptions",
        "select count(*) from pg_subscription where subdbid ="
        " (select oid from pg_database where datname = current_database())",
    ),
    (
        "collation_version_mismatch",
        "select count(*) from pg_database where datname = current_database()"
        " and datcollversion is distinct from pg_database_collation_actual_version(oid)",
    ),
    # pg_upgrade refuses user columns of reg* types other than regclass/regrole/regtype.
    (
        "reg_star_columns",
        "select count(*) from pg_attribute a join pg_class c on c.oid = a.attrelid"
        " join pg_namespace n on n.oid = c.relnamespace"
        " where a.attnum > 0 and not a.attisdropped"
        " and n.nspname not in ('pg_catalog', 'information_schema')"
        " and n.nspname not like 'pg_toast%'"
        " and a.atttypid in ('regcollation'::regtype, 'regconfig'::regtype,"
        " 'regdictionary'::regtype, 'regnamespace'::regtype, 'regoper'::regtype,"
        " 'regoperator'::regtype, 'regproc'::regtype, 'regprocedure'::regtype)",
    ),
    (
        "unlogged_tables",
        "select count(*) from pg_class where relpersistence = 'u' and relkind = 'r'",
    ),
    (
        "index_methods",
        "select string_agg(amname || ':' || n, ', ' order by amname)"
        " from (select am.amname, count(*) n from pg_index i"
        " join pg_class ic on ic.oid = i.indexrelid join pg_am am on am.oid = ic.relam"
        " where am.amname in ('hnsw', 'ivfflat', 'gin', 'gist') group by am.amname) x",
    ),
    # Zero-downtime mode only (logical replication limits).
    (
        "tables_without_pk_or_unique",
        "select count(*) from pg_class c join pg_namespace n on n.oid = c.relnamespace"
        " where c.relkind in ('r', 'p')"
        " and n.nspname not in ('pg_catalog', 'information_schema')"
        " and n.nspname not like 'pg_toast%' and not exists (select 1 from pg_index i"
        " where i.indrelid = c.oid and (i.indisprimary or i.indisunique))",
    ),
    ("materialized_views", "select count(*) from pg_matviews"),
    ("foreign_tables", "select count(*) from pg_foreign_table"),
    ("large_objects", "select count(*) from pg_largeobject_metadata"),
    ("sequences", "select count(*) from pg_sequences"),
)


async def _report(
    conn: asyncpg.Connection, scope: str, queries: tuple[tuple[str, str], ...]
) -> None:
    for name, sql in queries:
        try:
            value = await conn.fetchval(sql)
        except Exception as exc:  # a precheck reports every failure and keeps going
            value = f"ERROR {type(exc).__name__}: {str(exc).splitlines()[0][:120]}"
        print(f"{scope}\t{name}\t{value}")


async def main() -> None:
    conn = await asyncpg.connect(DSN)
    for setting in SERVER_SETTINGS:
        await _report(conn, "server", ((setting, f"show {setting}"),))
    await _report(conn, "server", SERVER_QUERIES)
    rows = await conn.fetch("select datname from pg_database where not datistemplate order by 1")
    await conn.close()
    for db in (r["datname"] for r in rows if r["datname"] not in SKIP_DBS):
        try:
            db_conn = await asyncpg.connect(DSN, database=db)
        except Exception as exc:
            print(f"db:{db}\tconnect\tERROR {type(exc).__name__}")
            continue
        await _report(db_conn, f"db:{db}", DB_QUERIES)
        await db_conn.close()


asyncio.run(main())
