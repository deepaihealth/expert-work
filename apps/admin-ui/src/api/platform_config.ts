/**
 * Platform Credentials SDK — backed by ``/v1/platform/credentials`` (Stream P,
 * Mini-ADR P-11). **Platform-level, system_admin-only** (not tenant-scoped).
 *
 * The backend returns the full provider/tool catalog with each row's source
 * (env seed / DB overlay / unset), the effective secret_ref (DB wins), the
 * enabled flag, and a cross-tenant used-by-agents count. Values are never
 * returned — only refs (kms:// / secret:// URIs) and flags. Writes carry a
 * ref (validated server-side to reject plaintext).
 */
import { apiClient, getJson, putJson } from "./client";

export type PlatformSecretSource = "env" | "db" | "unset";

/** Stream Y-MK — one credential key within a provider (multi-key failover). */
export interface PlatformProviderKey {
  key_id: string;
  secret_ref: string;
  enabled: boolean;
  priority: number;
}

/**
 * B-51 — one platform feature that needs this provider's credential.
 *
 * ``used_by_agents`` counts agent manifests only; the platform's own
 * background features (embedding / rerank / eval agent / quality judge /
 * memory consolidation) need the very same credential and used to count for
 * nothing — which is how an "unset + used by 0 agents" row got read as
 * "nobody needs this".
 *
 * ``feature`` is a stable machine id (i18n lives in the frontend);
 * ``enabled`` is the feature's *effective* on/off, DB overlay included.
 */
export interface PlatformProviderUse {
  feature: string;
  model: string;
  enabled: boolean;
}

export interface PlatformProviderRow {
  provider: string;
  source: PlatformSecretSource;
  /** Primary (default/best) key's ref — backward-compat scalar. */
  secret_ref: string | null;
  /** Primary key's enabled flag — backward-compat scalar. */
  enabled: boolean;
  /** Stream Y-MK — all keys, priority-sorted (best first). Empty for env/unset. */
  keys: PlatformProviderKey[];
  used_by_agents: number;
  /** B-51 — platform-side dependencies. Absent on the write endpoints' rows. */
  platform_uses?: PlatformProviderUse[];
  tenant_override_count: number;
}

export interface PlatformToolRow {
  tool: string;
  source: PlatformSecretSource;
  secret_ref: string | null;
  enabled: boolean;
  used_by_agents: number;
  tenant_override_count: number;
}

export interface PlatformCredentialsView {
  providers: PlatformProviderRow[];
  tools: PlatformToolRow[];
}

/**
 * Write body — provide exactly one of:
 *  - ``secret_ref``: a ``secret://`` / ``kms://`` reference (operator-managed); or
 *  - ``value``: a raw key pasted in the UI. The backend encrypts it and stores
 *    only the generated ref (Stream Q). Never logged; sent over the wire once.
 */
export interface PlatformSecretUpsertBody {
  secret_ref?: string;
  value?: string;
  enabled: boolean;
  /** Stream Y-MK — failover order within a provider (lower tried first). */
  priority?: number;
}

export async function getPlatformCredentials(): Promise<PlatformCredentialsView> {
  return getJson<PlatformCredentialsView>("/v1/platform/credentials");
}

export async function upsertPlatformProvider(
  provider: string,
  body: PlatformSecretUpsertBody,
): Promise<PlatformProviderRow> {
  return putJson<PlatformProviderRow>(
    `/v1/platform/credentials/providers/${encodeURIComponent(provider)}`,
    body,
  );
}

export async function upsertPlatformTool(
  tool: string,
  body: PlatformSecretUpsertBody,
): Promise<PlatformToolRow> {
  return putJson<PlatformToolRow>(
    `/v1/platform/credentials/tools/${encodeURIComponent(tool)}`,
    body,
  );
}

export async function deletePlatformProvider(provider: string): Promise<void> {
  await apiClient.delete(
    `/v1/platform/credentials/providers/${encodeURIComponent(provider)}`,
  );
}

/**
 * Stream Y-MK — upsert one named key of a provider for multi-key failover.
 * ``key_id="default"`` is the primary; extra keys (separate billing accounts /
 * rate-limit pools) are tried in ``priority`` order when the primary is rate-
 * limited / out of balance / revoked.
 */
export async function upsertPlatformProviderKey(
  provider: string,
  keyId: string,
  body: PlatformSecretUpsertBody,
): Promise<PlatformProviderKey> {
  return putJson<PlatformProviderKey>(
    `/v1/platform/credentials/providers/${encodeURIComponent(provider)}/keys/${encodeURIComponent(keyId)}`,
    body,
  );
}

export async function deletePlatformProviderKey(
  provider: string,
  keyId: string,
): Promise<void> {
  await apiClient.delete(
    `/v1/platform/credentials/providers/${encodeURIComponent(provider)}/keys/${encodeURIComponent(keyId)}`,
  );
}

export async function deletePlatformTool(tool: string): Promise<void> {
  await apiClient.delete(
    `/v1/platform/credentials/tools/${encodeURIComponent(tool)}`,
  );
}

/**
 * Per-tenant credential overrides — Stream HX-8. Platform-managed
 * (system_admin only; tenants never see these): an enabled override row
 * routes the tenant through its own platform-procured ref, a disabled row
 * suppresses the key for the tenant entirely (no fallback).
 */
export type TenantEffectiveSource =
  | "tenant"
  | "suppressed"
  | "db"
  | "env"
  | "unset";

export interface TenantOverrideRow {
  tenant_id: string;
  secret_ref: string;
  enabled: boolean;
  created_at: string;
  updated_at: string;
  updated_by: string;
}

export interface TenantProviderEntry {
  provider: string;
  override: (TenantOverrideRow & { provider: string }) | null;
  effective_source: TenantEffectiveSource;
  effective_ref: string | null;
}

export interface TenantToolEntry {
  tool: string;
  override: (TenantOverrideRow & { tool: string }) | null;
  effective_source: TenantEffectiveSource;
  effective_ref: string | null;
}

export interface TenantCredentialsView {
  tenant_id: string;
  providers: TenantProviderEntry[];
  tools: TenantToolEntry[];
}

export async function getTenantCredentials(
  tenantId: string,
): Promise<TenantCredentialsView> {
  return getJson<TenantCredentialsView>(
    `/v1/platform/credentials/tenants/${encodeURIComponent(tenantId)}`,
  );
}

export async function upsertTenantProviderOverride(
  tenantId: string,
  provider: string,
  body: PlatformSecretUpsertBody,
): Promise<TenantOverrideRow> {
  return putJson<TenantOverrideRow>(
    `/v1/platform/credentials/tenants/${encodeURIComponent(tenantId)}/providers/${encodeURIComponent(provider)}`,
    body,
  );
}

export async function upsertTenantToolOverride(
  tenantId: string,
  tool: string,
  body: PlatformSecretUpsertBody,
): Promise<TenantOverrideRow> {
  return putJson<TenantOverrideRow>(
    `/v1/platform/credentials/tenants/${encodeURIComponent(tenantId)}/tools/${encodeURIComponent(tool)}`,
    body,
  );
}

export async function deleteTenantProviderOverride(
  tenantId: string,
  provider: string,
): Promise<void> {
  await apiClient.delete(
    `/v1/platform/credentials/tenants/${encodeURIComponent(tenantId)}/providers/${encodeURIComponent(provider)}`,
  );
}

export async function deleteTenantToolOverride(
  tenantId: string,
  tool: string,
): Promise<void> {
  await apiClient.delete(
    `/v1/platform/credentials/tenants/${encodeURIComponent(tenantId)}/tools/${encodeURIComponent(tool)}`,
  );
}
