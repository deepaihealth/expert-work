/**
 * SettingsAudit tests — Stream H.4 PR 4.
 *
 * Backend returns *raw* (un-enveloped) payloads (per audit.py contract),
 * so the axios adapter mocks deliver plain ``{items, next_cursor, has_more,
 * applied_scope}`` objects without the ``success`` wrapper.
 */
import { describe, expect, it, vi, beforeEach } from "vitest";
import { MemoryRouter } from "react-router-dom";
import { App } from "antd";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import "../../i18n";

import { SettingsAudit } from "../SettingsAudit";
import { TenantScopeProvider } from "../../tenant/TenantScopeContext";
import { AuthProvider } from "../../auth/AuthContext";
import { apiClient, setStoredToken } from "../../api/client";

function makeJwt(payload: Record<string, unknown>): string {
  const header = btoa(JSON.stringify({ alg: "none", typ: "JWT" }));
  const body = btoa(JSON.stringify(payload));
  return `${header}.${body}.`;
}

interface RouteHandler {
  match: (url: string, method: string, params: Record<string, unknown>) => boolean;
  respond: (params: Record<string, unknown>) => unknown;
}

function installAdapter(handlers: RouteHandler[]) {
  apiClient.defaults.adapter = (config) => {
    const url = config.url ?? "";
    const method = (config.method ?? "get").toLowerCase();
    const params = (config.params as Record<string, unknown> | undefined) ?? {};
    const handler = handlers.find((h) => h.match(url, method, params));
    return Promise.resolve({
      data: handler?.respond(params) ?? {},
      status: 200,
      statusText: "OK",
      headers: {},
      config,
      request: {},
    });
  };
}

function renderAudit() {
  setStoredToken(makeJwt({ sub: "u1", tenant_id: "t1", roles: ["admin"] }));
  return render(
    <MemoryRouter>
      <AuthProvider>
        <TenantScopeProvider>
          <App>
            <SettingsAudit />
          </App>
        </TenantScopeProvider>
      </AuthProvider>
    </MemoryRouter>,
  );
}

const baseEntry = {
  id: 1,
  tenant_id: "t1",
  actor_type: "user" as const,
  actor_id: "user-alice",
  on_behalf_of: null,
  action: "memory:update",
  resource_type: "memory_item",
  resource_id: "mem-1",
  result: "success" as const,
  reason: null,
  ip: null,
  user_agent: null,
  request_id: null,
  trace_id: null,
  details: { kind: "fact", content_len: 42 },
  occurred_at: "2026-05-26T10:00:00Z",
};

beforeEach(() => {
  vi.restoreAllMocks();
});

describe("SettingsAudit", () => {
  it("renders entries from the timeline", async () => {
    installAdapter([
      {
        match: (u) => u === "/v1/audit",
        respond: () => ({
          items: [baseEntry, { ...baseEntry, id: 2, action: "role_binding:create", result: "denied" }],
          next_cursor: null,
          has_more: false,
          applied_scope: "t1",
        }),
      },
    ]);
    renderAudit();
    await waitFor(() => {
      expect(screen.getByText("memory:update")).toBeInTheDocument();
      expect(screen.getByText("role_binding:create")).toBeInTheDocument();
    });
  });

  it("renders empty state when items=[]", async () => {
    installAdapter([
      {
        match: (u) => u === "/v1/audit",
        respond: () => ({ items: [], next_cursor: null, has_more: false, applied_scope: "t1" }),
      },
    ]);
    renderAudit();
    await waitFor(() => {
      expect(screen.getByText(/No audit entries match/i)).toBeInTheDocument();
    });
  });

  it("loads more on cursor click and appends entries", async () => {
    let calls = 0;
    installAdapter([
      {
        match: (u) => u === "/v1/audit",
        respond: () => {
          calls += 1;
          if (calls === 1) {
            return {
              items: [baseEntry],
              next_cursor: "cursor-2",
              has_more: true,
              applied_scope: "t1",
            };
          }
          return {
            items: [{ ...baseEntry, id: 2, action: "trigger:fire" }],
            next_cursor: null,
            has_more: false,
            applied_scope: "t1",
          };
        },
      },
    ]);
    const user = userEvent.setup();
    renderAudit();
    await waitFor(() => expect(screen.getByText("memory:update")).toBeInTheDocument());
    await user.click(screen.getByTestId("audit-load-more"));
    await waitFor(() => expect(screen.getByText("trigger:fire")).toBeInTheDocument());
    expect(screen.getByText("memory:update")).toBeInTheDocument();
  });

  it("opens the detail drawer with JSON payload on row click", async () => {
    installAdapter([
      {
        match: (u) => u === "/v1/audit",
        respond: () => ({
          items: [baseEntry],
          next_cursor: null,
          has_more: false,
          applied_scope: "t1",
        }),
      },
    ]);
    const user = userEvent.setup();
    renderAudit();
    await waitFor(() => expect(screen.getByText("memory:update")).toBeInTheDocument());
    await user.click(screen.getByTestId("audit-row-1"));
    await waitFor(() => expect(screen.getByTestId("audit-detail-payload")).toBeInTheDocument());
    expect(screen.getByTestId("audit-detail-payload").textContent).toContain('"kind": "fact"');
  });

  it("shows the actor's member name; the detail keeps name (id) and the filter stays id-based (B-316)", async () => {
    const aliceSub = "6f1c2a8e-0b7d-4c55-9a3e-1d2f3a4b5c6d";
    let lastAuditParams: Record<string, unknown> = {};
    installAdapter([
      {
        match: (u) => u === "/v1/audit",
        respond: (params) => {
          lastAuditParams = params;
          return {
            items: [{ ...baseEntry, actor_id: aliceSub }],
            next_cursor: null,
            has_more: false,
            applied_scope: "t1",
          };
        },
      },
      {
        match: (u) => u === "/v1/members",
        respond: () => ({
          success: true,
          error: null,
          data: {
            items: [
              {
                id: "0e1d2c3b-4a59-4687-9a6b-5c4d3e2f1a0b",
                tenant_id: "t1",
                email: "alice@acme.com",
                display_name: "Alice Wang",
                role: "admin",
                status: "active",
                keycloak_user_id: aliceSub,
                subject_id: null,
                invited_by: "seed",
                invited_at: null,
                activated_at: null,
                updated_at: null,
              },
            ],
            total: 1,
          },
        }),
      },
    ]);
    const user = userEvent.setup();
    renderAudit();

    await waitFor(() => expect(screen.getByTestId("audit-actor-1")).toHaveTextContent("Alice Wang"));
    expect(screen.queryByText(aliceSub)).toBeNull();

    await user.click(screen.getByTestId("audit-row-1"));
    expect(await screen.findByText(`user / Alice Wang (${aliceSub})`)).toBeInTheDocument();

    await user.type(screen.getByTestId("audit-actor-filter"), aliceSub);
    await waitFor(() => expect(lastAuditParams.actor_id).toBe(aliceSub));
  });

  it("threads the action filter into the SDK call", async () => {
    let lastParams: Record<string, unknown> = {};
    installAdapter([
      {
        match: (u) => u === "/v1/audit",
        respond: (params) => {
          lastParams = params;
          return { items: [], next_cursor: null, has_more: false, applied_scope: "t1" };
        },
      },
    ]);
    const user = userEvent.setup();
    renderAudit();
    // Initial fetch sets baseline.
    await waitFor(() => expect(lastParams).toBeDefined());
    // Typing into the action filter triggers a refresh with the new param.
    await user.type(screen.getByTestId("audit-action-filter"), "memory:update");
    await waitFor(() => expect(lastParams.action).toBe("memory:update"));
  });

  it("surfaces an MCP traffic badge with server + response size (Stream 14.4)", async () => {
    const mcpEntry = {
      ...baseEntry,
      id: 7,
      action: "tool:call",
      resource_type: "tool",
      resource_id: "mcp__github__search",
      details: { mcp_server: "github", mcp_tool: "search", response_chars: 2400, mcp_is_error: false },
    };
    installAdapter([
      {
        match: (u) => u === "/v1/audit",
        respond: () => ({
          items: [mcpEntry],
          next_cursor: null,
          has_more: false,
          applied_scope: "t1",
        }),
      },
    ]);
    renderAudit();
    await waitFor(() => expect(screen.getByTestId("audit-mcp-7")).toBeInTheDocument());
    // Server name + compact response size visible inline (no drawer needed).
    expect(screen.getByTestId("audit-mcp-7").textContent).toContain("github");
    expect(screen.getByTestId("audit-mcp-7").textContent).toContain("2.4k");
  });

  it("shows no MCP badge for a non-MCP audit row", async () => {
    installAdapter([
      {
        match: (u) => u === "/v1/audit",
        respond: () => ({
          items: [baseEntry],
          next_cursor: null,
          has_more: false,
          applied_scope: "t1",
        }),
      },
    ]);
    renderAudit();
    await waitFor(() => expect(screen.getByText("memory:update")).toBeInTheDocument());
    expect(screen.queryByTestId("audit-mcp-1")).toBeNull();
  });

  it("surfaces backend errors via Alert", async () => {
    apiClient.defaults.adapter = (config) =>
      Promise.reject({
        isAxiosError: true,
        response: {
          status: 500,
          data: { detail: { code: "INTERNAL", message: "boom" } },
        },
        message: "Request failed",
        config,
      });
    renderAudit();
    await waitFor(() => expect(screen.getByTestId("audit-error")).toBeInTheDocument());
  });
});
