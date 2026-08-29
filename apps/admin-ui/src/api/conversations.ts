/**
 * Conversations SDK — the conversation-centric operations view
 * (``docs/design/conversation-centric-ia.md``).
 *
 * A *conversation* is a ``thread_meta`` row (the ``(agent, user_id,
 * session_id=thread_id)`` unit) enriched with a rollup of its
 * ``agent_run`` rows + token totals joined by ``trace_id``. Both
 * endpoints use the standard ``{success,data}`` envelope (``getJson``
 * unwraps ``data``).
 *
 *   - ``GET /v1/conversations`` — the list (agent / user / status / q).
 *   - ``GET /v1/conversations/{thread_id}`` — one conversation's run list.
 */
import { getJson, withTenantScope, type TenantScope } from "./client";
import type { RunStatus, RunTokens } from "./runs";

/** Thread lifecycle — server ``ThreadStatus`` (expert_work.protocol). */
export type ConversationStatus =
  | "active"
  | "paused"
  | "completed"
  | "failed"
  | "cancelled"
  | "archived";

/** One row from ``GET /v1/conversations`` — a conversation + its run rollup. */
export interface ConversationListItem {
  thread_id: string;
  tenant_id: string;
  user_id: string | null;
  agent_name: string | null;
  agent_version: string | null;
  /** Human title — auto-set from the first user message, or ``null``. */
  title: string | null;
  status: ConversationStatus;
  created_at: string | null;
  updated_at: string | null;
  /** Number of ``agent_run`` rows in the thread. */
  run_count: number;
  /** Runs in a failed terminal state (error / timeout). */
  error_count: number;
  /** Runs paused at an approval gate — "needs a human" signal. */
  pending_count: number;
  /** Newest run ``created_at`` — the "last active" clock (``null`` if no runs). */
  last_run_at: string | null;
  /** Token totals across the thread's runs (``null`` = no recorded usage). */
  tokens: RunTokens | null;
}

export interface ConversationList {
  items: ConversationListItem[];
  total: number;
  cross_tenant: boolean;
}

/** One run inside a conversation-detail run list. */
export interface ConversationRun {
  run_id: string;
  thread_id: string;
  user_id: string | null;
  status: RunStatus;
  is_resume: boolean;
  error: string | null;
  created_at: string;
  updated_at: string;
  finished_at: string | null;
  trace_id: string | null;
  /** 这一轮实际执行时用的 manifest 内容哈希。配置改动立刻对新一轮生效,所以
   *  同一个会话前后可能跑在两套配置上;``agent_version`` 分不出来(原地编辑,
   *  版本号不变),只能靠它。``null`` = 没记录(该列上线前的 run,或 run 在
   *  构建成功前就结束了)—— **不是**「用了另一套配置」。 */
  agent_spec_sha256: string | null;
  tokens: RunTokens | null;
}

/** ``GET /v1/conversations/{thread_id}`` — the list item + its runs. */
export interface ConversationDetail extends ConversationListItem {
  runs: ConversationRun[];
}

export interface ListConversationsParams {
  tenantScope?: TenantScope;
  /** Narrow to one agent's conversations (``agentVersion`` requires
   *  ``agentName`` — the backend 422s otherwise). */
  agentName?: string;
  agentVersion?: string;
  /** Narrow to one end-user's conversations. */
  userId?: string;
  status?: ConversationStatus;
  /** Free-text filter — substring match on the conversation title. */
  q?: string;
  /** Only conversations with ≥1 failed run (error / timeout) — the
   *  operations "what broke today" filter. Distinct from
   *  ``status=failed`` (thread lifecycle). */
  hasError?: boolean;
  /** Only conversations with ≥1 run paused at an approval gate — the
   *  "needs a human" queue in conversation context. */
  hasPending?: boolean;
  /** ISO instant — only conversations with ≥1 run at/after it (the
   *  "active in the last N hours" window; composes with ``hasError``). */
  since?: string;
  limit?: number;
  offset?: number;
}

/** GET /v1/conversations — the conversation index / global browser feed. */
export async function listConversations(
  params: ListConversationsParams = {},
): Promise<ConversationList> {
  const {
    tenantScope,
    agentName,
    agentVersion,
    userId,
    status,
    q,
    hasError,
    hasPending,
    since,
    limit,
    offset,
  } = params;
  const query = withTenantScope(
    {
      agent_name: agentName,
      agent_version: agentVersion,
      user_id: userId,
      status,
      q,
      has_error: hasError ? true : undefined,
      has_pending: hasPending ? true : undefined,
      since,
      limit,
      offset,
    },
    tenantScope,
  );
  return getJson<ConversationList>("/v1/conversations", { params: query });
}

/** GET /v1/conversations/{thread_id} — one conversation's runs + summary.
 *  ``tenantScope`` carries a concrete tenant id when a system_admin drills
 *  in from the cross-tenant browser (a thread belongs to one tenant). */
export async function getConversation(
  threadId: string,
  tenantScope?: TenantScope,
): Promise<ConversationDetail> {
  const query = withTenantScope({}, tenantScope);
  return getJson<ConversationDetail>(`/v1/conversations/${encodeURIComponent(threadId)}`, {
    params: query,
  });
}
