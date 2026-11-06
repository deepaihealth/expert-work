/**
 * Tenant Config SDK — backed by ``/v1/tenants/{tenant_id}/config``
 * (Stream C.7).
 *
 * **Tenant-scoped only** (same shape as tenant_quotas). Backend
 * returns the standard ``{success, data, error}`` envelope.
 *
 * ETag concurrency: backend has none in M0 — PUT is last-writer-wins.
 * M1 will add ``If-Match``; until then the UI surfaces a soft
 * "Reload to see latest" hint after every save, not a 412.
 */
import { getJson, putJson } from "./client";

export type TenantPlan = "free" | "pro" | "enterprise";

/** Stream Y-1 — LLM credentials are platform-exclusive; ``platform`` is the
 *  only mode (tenant BYOK was removed). */
export type CredentialsMode = "platform";

export interface TenantConfigRecord {
  tenant_id: string;
  display_name: string;
  plan: TenantPlan;
  /** Stream Y-1 — always ``platform``; LLM credentials are platform-exclusive. */
  credentials_mode: CredentialsMode;
  /** Stream Y-1 — DORMANT: tenant LLM BYOK removed; no longer resolved. */
  model_credentials_ref: Record<string, string>;
  /** Stream Y-1 — DORMANT: tenant LLM BYOK removed; no longer resolved. */
  tool_credentials: Record<string, string>;
  mcp_allowlist: string[];
  rate_limit_override: Record<string, unknown>;
  pii_fields: string[];
  /** E.8 — HTTP-tool egress. Empty allowlist ↔ allow all public hosts
   *  (SSRF blocked by the tool); non-empty ↔ strict allow-only. */
  http_tool_allowlist: string[];
  /** E.8 — hosts (exact or subdomain) the HTTP tool refuses even under
   *  allow-all; takes precedence over the allowlist. */
  http_tool_denylist: string[];
  mcp_servers: Record<string, unknown>[];
  audit_retention_days: number;
  event_log_retention_days: number;
  /** B-63 — upload / artifact retention in days, counted from LAST USE
   *  (upload time, last re-attach / download, last run in its conversation —
   *  whichever is latest). Range [7, 365]; ``null`` = platform default (90). */
  upload_retention_days: number | null;
  artifact_retention_days: number | null;
  /** B-282 — days a conversation's work directory survives after its last run.
   *  Range [7, 365]; ``null`` = platform default (90). */
  conversation_workspace_retention_days: number | null;
  /** Sprint #4 (Mini-ADR U-28) — Curator thresholds.
   *  ``skill_stale_days`` default 30, range [1, 365].
   *  ``skill_archive_days`` default 90, range [2, 730], and must be
   *  strictly greater than ``skill_stale_days`` (DB CHECK + Pydantic
   *  model_validator). */
  skill_stale_days: number;
  skill_archive_days: number;
  /** SE-16 (SE-A41) — per-tenant skill-evolution rollout gate; ANDed with
   *  the platform master switch. Default false (rolled out tenant by
   *  tenant). */
  skill_evolution_enabled: boolean;
  /** SE-16 (SE-A45) — what fraction of implicit-success candidates the
   *  evolution worker screens through the cheap aux quality judge before
   *  distilling. Default 5, range [0, 100]. */
  skill_evolution_judge_sample_pct: number;
  created_at: string;
  updated_at: string;
  updated_by: string;
}

export interface TenantConfigPatchBody {
  display_name?: string;
  plan?: TenantPlan;
  /** Stream Y-1 — DORMANT: tenant LLM BYOK removed; retained so the generic
   *  JSON tenant-config editor round-trips the field without resolving it. */
  model_credentials_ref?: Record<string, string>;
  mcp_allowlist?: string[];
  rate_limit_override?: Record<string, unknown>;
  pii_fields?: string[];
  http_tool_allowlist?: string[];
  http_tool_denylist?: string[];
  mcp_servers?: Record<string, unknown>[];
  audit_retention_days?: number;
  event_log_retention_days?: number;
  /** B-63 — ``null`` resets to the platform default; omit the key to leave it. */
  upload_retention_days?: number | null;
  artifact_retention_days?: number | null;
  conversation_workspace_retention_days?: number | null;
  skill_stale_days?: number;
  skill_archive_days?: number;
  skill_evolution_enabled?: boolean;
  skill_evolution_judge_sample_pct?: number;
}

/** Stream O Mini-ADR O-13 — one row of the Credentials panel's provider /
 *  tool tables (composite view from ``GET .../config/credentials``). */
export interface CredentialRow {
  /** ``provider`` rows carry this; ``tool`` rows leave it undefined. */
  provider?: string;
  /** ``tool`` rows carry this; ``provider`` rows leave it undefined. */
  tool?: string;
  platform_configured: boolean;
  tenant_secret_ref: string | null;
  used_by_agents: number;
}

export interface CredentialsView {
  mode: CredentialsMode;
  providers: CredentialRow[];
  tools: CredentialRow[];
}

export async function getTenantConfig(tenantId: string): Promise<TenantConfigRecord> {
  return getJson<TenantConfigRecord>(
    `/v1/tenants/${encodeURIComponent(tenantId)}/config`,
  );
}

/** B-63 — the platform default the backend compares against when a
 *  retention field is unset (``null``). */
export const FILE_RETENTION_DEFAULT_DAYS = 90;

/** B-63 — which file retention fields this patch lowers (effective days,
 *  unset = platform default). Lowering needs an explicit confirmation: the
 *  backend answers 409 ``RETENTION_DECREASE_NOT_CONFIRMED`` otherwise. */
export function retentionDecreases(
  record: TenantConfigRecord,
  patch: TenantConfigPatchBody,
): { field: string; from: number; to: number }[] {
  const fields = [
    "upload_retention_days",
    "artifact_retention_days",
    "conversation_workspace_retention_days",
  ] as const;
  const out: { field: string; from: number; to: number }[] = [];
  for (const field of fields) {
    if (!(field in patch)) continue;
    const from = record[field] ?? FILE_RETENTION_DEFAULT_DAYS;
    const to = patch[field] ?? FILE_RETENTION_DEFAULT_DAYS;
    if (to < from) out.push({ field, from, to });
  }
  return out;
}

export async function upsertTenantConfig(
  tenantId: string,
  body: TenantConfigPatchBody,
  options: { confirmRetentionDecrease?: boolean } = {},
): Promise<TenantConfigRecord> {
  return putJson<TenantConfigRecord>(
    `/v1/tenants/${encodeURIComponent(tenantId)}/config`,
    body,
    options.confirmRetentionDecrease ? { params: { confirm_retention_decrease: true } } : undefined,
  );
}

/** Stream O Mini-ADR O-13 — composite (read-only) view driving the
 *  Credentials panel. */
export async function getCredentialsView(tenantId: string): Promise<CredentialsView> {
  return getJson<CredentialsView>(
    `/v1/tenants/${encodeURIComponent(tenantId)}/config/credentials`,
  );
}
