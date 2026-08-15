/**
 * SSE frame-id ms parser — M2: a dependency-free leaf module so
 * `timeline.ts`, `tool_timeline.ts`, and `worker_timeline.ts` can each
 * import `serverMsOf` directly instead of reaching into
 * `gantt_timeline.ts` (which itself imports `parseTimeline` from
 * `timeline.ts` — a three-way cycle that only worked because JS/TS module
 * cycles resolve lazily). `gantt_timeline.ts` re-exports `serverMsOf` so
 * existing `import { serverMsOf } from "./gantt_timeline"` call sites (and
 * tests) keep working unchanged.
 */

/** Extract the millisecond segment from an SSE frame id (`"{server_ms}-{seq}"`
 *  — live `stream_bridge/memory.py` and replay `runs.py` both emit this
 *  shape). `null` on a missing or malformed id. */
export function serverMsOf(id: string | null): number | null {
  if (id === null) return null;
  const m = /^(\d{10,})-\d+$/.exec(id);
  return m === null ? null : Number(m[1]);
}

/** Extract the seq segment from an SSE frame id (`"{server_ms}-{seq}"`) —
 *  the durable `run_event.seq`, which is what `since_seq` takes on reconnect.
 *  `null` on a missing or malformed id, which covers every frame the server
 *  deliberately sends without an `id:` line (`end` / `truncated` / `gap` /
 *  `token` — see the SSE contract table). */
export function seqOf(id: string | null): number | null {
  if (id === null) return null;
  const m = /^\d{10,}-(\d+)$/.exec(id);
  return m === null ? null : Number(m[1]);
}
