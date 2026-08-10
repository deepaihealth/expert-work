/**
 * API keys SDK — backed by ``/v1/api_keys`` + ``/v1/service_accounts/{id}/api_keys``.
 *
 * Stream H.1b PR 3.
 *
 * Two distinct surfaces:
 *
 *   - ``GET /v1/api_keys`` is the cross-SA admin list. Stream N adds
 *     ``tenant_id=*`` for system_admin's cross-tenant aggregate; the
 *     UI surfaces this via :class:`TenantSwitcher`.
 *   - ``POST /v1/service_accounts/{id}/api_keys`` mints a key under a
 *     specific service account. The response carries the plaintext
 *     *once* — :class:`ApiKeyCreated`. The UI must surface it to the
 *     operator immediately + never re-fetch.
 *
 * Rotation (``POST /v1/api_keys/{id}/rotate``) returns a new
 * :class:`ApiKeyCreated` while keeping the old bearer alive for the
 * supplied ``grace_period_s`` — see Mini-ADR K-1.
 */
import { getJson, postJson, withTenantScope, type TenantScope } from "./client";
import { apiClient } from "./client";

export type ApiKeyScope = "read" | "write" | "admin";

export const API_KEY_SCOPES: readonly ApiKeyScope[] = ["read", "write", "admin"];

export interface ApiKeyRecord {
  id: string;
  service_account_id: string;
  tenant_id: string;
  prefix: string;
  scopes: ApiKeyScope[];
  expires_at: string | null;
  last_used_at: string | null;
  revoked_at: string | null;
  rotated_at: string | null;
  grace_period_s: number | null;
  created_by: string;
  created_at: string;
}

export interface ApiKeyList {
  items: ApiKeyRecord[];
  total: number;
  cross_tenant: boolean;
}

export interface ListApiKeysParams {
  tenantScope?: TenantScope;
  serviceAccountId?: string;
}

export async function listApiKeys(
  params: ListApiKeysParams = {},
): Promise<ApiKeyList> {
  const { tenantScope, serviceAccountId } = params;
  const query = withTenantScope(
    { service_account_id: serviceAccountId },
    tenantScope,
  );
  return getJson<ApiKeyList>("/v1/api_keys", { params: query });
}

export interface ApiKeyCreated {
  api_key: ApiKeyRecord;
  plaintext: string;
}

export interface CreateApiKeyRequest {
  scopes: ApiKeyScope[];
  expires_at?: string | null;
}

export async function createApiKey(
  serviceAccountId: string,
  body: CreateApiKeyRequest,
): Promise<ApiKeyCreated> {
  return postJson<ApiKeyCreated>(
    `/v1/service_accounts/${serviceAccountId}/api_keys`,
    body,
  );
}

export async function revealApiKey(apiKeyId: string): Promise<{ plaintext: string }> {
  return postJson<{ plaintext: string }>(`/v1/api_keys/${apiKeyId}/reveal`, {});
}

export async function revokeApiKey(apiKeyId: string): Promise<void> {
  // DELETE returns 204; the response body is empty, so we don't go
  // through ``unwrap``.
  await apiClient.delete(`/v1/api_keys/${apiKeyId}`);
}

export interface RotateApiKeyRequest {
  grace_period_s?: number;
}

export interface ApiKeyRotated {
  old: ApiKeyRecord;
  new: ApiKeyCreated;
}

export async function rotateApiKey(
  apiKeyId: string,
  body: RotateApiKeyRequest = {},
): Promise<ApiKeyRotated> {
  return postJson<ApiKeyRotated>(`/v1/api_keys/${apiKeyId}/rotate`, body);
}

/** @deprecated Moved to ``./service_accounts``. Re-exported for backward
 *  compat — H.4 PR 7 split the IAM surface into its own SDK file. */
export type {
  ServiceAccountList,
  ServiceAccountRecord,
} from "./service_accounts";
export { listServiceAccounts } from "./service_accounts";
