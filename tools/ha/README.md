# Stream 9.4 / 9.5 + 多副本 CAS 守卫 — HA live E2E

Three live-proof harnesses share this dir:

- **`verify_failover.py`** (Stream 9.4) — orphaned-run automatic hot-handoff.
- **`verify_queue.py`** (Stream 9.5) — distributed run-queue cross-instance drain
  (see [§ Stream 9.5](#stream-95--distributed-run-queue-live-e2e) below).
- **`verify_cancel.py`**(多副本 CAS 守卫执行面)— cross-replica cancel:
  非属主副本发 cancel → durable CAS → 属主心跳 CAS 失败 → abort。四断言:
  行立即 interrupted+user_cancel / blue 在心跳检测上界内停(end 帧)/ 属主
  心跳冻结 / 不复活。用法与两个姊妹脚本一致(`EXPERT_WORK_API_TOKEN` + 双色栈)。

---

## Stream 9.4 — HA-failover live E2E

Proves the orphaned-run **automatic hot-handoff** end to end against a real
two-instance dev stack. CI can't cover this: it needs two live control-plane
processes sharing one Postgres, a real model key (resolved server-side), and an
abrupt `docker kill` mid-run.

## What it proves

1. A run starts on **blue** and durably claims its ownership lease.
2. **blue** is killed mid-run (abrupt crash, no terminal status write) — the run
   is now an orphan: `status=running` with a lease nobody renews.
3. **green**'s `OrphanSweep` detects the expired lease, reclaims it (reclaim CAS
   → exactly-one winner), adopts it, and resumes from the durable LangGraph
   checkpoint via `run_agent(graph_input=None)`.
4. The run reaches `success` under a **different** owner with `reclaim_count >= 1`,
   a `run:failover` audit row exists, and green's
   `expert_work_run_orphan_reclaimed_total` metric incremented.

## Prerequisites

- Dev stack built with this branch (`make dev-up` — rebuilds the control-plane
  image from the working tree and runs Alembic migration `0081_agent_run_lease`).
- A **second** control-plane instance (green). `make dev-up` excludes green
  because its default host port `8001` collides with the sandbox supervisor, so
  bring it up with the overlay (remaps green to `8002`):

  ```bash
  docker compose -f infra/docker-compose.yml -f infra/docker-compose.ha-e2e.yml \
      --profile full up -d --no-deps control-plane-green
  ```

- An **active domestic-model agent** (deepseek/qwen/…) whose key is resolvable
  server-side, and a dev-login bearer token.

## Run

```bash
export EXPERT_WORK_API_TOKEN=<a dev-login bearer token>   # never logged
uv run python tools/ha/verify_failover.py            # auto-picks a domestic agent
uv run python tools/ha/verify_failover.py --agent my-agent@1.0.0
```

Exit code is non-zero if failover did not complete cleanly. Lease fields
(`claimed_by` / `reclaim_count`) aren't on the run API, so they're read straight
from Postgres via `docker exec expert-work-postgres psql` (the dev superuser bypasses
RLS); green's metrics are scraped over its container loopback via `docker exec`,
so green needs no reachable host port.

---

## Stream 9.5 — distributed run-queue live E2E

Proves the **distributed run queue**: a run submitted to one instance is drained
to completion by a *different* instance, exactly once. CI can't cover this — its
tests monkeypatch `run_agent` and run a single in-memory store.

### What it proves

1. A run is submitted to **blue** with `mode=queue` → blue returns `202` and
   persists a `status='queued'` row owned by no process (no `claimed_by`).
2. **blue**'s own queue worker is disabled (overlay), so the row can only be
   claimed by green. blue is then killed — proving the work survives the
   submitter's death.
3. **green**'s `RunQueueWorker` CAS-claims the queued run (`status='queued'` →
   `running` + lease → exactly-one winner), rebuilds the agent, and executes it
   from the persisted `enqueued_input`.
4. The run reaches `success` owned by **green** with `reclaim_count == 0` (a
   queue claim, not a failover reclaim), and green's
   `expert_work_run_queue_dequeued_total` metric incremented.

### Prerequisites

- Dev stack built with this branch (`make dev-up` — rebuilds the control-plane
  image from the working tree and runs the `0082_agent_run_queue` migration).
- **blue + green** brought up with the queue-e2e overlay (blue's queue worker
  off, green remapped to `8002`):

  ```bash
  docker compose -f infra/docker-compose.yml -f infra/docker-compose.queue-e2e.yml \
      --profile full up -d --no-deps control-plane-blue control-plane-green
  ```

- An **active domestic-model agent** and a dev-login bearer token.

### Run

```bash
export EXPERT_WORK_API_TOKEN=<a dev-login bearer token>   # never logged
uv run python tools/ha/verify_queue.py               # auto-picks a domestic agent
uv run python tools/ha/verify_queue.py --agent my-agent@1.0.0
```
