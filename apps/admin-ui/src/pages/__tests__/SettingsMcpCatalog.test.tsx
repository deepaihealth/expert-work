/**
 * MCP Catalog UI tests — Stream W.
 *
 * Covers the platform catalog page (admin gate + table), the platform-server
 * config form (edit/create) + tools tab (per-tool toggle), and the tenant
 * catalog browser (entitlement lock + enable toggle + oauth authorize).
 */
import { describe, expect, it, vi, beforeEach } from "vitest";
import { createRef } from "react";
import { MemoryRouter } from "react-router-dom";
import { App } from "antd";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import "../../i18n";

import { SettingsMcpCatalog } from "../SettingsMcpCatalog";
import { CatalogBrowser } from "../../components/mcp_catalog/CatalogBrowser";
import {
  CatalogConfigForm,
  type CatalogConfigFormHandle,
} from "../../components/mcp_catalog/CatalogConfigForm";
import { CatalogToolsTab } from "../../components/mcp_catalog/CatalogToolsTab";
import { AuthProvider } from "../../auth/AuthContext";
import { apiClient, setStoredToken } from "../../api/client";
import type { TenantCatalogEntry } from "../../api/mcp-catalog";

// Cross-tenant W3 — CatalogBrowser 的切入态判定 hook 会触 useTenantScope/
// useAuth(这里部分渲染不挂 Provider),mock 成 home 态(两态断言在
// AddMcpServerDrawer.test.tsx)。
vi.mock("../../tenant/useIsTenantSwitched", () => ({
  useIsTenantSwitched: () => false,
}));

const TENANT = "00000000-0000-0000-0000-00000000acme";

function makeJwt(payload: Record<string, unknown>): string {
  const header = btoa(JSON.stringify({ alg: "none", typ: "JWT" }));
  const body = btoa(JSON.stringify(payload));
  return `${header}.${body}.`;
}

interface RouteHandler {
  match: (url: string, method: string) => boolean;
  respond: () => unknown;
  status?: number;
}

function installAdapter(handlers: RouteHandler[]) {
  apiClient.defaults.adapter = (config) => {
    const url = config.url ?? "";
    const method = (config.method ?? "get").toLowerCase();
    const handler = handlers.find((h) => h.match(url, method));
    return Promise.resolve({
      data: handler?.respond() ?? {},
      status: handler?.status ?? 200,
      statusText: "OK",
      headers: {},
      config,
      request: {},
    });
  };
}

const ENTRY = {
  id: "cat-1",
  name: "github",
  display_name: "GitHub",
  description: "GitHub MCP connector",
  category: "dev-tools",
  icon: "",
  transport: "sse" as const,
  url_template: "https://mcp.github.com/sse",
  auth_type: "bearer" as const,
  required_tier: "pro" as const,
  enabled: true,
  created_at: "2026-05-01T10:00:00Z",
  updated_at: "2026-05-01T10:00:00Z",
  updated_by: "u1",
};

function renderCatalog(roles: string[]) {
  setStoredToken(makeJwt({ sub: "u1", tenant_id: TENANT, roles }));
  return render(
    <MemoryRouter>
      <AuthProvider>
        <App>
          <SettingsMcpCatalog />
        </App>
      </AuthProvider>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  vi.restoreAllMocks();
});

describe("SettingsMcpCatalog page", () => {
  it("non-system-admin sees the admin-only notice, no table", async () => {
    installAdapter([
      {
        match: (u) => u.endsWith("/mcp-catalog"),
        respond: () => ({ success: true, data: [], error: null }),
      },
    ]);
    renderCatalog(["admin"]);
    await waitFor(() =>
      expect(screen.getByTestId("cat-not-admin")).toBeInTheDocument(),
    );
    expect(screen.queryByTestId("cat-table")).not.toBeInTheDocument();
  });

  it("system_admin sees the catalog table with connector rows", async () => {
    installAdapter([
      {
        match: (u) => u.endsWith("/mcp-catalog"),
        respond: () => ({ success: true, data: [ENTRY], error: null }),
      },
    ]);
    renderCatalog(["system_admin"]);
    await waitFor(() =>
      expect(screen.getByTestId("cat-table")).toBeInTheDocument(),
    );
    expect(screen.getByText("GitHub")).toBeInTheDocument();
    expect(screen.getByTestId("cat-toggle-github")).toBeInTheDocument();
    expect(screen.getByTestId("cat-edit-github")).toBeInTheDocument();
    // Category column shows the i18n label, not the raw slug (ENTRY=dev-tools).
    expect(screen.getByText("Developer Tools")).toBeInTheDocument();
    expect(screen.queryByText("dev-tools")).not.toBeInTheDocument();
  });

  it("opening New connector reveals the tabbed form", async () => {
    installAdapter([
      {
        match: (u) => u.endsWith("/mcp-catalog"),
        respond: () => ({ success: true, data: [], error: null }),
      },
    ]);
    const user = userEvent.setup();
    renderCatalog(["system_admin"]);
    await waitFor(() =>
      expect(screen.getByTestId("cat-add")).toBeInTheDocument(),
    );
    await user.click(screen.getByTestId("cat-add"));
    await waitFor(() =>
      expect(screen.getByTestId("cce-form")).toBeInTheDocument(),
    );
    // Tabs force-render: basic (name/url) + auth (auth_type) fields are present.
    expect(screen.getByTestId("cce-name")).toBeInTheDocument();
    expect(screen.getByTestId("cce-auth")).toBeInTheDocument();
  });
});

describe("CatalogConfigForm", () => {
  it("disables the immutable name/transport/auth_type when editing", async () => {
    render(
      <App>
        <CatalogConfigForm editing={ENTRY} onSaved={() => {}} />
      </App>,
    );
    await waitFor(() =>
      expect(screen.getByTestId("cce-form")).toBeInTheDocument(),
    );
    expect(screen.getByTestId("cce-auth").className).toContain(
      "ant-select-disabled",
    );
    expect(screen.getByTestId("cce-name")).toBeDisabled();
    expect(screen.getByTestId("cce-transport").className).toContain(
      "ant-select-disabled",
    );
  });

  it("submit() PATCHes, omitting immutable auth_type / name and a blank token", async () => {
    let captured: { method?: string; body?: unknown } = {};
    apiClient.defaults.adapter = (config) => {
      if (config.method === "patch") {
        captured = { method: config.method, body: config.data };
      }
      return Promise.resolve({
        data: { success: true, data: { ...ENTRY }, error: null },
        status: 200,
        statusText: "OK",
        headers: {},
        config,
        request: {},
      });
    };
    const onSaved = vi.fn();
    const ref = createRef<CatalogConfigFormHandle>();
    render(
      <App>
        <CatalogConfigForm ref={ref} editing={ENTRY} onSaved={onSaved} />
      </App>,
    );
    await waitFor(() =>
      expect(screen.getByTestId("cce-form")).toBeInTheDocument(),
    );
    await ref.current!.submit();
    await waitFor(() => expect(onSaved).toHaveBeenCalled());
    expect(captured.method).toBe("patch");
    const body = JSON.parse(captured.body as string) as Record<string, unknown>;
    expect(body.auth_type).toBeUndefined();
    expect(body.name).toBeUndefined();
    expect(body.bearer_token).toBeUndefined();
  });

  it("create mode renders category select, icon upload, and timeout fields", async () => {
    render(
      <App>
        <CatalogConfigForm editing={null} onSaved={() => {}} />
      </App>,
    );
    await waitFor(() =>
      expect(screen.getByTestId("cce-form")).toBeInTheDocument(),
    );
    expect(screen.getByTestId("cce-category")).toBeInTheDocument();
    expect(screen.getByTestId("cce-icon-upload")).toBeInTheDocument();
    expect(screen.getByTestId("cce-timeout")).toBeInTheDocument();
    expect(screen.getByTestId("cce-sse-timeout")).toBeInTheDocument();
  });
});

describe("CatalogToolsTab", () => {
  it("probes and lists tools with a per-tool enable toggle", async () => {
    installAdapter([
      {
        match: (u) => u.endsWith("/tools"),
        respond: () => ({
          success: true,
          data: {
            status: "ok",
            tool_count: 1,
            tools: [
              {
                name: "list_issues",
                description: "List issues",
                input_schema: {},
                disabled: false,
              },
            ],
            error: null,
          },
          error: null,
        }),
      },
    ]);
    render(
      <App>
        <CatalogToolsTab entry={ENTRY} onUpdated={() => {}} />
      </App>,
    );
    await waitFor(() =>
      expect(screen.getByTestId("ct-list")).toBeInTheDocument(),
    );
    expect(screen.getByText("list_issues")).toBeInTheDocument();
    expect(screen.getByTestId("ct-toggle-list_issues")).toBeInTheDocument();
  });

  it("toggling a tool persists disabled_tools via PATCH", async () => {
    let patched: unknown = null;
    apiClient.defaults.adapter = (config) => {
      const url = config.url ?? "";
      if (config.method === "patch") patched = config.data;
      const data = url.endsWith("/tools")
        ? {
            success: true,
            data: {
              status: "ok",
              tool_count: 1,
              tools: [
                {
                  name: "drive",
                  description: "",
                  input_schema: {},
                  disabled: false,
                },
              ],
              error: null,
            },
            error: null,
          }
        : { success: true, data: { ...ENTRY, disabled_tools: ["drive"] }, error: null };
      return Promise.resolve({
        data,
        status: 200,
        statusText: "OK",
        headers: {},
        config,
        request: {},
      });
    };
    const user = userEvent.setup();
    render(
      <App>
        <CatalogToolsTab entry={ENTRY} onUpdated={() => {}} />
      </App>,
    );
    await waitFor(() =>
      expect(screen.getByTestId("ct-toggle-drive")).toBeInTheDocument(),
    );
    await user.click(screen.getByTestId("ct-toggle-drive"));
    await waitFor(() => expect(patched).not.toBeNull());
    const body = JSON.parse(patched as string) as { disabled_tools: string[] };
    expect(body.disabled_tools).toContain("drive");
  });
});

describe("CatalogBrowser entitlement lock", () => {
  function makeEntry(over: Partial<TenantCatalogEntry>): TenantCatalogEntry {
    return { ...ENTRY, entitled: true, tenant_enabled: false, ...over };
  }

  const noop = {
    onToggleEnable: async () => {},
    onAuthorize: () => {},
  };

  it("entitled entry exposes an enable toggle", () => {
    render(
      <App>
        <CatalogBrowser
          entries={[makeEntry({ id: "e1", name: "ok", entitled: true })]}
          loading={false}
          error={null}
          {...noop}
        />
      </App>,
    );
    expect(screen.getByTestId("cb-toggle-ok")).toBeEnabled();
  });

  it("non-entitled entry shows a lock badge instead of a toggle", () => {
    render(
      <App>
        <CatalogBrowser
          entries={[
            makeEntry({
              id: "e2",
              name: "locked",
              entitled: false,
              required_tier: "enterprise",
            }),
          ]}
          loading={false}
          error={null}
          {...noop}
        />
      </App>,
    );
    expect(screen.getByTestId("cb-locked-locked")).toBeDisabled();
    expect(screen.queryByTestId("cb-toggle-locked")).not.toBeInTheDocument();
  });

  it("oauth2 entry surfaces Authorize only once enabled", () => {
    const { rerender } = render(
      <App>
        <CatalogBrowser
          entries={[
            makeEntry({
              id: "e3",
              name: "lin",
              auth_type: "oauth2",
              tenant_enabled: false,
            }),
          ]}
          loading={false}
          error={null}
          {...noop}
        />
      </App>,
    );
    expect(screen.queryByTestId("cb-authorize-lin")).not.toBeInTheDocument();
    rerender(
      <App>
        <CatalogBrowser
          entries={[
            makeEntry({
              id: "e3",
              name: "lin",
              auth_type: "oauth2",
              tenant_enabled: true,
            }),
          ]}
          loading={false}
          error={null}
          {...noop}
        />
      </App>,
    );
    expect(screen.getByTestId("cb-authorize-lin")).toBeInTheDocument();
  });
});
