/**
 * Settings Ops tests — Stream H.4 PR 8.
 *
 * Both tenant_quotas + tenant_config backends are enveloped; the
 * adapter mocks deliver enveloped payloads.
 */
import { describe, expect, it, vi, beforeEach } from "vitest";
import { MemoryRouter } from "react-router-dom";
import { App } from "antd";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import "../../i18n";

import { SettingsTenantQuotas } from "../SettingsTenantQuotas";
import { SettingsTenantConfig } from "../SettingsTenantConfig";
import { TenantScopeProvider } from "../../tenant/TenantScopeContext";
import { AuthProvider } from "../../auth/AuthContext";
import { apiClient, setStoredToken } from "../../api/client";

vi.mock("@monaco-editor/react", () => {
  const Editor = ({
    value,
    onChange,
    ["data-testid"]: testId,
  }: {
    value: string;
    onChange?: (v: string | undefined) => void;
    "data-testid"?: string;
  }) => (
    <textarea
      data-testid={testId ?? "monaco-stub"}
      value={value}
      onChange={(e) => onChange?.(e.target.value)}
    />
  );
  return { default: Editor };
});

const TENANT = "00000000-0000-0000-0000-00000000acme";

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
    return Promise.resolve({
      data: handler?.respond({ data: config.data }) ?? {},
      status: handler?.status ?? 200,
      statusText: "OK",
      headers: {},
      config,
      request: {},
    });
  };
}

function renderQuotas() {
  setStoredToken(makeJwt({ sub: "u1", tenant_id: TENANT, roles: ["admin"] }));
  return render(
    <MemoryRouter>
      <AuthProvider>
        <TenantScopeProvider>
          <App>
            <SettingsTenantQuotas />
          </App>
        </TenantScopeProvider>
      </AuthProvider>
    </MemoryRouter>,
  );
}

function renderConfig() {
  setStoredToken(makeJwt({ sub: "u1", tenant_id: TENANT, roles: ["admin"] }));
  return render(
    <MemoryRouter>
      <AuthProvider>
        <TenantScopeProvider>
          <App>
            <SettingsTenantConfig />
          </App>
        </TenantScopeProvider>
      </AuthProvider>
    </MemoryRouter>,
  );
}

const quotaRow = {
  id: "q1",
  tenant_id: TENANT,
  dimension: "qps",
  scope: {},
  limit_value: 100,
  burst: 20,
  effective_from: "2026-05-26T10:00:00Z",
  effective_until: null,
  updated_by: "u1",
  updated_at: "2026-05-26T10:00:00Z",
};

const configRow = {
  tenant_id: TENANT,
  display_name: "Acme",
  plan: "pro" as const,
  credentials_mode: "platform" as const,
  model_credentials_ref: {},
  tool_credentials: {},
  mcp_allowlist: ["filesystem", "git"],
  rate_limit_override: {},
  pii_fields: [],
  http_tool_allowlist: ["https://api.github.com/*"],
  http_tool_denylist: ["internal.example.com"],
  mcp_servers: [],
  audit_retention_days: 90,
  event_log_retention_days: 30,
  created_at: "2026-05-20T10:00:00Z",
  updated_at: "2026-05-26T10:00:00Z",
  updated_by: "u1",
};

beforeEach(() => {
  vi.restoreAllMocks();
});

describe("SettingsTenantQuotas", () => {
  it("lists quotas + shows dimension/limit/burst", async () => {
    installAdapter([
      {
        match: (u) => u.endsWith("/quotas"),
        respond: () => ({ success: true, data: [quotaRow], error: null }),
      },
    ]);
    renderQuotas();
    await waitFor(() => expect(screen.getByText("qps")).toBeInTheDocument());
    expect(screen.getByText("100")).toBeInTheDocument();
    expect(screen.getByText("20")).toBeInTheDocument();
  });

  it("Create modal opens with dimension/limit/burst fields", async () => {
    installAdapter([
      {
        match: (u) => u.endsWith("/quotas"),
        respond: () => ({ success: true, data: [], error: null }),
      },
    ]);
    const user = userEvent.setup();
    renderQuotas();
    await waitFor(() => expect(screen.getByTestId("quota-create-btn")).toBeInTheDocument());
    await user.click(screen.getByTestId("quota-create-btn"));
    await waitFor(() => expect(screen.getByTestId("quota-limit-input")).toBeInTheDocument());
    expect(screen.getByTestId("quota-dimension-select")).toBeInTheDocument();
    expect(screen.getByTestId("quota-burst-input")).toBeInTheDocument();
  });

  it("table row exposes a Delete button per quota", async () => {
    installAdapter([
      {
        match: (u) => u.endsWith("/quotas"),
        respond: () => ({ success: true, data: [quotaRow], error: null }),
      },
    ]);
    renderQuotas();
    await waitFor(() => expect(screen.getByTestId("quota-delete-q1")).toBeInTheDocument());
  });
});

describe("SettingsTenantConfig", () => {
  it("renders the readonly card on first load", async () => {
    installAdapter([
      {
        match: (u) => u.endsWith("/config"),
        respond: () => ({ success: true, data: configRow, error: null }),
      },
    ]);
    renderConfig();
    await waitFor(() => expect(screen.getByText("Acme")).toBeInTheDocument());
    expect(screen.getByText("pro")).toBeInTheDocument();
    expect(screen.getByText("filesystem")).toBeInTheDocument();
    // E.8 — the per-tenant HTTP-tool denylist renders its hosts.
    expect(screen.getByTestId("tenant-config-http-denylist")).toHaveTextContent(
      "internal.example.com",
    );
  });

  it("shows file retention days or the platform default (B-63)", async () => {
    installAdapter([
      {
        match: (u) => u.endsWith("/config"),
        respond: () => ({
          success: true,
          data: { ...configRow, upload_retention_days: 30, artifact_retention_days: null },
          error: null,
        }),
      },
    ]);
    renderConfig();
    await waitFor(() => expect(screen.getByText("Acme")).toBeInTheDocument());
    expect(screen.getByTestId("tenant-config-upload-retention-days")).toHaveTextContent("30");
    expect(screen.getByTestId("tenant-config-artifact-retention-days")).toHaveTextContent(
      "Platform default",
    );
  });

  it("Edit button reveals the JSON editor + dirty detection", async () => {
    installAdapter([
      {
        match: (u) => u.endsWith("/config"),
        respond: () => ({ success: true, data: configRow, error: null }),
      },
    ]);
    const user = userEvent.setup();
    renderConfig();
    await waitFor(() => expect(screen.getByTestId("config-edit-btn")).toBeInTheDocument());
    await user.click(screen.getByTestId("config-edit-btn"));
    await waitFor(() => expect(screen.getByTestId("config-editor")).toBeInTheDocument());
    // Save initially disabled — pristine.
    expect(screen.getByTestId("config-save-btn")).toBeDisabled();
    // Touch the buffer.
    fireEvent.change(screen.getByTestId("config-editor"), {
      target: { value: '{"display_name":"Acme v2","plan":"enterprise","audit_retention_days":180,"event_log_retention_days":60}' },
    });
    await waitFor(() => expect(screen.getByTestId("config-save-btn")).not.toBeDisabled());
    expect(screen.getByTestId("config-dirty-tag")).toBeInTheDocument();
  });

  it("404 TENANT_CONFIG_NOT_FOUND surfaces empty state", async () => {
    apiClient.defaults.adapter = (config) =>
      Promise.reject({
        isAxiosError: true,
        response: {
          status: 404,
          data: {
            detail: { code: "TENANT_CONFIG_NOT_FOUND", message: "no tenant_config row exists for this tenant" },
          },
        },
        message: "404",
        config,
      });
    renderConfig();
    await waitFor(() => expect(screen.getByTestId("config-not-found")).toBeInTheDocument());
  });
});
