/**
 * Settings · API Keys tests — Task 2 (api-key-viewable).
 *
 * Covers the four Task 2 changes: create-modal scope explanations, the
 * "查看" (view) reveal flow (success + the 404
 * ``API_KEY_PLAINTEXT_UNAVAILABLE`` dedicated copy), and the removal of
 * the page-local sidebar.
 */
import { describe, expect, it, vi, beforeEach } from "vitest";
import { MemoryRouter } from "react-router-dom";
import { App } from "antd";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import "../../i18n";

import { SettingsApiKeys } from "../SettingsApiKeys";
import { TenantScopeProvider } from "../../tenant/TenantScopeContext";
import { AuthProvider } from "../../auth/AuthContext";
import { apiClient, setStoredToken } from "../../api/client";

function makeJwt(payload: Record<string, unknown>): string {
  const header = btoa(JSON.stringify({ alg: "none", typ: "JWT" }));
  const body = btoa(JSON.stringify(payload));
  return `${header}.${body}.`;
}

interface RouteHandler {
  match: (url: string, method: string) => boolean;
  respond: (config: { data?: unknown }) => unknown;
  status?: number;
}

function installAdapter(handlers: RouteHandler[]) {
  apiClient.defaults.adapter = (config) => {
    const url = config.url ?? "";
    const method = (config.method ?? "get").toLowerCase();
    const handler = handlers.find((h) => h.match(url, method));
    const status = handler?.status ?? 200;
    if (status >= 400) {
      return Promise.reject({
        isAxiosError: true,
        response: { status, data: handler?.respond({ data: config.data }) },
        message: "request failed",
        config,
      });
    }
    return Promise.resolve({
      data: handler?.respond({ data: config.data }) ?? {},
      status,
      statusText: "OK",
      headers: {},
      config,
      request: {},
    });
  };
}

function renderPage() {
  setStoredToken(makeJwt({ sub: "u1", tenant_id: "t1", roles: ["admin"] }));
  return render(
    <MemoryRouter>
      <AuthProvider>
        <TenantScopeProvider>
          <App>
            <SettingsApiKeys />
          </App>
        </TenantScopeProvider>
      </AuthProvider>
    </MemoryRouter>,
  );
}

const saRow = {
  id: "sa-1",
  tenant_id: "t1",
  name: "sa_data_pipeline",
  description: "Data pipeline service account",
  is_active: true,
  created_by: "u1",
  created_at: "2026-05-26T10:00:00Z",
};

const activeKey = {
  id: "key-1",
  service_account_id: "sa-1",
  tenant_id: "t1",
  prefix: "ewk_ab12cd",
  scopes: ["read"],
  expires_at: null,
  last_used_at: null,
  revoked_at: null,
  rotated_at: null,
  grace_period_s: null,
  created_by: "u1",
  created_at: "2026-05-26T10:00:00Z",
};

function saListHandler(): RouteHandler {
  return {
    match: (u, m) => u === "/v1/service_accounts" && m === "get",
    respond: () => ({
      success: true,
      data: { items: [saRow], total: 1, cross_tenant: false },
      error: null,
    }),
  };
}

function keyListHandler(items: unknown[] = [activeKey]): RouteHandler {
  return {
    match: (u, m) => u === "/v1/api_keys" && m === "get",
    respond: () => ({
      success: true,
      data: { items, total: items.length, cross_tenant: false },
      error: null,
    }),
  };
}

beforeEach(() => {
  vi.restoreAllMocks();
});

describe("SettingsApiKeys", () => {
  it("no longer renders the page-local Service Accounts sidebar", async () => {
    installAdapter([keyListHandler([]), saListHandler()]);
    renderPage();
    await waitFor(() => expect(screen.getByTestId("api-keys-table")).toBeInTheDocument());
    expect(screen.queryByText("Service Accounts")).toBeNull();
    expect(screen.queryByText("Role Bindings")).toBeNull();
  });

  it("create modal shows all three scope explanations", async () => {
    installAdapter([keyListHandler([]), saListHandler()]);
    const user = userEvent.setup();
    renderPage();
    await waitFor(() => expect(screen.getByTestId("api-key-create-open")).toBeEnabled());
    await user.click(screen.getByTestId("api-key-create-open"));
    await waitFor(() => expect(screen.getByTestId("api-key-create-modal")).toBeInTheDocument());

    expect(
      screen.getByText("Read-only: query sessions, run results, the Agent list, and other GET endpoints"),
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        "Business write: everything read allows, plus invoking Agent runs, creating/continuing sessions, uploading files",
      ),
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        "Admin: manage service accounts and keys, change authorizations — do not share with third parties",
      ),
    ).toBeInTheDocument();
  });

  it("clicking view reveals the plaintext in a modal (mocked 200)", async () => {
    installAdapter([
      keyListHandler(),
      saListHandler(),
      {
        match: (u, m) => u === "/v1/api_keys/key-1/reveal" && m === "post",
        respond: () => ({ success: true, data: { plaintext: "ewk_ab12cd_the_full_secret" }, error: null }),
      },
    ]);
    const user = userEvent.setup();
    renderPage();
    await waitFor(() => expect(screen.getByTestId("api-key-view-key-1")).toBeInTheDocument());
    await user.click(screen.getByTestId("api-key-view-key-1"));
    await waitFor(() => expect(screen.getByTestId("api-key-view-modal")).toBeInTheDocument());
    expect(screen.getByText("ewk_ab12cd_the_full_secret")).toBeInTheDocument();
  });

  it("404 API_KEY_PLAINTEXT_UNAVAILABLE shows the dedicated hint instead of the modal", async () => {
    installAdapter([
      keyListHandler(),
      saListHandler(),
      {
        match: (u, m) => u === "/v1/api_keys/key-1/reveal" && m === "post",
        respond: () => ({
          detail: {
            code: "API_KEY_PLAINTEXT_UNAVAILABLE",
            message: "key predates reveal support; rotate to get a viewable one",
          },
        }),
        status: 404,
      },
    ]);
    const user = userEvent.setup();
    renderPage();
    await waitFor(() => expect(screen.getByTestId("api-key-view-key-1")).toBeInTheDocument());
    await user.click(screen.getByTestId("api-key-view-key-1"));
    await waitFor(() =>
      expect(
        screen.getByText("This key predates plaintext reveal support; rotate it to get a viewable key."),
      ).toBeInTheDocument(),
    );
    expect(screen.queryByTestId("api-key-view-modal")).toBeNull();
  });

  it("hides the view button for revoked and expired rows", async () => {
    const revoked = { ...activeKey, id: "key-revoked", revoked_at: "2026-06-01T00:00:00Z" };
    const expired = { ...activeKey, id: "key-expired", expires_at: "2020-01-01T00:00:00Z" };
    installAdapter([keyListHandler([activeKey, revoked, expired]), saListHandler()]);
    renderPage();
    await waitFor(() => expect(screen.getByTestId("api-key-view-key-1")).toBeInTheDocument());
    expect(screen.queryByTestId("api-key-view-key-revoked")).toBeNull();
    expect(screen.queryByTestId("api-key-view-key-expired")).toBeNull();
  });

  it("hides the view button for grace-window rows (backend vault entry is already gone)", async () => {
    const grace = {
      ...activeKey,
      id: "key-grace",
      rotated_at: new Date().toISOString(),
      grace_period_s: 300,
    };
    installAdapter([keyListHandler([activeKey, grace]), saListHandler()]);
    renderPage();
    await waitFor(() => expect(screen.getByTestId("api-key-view-key-1")).toBeInTheDocument());
    expect(screen.queryByTestId("api-key-view-key-grace")).toBeNull();
    // rotate/revoke stay visible for grace rows — only view is gated.
    expect(screen.getByTestId("api-key-rotate-key-grace")).toBeInTheDocument();
    expect(screen.getByTestId("api-key-revoke-key-grace")).toBeInTheDocument();
  });

  it("status column filter narrows the list to the selected status", async () => {
    const revokedKey = {
      ...activeKey,
      id: "key-2",
      prefix: "ewk_zz99yy",
      revoked_at: "2026-06-01T00:00:00Z",
    };
    installAdapter([saListHandler(), keyListHandler([activeKey, revokedKey])]);
    renderPage();
    await screen.findByText("ewk_ab12cd");

    const trigger = document.querySelector(".ant-table-filter-trigger");
    expect(trigger).not.toBeNull();
    await userEvent.click(trigger as HTMLElement);
    const dropdown = await waitFor(() => {
      const el = document.querySelector(".ant-table-filter-dropdown");
      expect(el).not.toBeNull();
      return el as HTMLElement;
    });
    await userEvent.click(within(dropdown).getByText(/已撤销|revoked/i));
    await userEvent.click(within(dropdown).getByText(/^OK$|^确\s?定$/));

    await waitFor(() => expect(screen.queryByText("ewk_ab12cd")).toBeNull());
    expect(screen.getByText("ewk_zz99yy")).toBeInTheDocument();
  });
});
