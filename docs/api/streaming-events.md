# Agent Run Streaming Events (SSE)

A streaming agent run emits **Server-Sent Events**. The stream is the response
body of `POST /v1/agents/{agent_code}/runs` (unless `mode=queue`, which returns
`202` JSON and no stream) and can be re-attached via
`GET /v1/sessions/{thread_id}/runs/{run_id}/events`.

Each event has an SSE `event:` name and a JSON `data:` payload. This page
documents the event kinds a client sees; the authoritative, durable record is
the set of persisted frames replayed by the events endpoint.

## Event kinds

| `event:` | When | Persisted (replayed on reconnect) |
|---|---|---|
| `metadata` | Once at run start (`run_id`, `thread_id`, trace id) | yes |
| `system_prompt` | Once, right after `metadata`, when the run starts fresh (console plane only — external producers filter it) | yes |
| `updates`  | Once per agent/tool step — the **authoritative** step result | yes |
| `token`    | Fine-grained token preview during an LLM step (see below) | **no (live-only)** |
| `approval` | Run paused at a human-approval gate | yes |
| `retry` / `error` / `end` | Retry notice / failure / terminal | yes / yes / — |

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
