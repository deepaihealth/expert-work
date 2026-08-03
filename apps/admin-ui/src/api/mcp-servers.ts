/**
 * MCP Servers SDK — backed by ``/v1/mcp-servers`` (Stream V-F).
 *
 * Tenant admins manage remote MCP servers that their agents can call tools
 * from.  Tokens are write-only: create accepts a raw token; edit uses
 * ``UpdateMcpServerBody.token`` to rotate (leave ``undefined`` to keep the
 * current value).  The backend strips the persisted secret ref from every
 * response (``_public``), so ``token`` never appears in a server record.
 *
 * Backend returns the standard ``{success, data, error}`` envelope; the
 * unwrapped payload is typed below.  ``getJson`` / ``postJson`` / ``patchJson``
 * all call ``unwrap()`` internally — the caller receives the data directly.
 */
import {
  getJson,
  patchJson,
  postJson,
  apiClient,
  withTenantScope,
  type TenantScope,
} from "./client";

export type McpTransport = "sse" | "streamable_http";
// "oauth2" only appears on catalog entries (per-user OAuth connectors, Stream
// MCP-OAUTH); tenant_mcp_server rows are only ever "none" | "bearer".
export type McpAuthType = "none" | "bearer" | "oauth2";

export interface McpServer {
  id: string;
  name: string;
  transport: McpTransport;
  url: string;
  auth_type: McpAuthType;
  timeout_s: number;
  enabled: boolean;
  created_at: string;
  updated_at: string;
  /** Names of configured custom HTTP headers (values stay encrypted server-side). */
  custom_header_names?: string[] | null;
  /** SSE read-timeout override in seconds; null = SDK default. */
  sse_read_timeout_s?: number | null;
  /** Cross-tenant W4 — owning tenant in the ``tenant_id=*`` aggregate;
   *  absent on single-tenant reads / pre-W4 backends. */
  tenant_id?: string | null;
}

export interface CreateMcpServerBody {
  name: string;
  transport: McpTransport;
  url: string;
  auth_type: McpAuthType;
  /** Raw token — stored encrypted.  Required when ``auth_type="bearer"``. */
  token?: string;
  /** Custom HTTP headers (name → value); values stored encrypted as one blob. */
  custom_headers?: Record<string, string>;
  /** SSE read-timeout override in seconds. */
  sse_read_timeout_s?: number;
  timeout_s?: number;
}

export interface UpdateMcpServerBody {
  url?: string;
  /**
   * Rotate the bearer token.  Omit (``undefined``) to keep the current
   * token; supply a non-empty string to replace it.
   */
  token?: string;
  /** Replace the custom-header set (omit to keep; clearing is delete+recreate). */
  custom_headers?: Record<string, string>;
  sse_read_timeout_s?: number;
  timeout_s?: number;
  enabled?: boolean;
}

export interface McpTool {
  name: string;
  description: string;
}

export interface AvailableMcpServer {
  name: string;
  source: "platform" | "tenant";
  enabled?: boolean;
  /** Catalog connector id this tenant server was instantiated from (Stream W).
   *  Absent for custom-registered / platform-allowlisted servers. */
  catalog_id?: string;
  /** Human-readable catalog connector name (Stream W). */
  catalog_name?: string;
  /** Platform (allowlist) rows: connector display name (enriched by /available). */
  display_name?: string;
  /** Platform (allowlist) rows: connector auth type (enriched by /available). */
  auth_type?: McpAuthType;
}

export interface TestConnectionBody {
  transport: McpTransport;
  url: string;
  auth_type: McpAuthType;
  /** Required when ``auth_type="bearer"``. */
  token?: string;
  /** Custom HTTP headers (name → value) to send during the probe. */
  custom_headers?: Record<string, string>;
  sse_read_timeout_s?: number;
  timeout_s?: number;
}

/** ``GET /v1/mcp-servers`` — list all MCP servers for the current tenant. */
export async function listMcpServers(tenantScope?: TenantScope): Promise<McpServer[]> {
  return getJson<McpServer[]>("/v1/mcp-servers", {
    params: withTenantScope({}, tenantScope),
  });
}

/** ``POST /v1/mcp-servers`` — register a new MCP server. */
export async function createMcpServer(
  body: CreateMcpServerBody,
): Promise<McpServer> {
  return postJson<McpServer>("/v1/mcp-servers", body);
}

/** ``PATCH /v1/mcp-servers/{name}`` — update URL / token / timeout / enabled. */
export async function updateMcpServer(
  name: string,
  body: UpdateMcpServerBody,
): Promise<McpServer> {
  return patchJson<McpServer>(
    `/v1/mcp-servers/${encodeURIComponent(name)}`,
    body,
  );
}

/** ``DELETE /v1/mcp-servers/{name}`` — remove an MCP server. */
export async function deleteMcpServer(name: string): Promise<void> {
  await apiClient.delete(`/v1/mcp-servers/${encodeURIComponent(name)}`);
}

/**
 * ``POST /v1/mcp-servers/test`` — probe-only connection test; nothing is
 * persisted.  Returns the number of tools advertised by the server.
 */
export async function testMcpConnection(
  body: TestConnectionBody,
): Promise<{ tool_count: number }> {
  return postJson<{ tool_count: number }>("/v1/mcp-servers/test", body);
}

/**
 * ``GET /v1/mcp-servers/{name}/tools`` — live probe of a registered server;
 * returns the tool list.  On probe failure the backend returns 502 — callers
 * should treat that as ``unreachable``.
 */
export async function listMcpServerTools(
  name: string,
  tenantScope?: TenantScope,
): Promise<McpTool[]> {
  return getJson<McpTool[]>(
    `/v1/mcp-servers/${encodeURIComponent(name)}/tools`,
    { params: withTenantScope({}, tenantScope) },
  );
}

/**
 * ``GET /v1/mcp-servers/available`` — servers available to this tenant
 * (platform-allowlisted names + the tenant's own registered servers).
 */
export async function listAvailableMcpServers(
  tenantScope?: TenantScope,
): Promise<AvailableMcpServer[]> {
  return getJson<AvailableMcpServer[]>("/v1/mcp-servers/available", {
    params: withTenantScope({}, tenantScope),
  });
}
