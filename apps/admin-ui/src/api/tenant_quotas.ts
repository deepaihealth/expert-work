/**
 * Tenant Quotas SDK — backed by ``/v1/tenants/{tenant_id}/quotas``
 * (Stream C.5).
 *
 * **Tenant-scoped only**: no cross-tenant list endpoint. For
 * system_admin to manage quotas of another tenant, the UI uses
 * ``TenantSwitcher`` to switch the effective ``tenant_id`` in the path.
 *
 * Backend returns the standard ``{success, data, error}`` envelope, so
 * this SDK uses ``getJson`` / ``postJson`` directly.
 */
import { apiClient, getJson, postJson } from "./client";

export type QuotaDimension =
  | "qps"
  | "tokens_per_day"
  | "sandboxes"
  | "monthly_token_budget"
  | "image_upload_count_30d"
  | "image_storage_bytes"
  | "artifact_download_count_30d"
  | "workspace_bytes_per_user";

export interface TenantQuotaRecord {
  id: string;
  tenant_id: string;
  dimension: QuotaDimension;
  scope: Record<string, string>;
  limit_value: number;
  burst: number | null;
  effective_from: string;
  effective_until: string | null;
  updated_by: string;
  updated_at: string;
}

export interface TenantQuotaPatchBody {
  dimension: QuotaDimension;
  scope?: Record<string, string>;
  limit_value: number;
  burst?: number | null;
  effective_until?: string | null;
}

/** Backend GET returns ``{success, data: TenantQuotaRecord[], error}``
 *  — the data is a flat array, *not* ``{items, total}``. */
export async function listTenantQuotas(tenantId: string): Promise<TenantQuotaRecord[]> {
  return getJson<TenantQuotaRecord[]>(
    `/v1/tenants/${encodeURIComponent(tenantId)}/quotas`,
  );
}

export async function upsertTenantQuota(
  tenantId: string,
  body: TenantQuotaPatchBody,
): Promise<TenantQuotaRecord> {
  return postJson<TenantQuotaRecord>(
    `/v1/tenants/${encodeURIComponent(tenantId)}/quotas`,
    body,
  );
}

export async function deleteTenantQuota(tenantId: string, quotaId: string): Promise<void> {
  await apiClient.delete(
    `/v1/tenants/${encodeURIComponent(tenantId)}/quotas/${encodeURIComponent(quotaId)}`,
  );
}
