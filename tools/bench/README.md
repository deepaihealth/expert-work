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

### `first_llm_start` is NOT `first_output_seconds`

The output YAML's `first_llm_start` section (median/p95, alongside
`segments`) is **not** the same measurement as Task 3's
`expert_work_first_output_seconds` Prometheus histogram, even though both
answer "how long until the agent starts producing something".

This script only reads the trace facade (`GET .../trace`) — the trace has
no span for "first token frame" or "first `agent` updates chunk" (that's
a Prometheus-only counter Task 3 added directly in `sse.py`, with no
matching span). So `first_llm_start` is a **proxy**: the earliest
`startMs` among the trace's `kind == "llm"` spans, i.e. the moment the
first LLM call *starts*, not the moment its first token arrives. A model's
prefill sits between those two clocks — anywhere from a few hundred
milliseconds to a few seconds, depending on prompt length and model.

Practical consequence:

- **Comparing this script's own before/after runs is valid** — the one
  optimization this program is chasing (TLS handshake reuse) happens
  *inside* the LLM call, so it's fully captured by the LLM span's own
  latency and by `first_llm_start` moving. Front-to-back within this tool,
  the numbers are apples-to-apples.
- **Comparing `first_llm_start` against the `first_output_seconds` number
  on a Grafana dashboard is not valid** — they will not match, and the gap
  is not a bug in either measurement. If you see a large discrepancy,
  that's the prefill gap, not something broken.

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
