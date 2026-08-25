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
| `end` | Terminal marker for this connection; a reconnect gets a fresh one |
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
