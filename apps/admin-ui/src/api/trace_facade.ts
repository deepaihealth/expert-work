/**
 * Trace facade SDK — Batch 4b.
 *
 * Consumes ``GET /v1/sessions/{thread_id}/runs/{run_id}/trace`` (raw payload,
 * no envelope — matching the pattern in :func:`getRun`).
 */
import { apiClient, withTenantScope, type TenantScope } from "./client";

export type TraceStatus = "ok" | "not_ready" | "unavailable" | "no_trace";

/** A single rendered chat message inside a structured trace I/O payload. */
export type RenderedMessage = {
  role: string;
  content: string;
  truncated: boolean;
  fullChars: number;
  toolCalls: string[] | null;
};

/** Structured trace span input/output — either a rendered message list or
 *  plain text (both possibly truncated at the source). Wired into
 *  `TraceSpan.input`/`output` below; consumed by TraceView's `IoSection`. */
export type RunTraceIo =
  | { kind: "messages"; messages: RenderedMessage[] }
  | { kind: "text"; text: string; truncated: boolean; fullChars: number };

export interface TraceSpan {
  id: string;
  parentId: string | null;
  kind: "session" | "llm" | "tool" | "span";
  label: string;
  detail: string | null;
  startMs: number;
  latencyMs: number;
  model: string | null;
  inputTokens: number | null;
  outputTokens: number | null;
  costUsd: number | null;
  input: RunTraceIo | null;
  output: RunTraceIo | null;
  level: "default" | "warning" | "error";
  statusMessage: string | null;
  /** LLM-call intent for the visual marker: "" for non-LLM spans and the
   *  unwrapped/main agent turn, else "memory"/"planner"/"reflect"/… — an
   *  auxiliary LLM call the console flags apart from the main conversation. */
  purpose: string;
  /** Functional stage for colour grouping + the pre-first-token breakdown:
   *  "entry" = entry chain (recall / ingest / context gates), null = other.
   *  Separate from `purpose`, which is LLM-call intent only. */
  group: "entry" | null;
}

export interface RunTrace {
  status: TraceStatus;
  trace?: {
    name: string;
    latencyMs: number;
    totalCostUsd: number | null;
    spanCount: number;
  };
  spans?: TraceSpan[];
}

/** Fetch trace data for a run.
 *
 * The endpoint returns a raw payload (no envelope) with status indicating
 * availability. When status=ok, trace and spans are populated; otherwise
 * they are undefined.
 */
export async function getRunTrace(
  threadId: string,
  runId: string,
  tenantScope?: TenantScope,
): Promise<RunTrace> {
  const response = await apiClient.get<RunTrace>(
    `/v1/sessions/${threadId}/runs/${runId}/trace`,
    { params: withTenantScope({}, tenantScope) },
  );
  return response.data;
}
