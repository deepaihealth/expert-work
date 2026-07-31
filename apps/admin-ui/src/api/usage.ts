/**
 * Tenant usage SDK — Stream Z3, backed by the Z1/Z2 billing APIs.
 *
 * Two tenant-scoped endpoints (``billing:read``; cross-tenant W3 adds an
 * optional ``tenantScope`` → ``?tenant_id=`` for switched-in system_admin
 * reads — ``undefined`` falls through to the caller's home tenant):
 *
 *   - ``GET /v1/usage/cost``   — billed cost rollup (lags the hourly rollup,
 *     hence ``as_of``). The tenant view exposes **only** billed cost; the
 *     payload deliberately omits base_cost / markup / margin (the
 *     monetization no-leak rule). This SDK mirrors that — there is no
 *     base/markup/margin field anywhere below.
 *   - ``GET /v1/usage/tokens`` — realtime current-month token counters.
 *
 * Both return the standard ``{success, data, error}`` envelope; ``getJson``
 * unwraps it.
 */
import { getJson, withTenantScope, type TenantScope } from "./client";

export type UsageGroupBy = "agent" | "model" | "none";

/** A token tally shared by every usage row. */
export interface TokenCounts {
  input_tokens: number;
  output_tokens: number;
  cache_creation_tokens: number;
  cache_read_tokens: number;
}

/** One cost row from ``/v1/usage/cost``. ``provider`` / ``model`` /
 *  ``agent_name`` are only populated when ``group_by=none``. */
export interface UsageCostGroup extends TokenCounts {
  key: string;
  billed_cost_micros: number;
  unpriced: boolean;
  provider?: string;
  model?: string;
  agent_name?: string;
}

export interface UsageCost {
  month: string;
  group_by: UsageGroupBy;
  as_of: string | null;
  total_billed_cost_micros: number;
  groups: UsageCostGroup[];
}

/** A keyed token tally from ``/v1/usage/tokens``. */
export interface TokenGroup extends TokenCounts {
  key: string;
}

export interface UsageTokens {
  month: string;
  as_of: string;
  realtime: true;
  total: TokenCounts;
  by_agent: TokenGroup[];
  by_model: TokenGroup[];
  /** SE-16 (SE-A43) — spend split by what spent it: ``conversation`` vs
   *  ``skill_evolution`` (the evolution flywheel's aux + replay calls). */
  by_kind: TokenGroup[];
}

export interface GetUsageCostParams {
  tenantScope?: TenantScope;
  /** ``YYYY-MM``; defaults server-side to the current month when omitted. */
  month?: string;
  groupBy?: UsageGroupBy;
}

/** ``GET /v1/usage/cost`` — billed cost rollup (NO base/markup/margin). */
export async function getUsageCost(params: GetUsageCostParams = {}): Promise<UsageCost> {
  const query: Record<string, string> = {};
  if (params.month) query.month = params.month;
  if (params.groupBy) query.group_by = params.groupBy;
  return getJson<UsageCost>("/v1/usage/cost", {
    params: withTenantScope(query, params.tenantScope),
  });
}

export interface GetUsageTokensParams {
  tenantScope?: TenantScope;
  /** ``YYYY-MM``; defaults server-side to the current month when omitted. */
  month?: string;
  /** Narrow to one end-user (M2 user-detail usage tab). */
  userId?: string;
}

/** ``GET /v1/usage/tokens`` — realtime current-month token counters. */
export async function getUsageTokens(params: GetUsageTokensParams = {}): Promise<UsageTokens> {
  const query: Record<string, string> = {};
  if (params.month) query.month = params.month;
  if (params.userId) query.user_id = params.userId;
  return getJson<UsageTokens>("/v1/usage/tokens", {
    params: withTenantScope(query, params.tenantScope),
  });
}
