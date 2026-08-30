/**
 * Sessions SDK — Stream H.2 PR 3.
 *
 * Two flows:
 *
 *   1. ``createSession`` — POST /v1/sessions; binds a fresh thread to
 *      ``(agent_name, agent_version)``. Returns the persisted thread
 *      metadata (status, owner, timestamps).
 *   2. ``streamRun`` — POST /v1/sessions/{thread_id}/runs with input
 *      payload; the response is an ``text/event-stream``. We parse the
 *      SSE frames on the fly and yield :class:`SseEvent` per ``id /
 *      event / data`` triple.
 *
 * Implementation note — browser ``EventSource`` doesn't support custom
 * headers (which kills the Bearer token), so we drive the stream with
 * ``fetch`` + ``ReadableStream``. ``abortSignal`` plumbs the consumer's
 * cancel back to the network layer.
 */
import {
  apiClient,
  getStoredToken,
  withTenantScope,
  type ApiEnvelope,
  type TenantScope,
} from "./client";
import { unwrap } from "./client";

export interface ThreadMeta {
  thread_id: string;
  tenant_id: string;
  agent_name: string | null;
  agent_version: string | null;
  user_id: string | null;
  status: string;
  /** Human label for the session-history list — auto-set from the first user
   *  message, manually overridable. Null for threads that never ran. */
  title: string | null;
  created_by: string;
  created_at: string;
  updated_at: string;
}

export interface CreateSessionRequest {
  agent_name: string;
  agent_version: string;
}

export async function createSession(
  payload: CreateSessionRequest,
): Promise<ThreadMeta> {
  const response = await apiClient.post<ApiEnvelope<ThreadMeta>>(
    "/v1/sessions",
    payload,
  );
  return unwrap(response.data);
}

/** G.6 / SE-16 (SE-A46) — a 👍/👎 (+ optional comment) on a session turn.
 *  Fire-and-forget quality signal feeding the skill-evolution curation
 *  pipeline (👍 → golden candidate, 👎+comment → failure corpus). */
export interface SessionFeedback {
  id: number;
  thread_id: string;
  rating: "up" | "down";
  turn_seq: number | null;
  trace_id: string | null;
}

/** POST /v1/sessions/{threadId}/feedback — the endpoint returns a bare JSON
 *  row (201, no ``{success,data}`` envelope), so no ``unwrap`` here. */
export async function submitSessionFeedback(
  threadId: string,
  payload: { rating: "up" | "down"; comment?: string; turn_seq?: number },
): Promise<SessionFeedback> {
  const response = await apiClient.post<SessionFeedback>(
    `/v1/sessions/${threadId}/feedback`,
    payload,
  );
  return response.data;
}

/** Playground-Uplift D4 — the thread user's persistent workspace + artifacts.
 *  ``workspace`` is null when no VM has ever started for that user (read-only;
 *  the inspector never provisions one). */
export interface WorkspaceMeta {
  id: string;
  tenant_id: string;
  user_id: string;
  volume_name: string;
  size_bytes: number;
  size_limit_bytes: number;
  created_at: string | null;
  last_accessed_at: string | null;
  deleted_at: string | null;
  archived_object_key: string | null;
}

export interface WorkspaceArtifact {
  name: string;
  kind: string;
  latest_version: number;
  created_at: string | null;
  updated_at: string | null;
}

export interface SessionWorkspace {
  workspace: WorkspaceMeta | null;
  artifacts: WorkspaceArtifact[];
  /** Effective per-user byte cap (Task 7). Optional — old backends omit it. */
  limit_bytes?: number;
}

/** One file in a user's persistent workspace volume (browse). */
export interface WorkspaceFile {
  path: string;
  size: number;
}

/** Playground-Uplift #6 — a resumed thread's prior conversation (from the
 *  durable checkpoint), so resume shows what was said before. User/assistant
 *  text turns only. */
export interface HistoryMessage {
  role: "user" | "assistant";
  content: string;
  /** Structural output channel (backend read_turns): assistant rows carry
   *  "commentary" | "final"; user rows are null. Absent on old payloads. */
  channel?: "commentary" | "final" | null;
  /** The run that produced this message (backend ``expert_work_run_id``
   *  stamp). ``null`` on messages written before the stamp shipped — never
   *  backfilled — so ``buildHistoryTurns`` treats a single null as "this
   *  thread can't be grouped by run" and falls back to order pairing. */
  run_id?: string | null;
}

export async function getSessionMessages(
  threadId: string,
  /** The thread's tenant — lets a system_admin read a foreign tenant's
   *  transcript when drilling in from the cross-tenant browser. A
   *  caller's own tenant id is a no-op. */
  tenantId?: string,
): Promise<HistoryMessage[]> {
  const response = await apiClient.get<
    ApiEnvelope<{ messages: HistoryMessage[] }>
  >(`/v1/sessions/${threadId}/messages`, {
    params: tenantId ? { tenant_id: tenantId } : undefined,
  });
  return unwrap(response.data).messages;
}

/** Playground-Uplift #6 — list the caller's threads (user-scoped server-side),
 *  newest first; the playground filters to the current agent for resume.
 *  ``q`` searches the title; ``offset`` paginates; ``includeArchived`` also
 *  returns soft-deleted (archived) threads. */
export async function listSessions(
  params: {
    limit?: number;
    offset?: number;
    q?: string;
    agentName?: string;
    status?: string;
    includeArchived?: boolean;
    /** Stream N — the endpoint is scope-aware ("*" aggregates every
     *  tenant), so the raw scope passes through unmapped. */
    tenantScope?: TenantScope;
    /** PR-A — sort order, in the backend's own values. ``"created_at"`` is
     *  the backend default, so only ``"last_activity"`` is ever sent. */
    orderBy?: "created_at" | "last_activity";
  } = {},
): Promise<ThreadMeta[]> {
  const query: Record<string, string | number | boolean> = {
    limit: params.limit ?? 100,
  };
  if (params.offset) query.offset = params.offset;
  if (params.q) query.q = params.q;
  if (params.agentName) query.agent_name = params.agentName;
  if (params.status) query.status = params.status;
  if (params.includeArchived) query.include_archived = true;
  if (params.orderBy === "last_activity") query.order_by = "last_activity";
  const response = await apiClient.get<ApiEnvelope<{ items: ThreadMeta[] }>>(
    "/v1/sessions",
    { params: withTenantScope(query, params.tenantScope) },
  );
  return unwrap(response.data).items;
}

/** Rename a session — sets its title (overrides the auto-title). */
export async function renameSession(
  threadId: string,
  title: string,
): Promise<ThreadMeta> {
  const response = await apiClient.patch<ApiEnvelope<ThreadMeta>>(
    `/v1/sessions/${encodeURIComponent(threadId)}`,
    { title },
  );
  return unwrap(response.data);
}

/** Soft-delete a session — archive it (hidden from the default list,
 *  reversible; checkpoint/runs/workspace untouched). */
export async function archiveSession(threadId: string): Promise<void> {
  await apiClient.delete(`/v1/sessions/${encodeURIComponent(threadId)}`);
}

/** Hard-delete a session — irreversibly purge the whole conversation
 *  (checkpoint messages + run rows + the thread). The user's shared
 *  workspace/artifacts are intentionally left intact. */
export async function purgeSession(threadId: string): Promise<void> {
  await apiClient.post(`/v1/sessions/${encodeURIComponent(threadId)}:purge`);
}

export interface RunRequest {
  input?: string | null;
  image_refs?: string[];
  /** Stream PI-1c — structured untrusted input. Data to act on (a ticket,
   *  email, or document) passed here instead of concatenated into
   *  ``input`` is fenced with spotlighting before the model sees it, so an
   *  instruction embedded in it is treated as data — the root fix for
   *  inline prompt injection. Omitted → today's behaviour. */
  untrusted_content?: string[];
  /** Dynamic-Prompt — run-time Jinja variables substituted into the agent's
   *  ``system_prompt`` template (when it opts into jinja mode), validated
   *  against the agent's declared ``variables``. Omitted → no substitution. */
  inputs?: Record<string, string>;
  /** 用**未发布的草稿**跑这一轮,而不是线上那一版 —— 配置页发布前的试跑。
   *
   *  没有草稿时后端 409(不会静默退回线上版本 —— 那会让人以为自己验过了
   *  草稿),与 ``mode=queue`` 组合时 422(排队的 run 由 worker 稍后执行,
   *  而 worker 读的是线上那一版)。 */
  use_draft?: boolean;
}

/** A single SSE frame as parsed from the network stream. ``data`` is
 *  the JSON-decoded body when the frame's ``data:`` block is valid
 *  JSON; otherwise the raw string. */
export interface SseEvent {
  id: string | null;
  event: string;
  data: unknown;
  rawData: string;
  /** UTC timestamp the client received the frame. */
  receivedAt: string;
}

/** Yield SSE frames from a control-plane run stream. The caller awaits
 *  the iterator; cancellation flows through ``options.signal``. */
export async function* streamRun(
  threadId: string,
  payload: RunRequest,
  options: { signal?: AbortSignal; baseUrl?: string } = {},
): AsyncGenerator<SseEvent, void, void> {
  const baseUrl = options.baseUrl ?? "";
  const url = `${baseUrl}/v1/sessions/${encodeURIComponent(threadId)}/runs`;
  const token = getStoredToken();
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    Accept: "text/event-stream",
  };
  if (token) headers["Authorization"] = `Bearer ${token}`;
  const response = await fetch(url, {
    method: "POST",
    headers,
    body: JSON.stringify(payload),
    signal: options.signal,
  });
  if (!response.ok) {
    let detail = `HTTP ${response.status}`;
    try {
      const body = (await response.json()) as {
        detail?: { code?: string; message?: string };
      };
      const code = body.detail?.code ?? `HTTP_${response.status}`;
      const message = body.detail?.message ?? detail;
      detail = `${code}: ${message}`;
    } catch {
      // Body wasn't JSON — keep the HTTP-N fallback.
    }
    throw new Error(detail);
  }
  if (!response.body) {
    throw new Error("response has no body — SSE not available");
  }
  yield* parseSseStream(response.body, options.signal);
}

/** Internal — parse a ``text/event-stream`` ReadableStream into frames.
 *  Exported for tests; not part of the public SDK. */
export async function* parseSseStream(
  body: ReadableStream<Uint8Array>,
  signal?: AbortSignal,
): AsyncGenerator<SseEvent, void, void> {
  const reader = body.getReader();
  const decoder = new TextDecoder("utf-8");
  let buffer = "";
  try {
    while (!signal?.aborted) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      while (true) {
        const idx = buffer.indexOf("\n\n");
        if (idx === -1) break;
        const block = buffer.slice(0, idx);
        buffer = buffer.slice(idx + 2);
        const frame = parseSseBlock(block);
        if (frame) yield frame;
      }
    }
  } finally {
    reader.releaseLock();
  }
}

function parseSseBlock(block: string): SseEvent | null {
  let id: string | null = null;
  let event = "message";
  const dataLines: string[] = [];
  for (const line of block.split("\n")) {
    if (line === "" || line.startsWith(":")) continue;
    const colonAt = line.indexOf(":");
    if (colonAt === -1) continue;
    const field = line.slice(0, colonAt);
    const value = line.slice(colonAt + 1).replace(/^\s/, "");
    if (field === "id") id = value;
    else if (field === "event") event = value;
    else if (field === "data") dataLines.push(value);
  }
  if (dataLines.length === 0 && id === null) return null;
  const rawData = dataLines.join("\n");
  let data: unknown = rawData;
  try {
    data = JSON.parse(rawData);
  } catch {
    // Keep as raw string — not every event carries JSON (eg. ``end``).
  }
  return {
    id,
    event,
    data,
    rawData,
    receivedAt: new Date().toISOString(),
  };
}
