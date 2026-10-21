#!/usr/bin/env bash
# B-208 — HNSW recall check, READ-ONLY.
#
#   tools/deploy/pg_upgrade/vector_recall_check.sh prod     (or: test)
#
# Runbook: docs/runbooks/rds-pg-major-upgrade.md §6 / §9. Results so far
# (2026-10-21): test PG 16 before the upgrade — HNSW 1-6/10 vs exact; test
# PG 17.11 — 100% in every section; prod PG 16.14 — [A] 60%, [C] 100%
# because the app's filtered memory query never takes the HNSW path there.
#
# Runs one Python probe inside a Running+Ready control-plane pod with the
# pod's own DB config. The session is forced read-only
# (default_transaction_read_only = on) with a 30 s statement timeout. For a
# few seed rows it compares the HNSW-index top-10 with the exact top-10
# (index scans disabled = true nearest neighbours) and prints only counts:
# no ids, no tenant / user ids, no content, no credentials.
set -euo pipefail

case "${1:-}" in
    test) export KUBECONFIG="${HOME}/.kube/expert-work-test.yaml" ;;
    prod) export KUBECONFIG="${HOME}/.kube/expert-work-prod.yaml" ;;
    *) echo "Usage: $0 test|prod" >&2; exit 2 ;;
esac

pod="$(kubectl -n expert-work get pods -l app.kubernetes.io/name=control-plane \
    --field-selector=status.phase=Running \
    -o jsonpath='{range .items[*]}{range .status.conditions[?(@.type=="Ready")]}{.status}{end}|{.metadata.deletionTimestamp}|{.metadata.name}{"\n"}{end}' \
    | awk -F'|' '$1 == "True" && $2 == "" {print $3; exit}')"
[[ -n "${pod}" ]] || { echo "no Running+Ready control-plane pod" >&2; exit 1; }
echo "env=$1 pod=${pod}"

read -r -d '' PROBE <<'PY' || true
import asyncio, os
import asyncpg

DSN = os.environ["EXPERT_WORK_DB_DSN"].replace("postgresql+asyncpg://", "postgresql://")
SEEDS = 5


async def top10(c, sql, args, settings):
    async with c.transaction():
        for s in settings:
            await c.execute(s)
        plan = await c.fetchval("explain (format json) " + sql, *args)
        ids = [r[0] for r in await c.fetch(sql, *args)]
    return ids, "embedding_idx" in str(plan)


def line(label, got, exact, seed):
    ids, used_hnsw = got
    hit = len(set(ids) & set(exact))
    print(f"  {label:<38} returned={len(ids):>2}  overlap_with_exact={hit:>2}/{len(exact):<2}  seed_first={bool(ids) and ids[0] == seed}  hnsw_used={used_hnsw}")
    return hit, len(exact)


async def main():
    c = await asyncpg.connect(DSN)
    await c.execute("set default_transaction_read_only = on")
    await c.execute("set statement_timeout = '30s'")
    print("server", await c.fetchval("show server_version"), "kernel", await c.fetchval("show rds_release_date"),
          "vector", await c.fetchval("select extversion from pg_extension where extname = 'vector'"))
    for r in await c.fetch(
        "select ic.relname, pg_size_pretty(pg_relation_size(ic.oid)) sz, i.indisvalid from pg_index i"
        " join pg_class ic on ic.oid = i.indexrelid join pg_am am on am.oid = ic.relam where am.amname = 'hnsw' order by 1"):
        print("index", r["relname"], r["sz"], "valid" if r["indisvalid"] else "INVALID")
    for t in ("knowledge_chunk", "memory_item"):
        s = await c.fetchrow(
            "select n_live_tup, n_dead_tup, last_vacuum, last_autovacuum, last_analyze, last_autoanalyze"
            " from pg_stat_user_tables where relname = $1", t)
        n = await c.fetchval(f"select count(*) from {t} where embedding is not null")
        print(f"table {t} rows_with_embedding={n} live={s['n_live_tup']} dead={s['n_dead_tup']}"
              f" last_vacuum={s['last_vacuum'] or s['last_autovacuum']} last_analyze={s['last_analyze'] or s['last_autoanalyze']}")

    exact_cfg = ["set local enable_indexscan = off", "set local enable_bitmapscan = off"]
    force_hnsw_cfg = ["set local enable_seqscan = off"]
    app_kb_cfg = ["select set_config('hnsw.ef_search', '200', true)",
                  "select set_config('hnsw.iterative_scan', 'strict_order', true)"]
    totals = {}

    def add(key, res):
        h, n = totals.get(key, (0, 0))
        totals[key] = (h + res[0], n + res[1])

    print("\n[A] unfiltered, HNSW forced, server defaults (same probe as the test-env snapshot)")
    for t in ("knowledge_chunk", "memory_item"):
        for (seed,) in await c.fetch(f"select id from {t} where embedding is not null order by id limit {SEEDS}"):
            q = f"select id from {t} order by embedding <=> (select embedding from {t} where id = $1) limit 10"
            ex, _ = await top10(c, q, [seed], exact_cfg)
            add(f"A {t}", line(t, await top10(c, q, [seed], force_hnsw_cfg), ex, seed))

    print("\n[B] knowledge search as the app runs it (tenant + kb filter, ef_search 200, iterative strict_order)")
    q = ("select id from knowledge_chunk where tenant_id = $2 and kb_id = $3"
         " order by embedding <=> (select embedding from knowledge_chunk where id = $1) limit 10")
    for seed, tenant, kb in await c.fetch(
            f"select id, tenant_id, kb_id from knowledge_chunk where embedding is not null order by id limit {SEEDS}"):
        ex, _ = await top10(c, q, [seed, tenant, kb], exact_cfg)
        add("B app", line("app settings (planner's own choice)", await top10(c, q, [seed, tenant, kb], app_kb_cfg), ex, seed))
        add("B seqscan-off", line("seqscan off, ef 40, no iterative", await top10(c, q, [seed, tenant, kb], force_hnsw_cfg), ex, seed))

    print("\n[C] memory recall as the app runs it (tenant + user filter, not deleted, no scan settings)")
    q = ("select id from memory_item where tenant_id = $2 and user_id = $3 and deleted_at is null"
         " order by embedding <=> (select embedding from memory_item where id = $1) limit 10")
    for seed, tenant, user in await c.fetch(
            f"select id, tenant_id, user_id from memory_item where embedding is not null and deleted_at is null order by id limit {SEEDS}"):
        ex, _ = await top10(c, q, [seed, tenant, user], exact_cfg)
        add("C app", line("app settings (planner's own choice)", await top10(c, q, [seed, tenant, user], []), ex, seed))
        add("C seqscan-off", line("seqscan off, ef 40, no iterative", await top10(c, q, [seed, tenant, user], force_hnsw_cfg), ex, seed))

    print("\nSUMMARY (overlap with the exact top-10, all seeds)")
    for k, (h, n) in totals.items():
        print(f"  {k:<26} {h}/{n} = {100 * h / n:.0f}%" if n else f"  {k:<26} no rows")
    await c.close()


asyncio.run(main())
PY

kubectl -n expert-work exec "${pod}" -- python -c "${PROBE}"
