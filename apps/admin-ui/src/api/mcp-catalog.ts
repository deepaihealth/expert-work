/**
 * MCP Catalog SDK — backed by ``/v1/platform/mcp-catalog`` (platform,
 * system_admin only) and ``/v1/mcp-servers/catalog`` (tenant admin),
 * Stream W.
 *
 * A *catalog entry* (``McpCatalogEntry``) is a fully-configured platform MCP
 * server curated by a platform admin (transport + concrete URL + auth: none /
 * shared bearer / per-user oauth2). Tenants browse the catalog (each entry
 * carries an ``entitled`` flag derived from their plan tier and a
 * ``tenant_enabled`` opt-in flag) and enable the ones they want.
 *
 * Backend returns the standard ``{success, data, error}`` envelope; the
 * unwrapped payload is typed below.  ``getJson`` / ``postJson`` / ``patchJson``
 * call ``unwrap()`` internally — callers receive the data directly.
 */
import { getJson, patchJson, postJson, apiClient, unwrap } from "./client";
import type { ApiEnvelope } from "./client";
import type { McpAuthType, McpTransport } from "./mcp-servers";

// ── Domain types ─────────────────────────────────────────────────────────

export type McpRequiredTier = "free" | "pro" | "enterprise";

/** Preset connector categories — stored as the stable slug, displayed via the
 *  i18n ``labelKey``. Shared by the platform config drawer (Select options) and
 *  the catalog list (category column). */
export const MCP_CATEGORIES: { value: string; labelKey: string }[] = [
  { value: "search", labelKey: "mcp_catalog.cat_search" },
  { value: "database", labelKey: "mcp_catalog.cat_database" },
  { value: "payment", labelKey: "mcp_catalog.cat_payment" },
  { value: "location", labelKey: "mcp_catalog.cat_location" },
  { value: "social", labelKey: "mcp_catalog.cat_social" },
  { value: "design", labelKey: "mcp_catalog.cat_design" },
  { value: "document", labelKey: "mcp_catalog.cat_document" },
  { value: "browser-automation", labelKey: "mcp_catalog.cat_browser" },
  { value: "scraping", labelKey: "mcp_catalog.cat_scraping" },
  { value: "dev-tools", labelKey: "mcp_catalog.cat_dev_tools" },
  { value: "other", labelKey: "mcp_catalog.cat_other" },
];

/** The i18n key for a category slug, or ``null`` for an unknown (legacy
 *  free-text) value the caller should render verbatim. */
export function mcpCategoryLabelKey(slug: string): string | null {
  return MCP_CATEGORIES.find((c) => c.value === slug)?.labelKey ?? null;
}

export interface McpCatalogEntry {
  id: string;
  name: string;
  display_name: string;
  description: string;
  category: string;
  icon: string;
  transport: McpTransport;
  /** Concrete server URL (the platform-server model dropped the {param}
   *  template + auth_schema fields). */
  url_template: string;
  auth_type: McpAuthType;
  /** oauth2 entries: the platform-registered OAuth app. */
  oauth_client_id?: string | null;
  oauth_scopes?: string | null;
  /** bearer (shared A) entries: whether a platform token is stored (the ref
   *  itself is never exposed). */
  has_bearer_token?: boolean;
  /** Runtime tuning (null = orchestrator defaults). ``timeout_s`` caps the
   *  connect/call round-trip; ``sse_read_timeout_s`` is the per-read idle wait
   *  between streamed events. */
  timeout_s?: number | null;
  sse_read_timeout_s?: number | null;
  /** Platform-curated tool denylist — bare tool names filtered from list_tools
   *  at runtime. Empty = every advertised tool is exposed. */
  disabled_tools?: string[];
  required_tier: McpRequiredTier;
  enabled: boolean;
  created_at: string;
  updated_at: string;
  updated_by: string;
}

/** A catalog entry as seen by a tenant admin — augmented with whether the
 *  tenant's plan tier meets ``required_tier`` and whether the tenant has opted
 *  into this platform server (``mcp_allowlist``). */
export interface TenantCatalogEntry extends McpCatalogEntry {
  entitled: boolean;
  /** Opt-in selection state — the server's name is in the tenant's
   *  ``mcp_allowlist`` (P4). A/B alike must be enabled before use; oauth2 then
   *  additionally needs a per-user authorization. */
  tenant_enabled: boolean;
}

/** Result of a tenant enable/disable toggle on a platform server. */
export interface PlatformServerToggle {
  name: string;
  tenant_enabled: boolean;
}

/** Upsert (create) body for ``POST /v1/platform/mcp-catalog``. */
export interface CatalogUpsertBody {
  name: string;
  display_name: string;
  description?: string;
  category?: string;
  icon?: string;
  transport: McpTransport;
  /** Concrete server URL. */
  url_template: string;
  auth_type?: McpAuthType;
  /** bearer (shared A): write-only platform token; stored in the SecretStore,
   *  only a ref is persisted. */
  bearer_token?: string;
  /** oauth2 (B): platform-registered OAuth app + space-separated scopes. */
  oauth_client_id?: string;
  oauth_scopes?: string;
  /** Runtime tuning (omit = orchestrator defaults). */
  timeout_s?: number;
  sse_read_timeout_s?: number;
  required_tier?: McpRequiredTier;
  enabled?: boolean;
}

/** Patch body — ``name`` / ``transport`` / ``auth_type`` are immutable. */
export interface CatalogPatchBody {
  display_name?: string;
  description?: string;
  category?: string;
  icon?: string;
  url_template?: string;
  /** Re-paste the platform bearer token (write-only); omit to keep existing. */
  bearer_token?: string;
  timeout_s?: number;
  sse_read_timeout_s?: number;
  /** Platform-curated tool denylist; ``[]`` re-enables every tool. */
  disabled_tools?: string[];
  required_tier?: McpRequiredTier;
  enabled?: boolean;
}

// ── Platform catalog (system_admin) ────────────────────────────────────────

/** ``GET /v1/platform/mcp-catalog`` — full connector catalog. */
export async function listPlatformCatalog(): Promise<McpCatalogEntry[]> {
  return getJson<McpCatalogEntry[]>("/v1/platform/mcp-catalog");
}

/** ``POST /v1/platform/mcp-catalog`` — create a connector type. */
export async function createPlatformCatalogEntry(
  body: CatalogUpsertBody,
): Promise<McpCatalogEntry> {
  return postJson<McpCatalogEntry>("/v1/platform/mcp-catalog", body);
}

/** ``GET /v1/platform/mcp-catalog/{id}`` — single connector type. */
export async function getPlatformCatalogEntry(
  id: string,
): Promise<McpCatalogEntry> {
  return getJson<McpCatalogEntry>(
    `/v1/platform/mcp-catalog/${encodeURIComponent(id)}`,
  );
}

/** ``PATCH /v1/platform/mcp-catalog/{id}`` — update a mutable subset. */
export async function updatePlatformCatalogEntry(
  id: string,
  body: CatalogPatchBody,
): Promise<McpCatalogEntry> {
  return patchJson<McpCatalogEntry>(
    `/v1/platform/mcp-catalog/${encodeURIComponent(id)}`,
    body,
  );
}

/** ``DELETE /v1/platform/mcp-catalog/{id}`` — 204; 409 ``CATALOG_IN_USE``
 *  when at least one tenant has instantiated the entry. */
export async function deletePlatformCatalogEntry(id: string): Promise<void> {
  await apiClient.delete(`/v1/platform/mcp-catalog/${encodeURIComponent(id)}`);
}

/** A single tool a probed server exposes. */
export interface McpCatalogTool {
  name: string;
  description: string;
  /** JSON Schema for the tool's arguments (object with properties/required). */
  input_schema?: Record<string, unknown>;
  /** Whether this tool is in the platform-curated denylist. */
  disabled?: boolean;
}

/** Result of live-probing a configured platform server.
 *  ``ok`` = tools listed / ``unreachable`` = probe failed (``error`` carries the
 *  code) / ``not_probeable`` = oauth2 (per-user token the platform never holds). */
export interface McpCatalogToolsResult {
  status: "ok" | "unreachable" | "not_probeable";
  tool_count: number;
  tools: McpCatalogTool[];
  error: string | null;
}

/** ``POST /v1/platform/mcp-catalog/{id}/tools`` — live-probe + list tools. */
export async function listCatalogTools(
  id: string,
): Promise<McpCatalogToolsResult> {
  return postJson<McpCatalogToolsResult>(
    `/v1/platform/mcp-catalog/${encodeURIComponent(id)}/tools`,
    {},
  );
}

// ── Tenant catalog (tenant admin) ──────────────────────────────────────────

/** ``GET /v1/mcp-servers/catalog`` — catalog entries the tenant may browse,
 *  each carrying an ``entitled`` flag. */
export async function listTenantCatalog(): Promise<TenantCatalogEntry[]> {
  return getJson<TenantCatalogEntry[]>("/v1/mcp-servers/catalog");
}

/** ``POST /v1/mcp-servers/catalog/{id}/enable`` — tenant opts into a platform
 *  shared server (adds it to ``mcp_allowlist``). Idempotent; tier-gated. */
export async function enablePlatformServer(
  id: string,
): Promise<PlatformServerToggle> {
  return postJson<PlatformServerToggle>(
    `/v1/mcp-servers/catalog/${encodeURIComponent(id)}/enable`,
    {},
  );
}

/** ``DELETE /v1/mcp-servers/catalog/{id}/enable`` — tenant opts out (removes it
 *  from ``mcp_allowlist``). Idempotent. */
export async function disablePlatformServer(
  id: string,
): Promise<PlatformServerToggle> {
  const response = await apiClient.delete<ApiEnvelope<PlatformServerToggle>>(
    `/v1/mcp-servers/catalog/${encodeURIComponent(id)}/enable`,
  );
  return unwrap(response.data);
}

/** Result of a by-name allowlist removal; ``changed`` = the name was actually
 *  present (false when it was already gone — the call is idempotent). */
export interface AllowlistRemoval extends PlatformServerToggle {
  changed: boolean;
}

/** ``DELETE /v1/mcp-servers/allowlist/{name}`` — X-5: drop a name from
 *  ``mcp_allowlist`` by NAME, without resolving the catalog. For residual
 *  entries whose catalog entry was deleted (the catalog-id route has nothing
 *  to address). Idempotent. */
export async function removeAllowlistName(name: string): Promise<AllowlistRemoval> {
  const response = await apiClient.delete<ApiEnvelope<AllowlistRemoval>>(
    `/v1/mcp-servers/allowlist/${encodeURIComponent(name)}`,
  );
  return unwrap(response.data);
}
