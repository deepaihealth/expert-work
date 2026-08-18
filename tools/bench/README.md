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

It's also the earliest of **all** LLM calls in the trace, not specifically
the main generation call. An agent with an auxiliary LLM call inside the
entry chain itself (e.g. a query-rewrite step that runs inside
`memory.recall`, necessarily before the main generation) will report that
auxiliary call's start time, not the main call's. This doesn't affect a
before/after comparison for this program's connection-pooling work (TLS
reuse applies to every outbound LLM call, auxiliary or not), but it is
misleading if you use `first_llm_start` to compare "time to main-generation
start" *across different agents* that don't have the same auxiliary-call
shape.

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

### A third clock: the admin-ui breakdown bar

TraceView's pre-first-token breakdown bar (`entry_breakdown.ts`,
`trace.breakdown_title`) shows a third number, and it's a different clock
from both of the above:

- **This script's `first_llm_start`** — a point in time: the earliest
  `startMs` among `kind == "llm"` spans (proxy for "main generation
  starts").
- **`expert_work_first_output_seconds`** — a point in time: the first
  token frame, or the first `agent` updates chunk when there's no
  streaming (proxy for "the user sees something").
- **The breakdown bar's total** — a *duration*, not a point: the
  top-level entry-chain segments plus the **complete latency** of the
  first LLM span (start to finish, i.e. including its own generation
  time), not just its start. 入口链并行化(P1.3)后该 total 为段之和,
  大于真实墙钟,对照请以本脚本输出的顶层 `total_ms` 为准。

All three are legitimate, they just answer different questions, so
they won't line up numerically even on the same run — the breakdown
bar's total is always ≥ the other two, by however much of the first LLM
call's prefill + generation time the other two clocks stop short of. A
mismatch here is not a bug to chase.

### Bench segment keys are the facade's Chinese labels — the two are coupled

This script aggregates by span **label** (`segments.<label>.median/p95`
in the output YAML), and those labels are the trace facade's Chinese
display text (e.g. `记忆召回`), not the underlying span name. Renaming a
label in the facade doesn't break this script or produce invalid YAML —
it silently produces a *different key* for what's semantically the same
span, so an old baseline and a new one stop being comparable side by
side without any error. If you rename a traced span's label, re-run the
bench and refresh the committed baselines in the same change.

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

### 二期:8 段全亮的跑法(bench-entry manifest + seed 记忆轮)

一期基线只亮 8 段中的 5 段:固定 prompt 打在空记忆库上零召回结果
(rerank / bump_access / verify 都不触发),而且没开持久工作区
(workspace_ingest 段不存在)。二期把缺的段点亮,分三步:

1. **注册 bench agent** — `tools/bench/manifests/bench-entry.yaml`
   (`memory.long_term` 全链 write_back + verify_reads,外加
   `sandbox.filesystem.persistent_workspace`):

   ```bash
   curl -sS -X POST "$EXPERT_WORK_API_URL/v1/agents" \
       -H "Authorization: Bearer $EXPERT_WORK_API_TOKEN" \
       -H "Content-Type: application/json" \
       --data "$(jq -n --rawfile m tools/bench/manifests/bench-entry.yaml \
           '{manifest_yaml: $m}')"
   ```

   Body key 是 **`manifest_yaml`**(不是 `{"manifest": ...}` —— 那个拼法
   只在计划文档里出现过)。定论来源:`POST /v1/agents` 的请求模型
   `ManifestPayload`(`services/control-plane/src/control_plane/api/agents.py`)
   只收 `{"manifest_yaml": "..."}`,发 `{"manifest":
   ...}` 会因缺字段 422。manifest 本身有解析守卫
   (`test_bench_entry_manifest_parses_against_protocol_schema`):schema
   漂移在 CI 先红,不会等到注册时才 422。

2. **Seed 记忆轮** — 加 `--seed-prompt-file tools/bench/prompts/seed.txt`,
   脚本在建 bench session **之前**先用一条独立的一次性 session 跑一轮
   种子对话:run 终态 success 即 `memory_writeback` 已落库(它是 graph
   的 end 前节点);记忆是 (tenant, user, agent) 维度、跨 session 可召回,
   所以 bench 轮的召回自然非空。种子轮**不计入 bench 数据**——不拉
   trace、不进 `per_run`、不影响 `meta.runs` / `successful_runs`。种子轮
   失败直接抛(fail-fast):种子没种上,后面所有轮的召回全空,数据白跑。
   `seed.txt` 的内容与 `fixed.txt` 的提问语义重叠(团队系统组件 + 开发
   流程)——召回是语义检索,种不相关的记忆等于没种。

3. **正常跑 bench**(与一期相同,换 agent 名 + 带上 seed):

   ```bash
   uv run python tools/bench/entry_latency.py \
       --agent bench-entry@2.0.0 \
       --prompt-file tools/bench/prompts/fixed.txt \
       --seed-prompt-file tools/bench/prompts/seed.txt \
       --runs 10 \
       --out tools/bench/baselines/2026-07-27-phase2-before.yaml
   ```

#### `verify_ms` 顶层节

verify_reads 开着时,trace facade 给「记忆校验」输出的是一个
`kind == "llm"` 的 span,`group` 是 `null` **不是** `"entry"`(facade
`_LLM_LABELS` 定死),所以它进不了 `segments` —— 脚本按 label 单独抓,
写进顶层 `verify_ms` 节(median / p95 / n,`n` = 实际出现 verify span
的轮数)。这是二期 P1.4(verify on/off 两组基线)的对照数据源。

#### verify 开着时 `first_llm_start` 的含义变了

`first_llm_start` 取的是全 trace 最早的 `kind == "llm"` span 的
`startMs`,而 verify 本身就是一次 LLM 调用、且发生在主生成之前 —— 所以
verify 开着时 `first_llm_start` 量的是 **verify 调用的开始时间**,不再
近似"主生成开始"。verify on/off 两组对照不要直接比 `first_llm_start`;
对照口径看顶层 **`total_ms`**(整 run 墙钟,来自 trace 根节点的
`trace.latencyMs`)+ `verify_ms` 自身。Σ segments **不可**作端到端:
segments 里父 span(记忆召回)与子 span(向量化/检索/读配置)并列,
直接求和双计;且入口链并行化(P1.3)后段之和 > 真实墙钟。

The output YAML is written after **every** round, not just once at the end
— a round that fails part-way through a batch (network blip, a gateway
mangling a response body, …) doesn't discard the real LLM data already
collected by earlier rounds. `meta` also records `successful_runs` /
`failed_runs` (distinct from the requested `runs` count) and a
`prompt_sha256` content hash alongside `prompt_file`, so an all-failed run
(e.g. a typo'd `--agent` version) can't be mistaken for "0 real
measurements because this agent has no entry-chain spans", and a
before/after pair pointed at a since-edited prompt file is detectable
instead of silently attributing a prompt-length difference to the
optimization being measured.

### Tests

```bash
uv run pytest tools/bench/test_entry_latency.py -v
```

Covers the pure functions (`aggregate`, `extract_run_metrics`,
`_exit_status`, `_prompt_fingerprint`, `_write_result`) plus `run_rounds`'s
per-round fault tolerance via `httpx.MockTransport` (no live stack needed).
The full live-stack path (session creation, driving a real agent, real
Langfuse ingestion) isn't covered — see `.superpowers/sdd/perf-task-4-report.md`
for what a coordinator running it for real should know.
