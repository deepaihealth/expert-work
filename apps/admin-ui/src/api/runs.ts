/**
 * Runs SDK — backed by ``/v1/sessions/{thread_id}/runs/*`` (per-thread).
 * The cross-thread index moved to the conversations SDK
 * (``api/conversations.ts`` — conversation-centric IA).
 *
 * ``GET /v1/sessions/{thread_id}/runs/{run_id}`` is one of the few
 * endpoints that returns the raw payload (no envelope) — see :func:`getRun`.
 */
import { apiClient, unwrap, withTenantScope, type ApiEnvelope, type TenantScope } from "./client";
import { seqOf } from "./sse_id";

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

/** Run statuses that mean "still in flight" — D-5's tail tolerance, the
 *  live-attach mode switch and the conversation page's cancel affordance all
 *  key off this one set. ``queued`` is real (queue-mode runs) and cancellable
 *  (``RunStore.request_cancel`` supports it explicitly). */
export const NON_TERMINAL_RUN_STATUSES: ReadonlySet<string> = new Set([
  "pending",
  "queued",
  "running",
  "paused",
]);

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

/** 回放翻页上限。一页 500 帧 ⇒ 一次消费封顶 1 万帧。到顶就停下并打日志 ——
 *  服务端若因 bug 一直回 ``truncated``,这个循环不能变成能无限拉的循环。 */
const MAX_EVENT_PAGES = 20;

/** 下一页的 ``since_seq``。三处来源取最大值,保证游标只前进不后退:
 *
 *  1. ``truncated`` 帧的 ``next_seq`` —— 首选,body 里的帧不会被中间代理剥掉;
 *  2. ``X-Expert-Work-Next-Seq`` 响应头 —— 帧里读不到时的兜底(代理会剥掉不认识
 *     的响应头,跨源还得服务端 expose,所以只当兜底);
 *  3. ``maxSeqSeen`` —— 前两者都读不到时的最后一根稻草。
 *
 *  ``headers`` 用可选链取:老测试替身的 fetch mock 只有 ``{ok, body}``。 */
function nextCursorOf(
  truncatedFrame: import("./sessions").SseEvent,
  response: Response,
  maxSeqSeen: number | null,
): number | null {
  const candidates: number[] = [];
  const data = truncatedFrame.data;
  if (data !== null && typeof data === "object") {
    const raw = (data as { next_seq?: unknown }).next_seq;
    if (typeof raw === "number" && Number.isInteger(raw)) candidates.push(raw);
  }
  const header = response.headers?.get("X-Expert-Work-Next-Seq");
  if (header !== null && header !== undefined && header !== "") {
    const parsed = Number(header);
    if (Number.isInteger(parsed)) candidates.push(parsed);
  }
  if (maxSeqSeen !== null) candidates.push(maxSeqSeen);
  return candidates.length === 0 ? null : Math.max(...candidates);
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
 *  **翻页(P3 PR-1)**:回放一页装不下时,后端以 ``truncated`` 帧收尾并且
 *  **不发 ``end``** —— 流并没有结束。这个生成器据此带 ``next_seq`` 再拉一页,
 *  拼接后继续吐给调用方,直到收到 ``end``;续拉成功时 ``truncated`` 帧本身不
 *  外泄(它是传输层控制帧)。翻不动了(游标不前进 / 到页数上限)才把最后那帧
 *  ``truncated`` 吐出去 —— 调用方据此知道流没走完,**这一页没有终局状态**。
 *
 *  游标取**见过的最大 seq**,不是最后一帧的 seq:补发的落库空洞帧会排在更大
 *  seq 之后(wire 上不保证按 seq 递增到达),拿最后一帧当游标会把中间那段重收
 *  一遍。
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
  const { getStoredToken } = await import("./client");
  const { parseSseStream } = await import("./sessions");
  const token = getStoredToken();
  const headers: Record<string, string> = { Accept: "text/event-stream" };
  if (token) headers["Authorization"] = `Bearer ${token}`;
  const endpoint = `${baseUrl}/v1/sessions/${encodeURIComponent(threadId)}/runs/${encodeURIComponent(runId)}/events`;

  let cursor = sinceSeq;
  let maxSeqSeen: number | null = sinceSeq ?? null;

  for (let pageCount = 1; ; pageCount += 1) {
    const params = new URLSearchParams();
    if (cursor !== undefined) params.set("since_seq", String(cursor));
    if (tenantScope !== undefined) params.set("tenant_id", tenantScope);
    const qs = params.toString();
    const response = await fetch(endpoint + (qs ? `?${qs}` : ""), {
      method: "GET",
      headers,
      signal,
    });
    if (!response.ok) {
      throw new Error(`HTTP ${response.status} on /events`);
    }
    if (!response.body) {
      throw new Error("response has no body — SSE not available");
    }

    let truncatedFrame: import("./sessions").SseEvent | null = null;
    for await (const frame of parseSseStream(response.body, signal)) {
      // ``maxSeqSeen`` **只是翻页游标,不是去重判据**。到达顺序不等于 seq 顺序
      // (契约实况表 B:0,1,2,5,6,3,4 —— 3、4 是补发的真帧,在 6 之后才到),
      // 拿它当 ``seq <= maxSeqSeen → drop`` 会把补发帧静默丢掉。每一帧都照发。
      const seq = seqOf(frame.id);
      if (seq !== null && (maxSeqSeen === null || seq > maxSeqSeen)) maxSeqSeen = seq;
      if (frame.event === "truncated") {
        truncatedFrame = frame;
        break;
      }
      yield frame;
    }
    // 流自然走完(``end``,或连接断开)—— 没有下一页。
    if (truncatedFrame === null) return;

    const next = nextCursorOf(truncatedFrame, response, maxSeqSeen);
    const stalled = next === null || (cursor !== undefined && next <= cursor);
    if (stalled || pageCount >= MAX_EVENT_PAGES) {
      console.warn(
        `[streamRunEvents] 回放翻页中止 run=${runId} pages=${pageCount} cursor=${String(cursor)} next=${String(next)} — ` +
          (stalled ? "游标不前进" : `到达 ${MAX_EVENT_PAGES} 页上限`) +
          ";流未走完,没有终局状态",
      );
      yield truncatedFrame;
      return;
    }
    cursor = next;
  }
}

/** Playground history reconstruction — one row of ``GET
 *  /v1/sessions/{thread_id}/runs``. ``createdAt`` orders turns; ``runId``
 *  feeds the per-run event replay that rebuilds a full historical turn. */
export interface ThreadRunSummary {
  runId: string;
  status: RunStatus;
  isResume: boolean;
  createdAt: string;
  /** PR-A — persisted per-run token rollup (``null`` when the run has no
   *  recorded usage; absent on old backends → treated as null). */
  tokens: RunTokens | null;
}

interface ThreadRunRow {
  run_id: string;
  status: RunStatus;
  is_resume: boolean;
  created_at: string;
  tokens?: RunTokens | null;
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
    tokens: r.tokens ?? null,
  }));
}

/** POST /v1/sessions/{thread}/runs/{run}:cancel — D-6, cancel one in-flight
 *  run (running / pending / queued). 409 when the run is already terminal or
 *  paused (a paused run is decided through the approval path instead). */
export async function cancelRun(threadId: string, runId: string): Promise<void> {
  const response = await apiClient.post<ApiEnvelope<{ cancelled: boolean }>>(
    `/v1/sessions/${encodeURIComponent(threadId)}/runs/${encodeURIComponent(runId)}:cancel`,
    {},
  );
  unwrap(response.data);
}
