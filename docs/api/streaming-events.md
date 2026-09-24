# Agent Run Streaming Events (SSE)

A streaming agent run emits **Server-Sent Events**. The stream is the response
body of `POST /v1/agents/{agent_code}/runs` (unless `mode=queue`, which returns
`202` JSON and no stream) and can be re-attached via
`GET /v1/sessions/{thread_id}/runs/{run_id}/events`.

Each event has an SSE `event:` name and a JSON `data:` payload. This page
documents the event kinds a client sees; the authoritative, durable record is
the set of persisted frames replayed by the events endpoint.

## Event kinds

Frames published by `orchestrator/sse.py`. The authoritative list is
`orchestrator.stream_items.PUBLISHED_EVENTS`, kept closed against the actual
`_publish_frame` / `publish_ephemeral` call sites by an AST gate
(`services/orchestrator/tests/test_stream_items_vocabulary.py`). Add a frame
kind there or that test goes red.

| `event:` | When | Persisted (replayed on reconnect) |
|---|---|---|
| `metadata` | Once at run start (`run_id`, `thread_id`, trace id) | yes |
| `system_prompt` | Once, right after `metadata`, when the run starts fresh (console plane only — external producers filter it via `EXTERNAL_HIDDEN_EVENTS`) | yes |
| `updates`  | Once per agent/tool step — the **authoritative** step result | yes |
| `plan` | Whole-plan snapshot whenever the plan changes (not a delta) | yes |
| `worker` | Sub-task (child run) lifecycle: one `start`, one `update` per child step, one `end` | yes |
| `guard` | An output-safety guard fired | yes |
| `compaction` | Context was compacted mid-run | yes |
| `retry` | Transient retry notice | yes |
| `approval` | Run paused at a human-approval gate | yes |
| `error` | Run failed (`{message, name}`) | yes |
| `token`    | Fine-grained token preview during an LLM step (see below) | **no (live-only)** |

Three more frames are minted by the API layer, not by `sse.py` — they describe
*this connection*, not the run, and are never persisted
(`orchestrator.stream_items.CONNECTION_EVENTS`):

| `event:` | When |
|---|---|
| `end` | Terminal marker for this connection; a reconnect gets a fresh one. `data` = `{status, run_id, artifacts, usage_by_model, completed, exit_reason}` — `artifacts` is the run's artifact-registration snapshot (产物清单契约): `[]` means an explicit zero-delivery turn; the key is absent only for pre-migration historical runs (no record ≠ zero delivery). The same snapshot is readable from `agent_run.artifacts` via listRuns and the `/items` `runs[]` metadata. **`completed` / `exit_reason` (B-85 ③) are orthogonal to `status`**: `status` only says "the graph ran to the end without raising", `completed` says "the work got done" — `status="success"` with `completed=false` is a legitimate, meaningful combination (a tool failed and the model stopped acting). Both keys absent = no record (pre-migration run, or the graph state was unreadable) — **never read absence as `false`**. `exit_reason` is a closed set: `text_response` / `max_steps` / `no_progress` / `token_budget` / `approval_pending` / `approval_rejected`; everything but `text_response` means the platform stopped the run, so `completed` is always `false` there. Consumers判成功 should use `status === "success" && completed !== false` (`!== false`, not `=== true`, so an absent key keeps the old verdict) |
| `gap` | Replay found a hole in the persisted seq range |
| `truncated` | Replay hit the page limit; carries `next_seq` |

## `stream_format=items`

The four **external** SSE entry points accept `stream_format`
(`legacy`, the default, or `items`); see
`docs/superpowers/specs/2026-08-25-conversation-items-design.md`. Under
`items` the consumer-side converter (`orchestrator/stream_items.py`) replaces
`updates` / `token` / `plan` / `approval` / `error` with `item.added` /
`item.delta` / `item.done`, leaving the other frames untouched. The event store
always holds legacy frames only — conversion happens on read, per connection.

The full items-mode wire set is **11** events
(`orchestrator.stream_items.ITEMS_WIRE_EVENTS`, pinned by
`test_items_wire_vocabulary_is_closed`): `item.added`, `item.delta`,
`item.done`, `metadata`, `end`, `gap`, `truncated`, `guard`, `compaction`,
`retry`, `worker`. The design doc's prose says nine — it predates the constant.
Count from the constant, not from that paragraph.

`worker` is deliberately **not** converted: folding a sub-task into
`tool_call.worker` would force the tool card's `item.done` to wait for the
child's `end`. History (`GET /v1/agents/{code}/sessions/{id}/items`) does fill
that field, since there the frames are all already on hand. That is the one
place the two shapes differ.

The console replay endpoint (`GET /v1/sessions/{thread_id}/runs/{run_id}/events`)
does **not** take `stream_format`; it is legacy-only.

## Consuming `worker` frames

Agents with dynamic sub-agents delegate parts of a task to ephemeral child
runs ("workers"). Agents configured with structured execution
(`workflow.execution_mode: plan_first`) do this **routinely and in
parallel** — a single run can carry several concurrent workers and thousands
of frames, so a consumer that has only ever seen worker-free streams should
verify each point below.

Every `worker` frame shares one envelope:

| field | value |
|---|---|
| `worker_id` | The child run's id — group a worker's frames by it |
| `parent_worker_id` | Set when a worker itself delegated (nested); `null` at depth 1 |
| `parent_tool_call_id` | The tool call that spawned this worker |
| `label` | `"spawn_worker"` for dynamic workers; the sub-agent tool name for statically declared ones |
| `agent_ref` | `"dynamic:<role>"`, or `name@version` for a declared sub-agent |
| `depth` | Nesting depth (1 = direct child) |
| `kind` | `"start"` \| `"update"` \| `"end"` |
| `wseq` | Monotonic sequence **within this worker** (not global) |
| `data` | Per-kind payload, below |

Per-kind `data`:

- `start` — `task_excerpt` (the delegated task, truncated to 500 chars),
  `role` (nullable), `max_steps`.
- `update` — one per child step: `node` (`"agent"` / `"tools"` / …),
  `_duration_ms`, `step_count` (when known), and `messages` — an array of
  summaries: `{type: "ai", content_excerpt, tool_calls?: [{name,
  args_excerpt}]}` or `{type: "tool", name, tool_result_excerpt, …}`. All
  excerpts are truncated; they are a progress view, not the full transcript.
- `end` — `outcome`, `iteration_used`, `llm_call_count` (the number of model
  calls counted in `usage`; when the platform recorded no usage for the
  worker, the number of model replies), `wall_clock_ms`, `usage` and
  `usage_by_model` (both optional, see below).

### A worker's token usage

The `end` frame carries a `usage` block with the tokens that worker spent:

```json
{
  "input_tokens": 3200800,
  "output_tokens": 118014,
  "total_tokens": 3318814,
  "input_token_details": {"cache_read": 900000, "cache_creation": 7},
  "output_token_details": {"reasoning": 20000}
}
```

`usage` covers every model call the worker made on the user's behalf:

- the worker's own replies, including a reply that a fallback model gave
  after the configured model failed;
- planning and reflection steps;
- summarising earlier conversation when the context grows long;
- reading and writing long-term memory;
- questions about images (the vision model).

Calls the platform makes for its own safety checks and for ranking search
results are not included. `llm_call_count` counts the same calls.

Next to `usage`, `usage_by_model` splits the same account by the model that
actually answered each call. It is a list with one entry per
`(provider, model)`; each entry carries `provider`, `model` and the same
fields as `usage`. The entries sum to `usage`, field by field.

The list can hold several entries. Each of the following gets its own entry:

- the worker's own model, which is not necessarily the parent agent's model
  (`dynamic_workers.model`);
- a fallback model, for the calls it answered;
- the vision model, for image questions;
- the model that the agent's routing rules assign to planning or reflection,
  when it differs from the worker's own model.

`model` is the model name as configured on the platform, not the name the
provider reports back.

```json
[
  {
    "provider": "zhipu",
    "model": "glm-5.3",
    "input_tokens": 3200000,
    "output_tokens": 117974,
    "total_tokens": 3317974,
    "input_token_details": {"cache_read": 900000, "cache_creation": 7},
    "output_token_details": {"reasoning": 20000}
  },
  {
    "provider": "qwen",
    "model": "qwen-vl-max",
    "input_tokens": 800,
    "output_tokens": 40,
    "total_tokens": 840,
    "input_token_details": {"cache_read": 0, "cache_creation": 0},
    "output_token_details": {"reasoning": 0}
  }
]
```

Price a run by summing each entry's tokens at that entry's own
`(provider, model)` rate. `usage_by_model` is absent when the platform
recorded no model calls for that worker; in that case price `usage` at the
parent agent's rate. The run's persisted rollup (`tokens.usage_by_model` on
the run and conversation records) carries the same split, computed from the
platform's usage records instead of the events.

How to get a run's total tokens:

- **Use the run-level total, not a sum of events.** The authoritative total
  is `usage_by_model` on the run's `end` event (the last event on the
  stream), or the same data from `GET /v1/agents/{code}/runs/{run_id}/usage`.
  It covers every model call made on the user's behalf: the main agent, its
  workers, and the extra calls listed below. As with a worker's `usage`, the
  platform's own safety checks and search-result ranking are not included.
- **Events show progress, not the full account.** Summing `usage_metadata`
  from `updates` events and `usage` from worker `end` events gives a lower
  number than the run-level total. The `updates` events carry only the main
  agent's replies. The main agent's own planning, reflection, conversation
  summaries, long-term memory reads and writes, and image questions produce
  no `updates` event, so they appear only in the run-level total. A worker's
  `usage`, by contrast, already includes that worker's own calls of those
  kinds. Use the event sum for an in-progress display only.
- **Count each worker's `end` event once.** Each worker emits exactly one,
  and a nested worker reports on its own `end` event, so adding up every
  worker `end` event counts each worker exactly once.
- **`usage` may be absent, and absent is not zero.** Token counts are
  optional for some providers and some cache paths. A missing block means
  "not reported", so treat it as unknown rather than free.
  `outcome` is one of `"success"`, `"max_steps"` (partial result, not a
  failure), `"cancelled"`, or `"approval_blocked"` (the worker hit a tool
  that requires human approval, which is unavailable inside a worker — the
  main agent takes the sub-task back). Treat unknown outcome values as
  non-success rather than erroring.

Consumer checklist:

1. **Event allowlist** — if you filter the stream by event name, `worker`
   must be on the list; otherwise sub-tasks silently vanish from your
   timeline.
2. **Handle all three kinds**, and skip unknown kinds without aborting the
   stream.
3. **Interleaving** — frames from concurrent workers interleave. Group by
   `worker_id`, order within a group by `wseq`; never assume one worker's
   frames arrive contiguously.
4. **Reconnect idempotency** — `worker` frames are persisted and replayed;
   deduplicate on `(worker_id, wseq)`.
5. **No token stream inside a worker** — `token` frames always belong to the
   main agent. A worker's progress is only ever `update`-frame granular.
6. **Volume** — budget parsing and rendering for runs with thousands of
   frames.

Frames persisted before 2026-08-27 may carry datamark fencing inside the
excerpts (since fixed at the source); strip it on historical replays.

## The `token` event (provisional preview)

For a streaming-capable run, the model's answer text is previewed token-by-token
as it is generated:

```
event: token
data: {"step": 0, "channel": "content", "text": "partial answer fragment"}
event: token
data: {"step": 0, "channel": "reasoning", "text": "let me think about..."}
event: token
data: {"step": 0, "channel": "tool_args", "tool_index": 0, "call_id": "call_de58e676916d442d925bff27", "name": "search_web"}
```

- `step` — the agent step index the fragment belongs to.
- `channel` — one of `"content"` (answer text), `"reasoning"` (the model's
  thinking, for reasoning-capable models), or `"tool_args"` (a tool call is
  being made).
- `content` / `reasoning` frames carry `text` — an already-redacted fragment.
- `tool_args` frames carry `call_id`, `tool_index` and `name` (the tool being
  called), emitted once when the name first appears. The tool
  **arguments are not streamed**; they arrive complete on the authoritative
  `updates` frame.
  - `call_id` is the vendor tool-call id — identical to `ai.tool_calls[].id`
    on the `updates` frame and to the tool result's `tool_call_id`. It is the
    **only** correct key for pairing a preview card with its final call.
  - `tool_index` is a per-connection dedup key, **not** an array subscript. Its
    meaning is provider-specific: on the OpenAI wire it is the assistant
    message's `tool_calls[]` index, but on the Anthropic wire it is the
    `content` block index — text and thinking blocks consume numbers too, and a
    dropped incomplete call shifts the final array. Never pair on it.

**`token` frames are provisional.** Treat them as a live typewriter preview only:

1. Accumulate `token.text` (per `step`) for live display.
2. When the `updates` frame for that step arrives, it is **authoritative** —
   replace the accumulated preview with the content from `updates`. The
   `updates` content has passed the full output-safety guards; a run that is
   blocked by a guard yields a refusal in `updates` that supersedes any preview.
3. On reconnect, `token` frames are **not** replayed — only the persisted
   `metadata` / `updates` / … frames are. Rebuild state from those.

## Which runs emit `token`

Emitted for streaming-provider runs **without** a model-backed output judge.
Not emitted (only step-level `updates`, exactly as before) for: `mode=queue`,
cached responses, non-streaming providers, and runs with the output judge enabled.
Structured-output runs DO emit `token` frames for the primary candidate (the schema is enforced only on a correction resend, which does not stream).
