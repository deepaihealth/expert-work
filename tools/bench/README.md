# tools/bench

Dev-only bench scripts, not an installed workspace package (no
`__init__.py` — see `conftest.py`, same convention as `tools/eval`).

## entry_latency.py

Entry-chain latency take script — Task 4 of the [agent latency
observability plan](../../docs/superpowers/plans/2026-07-27-agent-latency-observability.md).
Runs N rounds of a fixed prompt against a real agent on a running
control-plane, pulls each run's span tree from the trace facade
(`GET /v1/sessions/{thread_id}/runs/{run_id}/trace`), aggregates the 8
entry-chain span latencies (`group == "entry"`) into median/p95 per
segment, and writes a baseline YAML.

Not a benchmark framework — a take script. The four Task-2-epic
optimizations in a later phase reuse it to measure before/after.

### Requirements

- A running control-plane (+ full stack — the agent actually executes;
  this is a live-stack tool, not something CI runs).
- A bearer token (`EXPERT_WORK_API_TOKEN`) for a tenant that has the
  target agent registered, ACTIVE, and bound to real model credentials.
- A text file with the fixed prompt to repeat every round.

### Usage

```bash
export EXPERT_WORK_API_URL=http://localhost:8000
export EXPERT_WORK_API_TOKEN=<a dev-login bearer token>
uv run python tools/bench/entry_latency.py \
    --agent my-agent@1.0.0 \
    --prompt-file tools/bench/prompts/fixed.txt \
    --runs 10 \
    --out tools/bench/baselines/2026-07-27-before.yaml
```

Run as a script (`uv run python tools/bench/entry_latency.py`), not as a
module (`-m`) — `tools/bench` isn't a package.

### Tests

```bash
uv run pytest tools/bench/test_entry_latency.py -v
```

Covers the pure functions only (`aggregate`, `extract_run_metrics`) —
the HTTP / live-stack path isn't unit-tested (see
`.superpowers/sdd/perf-task-4-report.md` for why and what a coordinator
running it for real should know).
