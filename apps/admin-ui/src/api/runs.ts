/**
 * Runs SDK — backed by ``/v1/sessions/{thread_id}/runs/*`` (per-thread).
 * The cross-thread index moved to the conversations SDK
 * (``api/conversations.ts`` — conversation-centric IA).
 *
 * ``GET /v1/sessions/{thread_id}/runs/{run_id}`` is one of the few
 * endpoints that returns the raw payload (no envelope) — see :func:`getRun`.
 */
import { apiClient, unwrap, withTenantScope, type ApiEnvelope, type TenantScope } from "./client";

export type RunStatus =
  // Server-side ``RunStatus`` enum (expert_work.runtime.runs.RunStatus).
  // ``pending`` / ``running`` / ``success`` / ``error`` / ``timeout`` /
  // ``interrupted`` / ``paused`` are the canonical values.
  | "pending"
  | "running"
  | "success"
  | "error"
  | "timeout"
  | "interrupted"
  | "paused"
  // Pre-H.3 PR 1 names kept here for the per-thread endpoint that may
  // still be in flight in older clients.
  | "queued"
  | "awaiting_approval"
  | "completed"
  | "failed"
  | "cancelled"
  | "unknown";

export interface PendingApproval {
  request_id: string;
  node: string;
  reason_kind: string;
  action_summary: string;
  proposed_args: Record<string, unknown>;
  requested_at: string;
  timeout_at: string;
  /** RT-6 Tier A (RT-ADR-19) — the approved-args fingerprint bound at mint,
   *  re-verified before dispatch. Empty for a legacy / unbound or action-screen
   *  approval. Shown as a short receipt on the review card. */
  binding_digest?: string;
  /** RT-6 Tier B (RT-ADR-20) — a workspace-write-capable tool ran since this
   *  approval was requested (approve-then-swap-script). Audit-only signal, a
   *  conservative over-approximation — surfaced as a warning, never blocks. */
  workspace_drift?: boolean;
}

/** Per-run token usage summary — aggregated from expert_work's own
 *  ``token_usage`` (G.9), joined to the run by ``trace_id``. ``null``
 *  when the run has no trace_id / no recorded usage (legacy or
 *  auto-triggered runs). Deep per-span traces stay in Langfuse. */
export interface RunTokens {
  input_tokens: number;
  output_tokens: number;
  cache_creation_tokens: number;
  cache_read_tokens: number;
  total_tokens: number;
  llm_calls: number;
  models: string[];
}

export interface RunDetail {
  run_id: string;
  thread_id: string;
  status: RunStatus;
  pending_approval: PendingApproval | null;
  /** Mini-ADR H-9.5 — OTel trace_id captured at run start, persisted
   *  on the ``agent_run`` row. ``null`` for legacy runs created before
   *  the migration or for auto-triggered runs (scheduler / curation
   *  worker that passes ``None``). */
  trace_id?: string | null;
  /** Runs-enrichment — per-run token summary (``null`` = no usage). */
  tokens?: RunTokens | null;
  /** Runs-enrichment — durable-row timestamps (``null`` when the run is
   *  only in the in-memory RunManager); the summary derives duration. */
  created_at?: string | null;
  finished_at?: string | null;
}

/** Raw (no envelope) fetch — runs.py historically returns the run
 *  status directly. Keeping this endpoint un-enveloped to match
 *  Mini-ADR J-41; ADR refresh PR would normalise.
 *
 *  ``tenantScope`` (Track C W2) carries the concrete tenant id when a
 *  system_admin has switched into a foreign tenant; ``undefined`` = home. */
export async function getRun(
  threadId: string,
  runId: string,
  tenantScope?: TenantScope,
): Promise<RunDetail> {
  const response = await apiClient.get<RunDetail>(
    `/v1/sessions/${threadId}/runs/${runId}`,
    { params: withTenantScope({}, tenantScope) },
  );
  return response.data;
}

/** POST body — matches backend ``ResumeRequest`` shape:
 *  ``decision ∈ {"approve","reject","modify"}``;
 *  ``modified_args`` MUST be present iff ``decision === "modify"``.
 *  H.3 PR 5 — Mini-ADR H-9: ``modify`` lands when the reviewer edits
 *  the agent's proposed_args in the Monaco JSON inline. */
export interface ResumeRunRequest {
  decision: "approve" | "reject" | "modify";
  reason?: string;
  modified_args?: Record<string, unknown>;
}

export async function resumeRun(
  threadId: string,
  runId: string,
  body: ResumeRunRequest,
): Promise<RunDetail> {
  const response = await apiClient.post<RunDetail>(
    `/v1/sessions/${threadId}/runs/${runId}/resume`,
    body,
  );
  return response.data;
}

/** GET /v1/sessions/{thread}/runs/{run}/events — SSE stream.
 *
 *  Active runs (running/paused/pending) get a live attach via the
 *  StreamBridge; terminal runs get a one-shot replay from the
 *  ``run_event`` table. The wire format is identical (Mini-ADR H-7
 *  decision A — SSE id ``"{ms}-{seq}"``); this SDK doesn't have to
 *  know which mode it got.
 *
 *  ``sinceSeq`` is Last-Event-ID semantics: ``undefined`` returns the
 *  stream from the beginning; ``N`` returns events with ``seq > N``.
 *
 *  ``tenantScope`` (Track C W2) — hand-built URL, so ``tenant_id`` is
 *  appended manually (cf. ``listThreadRuns``); ``undefined`` omits it. */
export async function* streamRunEvents(
  threadId: string,
  runId: string,
  options: {
    sinceSeq?: number;
    signal?: AbortSignal;
    baseUrl?: string;
    tenantScope?: TenantScope;
  } = {},
): AsyncGenerator<import("./sessions").SseEvent, void, void> {
  const { sinceSeq, signal, baseUrl = "", tenantScope } = options;
  const params = new URLSearchParams();
  if (sinceSeq !== undefined) params.set("since_seq", String(sinceSeq));
  if (tenantScope !== undefined) params.set("tenant_id", tenantScope);
  const qs = params.toString();
  const url =
    `${baseUrl}/v1/sessions/${encodeURIComponent(threadId)}/runs/${encodeURIComponent(runId)}/events` +
    (qs ? `?${qs}` : "");
  const { getStoredToken } = await import("./client");
  const token = getStoredToken();
  const headers: Record<string, string> = { Accept: "text/event-stream" };
  if (token) headers["Authorization"] = `Bearer ${token}`;
  const response = await fetch(url, { method: "GET", headers, signal });
  if (!response.ok) {
    throw new Error(`HTTP ${response.status} on /events`);
  }
  if (!response.body) {
    throw new Error("response has no body — SSE not available");
  }
  const { parseSseStream } = await import("./sessions");
  yield* parseSseStream(response.body, signal);
}

/** Playground history reconstruction — one row of ``GET
 *  /v1/sessions/{thread_id}/runs``. ``createdAt`` orders turns; ``runId``
 *  feeds the per-run event replay that rebuilds a full historical turn. */
export interface ThreadRunSummary {
  runId: string;
  status: RunStatus;
  isResume: boolean;
  createdAt: string;
}

interface ThreadRunRow {
  run_id: string;
  status: RunStatus;
  is_resume: boolean;
  created_at: string;
}

/** List a thread's runs oldest-first. ``tenantId`` (a system_admin drilling
 *  into a foreign tenant) is a no-op for a caller's own tenant. */
export async function listThreadRuns(
  threadId: string,
  tenantId?: string,
): Promise<ThreadRunSummary[]> {
  const response = await apiClient.get<ApiEnvelope<{ runs: ThreadRunRow[] }>>(
    `/v1/sessions/${threadId}/runs`,
    { params: tenantId ? { tenant_id: tenantId } : undefined },
  );
  return unwrap(response.data).runs.map((r) => ({
    runId: r.run_id,
    status: r.status,
    isResume: r.is_resume,
    createdAt: r.created_at,
  }));
}
