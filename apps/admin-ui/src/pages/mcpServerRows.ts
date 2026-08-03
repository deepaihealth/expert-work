/**
 * Unify the tenant's MCP servers into one row model for SettingsMcpServers.
 *
 * Two disjoint sources merge here:
 *   - custom servers (`tenant_mcp_server` rows) → full McpServer detail, editable.
 *   - platform servers the tenant opted into (allowlist, from `/available`
 *     with source==="platform") → read-only, platform-hosted config.
 *
 * `available`'s source==="tenant" entries are ignored: `servers` already carries
 * those with full columns (transport/url/auth), so they'd be duplicates.
 */
import type { AvailableMcpServer, McpAuthType, McpServer } from "../api/mcp-servers";

export type UnifiedRow =
  | { key: string; source: "tenant"; server: McpServer }
  | {
      key: string;
      source: "platform";
      name: string;
      displayName: string;
      authType: McpAuthType;
      catalogId: string | null;
    };

export function buildUnifiedRows(
  servers: readonly McpServer[],
  available: readonly AvailableMcpServer[],
): UnifiedRow[] {
  const platform: UnifiedRow[] = available
    .filter((a) => a.source === "platform")
    .map((a) => ({
      key: `platform:${a.name}`,
      source: "platform" as const,
      name: a.name,
      displayName: a.display_name ?? a.name,
      authType: a.auth_type ?? "none",
      catalogId: a.catalog_id ?? null,
    }));
  const custom: UnifiedRow[] = servers.map((s) => ({
    // Cross-tenant W4 — two tenants may own a same-named server in the
    // ``tenant_id=*`` aggregate; key by (tenant, name) so rows never collide.
    key: `tenant:${s.tenant_id ?? "home"}:${s.name}`,
    source: "tenant" as const,
    server: s,
  }));
  return [...platform, ...custom];
}
