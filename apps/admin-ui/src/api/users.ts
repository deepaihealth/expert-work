/**
 * Agent users SDK — the per-agent users rollup
 * (``docs/design/conversation-centric-ia.md`` §5 M2).
 *
 * ``GET /v1/agents/{name}/{version}/users`` lists every end-user with
 * ≥1 conversation on the agent, with their conversation / run rollup
 * and token totals — the top of the user → conversation → run
 * drill-down. Standard ``{success,data}`` envelope.
 */
import { getJson, postJson, withTenantScope, type TenantScope } from "./client";
import type { RunTokens } from "./runs";

/** One row from the users rollup. */
export interface AgentUserItem {
  user_id: string;
  /** From the ``tenant_user`` registry — ``null`` when never registered. */
  display_name: string | null;
  conversation_count: number;
  run_count: number;
  error_count: number;
  pending_count: number;
  /** Newest run across the user's conversations (``null`` if none). */
  last_run_at: string | null;
  /** Token totals for this user on this agent (``null`` = no usage). */
  tokens: RunTokens | null;
}

export interface AgentUserList {
  items: AgentUserItem[];
  total: number;
  cross_tenant: boolean;
}

export interface ListAgentUsersParams {
  tenantScope?: TenantScope;
  limit?: number;
  offset?: number;
}

/** GET /v1/agents/{name}/{version}/users — the users rollup. */
export async function listAgentUsers(
  agentName: string,
  agentVersion: string,
  params: ListAgentUsersParams = {},
): Promise<AgentUserList> {
  const { tenantScope, limit, offset } = params;
  const query = withTenantScope({ limit, offset }, tenantScope);
  return getJson<AgentUserList>(
    `/v1/agents/${encodeURIComponent(agentName)}/${encodeURIComponent(agentVersion)}/users`,
    { params: query },
  );
}

/** One ``tenant_user`` registry row — the user-detail header. */
export interface TenantUser {
  user_id: string;
  /** The passed-in ``user_id`` / OIDC ``sub`` — the human-recognizable
   *  identifier (distinct from the surrogate ``user_id`` UUID). */
  subject_id: string;
  display_name: string | null;
  subject_type: string;
  created_at: string | null;
  last_active_at: string | null;
}

/** GET /v1/users/{user_id} — one registry row (self-or-admin gated). */
export async function getTenantUser(
  userId: string,
  tenantScope?: TenantScope,
): Promise<TenantUser> {
  const query = withTenantScope({}, tenantScope);
  return getJson<TenantUser>(`/v1/users/${encodeURIComponent(userId)}`, { params: query });
}

/** One row from ``GET /v1/users`` — the tenant-wide user roster
 *  (``tenant_user`` registry × conversation/run rollup). The
 *  human-recognizable identifier is ``subject_id`` (the passed-in
 *  ``user_id`` / OIDC ``sub``); ``user_id`` is the internal surrogate. */
export interface TenantUserRosterItem {
  user_id: string;
  subject_id: string;
  subject_type: string;
  display_name: string | null;
  /** ``true`` when the user maps to a tenant member (a logged-in employee)
   *  rather than an external caller. */
  is_member: boolean;
  member_email: string | null;
  member_role: string | null;
  conversation_count: number;
  run_count: number;
  error_count: number;
  pending_count: number;
  last_active_at: string | null;
  last_run_at: string | null;
}

export interface TenantUserRoster {
  items: TenantUserRosterItem[];
  total: number;
  cross_tenant: boolean;
}

export interface ListUsersParams {
  tenantScope?: TenantScope;
  limit?: number;
  offset?: number;
}

/** GET /v1/users — the tenant-wide user roster (admin-only, server-gated;
 *  ``tenant_id=*`` yields the system_admin cross-tenant aggregate). Sorted
 *  by ``last_active_at`` desc server-side. May return a ``X-Limit-Capped``
 *  header the caller can't read through the unwrapped envelope; the list
 *  page surfaces truncation from ``total`` vs ``items.length`` instead. */
export async function listUsers(
  params: ListUsersParams = {},
): Promise<TenantUserRoster> {
  const { tenantScope, limit, offset } = params;
  const query = withTenantScope({ limit, offset }, tenantScope);
  return getJson<TenantUserRoster>("/v1/users", { params: query });
}

/** The per-store rollup ``POST /v1/users/{id}:purge`` returns (Phase 3a).
 *  ``deleted`` / ``anonymized`` map a store name → row count; ``failures``
 *  maps a step name → error string (best-effort — a re-run retries them). */
export interface PurgeSummary {
  tenant_id: string;
  user_id: string;
  subject_id: string;
  threads_purged: number;
  runs_deleted: number;
  threads_capped: boolean;
  deleted: Record<string, number>;
  anonymized: Record<string, number>;
  workspace_marked_deleted: boolean;
  deactivated: boolean;
  failures: Record<string, string>;
  ok: boolean;
}

/** POST /v1/users/{id}:purge — irreversibly cascade-purge a user's data +
 *  assets (admin-only, server-gated). Caller-home-tenant-scoped like the rest
 *  of the /users feature. Works for any user — external end-user or console
 *  member (employee); account deletion stays a members-page concern (this
 *  endpoint never touches Keycloak / roles / tenant_member). Best-effort +
 *  idempotent; the returned summary carries per-store counts and any step
 *  that failed. */
export async function purgeUser(userId: string): Promise<PurgeSummary> {
  return postJson<PurgeSummary>(`/v1/users/${encodeURIComponent(userId)}:purge`, {});
}
